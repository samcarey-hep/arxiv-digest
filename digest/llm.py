"""Ranking and summarization with the Anthropic API.

- Ranking: a small/fast model scores every abstract 0-10 against your interest profile.
- Summaries: a stronger model reads the paper's full text (intro, conclusions, and the
  start of each section) and writes a structured summary.
Model ids in config.yaml can be explicit ("claude-sonnet-4-5") or "auto-haiku" /
"auto-sonnet" / "auto-opus", which pick the newest available model of that family.
"""
from __future__ import annotations

import json
import os
import re
import sys

from .arxiv_feed import Paper

FALLBACK_MODELS = {"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-4-5", "opus": "claude-opus-4-1"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get_client():
    import anthropic
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=5, timeout=300)


def resolve_model(client, name: str) -> str:
    if not name.startswith("auto-"):
        return name
    family = name.split("-", 1)[1]
    try:
        ids = [m.id for m in client.models.list(limit=100).data]   # newest first
        for mid in ids:
            if family in mid:
                return mid
    except Exception as e:  # noqa: BLE001
        log(f"[models] list failed ({e}); using fallback")
    return FALLBACK_MODELS[family]


def _system(cfg: dict) -> list[dict]:
    authors = ", ".join(cfg.get("favorite_authors", []))
    text = (
        "You are a research assistant for a theoretical neutrino physicist. "
        "Their interest profile follows.\n\n<interests>\n" + cfg["interests"].strip() +
        "\n</interests>\n\n<favorite_authors>" + authors + "</favorite_authors>\n\n"
        "Be concrete and technically precise; the reader has a physics PhD. "
        "Use LaTeX in $...$ for math. Never invent results that are not in the provided text."
    )
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _tool_call(client, model, system, user, tool, max_tokens):
    resp = client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        tools=[tool], tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": user}],
    )
    for b in resp.content:
        if b.type == "tool_use":
            return b.input, resp.usage
    raise RuntimeError("model did not call the tool")


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


def rank(client, model: str, cfg: dict, papers: list[Paper], batch_size: int = 40) -> dict:
    usage = {"input": 0, "output": 0}
    system = _system(cfg)
    for i in range(0, len(papers), batch_size):
        batch = papers[i:i + batch_size]
        listing = "\n\n".join(
            f"[{p.id}] ({', '.join(p.categories)})\nTitle: {p.title}\nAuthors: {', '.join(p.authors[:12])}"
            f"{' et al.' if len(p.authors) > 12 else ''}\nAbstract: {p.abstract[:1500]}"
            for p in batch
        )
        try:
            out, u = _tool_call(client, model, system, RANK_RUBRIC + "\n\n<papers>\n" + listing + "\n</papers>",
                                RANK_TOOL, max_tokens=8000)
            usage["input"] += u.input_tokens; usage["output"] += u.output_tokens
            by_id = {s["id"].strip().strip("[]"): s for s in out.get("scores", [])}
        except Exception as e:  # noqa: BLE001
            log(f"[rank] batch {i} failed: {e}; falling back to keywords")
            by_id = {}
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


def apply_boosts(papers: list[Paper], cfg: dict) -> None:
    fav = [a.lower() for a in cfg.get("favorite_authors", [])]
    for p in papers:
        names = " | ".join(p.authors).lower()
        if any(re.search(r"\b" + re.escape(f) + r"\b", names) for f in fav):
            p.score = min(10.0, p.score + cfg.get("favorite_author_boost", 1.5))
            p.topic = p.topic or "favorite author"


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


def summarize(client, model: str, cfg: dict, p: Paper, fulltext: str) -> tuple[dict, object]:
    body = fulltext if fulltext else "(Full text unavailable; summarize from the abstract only and say so in caveats.)"
    user = (
        f"Summarize this arXiv paper for the reader.\n\narXiv:{p.id} [{', '.join(p.categories)}]\n"
        f"Title: {p.title}\nAuthors: {', '.join(p.authors[:20])}{' et al.' if len(p.authors) > 20 else ''}\n\n"
        f"Abstract: {p.abstract}\n\n<paper_text>\n{body}\n</paper_text>"
    )
    return _tool_call(client, model, _system(cfg), user, SUM_TOOL, max_tokens=2000)


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
