"""Ranking and summarization with an LLM.

Two backends, chosen by `provider` in config.yaml:
  - gemini    (default): Google Gemini API free tier. Key from aistudio.google.com, no card.
  - anthropic          : Claude via the Anthropic API (paid, prepaid credit).
Both are called through `Backend.call(system, user, tool, max_tokens)`, which returns
(json_dict, (input_tokens, output_tokens)).

Model names may be explicit ("gemini-3.6-flash", "claude-sonnet-4-5") or "auto-<family>":
auto-flash / auto-flash-lite for Gemini, auto-haiku / auto-sonnet / auto-opus for Claude,
which pick the newest available model of that family (falling back to older ones if the
newest isn't on the free tier).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from .arxiv_feed import Paper


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class QuotaExhausted(RuntimeError):
    """Daily free quota used up: stop calling the API for this run."""


# ============================================================ Anthropic backend

class AnthropicBackend:
    FALLBACK = {"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-4-5", "opus": "claude-opus-4-1"}
    parallel = 4

    def __init__(self, cfg):
        import anthropic
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=5, timeout=300)

    def resolve(self, name: str) -> list[str]:
        if not name.startswith("auto-"):
            return [name]
        fam = name.split("-", 1)[1]
        try:
            ids = [m.id for m in self.client.models.list(limit=100).data if fam in m.id]   # newest first
            if ids:
                return ids[:3]
        except Exception as e:  # noqa: BLE001
            log(f"[models] list failed ({e}); using fallback")
        return [self.FALLBACK[fam]]

    def call(self, model, system, user, tool, max_tokens):
        resp = self.client.messages.create(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=[tool], tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": user}],
        )
        for b in resp.content:
            if b.type == "tool_use":
                return b.input, (resp.usage.input_tokens, resp.usage.output_tokens)
        raise RuntimeError("model did not call the tool")


# =============================================================== Gemini backend

def _gemini_schema(js: dict) -> dict:
    """JSON schema -> Gemini responseSchema (OpenAPI subset, upper-case types)."""
    out = {"type": js["type"].upper()}
    for k in ("description", "enum"):
        if k in js:
            out[k] = js[k]
    if "items" in js:
        out["items"] = _gemini_schema(js["items"])
    if "properties" in js:
        out["properties"] = {k: _gemini_schema(v) for k, v in js["properties"].items()}
        out["propertyOrdering"] = list(js["properties"])
        out["required"] = js.get("required", list(js["properties"]))
    return out


def _version(name: str) -> float:
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


class GeminiBackend:
    BASE = "https://generativelanguage.googleapis.com/v1beta"
    FALLBACK = {"flash": ["gemini-flash-latest", "gemini-2.5-flash"],
                "flash-lite": ["gemini-flash-lite-latest", "gemini-2.5-flash-lite"],
                "pro": ["gemini-2.5-pro"]}
    parallel = 1   # free tier has low requests-per-minute

    def __init__(self, cfg):
        self.key = os.environ["GEMINI_API_KEY"]
        self.min_gap = float(cfg.get("gemini", {}).get("seconds_between_calls", 8))
        self._last = 0.0
        self.dead: set[str] = set()   # models with no free quota left today

    def _req(self, method, path, body=None, timeout=300):
        req = urllib.request.Request(f"{self.BASE}/{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"x-goog-api-key": self.key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def resolve(self, name: str) -> list[str]:
        """Return candidate model ids, best first."""
        if not name.startswith("auto-"):
            return [name]
        fam = name.split("-", 1)[1]                      # flash | flash-lite | pro
        pat = re.compile(rf"^gemini-\d+(?:\.\d+)?-{re.escape(fam)}$")
        try:
            names, token = [], ""
            while True:
                d = self._req("GET", "models?pageSize=1000" + (f"&pageToken={token}" if token else ""), timeout=60)
                names += [m["name"].split("/", 1)[1] for m in d.get("models", [])
                          if "generateContent" in m.get("supportedGenerationMethods", [])]
                token = d.get("nextPageToken")
                if not token:
                    break
            cands = sorted({n for n in names if pat.match(n)}, key=_version, reverse=True)
            alias = [a for a in self.FALLBACK[fam] if a in names]
            if cands or alias:
                return cands[:3] + alias
        except Exception as e:  # noqa: BLE001
            log(f"[models] list failed ({e}); using fallback")
        return self.FALLBACK[fam]

    def call(self, model, system, user, tool, max_tokens):
        if model in self.dead:
            raise QuotaExhausted(model)
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseJsonSchema": tool["input_schema"],     # standard JSON Schema
                                 "maxOutputTokens": max(max_tokens, 32768), "temperature": 0.3},
        }
        legacy_schema = False
        d = None
        for attempt in range(5):
            wait = self.min_gap - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                d = self._req("POST", f"models/{model}:generateContent", body)
                break
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")
                if e.code == 429:
                    if re.search(r"PerDay|per day|limit: 0", msg, re.I):
                        self.dead.add(model)
                        raise QuotaExhausted(f"{model}: {msg[:300]}")
                    m = re.search(r'"retryDelay":\s*"(\d+)', msg)
                    delay = int(m.group(1)) + 2 if m else 30 * (attempt + 1)
                    log(f"[gemini] rate limited; sleeping {delay}s")
                    time.sleep(delay)
                    continue
                if e.code == 400 and not legacy_schema and "schema" in msg.lower():
                    # older API surface: fall back to the OpenAPI-style responseSchema field
                    gc = body["generationConfig"]
                    gc["responseSchema"] = _gemini_schema(gc.pop("responseJsonSchema"))
                    legacy_schema = True
                    continue
                if e.code in (500, 502, 503, 504):
                    if attempt >= 2:
                        break          # persistently overloaded: give up on this model for today
                    time.sleep(20 * (attempt + 1))
                    continue
                if e.code in (403, 404):
                    self.dead.add(model)
                raise RuntimeError(f"Gemini HTTP {e.code} for {model}: {msg[:400]}")
        else:
            d = None
        if d is None:
            self.dead.add(model)   # skip this model for the rest of the run
            raise RuntimeError(f"Gemini: gave up after retries ({model})")
        cand = (d.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
        if not text:
            raise RuntimeError(f"Gemini returned no text (finishReason={cand.get('finishReason')}, "
                               f"block={d.get('promptFeedback', {}).get('blockReason')})")
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        um = d.get("usageMetadata", {})
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"bad JSON from {model} (finishReason={cand.get('finishReason')}): {e}")
        return parsed, (um.get("promptTokenCount", 0),
                                  um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0))


# ================================================================== dispatch

def get_backend(cfg):
    prov = cfg.get("provider", "gemini")
    return GeminiBackend(cfg) if prov == "gemini" else AnthropicBackend(cfg)


def api_key_present(cfg) -> bool:
    return bool(os.environ.get("GEMINI_API_KEY" if cfg.get("provider", "gemini") == "gemini" else "ANTHROPIC_API_KEY"))


class Caller:
    """Calls the first working model from a candidate list; remembers the winner."""

    def __init__(self, backend, candidates: list[str]):
        self.b, self.cands, self.used = backend, list(candidates), None

    def __call__(self, system, user, tool, max_tokens):
        """Try each candidate model in turn. Any failure (quota, overload, timeout, bad or
        truncated JSON) moves on to the next model; models out of quota are dropped for the
        rest of the run."""
        last = None
        for m in list(self.cands):
            try:
                out = self.b.call(m, system, user, tool, max_tokens)
                self.used = m
                return out
            except Exception as e:  # noqa: BLE001
                last = e
                log(f"[llm] {m} failed: {type(e).__name__}: {str(e)[:300]}")
                if isinstance(e, QuotaExhausted) or m in getattr(self.b, "dead", set()):
                    if m in self.cands:
                        self.cands.remove(m)
        if not self.cands:
            raise QuotaExhausted(f"no usable model left: {last}")
        raise RuntimeError(f"all models failed for this request: {last}")


def _system(cfg: dict) -> str:
    authors = ", ".join(cfg.get("favorite_authors", []))
    return (
        "You are a research assistant for a theoretical neutrino physicist. "
        "Their interest profile follows.\n\n<interests>\n" + cfg["interests"].strip() +
        "\n</interests>\n\n<favorite_authors>" + authors + "</favorite_authors>\n\n"
        "Be concrete and technically precise; the reader has a physics PhD. "
        "Use LaTeX in $...$ for math. Never invent results that are not in the provided text."
    )


# ------------------------------------------------------------------ ranking

RANK_TOOL = {
    "name": "record_scores",
    "description": "Record a relevance score for every paper in the batch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "score": {"type": "number", "description": "0-10 relevance to this reader"},
                        "topic": {"type": "string", "description": "2-4 word topic label"},
                        "reason": {"type": "string", "description": "One sentence: what the paper does and why it is (or is not) relevant to the reader"},
                    },
                    "required": ["id", "score", "topic", "reason"],
                },
            }
        },
        "required": ["scores"],
    },
}

RANK_RUBRIC = """Score each paper 0-10 for how much THIS reader would want to read it today.
10 = squarely in their core research (would read today); 7-9 = clearly relevant;
4-6 = adjacent / worth a glance; 1-3 = same field, little relevance; 0 = unrelated.
Judge by the substance, not keyword overlap. Novel ideas and strong results in core
areas deserve the top of the scale; routine model-building with a passing mention of
neutrinos does not. Return a score for every paper id given."""


def rank(call: Caller, cfg: dict, papers: list[Paper], batch_size: int = 40) -> dict:
    usage = {"input": 0, "output": 0}
    system = _system(cfg)
    quota_gone = False
    for i in range(0, len(papers), batch_size):
        batch = papers[i:i + batch_size]
        listing = "\n\n".join(
            f"[{p.id}] ({', '.join(p.categories)})\nTitle: {p.title}\nAuthors: {', '.join(p.authors[:12])}"
            f"{' et al.' if len(p.authors) > 12 else ''}\nAbstract: {p.abstract[:1500]}"
            for p in batch
        )
        by_id = {}
        if not quota_gone:
            try:
                out, u = call(system, RANK_RUBRIC + "\n\n<papers>\n" + listing + "\n</papers>", RANK_TOOL, 8000)
                usage["input"] += u[0]; usage["output"] += u[1]
                by_id = {str(s["id"]).strip().strip("[]").replace("arXiv:", ""): s for s in out.get("scores", [])}
            except QuotaExhausted as e:
                log(f"[rank] quota exhausted ({e}); keyword ranking for the rest")
                quota_gone = True
            except Exception as e:  # noqa: BLE001
                log(f"[rank] batch {i} failed: {e}; falling back to keywords")
        for p in batch:
            s = by_id.get(p.id)
            if s:
                p.score, p.topic, p.reason = float(s["score"]), s.get("topic", ""), s.get("reason", "")
            else:
                p.score = keyword_score(p, cfg)
                p.reason = p.reason or "(keyword-ranked)"
    return usage


def keyword_score(p: Paper, cfg: dict) -> float:
    """Cheap fallback / dry-run ranking: weighted keyword hits."""
    text = (p.title + " " + p.abstract).lower()
    score = 0.0
    for kw, w in (cfg.get("keywords") or {}).items():
        if kw.lower() in text:
            score += w
    return min(10.0, score)


def _norm(name: str) -> list[str]:
    import unicodedata
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z]+(?:-[a-z]+)*", n)


def author_matches(entry: str, author: str) -> bool:
    """'Machado' matches any author surnamed Machado; 'Pedro Machado' also matches
    'Pedro A. N. Machado' and 'P. Machado' (first name or initial + surname). Accents ignored;
    multi-word surnames like 'De Vries' or 'de Gouvea' work."""
    e, a = _norm(entry), _norm(author)
    if not e or not a:
        return False
    if len(e) == 1:                                   # surname only
        return e[0] == a[-1] or (len(a) > 1 and e[0] == "-".join(a[-2:]))
    # try the entry's last 1-2 words as the surname
    for k in (2, 1):
        if len(e) <= k - 1:
            continue
        sur, first = e[-k:], e[:-k]
        if a[-k:] == sur:
            if not first:
                return True
            given = a[:-k]
            if given and (given[0] == first[0] or (len(given[0]) == 1 and given[0] == first[0][0])):
                return True
    return False


def apply_boosts(papers: list[Paper], cfg: dict) -> None:
    fav = [str(a).strip() for a in (cfg.get("favorite_authors") or []) if a and str(a).strip()]
    boost = cfg.get("favorite_author_boost", 1.5)
    for p in papers:
        hit = next((f for f in fav for au in p.authors if author_matches(f, au)), None)
        if hit:
            p.score = min(10.0, p.score + boost)
            p.topic = p.topic or "favorite author"
            p.reason = (p.reason + f" [favorite author: {hit}]").strip()


# ---------------------------------------------------------------- summaries

SUM_TOOL = {
    "name": "record_summary",
    "description": "Record a structured summary of the paper.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tldr": {"type": "string", "description": "One sentence, <= 35 words: the main claim."},
            "novel_idea": {"type": "string", "description": "2-4 sentences: what is genuinely new compared with prior work, as the paper positions it."},
            "method": {"type": "string", "description": "2-4 sentences: approach, framework, data/simulation, key approximations."},
            "key_results": {"type": "array", "items": {"type": "string"}, "description": "2-5 bullet points with concrete numbers, limits or plots where available."},
            "why_it_matters": {"type": "string", "description": "1-3 sentences on the relevance to THIS reader's research (be specific: which of their projects/tools it touches) or say plainly if it's only tangential."},
            "caveats": {"type": "string", "description": "1-2 sentences: limitations, assumptions, open questions or things to check."},
            "read_priority": {"type": "string", "enum": ["must-read", "worth reading", "skim"]},
        },
        "required": ["tldr", "novel_idea", "method", "key_results", "why_it_matters", "caveats", "read_priority"],
    },
}


SUMS_TOOL = {
    "name": "record_summaries",
    "description": "Record one structured summary per paper.",
    "input_schema": {
        "type": "object",
        "properties": {"summaries": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "arXiv id exactly as given"},
                           **SUM_TOOL["input_schema"]["properties"]},
            "required": ["id"] + SUM_TOOL["input_schema"]["required"]}}},
        "required": ["summaries"],
    },
}


def _paper_block(p: Paper, fulltext: str) -> str:
    body = fulltext if fulltext else "(Full text unavailable; summarize from the abstract only and say so in caveats.)"
    return (f"<paper id=\"{p.id}\">\narXiv:{p.id} [{', '.join(p.categories)}]\nTitle: {p.title}\n"
            f"Authors: {', '.join(p.authors[:20])}{' et al.' if len(p.authors) > 20 else ''}\n\n"
            f"Abstract: {p.abstract}\n\n<paper_text>\n{body}\n</paper_text>\n</paper>")


def summarize(call: Caller, cfg: dict, p: Paper, fulltext: str):
    """One paper per request."""
    return call(_system(cfg), "Summarize this arXiv paper for the reader.\n\n" + _paper_block(p, fulltext),
                SUM_TOOL, 2000)


def summarize_many(call: Caller, cfg: dict, items: list[tuple[Paper, str]]):
    """Several papers in one request (keeps free-tier request counts low).
    Returns ({id: summary}, usage)."""
    user = (f"Summarize each of these {len(items)} arXiv papers separately for the reader. "
            "Return one entry per paper, using its id.\n\n" + "\n\n".join(_paper_block(p, t) for p, t in items))
    out, u = call(_system(cfg), user, SUMS_TOOL, 4000 * len(items))
    res = {}
    for s in out.get("summaries", []):
        sid = str(s.pop("id", "")).replace("arXiv:", "").strip()
        res[sid] = s
    return res, u


def abstract_only_summary(p: Paper) -> dict:
    """Used in --dry-run (no API key) so the site can be previewed."""
    sents = re.split(r"(?<=[.!?])\s+", p.abstract)
    return {
        "tldr": sents[0] if sents else p.title,
        "novel_idea": " ".join(sents[1:3]),
        "method": " ".join(sents[3:5]),
        "key_results": sents[5:8] or ["(dry run: no model summary)"],
        "why_it_matters": "(dry run: relevance note is written by the model in real runs)",
        "caveats": "Dry-run preview generated from the abstract only.",
        "read_priority": "worth reading" if p.score >= 7 else "skim",
    }
