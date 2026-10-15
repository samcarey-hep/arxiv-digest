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


class FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, model, max_tokens, system, tools, tool_choice, messages):
        self.calls.append(model)
        name = tools[0]["name"]
        user = messages[0]["content"]
        if name == "record_scores":
            import re
            ids = re.findall(r"^\[(\S+)\]", user, re.M)
            data = {"scores": [{"id": i, "score": 9 if "99999" in i or "16836" in i else 6 if "17263" in i else 2,
                                "topic": "test topic", "reason": f"reason for {i}"} for i in ids]}
        else:
            data = {"tldr": "TLDR", "novel_idea": "NOVEL", "method": "METHOD", "key_results": ["R1", "R2"],
                    "why_it_matters": "WHY", "caveats": "CAV", "read_priority": "must-read"}
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=data)],
                               usage=SimpleNamespace(input_tokens=100, output_tokens=10))


class FakeClient:
    def __init__(self):
        self.messages = FakeMessages()
        self.models = SimpleNamespace(list=lambda limit=100: SimpleNamespace(
            data=[SimpleNamespace(id="claude-opus-9"), SimpleNamespace(id="claude-sonnet-9"), SimpleNamespace(id="claude-haiku-9")]))


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
    fake = FakeClient()
    runmod.llm.get_client = lambda: fake
    runmod.arxiv_feed.fetch_fulltext = lambda pid, budget=0: ("FULL TEXT", "fulltext-html")
    import os
    os.environ["ANTHROPIC_API_KEY"] = "test"
    sys.argv = ["run.py", "--feed-file", str(FX / "feed_2026-10-14.xml"), "--no-email"]
    assert runmod.main() == 0
    rec = json.loads((tmp / "docs/data/2026-10-14.json").read_text())
    assert rec["models"] == {"rank": "claude-haiku-9", "summary": "claude-sonnet-9"}
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
    n = len(fake.messages.calls)
    assert runmod.main() == 0 and len(fake.messages.calls) == n
    # empty feed (weekend) is a no-op
    sys.argv = ["run.py", "--feed-file", str(FX / "feed_empty.xml"), "--no-email"]
    assert runmod.main() == 0
    os.environ.pop("ANTHROPIC_API_KEY")
    sys.path.remove(str(tmp))
    shutil.rmtree(tmp)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
