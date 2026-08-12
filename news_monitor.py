"""Local storage and rule-based extraction for mortgage news."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


RATE_RE = re.compile(r"(?<!\d)(\d{1,2}(?:[.,]\d{1,2})?)\s*%")
MORTGAGE_MARKERS = (
    "ипотек", "ставк", "кредит", "первоначальн", "семейн", "it-ипотек",
    "господдерж", "новостро", "жиль", "рефинанс",
)
PROGRAM_MARKERS = {
    "Семейная ипотека": ("семейн",),
    "IT-ипотека": ("it-ипотек", "айти-ипотек"),
    "Новостройки": ("новостро", "первичн"),
    "Вторичное жильё": ("вторич",),
    "Рефинансирование": ("рефинанс",),
}


@dataclass(frozen=True)
class RateMention:
    program: str
    rate: float
    context: str


def extract_rate_mentions(text: str) -> list[RateMention]:
    """Extract likely mortgage-rate mentions without claiming offer accuracy."""
    lowered = text.lower().replace("ё", "е")
    if not any(marker.replace("ё", "е") in lowered for marker in MORTGAGE_MARKERS):
        return []

    program = "Ипотека (программа не определена)"
    for name, markers in PROGRAM_MARKERS.items():
        if any(marker in lowered for marker in markers):
            program = name
            break

    mentions = []
    for match in RATE_RE.finditer(text):
        rate = float(match.group(1).replace(",", "."))
        if not 0 < rate < 50:
            continue
        start = max(0, match.start() - 90)
        end = min(len(text), match.end() + 90)
        context = " ".join(text[start:end].split())
        mentions.append(RateMention(program, rate, context))
    return mentions


class NewsStore:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS posts (
                channel TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                published_at TEXT NOT NULL,
                text TEXT NOT NULL,
                link TEXT,
                PRIMARY KEY (channel, message_id)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS rate_mentions (
                channel TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                program TEXT NOT NULL,
                rate REAL NOT NULL,
                context TEXT NOT NULL,
                FOREIGN KEY (channel, message_id) REFERENCES posts(channel, message_id)
            )"""
        )
        return connection

    def save_post(self, channel: str, message_id: int, published_at: datetime,
                  text: str, link: str | None) -> int:
        mentions = extract_rate_mentions(text)
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO posts VALUES (?, ?, ?, ?, ?)",
                (channel, message_id, published_at.astimezone(timezone.utc).isoformat(), text, link),
            )
            if cursor.rowcount:
                db.executemany(
                    "INSERT INTO rate_mentions VALUES (?, ?, ?, ?, ?)",
                    [(channel, message_id, item.program, item.rate, item.context) for item in mentions],
                )
        return len(mentions)

    def latest_rates(self, limit: int = 15) -> list[sqlite3.Row]:
        with self._connect() as db:
            return db.execute(
                """WITH ranked AS (
                       SELECT p.channel, p.published_at, p.link, r.program, r.rate, r.context,
                              ROW_NUMBER() OVER (
                                  PARTITION BY p.channel, r.program ORDER BY p.published_at DESC
                              ) AS position
                       FROM rate_mentions r JOIN posts p USING(channel, message_id)
                   )
                   SELECT channel, published_at, link, program, rate, context
                   FROM ranked WHERE position = 1
                   ORDER BY published_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    def recent_changes(self, days: int = 7, limit: int = 20) -> list[sqlite3.Row]:
        with self._connect() as db:
            return db.execute(
                """WITH history AS (
                       SELECT p.channel, p.published_at, p.link, r.program, r.rate, r.context,
                              LAG(r.rate) OVER (
                                  PARTITION BY p.channel, r.program ORDER BY p.published_at
                              ) AS previous_rate
                       FROM rate_mentions r JOIN posts p USING(channel, message_id)
                   )
                   SELECT channel, published_at, link, program, rate, context, previous_rate
                   FROM history
                   WHERE published_at >= datetime('now', ?) AND previous_rate IS NOT NULL
                         AND rate <> previous_rate
                   ORDER BY published_at DESC LIMIT ?""",
                (f"-{days} days", limit),
            ).fetchall()


def format_rate_report(rows: list[sqlite3.Row], heading: str) -> str:
    if not rows:
        return f"{heading}\n\nЗа выбранный период упоминаний ставок пока нет."
    lines = [heading, "", "Данные из публикаций; условия нужно сверять с банком."]
    for row in rows:
        date = datetime.fromisoformat(row["published_at"]).strftime("%d.%m.%Y")
        rate = f'{row["rate"]:g}'.replace(".", ",")
        source = f'@{row["channel"]}'
        previous = row["previous_rate"] if "previous_rate" in row.keys() else None
        if previous is None:
            rate_text = f"{rate}%"
        else:
            old_rate = f"{previous:g}".replace(".", ",")
            rate_text = f"{old_rate}% → {rate}%"
        line = f"• {date} · {source} · {row['program']}: {rate_text}"
        if row["link"]:
            line += f"\n  {row['link']}"
        lines.append(line)
    return "\n".join(lines)
