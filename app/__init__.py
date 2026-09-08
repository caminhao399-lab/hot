"""Application package.

Registers the web checkout when the FastAPI application is created.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from app.checkout import router as _checkout_router

_original_fastapi_init = FastAPI.__init__


def _fastapi_init_with_checkout(self, *args, **kwargs):
    _original_fastapi_init(self, *args, **kwargs)
    if getattr(self, "_hot_checkout_registered", False):
        return
    self.include_router(_checkout_router)

    @self.get("/checkout", response_class=HTMLResponse)
    async def checkout_page():
        return (Path(__file__).resolve().parent / "checkout.html").read_text(encoding="utf-8")

    @self.get("/obrigado", response_class=HTMLResponse)
    async def thank_you_page():
        return """<!doctype html><html lang='pt-BR'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Pagamento confirmado</title><style>body{font-family:Arial;background:#0b0b0f;color:#fff;text-align:center;padding:70px 20px}main{max-width:520px;margin:auto;background:#17171e;padding:35px;border-radius:22px}a{display:inline-block;margin-top:20px;padding:14px 20px;border-radius:10px;background:#fff;color:#111;text-decoration:none;font-weight:bold}</style><main><h1>Pagamento confirmado ✅</h1><p>Seu pagamento foi confirmado pela BravoPay.</p><p>Se o acesso for vinculado ao Telegram, aguarde a liberação automática.</p><a href='/checkout'>Voltar ao checkout</a></main></html>"""

    self._hot_checkout_registered = True


FastAPI.__init__ = _fastapi_init_with_checkout
