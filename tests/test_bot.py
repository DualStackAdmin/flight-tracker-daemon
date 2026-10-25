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

    async def notify(t):
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
