#!/usr/bin/env python3
"""SQLite storage for generated public-sector reports."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


DEFAULT_DATABASE_PATH = Path("reports.db")


@dataclass(frozen=True)
class StoredReport:
    id: int
    title: str
    request: str
    document: str
    approved: bool
    score: int
    attempts: int
    created_at: str


class ReportRepository:
    """Persist generated Markdown reports in a local SQLite database."""

    def __init__(self, database_path: Path | str = DEFAULT_DATABASE_PATH):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    request TEXT NOT NULL,
                    document TEXT NOT NULL,
                    approved INTEGER NOT NULL CHECK (approved IN (0, 1)),
                    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
                    attempts INTEGER NOT NULL CHECK (attempts >= 1),
                    created_at TEXT NOT NULL
                )
                """
            )

    def save(
        self,
        *,
        title: str,
        request: str,
        document: str,
        approved: bool,
        score: int,
        attempts: int,
    ) -> StoredReport:
        """Save a report and return its database record."""
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO reports (title, request, document, approved, score, attempts, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (title.strip() or "제목 미생성", request.strip(), document.strip(), int(approved), score, attempts, created_at),
            )
            report_id = cursor.lastrowid
        return StoredReport(report_id, title.strip() or "제목 미생성", request.strip(), document.strip(), approved, score, attempts, created_at)

    def get(self, report_id: int) -> StoredReport | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return self._to_report(row) if row else None

    def list_recent(self, limit: int = 20) -> list[StoredReport]:
        if limit < 1:
            raise ValueError("limit은 1 이상이어야 합니다.")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reports ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._to_report(row) for row in rows]

    @staticmethod
    def _to_report(row: sqlite3.Row) -> StoredReport:
        return StoredReport(
            id=row["id"],
            title=row["title"],
            request=row["request"],
            document=row["document"],
            approved=bool(row["approved"]),
            score=row["score"],
            attempts=row["attempts"],
            created_at=row["created_at"],
        )


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="저장된 보고서 조회")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    reports = [asdict(report) for report in ReportRepository(args.database).list_recent(args.limit)]
    print(json.dumps(reports, ensure_ascii=False, indent=2))