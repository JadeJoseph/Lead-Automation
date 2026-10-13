"""The leads sheet (CSV) plus an append-only audit log (SQLite).

Sheet  : out/leads.csv. Seeded from existing_leads.csv on first run; the input file is never modified.
Audit  : out/leadflow.db. Every decision (added, duplicate, rejected, spam, error, ...) with a reason.
Review : out/review_queue.csv. Everything that is NOT a clean add, exported for humans.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SHEET_COLUMNS = [
    # original sheet columns, unchanged and in the same order
    "lead_id", "name", "email", "company", "source", "tag", "created_at",
    # added by the workflow
    "phone", "message", "draft_reply", "tag_reason", "flags", "source_id", "received_at",
    "processed_at", "triage_mode",
]
TERMINAL = ("added", "duplicate", "rejected", "spam")  # outcomes after which a source id is never reprocessed
REVIEW_COLUMNS = ["ts", "run_id", "source_id", "outcome", "stage", "reason", "matched_lead_id",
                  "name", "email", "message", "raw_json"]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_rows(path: Path) -> list[dict]:
    """Read a CSV/TSV, sniffing the delimiter, tolerating BOMs and missing columns."""
    text = Path(path).read_text(encoding="utf-8-sig")
    first = text.splitlines()[0] if text.strip() else ""
    delim = max((",", "\t", ";"), key=first.count) if first else ","
    rows = list(csv.DictReader(text.splitlines(), delimiter=delim))
    return [{c: (r.get(c) or "").strip() for c in SHEET_COLUMNS} | {k: v for k, v in r.items() if k not in SHEET_COLUMNS and k}
            for r in rows]


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        self.sheet_path = cfg.out_dir / "leads.csv"
        self.review_path = cfg.out_dir / "review_queue.csv"
        if self.sheet_path.exists():
            self.rows = read_rows(self.sheet_path)
        else:
            if not cfg.existing_csv.exists():
                raise FileNotFoundError(f"existing leads file not found: {cfg.existing_csv}")
            self.rows = read_rows(cfg.existing_csv)
            self.save()
        self.db = sqlite3.connect(cfg.out_dir / "leadflow.db", check_same_thread=False)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, run_id TEXT, source_id TEXT, stage TEXT,
                outcome TEXT, reason TEXT, lead_id TEXT, matched_lead_id TEXT, name TEXT, email TEXT,
                message TEXT, raw_json TEXT)"""
        )
        self.db.commit()

    # ---------------------------------------------------------------- sheet
    def save(self) -> None:
        """Atomic write: a crash mid-write can never leave a half-written sheet."""
        fd, tmp = tempfile.mkstemp(dir=self.cfg.out_dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=SHEET_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self.rows)
        os.replace(tmp, self.sheet_path)

    def next_lead_id(self) -> str:
        nums = [int(m.group(1)) for r in self.rows if (m := re.fullmatch(r"L-(\d+)", r.get("lead_id", "")))]
        return f"L-{(max(nums) if nums else 1000) + 1}"

    def add_row(self, row: dict) -> None:
        self.rows.append({c: row.get(c, "") for c in SHEET_COLUMNS})
        self.save()

    def get_row(self, lead_id: str) -> dict | None:
        return next((r for r in self.rows if r["lead_id"] == lead_id), None)

    def update_row(self, lead_id: str, **fields) -> None:
        row = self.get_row(lead_id)
        if row is None:
            raise KeyError(lead_id)
        row.update(fields)
        self.save()

    # ---------------------------------------------------------------- audit log
    def log(self, run_id: str, source_id: str, stage: str, outcome: str, reason: str = "",
            lead_id: str = "", matched: str = "", name: str = "", email: str = "",
            message: str = "", raw=None) -> None:
        self.db.execute(
            "INSERT INTO events (ts, run_id, source_id, stage, outcome, reason, lead_id, matched_lead_id,"
            " name, email, message, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), run_id, source_id, stage, outcome, reason, lead_id, matched, name, email,
             message[:300], json.dumps(raw, ensure_ascii=False, default=str) if raw is not None else ""),
        )
        self.db.commit()

    def already_processed(self, source_id: str) -> str | None:
        q = ",".join("?" * len(TERMINAL))
        row = self.db.execute(
            f"SELECT outcome FROM events WHERE source_id=? AND outcome IN ({q}) LIMIT 1", (source_id, *TERMINAL)
        ).fetchone()
        return row[0] if row else None

    def export_review_queue(self) -> int:
        rows = self.db.execute(
            "SELECT ts, run_id, source_id, outcome, stage, reason, matched_lead_id, name, email, message, raw_json"
            " FROM events WHERE outcome != 'added' AND outcome != 'skipped' ORDER BY id"
        ).fetchall()
        with open(self.review_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(REVIEW_COLUMNS)
            w.writerows(rows)
        return len(rows)
