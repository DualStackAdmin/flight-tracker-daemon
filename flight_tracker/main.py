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

from .alerts import (MONTHS, airline_name, alert_buttons, format_chart, format_weekend, stops,
                     unmute_buttons)
from .config import Config, load_config
from .extras import AltOrigins, Climate, Enricher
from .locations import Locations
from .scanner import Scanner
from .settings import (Settings, describe_trip, parse_addroute, parse_trip,
                       route_rt_target, valid_code)
from .storage import Storage
from .telegram import Telegram
from .travelpayouts import TravelpayoutsClient

log = logging.getLogger("flight_tracker")

GUIDE = """👋 <b>Welcome to Flight Tracker</b>
I watch flight prices from <b>{origin}</b> 24/7 and message you when a fare is cheap.

<b>When you get an alert</b>
• the fare is at or below your target price, or
• it is {drop:g}% below the lowest price of the last 30 days.
The same deal is never sent twice.

<b>Buttons under each alert</b>
✈️ Open ticket: see and book it on the website
🔕 Mute: no more alerts for that city (tap 🔔 to undo)
📊 Price chart: lowest price per day, last 30 days

<b>Look things up</b>
/top: cheapest fares right now
/weekend [150]: Friday evening → Sunday trips
/chart IST: price history of a city
/routes: what I watch and your settings

<b>Change what I watch</b> (saved on the server, no restart)
/addroute PAR 150: watch Paris, alert at 150 or less
/addroute DXB 400 rt 4-10: round trip, 4–10 day stay
/addroute IST 150 both 3-7: one way AND round trip
/trip IST both 3-7: change the trip type of a city
/trip anywhere both 3-7: same for the worldwide search
/setlimit IST 120: new target for one city
/setlimit 100: new target for the worldwide search
/removeroute PAR · /mute IST · /unmute IST · /muted

<b>Your trip</b>
/family 2: also show the total for 2 passengers
/alt TBS KUT: also check nearby airports (e.g. a cheaper flight from Kutaisi)
/holidays: upcoming public holidays

<b>Extra lines in alerts</b>
💡 the same trip is much cheaper from a nearby airport
🟢 visa-free · 🟡 eVisa / on arrival · 🔴 visa needed · ❔ check
🌟 a round trip that needs few or no vacation days
🌡 typical day / night temperature for that month

<b>Service</b>
/scan: scan now · /status: health · /help: short list

<b>Good to know</b>
• Use city codes: PAR, LON, IST, DXB, TBS, NYC...
• Prices are per adult, in {currency}, from searches made in the last 48 hours. Always check the price on the site before buying.
• If a round-trip target is not set, it is twice the one-way target.
• 🎒/🧳 baggage, visa and weather lines are hints: confirm before you travel."""

HELP = (
    "<b>Flight Tracker</b> (/start for the full guide)\n"
    "/top: cheapest prices from the last 48 hours\n"
    "/weekend [max_price]: Friday evening → Sunday getaways\n"
    "/scan: scan now\n"
    "/routes: watched routes and settings\n"
    "/addroute PAR [150] [rt 4-10 | both 4-10] [direct]: watch a new destination\n"
    "/trip IST|anywhere oneway|round|both [3-7] [price]: trip type\n"
    "/removeroute PAR: stop watching a destination\n"
    "/setlimit 150 or /setlimit IST 150: change a target price\n"
    "/family 3: show total prices for 3 passengers\n"
    "/alt TBS KUT: compare with nearby airports (/alt off)\n"
    "/holidays: upcoming public holidays\n"
    "/mute IST, /unmute IST, /muted: mute list\n"
    "/chart IST: price history\n"
    "/status: service status\n"
    "/help: this message"
)

COMMANDS = [
    ("top", "Cheapest prices (48h)"), ("weekend", "Fri evening to Sun getaways"),
    ("scan", "Scan now"), ("routes", "Watched routes and settings"),
    ("addroute", "Watch a destination: /addroute PAR 150"),
    ("removeroute", "Stop watching: /removeroute PAR"),
    ("trip", "One way / round / both: /trip IST both 3-7"),
    ("setlimit", "Target price: /setlimit IST 150"), ("family", "Passengers: /family 3"),
    ("alt", "Nearby airports: /alt TBS KUT"), ("holidays", "Upcoming public holidays"),
    ("mute", "Mute a destination"), ("unmute", "Unmute a destination"), ("muted", "Muted list"),
    ("chart", "Price history: /chart IST"), ("status", "Service status"), ("help", "Short command list"),
    ("start", "Full user guide"),
]


