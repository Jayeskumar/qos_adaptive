"""Read/write JSON result files, transparently gzip-compressed when the name ends in .gz."""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any


def read_json(path: str | Path) -> Any:
    p = Path(path)
    if p.suffix == ".gz":
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(p.read_text())


def write_json(path: str | Path, obj: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix == ".gz":
        with gzip.open(p, "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump(obj, f)
    else:
        p.write_text(json.dumps(obj))


def resolve(path: str | Path) -> Path:
    """Return `path`, or its .gz sibling if only that exists."""
    p = Path(path)
    if not p.exists() and Path(str(p) + ".gz").exists():
        return Path(str(p) + ".gz")
    return p
