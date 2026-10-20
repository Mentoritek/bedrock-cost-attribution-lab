"""Masks 12-digit AWS account IDs in everything the lab prints.

Nothing with a real account ID should reach a screen recording. Pass --show-ids to any
command to turn masking off for that run.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

MASK = "************"
_ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")

_show_ids = False


def set_show_ids(value: bool) -> None:
    global _show_ids
    _show_ids = bool(value)


def redact(text: str, show_ids: bool | None = None) -> str:
    reveal = _show_ids if show_ids is None else show_ids
    return text if reveal else _ACCOUNT_ID.sub(MASK, text)


def out(*parts: Any, file=None) -> None:
    """print() replacement: every line of console output goes through redact()."""
    text = " ".join(str(p) for p in parts)
    print(redact(text), file=file or sys.stdout, flush=True)


def err(*parts: Any) -> None:
    out(*parts, file=sys.stderr)


def out_json(obj: Any) -> None:
    out(json.dumps(obj, indent=2, default=str))


def table(rows: list[dict[str, Any]], columns: list[str] | None = None) -> None:
    """Prints rows as a plain fixed-width table (redacted)."""
    if not rows:
        out("(no rows)")
        return
    cols = columns or list(rows[0].keys())
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    out("  ".join(c.ljust(widths[c]) for c in cols))
    out("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        out("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))
