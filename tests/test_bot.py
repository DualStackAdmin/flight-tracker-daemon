import time
from datetime import date

import httpx
import pytest
import respx

from flight_tracker.alerts import decide, format_offer
from flight_tracker.config import Alerts, Anywhere, Config, Route, load_config
from flight_tracker.scanner import Scanner, month_starts
from flight_tracker.storage import Storage
from flight_tracker.travelpayouts import ApiError, Offer, TravelpayoutsClient

API = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


def row(dest, price, dep, ret=None, airline="J2", transfers=0):
    return {"origin": "BAK", "destination": dest, "price": price, "airline": airline,
            "flight_number": "61", "departure_at": dep, "return_at": ret,
            "transfers": transfers, "return_transfers": 0 if ret else None,
            "duration": 180, "link": f"/search/BAK{dest}?t=x"}


def offer(price, key_dep="2026-12-05T08:00:00+04:00"):
    return Offer("BAK", "IST", key_dep, None, price, "USD", "J2", "61", 0, None, 180, "https://x")


# ---------- alert logic ----------
def test_decide_under_target():
    d = decide(offer(90), max_price=100, recent_min=None, drop_percent=10,
               last_alert=None, cooldown_hours=12, now=time.time())
    assert d.send and "target" in d.reasons[0]


def test_decide_no_reason():
    d = decide(offer(150), max_price=100, recent_min=155, drop_percent=10,
               last_alert=None, cooldown_hours=12, now=time.time())
    assert not d.send


def test_decide_drop():
    d = decide(offer(80), max_price=None, recent_min=100, drop_percent=15,
               last_alert=None, cooldown_hours=12, now=time.time())
    assert d.send and "20%" in d.reasons[0]


def test_decide_dedup_and_cooldown():
    now = time.time()
    o = offer(90)
    same = {"offer_key": o.key, "price": 90, "sent_at": now - 100 * 3600}
    assert not decide(o, max_price=100, recent_min=None, drop_percent=10,
                      last_alert=same, cooldown_hours=12, now=now).send
    other_recent = {"offer_key": "x", "price": 85, "sent_at": now - 3600}
    assert not decide(o, max_price=100, recent_min=None, drop_percent=10,
                      last_alert=other_recent, cooldown_hours=12, now=now).send
    other_old = {"offer_key": "x", "price": 85, "sent_at": now - 13 * 3600}
    assert decide(o, max_price=100, recent_min=None, drop_percent=10,
                  last_alert=other_old, cooldown_hours=12, now=now).send
    cheaper = offer(80)
    assert decide(cheaper, max_price=100, recent_min=None, drop_percent=10,
                  last_alert=other_recent, cooldown_hours=12, now=now).send


def test_format_escapes_and_roundtrip():
    o = Offer("BAK", "DXB", "2026-12-05T08:00:00+04:00", "2026-12-12T10:00:00+04:00",
              300, "USD", "FZ", "712", 0, 1, 600, "https://a?b=1&c=<2>")
    txt = format_offer(o, ["test <x>"])
    assert "round trip" in txt and "7 days" in txt and "flydubai" in txt
    assert "&lt;x&gt;" in txt and "&amp;c=&lt;2&gt;" in txt


def test_month_starts_wraps_year():
    assert month_starts(date(2026, 11, 20), 3) == [date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)]