def format_top(storage: Storage, limit: int = 15, locations: Locations | None = None,
               passengers: int = 1) -> str:
    rows = storage.latest_per_route(since_hours=48)[:limit]
    if not rows:
        return "No data yet. Send /scan."
    head = "<b>🏆 Cheapest (last 48 hours)</b>"
    if passengers > 1:
        head += f"\n<i>Per person, total for {passengers} passengers in brackets</i>"
    lines = [head]
    for r in rows:
        dep = r["departure_at"][:10] if r["departure_at"] else ""
        ret = f"–{r['return_at'][:10]}" if r["return_at"] else ""
        route = r["route_key"].rsplit("-", 1)[0].replace("-", "→")
        if locations:
            o, d = r["route_key"].split("-")[:2]
            route = html.escape(f"{locations.short(o)} → {locations.short(d)}")
        link = f' <a href="{html.escape(r["link"], quote=True)}">↗</a>' if r["link"] else ""
        total = f" ({r['price'] * passengers:g})" if passengers > 1 else ""
        lines.append(f"• {route}: <b>{r['price']:g} {r['currency']}</b>{total} · {dep}{ret} · "
                     f"{airline_name(r['airline'])} · {stops(r['transfers'])}{link}")
    return "\n".join(lines)


def _price(arg: str) -> float:
    try:
        v = float(arg)
    except ValueError:
        raise ValueError(f"'{arg}' is not a number")
    if v <= 0:
        raise ValueError("Price must be positive")
    return v


def _targets(trip: str, ow_price: float | None, rt_price: float | None) -> str:
    parts = []
    if trip in ("ow", "both") and ow_price:
        parts.append(f"one way ≤ {ow_price:g}" if trip == "both" else f"≤ {ow_price:g}")
    if trip in ("rt", "both") and rt_price:
        parts.append(f"round trip ≤ {rt_price:g}" if trip == "both" else f"≤ {rt_price:g}")
    return f", target {' / '.join(parts)}" if parts else ""


