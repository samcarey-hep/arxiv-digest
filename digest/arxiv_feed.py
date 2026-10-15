"""Fetch today's arXiv announcement from the official RSS feed, and paper full text.

Why RSS: rss.arxiv.org/rss/<cats> contains exactly the papers in the most recent
announcement, each tagged new / cross / replace / replace-cross. The search API
sorts by *submission* date, which lags announcements by 1-2 days and is the usual
cause of "yesterday's papers" showing up in digests.
"""
from __future__ import annotations

import io
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

UA = "arxiv-digest/1.0 (personal daily digest; github actions)"
NS = {"arxiv": "http://arxiv.org/schemas/atom", "dc": "http://purl.org/dc/elements/1.1/"}


@dataclass
class Paper:
    id: str                      # e.g. 2610.16836 (no version)
    version: str                 # e.g. v1
    title: str
    abstract: str
    authors: list[str]
    categories: list[str]
    announce_type: str           # new | cross | replace | replace-cross
    date: str                    # announcement date, YYYY-MM-DD
    # filled in later
    score: float = 0.0
    topic: str = ""
    reason: str = ""
    summary: dict = field(default_factory=dict)
    summary_source: str = ""     # fulltext-html | fulltext-pdf | abstract

    @property
    def primary(self) -> str:
        return self.categories[0] if self.categories else ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Paper":
        return cls(**d)


def _get(url: str, timeout: int = 60, retries: int = 3) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _fix_tex_accents(s: str) -> str:
    """Author names come with TeX accents (Arg\\"uelles). Convert the common ones."""
    acc = {'"': "\u0308", "'": "\u0301", "`": "\u0300", "^": "\u0302", "~": "\u0303", "c": "\u0327", "v": "\u030c"}
    def rep(m):
        a, ch = m.group(1), m.group(2)
        import unicodedata
        return unicodedata.normalize("NFC", ch + acc[a])
    s = re.sub(r"\\([\"'`^~])\{?([A-Za-z])\}?", rep, s)
    s = re.sub(r"\\([cv])\{([A-Za-z])\}", rep, s)
    s = s.replace("\\ss", "ß").replace("{", "").replace("}", "")
    return s


def parse_feed(xml_bytes: bytes) -> tuple[str | None, list[Paper]]:
    """Return (announcement_date, papers). date is None when the feed is empty
    (weekends/holidays: arXiv publishes an empty channel)."""
    root = ET.fromstring(xml_bytes)
    chan = root.find("channel")
    papers: list[Paper] = []
    for it in chan.findall("item"):
        desc = it.findtext("description") or ""
        m = re.match(r"\s*arXiv:(\S+?)(v\d+)\s+Announce Type:\s*(\S+)\s*Abstract:\s*(.*)", desc, re.S)
        if not m:
            continue
        pid, ver, atype, abstract = m.group(1), m.group(2), m.group(3), m.group(4)
        atype = it.findtext("arxiv:announce_type", default=atype, namespaces=NS).strip()
        pub = it.findtext("pubDate")
        date = parsedate_to_datetime(pub).date().isoformat() if pub else ""
        authors = [_fix_tex_accents(a.strip()) for a in (it.findtext("dc:creator", default="", namespaces=NS)).split(",") if a.strip()]
        papers.append(Paper(
            id=pid, version=ver, title=_clean(it.findtext("title")), abstract=_clean(abstract),
            authors=authors, categories=[c.text.strip() for c in it.findall("category") if c.text],
            announce_type=atype, date=date,
        ))
    date = None
    if papers:
        # all items share the announcement date; take the most common to be safe
        ds = [p.date for p in papers if p.date]
        date = max(set(ds), key=ds.count) if ds else None
    elif chan.findtext("pubDate"):
        date = None
    return date, papers


def fetch_announcement(categories: list[str], include_cross: bool, include_replacements: bool):
    url = "https://rss.arxiv.org/rss/" + "+".join(categories)
    date, papers = parse_feed(_get(url))
    keep = {"new"}
    if include_cross:
        keep.add("cross")
    if include_replacements:
        keep |= {"replace", "replace-cross"}
    seen, out = set(), []
    for p in papers:
        if p.announce_type in keep and p.id not in seen:
            seen.add(p.id)
            out.append(p)
    return date, out


# ---------------------------------------------------------------- full text

