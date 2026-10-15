"""Static site generator: docs/index.html, docs/days/<date>.html, archive, search."""
from __future__ import annotations

import datetime as dt
import json
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

KATEX = "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist"


def e(s) -> str:
    return escape(str(s or ""), quote=True)


def _authors(a: list[str], n: int = 6) -> str:
    return e(", ".join(a[:n]) + (f" + {len(a) - n} more" if len(a) > n else ""))


def _fmt_date(d: str) -> str:
    return dt.date.fromisoformat(d).strftime("%a %d %b %Y")


def head(title: str, depth: int) -> str:
    up = "../" * depth
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{e(title)}</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="arXiv Digest">
<meta name="theme-color" content="#f7f6f3" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#16171a" media="(prefers-color-scheme: dark)">
<link rel="manifest" href="{up}manifest.webmanifest">
<link rel="icon" href="{up}assets/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="{up}assets/apple-touch-icon.png">
<link rel="stylesheet" href="{up}assets/style.css">
<link rel="stylesheet" href="{KATEX}/katex.min.css">
<script defer src="{KATEX}/katex.min.js"></script>
<script defer src="{KATEX}/contrib/auto-render.min.js"></script>
<script defer src="{up}assets/app.js"></script>
</head><body data-root="{up}">
"""


def nav(cfg: dict, depth: int, active: str) -> str:
    up = "../" * depth
    items = [("today", "Today", f"{up}index.html"), ("archive", "Archive", f"{up}archive.html"),
             ("search", "Search", f"{up}search.html"), ("saved", "Saved", f"{up}search.html#saved")]
    links = "".join(f'<a href="{h}" class="{"on" if k == active else ""}">{t}</a>' for k, t, h in items)
    return f'<header class="top"><a class="brand" href="{up}index.html"><span class="full">{e(cfg["site_title"])}</span><span class="short">ν</span></a><nav>{links}</nav></header>'


def links(p: dict) -> str:
    i = e(p["id"])
    return (f'<a href="https://arxiv.org/abs/{i}">abs</a><a href="https://arxiv.org/pdf/{i}">pdf</a>'
            f'<a href="https://arxiv.org/html/{i}">html</a>'
            f'<button class="star" data-id="{i}" aria-label="Save for later">☆</button>')


def score_pill(s: float) -> str:
    cls = "hi" if s >= 8 else "mid" if s >= 6 else "lo"
    return f'<span class="score {cls}" title="relevance 0-10">{s:.0f}</span>'


def card(p: dict) -> str:
    s = p.get("summary") or {}
    pr = s.get("read_priority", "")
    src = {"fulltext-html": "read from full text", "fulltext-pdf": "read from PDF",
           "abstract": "abstract only"}.get(p.get("summary_source"), "")
    cats = " ".join(f'<span class="cat">{e(c)}</span>' for c in p["categories"][:4])
    body = ""
    if s:
        kr = "".join(f"<li>{e(x)}</li>" for x in s.get("key_results", []) if x)
        f = lambda label, key, cls="": (f'<div class="field {cls}"><h4>{label}</h4><p>{e(s.get(key))}</p></div>'
                                        if s.get(key) else "")
        body = (f'\n  <p class="tldr">{e(s.get("tldr"))}</p>' + f("Novel idea", "novel_idea") + f("Method", "method")
                + (f'<div class="field"><h4>Key results</h4><ul>{kr}</ul></div>' if kr else "")
                + f("Why it matters to you", "why_it_matters", "why") + f("Caveats", "caveats", "muted"))
    else:
        body = f'<p class="tldr">{e(p.get("reason"))}</p><details><summary>Abstract</summary><p>{e(p["abstract"])}</p></details>'
    return f"""<article class="card" id="p{e(p['id'])}">
  <div class="meta">{score_pill(p['score'])}{f'<span class="prio {e(pr.replace(" ", "-"))}">{e(pr)}</span>' if pr else ''}<span class="topic">{e(p.get('topic'))}</span></div>
  <h3><a href="https://arxiv.org/abs/{e(p['id'])}">{e(p['title'])}</a></h3>
  <p class="authors">{_authors(p['authors'])}</p>
  <p class="cats">{cats}<span class="id">arXiv:{e(p['id'])}{' · cross-list' if p['announce_type'] == 'cross' else ''}</span></p>{body}
  <details class="abs"><summary>Abstract</summary><p>{e(p['abstract'])}</p></details>
  <div class="links">{links(p)}<span class="src">{e(src)}</span></div>
