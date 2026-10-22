#!/usr/bin/env python3
"""Daily arXiv digest: fetch -> rank -> summarize -> build site (-> email).

  python run.py                 # normal daily run (needs GEMINI_API_KEY, or ANTHROPIC_API_KEY)
  python run.py --dry-run       # no API calls: keyword ranking + abstract snippets
  python run.py --feed-file f   # use a saved RSS file instead of downloading
  python run.py --force         # redo today's digest even if it already exists
  python run.py --rebuild-site  # only regenerate HTML from saved data
Re-running on a day that already has a digest fills in any summaries that failed earlier.
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


def select_top(papers, cfg):
    scfg = cfg["summaries"]
    ranked = sorted(papers, key=lambda p: -p.score)
    return [p for p in ranked if p.score >= scfg.get("min_score", 6)][: scfg.get("max_detailed", 12)]


def summarize_top(papers, cfg, sum_call) -> dict:
    """Summarize the top papers that don't have a summary yet. Batches of several papers per
    request; if a batch fails, its papers are retried one at a time."""
    scfg = cfg["summaries"]
    todo = [p for p in select_top(papers, cfg) if not p.summary]
    log(f"Summarizing {len(todo)} papers")
    su = {"input": 0, "output": 0}
    if not todo:
        return su

    def get_text(p):
        if scfg.get("full_text", True):
            return arxiv_feed.fetch_fulltext(p.id, scfg.get("max_fulltext_chars", 45000))
        return "", "abstract"
    with ThreadPoolExecutor(max_workers=4) as ex:
        texts = list(ex.map(get_text, todo))
    for p, (_, src) in zip(todo, texts):
        p.summary_source = src
    items = list(zip(todo, [t for t, _ in texts]))
    per = max(1, int(scfg.get("papers_per_call", 5 if cfg.get("provider", "gemini") == "gemini" else 1)))

    def run_chunk(chunk):
        if len(chunk) == 1:
            s1, u = llm.summarize(sum_call, cfg, chunk[0][0], chunk[0][1])
            res = {chunk[0][0].id: s1}
        else:
            res, u = llm.summarize_many(sum_call, cfg, chunk)
        su["input"] += u[0]; su["output"] += u[1]
        for p, _ in chunk:
            p.summary = res.get(p.id) or p.summary
        return [it for it in chunk if not it[0].summary]

    for i in range(0, len(items), per):
        chunk = items[i:i + per]
        try:
            missing = run_chunk(chunk)
        except llm.QuotaExhausted as e:
            log(f"[summary] free quota exhausted: {e}")
            break
        except Exception as e:  # noqa: BLE001
            log(f"[summary] batch starting {chunk[0][0].id} failed: {e}; retrying one by one")
            missing = chunk
        if len(chunk) > 1:
            for it in missing:
                try:
                    run_chunk([it])
                except llm.QuotaExhausted as e:
                    log(f"[summary] free quota exhausted: {e}")
                    return su
                except Exception as e:  # noqa: BLE001
                    log(f"[summary] {it[0].id} failed: {e}")
    done = sum(1 for p in todo if p.summary)
    log(f"[summary] {done}/{len(todo)} summarized")
    return su


def fill_missing(out_file, cfg) -> bool:
    """A later run on a day that already has a digest: fill in any summaries that failed
    earlier (rate limits, timeouts). Returns True if the file changed."""
    rec = json.loads(out_file.read_text())
    if rec.get("dry_run") or not llm.api_key_present(cfg):
        return False
    papers = [arxiv_feed.Paper.from_dict(d) for d in rec["papers"]]
    if all(p.summary for p in select_top(papers, cfg)):
        return False
    backend = llm.get_backend(cfg)
    sum_c = backend.resolve(cfg["summaries"]["model"])
    rank_c = backend.resolve(cfg["ranking"]["model"])
    sum_call = llm.Caller(backend, sum_c + [m for m in rank_c if m not in sum_c])
    before = sum(1 for p in papers if p.summary)
    u = summarize_top(papers, cfg, sum_call)
    after = sum(1 for p in papers if p.summary)
    if after == before:
        return False
    rec["papers"] = [p.to_dict() for p in papers]
    rec["counts"]["detailed"] = sum(1 for p in select_top(papers, cfg) if p.summary)
    rec.setdefault("models", {})["summary"] = sum_call.used or rec.get("models", {}).get("summary")
    prev = rec.setdefault("usage", {}).get("summary") or {"input": 0, "output": 0}
    rec["usage"]["summary"] = {"input": prev["input"] + u["input"], "output": prev["output"] + u["output"]}
    out_file.write_text(json.dumps(rec, indent=1, ensure_ascii=False))
    log(f"Filled {after - before} missing summaries in {out_file.name}")
    return True


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
        if not args.dry_run and fill_missing(out_file, cfg):
            site.build(DOCS, cfg)
            return 0
        log(f"Digest for announcement {date} already exists and is complete. Skipping.")
        return 0

    seen = recently_seen() if not args.force else set()
    papers = [p for p in papers if p.id not in seen]
    log(f"Announcement {date}: {len(papers)} papers to rank")
    if not papers:
        return 0

    # 2. rank -------------------------------------------------------------
    usage = {}
    models = {}
    rank_call = sum_call = None
    if args.dry_run or not llm.api_key_present(cfg):
        if not args.dry_run:
            log("No API key set (GEMINI_API_KEY / ANTHROPIC_API_KEY) -> dry run")
            args.dry_run = True
        for p in papers:
            p.score = llm.keyword_score(p, cfg)
            p.reason = "(dry run: keyword-ranked)"
    else:
        backend = llm.get_backend(cfg)
        rank_c = backend.resolve(cfg["ranking"]["model"])
        sum_c = backend.resolve(cfg["summaries"]["model"])
        log(f"Model candidates: rank={rank_c} summary={sum_c}")
        rank_call = llm.Caller(backend, rank_c)
        # if the summary model runs out of free quota, fall back to the ranking model
        sum_call = llm.Caller(backend, sum_c + [m for m in rank_c if m not in sum_c])
        usage["rank"] = llm.rank(rank_call, cfg, papers, cfg["ranking"].get("batch_size", 40))
        models["rank"] = rank_call.used or "keywords"
    llm.apply_boosts(papers, cfg)
    papers.sort(key=lambda p: -p.score)

    # 3. summarize the top papers ----------------------------------------
    if args.dry_run:
        top = select_top(papers, cfg)
        for p in top:
            p.summary, p.summary_source = llm.abstract_only_summary(p), "abstract"
        usage["summary"] = {"input": 0, "output": 0}
    else:
        usage["summary"] = summarize_top(papers, cfg, sum_call)
        models["summary"] = sum_call.used or "none"
    top = [p for p in select_top(papers, cfg) if p.summary]

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
