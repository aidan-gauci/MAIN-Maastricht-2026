import json
import re
from io import TextIOWrapper
from pathlib import Path
from zipfile import ZipFile

from pydantic import ValidationError

from .config import Article


def feeds(path):
    if path.is_dir():
        for file in sorted(path.glob("*.jsonl")):
            with file.open(encoding="utf-8-sig") as stream:
                yield file.name, stream
    elif path.suffix.lower() == ".zip":
        with ZipFile(path) as archive:
            for name in sorted(archive.namelist()):
                if name.endswith(".jsonl"):
                    with TextIOWrapper(archive.open(name), encoding="utf-8-sig") as stream:
                        yield name, stream
    else:
        with path.open(encoding="utf-8-sig") as stream:
            yield path.name, stream


def read_articles(path: Path, *, official=False, limit=None):
    items = []
    for name, stream in feeds(path):
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if "$id" in row:
                    row = dict(
                        article_id=row["$id"],
                        title=row.get("headline"),
                        source=(row.get("publication") or {}).get("name") or row.get("source"),
                        publication_date=row.get("date"),
                        language=row.get("language"),
                        body=row.get("text_content") or row.get("excerpt"),
                        url=row.get("permalink"),
                        tickers=row.get("company_tickers") or [],
                    )
                for key in ("title", "body", "source"):
                    if isinstance(row.get(key), str):
                        row[key] = re.sub(r"\s+", " ", row[key]).strip()
                row["official"] = official or row.get("official", False)
                items.append(Article.model_validate(row).model_dump(mode="json"))
            except (ValidationError, ValueError, AttributeError, TypeError):
                raise ValueError(f"Invalid article at {Path(name).name}:{line_number}") from None
            if limit is not None and len(items) >= limit:
                return items
    if not items:
        raise ValueError("No articles found in the JSONL feeds")
    return items