</article>"""


def row(p: dict, show_reason: bool = True) -> str:
    reason = f'<span class="reason">{e(p.get("reason"))}</span>' if show_reason and p.get("reason") else ""
    return (f'<li class="row">{score_pill(p["score"])}<div><a href="https://arxiv.org/abs/{e(p["id"])}">{e(p["title"])}</a>'
            f'<span class="sub">{_authors(p["authors"], 3)} · {e(p["categories"][0])}</span>{reason}'
            f'<details class="abs"><summary>Abstract</summary><p>{e(p["abstract"])}</p></details></div>'
            f'<button class="star" data-id="{e(p["id"])}" aria-label="Save">☆</button></li>')


def day_page(rec: dict, cfg: dict, prev_d: str | None, next_d: str | None, depth: int, active: str) -> str:
    up = "../" * depth
    papers = rec["papers"]
    lo = cfg["ranking"].get("min_score", 4)
    detailed = [p for p in papers if p.get("summary")]
    det_ids = {p["id"] for p in detailed}
    also = [p for p in papers if p["id"] not in det_ids and p["score"] >= lo]
    rest = [p for p in papers if p["id"] not in det_ids and p["score"] < lo]
    pn = (f'<a href="{up}days/{prev_d}.html">← {e(_fmt_date(prev_d))}</a>' if prev_d else "<span></span>") + \
         (f'<a href="{up}days/{next_d}.html">{e(_fmt_date(next_d))} →</a>' if next_d else "<span></span>")
    gen = dt.datetime.fromisoformat(rec["generated_at"]).astimezone(
        ZoneInfo(cfg.get("timezone", "America/New_York"))).strftime("%a %H:%M") if rec.get("generated_at") else ""
    banner = '<p class="warn">Preview generated without the API (keyword ranking, abstract snippets).</p>' if rec.get("dry_run") else ""
    return head(f"arXiv · {_fmt_date(rec['date'])}", depth) + nav(cfg, depth, active) + f"""
<main>
<section class="dayhead">
  <p class="eyebrow">arXiv announcement</p>
  <h1>{e(_fmt_date(rec['date']))}</h1>
  <p class="stats">{rec['counts']['total']} new papers in {e(', '.join(cfg['categories']))} · {len(also) + len(detailed)} relevant · {len(detailed)} summarized</p>
  {banner}
  <div class="pn">{pn}</div>
</section>
<h2 class="sec">Top picks</h2>
{''.join(card(p) for p in detailed) or '<p class="muted">Nothing above the summary threshold today.</p>'}
<h2 class="sec">Also relevant <span class="n">{len(also)}</span></h2>
<ul class="rows">{''.join(row(p) for p in also)}</ul>
<details class="rest"><summary>Everything else <span class="n">{len(rest)}</span></summary>
<ul class="rows">{''.join(row(p, False) for p in rest)}</ul></details>
<div class="pn bottom">{pn}</div>
<footer>Ranked by {e(rec.get('models', {}).get('rank', 'keywords'))}, summaries by {e(rec.get('models', {}).get('summary', '—'))}. Generated {e(gen)}. Summaries are AI-written; check the paper before citing.</footer>
</main></body></html>"""


def build(docs: Path, cfg: dict) -> None:
    data = docs / "data"
    days_dir = docs / "days"
    days_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(data.glob("20??-??-??.json"))
    recs = [json.loads(f.read_text()) for f in files]
    dates = [r["date"] for r in recs]
    lo = cfg["ranking"].get("min_score", 4)
    all_rows = []
    for i, r in enumerate(recs):
        prev_d = dates[i - 1] if i > 0 else None
        next_d = dates[i + 1] if i + 1 < len(dates) else None
        (days_dir / f"{r['date']}.html").write_text(day_page(r, cfg, prev_d, next_d, 1, ""))
        for p in r["papers"]:
            if p["score"] >= lo:
                all_rows.append({"id": p["id"], "d": r["date"], "t": p["title"], "s": round(p["score"], 1),
                                 "k": p.get("topic", ""), "a": ", ".join(p["authors"][:3]) + (" et al." if len(p["authors"]) > 3 else ""),
                                 "x": (p.get("summary") or {}).get("tldr") or p.get("reason", "")})
    if recs:
        (docs / "index.html").write_text(day_page(recs[-1], cfg, dates[-2] if len(dates) > 1 else None, None, 0, "today"))
    else:
        (docs / "index.html").write_text(head(cfg["site_title"], 0) + nav(cfg, 0, "today") +
                                         '<main><p class="muted">No digests yet; the first one appears after the next arXiv announcement.</p></main></body></html>')
    (data / "all.json").write_text(json.dumps(all_rows[::-1], ensure_ascii=False, separators=(",", ":")))

    # archive
    items = []
    for r in reversed(recs):
        tops = [p for p in r["papers"] if p.get("summary")][:3]
        t = "".join(f"<li>{e(p['title'])}</li>" for p in tops)
        items.append(f'<a class="arch" href="days/{r["date"]}.html"><h3>{e(_fmt_date(r["date"]))}</h3>'
                     f'<p class="stats">{r["counts"]["total"]} papers · {r["counts"]["detailed"]} summarized</p><ul>{t}</ul></a>')
    (docs / "archive.html").write_text(head("Archive", 0) + nav(cfg, 0, "archive") +
                                       f'<main><h1>Archive</h1>{"".join(items) or "<p class=muted>Empty.</p>"}</main></body></html>')

    # search / saved
    (docs / "search.html").write_text(head("Search", 0) + nav(cfg, 0, "search") + """
<main><h1 id="sh">Search</h1>
<input id="q" type="search" placeholder="Title, author, topic…" autocomplete="off">
<label class="chk"><input type="checkbox" id="savedonly"> Saved only</label>
<p id="count" class="stats"></p><ul id="results" class="rows"></ul></main></body></html>""")
