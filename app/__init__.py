"""Application package hooks for checkout and Telegram webhook mode."""

import hashlib
import os
from pathlib import Path

from aiogram import Dispatcher
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from app.checkout import router as _checkout_router


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PUBLIC_URL = (
    os.getenv("PUBLIC_BASE_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or "https://hot-1-ih2f.onrender.com"
).strip().rstrip("/")
WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest() if BOT_TOKEN else ""


async def _start_webhook_instead_of_polling(self, *bots, **kwargs):
    """Keep exactly one Telegram delivery mechanism: outgoing webhook."""
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


# app.main calls dp.start_polling(). Patch it before app.main is imported.
Dispatcher.start_polling = _start_webhook_instead_of_polling


_original_fastapi_init = FastAPI.__init__


def _fastapi_init_with_checkout(self, *args, **kwargs):
    _original_fastapi_init(self, *args, **kwargs)
    if getattr(self, "_hot_integrations_registered", False):
        return

    self.include_router(_checkout_router)

    @self.get("/checkout", response_class=HTMLResponse)
    async def checkout_page():
        return (Path(__file__).resolve().parent / "checkout.html").read_text(encoding="utf-8")

    @self.get("/obrigado", response_class=HTMLResponse)
    async def thank_you_page():
        return """<!doctype html><html lang='pt-BR'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Pagamento confirmado</title><style>body{font-family:Arial;background:#0b0b0f;color:#fff;text-align:center;padding:70px 20px}main{max-width:520px;margin:auto;background:#17171e;padding:35px;border-radius:22px}a{display:inline-block;margin-top:20px;padding:14px 20px;border-radius:10px;background:#fff;color:#111;text-decoration:none;font-weight:bold}</style><main><h1>Pagamento confirmado ✅</h1><p>Seu pagamento foi confirmado pela BravoPay.</p><p>Se o acesso for vinculado ao Telegram, aguarde a liberação automática.</p><a href='/checkout'>Voltar ao checkout</a></main></html>"""

    @self.post(WEBHOOK_PATH)
    async def telegram_webhook(request: Request):
        if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return JSONResponse({"ok": False}, status_code=401)
        payload = await request.json()
        # Import lazily so app.main has finished initializing its bot/dispatcher.
        import app.main as main
        await main.dp.feed_raw_update(main.bot, payload)
        return JSONResponse({"ok": True})

    self._hot_integrations_registered = True


FastAPI.__init__ = _fastapi_init_with_checkout
