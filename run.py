import asyncio
import hashlib
import os

from aiogram import Dispatcher
from fastapi import Request
from fastapi.responses import JSONResponse


WEBHOOK_PATH = "/telegram/webhook"
PUBLIC_URL = (
    os.getenv("PUBLIC_BASE_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or "https://hot-1-ih2f.onrender.com"
).strip().rstrip("/")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest() if BOT_TOKEN else ""


async def start_webhook(self, *bots, **kwargs):
    bot = bots[0] if bots else kwargs.get("bot")
    if bot is None:
        raise RuntimeError("Telegram bot is missing")
    await bot.set_webhook(
        f"{PUBLIC_URL}{WEBHOOK_PATH}",
        allowed_updates=self.resolve_used_update_types(),
        drop_pending_updates=False,
        secret_token=WEBHOOK_SECRET or None,
    )
    print(f"Telegram webhook active: {PUBLIC_URL}{WEBHOOK_PATH}")


# Patch aiogram before app.main is imported. This is intentionally done in the
# process entrypoint, where it is guaranteed to run before bot_runner().
Dispatcher.start_polling = start_webhook

import app.main as main  # noqa: E402


@main.app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return JSONResponse({"ok": False}, status_code=401)
    payload = await request.json()
    await main.dp.feed_raw_update(main.bot, payload)
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(main.app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
