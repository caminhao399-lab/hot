"""Small compatibility shim for the GGPIX PIX In request.

The provider documents amountCents, description, payerName, payerDocument and
externalId as the minimal stable PIX In payload. Keep optional per-request
metadata/webhook fields out of the HTTP request because the merchant already
has its webhook configured in the provider panel.
"""

from __future__ import annotations

import aiohttp


_original_request = aiohttp.ClientSession.request


async def _request(self, method, url, *args, **kwargs):
    if str(method).upper() == "POST" and str(url).rstrip("/").endswith("/pix/in"):
        payload = kwargs.get("json")
        if isinstance(payload, dict):
            payload = dict(payload)
            payload.pop("webhookUrl", None)
            payload.pop("metadata", None)
            kwargs["json"] = payload
    return await _original_request(self, method, url, *args, **kwargs)


aiohttp.ClientSession.request = _request
