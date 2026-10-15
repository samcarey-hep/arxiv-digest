#!/usr/bin/env python3
"""Daily arXiv digest: fetch -> rank -> summarize -> build site (-> email).

  python run.py                 # normal daily run (needs ANTHROPIC_API_KEY)
  python run.py --dry-run       # no API calls: keyword ranking + abstract snippets
  python run.py --feed-file f   # use a saved RSS file instead of downloading
  python run.py --force         # redo today's digest even if it already exists
  python run.py --rebuild-site  # only regenerate HTML from saved data
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from digest import arxiv_feed, llm, site, emailer

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
DATA = DOCS / "data"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def recently_seen(days: int = 10) -> set[str]:
    ids = set()
    for f in sorted(DATA.glob("20??-??-??.json"))[-days:]:
        ids |= {p["id"] for p in json.loads(f.read_text())["papers"]}
    return ids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--feed-file")
    ap.add_argument("--rebuild-site", action="store_true")
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    DATA.mkdir(parents=True, exist_ok=True)

    if args.rebuild_site:
        site.build(DOCS, cfg)
        return 0

    # 1. fetch today's announcement --------------------------------------
    if args.feed_file:
        date, papers = arxiv_feed.parse_feed(Path(args.feed_file).read_bytes())
        keep = {"new"} | ({"cross"} if cfg.get("include_cross_lists", True) else set()) \
            | ({"replace", "replace-cross"} if cfg.get("include_replacements") else set())
        papers = [p for p in papers if p.announce_type in keep]
    else:
        date, papers = arxiv_feed.fetch_announcement(
            cfg["categories"], cfg.get("include_cross_lists", True), cfg.get("include_replacements", False))
    if not date or not papers:
        log("No announcement in the feed (weekend/holiday, or feed not yet updated). Nothing to do.")
        return 0
    out_file = DATA / f"{date}.json"
    if out_file.exists() and not args.force:
        log(f"Digest for announcement {date} already exists. Feed not updated yet? Skipping.")
        return 0

    seen = recently_seen() if not args.force else set()
    papers = [p for p in papers if p.id not in seen]
    log(f"Announcement {date}: {len(papers)} papers to rank")
    if not papers:
        return 0

    # 2. rank -------------------------------------------------------------
    usage = {}
    models = {}
    client = None
    if args.dry_run or not os.environ.get("ANTHROPIC_API_KEY"):
        if not args.dry_run:
            log("ANTHROPIC_API_KEY not set -> dry run")
            args.dry_run = True
        for p in papers:
            p.score = llm.keyword_score(p, cfg)
            p.reason = "(dry run: keyword-ranked)"
    else:
        client = llm.get_client()
        models["rank"] = llm.resolve_model(client, cfg["ranking"]["model"])
        models["summary"] = llm.resolve_model(client, cfg["summaries"]["model"])
        log(f"Models: {models}")
        usage["rank"] = llm.rank(client, models["rank"], cfg, papers, cfg["ranking"].get("batch_size", 40))
    llm.apply_boosts(papers, cfg)
    papers.sort(key=lambda p: -p.score)

    # 3. summarize the top papers ----------------------------------------
    scfg = cfg["summaries"]
    top = [p for p in papers if p.score >= scfg.get("min_score", 6)][: scfg.get("max_detailed", 12)]
    log(f"Summarizing {len(top)} papers")

    def work(p):
        if args.dry_run:
            return p, llm.abstract_only_summary(p), "abstract", None
        text, src = ("", "abstract")
        if scfg.get("full_text", True):
            text, src = arxiv_feed.fetch_fulltext(p.id, scfg.get("max_fulltext_chars", 45000))
        try:
            s, u = llm.summarize(client, models["summary"], cfg, p, text)
            return p, s, src, u
        except Exception as e:  # noqa: BLE001
            log(f"[summary] {p.id} failed: {e}")
            return p, {}, src, None

    su = {"input": 0, "output": 0}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for p, s, src, u in ex.map(work, top):
            p.summary, p.summary_source = s, src
            if u:
                su["input"] += u.input_tokens; su["output"] += u.output_tokens
    usage["summary"] = su

    # 4. save + build ------------------------------------------------------
    record = {
        "date": date,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "dry_run": args.dry_run,
        "models": models,
        "usage": usage,
        "counts": {"total": len(papers), "relevant": sum(p.score >= cfg["ranking"].get("min_score", 4) for p in papers),
                   "detailed": len(top)},
        "papers": [p.to_dict() for p in papers],
    }
    out_file.write_text(json.dumps(record, indent=1, ensure_ascii=False))
    log(f"Saved {out_file.relative_to(ROOT)}; usage {usage}")
    site.build(DOCS, cfg)

    if not args.no_email and not args.dry_run:
        emailer.maybe_send(record, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
