"""Telegram Bot API: send messages and receive commands (long polling)."""
from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

Handler = Callable[[int, str], Awaitable[str | None]]
# callback handler: (chat_id, data, message) -> (toast_text, reply_text | None, new_buttons | None)
CallbackHandler = Callable[[int, str, dict], Awaitable[tuple[str, str | None, list | None]]]

MAX_LEN = 4000  # Telegram limit is 4096; keep a margin


def html_to_text(text: str) -> str:
    """Strip HTML tags and unescape entities (plain-text fallback)."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    """Split a long message into chunks at line breaks, so an HTML tag is never cut in half.
    A single line longer than the limit is sent as plain text, cut safely."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > limit:
            line = html.escape(html_to_text(line))[:limit]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


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

    async def send(self, text: str, chat_id: int | None = None,
                   buttons: list[list[dict]] | None = None) -> None:
        """Send text (split if long). Inline buttons are attached to the last part."""
        targets = [chat_id] if chat_id is not None else self.chat_ids
        for cid in targets:
            chunks = split_message(text)
            for i, chunk in enumerate(chunks):
                await self._send_one(cid, chunk, buttons=buttons if i == len(chunks) - 1 else None)

    async def _send_one(self, cid: int, text: str, html_mode: bool = True,
                        buttons: list[list[dict]] | None = None) -> None:
        payload = {"chat_id": cid, "text": text, "disable_web_page_preview": True}
        if html_mode:
            payload["parse_mode"] = "HTML"
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        for attempt in range(3):
            try:
                r = await self._client.post("/sendMessage", json=payload)
            except httpx.HTTPError as e:
                log.warning("Telegram network error: %s", e)
                await asyncio.sleep(2 ** attempt)
                continue
            if r.status_code == 429:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                await asyncio.sleep(wait)
                continue
            if r.status_code == 400 and html_mode and "parse entities" in r.text:
                # Safety net: never lose a message because of markup, send it as plain text
                log.warning("Telegram rejected HTML, resending as plain text: %s", r.text[:200])
                await self._send_one(cid, html_to_text(text), html_mode=False, buttons=buttons)
                return
            if r.status_code != 200:
                log.error("Telegram error %s: %s", r.status_code, r.text[:300])
            return

    async def _call(self, method: str, payload: dict) -> None:
        try:
            r = await self._client.post(f"/{method}", json=payload)
            if r.status_code != 200 and "message is not modified" not in r.text:
                log.warning("Telegram %s %s: %s", method, r.status_code, r.text[:200])
        except httpx.HTTPError as e:
            log.warning("Telegram %s network error: %s", method, e)

    async def set_commands(self, commands: list[tuple[str, str]]) -> None:
        """Register the command menu shown by the '/' button in Telegram."""
        await self._call("setMyCommands", {"commands": [
            {"command": c, "description": d} for c, d in commands]})

    async def _handle_callback(self, cq: dict, on_callback: CallbackHandler | None) -> None:
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if chat_id not in self.chat_ids or on_callback is None:
            await self._call("answerCallbackQuery", {"callback_query_id": cq["id"]})
            return
        try:
            toast, reply, new_buttons = await on_callback(chat_id, cq.get("data") or "", msg)
        except Exception as e:  # noqa: BLE001
            log.exception("Button failed")
            toast, reply, new_buttons = f"Error: {e}", None, None
        await self._call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": toast[:200]})
        if new_buttons is not None and msg.get("message_id"):
            await self._call("editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": msg["message_id"],
                "reply_markup": {"inline_keyboard": new_buttons}})
        if reply:
            await self.send(reply, chat_id)

    async def poll(self, handler: Handler, stop: asyncio.Event,
                   on_callback: CallbackHandler | None = None) -> None:
        """Listen for commands. Only chats listed in TELEGRAM_CHAT_ID get answers,
        except /start, which replies to anyone with their chat_id (for first-time setup)."""
        while not stop.is_set():
            try:
                r = await self._client.get("/getUpdates", params={
                    "offset": self._offset, "timeout": 30, "allowed_updates": '["message","callback_query"]'})
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
                if "callback_query" in u:
                    await self._handle_callback(u["callback_query"], on_callback)
                    continue
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
