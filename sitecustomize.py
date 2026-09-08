"""Ajustes de inicialização para a integração BravoPay.

Este arquivo é carregado automaticamente pelo Python antes do aplicativo.
Não contém nenhuma credencial.
"""

import asyncio
import os
import uuid
import sys
from pathlib import Path

# Mantém a URL oficial mesmo se uma variável antiga tiver ficado no Render.
os.environ["BRAVOPAY_BASE_URL"] = os.getenv(
    "BRAVOPAY_BASE_URL", "https://bravopay.club/api/v1"
).strip().rstrip("/") or "https://bravopay.club/api/v1"

# Evita que IDs de produto vazios/placeholder sejam enviados por engano.
for _key in (
    "BRAVOPAY_PRODUCT_ID_ESSENTIAL",
    "BRAVOPAY_PRODUCT_ID_PREMIUM",
    "BRAVOPAY_PRODUCT_ID_ACERVO",
    "BRAVOPAY_PRODUCT_ID_FULL",
):
    _value = os.getenv(_key, "").strip()
    if _value and not _value.startswith("prd_"):
        os.environ[_key] = ""

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


def _attach_checkout():
    main = sys.modules.get("app.main")
    if main is None or getattr(main, "_checkout_attached", False):
        return False
    try:
        from fastapi.responses import HTMLResponse
        from app.checkout import router as checkout_router
        main.app.include_router(checkout_router)

        @main.app.get("/checkout", response_class=HTMLResponse)
        async def checkout_page():
            path = Path(__file__).resolve().parent / "app" / "checkout.html"
            return path.read_text(encoding="utf-8")

        @main.app.get("/obrigado", response_class=HTMLResponse)
        async def thank_you_page():
            return """<!doctype html><html lang='pt-BR'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Pagamento confirmado</title><style>body{font-family:Arial;background:#0b0b0f;color:#fff;text-align:center;padding:70px 20px}main{max-width:520px;margin:auto;background:#17171e;padding:35px;border-radius:22px}a{display:inline-block;margin-top:20px;padding:14px 20px;border-radius:10px;background:#fff;color:#111;text-decoration:none;font-weight:bold}</style><main><h1>Pagamento confirmado ✅</h1><p>Seu pagamento foi confirmado pela BravoPay.</p><p>Se o acesso for vinculado ao Telegram, aguarde a liberação automática.</p><a href='/checkout'>Voltar ao checkout</a></main></html>"""

        main._checkout_attached = True
        return True
    except Exception:
        return False


def _watch():
    async def runner():
        for _ in range(100):
            if _attach_checkout():
                return
            await asyncio.sleep(0.05)
    try:
        asyncio.get_event_loop().create_task(runner())
    except Exception:
        pass


if os.getenv("RENDER", "") or os.getenv("PORT", ""):
    _watch()
