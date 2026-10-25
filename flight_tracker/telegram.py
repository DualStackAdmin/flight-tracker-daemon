"""Telegram Bot API: send messages and receive commands (long polling)."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

Handler = Callable[[int, str], Awaitable[str | None]]


class Telegram:
    def __init__(self, token: str, chat_ids: list[int], client: httpx.AsyncClient | None = None):
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is empty, check your .env file")
        self.chat_ids = chat_ids
        self._client = client or httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}", timeout=httpx.Timeout(40.0))
        self._offset = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, text: str, chat_id: int | None = None) -> None:
        targets = [chat_id] if chat_id is not None else self.chat_ids
        for cid in targets:
            for attempt in range(3):
                try:
                    r = await self._client.post("/sendMessage", json={
                        "chat_id": cid, "text": text[:4096], "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    })
                    if r.status_code == 429:
                        wait = r.json().get("parameters", {}).get("retry_after", 5)
                        await asyncio.sleep(wait)
                        continue
                    if r.status_code != 200:
                        log.error("Telegram error %s: %s", r.status_code, r.text[:300])
                    break
                except httpx.HTTPError as e:
                    log.warning("Telegram network error: %s", e)
                    await asyncio.sleep(2 ** attempt)

    async def poll(self, handler: Handler, stop: asyncio.Event) -> None:
        """Listen for commands. Only chats listed in TELEGRAM_CHAT_ID get answers,
        except /start, which replies to anyone with their chat_id (for first-time setup)."""
        while not stop.is_set():
            try:
                r = await self._client.get("/getUpdates", params={
                    "offset": self._offset, "timeout": 30, "allowed_updates": '["message"]'})
                updates = r.json().get("result", []) if r.status_code == 200 else []
                if r.status_code != 200:
                    log.error("getUpdates %s: %s", r.status_code, r.text[:200])
                    await asyncio.sleep(10)
            except httpx.HTTPError as e:
                log.warning("getUpdates network error: %s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                self._offset = u["update_id"] + 1
                msg = u.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = (msg.get("chat") or {}).get("id")
                if not text.startswith("/") or chat_id is None:
                    continue
                cmd = text.split()[0].split("@")[0].lower()
                if cmd == "/start" and chat_id not in self.chat_ids:
                    await self.send(f"Hi! Your chat_id is <code>{chat_id}</code>\n"
                                    "Put it in TELEGRAM_CHAT_ID in the .env file on your server.", chat_id)
                    continue
                if chat_id not in self.chat_ids:
                    log.info("Unauthorized chat %s: %s", chat_id, text)
                    continue
                try:
                    reply = await handler(chat_id, text)
                except Exception as e:  # noqa: BLE001
                    log.exception("Command failed")
                    reply = f"Error: {e}"
                if reply:
                    await self.send(reply, chat_id)
