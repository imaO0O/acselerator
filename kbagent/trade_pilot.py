"""Standalone SQLite measurements for a trade pilot; no estimated manual times.

Callers supply an authenticated user and a trusted admin flag. Rates are fractions;
time_reduction_pct is 100 * (1 - paired agent total / paired manual total).
Missing measurements/rates are None. Request lines use str.splitlines(), including
blank lines, without counting a trailing line terminator as an extra line.
"""

import csv
import io
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


RATINGS = ("верно", "частично", "неверно")
FIELDS = (
    "id", "user", "request", "request_lines", "positions", "agent_seconds",
    "manual_seconds", "rating", "comment", "created_at", "reviewed_at",
)


def _user(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("user must be a nonempty string")


def _seconds(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite nonnegative number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return number


def _now():
    return datetime.now(timezone.utc).isoformat()


def _csv_cell(value):
    # Quoting alone does not prevent spreadsheet formula execution. Protect text
    # after leading whitespace/control characters too, without changing storage.
    if isinstance(value, str):
        significant = value.lstrip()
        while significant and ord(significant[0]) < 32:
            significant = significant[1:].lstrip()
        if value.startswith(("\t", "\r", "\n")) or significant.startswith(("=", "+", "-", "@")):
            return "'" + value
    return value


class PilotLog:
    """File-backed pilot log. Owners see their records; admins see all records.

    review() retains an existing manual measurement when manual_seconds is None.
    A review replaces the rating/comment, but never the original request or timing
    with the agent. Unknown and inaccessible IDs both raise PermissionError.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS trade_pilot (
                    id TEXT PRIMARY KEY,
                    user TEXT NOT NULL,
                    request TEXT NOT NULL,
                    request_lines INTEGER NOT NULL CHECK(request_lines >= 0),
                    positions INTEGER NOT NULL CHECK(positions >= 0),
                    agent_seconds REAL NOT NULL CHECK(agent_seconds >= 0),
                    manual_seconds REAL CHECK(manual_seconds >= 0),
                    rating TEXT CHECK(rating IN ('верно', 'частично', 'неверно')),
                    comment TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS trade_pilot_user ON trade_pilot(user)")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, user: str, request: str, positions: int, agent_seconds: float,
               manual_seconds: float | None = None) -> str:
        _user(user)
        if not isinstance(request, str):
            raise ValueError("request must be a string")
        if type(positions) is not int or not 0 <= positions <= 2**63 - 1:
            raise ValueError("positions must be a nonnegative SQLite integer")
        agent = _seconds(agent_seconds, "agent_seconds")
        manual = None if manual_seconds is None else _seconds(manual_seconds, "manual_seconds")
        record_id = uuid4().hex
        with self._db() as db:
            db.execute("""INSERT INTO trade_pilot
                (id, user, request, request_lines, positions, agent_seconds, manual_seconds, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                       (record_id, user, request, len(request.splitlines()), positions, agent, manual, _now()))
        return record_id

    def review(self, id, user, rating, manual_seconds=None, comment='', is_admin=False):
        _user(user)
        if not isinstance(rating, str) or rating not in RATINGS:
            raise ValueError(f"rating must be exactly one of {RATINGS}")
        if not isinstance(comment, str):
            raise ValueError("comment must be a string")
        manual = None if manual_seconds is None else _seconds(manual_seconds, "manual_seconds")
        with self._db() as db:
            result = db.execute("""UPDATE trade_pilot SET rating = ?, comment = ?,
                manual_seconds = COALESCE(?, manual_seconds), reviewed_at = ?
                WHERE id = ? AND (user = ? OR ? = 1)""",
                                (rating, comment, manual, _now(), id, user, is_admin is True))
            if result.rowcount != 1:
                raise PermissionError("record not found or not owned by user")

    def rows(self, user, is_admin=False) -> list[dict]:
        _user(user)
        with self._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM trade_pilot WHERE user = ? OR ? = 1 ORDER BY created_at, id",
                (user, is_admin is True))]

    def summary(self, user, is_admin=False) -> dict:
        records = self.rows(user, is_admin)
        paired = [row for row in records if row["manual_seconds"] is not None]
        counts = {rating: sum(row["rating"] == rating for row in records) for rating in RATINGS}
        reviewed = sum(counts.values())
        paired_agent = sum(row["agent_seconds"] for row in paired)
        paired_manual = sum(row["manual_seconds"] for row in paired)
        return {
            "count": len(records),
            "positions": sum(row["positions"] for row in records),
            "request_lines": sum(row["request_lines"] for row in records),
            "agent_seconds": sum(row["agent_seconds"] for row in records),
            "manual_count": len(paired),
            "manual_seconds": paired_manual if paired else None,
            "paired_count": len(paired),
            "unpaired_count": len(records) - len(paired),
            "paired_agent_seconds": paired_agent if paired else None,
            "paired_manual_seconds": paired_manual if paired else None,
            "time_reduction_pct": 100 * (1 - paired_agent / paired_manual) if paired_manual else None,
            "reviewed_count": reviewed,
            "unreviewed_count": len(records) - reviewed,
            "rating_counts": counts,
            "rating_rates": {rating: count / reviewed if reviewed else None for rating, count in counts.items()},
            "correct_rate": counts["верно"] / reviewed if reviewed else None,
        }

    def export_csv(self, user, is_admin=False) -> bytes:
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(FIELDS)
        for row in self.rows(user, is_admin):
            writer.writerow([_csv_cell(row[field]) for field in FIELDS])
        return output.getvalue().encode("utf-8-sig")