class App:
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.storage = Storage(cfg.db_path)
        self.settings = Settings(cfg, self.storage)
        self.api = TravelpayoutsClient(cfg.travelpayouts_token, cfg.currency, cfg.market,
                                       cfg.travelpayouts_marker)
        self.tg = None if dry_run else Telegram(cfg.telegram_token, cfg.telegram_chat_ids)
        self.locations = Locations(Path(cfg.db_path).parent / "locations.json")
        data_dir = Path(cfg.db_path).parent
        self.enricher = Enricher(
            locations=self.locations, origin=cfg.origin,
            passport=cfg.passport, holidays_country=cfg.holidays,
            climate=Climate(data_dir / "climate.json", enabled=cfg.climate),
            alt=AltOrigins(self.api, cfg.alt_flex_days, cfg.alt_min_saving_percent),
            alt_origins=self.settings.alt_origins)
        self.scanner = Scanner(cfg, self.api, self.storage, self.notify,
                               locations=self.locations, settings=self.settings,
                               enricher=self.enricher)
        self.stop = asyncio.Event()
        self.started = time.time()

    async def notify(self, text: str, buttons: list | None = None) -> None:
        if self.tg is None:
            labels = " ".join(f"[{b['text']}]" for row in (buttons or []) for b in row)
            print("\n" + text + ("\n" + labels if labels else "") + "\n")
        else:
            await self.tg.send(text, buttons=buttons)

    def _route_key_for(self, dest: str) -> str | None:
        """Find the route key with history for a destination (for /chart IST)."""
        for kind in ("ow", "rt", "any", "anyrt"):
            key = f"{self.cfg.origin}-{dest}-{kind}"
            if self.storage.daily_min(key, days=30):
                return key
        return None

    def _routes_text(self) -> str:
        s = self.settings
        lines = [f"Origin: <b>{self.cfg.origin}</b>, {self.cfg.months_ahead} months ahead, "
                 f"every {self.cfg.scan_interval_minutes} min"]
        muted = s.muted()
        for r in s.routes():
            extra = describe_trip(r.trip, r.min_days, r.max_days)
            lim = _targets(r.trip, r.max_price, route_rt_target(r))
            mute = " 🔕" if r.destination in muted else ""
            lines.append(f"• {r.destination}: {extra}{lim}{', direct only' if r.direct_only else ''}{mute}")
        if self.cfg.anywhere.enabled:
            lo, hi = s.anywhere_stay
            trip = s.anywhere_trip
            lim = _targets(trip, s.anywhere_max_price, s.anywhere_rt_max_price)
            lines.append(f"• Anywhere: {describe_trip(trip, lo, hi)}{lim}")
        else:
            lines.append("• Anywhere: off (anywhere.enabled in config.yaml)")
        alts = s.alt_origins()
        lines.append("Nearby airports: " + (", ".join(a.code for a in alts) if alts else "off (/alt TBS KUT)"))
        lines.append(f"Passengers: {s.passengers}")
        if muted:
            lines.append("Muted: " + ", ".join(sorted(muted)))
        return "\n".join(lines)

    async def handle_command(self, chat_id: int, text: str) -> str | None:
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        s = self.settings
        try:
            if cmd == "/start":
                return self.guide()
            if cmd == "/help":
                return HELP
            if cmd == "/top":
                return format_top(self.storage, locations=self.locations, passengers=s.passengers)
            if cmd == "/scan":
                asyncio.create_task(self._scan_and_report(chat_id))
                return "🔎 Scan started..."
            if cmd == "/weekend":
                max_price = _price(args[0]) if args else None
                asyncio.create_task(self._weekend_and_report(chat_id, max_price))
                return "🏖 Looking for Friday → Sunday getaways..."
            if cmd == "/routes":
                return self._routes_text()
            if cmd == "/addroute":
                route = parse_addroute(args)
                s.add_route(route)
                return f"✅ Watching {route.destination}. It will be checked on the next scan (/scan to run now).\n\n" + self._routes_text()
            if cmd == "/trip":
                target, mode, stay, price = parse_trip(args)
                if target == "ANYWHERE":
                    s.set_anywhere_trip(mode, stay, price)
                    note = "" if self.cfg.anywhere.enabled else \
                        "\n⚠️ The worldwide search is off: set anywhere.enabled: true in config.yaml."
                    return f"✅ Worldwide search: {describe_trip(mode, *s.anywhere_stay)}.{note}\n\n" \
                        + self._routes_text()
                if not s.set_route_trip(target, mode, stay, price):
                    return (f"{target} is not in your routes. Add it first, e.g. "
                            f"/addroute {target} 150 {'both 3-7' if mode == 'both' else 'rt 3-7' if mode == 'rt' else ''}".rstrip())
                return f"✅ {target}: {describe_trip(mode, *self._stay(target))}.\n\n" + self._routes_text()
            if cmd == "/alt":
                if not args:
                    alts = s.alt_origins()
                    return ("Nearby airports: " + ", ".join(a.code for a in alts) if alts else
                            "Nearby airports are off.") + \
                        "\nSet with /alt TBS KUT, turn off with /alt off."
                if args[0].lower() in ("off", "none", "clear"):
                    s.set_alt_origins([])
                    return "✅ Nearby airport comparison turned off."
                codes = [a.upper() for a in args]
                bad = [c for c in codes if not valid_code(c)]
                if bad:
                    return f"⚠️ Not IATA codes: {', '.join(bad)}. Example: /alt TBS KUT"
                s.set_alt_origins(codes[:4])
                return (f"✅ Each alert will also check {', '.join(codes[:4])} for the same dates "
                        f"(±{self.cfg.alt_flex_days} days) and tell you if it is "
                        f"at least {self.cfg.alt_min_saving_percent:g}% cheaper.")
            if cmd == "/holidays":
                self.enricher.resolve()
                hol = self.enricher.holidays
                if not hol or not hol.enabled:
                    return "No holiday calendar for your country yet (extras/holidays_XX.yaml)."
                today = datetime.now(ZoneInfo(self.cfg.timezone)).date()
                rows = [f"• {d.day} {MONTHS[d.month]} {d.year} ({d:%a}): {html.escape(n, quote=False)}"
                        for d, n in hol.upcoming(today)]
                return "🗓 <b>Upcoming public holidays</b>\n" + "\n".join(rows) + \
                    "\n<i>Round trips over these days get a 🌟 when few vacation days are needed.</i>"
            if cmd == "/removeroute":
                if not args:
                    return "Usage: /removeroute PAR"
                dest = args[0].upper()
                return (f"🗑 Removed {dest}." if s.remove_route(dest) else f"{dest} is not in your routes.") \
                    + "\n\n" + self._routes_text()
            if cmd == "/setlimit":
                if len(args) == 1:
                    s.anywhere_max_price = _price(args[0])
                    return f"✅ 'Anywhere' target price set to {args[0]}."
                if len(args) == 2:
                    dest = args[0].upper()
                    if not valid_code(dest):
                        return "Usage: /setlimit IST 150"
                    if not s.set_route_limit(dest, _price(args[1])):
                        return f"{dest} is not in your routes. Add it first: /addroute {dest} {args[1]}"
                    return f"✅ Target price for {dest} set to {args[1]}."
                return "Usage: /setlimit 150 (anywhere) or /setlimit IST 150 (one route)"
            if cmd == "/family":
                if not args:
                    return f"Passengers: {s.passengers}. Change with /family 3 (reset with /family 1)."
                n = int(args[0]) if args[0].isdigit() else 0
                if not 1 <= n <= 9:
                    return "Passengers must be a number from 1 to 9."
                s.passengers = n
                return (f"✅ Prices will also show the total for {n} passengers."
                        if n > 1 else "✅ Back to single-passenger prices.") + \
                    "\n<i>Fares are per adult; child and infant fares are often cheaper.</i>"
            if cmd in ("/mute", "/unmute"):
                if not args or not valid_code(args[0].upper()):
                    return f"Usage: {cmd} IST"
                dest = args[0].upper()
                if cmd == "/mute":
                    s.mute(dest)
                    return f"🔕 {dest} muted. No more alerts for it. /unmute {dest} to undo."
                return f"🔔 {dest} unmuted." if s.unmute(dest) else f"{dest} was not muted."
            if cmd == "/muted":
                m = sorted(s.muted())
                return "Muted: " + ", ".join(m) if m else "Nothing is muted."
            if cmd == "/chart":
                if not args:
                    return "Usage: /chart IST"
                dest = args[0].upper()
                key = self._route_key_for(dest)
                if not key:
                    return f"No price history for {dest} yet."
                return format_chart(key, self.storage.daily_min(key), self.locations)
            if cmd == "/status":
                lr = self.scanner.last_result
                last = (datetime.fromtimestamp(self.scanner.last_run, ZoneInfo(self.cfg.timezone))
                        .strftime("%d.%m %H:%M") if self.scanner.last_run else "not yet")
                up_h = (time.time() - self.started) / 3600
                out = f"✅ Running ({up_h:.1f} h)\nLast scan: {last}"
                if lr:
                    out += f"\nRoutes: {lr.checked}, alerts: {lr.alerts}, errors: {len(lr.errors)}"
                    if lr.errors:
                        out += "\n" + "\n".join(lr.errors[:5])
                return out
        except ValueError as e:
            return f"⚠️ {e}"
        return "Unknown command. /help"

    def guide(self) -> str:
        return GUIDE.format(origin=html.escape(self.cfg.origin),
                            currency=html.escape(self.cfg.currency.upper()),
                            drop=self.cfg.alerts.drop_percent)

    def _stay(self, dest: str) -> tuple[int, int]:
        r = self.settings.get_route(dest)
        return (r.min_days, r.max_days) if r else (3, 14)

    async def handle_callback(self, chat_id: int, data: str, message: dict):
        """Inline button presses. Returns (toast, reply_text, new_buttons)."""
        action, _, value = data.partition(":")
        buttons = [b for row in (message.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]
        link = next((b["url"] for b in buttons if b.get("url")), None)
        route_key = next((b["callback_data"][len("chart:"):] for b in buttons
                          if b.get("callback_data", "").startswith("chart:")), None)
        if action == "mute" and valid_code(value):
            self.settings.mute(value)
            return f"🔕 {value} muted", None, unmute_buttons(link, value, route_key)
        if action == "unmute" and valid_code(value):
            self.settings.unmute(value)
            return f"🔔 {value} unmuted", None, alert_buttons(link, value, route_key)
        if action == "chart" and value:
            return "📊", format_chart(value, self.storage.daily_min(value), self.locations), None
        return "Unknown button", None, None

    async def _scan_and_report(self, chat_id: int) -> None:
        res = await self.scanner.run_once()
        msg = f"Scan finished: {res.checked} routes, {res.alerts} new alerts."
        if res.errors:
            msg += f"\n⚠️ {len(res.errors)} errors (/status)"
        await self.tg.send(msg + "\n\n" + format_top(self.storage, 10, self.locations,
                                                     self.settings.passengers), chat_id)

    async def _weekend_and_report(self, chat_id: int, max_price: float | None) -> None:
        deals = await self.scanner.weekend_deals(max_price=max_price)
        await self.tg.send(format_weekend(deals, self.locations, self.settings.passengers), chat_id)

    async def maybe_digest(self) -> None:
        if self.cfg.digest_hour is None:
            return
        now = datetime.now(ZoneInfo(self.cfg.timezone))
        today = now.strftime("%Y-%m-%d")
        if now.hour >= int(self.cfg.digest_hour) and self.storage.get_meta("last_digest") != today:
            self.storage.set_meta("last_digest", today)
            await self.notify("☀️ <b>Daily digest</b>\n\n" + format_top(self.storage, 10, self.locations, self.settings.passengers))

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
            await self.tg.set_commands(COMMANDS)
            if self.storage.get_meta("guide_sent"):
                await self.tg.send("🚀 Flight Tracker started. /help")
            else:  # very first start: send the full user guide once
                await self.tg.send("🚀 Flight Tracker started.\n\n" + self.guide())
                self.storage.set_meta("guide_sent", "1")
        tasks = [asyncio.create_task(self.scan_loop())]
        if self.tg:
            tasks.append(asyncio.create_task(self.tg.poll(self.handle_command, self.stop, self.handle_callback)))
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
