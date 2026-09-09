"""Application package hooks for checkout and Telegram webhook mode."""

import asyncio
import hashlib
import os
import re
from pathlib import Path

from aiogram import Dispatcher
from aiogram.types import CallbackQuery, Message as AiogramMessage
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


Dispatcher.start_polling = _start_webhook_instead_of_polling


# Callback queries can expire while a Render instance is waking up or while
# another Telegram/BravoPay request is being processed. Never let an expired
# callback answer abort the actual button handler.
_original_callback_answer = CallbackQuery.answer


async def _safe_callback_answer(self, *args, **kwargs):
    try:
        return await asyncio.wait_for(_original_callback_answer(self, *args, **kwargs), timeout=2.5)
    except Exception:
        return None


if not getattr(CallbackQuery.answer, "_hot_safe_callback_patch", False):
    _safe_callback_answer._hot_safe_callback_patch = True
    CallbackQuery.answer = _safe_callback_answer


_original_message_answer = AiogramMessage.answer


async def _message_answer_with_split_pix_flow(self, text=None, *args, **kwargs):
    """Render the existing PIX response as the requested separate messages."""
    if isinstance(text, str) and "PIX gerado com sucesso" in text and "Código PIX copia e cola:" in text:
        code_match = re.search(r"<code>(.*?)</code>", text, re.S)
        plan_match = re.search(r"<b>Plano:</b>\s*(.*?)\s*<b>Valor:</b>\s*R\$\s*([0-9.,]+)", text, re.S)
        if code_match and plan_match:
            pix_code = code_match.group(1).strip()
            plan_label = re.sub(r"\s+", " ", plan_match.group(1).strip())
            value = plan_match.group(2).strip()
            final_kwargs = dict(kwargs)
            reply_markup = final_kwargs.pop("reply_markup", None)
            await _original_message_answer(self, f"<b>PIX gerado com sucesso</b> ✅\n\n<b>Plano:</b> {plan_label}\n\n<b>Valor:</b> R$ {value}", *args, **final_kwargs)
            await _original_message_answer(self, "✅ <b>Como realizar o pagamento:</b>\n\n1. Abra o aplicativo do seu banco.\n2. Selecione a opção \"Pagar\" ou \"PIX\".\n3. Escolha \"PIX Copia e Cola\".\n4. Cole a chave da mensagem abaixo...", *args, **final_kwargs)
            await _original_message_answer(self, "Copie o código abaixo:", *args, **final_kwargs)
            await _original_message_answer(self, f"<code>{pix_code}</code>", *args, **final_kwargs)
            final_kwargs["reply_markup"] = reply_markup
            return await _original_message_answer(self, "Após efetuar o pagamento, clique no botão abaixo 👇", *args, **final_kwargs)
    return await _original_message_answer(self, text, *args, **kwargs)


if not getattr(AiogramMessage.answer, "_hot_pix_split_patch", False):
    _message_answer_with_split_pix_flow._hot_pix_split_patch = True
    AiogramMessage.answer = _message_answer_with_split_pix_flow


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
        import app.main as main
        # Acknowledge Telegram immediately. Processing the update in the
        # background prevents slow BravoPay/API calls from making Telegram
        # retry the same webhook update or making button callbacks expire.
        task = asyncio.create_task(main.dp.feed_raw_update(main.bot, payload))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return JSONResponse({"ok": True})

    @self.on_event("startup")
    async def _patch_pix_button_labels():
        import app.main as main
        original_pix_keyboard = main.pix_keyboard
        if getattr(original_pix_keyboard, "_hot_button_label_patch", False):
            return

        def _pix_keyboard_with_requested_labels(order_id: str, pix_code: str = ""):
            markup = original_pix_keyboard(order_id, pix_code)
            for row in markup.inline_keyboard:
                for button in row:
                    if button.text == "📋 Copiar chave PIX":
                        button.text = "📋 Copiar Código"
                    elif button.text == "🔄 Verificar pagamento":
                        button.text = "✅ Verificar Status"
            return markup

        _pix_keyboard_with_requested_labels._hot_button_label_patch = True
        main.pix_keyboard = _pix_keyboard_with_requested_labels

    self._hot_integrations_registered = True


FastAPI.__init__ = _fastapi_init_with_checkout
