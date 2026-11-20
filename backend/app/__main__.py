import argparse
import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import CORPUS, DATA, DEMO, MODE
from .importer import read_articles
from .service import get_store, prepare_corpus, usage_report
from .storage import Store


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
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("answer_key", type=Path)
    commands.add_parser("costs")
    purge = commands.add_parser("purge-event")
    purge.add_argument("--confirm", action="store_true", required=True)
    args = parser.parse_args()
    store = get_store()
    if args.command == "purge-event":
        if CORPUS != "event":
            parser.error("Set LIQUID_CORPUS=event before purging")
        for mode in ("demo", "live"):
            directory = DATA / "event" / mode
            Store(directory / "briefings.sqlite3", directory / "audio").purge()
        print("Purged app-managed event data")
        return
    if args.command == "import":
        if (args.path.resolve() != (DEMO / "articles.jsonl").resolve() or args.official) and CORPUS != "event":
            parser.error("Set LIQUID_CORPUS=event to import material from outside the fixture")
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
        stories = prepare_corpus(store, articles)
        print(json.dumps({"articles": len(articles), "stories": len(stories), "mode": MODE, "corpus": CORPUS}))
    elif args.command == "evaluate":
        report = compare(store.stories(), json.loads(args.answer_key.read_text(encoding="utf-8-sig")))
        print(json.dumps(report, indent=2))
        if any(not row["grouped"] or row["extra_members"] or row["missing_values"] for row in report):
            raise SystemExit(1)
    elif args.command == "costs":
        print(json.dumps(usage_report(store), indent=2))


if __name__ == "__main__":
    main()
