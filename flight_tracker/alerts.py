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


def _place_line(label: str, city_code: str, airport_code: str | None, loc: Locations) -> str:
    parts = [loc.place(city_code)]
    if airport_code:
        parts.append(loc.airport(airport_code))
    return f"📍 {label}: " + " · ".join(html.escape(p) for p in parts if p)


def format_offer(offer: Offer, reasons: list[str] | None = None,
                 locations: Locations | None = None) -> str:
    e = html.escape
    trip = "round trip" if offer.return_at else "one way"
    if locations:
        o_name, d_name = locations.city(offer.origin)[0], locations.city(offer.destination)[0]
    else:
        o_name, d_name = offer.origin, offer.destination
    lines = [
        f"✈️ <b>{e(o_name)} → {e(d_name)}</b> — <b>{offer.price:g} {e(offer.currency)}</b> ({trip})",
    ]
    if locations:
        lines.append(_place_line("From", offer.origin, offer.origin_airport, locations))
        lines.append(_place_line("To", offer.destination, offer.destination_airport, locations))
    lines += [
        f"🛫 {_fmt_dt(offer.departure_at)} · {stops(offer.transfers)}",
    ]
    if offer.return_at:
        lines.append(f"🛬 {_fmt_dt(offer.return_at)} · {stops(offer.return_transfers)} · {offer.stay_days} days")
    lines.append(f"🏷 {e(airline_name(offer.airline))} {e(offer.flight_number)}")
    if reasons:
        lines.append("📉 " + "; ".join(e(r) for r in reasons))
    if offer.link:
        lines.append(f'🔗 <a href="{e(offer.link, quote=True)}">Open ticket</a>')
    lines.append("<i>Cached price: confirm on the site before buying.</i>")
    return "\n".join(lines)