class _LatexmlText(HTMLParser):
    """Extract (heading, text) sections from arXiv's LaTeXML HTML. Math -> $alttext$.
    Everything from the bibliography onward is dropped."""
    SKIP = {"script", "style", "nav", "header", "footer", "figure", "table", "button", "svg"}
    VOID = {"br", "img", "hr", "meta", "link", "input", "col", "wbr", "source"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections: list[list[str]] = [["Preamble", ""]]
        self.stack: list[tuple[str, bool]] = []   # (tag, opened_a_skip)
        self.skip_depth = 0
        self.math_depth = 0
        self.in_heading = False
        self.heading_buf = ""
        self.done = False   # reached bibliography

    def handle_starttag(self, tag, attrs):
        if self.done or tag in self.VOID:
            return
        a = dict(attrs)
        cls = a.get("class", "") or ""
        if "ltx_bibliography" in cls:
            self.done = True
            return
        is_skip = tag in self.SKIP
        if is_skip:
            self.skip_depth += 1
        self.stack.append((tag, is_skip))
        if self.skip_depth:
            return
        if tag == "math":
            self.math_depth += 1
            alt = a.get("alttext")
            if alt and self.math_depth == 1:
                self._emit(f" ${alt}$ ")
        elif tag in ("h2", "h3", "h4") and "ltx_title" in cls:
            self.in_heading, self.heading_buf = True, ""
        elif tag == "p" or (tag == "div" and "ltx_para" in cls):
            self._emit("\n")

    def handle_endtag(self, tag):
        if self.done or not any(t == tag for t, _ in self.stack):
            return
        while self.stack:
            t, was_skip = self.stack.pop()
            if was_skip:
                self.skip_depth -= 1
            if t == "math" and self.math_depth:
                self.math_depth -= 1
            if t in ("h2", "h3", "h4") and self.in_heading:
                self.in_heading = False
                self.sections.append([_clean(self.heading_buf), ""])
            if t == tag:
                break

    def handle_data(self, data):
        if self.done or self.skip_depth or self.math_depth:
            return
        if self.in_heading:
            self.heading_buf += data
        else:
            self._emit(data)

    def _emit(self, s):
        if not self.done and not self.skip_depth and not self.in_heading:
            self.sections[-1][1] += s


def _select_text(sections: list[tuple[str, str]], budget: int) -> str:
    """Keep intro + conclusions in full (up to limits), then the start of each
    remaining section, within a character budget."""
    secs = [(h, re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip()) for h, t in sections]
    secs = [(h, t) for h, t in secs if len(t) > 80]
    is_intro = lambda h: re.search(r"introduc", h, re.I)
    is_concl = lambda h: re.search(r"conclu|summary|discussion|outlook", h, re.I)
    parts: dict[int, str] = {}
    used = 0
    for i, (h, t) in enumerate(secs):
        if is_intro(h):
            parts[i] = t[: int(budget * 0.35)]
        elif is_concl(h):
            parts[i] = t[: int(budget * 0.25)]
    used = sum(len(v) for v in parts.values())
    remaining = [i for i in range(len(secs)) if i not in parts]
    if remaining:
        per = max(800, (budget - used) // len(remaining))
        for i in remaining:
            if used >= budget:
                break
            chunk = secs[i][1][: min(per, budget - used)]
            parts[i] = chunk
            used += len(chunk)
    return "\n\n".join(f"## {secs[i][0]}\n{parts[i]}" for i in sorted(parts))


def fetch_fulltext(paper_id: str, budget: int = 45000) -> tuple[str, str]:
    """Return (text, source). Tries arXiv HTML, then PDF (PyMuPDF), else ('', 'abstract')."""
    try:
        html = _get(f"https://arxiv.org/html/{paper_id}", timeout=60, retries=2).decode("utf-8", "replace")
        if "ltx_page_main" in html or "ltx_document" in html:
            p = _LatexmlText()
            p.feed(html)
            text = _select_text([tuple(s) for s in p.sections], budget)
            if len(text) > 3000:
                return text, "fulltext-html"
    except Exception:  # noqa: BLE001
        pass
    try:
        import fitz  # PyMuPDF
        pdf = _get(f"https://arxiv.org/pdf/{paper_id}", timeout=90, retries=2)
        doc = fitz.open(stream=io.BytesIO(pdf), filetype="pdf")
        raw = "\n".join(page.get_text() for page in doc)
        # cut references
        cut = re.search(r"\n\s*(References|REFERENCES|Bibliography)\s*\n", raw)
        if cut and cut.start() > len(raw) * 0.4:
            raw = raw[: cut.start()]
        if len(raw) > 3000:
            if len(raw) > budget:
                raw = raw[: int(budget * 0.75)] + "\n...\n" + raw[-int(budget * 0.25):]
            return raw, "fulltext-pdf"
    except Exception:  # noqa: BLE001
        pass
    return "", "abstract"
