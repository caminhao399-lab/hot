import os
import re
import uuid
import aiohttp
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/checkout")
BASE = os.getenv("BRAVOPAY_BASE_URL", "https://bravopay.club/api/v1").rstrip("/")
KEY = os.getenv("BRAVOPAY_API_KEY", "").strip()

PLANS = {
    "essential": {"name": "VIP Essencial", "amount": 800, "product": os.getenv("BRAVOPAY_PRODUCT_ID_ESSENTIAL", "").strip()},
    "premium": {"name": "VIP Premium", "amount": 1490, "product": os.getenv("BRAVOPAY_PRODUCT_ID_PREMIUM", "").strip()},
    "acervo": {"name": "VIP Premium + Acervo", "amount": 1690, "product": os.getenv("BRAVOPAY_PRODUCT_ID_ACERVO", "").strip()},
    "full": {"name": "Acesso Full + Bônus", "amount": 2390, "product": os.getenv("BRAVOPAY_PRODUCT_ID_FULL", "").strip()},
}


class Customer(BaseModel):
    name: str
    email: str
    phone: str
    cpf: str


class CheckoutRequest(BaseModel):
    plan: str
    customer: Customer
    utm: dict = {}


def digits(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


def valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()))


async def bravopay(method: str, endpoint: str, payload=None):
    if not KEY:
        raise HTTPException(500, "BRAVOPAY_API_KEY não configurada no Render.")
    headers = {"Authorization": f"Bearer {KEY}", "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.request(method, f"{BASE}{endpoint}", headers=headers, json=payload) as response:
            raw = await response.text()
            try:
                data = await response.json(content_type=None)
            except Exception:
                data = {"raw": raw[:1000]}
            if response.status >= 400:
                # Nunca registra a chave ou os dados do cliente.
                print(f"BravoPay HTTP {response.status}")
                raise HTTPException(502, "A BravoPay recusou a cobrança. Confira a configuração do pagamento no Render.")
            return data


@router.post("/create")
async def create_payment(req: CheckoutRequest):
    plan = PLANS.get(req.plan)
    if not plan:
        raise HTTPException(400, "Plano inválido.")
    name = req.customer.name.strip()
    email = req.customer.email.strip()
    phone = req.customer.phone.strip()
    document = digits(req.customer.cpf)
    if len(name) < 3:
        raise HTTPException(400, "Nome inválido.")
    if not valid_email(email):
        raise HTTPException(400, "E-mail inválido.")
    if len(digits(phone)) < 10:
        raise HTTPException(400, "Telefone inválido.")
    if len(document) not in (11, 14):
        raise HTTPException(400, "CPF/CNPJ inválido.")

    payload = {
        "amount_cents": plan["amount"],
        "method": "pix",
        "customer": {
            "name": name,
            "email": email,
            "phone": phone,
            "cpf": document,
        },
        "external_reference": f"checkout_{uuid.uuid4().hex}",
        "description": plan["name"],
    }
    if plan["product"]:
        payload["product_id"] = plan["product"]

    allowed = ["source", "medium", "campaign", "content", "term", "fbclid", "ttclid", "gclid"]
    utm = {k: str(req.utm[k])[:500] for k in allowed if req.utm.get(k)}
    if utm:
        payload["utm"] = utm

    data = await bravopay("POST", "/transactions", payload)
    pix = data.get("pix") or {}
    code = pix.get("copy_paste")
    if not code:
        raise HTTPException(502, "A BravoPay não retornou pix.copy_paste.")
    return {
        "success": True,
        "transaction_id": data.get("id"),
        "status": data.get("status"),
        "pix": {"copy_paste": code, "expires_at": pix.get("expires_at")},
    }


@router.get("/status/{transaction_id}")
async def payment_status(transaction_id: str):
    data = await bravopay("GET", f"/transactions/{transaction_id}")
    return {"success": True, "id": data.get("id"), "status": data.get("status")}
