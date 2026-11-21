import argparse
import json
import logging
import os
from pathlib import Path

from .config import CORPUS, DATA, DEMO, MODE
from .importer import read_articles
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
    store = get_store()
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
    if args.command == "import":
        if (args.path.resolve() != (DEMO / "articles.jsonl").resolve() or args.official) and CORPUS != "event":
            parser.error("Set BRIEFING_CORPUS=event to import material from outside the fixture")
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


if __name__ == "__main__":
    main()
