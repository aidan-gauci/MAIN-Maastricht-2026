import json
import re
from io import TextIOWrapper
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from pydantic import ValidationError

from .config import Article


def is_feed(name):
    """JSONL feeds at any depth, skipping macOS archive metadata such as __MACOSX/ and ._ files."""
    parts = PurePosixPath(str(name).replace("\\", "/")).parts
    return (
        bool(parts) and parts[-1].lower().endswith(".jsonl")
        and "__MACOSX" not in parts and not parts[-1].startswith("._")
    )


def feeds(path):
    path = Path(path)
    if not path.exists():
        raise ValueError(f"Import path not found: {path}")
    if path.is_dir():
        for file in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
            if file.is_file() and is_feed(file.relative_to(path).as_posix()):
                with file.open(encoding="utf-8-sig") as stream:
                    yield file.name, stream
    elif path.suffix.lower() == ".zip":
        try:
            archive = ZipFile(path)
        except BadZipFile:
            raise ValueError(f"Not a readable ZIP archive: {path.name}") from None
        with archive:
            for name in sorted(archive.namelist()):
                if not name.endswith("/") and is_feed(name):
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
                        region=row.get("region"),
                        country=row.get("country"),
                    )
                for key in ("title", "body", "source"):
                    if isinstance(row.get(key), str):
                        row[key] = re.sub(r"\s+", " ", row[key]).strip()
                if isinstance(row.get("language"), str):
                    row["language"] = row["language"].strip().lower()
                row["official"] = official or row.get("official", False)
                items.append(Article.model_validate(row).model_dump(mode="json"))
            except ValidationError as exc:
                fields = sorted({".".join(map(str, error["loc"])) or "row" for error in exc.errors()})
                raise ValueError(f"Invalid article at {Path(name).name}:{line_number} (check {', '.join(fields)})") from None
            except (ValueError, AttributeError, TypeError):
                raise ValueError(f"Invalid article at {Path(name).name}:{line_number}") from None
            if limit is not None and len(items) >= limit:
                return items
    if not items:
        raise ValueError("No articles found in the JSONL feeds")
    return items
