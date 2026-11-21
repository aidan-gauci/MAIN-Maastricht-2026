import argparse
import sys
import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import CORPUS, DATA, DEMO, MODE
from .grouping_eval import run_evaluation
from .importer import read_articles
from .regroup import regroup_corpus
from .service import get_store, prepare_corpus, usage_report
from .storage import Store

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")


def compare(stories, key):
    def number(value):
        text = re.sub(r"\s*(?:%|percent|prozent|procent)$", "", str(value).strip(" ,.;"), flags=re.I)
        if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
            text = text.replace(",", "")
        elif "," in text and "." not in text:
            text = text.replace(",", ".")
        try:
            return str(Decimal(text).normalize())
        except InvalidOperation:
            return str(value)

    by_article = {article["article_id"]: story for story in stories for article in story["articles"]}
    results = []
    for expected in key["stories"]:
        members = set(expected["article_ids"])
        actual_ids = {by_article[article]["id"] for article in members if article in by_article}
        present = members <= by_article.keys()
        actual = by_article[next(iter(members))] if members and present and len(actual_ids) == 1 else None
        observed = {number(fact["value"]) for fact in actual["facts"]} if actual else set()
        values = {number(value) for value in expected.get("values", [])}
        results.append({
            "label": expected["label"],
            "grouped": bool(actual),
            "extra_members": sorted({row["article_id"] for row in actual["articles"]} - members) if actual else [],
            "missing_values": sorted(values - observed),
        })
    return results


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
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("answer_key", type=Path)
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
    elif args.command == "evaluate":
        report = compare(store.stories(), json.loads(args.answer_key.read_text(encoding="utf-8-sig")))
        print(json.dumps(report, indent=2))
        if any(not row["grouped"] or row["extra_members"] or row["missing_values"] for row in report):
            raise SystemExit(1)
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
