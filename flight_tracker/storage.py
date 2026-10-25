"""SQLite: price history and sent alerts."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .travelpayouts import Offer

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_key TEXT NOT NULL,
    offer_key TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    departure_at TEXT,
    return_at TEXT,
    airline TEXT,
    transfers INTEGER,
    link TEXT,
    seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prices_route ON prices(route_key, seen_at);

CREATE TABLE IF NOT EXISTS sent_alerts (
    route_key TEXT NOT NULL,
    offer_key TEXT NOT NULL,
    price REAL NOT NULL,
    sent_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sent_route ON sent_alerts(route_key, sent_at);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class Storage:
    def __init__(self, path: str | Path):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # --- prices ---
    def record(self, route_key: str, offer: Offer, now: float | None = None) -> None:
        now = now or time.time()
        self.db.execute(
            "INSERT INTO prices(route_key, offer_key, price, currency, departure_at, return_at,"
            " airline, transfers, link, seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (route_key, offer.key, offer.price, offer.currency, offer.departure_at,
             offer.return_at, offer.airline, offer.transfers, offer.link, now),
        )
        self.db.commit()

    def recent_min(self, route_key: str, days: float = 30, now: float | None = None) -> float | None:
        """Lowest price seen for this route in the last N days (excluding the current scan)."""
        now = now or time.time()
        row = self.db.execute(
            "SELECT MIN(price) AS p FROM prices WHERE route_key=? AND seen_at >= ? AND seen_at < ?",
            (route_key, now - days * 86400, now),
        ).fetchone()
        return row["p"] if row and row["p"] is not None else None

    def latest_per_route(self, since_hours: float = 48) -> list[sqlite3.Row]:
        """Cheapest price per route seen in the last N hours."""
        cutoff = time.time() - since_hours * 3600
        return self.db.execute(
            "SELECT route_key, MIN(price) AS price, currency, departure_at, return_at, airline,"
            " transfers, link FROM prices WHERE seen_at >= ? GROUP BY route_key ORDER BY price",
            (cutoff,),
        ).fetchall()

    def daily_min(self, route_key: str, days: int = 30) -> list[sqlite3.Row]:
        """Lowest price per day for a route (for the price chart)."""
        cutoff = time.time() - days * 86400
        return self.db.execute(
            "SELECT date(seen_at, 'unixepoch') AS day, MIN(price) AS price, MAX(currency) AS currency"
            " FROM prices WHERE route_key=? AND seen_at >= ? GROUP BY day ORDER BY day",
            (route_key, cutoff),
        ).fetchall()

    def prune(self, keep_days: int = 90) -> None:
        cutoff = time.time() - keep_days * 86400
        self.db.execute("DELETE FROM prices WHERE seen_at < ?", (cutoff,))
        self.db.execute("DELETE FROM sent_alerts WHERE sent_at < ?", (cutoff,))
        self.db.commit()

    # --- alerts ---
    def last_alert(self, route_key: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM sent_alerts WHERE route_key=? ORDER BY sent_at DESC LIMIT 1",
            (route_key,),
        ).fetchone()

    def mark_alert(self, route_key: str, offer: Offer, now: float | None = None) -> None:
        self.db.execute(
            "INSERT INTO sent_alerts(route_key, offer_key, price, sent_at) VALUES (?,?,?,?)",
            (route_key, offer.key, offer.price, now or time.time()),
        )
        self.db.commit()

    # --- meta ---
    def get_meta(self, k: str) -> str | None:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None

    def set_meta(self, k: str, v: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?,?)", (k, v))
        self.db.commit()
