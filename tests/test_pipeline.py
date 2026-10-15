"""Offline tests: feed parsing, full-text extraction, and the full run with a fake API.
Run:  python -m pytest -q tests   (or: python tests/test_pipeline.py)
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FX = ROOT / "tests" / "fixtures"

from digest import arxiv_feed, llm  # noqa: E402


def test_parse_feed():
    date, papers = arxiv_feed.parse_feed((FX / "feed_2026-10-14.xml").read_bytes())
    assert date == "2026-10-14"
    assert len(papers) == 11
    p = {p.id: p for p in papers}
    assert p["2610.17263"].title.startswith("Neutrino Tridents and $W$-boson")
    assert p["2610.17263"].authors[1] == "Toni Mäkelä"
    assert p["2610.15917"].authors[0] == "Hikmet Özşahin"
    assert p["2610.16496"].announce_type == "cross"
    assert p["2505.08871"].announce_type == "replace" and p["2505.08871"].version == "v4"
    assert p["2610.16836"].abstract.startswith("Neutrinos and muons")


def test_empty_feed():
    date, papers = arxiv_feed.parse_feed((FX / "feed_empty.xml").read_bytes())
    assert date is None and papers == []


def test_fulltext_extraction():
    p = arxiv_feed._LatexmlText()
    p.feed((FX / "paper.html").read_text())
    text = arxiv_feed._select_text([tuple(s) for s in p.sections], 45000)
    assert "## 1 Introduction" in text and "INTROTEXT" in text
    assert "$E_\\nu\\sim 1$" in text            # math kept as LaTeX
    assert "METHODTEXT" in text and "CONCLTEXT" in text
    for junk in ("FIGCAPTION", "BIBTEXT", "APPENDIX", "EQTABLE", "Contents junk", "semantics"):
        assert junk not in text, junk


def make_fake_gemini_req(calls):
    """Stand-in for GeminiBackend._req: model listing + generateContent with a quota error."""
    import io
    import re
    import urllib.error

    def _req(self, method, path, body=None, timeout=300):
        calls.append(path)
        if path.startswith("models?"):
            names = ["gemini-4-argon", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.8-flash-tts",
                     "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-latest"]
            return {"models": [{"name": "models/" + n, "supportedGenerationMethods": ["generateContent"]} for n in names]}
        model = path.split("/")[1].split(":")[0]
        if model == "gemini-3.8-flash":   # newest Flash not on free tier -> must fall back
            raise urllib.error.HTTPError(path, 429, "quota", {}, io.BytesIO(
                b'{"error":{"message":"Quota exceeded for metric generate_content_free_tier_requests, limit: 0"}}'))
        schema = body["generationConfig"]["responseJsonSchema"]
        assert schema["type"] == "object"
        user = body["contents"][0]["parts"][0]["text"]
        if "scores" in schema["properties"]:
            ids = re.findall(r"^\[(\S+)\]", user, re.M)
            data = {"scores": [{"id": i, "score": 9 if "99999" in i or "16836" in i else 6 if "17263" in i else 2,
                                "topic": "test topic", "reason": f"reason for {i}"} for i in ids]}
        else:
            ids = re.findall(r'<paper id="(\S+)">', user)
            data = {"summaries": [{"id": i, "tldr": "TLDR", "novel_idea": "NOVEL", "method": "METHOD",
                                   "key_results": ["R1", "R2"], "why_it_matters": "WHY", "caveats": "CAV",
                                   "read_priority": "must-read"} for i in ids]}
        return {"candidates": [{"content": {"parts": [{"text": "```json\n" + json.dumps(data) + "\n```"}]}}],
                "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 10}}
    return _req


def test_full_run_with_fake_api(monkeypatch=None):
    tmp = Path(tempfile.mkdtemp())
    for f in ("run.py", "config.yaml"):
        shutil.copy(ROOT / f, tmp / f)
    shutil.copytree(ROOT / "digest", tmp / "digest")
    shutil.copytree(ROOT / "docs" / "assets", tmp / "docs" / "assets")
    sys.path.insert(0, str(tmp))
    import importlib
    import run as runmod
    importlib.reload(runmod)
    calls = []
    runmod.llm.GeminiBackend._req = make_fake_gemini_req(calls)
    runmod.llm.time.sleep = lambda s: None
    runmod.arxiv_feed.fetch_fulltext = lambda pid, budget=0: ("FULL TEXT", "fulltext-html")
    import os
    os.environ["GEMINI_API_KEY"] = "test"
    sys.argv = ["run.py", "--feed-file", str(FX / "feed_2026-10-14.xml"), "--no-email"]
    assert runmod.main() == 0
    rec = json.loads((tmp / "docs/data/2026-10-14.json").read_text())
    assert rec["models"] == {"rank": "gemini-3.5-flash-lite", "summary": "gemini-3.6-flash"}, rec["models"]
    gen = [c for c in calls if ":generateContent" in c]
    assert gen.count("models/gemini-3.8-flash:generateContent") == 1   # tried once, then skipped
    assert len(gen) == 3   # 1 ranking batch + 1 failed + 1 batched summary call
    ids = [p["id"] for p in rec["papers"]]
    assert "2505.08871" not in ids                       # replacement excluded
    assert "2610.16496" in ids                           # cross-list included
    assert ids[0] == "2610.99999"                        # 9 + favourite-author boost -> top
    summarized = [p["id"] for p in rec["papers"] if p["summary"]]
    assert set(summarized) == {"2610.99999", "2610.16836", "2610.17263"}
    html = (tmp / "docs/index.html").read_text()
    assert "NOVEL" in html and "Why it matters to you" in html and "Wed 14 Oct 2026" in html
    assert (tmp / "docs/days/2026-10-14.html").exists() and (tmp / "docs/archive.html").exists()
    # second run is a no-op (this is what prevents stale/duplicate days)
    n = len(calls)
    assert runmod.main() == 0 and len(calls) == n
    # empty feed (weekend) is a no-op
    sys.argv = ["run.py", "--feed-file", str(FX / "feed_empty.xml"), "--no-email"]
    assert runmod.main() == 0
    os.environ.pop("GEMINI_API_KEY")
    sys.path.remove(str(tmp))
    shutil.rmtree(tmp)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
