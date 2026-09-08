"""Compatibility shim for the GGPIX PIX In request.

The provider documents amountCents, description, payerName, payerDocument and
externalId as a valid minimal PIX In payload. This shim removes only optional
per-request webhook/metadata fields before the request is sent.
"""

from __future__ import annotations

import aiohttp


_original_request = aiohttp.ClientSession.request


def _request(self, method, url, *args, **kwargs):
    if str(method).upper() == "POST" and str(url).rstrip("/").endswith("/pix/in"):
        payload = kwargs.get("json")
        if isinstance(payload, dict):
            payload = dict(payload)
            payload.pop("webhookUrl", None)
            payload.pop("metadata", None)
            kwargs["json"] = payload
    return _original_request(self, method, url, *args, **kwargs)


aiohttp.ClientSession.request = _request
