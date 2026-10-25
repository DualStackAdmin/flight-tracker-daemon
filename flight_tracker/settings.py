"""Live settings changed from Telegram, stored in SQLite.

config.yaml stays the baseline. Changes made with /addroute, /removeroute,
/setlimit, /family and /mute are stored in the database and applied on top of it,
so they survive restarts and need no SSH.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict

from .config import Config, Route
from .storage import Storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS dyn_routes (destination TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS removed_routes (destination TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS muted (code TEXT PRIMARY KEY, muted_at REAL NOT NULL);
"""

IATA = re.compile(r"^[A-Z]{3}$")


def valid_code(code: str) -> bool:
    return bool(IATA.match(code or ""))


class Settings:
    def __init__(self, cfg: Config, storage: Storage):
        self.cfg = cfg
        self.db = storage.db
        self.storage = storage
        self.db.executescript(SCHEMA)

    # ---------- routes ----------
    def routes(self) -> list[Route]:
        """config.yaml routes, minus removed ones, plus/overridden by Telegram changes."""
        removed = {r[0] for r in self.db.execute("SELECT destination FROM removed_routes")}
        out = {r.destination: r for r in self.cfg.routes if r.destination not in removed}
        for dest, data in self.db.execute("SELECT destination, data FROM dyn_routes"):
            out[dest] = Route(**json.loads(data))
        return list(out.values())

    def get_route(self, dest: str) -> Route | None:
        return next((r for r in self.routes() if r.destination == dest), None)

    def add_route(self, route: Route) -> None:
        self.db.execute("INSERT OR REPLACE INTO dyn_routes(destination, data) VALUES (?, ?)",
                        (route.destination, json.dumps(asdict(route))))
        self.db.execute("DELETE FROM removed_routes WHERE destination=?", (route.destination,))
        self.db.commit()

    def remove_route(self, dest: str) -> bool:
        existed = self.get_route(dest) is not None
        self.db.execute("DELETE FROM dyn_routes WHERE destination=?", (dest,))
        if any(r.destination == dest for r in self.cfg.routes):
            self.db.execute("INSERT OR IGNORE INTO removed_routes(destination) VALUES (?)", (dest,))
        self.db.commit()
        return existed

    def set_route_limit(self, dest: str, price: float | None) -> bool:
        route = self.get_route(dest)
        if route is None:
            return False
        route.max_price = price
        self.add_route(route)
        return True

    # ---------- anywhere limit ----------
    @property
    def anywhere_max_price(self) -> float | None:
        v = self.storage.get_meta("anywhere_max_price")
        if v is None:
            return self.cfg.anywhere.max_price
        return float(v) if v else None

    @anywhere_max_price.setter
    def anywhere_max_price(self, value: float | None) -> None:
        self.storage.set_meta("anywhere_max_price", "" if value is None else str(value))

    # ---------- passengers ----------
    @property
    def passengers(self) -> int:
        v = self.storage.get_meta("passengers")
        return int(v) if v else max(1, int(self.cfg.passengers))

    @passengers.setter
    def passengers(self, n: int) -> None:
        self.storage.set_meta("passengers", str(int(n)))

    # ---------- mute list ----------
    def mute(self, code: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO muted(code, muted_at) VALUES (?, ?)", (code, time.time()))
        self.db.commit()

    def unmute(self, code: str) -> bool:
        cur = self.db.execute("DELETE FROM muted WHERE code=?", (code,))
        self.db.commit()
        return cur.rowcount > 0

    def muted(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT code FROM muted")}


def parse_addroute(args: list[str]) -> Route:
    """/addroute PAR [150] [rt 4-10] [direct]  ->  Route. Raises ValueError with a friendly message."""
    if not args:
        raise ValueError("Usage: /addroute PAR [max_price] [rt 4-10] [direct]")
    dest = args[0].upper()
    if not valid_code(dest):
        raise ValueError(f"'{args[0]}' is not a 3-letter IATA code (e.g. PAR, IST, DXB)")
    route = Route(destination=dest)
    rest = [a.lower() for a in args[1:]]
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("rt", "return", "roundtrip"):
            route.round_trip = True
            if i + 1 < len(rest) and re.fullmatch(r"\d+-\d+", rest[i + 1]):
                lo, hi = map(int, rest[i + 1].split("-"))
                if lo > hi or lo < 1:
                    raise ValueError("Stay must look like 4-10 (min-max days)")
                route.min_days, route.max_days = lo, hi
                i += 1
        elif a in ("direct", "nonstop"):
            route.direct_only = True
        else:
            try:
                route.max_price = float(a)
            except ValueError:
                raise ValueError(f"Unknown option '{a}'. Usage: /addroute PAR [max_price] [rt 4-10] [direct]")
            if route.max_price <= 0:
                raise ValueError("Price must be positive")
        i += 1
    return route
