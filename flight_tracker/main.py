"""Entry point: python -m flight_tracker [--once] [--dry-run]"""
from __future__ import annotations

import argparse
import asyncio
import html
import logging
import random
import signal
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .alerts import airline_name, stops
from .config import Config, load_config
from .locations import Locations
from .scanner import Scanner
from .storage import Storage
from .telegram import Telegram
from .travelpayouts import TravelpayoutsClient

log = logging.getLogger("flight_tracker")

HELP = (
    "<b>Flight Tracker</b>\n"
    "/top: cheapest prices from the last 48 hours\n"
    "/scan: scan now\n"
    "/status: service status\n"
    "/routes: watched routes\n"
    "/help: this message"
)


def format_top(storage: Storage, limit: int = 15, locations: Locations | None = None) -> str:
    rows = storage.latest_per_route(since_hours=48)[:limit]
    if not rows:
        return "No data yet. Send /scan."
    lines = ["<b>🏆 Cheapest (last 48 hours)</b>"]
    for r in rows:
        dep = r["departure_at"][:10] if r["departure_at"] else ""
        ret = f"–{r['return_at'][:10]}" if r["return_at"] else ""
        route = r["route_key"].rsplit("-", 1)[0].replace("-", "→")
        if locations:
            o, d = r["route_key"].split("-")[:2]
            route = html.escape(f"{locations.short(o)} → {locations.short(d)}")
        link = f' <a href="{html.escape(r["link"], quote=True)}">↗</a>' if r["link"] else ""
        lines.append(f"• {route}: <b>{r['price']:g} {r['currency']}</b> · {dep}{ret} · "
                     f"{airline_name(r['airline'])} · {stops(r['transfers'])}{link}")
    return "\n".join(lines)


class App:
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.storage = Storage(cfg.db_path)
        self.api = TravelpayoutsClient(cfg.travelpayouts_token, cfg.currency, cfg.market,
                                       cfg.travelpayouts_marker)
        self.tg = None if dry_run else Telegram(cfg.telegram_token, cfg.telegram_chat_ids)
        self.locations = Locations(Path(cfg.db_path).parent / "locations.json")
        self.scanner = Scanner(cfg, self.api, self.storage, self.notify,
                               locations=self.locations)
        self.stop = asyncio.Event()
        self.started = time.time()

    async def notify(self, text: str) -> None:
        if self.tg is None:
            print("\n" + text + "\n")
        else:
            await self.tg.send(text)

    async def handle_command(self, chat_id: int, text: str) -> str | None:
        cmd = text.split()[0].split("@")[0].lower()
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/top":
            return format_top(self.storage, locations=self.locations)
        if cmd == "/scan":
            asyncio.create_task(self._scan_and_report(chat_id))
            return "🔎 Scan started..."
        if cmd == "/routes":
            lines = [f"Origin: <b>{self.cfg.origin}</b>, {self.cfg.months_ahead} months ahead, "
                     f"every {self.cfg.scan_interval_minutes} min"]
            for r in self.cfg.routes:
                extra = f"round trip {r.min_days}–{r.max_days} days" if r.round_trip else "one way"
                lim = f", target ≤ {r.max_price:g}" if r.max_price else ""
                lines.append(f"• {r.destination}: {extra}{lim}{', direct only' if r.direct_only else ''}")
            if self.cfg.anywhere.enabled:
                lim = f" (target ≤ {self.cfg.anywhere.max_price:g})" if self.cfg.anywhere.max_price else ""
                lines.append(f"• Anywhere{lim}")
            return "\n".join(lines)
        if cmd == "/status":
            lr = self.scanner.last_result
            last = (datetime.fromtimestamp(self.scanner.last_run, ZoneInfo(self.cfg.timezone))
                    .strftime("%d.%m %H:%M") if self.scanner.last_run else "not yet")
            up_h = (time.time() - self.started) / 3600
            s = f"✅ Running ({up_h:.1f} h)\nLast scan: {last}"
            if lr:
                s += f"\nRoutes: {lr.checked}, alerts: {lr.alerts}, errors: {len(lr.errors)}"
                if lr.errors:
                    s += "\n" + "\n".join(lr.errors[:5])
            return s
        return "Unknown command. /help"

    async def _scan_and_report(self, chat_id: int) -> None:
        res = await self.scanner.run_once()
        msg = f"Scan finished: {res.checked} routes, {res.alerts} new alerts."
        if res.errors:
            msg += f"\n⚠️ {len(res.errors)} errors (/status)"
        await self.tg.send(msg + "\n\n" + format_top(self.storage, 10, self.locations), chat_id)

    async def maybe_digest(self) -> None:
        if self.cfg.digest_hour is None:
            return
        now = datetime.now(ZoneInfo(self.cfg.timezone))
        today = now.strftime("%Y-%m-%d")
        if now.hour >= int(self.cfg.digest_hour) and self.storage.get_meta("last_digest") != today:
            self.storage.set_meta("last_digest", today)
            await self.notify("☀️ <b>Daily digest</b>\n\n" + format_top(self.storage, 10, self.locations))

    async def scan_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await self.scanner.run_once()
                await self.maybe_digest()
                self.storage.prune()
            except Exception:  # noqa: BLE001 - the service must never die
                log.exception("Scan loop error")
            # Small random jitter so requests do not always hit the same second
            wait = self.cfg.scan_interval_minutes * 60 * random.uniform(0.9, 1.1)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        if self.tg:
            await self.tg.send("🚀 Flight Tracker started. /help")
        tasks = [asyncio.create_task(self.scan_loop())]
        if self.tg:
            tasks.append(asyncio.create_task(self.tg.poll(self.handle_command, self.stop)))
        await self.stop.wait()
        log.info("Shutting down...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.close()

    async def close(self) -> None:
        await self.api.aclose()
        if self.tg:
            await self.tg.aclose()
        self.storage.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="flight_tracker", description="Cheap flight scanner with Telegram alerts")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-e", "--env", default=".env")
    p.add_argument("--once", action="store_true", help="scan once and exit")
    p.add_argument("--dry-run", action="store_true", help="print to stdout instead of Telegram")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keep tokens out of the logs

    try:
        cfg = load_config(args.config, args.env)
        app = App(cfg, dry_run=args.dry_run)
    except (ValueError, FileNotFoundError) as e:
        log.error("Configuration error: %s", e)
        raise SystemExit(1)

    async def once():
        try:
            await app.scanner.run_once()
            print(format_top(app.storage, locations=app.locations))
        finally:
            await app.close()

    asyncio.run(once() if args.once else app.run())


if __name__ == "__main__":
    main()
