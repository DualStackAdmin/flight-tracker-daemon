"""Alert decision (a pure function, easy to test) and message formatting."""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime

from .locations import Locations
from .travelpayouts import Offer

AIRLINES = {
    "J2": "AZAL", "TK": "Turkish Airlines", "PC": "Pegasus", "FZ": "flydubai",
    "G9": "Air Arabia", "W6": "Wizz Air", "W9": "Wizz Air UK", "5W": "Wizz Air Abu Dhabi",
    "QR": "Qatar Airways", "EK": "Emirates", "LH": "Lufthansa", "SU": "Aeroflot",
    "A9": "Georgian Airways", "KC": "Air Astana", "HY": "Uzbekistan Airways",
    "XQ": "SunExpress", "VF": "AJet", "6E": "IndiGo", "EY": "Etihad", "LO": "LOT",
    "OS": "Austrian", "BA": "British Airways", "AF": "Air France", "KL": "KLM",
    "SZ": "Somon Air", "DP": "Pobeda", "S7": "S7 Airlines", "UT": "UTair",
    "WZ": "Red Wings", "PS": "UIA", "FV": "Rossiya", "WY": "Oman Air", "GF": "Gulf Air",
    "J9": "Jazeera Airways", "XY": "flynas", "SV": "Saudia", "IR": "Iran Air",
}

# Low-cost carriers: the cheapest fare usually includes only a small under-seat bag.
# The flight-price API has no baggage data, so this is a hint based on the airline.
LOW_COST = {
    "W6", "W9", "W4", "5W", "PC", "VF", "G9", "3O", "E5", "FZ", "J9", "XY", "F3", "DP",
    "FR", "RK", "U2", "EC", "DS", "VY", "V7", "HV", "TO", "LS", "EW", "XQ", "6E", "SG",
    "IX", "G8", "QP", "OV", "A1", "Z9", "ZF", "HO", "DY", "D8", "BJ", "NK", "F9", "G4",
}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class Decision:
    send: bool
    reasons: list[str]


def decide(offer: Offer, *, max_price: float | None, recent_min: float | None,
           drop_percent: float, last_alert: dict | None, cooldown_hours: float,
           now: float) -> Decision:
    reasons: list[str] = []
    if max_price is not None and offer.price <= max_price:
        reasons.append(f"at or below target (≤ {max_price:g})")
    if recent_min is not None and offer.price <= recent_min * (1 - drop_percent / 100):
        pct = (1 - offer.price / recent_min) * 100
        reasons.append(f"{pct:.0f}% below the 30-day low (was {recent_min:g})")
    if not reasons:
        return Decision(False, [])

    if last_alert:
        # Never re-send the same offer at the same (or a higher) price
        if last_alert["offer_key"] == offer.key and offer.price >= last_alert["price"]:
            return Decision(False, reasons)
        # During cooldown, only send if the price dropped further
        within = now - last_alert["sent_at"] < cooldown_hours * 3600
        if within and offer.price >= last_alert["price"]:
            return Decision(False, reasons)
    return Decision(True, reasons)


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d.day} {MONTHS[d.month]} {d.year}, {d:%H:%M}"


def airline_name(code: str) -> str:
    return f"{AIRLINES[code]} ({code})" if code in AIRLINES else code


def stops(n: int | None) -> str:
    if n is None:
        return ""
    return "direct" if n == 0 else f"{n} stop{'s' if n > 1 else ''}"


def baggage_hint(airline: str) -> str:
    if airline in LOW_COST:
        return "🎒 Low-cost airline: cheapest fare is usually hand luggage only, check bag price"
    return "🧳 Full-service airline: checked bag often included, check fare conditions"


def total_line(price: float, currency: str, passengers: int) -> str:
    return f"👨‍👩‍👧 {passengers} passengers: <b>{price * passengers:g} {html.escape(currency)}</b> total"


def _place_line(label: str, city_code: str, airport_code: str | None, loc: Locations) -> str:
    parts = [loc.place(city_code)]
    if airport_code:
        parts.append(loc.airport(airport_code))
    return f"📍 {label}: " + " · ".join(html.escape(p) for p in parts if p)


def format_offer(offer: Offer, reasons: list[str] | None = None,
                 locations: Locations | None = None, passengers: int = 1,
                 with_link: bool = True, extra: list[str] | None = None) -> str:
    e = html.escape
    trip = "round trip" if offer.return_at else "one way"
    if locations:
        o_name, d_name = locations.city(offer.origin)[0], locations.city(offer.destination)[0]
    else:
        o_name, d_name = offer.origin, offer.destination
    lines = [
        f"✈️ <b>{e(o_name)} → {e(d_name)}</b> — <b>{offer.price:g} {e(offer.currency)}</b> ({trip})",
    ]
    if passengers > 1:
        lines.append(total_line(offer.price, offer.currency, passengers))
    if locations:
        lines.append(_place_line("From", offer.origin, offer.origin_airport, locations))
        lines.append(_place_line("To", offer.destination, offer.destination_airport, locations))
    lines += [
        f"🛫 {_fmt_dt(offer.departure_at)} · {stops(offer.transfers)}",
    ]
    if offer.return_at:
        lines.append(f"🛬 {_fmt_dt(offer.return_at)} · {stops(offer.return_transfers)} · {offer.stay_days} days")
    lines.append(f"🏷 {e(airline_name(offer.airline))} {e(offer.flight_number)}")
    if offer.airline:
        lines.append(baggage_hint(offer.airline))
    lines += extra or []          # nearby airports, visa, holidays, weather (already HTML-safe)
    if reasons:
        lines.append("📉 " + "; ".join(e(r) for r in reasons))
    if offer.link and with_link:
        lines.append(f'🔗 <a href="{e(offer.link, quote=True)}">Open ticket</a>')
    lines.append("<i>Cached price: confirm on the site before buying.</i>")
    return "\n".join(lines)


