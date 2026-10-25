# ✈️ Flight Tracker Daemon

[![Tests](https://github.com/DualStackAdmin/flight-tracker-daemon/actions/workflows/tests.yml/badge.svg)](https://github.com/DualStackAdmin/flight-tracker-daemon/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-Ubuntu%20%7C%20systemd-orange)

**A self-hosted cheap flight tracker. It runs 24/7 on your Linux server and sends you a Telegram message when fares from your city drop.**

Works from **any city in the world**. Set your departure airport in one line of config. The defaults use Baku (BAK) as an example.

---

## Features

- 🔎 **Route watching**: one-way or round trip, stay length (e.g. 4–10 days), direct-only filter.
- 🌍 **"Anywhere" mode**: finds the cheapest destinations from your city that you didn't even think of.
- 📉 **Smart alerts**:
  - you get a message when a fare hits your target price, or drops X% below the route's 30-day low;
  - the same deal is never sent twice;
  - a per-route cooldown stops spam.
- ☀️ **Daily digest**: the cheapest fares, every morning at a time you choose.
- 🤖 **Telegram commands**: `/top`, `/scan`, `/status`, `/routes`, `/help`.
- 🗄 **Price history** stored in SQLite.
- 🛡 **Production-ready on Ubuntu**:
  - one-command installer and a hardened systemd service;
  - auto-restarts if the process crashes;
  - retries with backoff when the API fails.
- 🪶 **Tiny footprint**: only two dependencies (`httpx`, `PyYAML`), no heavy bot framework.

## Example alert

```
✈️ Baku → Istanbul — 85 USD (one way)
📍 From: 🇦🇿 Baku, Azerbaijan · Heydar Aliyev International Airport (GYD)
📍 To: 🇹🇷 Istanbul, Turkiye · Sabiha Gokcen International Airport (SAW)
🛫 10 Dec 2026, 05:30 · 1 stop
🏷 Pegasus (PC) 411
📉 at or below target (≤ 200)
🔗 Open ticket
Cached price: confirm on the site before buying.
```

## How it works

```
 systemd (24/7, auto-restart)
   └─ python -m flight_tracker
        ├─ Scan loop (every 60 min)
        │    Travelpayouts / Aviasales API ─► cheapest offers
        │    SQLite (data/prices.db)       ─► price history
        │    Alert rules                   ─► Telegram message
        ├─ Daily digest
        └─ Telegram bot commands (long polling)
```

### Why an API and not web scraping?

Airline and travel sites forbid scraping in their terms of service. They also fight it with CAPTCHAs, bot detection and IP bans, and they change their markup often, so scrapers break within weeks.

This project uses the official **[Travelpayouts (Aviasales) Data API](https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API)** instead. It is free, legal and stable, and it aggregates fares from hundreds of airlines and agencies worldwide.

> ⚠️ The API returns prices cached from real user searches made in the last 48 hours. They are not live quotes, so always confirm the price through the link before you buy.

---

## Quick start (Ubuntu 22.04 / 24.04)

### 1. Get the tokens (both free)

| What | Where |
|---|---|
| Travelpayouts API token | Sign up at [travelpayouts.com](https://www.travelpayouts.com) → **Profile → API token** |
| Telegram bot token | Message [@BotFather](https://t.me/BotFather) → `/newbot` |

### 2. Install

```bash
git clone https://github.com/DualStackAdmin/flight-tracker-daemon.git
cd flight-tracker-daemon
sudo bash deploy/install.sh
```

The installer does four things in place:

1. creates a Python virtualenv;
2. copies `config.yaml` and `.env` from the examples;
3. sets `.env` permissions to `600`;
4. installs and enables the `flight-tracker` systemd service, which runs as the owner of the directory.

### 3. Configure

```bash
nano .env          # TRAVELPAYOUTS_TOKEN and TELEGRAM_BOT_TOKEN
nano config.yaml   # origin city, routes, target prices, timezone
```

Test your token without Telegram. This prints results to the terminal:

```bash
venv/bin/python -m flight_tracker --once --dry-run
```

### 4. Start and link Telegram

```bash
sudo systemctl start flight-tracker
```

1. Send `/start` to your bot. It replies with your `chat_id`.
2. Put the `chat_id` in `.env` as `TELEGRAM_CHAT_ID=...`.
3. Restart the service: `sudo systemctl restart flight-tracker`.

The bot says **🚀 Flight Tracker started**. Send `/scan` to run a scan right away.

---

## Configuration

| Key | Meaning |
|---|---|
| `origin` | Your departure city IATA code (`BAK`, `IST`, `LON`, `NYC`, `BER`, ...) |
| `currency` | `usd`, `eur`, `try`, ... |
| `timezone` | Your timezone for the daily digest (e.g. `Europe/Berlin`) |
| `months_ahead` | How many months ahead to search |
| `scan_interval_minutes` | Scan frequency (min 10) |
| `digest_hour` | Daily digest hour; set to `null` to disable |
| `routes[].destination` | Destination IATA code |
| `routes[].max_price` | Alert when the fare is at or below this |
| `routes[].round_trip`, `min_days`, `max_days` | Round trip and stay length |
| `routes[].direct_only` | Only non-stop flights |
| `anywhere.enabled`, `anywhere.max_price` | Cheapest fares to any destination |
| `alerts.drop_percent` | Alert when the fare is this % below the 30-day low |
| `alerts.cooldown_hours` | Minimum time between alerts for one route |

See [`config.example.yaml`](config.example.yaml) for a full example.

## Operations

| Task | Command |
|---|---|
| Live logs | `journalctl -u flight-tracker -f` |
| Status | `systemctl status flight-tracker` |
| Restart after config change | `sudo systemctl restart flight-tracker` |
| Stop | `sudo systemctl stop flight-tracker` |
| One-off scan, no Telegram | `venv/bin/python -m flight_tracker --once --dry-run` |
| Update | `git pull && sudo bash deploy/install.sh` (keeps `config.yaml`, `.env` and `data/`) |

## Development

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

The tests mock the API, so they need no tokens or network.

```
flight_tracker/
  config.py        # config.yaml + .env loading
  travelpayouts.py # API client (retries, 429/5xx backoff)
  storage.py       # SQLite price history
  alerts.py        # alert rules + message formatting
  scanner.py       # scan cycle
  telegram.py      # send messages + commands (long polling)
  main.py          # service, daily digest, CLI
deploy/            # installer + systemd unit
tests/             # tests with a mocked API
```

## Roadmap

- [ ] Manage routes from Telegram (`/add IST 150`, `/remove IST`)
- [ ] Price history charts sent as images
- [ ] Weekend-only and date-range filters
- [ ] Docker image
- [ ] Extra data sources

Contributions are welcome. Open an [issue](https://github.com/DualStackAdmin/flight-tracker-daemon/issues) or a pull request.

## Author

**Ulvi Guluzada**

- GitHub: [@DualStackAdmin](https://github.com/DualStackAdmin)
- LinkedIn: [uguluzada](https://www.linkedin.com/in/uguluzada/)

If this project saved you money on a ticket, give it a ⭐

## Disclaimer

This project is not affiliated with Aviasales, Travelpayouts, Telegram or any airline. Prices are informational and may differ from the final booking price.

## License

[MIT](LICENSE) © 2026 Ulvi Guluzada. Free to use, modify and share, including commercially.
