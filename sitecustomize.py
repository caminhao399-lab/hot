"""Ajustes de inicialização para a integração BravoPay."""

import os
import uuid
from pathlib import Path

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

# O checkout usa o mesmo cliente HTTP do bot e recebe uma chave de idempotência.
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

# Registra o checkout no FastAPI no momento em que a aplicação principal é criada.
# Isso evita depender de um event loop separado do Uvicorn.
try:
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    _original_fastapi_init = FastAPI.__init__

    def _fastapi_init_with_checkout(self, *args, **kwargs):
        _original_fastapi_init(self, *args, **kwargs)
        try:
            from app.checkout import router as checkout_router

            self.include_router(checkout_router)

            @self.get("/checkout", response_class=HTMLResponse)
            async def checkout_page():
                path = Path(__file__).resolve().parent / "app" / "checkout.html"
                return path.read_text(encoding="utf-8")

            @self.get("/obrigado", response_class=HTMLResponse)
            async def thank_you_page():
                return """<!doctype html><html lang='pt-BR'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Pagamento confirmado</title><style>body{font-family:Arial;background:#0b0b0f;color:#fff;text-align:center;padding:70px 20px}main{max-width:520px;margin:auto;background:#17171e;padding:35px;border-radius:22px}a{display:inline-block;margin-top:20px;padding:14px 20px;border-radius:10px;background:#fff;color:#111;text-decoration:none;font-weight:bold}</style><main><h1>Pagamento confirmado ✅</h1><p>Seu pagamento foi confirmado pela BravoPay.</p><p>Se o acesso for vinculado ao Telegram, aguarde a liberação automática.</p><a href='/checkout'>Voltar ao checkout</a></main></html>"""
        except Exception:
            pass

    FastAPI.__init__ = _fastapi_init_with_checkout
except Exception:
    pass