# ---------- inline buttons ----------
def offer_buttons(offer: Offer, route_key: str) -> list[list[dict]]:
    """Inline keyboard under an alert. callback_data must stay under 64 bytes."""
    return alert_buttons(offer.link, offer.destination, route_key)


def alert_buttons(link: str | None, dest: str, route_key: str | None) -> list[list[dict]]:
    rows: list[list[dict]] = []
    if link:
        rows.append([{"text": "✈️ Open ticket", "url": link}])
    row = [{"text": f"🔕 Mute {dest}", "callback_data": f"mute:{dest}"}]
    if route_key:
        row.append({"text": "📊 Price chart", "callback_data": f"chart:{route_key}"})
    rows.append(row)
    return rows


def unmute_buttons(offer_link: str | None, dest: str, route_key: str | None) -> list[list[dict]]:
    rows: list[list[dict]] = []
    if offer_link:
        rows.append([{"text": "✈️ Open ticket", "url": offer_link}])
    row = [{"text": f"🔔 Unmute {dest}", "callback_data": f"unmute:{dest}"}]
    if route_key:
        row.append({"text": "📊 Price chart", "callback_data": f"chart:{route_key}"})
    rows.append(row)
    return rows


# ---------- price chart ----------
def format_chart(route_key: str, rows, locations: Locations | None = None, width: int = 12) -> str:
    """Text bar chart of the lowest price per day (works in any Telegram client)."""
    origin, dest, kind = (route_key.split("-") + ["", "", ""])[:3]
    name = (f"{locations.short(origin)} → {locations.short(dest)}" if locations
            else f"{origin} → {dest}")
    kind_txt = {"rt": "round trip", "ow": "one way", "any": "one way", "anyrt": "round trip"}.get(kind, kind)
    if not rows:
        return f"📊 <b>{html.escape(name)}</b>\nNo price history yet."
    prices = [r["price"] for r in rows]
    lo, hi = min(prices), max(prices)
    cur = rows[-1]["currency"] or ""
    out = [f"📊 <b>{html.escape(name)}</b> ({kind_txt})",
           f"Lowest price per day, last {len(rows)} day(s), {html.escape(cur)}", "<pre>"]
    for r in rows:
        d = datetime.strptime(r["day"], "%Y-%m-%d")
        filled = width if hi == lo else 1 + round((r["price"] - lo) / (hi - lo) * (width - 1))
        bar = "█" * filled + "░" * (width - filled)
        mark = " ◀ low" if r["price"] == lo else ""
        out.append(f"{MONTHS[d.month]} {d.day:>2} {bar} {r['price']:g}{mark}")
    out.append("</pre>")
    out.append(f"Now: <b>{prices[-1]:g}</b> · Low: {lo:g} · High: {hi:g}")
    return "\n".join(out)


# ---------- weekend getaways ----------
def is_weekend_trip(offer: Offer, depart_after_hour: int = 15) -> bool:
    """Leave Friday at/after `depart_after_hour` (local time), come back on Sunday."""
    if not offer.return_at:
        return False
    try:
        dep = datetime.fromisoformat(offer.departure_at)
        ret = datetime.fromisoformat(offer.return_at)
    except ValueError:
        return False
    return (dep.weekday() == 4 and dep.hour >= depart_after_hour
            and ret.weekday() == 6 and (ret.date() - dep.date()).days == 2)


def format_weekend(offers: list[Offer], locations: Locations | None = None,
                   passengers: int = 1, depart_after_hour: int = 15) -> str:
    head = f"🏖 <b>Weekend getaways</b> (Fri after {depart_after_hour}:00 → Sun)"
    if not offers:
        return (head + "\nNo Friday-to-Sunday fares in the cached data right now. "
                "The API only knows fares people searched in the last 48 hours, so try again later.")
    lines = [head]
    for o in offers:
        dep, ret = datetime.fromisoformat(o.departure_at), datetime.fromisoformat(o.return_at)
        place = locations.place(o.destination) if locations else o.destination
        total = f" (×{passengers} = {o.price * passengers:g})" if passengers > 1 else ""
        link = f' <a href="{html.escape(o.link, quote=True)}">↗</a>' if o.link else ""
        lines.append(
            f"• {html.escape(place)}: <b>{o.price:g} {html.escape(o.currency)}</b>{total} · "
            f"Fri {dep.day} {MONTHS[dep.month]} {dep:%H:%M} → Sun {ret:%H:%M} · "
            f"{html.escape(airline_name(o.airline))}{link}")
    lines.append("<i>Prices per person, from cached searches: confirm before buying.</i>")
    return "\n".join(lines)