# ---------- config ----------
def test_load_config(tmp_path, monkeypatch):
    for k in ("TRAVELPAYOUTS_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / ".env").write_text("TRAVELPAYOUTS_TOKEN=abc\nTELEGRAM_CHAT_ID=1, 2\n")
    import shutil, pathlib
    shutil.copy(pathlib.Path(__file__).parent.parent / "config.example.yaml", tmp_path / "c.yaml")
    cfg = load_config(tmp_path / "c.yaml", tmp_path / ".env")
    assert cfg.origin == "BAK" and cfg.travelpayouts_token == "abc"
    assert cfg.telegram_chat_ids == [1, 2]
    assert any(r.round_trip and r.destination == "DXB" for r in cfg.routes)
    assert cfg.anywhere.enabled


def test_dotenv_last_line_wins_and_env_has_priority(tmp_path, monkeypatch):
    monkeypatch.delenv("TRAVELPAYOUTS_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-env")
    (tmp_path / ".env").write_text(
        "TRAVELPAYOUTS_TOKEN=\nTRAVELPAYOUTS_TOKEN='abc'\nTELEGRAM_BOT_TOKEN=from-file\n")
    (tmp_path / "c.yaml").write_text("origin: ist\n")
    cfg = load_config(tmp_path / "c.yaml", tmp_path / ".env")
    assert cfg.travelpayouts_token == "abc"
    assert cfg.telegram_token == "from-env"
    assert cfg.origin == "IST"


# ---------- API client ----------
@pytest.mark.asyncio
@respx.mock
async def test_client_parses_and_builds_link():
    respx.get(API).mock(return_value=httpx.Response(200, json={
        "success": True, "data": [row("IST", 95, "2026-12-05T08:00:00+04:00")]}))
    c = TravelpayoutsClient("tok", marker="777")
    offers = await c.cheapest_for_month("BAK", "IST", date(2026, 12, 1))
    await c.aclose()
    assert offers[0].price == 95
    assert offers[0].link == "https://www.aviasales.com/search/BAKIST?t=x&marker=777"
    req = respx.calls.last.request
    assert req.headers["X-Access-Token"] == "tok"
    assert req.url.params["departure_at"] == "2026-12" and req.url.params["one_way"] == "true"


@pytest.mark.asyncio
@respx.mock
async def test_client_retries_then_401(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _nosleep)
    route = respx.get(API).mock(side_effect=[httpx.Response(503), httpx.Response(200, json={"success": True, "data": []})])
    c = TravelpayoutsClient("tok")
    assert await c.cheapest_for_month("BAK", "IST", date(2026, 12, 1)) == []
    assert route.call_count == 2
    respx.get(API).mock(return_value=httpx.Response(401))
    with pytest.raises(ApiError):
        await c.cheapest_for_month("BAK", "IST", date(2026, 12, 1))
    await c.aclose()


async def _nosleep(*a, **k):
    return None


# ---------- end-to-end scan ----------
@pytest.mark.asyncio
@respx.mock
async def test_scanner_end_to_end(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _nosleep)

    def responder(request):
        p = request.url.params
        dest = p.get("destination")
        if dest == "IST":
            data = [row("IST", 120, "2026-12-03T08:00:00+04:00"),
                    row("IST", 99, "2026-12-10T08:00:00+04:00"),
                    row("IST", 50, "2026-10-01T08:00:00+04:00")]  # past date, must be dropped
        elif dest == "DXB":
            data = [row("DXB", 200, "2026-12-01T08:00:00+04:00", "2026-12-03T08:00:00+04:00"),  # 2-day stay, must be dropped
                    row("DXB", 320, "2026-12-01T08:00:00+04:00", "2026-12-08T08:00:00+04:00", "FZ")]
        else:  # anywhere
            data = [row("IST", 10, "2026-12-01T08:00:00+04:00"),   # explicit route, must be skipped
                    row("TBS", 60, "2026-12-02T08:00:00+04:00", airline="A9"),
                    row("TBS", 55, "2026-12-20T08:00:00+04:00", airline="A9"),
                    row("EVN", 300, "2026-12-02T08:00:00+04:00")]
        return httpx.Response(200, json={"success": True, "data": data})

    respx.get(API).mock(side_effect=responder)
    cfg = Config(routes=[Route("IST", max_price=100), Route("DXB", round_trip=True, min_days=4, max_days=10, max_price=350)],
                 anywhere=Anywhere(enabled=True, max_price=100), alerts=Alerts(), months_ahead=1)
    sent = []

    async def notify(t, buttons=None):
        sent.append(t)

    st = Storage(":memory:")
    api = TravelpayoutsClient("tok")
    sc = Scanner(cfg, api, st, notify)
    res = await sc.run_once(today=date(2026, 11, 15))
    assert res.best["BAK-IST-ow"].price == 99
    assert res.best["BAK-DXB-rt"].price == 320
    assert res.best["BAK-TBS-any"].price == 55
    assert "BAK-IST-any" not in res.best
    assert res.alerts == 3 and len(sent) == 3          # IST, DXB, TBS (EVN > 100)

    # Second scan with identical prices: no duplicate alerts
    res2 = await sc.run_once(today=date(2026, 11, 15))
    assert res2.alerts == 0 and len(sent) == 3
    await api.aclose()


# ---------- location names (city / country / airport) ----------
from flight_tracker.locations import Locations, flag  # noqa: E402

CITIES = [{"code": "BAK", "name": "Baku", "name_translations": {"en": "Baku"}, "country_code": "AZ"},
          {"code": "IST", "name": "Istanbul", "name_translations": {"en": "Istanbul"}, "country_code": "TR"}]
AIRPORTS = [{"code": "GYD", "name": "Heydar Aliyev International Airport", "city_code": "BAK", "country_code": "AZ"},
            {"code": "SAW", "name": "Sabiha Gokcen International Airport", "city_code": "IST", "country_code": "TR"}]
COUNTRIES = [{"code": "AZ", "name": "Azerbaijan", "name_translations": {"en": "Azerbaijan"}},
             {"code": "TR", "name": "Turkiye", "name_translations": {"en": "Turkiye"}}]
DATA = "https://api.travelpayouts.com/data/en/"


def mock_location_files():
    respx.get(DATA + "cities.json").mock(return_value=httpx.Response(200, json=CITIES))
    respx.get(DATA + "airports.json").mock(return_value=httpx.Response(200, json=AIRPORTS))
    return respx.get(DATA + "countries.json").mock(return_value=httpx.Response(200, json=COUNTRIES))


def test_flag():
    assert flag("AZ") == "🇦🇿" and flag("tr") == "🇹🇷" and flag(None) == "" and flag("X1") == ""


@pytest.mark.asyncio
@respx.mock
async def test_locations_download_cache_and_lookup(tmp_path):
    route = mock_location_files()
    loc = Locations(tmp_path / "locations.json")
    await loc.ensure_fresh()
    assert loc.place("BAK") == "🇦🇿 Baku, Azerbaijan"
    assert loc.place("SAW") == "🇹🇷 Istanbul, Turkiye"          # airport code -> its city
    assert loc.airport("GYD") == "Heydar Aliyev International Airport (GYD)"
    assert loc.short("IST") == "Istanbul 🇹🇷"
    assert loc.place("XXX") == "XXX" and loc.airport("XXX") == "XXX"   # unknown -> code
    assert route.call_count == 1

    # Second instance reads the cache, no new download
    loc2 = Locations(tmp_path / "locations.json")
    await loc2.ensure_fresh()
    assert route.call_count == 1 and loc2.place("IST") == "🇹🇷 Istanbul, Turkiye"


@pytest.mark.asyncio
@respx.mock
async def test_locations_download_failure_never_raises(tmp_path):
    respx.get(url__startswith=DATA).mock(return_value=httpx.Response(500))
    loc = Locations(tmp_path / "locations.json")
    await loc.ensure_fresh()                       # must not raise
    assert loc.place("IST") == "IST" and loc.airport("SAW") == "SAW"


@pytest.mark.asyncio
@respx.mock
async def test_alert_shows_city_country_and_airport(tmp_path):
    mock_location_files()
    loc = Locations(tmp_path / "locations.json")
    await loc.ensure_fresh()
    o = Offer("BAK", "IST", "2026-12-05T08:00:00+04:00", None, 85, "USD", "PC", "411", 0, None, 180,
              "https://x", origin_airport="GYD", destination_airport="SAW")
    txt = format_offer(o, ["at or below target"], loc)
    assert "<b>Baku → Istanbul</b>" in txt
    assert "📍 From: 🇦🇿 Baku, Azerbaijan · Heydar Aliyev International Airport (GYD)" in txt
    assert "📍 To: 🇹🇷 Istanbul, Turkiye · Sabiha Gokcen International Airport (SAW)" in txt
    # Without location data the old format is unchanged
    assert "<b>BAK → IST</b>" in format_offer(o) and "📍" not in format_offer(o)


@pytest.mark.asyncio
@respx.mock
async def test_client_reads_airport_codes():
    respx.get(API).mock(return_value=httpx.Response(200, json={"success": True, "data": [
        {**row("IST", 95, "2026-12-05T08:00:00+04:00"), "origin_airport": "GYD", "destination_airport": "SAW"}]}))
    c = TravelpayoutsClient("tok")
    o = (await c.cheapest_for_month("BAK", "IST", date(2026, 12, 1)))[0]
    await c.aclose()
    assert (o.origin_airport, o.destination_airport) == ("GYD", "SAW")


# ---------- Telegram long messages (bug: "can't parse entities: Unclosed start tag") ----------
from flight_tracker.telegram import Telegram, html_to_text, split_message  # noqa: E402

TG = "https://api.telegram.org/botTOKEN/sendMessage"


def long_top_message(lines=15):
    link = "https://www.aviasales.com/search/BAK1012IST1?t=" + "x" * 400 + "&amp;marker=1"
    rows = [f'• Baku 🇦🇿 → City{i}: <b>{100 + i} USD</b> · 2026-12-10 · Pegasus (PC) · direct '
            f'<a href="{link}">↗</a>' for i in range(lines)]
    return "<b>🏆 Cheapest (last 48 hours)</b>\n" + "\n".join(rows)


def test_split_message_never_cuts_tags():
    text = long_top_message()
    assert len(text) > 4096                     # the real-world case that failed
    chunks = split_message(text)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 4000
        assert c.count("<a ") == c.count("</a>") and c.count("<b>") == c.count("</b>")
    assert "\n".join(chunks) == text            # nothing lost


def test_split_message_short_and_huge_line():
    assert split_message("hi") == ["hi"]
    huge = "<b>" + "y" * 9000 + "</b>"
    (chunk,) = split_message(huge)
    assert len(chunk) <= 4000 and "<b>" not in chunk


@pytest.mark.asyncio
@respx.mock
async def test_telegram_sends_long_message_in_parts():
    route = respx.post(TG).mock(return_value=httpx.Response(200, json={"ok": True}))
    tg = Telegram("TOKEN", [42])
    await tg.send(long_top_message())
    await tg.aclose()
    assert route.call_count >= 2
    import json as _json
    for call in route.calls:
        body = _json.loads(call.request.content)
        assert body["parse_mode"] == "HTML" and len(body["text"]) <= 4000


@pytest.mark.asyncio
@respx.mock
async def test_telegram_falls_back_to_plain_text(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _nosleep)
    route = respx.post(TG).mock(side_effect=[
        httpx.Response(400, json={"ok": False, "description": "Bad Request: can't parse entities: x"}),
        httpx.Response(200, json={"ok": True})])
    tg = Telegram("TOKEN", [42])
    await tg.send("<b>broken <i>markup</b> &amp; text")
    await tg.aclose()
    import json as _json
    second = _json.loads(route.calls[1].request.content)
    assert "parse_mode" not in second and second["text"] == "broken markup & text"
    assert html_to_text("<a href='x'>↗</a>") == "↗"


# =====================================================================
# v1.1 features: inline buttons, /weekend, live config, /family, baggage hint
# =====================================================================
import json as _json  # noqa: E402

from flight_tracker.alerts import (alert_buttons, baggage_hint, format_chart,  # noqa: E402
                                   format_weekend, is_weekend_trip, offer_buttons)
from flight_tracker.main import App, format_top  # noqa: E402
from flight_tracker.settings import Settings, parse_addroute  # noqa: E402


def rt_offer(dest, price, dep, ret, airline="J2"):
    return Offer("BAK", dest, dep, ret, price, "USD", airline, "1", 0, 0, 100, f"https://t/{dest}")


# ---------- settings / live config ----------
def test_parse_addroute():
    r = parse_addroute(["par"])
    assert (r.destination, r.max_price, r.round_trip, r.direct_only) == ("PAR", None, False, False)
    r = parse_addroute(["DXB", "400", "rt", "4-10", "direct"])
    assert (r.max_price, r.round_trip, r.min_days, r.max_days, r.direct_only) == (400, True, 4, 10, True)
    for bad in ([], ["PARIS"], ["PAR", "abc"], ["PAR", "-5"], ["PAR", "rt", "10-4"]):
        with pytest.raises(ValueError):
            parse_addroute(bad)


def test_settings_override_config_routes():
    cfg = Config(routes=[Route("IST", max_price=200), Route("TBS", max_price=120)])
    s = Settings(cfg, Storage(":memory:"))
    assert {r.destination for r in s.routes()} == {"IST", "TBS"}
    s.add_route(Route("PAR", max_price=150))
    assert s.set_route_limit("IST", 99) and s.get_route("IST").max_price == 99
    assert not s.set_route_limit("XXX", 99)
    assert s.remove_route("TBS") and not s.remove_route("TBS")
    assert {r.destination for r in s.routes()} == {"IST", "PAR"}
    s.add_route(Route("TBS"))                       # re-adding a removed config route works
    assert "TBS" in {r.destination for r in s.routes()}


def test_settings_anywhere_passengers_mute():
    cfg = Config(anywhere=Anywhere(enabled=True, max_price=100))
    s = Settings(cfg, Storage(":memory:"))
    assert s.anywhere_max_price == 100 and s.passengers == 1
    s.anywhere_max_price = 80
    s.passengers = 3
    assert s.anywhere_max_price == 80 and s.passengers == 3
    s.mute("IST")
    assert s.muted() == {"IST"} and s.unmute("IST") and not s.unmute("IST")


# ---------- alert content ----------
def test_baggage_hint_and_family_total():
    assert "hand luggage" in baggage_hint("W6") and "Full-service" in baggage_hint("J2")
    o = offer(85)
    txt = format_offer(o, passengers=3, with_link=False)
    assert "3 passengers: <b>255 USD</b> total" in txt
    assert "Open ticket" not in txt and "🧳" in txt
    assert "passengers" not in format_offer(o)       # single passenger: no total line


def test_offer_buttons_shape_and_callback_size():
    o = offer(85)
    rows = offer_buttons(o, "BAK-IST-ow")
    assert rows[0][0] == {"text": "✈️ Open ticket",
                          "url": "https://www.aviasales.com/search/BAK0512IST1"}
    datas = [b["callback_data"] for b in rows[1]]
    assert datas == ["mute:IST", "chart:BAK-IST-ow"]
    assert all(len(d.encode()) <= 64 for d in datas)
    assert alert_buttons(None, "IST", None) == [[{"text": "🔕 Mute IST", "callback_data": "mute:IST"}]]


def test_weekend_filter():
    fri_eve = "2026-11-13T19:30:00+04:00"   # Friday
    assert is_weekend_trip(rt_offer("TBS", 90, fri_eve, "2026-11-15T21:00:00+04:00"))
    assert not is_weekend_trip(rt_offer("TBS", 90, "2026-11-13T08:00:00+04:00", "2026-11-15T21:00:00+04:00"))  # Fri morning
    assert not is_weekend_trip(rt_offer("TBS", 90, fri_eve, "2026-11-16T09:00:00+04:00"))  # Monday return
    assert not is_weekend_trip(rt_offer("TBS", 90, "2026-11-12T19:30:00+04:00", "2026-11-15T21:00:00+04:00"))  # Thursday
    assert not is_weekend_trip(offer(50))                       # one way


def test_format_weekend_and_chart():
    deals = [rt_offer("TBS", 90, "2026-11-13T19:30:00+04:00", "2026-11-15T21:00:00+04:00")]
    txt = format_weekend(deals, passengers=2)
    assert "Fri 13 Nov 19:30 → Sun 21:00" in txt and "(×2 = 180)" in txt
    assert "No Friday-to-Sunday" in format_weekend([])
    rows = [{"day": "2026-10-20", "price": 120.0, "currency": "USD"},
            {"day": "2026-10-21", "price": 99.0, "currency": "USD"}]
    chart = format_chart("BAK-IST-ow", rows)
    assert "<pre>" in chart and "◀ low" in chart and "Now: <b>99</b>" in chart
    assert "No price history" in format_chart("BAK-IST-ow", [])


# ---------- scanner with live settings ----------
@pytest.mark.asyncio
@respx.mock
async def test_scanner_respects_mute_and_dynamic_routes(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _nosleep)

    def responder(request):
        dest = request.url.params.get("destination")
        if dest:
            return httpx.Response(200, json={"success": True, "data": [row(dest, 50, "2026-12-03T08:00:00+04:00")]})
        return httpx.Response(200, json={"success": True, "data": [
            row("TBS", 40, "2026-12-02T08:00:00+04:00"), row("EVN", 45, "2026-12-02T08:00:00+04:00")]})

    respx.get(API).mock(side_effect=responder)
    cfg = Config(routes=[Route("IST", max_price=100)], anywhere=Anywhere(enabled=True, max_price=100),
                 months_ahead=1)
    st = Storage(":memory:")
    s = Settings(cfg, st)
    s.add_route(Route("PAR", max_price=100))
    s.mute("IST")
    s.mute("EVN")
    sent = []

    async def notify(t, buttons=None):
        sent.append((t, buttons))

    api = TravelpayoutsClient("tok")
    res = await Scanner(cfg, api, st, notify, settings=s).run_once(today=date(2026, 11, 15))
    await api.aclose()
    assert set(res.best) == {"BAK-PAR-ow", "BAK-TBS-any"}       # IST and EVN muted
    assert all(b and b[0][0]["url"] for _, b in sent)           # every alert has buttons


@pytest.mark.asyncio
@respx.mock
async def test_weekend_deals(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _nosleep)
    data = [
        {**row("TBS", 120, "2026-11-13T19:30:00+04:00", "2026-11-15T20:00:00+04:00")},
        {**row("TBS", 100, "2026-11-20T18:00:00+04:00", "2026-11-22T20:00:00+04:00")},
        {**row("IST", 80, "2026-11-13T07:00:00+04:00", "2026-11-15T20:00:00+04:00")},   # Fri morning
        {**row("DXB", 300, "2026-11-13T21:00:00+04:00", "2026-11-15T23:00:00+04:00")},
    ]
    route = respx.get(API).mock(return_value=httpx.Response(200, json={"success": True, "data": data}))
    cfg = Config(months_ahead=1)
    api = TravelpayoutsClient("tok")
    sc = Scanner(cfg, api, Storage(":memory:"), None)
    deals = await sc.weekend_deals(today=date(2026, 11, 1))
    assert [(d.destination, d.price) for d in deals] == [("TBS", 100), ("DXB", 300)]
    assert route.calls.last.request.url.params["one_way"] == "false"
    assert [d.destination for d in await sc.weekend_deals(today=date(2026, 11, 1), max_price=200)] == ["TBS"]
    await api.aclose()


# ---------- Telegram commands and buttons (through App) ----------
@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAVELPAYOUTS_TOKEN", "t")
    (tmp_path / "c.yaml").write_text(
        f"db_path: {tmp_path}/p.db\nroutes:\n  - destination: IST\n    max_price: 200\n"
        "anywhere:\n  enabled: true\n  max_price: 100\n")
    a = App(load_config(tmp_path / "c.yaml", tmp_path / "none.env"), dry_run=True)
    yield a
    a.storage.close()


@pytest.mark.asyncio
async def test_app_live_config_commands(app):
    h = app.handle_command
    assert "Watching PAR" in await h(1, "/addroute PAR 150 rt 3-7")
    assert "PAR: round trip 3–7 days, target ≤ 150" in await h(1, "/routes")
    assert "not a 3-letter" in await h(1, "/addroute PARIS")
    assert "set to 99" in await h(1, "/setlimit IST 99")
    assert "Add it first" in await h(1, "/setlimit XXX 99")
    assert "'Anywhere' target price set to 80" in await h(1, "/setlimit 80")
    assert "not a number" in await h(1, "/setlimit abc")
    assert "Removed IST" in await h(1, "/removeroute IST")
    assert "3 passengers" in await h(1, "/family 3")
    assert "1 to 9" in await h(1, "/family 20")
    assert "IST muted" in await h(1, "/mute ist")
    assert "Muted: IST" in await h(1, "/muted")
    assert "unmuted" in await h(1, "/unmute IST")
    assert "No price history" in await h(1, "/chart IST")
    routes = await h(1, "/routes")
    assert "Passengers: 3" in routes and "Anywhere: one way, target ≤ 80" in routes and "IST" not in routes


@pytest.mark.asyncio
async def test_app_buttons(app):
    msg = {"reply_markup": {"inline_keyboard": offer_buttons(offer(85), "BAK-IST-ow")}}
    toast, reply, kb = await app.handle_callback(1, "mute:IST", msg)
    assert toast == "🔕 IST muted" and reply is None and app.settings.muted() == {"IST"}
    assert kb[0][0]["url"].startswith("https://www.aviasales.com/search/BAK0512IST1")
    assert kb[1][0]["callback_data"] == "unmute:IST"
    assert kb[1][1]["callback_data"] == "chart:BAK-IST-ow"
    toast, _, kb = await app.handle_callback(1, "unmute:IST", {"reply_markup": {"inline_keyboard": kb}})
    assert app.settings.muted() == set() and kb[1][0]["callback_data"] == "mute:IST"
    app.storage.record("BAK-IST-ow", offer(99))
    _, reply, kb = await app.handle_callback(1, "chart:BAK-IST-ow", {})
    assert "📊" in reply and kb is None
    assert "(297)" in format_top(app.storage, passengers=3)


# ---------- Telegram transport for buttons ----------
@pytest.mark.asyncio
@respx.mock
async def test_telegram_buttons_and_callbacks():
    base = "https://api.telegram.org/botTOKEN/"
    send = respx.post(base + "sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    answer = respx.post(base + "answerCallbackQuery").mock(return_value=httpx.Response(200, json={"ok": True}))
    edit = respx.post(base + "editMessageReplyMarkup").mock(return_value=httpx.Response(200, json={"ok": True}))
    tg = Telegram("TOKEN", [42])
    kb = [[{"text": "x", "callback_data": "mute:IST"}]]

    await tg.send(long_top_message(), buttons=kb)       # long message: buttons only on the last part
    bodies = [_json.loads(c.request.content) for c in send.calls]
    assert "reply_markup" not in bodies[0] and bodies[-1]["reply_markup"] == {"inline_keyboard": kb}

    async def on_cb(chat_id, data, message):
        return "done", "reply text", [[{"text": "y", "callback_data": "unmute:IST"}]]

    await tg._handle_callback({"id": "1", "data": "mute:IST",
                               "message": {"message_id": 7, "chat": {"id": 42}}}, on_cb)
    assert _json.loads(answer.calls.last.request.content)["text"] == "done"
    assert _json.loads(edit.calls.last.request.content)["message_id"] == 7
    # unauthorized chat: only acknowledged, handler not called
    await tg._handle_callback({"id": "2", "data": "mute:IST",
                               "message": {"message_id": 8, "chat": {"id": 999}}}, on_cb)
    assert edit.call_count == 1 and answer.call_count == 2
    await tg.aclose()



# =====================================================================
# v1.2: trip type (one way / round trip / both) and the /start guide
# =====================================================================
from flight_tracker.settings import parse_trip, route_rt_target  # noqa: E402


def test_parse_trip_and_addroute_both():
    assert parse_trip(["ist", "both", "3-7", "300"]) == ("IST", "both", (3, 7), 300.0)
    assert parse_trip(["anywhere", "round", "4-10"]) == ("ANYWHERE", "rt", (4, 10), None)
    assert parse_trip(["world", "oneway"]) == ("ANYWHERE", "ow", None, None)
    for bad in ([], ["IST"], ["IST", "sometimes"], ["PARIS", "both"], ["IST", "both", "7-3"], ["IST", "rt", "x"]):
        with pytest.raises(ValueError):
            parse_trip(bad)
    r = parse_addroute(["IST", "150", "both", "3-7"])
    assert (r.both, r.round_trip, r.min_days, r.max_days, r.trip) == (True, False, 3, 7, "both")
    assert route_rt_target(r) == 300                         # default: 2x the one-way target


def test_settings_route_and_anywhere_trip():
    cfg = Config(routes=[Route("IST", max_price=150)], anywhere=Anywhere(enabled=True, max_price=100))
    s = Settings(cfg, Storage(":memory:"))
    assert s.anywhere_trip == "ow" and s.anywhere_rt_max_price == 200     # 2x default
    s.set_anywhere_trip("both", (3, 7), 250)
    assert (s.anywhere_trip, s.anywhere_stay, s.anywhere_rt_max_price) == ("both", (3, 7), 250)
    assert s.set_route_trip("IST", "both", (2, 5))
    r = s.get_route("IST")
    assert (r.trip, r.min_days, r.max_days, r.max_price) == ("both", 2, 5, 150)  # price kept
    assert s.set_route_trip("IST", "rt") and s.get_route("IST").trip == "rt"
    assert not s.set_route_trip("XXX", "both")


@pytest.mark.asyncio
@respx.mock
async def test_scanner_both_for_routes_and_anywhere(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _nosleep)

    def responder(request):
        p = request.url.params
        dest, rt = p.get("destination"), p.get("one_way") == "false"
        dep, ret = "2026-12-03T08:00:00+04:00", "2026-12-08T08:00:00+04:00"   # 5-day stay
        if dest:
            return httpx.Response(200, json={"success": True, "data": [
                row(dest, 250 if rt else 120, dep, ret if rt else None)]})
        if rt:
            return httpx.Response(200, json={"success": True, "data": [
                row("TBS", 150, dep, ret), row("EVN", 90, dep, "2026-12-20T08:00:00+04:00")]})  # EVN stay too long
        return httpx.Response(200, json={"success": True, "data": [row("TBS", 60, dep)]})

    route = respx.get(API).mock(side_effect=responder)
    cfg = Config(routes=[Route("IST", max_price=150, both=True, min_days=3, max_days=7)],
                 anywhere=Anywhere(enabled=True, max_price=100, trip="both", min_days=3, max_days=7),
                 months_ahead=1)
    st = Storage(":memory:")
    sent = []

    async def notify(t, buttons=None):
        sent.append(t)

    api = TravelpayoutsClient("tok")
    res = await Scanner(cfg, api, st, notify, settings=Settings(cfg, st)).run_once(today=date(2026, 11, 15))
    await api.aclose()
    assert set(res.best) == {"BAK-IST-ow", "BAK-IST-rt", "BAK-TBS-any", "BAK-TBS-anyrt"}
    assert res.best["BAK-IST-rt"].price == 250 and res.best["BAK-TBS-anyrt"].price == 150
    # IST rt 250 <= 2x150 and TBS anyrt 150 <= 2x100 -> all four alert
    assert res.alerts == 4 and sum("round trip" in t for t in sent) == 2
    assert any(c.request.url.params.get("one_way") == "false" and not c.request.url.params.get("destination")
               for c in route.calls)


@pytest.mark.asyncio
async def test_app_trip_command_and_guide(app):
    h = app.handle_command
    guide = await h(1, "/start")
    assert "Welcome to Flight Tracker" in guide and "BAK" in guide and "/trip anywhere both 3-7" in guide
    assert len(guide) < 4000 and guide.count("<b>") == guide.count("</b>")
    assert "/start for the full guide" in await h(1, "/help")
    out = await h(1, "/trip IST both 3-7")
    assert "IST: one way + round trip 3–7 days" in out
    assert "target one way ≤ 200 / round trip ≤ 400" in out
    out = await h(1, "/trip anywhere both 4-10 250")
    assert "Anywhere: one way + round trip 4–10 days, target one way ≤ 100 / round trip ≤ 250" in out
    assert "Add it first" in await h(1, "/trip XXX both")
    assert "Unknown trip type" in await h(1, "/trip IST sometimes")
    assert "Watching DXB" in await h(1, "/addroute DXB 300 both 4-8")
    assert "DXB: one way + round trip 4–8 days" in await h(1, "/routes")


# =====================================================================
# v1.3: nearby airports, visa hints, holidays, weather
# =====================================================================
from pathlib import Path  # noqa: E402

from flight_tracker.extras import (AltOrigin, AltOrigins, Climate, Enricher,  # noqa: E402
                                   Holidays, Visa, temp_icon)

EXTRAS = Path(__file__).resolve().parent.parent / "extras"
METEO = "https://archive-api.open-meteo.com/v1/archive"


def test_shipped_data_files_load():
    v = Visa(EXTRAS / "visa_AZ.yaml", "AZ")
    assert v.line("GE").startswith("🟢") and v.line("AE").startswith("🟡") and v.line("IT").startswith("🔴")
    assert v.line("ZZ").startswith("❔") and v.line("AZ") is None       # own country: no line
    h = Holidays(EXTRAS / "holidays_AZ.yaml")
    assert h.days[date(2026, 3, 22)] == "Novruz"
    assert Visa(None, "XX").line("GE") is None                          # no file: feature off


def test_holidays_carry_over_and_leave_days(tmp_path):
    f = tmp_path / "h.yaml"
    f.write_text("weekend_carry_over: true\nholidays:\n"
                 "  2026-11-08: Victory Day\n  2026-11-09: State Flag Day\n"
                 "  2026-03-20: Novruz\n  2026-03-21: Novruz\n  2026-03-22: Novruz\n"
                 "  2026-03-23: Novruz\n  2026-03-24: Novruz\n")
    h = Holidays(f)
    # Nov 8 2026 is a Sunday -> Mon 9 is already a holiday -> Tue 10 is the moved day off
    assert h.days[date(2026, 11, 10)] == "Victory Day (moved)"
    # Mar 21-22 2026 are Sat/Sun -> moved to Wed 25 and Thu 26
    assert date(2026, 3, 25) in h.days and date(2026, 3, 26) in h.days
    # Fri 20 Mar 08:00 -> Sun 29 Mar: every weekday is a holiday or moved day, only Fri 27 is a working day
    assert h.leave_needed("2026-03-20T08:00:00+04:00", "2026-03-29T20:00:00+04:00") == (1, ["Novruz"])
    off = rt_offer("IST", 100, "2026-03-20T08:00:00+04:00", "2026-03-26T20:00:00+04:00")
    assert h.line(off) == "🌟 No vacation days needed: covers Novruz"
    two = rt_offer("IST", 100, "2026-03-20T08:00:00+04:00", "2026-03-29T20:00:00+04:00")
    assert h.line(two) == "🌟 Only 1 vacation day needed thanks to Novruz"
    wk = rt_offer("IST", 100, "2026-11-13T19:00:00+04:00", "2026-11-15T20:00:00+04:00")
    assert h.line(wk) == "🌟 No vacation days needed (weekend trip)"
    long = rt_offer("IST", 100, "2026-04-06T08:00:00+04:00", "2026-04-17T20:00:00+04:00")
    assert h.line(long) is None and h.line(offer(50)) is None          # long trip / one way


def test_climate_reference_year_and_icons():
    assert Climate.reference_year(1, date(2026, 10, 25)) == 2026
    assert Climate.reference_year(12, date(2026, 10, 25)) == 2025
    assert Climate.reference_year(10, date(2026, 10, 25)) == 2025       # current month not complete
    assert [temp_icon(t) for t in (-5, 8, 20, 30, 38)] == ["🥶", "🧥", "🌤", "☀️", "🔥"]


@pytest.mark.asyncio
@respx.mock
async def test_climate_fetch_cache_and_failure(tmp_path):
    route = respx.get(METEO).mock(return_value=httpx.Response(200, json={"daily": {
        "time": ["2026-01-01", "2026-01-02"],
        "temperature_2m_max": [-1.0, -3.0], "temperature_2m_min": [-9.0, -11.0]}}))
    c = Climate(tmp_path / "climate.json")
    assert await c.month_avg(39.9, 116.4, 1, today=date(2026, 10, 25)) == (-2, -10)
    p = route.calls.last.request.url.params
    assert (p["start_date"], p["end_date"]) == ("2026-01-01", "2026-01-31")
    c2 = Climate(tmp_path / "climate.json")                              # cache on disk
    assert await c2.month_avg(39.9, 116.4, 1) == (-2, -10) and route.call_count == 1
    respx.get(METEO).mock(return_value=httpx.Response(500))
    assert await Climate(tmp_path / "x.json").month_avg(1, 1, 5) is None   # failure -> no line, no crash


@pytest.mark.asyncio
@respx.mock
async def test_alt_origins_compare(monkeypatch):
    def responder(request):
        p = request.url.params
        if p["origin"] == "KUT":
            return httpx.Response(200, json={"success": True, "data": [
                row("MIL", 45, "2026-12-11T09:00:00+04:00"),          # 1 day off: ok
                row("MIL", 20, "2026-12-25T09:00:00+04:00")]})         # too far from the date
        if p["origin"] == "TBS":
            return httpx.Response(200, json={"success": True, "data": [row("MIL", 140, "2026-12-10T09:00:00+04:00")]})
        return httpx.Response(200, json={"success": True, "data": []})

    respx.get(API).mock(side_effect=responder)
    api = TravelpayoutsClient("tok")
    alt = AltOrigins(api, flex_days=2, min_saving_percent=25)
    base = Offer("BAK", "MIL", "2026-12-10T08:00:00+04:00", None, 150, "USD", "J2", "1", 0, None, 1, "https://x")
    lines = await alt.lines(base, [AltOrigin("KUT", "~9 h by car"), AltOrigin("TBS"), AltOrigin("MIL")], None)
    await api.aclose()
    assert len(lines) == 1                                             # TBS only 7% cheaper, MIL = destination
    assert "From KUT: <b>45 USD</b> on 11 Dec (save 105) (~9 h by car). Worth the trip?" in lines[0]


@pytest.mark.asyncio
@respx.mock
async def test_full_alert_with_all_extras(tmp_path, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _nosleep)
    mock_location_files()
    respx.get(DATA + "cities.json").mock(return_value=httpx.Response(200, json=[
        {"code": "BAK", "name": "Baku", "country_code": "AZ", "coordinates": {"lat": 40.4, "lon": 49.9}},
        {"code": "IST", "name": "Istanbul", "country_code": "TR", "coordinates": {"lat": 41.0, "lon": 28.9}},
        {"code": "TBS", "name": "Tbilisi", "country_code": "GE", "coordinates": {"lat": 41.7, "lon": 44.8}}]))
    respx.get(DATA + "countries.json").mock(return_value=httpx.Response(200, json=[
        {"code": "AZ", "name": "Azerbaijan"}, {"code": "TR", "name": "Turkiye"}, {"code": "GE", "name": "Georgia"}]))
    respx.get(METEO).mock(return_value=httpx.Response(200, json={"daily": {
        "temperature_2m_max": [16.0], "temperature_2m_min": [8.0]}}))

    def api(request):
        p = request.url.params
        price = 40 if p["origin"] == "TBS" else 120
        return httpx.Response(200, json={"success": True, "data": [
            row("IST", price, "2026-03-20T08:00:00+04:00", "2026-03-24T20:00:00+04:00")]})

    respx.get(API).mock(side_effect=api)
    loc = Locations(tmp_path / "loc.json")
    await loc.ensure_fresh()
    assert loc.coords("SAW") == (41.0, 28.9) and loc.country_code("BAK") == "AZ"
    client = TravelpayoutsClient("tok")
    enr = Enricher(locations=loc, origin="BAK", climate=Climate(tmp_path / "c.json"),
                   alt=AltOrigins(client), alt_origins=lambda: [AltOrigin("TBS")])
    o = (await client.cheapest_for_month("BAK", "IST", date(2026, 3, 1), round_trip=True))[0]
    lines = await enr.lines(o)
    await client.aclose()
    text = "\n".join(lines)
    assert "💡 From Tbilisi 🇬🇪: <b>40 USD</b>" in text
    assert "🟢 Visa-free for Azerbaijani passport" in text
    assert "🌟 No vacation days needed: covers Novruz" in text
    assert "🌤 March in Istanbul: about +16°C day / +8°C night" in text
    full = format_offer(o, ["test"], loc, extra=lines)
    assert full.index("💡") < full.index("📉")


@pytest.mark.asyncio
async def test_enricher_never_breaks_alerts():
    class Boom:
        enabled = True

        async def line(self, *a):
            raise RuntimeError("down")

    enr = Enricher(locations=None, climate=Boom(), passport="off", holidays_country="off")
    assert await enr.lines(offer(50)) == []


def test_location_cache_upgrade(tmp_path):
    p = tmp_path / "locations.json"
    p.write_text(_json.dumps({"saved_at": 9e12, "cities": {"BAK": ["Baku", "AZ"]}, "airports": {}, "countries": {}}))
    assert Locations(p)._read_cache() is False          # v1 cache without coordinates -> re-download


@pytest.mark.asyncio
async def test_app_alt_and_holidays_commands(app):
    h = app.handle_command
    assert "Nearby airports are off" in await h(1, "/alt")
    assert "check TBS, KUT" in await h(1, "/alt tbs kut")
    assert [a.code for a in app.settings.alt_origins()] == ["TBS", "KUT"]
    assert "Nearby airports: TBS, KUT" in await h(1, "/routes")
    assert "Not IATA codes: TBILISI" in await h(1, "/alt TBILISI")
    assert "turned off" in await h(1, "/alt off") and app.settings.alt_origins() == []
    app.enricher.passport_setting = app.enricher.holidays_setting = "AZ"   # no location data in tests
    out = await h(1, "/holidays")
    assert "Upcoming public holidays" in out
    guide = await h(1, "/start")
    assert len(guide) < 4000 and "/alt TBS KUT" in guide and "🟢 visa-free" in guide


@pytest.mark.asyncio
async def test_app_guide_button(app):
    toast, reply, kb = await app.handle_callback(1, "guide", {})
    assert toast == "📘" and "Welcome to Flight Tracker" in reply and kb is None



# ---------- affiliate ticket link ----------
from flight_tracker.alerts import affiliate_link  # noqa: E402


def test_affiliate_link():
    assert affiliate_link("BAK", "IST", "2026-12-10", marker="123456") == \
        "https://www.aviasales.com/search/BAK1012IST1?marker=123456"
    assert affiliate_link("bak", "dxb", "2026-12-10T05:30:00+04:00", "2027-01-07T09:00:00+04:00",
                          marker="777") == "https://www.aviasales.com/search/BAK1012DXB07011?marker=777"
    assert affiliate_link("BAK", "IST", "2026-12-10") == "https://www.aviasales.com/search/BAK1012IST1"
    assert affiliate_link("BAK", "IST", "10.12.2026", marker="1") is None       # bad date
    assert affiliate_link("BAK", "IST", "", marker="1") is None
    assert affiliate_link("BAK", "IST", "2026-12-10", "garbage") is None
    assert affiliate_link("BAK", "IST", "2026-12-10", marker="a b&c") == \
        "https://www.aviasales.com/search/BAK1012IST1?marker=a%20b%26c"


def test_ticket_button_uses_affiliate_link_with_fallback():
    o = offer(85)
    assert offer_buttons(o, "BAK-IST-ow", "123456")[0][0]["url"] == \
        "https://www.aviasales.com/search/BAK0512IST1?marker=123456"
    bad = Offer("BAK", "IST", "not-a-date", None, 85, "USD", "J2", "61", 0, None, 1, "https://api-link")
    assert offer_buttons(bad, "BAK-IST-ow", "123456")[0][0]["url"] == "https://api-link"


@pytest.mark.asyncio
@respx.mock
async def test_scanner_alert_button_carries_marker(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _nosleep)
    respx.get(API).mock(return_value=httpx.Response(200, json={"success": True, "data": [
        row("IST", 50, "2026-12-03T08:00:00+04:00")]}))
    cfg = Config(routes=[Route("IST", max_price=100)], months_ahead=1, travelpayouts_marker="999")
    sent = []

    async def notify(t, buttons=None):
        sent.append(buttons)

    api = TravelpayoutsClient("tok")
    await Scanner(cfg, api, Storage(":memory:"), notify).run_once(today=date(2026, 11, 15))
    await api.aclose()
    assert sent[0][0][0]["url"] == "https://www.aviasales.com/search/BAK0312IST1?marker=999"
