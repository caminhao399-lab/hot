"""Runtime compatibility for BravoPay and Telegram webhook mode."""

import hashlib
import importlib
import os
import uuid

# Keep BravoPay configuration normalized without exposing secrets.
os.environ["BRAVOPAY_BASE_URL"] = os.getenv(
    "BRAVOPAY_BASE_URL", "https://bravopay.club/api/v1"
).strip().rstrip("/") or "https://bravopay.club/api/v1"

for _key in (
    "BRAVOPAY_PRODUCT_ID_ESSENTIAL",
    "BRAVOPAY_PRODUCT_ID_PREMIUM",
    "BRAVOPAY_PRODUCT_ID_ACERVO",
    "BRAVOPAY_PRODUCT_ID_FULL",
):
    _value = os.getenv(_key, "").strip()
    if _value and not _value.startswith("prd_"):
        os.environ[_key] = ""

# BravoPay requires an Idempotency-Key for transaction creation.
try:
    import aiohttp

    _original_request = aiohttp.ClientSession._request

    async def _bravopay_request(self, method, url, *args, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        if str(method).upper() == "POST" and str(url).rstrip("/").endswith("/transactions"):
            headers.setdefault("Idempotency-Key", "pix-" + uuid.uuid4().hex)
            headers.setdefault("Content-Type", "application/json")
        kwargs["headers"] = headers
        return await _original_request(self, method, url, *args, **kwargs)

    aiohttp.ClientSession._request = _bravopay_request
except Exception:
    pass

# Render can briefly have two instances during a rolling deploy. Long polling uses
# Telegram getUpdates and therefore two instances fight over the same bot. Use a
# Telegram outgoing webhook instead: Telegram delivers each update to the public URL,
# so multiple Render instances can coexist without getUpdates conflicts.
try:
    from aiogram import Bot, Dispatcher
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    _WEBHOOK_PATH = "/telegram/webhook"
    _BASE_URL = (
        os.getenv("PUBLIC_BASE_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
        or "https://hot-1-ih2f.onrender.com"
    ).strip().rstrip("/")
    _bot_token = os.getenv("BOT_TOKEN", "").strip()
    _webhook_secret = hashlib.sha256(_bot_token.encode("utf-8")).hexdigest() if _bot_token else ""

    if not getattr(Dispatcher.start_polling, "_hot_webhook_patch", False):
        async def _start_webhook_instead_of_polling(self, *bots, **kwargs):
            bot = bots[0] if bots else kwargs.get("bot")
            if bot is None:
                raise RuntimeError("Telegram webhook bot is missing")
            await bot.set_webhook(
                f"{_BASE_URL}{_WEBHOOK_PATH}",
                allowed_updates=self.resolve_used_update_types(),
                drop_pending_updates=False,
                secret_token=_webhook_secret or None,
            )
            print(f"Telegram webhook configured at {_BASE_URL}{_WEBHOOK_PATH}")
            return None

        _start_webhook_instead_of_polling._hot_webhook_patch = True
        Dispatcher.start_polling = _start_webhook_instead_of_polling

    if not getattr(Bot.delete_webhook, "_hot_webhook_patch", False):
        async def _do_not_delete_webhook_during_startup(self, *args, **kwargs):
            # app.main historically deleted the webhook before starting polling.
            # In webhook mode that would create a startup race, so leave it intact.
            return True

        _do_not_delete_webhook_during_startup._hot_webhook_patch = True
        Bot.delete_webhook = _do_not_delete_webhook_during_startup

    if not getattr(FastAPI.__init__, "_hot_telegram_webhook_patch", False):
        _original_fastapi_init = FastAPI.__init__

        def _fastapi_init_with_telegram_webhook(self, *args, **kwargs):
            _original_fastapi_init(self, *args, **kwargs)

            @self.post(_WEBHOOK_PATH)
            async def _telegram_webhook(request):
                if _webhook_secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != _webhook_secret:
                    return JSONResponse({"ok": False}, status_code=401)
                payload = await request.json()
                main = importlib.import_module("app.main")
                result = await main.dp.feed_raw_update(main.bot, payload)
                # Telegram only needs a fast 2xx response. Handler replies are sent
                # through the Bot API by aiogram, so no API method is serialized here.
                return JSONResponse({"ok": True})

            @self.on_event("startup")
            async def _prepare_singleton_background_tasks():
                # The webhook itself is safe with multiple instances. The legacy
                # background loops are not, because this app uses an ephemeral local
                # SQLite DB. Disable those loops so deploys cannot duplicate reminders,
                # expiration actions, or PIX polling.
                main = importlib.import_module("app.main")

                async def _disabled_background_loop():
                    return None

                main.cleanup_expired_access = _disabled_background_loop
                main.send_subscription_reminders = _disabled_background_loop
                main.poll_pending_pix = _disabled_background_loop

            self._hot_telegram_webhook_registered = True

        _fastapi_init_with_telegram_webhook._hot_telegram_webhook_patch = True
        FastAPI.__init__ = _fastapi_init_with_telegram_webhook
except Exception as _exc:
    print(f"Telegram webhook runtime patch unavailable: {_exc}")
