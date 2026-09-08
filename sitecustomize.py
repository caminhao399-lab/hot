"""Ajustes de inicialização para a integração BravoPay."""

import os
import uuid

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
