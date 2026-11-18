"""Admin-only CSV downloads, without writing export files or changing logs.

API: export_csv(source, *, session_token) -> UTF-8-with-BOM bytes. Pass a server
session token, never a client-supplied role/is_admin flag. Integration is opt-in.
Only actions.jsonl and audit.jsonl directly under settings.log_dir are readable.
The directory must be protected by OS permissions against untrusted writers.
"""
import csv
import io
import json
import os
from pathlib import Path
import stat

from . import auth
from .config import settings

FIELDS = {
    "actions.jsonl": ("time", "action", "user", "target", "details"),
    "audit.jsonl": ("time", "user", "role", "question", "asked", "route", "status",
                    "answer", "seconds", "cited", "sql", "unapproved"),
}
MAX_BYTES = 64 * 1024 * 1024


def _cell(value) -> str:
    if value is None:
        return ""
    text = (json.dumps(value, ensure_ascii=False, sort_keys=True)
            if isinstance(value, (dict, list, bool)) else str(value))
    # Quoting alone does not stop spreadsheet formula execution. Include leading
    # controls/whitespace (and BOM), which spreadsheet importers may discard.
    probe = text.lstrip("\ufeff" + "".join(chr(i) for i in range(33)))
    if probe.startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def export_csv(source: str = "actions.jsonl", *, session_token: str) -> bytes:
    """Fail closed on auth, invalid paths, oversized or malformed input.

    Missing logs raise FileNotFoundError; malformed records raise ValueError.
    Unknown record fields are retained in an escaped JSON 'extra' column.
    Reads are bounded but not transactional with concurrent append/retention.
    """
    user = auth.session_user(session_token)
    if not user or user.get("role") != "Администратор" or user.get("must_change", False):
        raise PermissionError("A live administrator session is required")
    if not isinstance(source, str) or source not in FIELDS:
        raise ValueError("Only actions.jsonl and audit.jsonl may be exported")
    root = Path(settings.log_dir).resolve(strict=True)
    path = root / source
    if path.is_symlink() or path.resolve(strict=True).parent != root:
        raise ValueError("Log path leaves the configured log directory")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Log must be a regular file")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Log exceeds export size limit")
    output = io.StringIO(newline="")
    fields = FIELDS[source]
    writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow((*fields, "extra"))
    for number, line in enumerate(raw.decode("utf-8-sig").split("\n"), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(record, dict):
                raise ValueError()
        except ValueError:
            raise ValueError(f"Invalid JSON object at line {number}") from None
        extra = {k: v for k, v in record.items() if k not in fields}
        writer.writerow([_cell(record.get(k)) for k in fields] + [_cell(extra) if extra else ""])
    return output.getvalue().encode("utf-8-sig")
