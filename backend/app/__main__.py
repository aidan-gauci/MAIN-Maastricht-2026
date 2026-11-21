import argparse
import sys
import json
import logging
import os
from pathlib import Path

from .config import CORPUS, DATA, DEMO, MODE
from .grouping_eval import run_evaluation
from .importer import read_articles
from .regroup import regroup_corpus
from .service import get_store, prepare_corpus, usage_report
from .storage import Store

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")


def main():
    parser = argparse.ArgumentParser(prog="python -m backend.app")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("import")
    ingest.add_argument("path", type=Path, nargs="?", default=DEMO / "articles.jsonl")
    ingest.add_argument("--official", type=Path)
    ingest.add_argument("--limit", type=int)
    ingest.add_argument("--allow-provider-processing", action="store_true")
    ingest.add_argument(
        "--allow-offline-fallback", action="store_true",
        help="In live mode, store heuristic clusters if provider extraction fails instead of keeping the previous corpus",
    )
    commands.add_parser("costs")
    regroup = commands.add_parser(
        "regroup", help="Re-cluster the imported corpus under the current grouping rules; no provider calls")
    regroup.add_argument("path", type=Path, nargs="?", default=DEMO / "articles.jsonl",
                         help="The feeds the corpus was imported from (article text is not stored)")
    regroup.add_argument("--official", type=Path, help="The --official feed given to import, if any")
    grouping = commands.add_parser(
        "evaluate-grouping", help="Offline: score story grouping against the organizers' translation ground truth")
    grouping.add_argument("path", type=Path, help="Organizer ZIP, directory of JSONL feeds, or one JSONL file")
    grouping.add_argument("--embeddings", type=Path,
                          help="JSON mapping article ID to a story vector, to include the cross-outlet merge")
    grouping.add_argument("--probe", action="append", default=[], metavar="WORD",
                          help="Report how many stories with WORD in an English headline sit in an event thread")
    grouping.add_argument("--json", action="store_true", help="Print the full report as JSON")
    backtest = commands.add_parser(
        "backtest-audio", help="Simulate an audience on the imported corpus: clips rendered, cost, checks; no provider calls")
    backtest.add_argument("--listeners", default="100,1000,10000", help="Audience sizes, comma separated")
    backtest.add_argument("--days", type=int, default=3, help="Consecutive daily briefings per listener")
    backtest.add_argument("--seed", type=int, default=1)
    backtest.add_argument("--even", action="store_true", help="Spread choices evenly over the options (worst case for sharing)")
    backtest.add_argument("--both", action="store_true", help="Report the realistic spread and the even worst case")
    backtest.add_argument("--usd-per-1000-chars", type=float, default=0.08)
    backtest.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1), help="Processes computing listener types")
    claims = commands.add_parser(
        "audit-claims", help="Check every stored figure and narration against the original articles; no provider calls")
    claims.add_argument("path", type=Path, nargs="?", default=DEMO / "articles.jsonl",
                        help="The feeds the corpus was imported from")
    commands.add_parser(
        "translate-labels",
        help="Live: translate the imported stories' headlines and measure names for the other listener languages")
    purge = commands.add_parser("purge-event")
    purge.add_argument("--confirm", action="store_true", required=True)
    reset = commands.add_parser("reset-demo", help="Delete generated fixture data; the next start or import rebuilds it")
    reset.add_argument("--confirm", action="store_true", required=True)
    args = parser.parse_args()
    try:
        run(parser, args)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"{parser.prog}: error: {exc}\n")


def purge_corpus(corpus):
    for mode in ("demo", "live"):
        directory = DATA / corpus / mode
        Store(directory / "briefings.sqlite3", directory / "audio").purge()
    try:
        (DATA / corpus).rmdir()
    except OSError:
        pass


def run(parser, args):
    if args.command == "evaluate-grouping":
        # Reads the feeds only: no store, no provider calls. Titles can hold characters a console lacks.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
        print(run_evaluation(args.path, args.embeddings, as_json=args.json, probes=args.probe))
        return
    store = get_store()
    if args.command == "backtest-audio":
        from .audio_backtest import run as backtest_audio
        store.initialize()
        sizes = tuple(int(value) for value in args.listeners.split(",") if value.strip())
        computed = {}
        spreads = (False, True) if args.both else (args.even,)
        reports = [backtest_audio(store, sizes, args.days, args.seed, even, args.usd_per_1000_chars, args.workers, computed)
                   for even in spreads]
        print(json.dumps(reports if args.both else reports[0], indent=2))
        return
    if args.command == "audit-claims":
        from .claim_audit import audit
        store.initialize()
        summaries = store.recommendation_snapshot(lambda stories, summaries: summaries)
        print(json.dumps(audit(store, read_articles(args.path), summaries), indent=2))
        return
    if args.command == "translate-labels":
        # For a corpus imported before labels were translated. Only what is missing is asked for, so it can
        # be run again; figures, evidence and grouping are untouched and the summaries are prepared again.
        from .service import get_providers
        from .summaries import prepare_summaries
        from .translations import translate_corpus
        if MODE != "live" or os.environ.get("ALLOW_EXTERNAL_PROVIDER_DATA") != "1":
            parser.error("translate-labels needs BRIEFING_MODE=live and ALLOW_EXTERNAL_PROVIDER_DATA=1")
        store.initialize()
        stories = store.stories()
        if not stories:
            parser.error("Nothing to translate: import a corpus for this mode first")
        report = translate_corpus(stories, get_providers(store))
        store.replace_corpus(store.articles(), stories, prepare_summaries(stories))
        print(json.dumps({**report, "mode": MODE, "corpus": CORPUS}))
        return
    if args.command == "purge-event":
        if CORPUS != "event":
            parser.error("Set BRIEFING_CORPUS=event before purging")
        purge_corpus("event")
        print(f"Purged app-managed event data in {DATA}")
        return
    if args.command == "reset-demo":
        purge_corpus("fixture")
        print(f"Reset fictional fixture data in {DATA}")
        return
    if args.command in {"import", "regroup"}:
        if (args.path.resolve() != (DEMO / "articles.jsonl").resolve() or args.official) and CORPUS != "event":
            parser.error(f"Set BRIEFING_CORPUS=event to {args.command} material from outside the fixture")
    if args.command == "import":
        if args.limit is not None and args.limit < 1:
            parser.error("--limit requires a positive article count")
        if MODE == "live" and CORPUS == "event" and not (
            args.allow_provider_processing and os.environ.get("ALLOW_EXTERNAL_PROVIDER_DATA") == "1"
        ):
            parser.error("Live event import requires organizer permission and explicit opt-in")
    store.initialize()
    if args.command == "import":
        articles = read_articles(args.path, limit=args.limit)
        if args.official:
            articles += read_articles(args.official, official=True)
        report = {}
        stories = prepare_corpus(store, articles, allow_fallback=args.allow_offline_fallback or MODE != "live", report=report)
        summary = {"articles": len(articles), "stories": len(stories), "mode": MODE, "corpus": CORPUS}
        if MODE == "live":
            summary["heuristic_fallback_articles"] = report.get("heuristic_fallback_articles", 0)
        print(json.dumps(summary))
    elif args.command == "costs":
        print(json.dumps(usage_report(store), indent=2))
    elif args.command == "regroup":
        # No provider is created here: facts, embeddings and usage already stored are all it needs.
        articles = read_articles(args.path)
        if args.official:
            articles += read_articles(args.official, official=True)
        summary = {**regroup_corpus(store, articles), "mode": MODE, "corpus": CORPUS}
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
        print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
