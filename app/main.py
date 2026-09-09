import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Message
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hot-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "hotvip_oficial_bot").strip().lstrip("@")
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
GROUP_ID = os.getenv("GROUP_ID", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")
VIDEO_FILE_ID = os.getenv("VIDEO_FILE_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "/tmp/hot_bot.sqlite3")

BRAVOPAY_API_KEY = os.getenv("BRAVOPAY_API_KEY", "").strip()
BRAVOPAY_BASE_URL = os.getenv("BRAVOPAY_BASE_URL", "https://bravopay.club/api/v1").strip().rstrip("/")
BRAVOPAY_WEBHOOK_SECRET = os.getenv("BRAVOPAY_WEBHOOK_SECRET", "").strip()

BRAVOPAY_PRODUCT_IDS = {
    "essential": os.getenv("BRAVOPAY_PRODUCT_ID_ESSENTIAL", "").strip(),
    "premium": os.getenv("BRAVOPAY_PRODUCT_ID_PREMIUM", "").strip(),
    "acervo": os.getenv("BRAVOPAY_PRODUCT_ID_ACERVO", "").strip(),
    "full": os.getenv("BRAVOPAY_PRODUCT_ID_FULL", "").strip(),
}

DEFAULT_UTM = {
    "source": os.getenv("UTM_SOURCE", "").strip(),
    "medium": os.getenv("UTM_MEDIUM", "").strip(),
    "campaign": os.getenv("UTM_CAMPAIGN", "").strip(),
    "content": os.getenv("UTM_CONTENT", "").strip(),
    "term": os.getenv("UTM_TERM", "").strip(),
    "fbclid": os.getenv("UTM_FBCLID", "").strip(),
    "ttclid": os.getenv("UTM_TTCLID", "").strip(),
    "gclid": os.getenv("UTM_GCLID", "").strip(),
}

PLANS = {
    "essential": {"label": "VIP Essencial", "amount_cents": 1290, "days": 30},
    "premium": {"label": "VIP Premium", "amount_cents": 1890, "days": 30},
    "acervo": {"label": "VIP Premium + Acervo", "amount_cents": 2090, "days": 30},
    "full": {"label": "Acesso Full + Bônus", "amount_cents": 2990, "days": 30},
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not CHANNEL_ID:
    log.warning("CHANNEL_ID is not configured yet")

router = Router()
dp = Dispatcher()
dp.include_router(router)
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
app = FastAPI(title="VIP Telegram Bot")


def now() -> datetime:
    return datetime.now(timezone.utc)


def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, age_confirmed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL, expires_at TEXT NOT NULL, charge_id TEXT UNIQUE, payload TEXT UNIQUE, created_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS invite_links (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL, chat_id TEXT NOT NULL, invite_link TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS reminders (telegram_id INTEGER PRIMARY KEY, sent_at TEXT NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS pix_orders (
        order_id TEXT PRIMARY KEY,
        telegram_id INTEGER NOT NULL,
        plan_key TEXT NOT NULL,
        transaction_id TEXT,
        status TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        pix_code TEXT,
        created_at TEXT NOT NULL,
        paid_at TEXT,
        delivered_at TEXT
    )""")
    conn.commit()
    return conn


def upsert_user(message: Message):
    u = message.from_user
    ts = now().isoformat()
    with closing(db()) as conn:
        existing = conn.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (u.id,)).fetchone()
        if existing:
            conn.execute("UPDATE users SET username=?, first_name=?, age_confirmed=1, updated_at=? WHERE telegram_id=?", (u.username, u.first_name, ts, u.id))
        else:
            conn.execute("INSERT INTO users(telegram_id, username, first_name, age_confirmed, created_at, updated_at) VALUES(?,?,?,?,?,?)", (u.id, u.username, u.first_name, 1, ts, ts))
        conn.execute("DELETE FROM reminders WHERE telegram_id=?", (u.id,))
        conn.commit()


def active_subscription(user_id: int) -> bool:
    with closing(db()) as conn:
        row = conn.execute("SELECT expires_at FROM subscriptions WHERE telegram_id=? ORDER BY expires_at DESC LIMIT 1", (user_id,)).fetchone()
        return bool(row and datetime.fromisoformat(row["expires_at"]) > now())


def add_subscription(user_id: int, charge_id: str, payload: str, days: int) -> datetime:
    with closing(db()) as conn:
        row = conn.execute("SELECT expires_at FROM subscriptions WHERE telegram_id=? ORDER BY expires_at DESC LIMIT 1", (user_id,)).fetchone()
        base = now()
        if row:
            previous = datetime.fromisoformat(row["expires_at"])
            if previous > base:
                base = previous
        expires = base + timedelta(days=days)
        conn.execute("INSERT INTO subscriptions(telegram_id, expires_at, charge_id, payload, created_at) VALUES(?,?,?,?,?)", (user_id, expires.isoformat(), charge_id, payload, now().isoformat()))
        conn.execute("DELETE FROM reminders WHERE telegram_id=?", (user_id,))
        conn.commit()
    return expires


def save_invite(user_id: int, chat_id: str, link: str, expires: datetime):
    with closing(db()) as conn:
        conn.execute("INSERT INTO invite_links(telegram_id, chat_id, invite_link, expires_at, created_at) VALUES(?,?,?,?,?)", (user_id, chat_id, link, expires.isoformat(), now().isoformat()))
        conn.commit()


def keyboard_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Assinar acesso VIP", callback_data="buy")],
        [InlineKeyboardButton(text="📅 Meu acesso", callback_data="status")],
    ])


def plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 VIP Essencial — R$ 12,90", callback_data="plan:essential")],
        [InlineKeyboardButton(text="🔴 VIP Premium — R$ 18,90", callback_data="plan:premium")],
        [InlineKeyboardButton(text="⭐ VIP Premium + Acervo — R$ 20,90", callback_data="plan:acervo")],
        [InlineKeyboardButton(text="👑 Acesso Full + Bônus — R$ 29,90", callback_data="plan:full")],
        [InlineKeyboardButton(text="⬅️ Voltar", callback_data="back")],
    ])


def pix_keyboard(order_id: str, pix_code: str = "") -> InlineKeyboardMarkup:
    buttons = []
    if pix_code:
        buttons.append([InlineKeyboardButton(text="📋 Copiar chave PIX", copy_text=CopyTextButton(text=pix_code))])
    buttons.append([InlineKeyboardButton(text="🔄 Verificar pagamento", callback_data=f"pixcheck:{order_id}")])
    buttons.append([InlineKeyboardButton(text="❌ Cancelar", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def promo_text() -> str:
    return ("<b>🔥 VOCÊ ESTÁ A UM CLIQUE DO CONTEÚDO VIP EXCLUSIVO</b> 😈\n\n"
            "🟢 <b>OFERTA ESPECIAL DE LANÇAMENTO</b>\n\n"
            "🌸 Criadoras adultas\n"
            "⭐ Conteúdo exclusivo\n"
            "🎥 Vídeos e atualizações frequentes\n"
            "💋 Conteúdo sensual para maiores de 18\n"
            "🔥 Conteúdo premium e novidades\n"
            "🔒 Área privada para assinantes\n\n"
            "🎁 <b>BÔNUS IMEDIATO APÓS A COMPRA</b>\n"
            "• Novidades exclusivas\n"
            "• Conteúdo premium adicional\n"
            "• Atualizações para assinantes\n"
            "• Acesso a materiais exclusivos\n\n"
            "🌶️ <b>Conteúdo atualizado regularmente</b> ✅\n"
            "🌶️ <b>Área VIP privada</b> ✅\n"
            "🌶️ <b>Acesso liberado após o pagamento</b> ✅\n"
            "🌶️ <b>Novidades frequentes</b> ✅\n\n"
            "⚠️ <b>SERVIÇO EXCLUSIVO PARA MAIORES DE 18 ANOS.</b>\n\n"
            "🚨 <b>ÚLTIMAS VAGAS DA OFERTA ESPECIAL</b>\n"
            "<i>Entre agora e aproveite o acesso VIP.</i>")


def reminder_text() -> str:
    return ("👋 <b>Oi! Sua oferta VIP ainda está disponível.</b>\n\n"
            "Você iniciou o acesso, mas ainda não concluiu a assinatura.\n\n"
            "🔥 Aproveite a oferta especial enquanto estiver disponível.\n"
            "⭐ Conteúdo exclusivo para adultos\n"
            "🔒 Área privada para assinantes\n"
            "🎁 Bônus e novidades para assinantes\n\n"
            "⚠️ Serviço exclusivo para maiores de 18 anos.\n\n"
            "Se quiser continuar, é só tocar em <b>⭐ Assinar acesso VIP</b>.")


def support_text() -> str:
    return f"Suporte: @{SUPPORT_USERNAME}" if SUPPORT_USERNAME else "Suporte: configure SUPPORT_USERNAME no Render."


def configured_pix() -> bool:
    return bool(BRAVOPAY_API_KEY)


def order_payload(order_id: str) -> str:
    return f"vip:{order_id}"


async def bravopay_request(method: str, path: str, payload=None):
    headers = {
        "Authorization": f"Bearer {BRAVOPAY_API_KEY}",
        "Accept": "application/json",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    url = f"{BRAVOPAY_BASE_URL}{path}"
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, url, headers=headers, json=payload) as response:
            raw = await response.text()
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"raw": raw[:1000]}
            if response.status >= 400:
                log.error("BravoPay HTTP %s: %s", response.status, data)
                message = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else None
                raise RuntimeError(f"BravoPay HTTP {response.status}: {message or 'request failed'}")
            return data


def build_utm():
    return {key: value for key, value in DEFAULT_UTM.items() if value}


async def create_pix_order(user_id: int, plan_key: str):
    if not configured_pix():
        raise RuntimeError("PIX_NOT_CONFIGURED")
    plan = PLANS[plan_key]
    order_id = secrets.token_urlsafe(18)
    payload = {
        "amount_cents": int(plan["amount_cents"]),
        "method": "pix",
        "description": f"Acesso VIP - {plan['label']}",
        "external_reference": order_id,
        "metadata": {
            "telegram_user_id": str(user_id),
            "plan": plan_key,
        },
        "expires_in": 3600,
    }
    product_id = BRAVOPAY_PRODUCT_IDS.get(plan_key)
    if product_id:
        payload["product_id"] = product_id
    utm = build_utm()
    if utm:
        payload["utm"] = utm

    data = await bravopay_request("POST", "/transactions", payload)
    transaction_id = str(data.get("id") or "")
    pix = data.get("pix") or {}
    pix_copy = str(pix.get("copy_paste") or "")
    if not transaction_id or not pix_copy:
        log.error("BravoPay response missing transaction id or PIX code")
        raise RuntimeError("BRAVOPAY_INVALID_RESPONSE")

    with closing(db()) as conn:
        conn.execute("""INSERT INTO pix_orders
            (order_id, telegram_id, plan_key, transaction_id, status, amount_cents, pix_code, created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (
                order_id,
                user_id,
                plan_key,
                transaction_id,
                str(data.get("status") or "PENDING").upper(),
                int(plan["amount_cents"]),
                pix_copy,
                now().isoformat(),
            ))
        conn.commit()
    return order_id, pix_copy


async def get_pix_status(transaction_id: str):
    return await bravopay_request("GET", f"/transactions/{transaction_id}")


def get_order(order_id: str):
    with closing(db()) as conn:
        return conn.execute("SELECT * FROM pix_orders WHERE order_id=?", (order_id,)).fetchone()


async def recover_order_from_bravopay(order_id: str, telegram_id: int):
    data = await bravopay_request(
        "GET",
        f"/transactions?external_reference={quote(order_id, safe='')}&limit=1",
    )
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return None

    tx = items[0] if isinstance(items[0], dict) else {}
    metadata = tx.get("metadata") if isinstance(tx.get("metadata"), dict) else {}
    stored_user_id = str(metadata.get("telegram_user_id") or "")
    if stored_user_id and stored_user_id != str(telegram_id):
        return None

    amount_cents = int(tx.get("amount_cents") or 0)
    plan_key = str(metadata.get("plan") or "")
    if plan_key not in PLANS:
        plan_key = next((key for key, plan in PLANS.items() if int(plan["amount_cents"]) == amount_cents), "")
    if plan_key not in PLANS:
        return None

    transaction_id = str(tx.get("id") or "")
    if not transaction_id:
        return None

    status = str(tx.get("status") or "PENDING").upper()
    pix = tx.get("pix") if isinstance(tx.get("pix"), dict) else {}
    pix_code = str(pix.get("copy_paste") or "")
    created_at = str(tx.get("created_at") or now().isoformat())
    paid_at = str(tx.get("paid_at") or "") or None

    with closing(db()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pix_orders (order_id, telegram_id, plan_key, transaction_id, status, amount_cents, pix_code, created_at, paid_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (order_id, telegram_id, plan_key, transaction_id, status, amount_cents, pix_code, created_at, paid_at),
        )
        conn.commit()
    log.info("Recovered PIX order %s from BravoPay after local state loss", order_id)
    return get_order(order_id)


async def deliver_order(order_id: str):
    row = get_order(order_id)
    if not row or row["delivered_at"]:
        return
    if str(row["status"]).upper() != "PAID":
        return
    user_id = row["telegram_id"]
    plan = PLANS[row["plan_key"]]
    try:
        expires = add_subscription(user_id, row["transaction_id"], order_payload(order_id), plan["days"])
    except sqlite3.IntegrityError:
        with closing(db()) as conn:
            conn.execute("UPDATE pix_orders SET delivered_at=COALESCE(delivered_at, ?) WHERE order_id=?", (now().isoformat(), order_id))
            conn.commit()
        return
    links = []
    invite_expiry = now() + timedelta(hours=48)
    for chat_id in (CHANNEL_ID, GROUP_ID):
        if not chat_id:
            continue
        try:
            invite = await bot.create_chat_invite_link(chat_id=chat_id, name=f"VIP {user_id}", expire_date=int(invite_expiry.timestamp()), member_limit=1)
            save_invite(user_id, chat_id, invite.invite_link, invite_expiry)
            links.append(invite.invite_link)
        except Exception as exc:
            log.exception("Could not create invite for %s: %s", chat_id, exc)
    with closing(db()) as conn:
        conn.execute("UPDATE pix_orders SET delivered_at=? WHERE order_id=?", (now().isoformat(), order_id))
        conn.commit()
    text = f"<b>Pagamento confirmado!</b> ⭐\n\nSeu <b>{plan['label']}</b> foi liberado.\nValidade: <b>{expires.strftime('%d/%m/%Y %H:%M UTC')}</b>.\n\n"
    if links:
        text += "<b>Seu acesso:</b>\n" + "\n".join(f'• <a href="{link}">Entrar na área VIP</a>' for link in links) + "\n\nNão compartilhe esse link."
    else:
        text += "Seu pagamento foi confirmado, mas o link da área privada não está configurado. Fale com o suporte."
    await bot.send_message(user_id, text, reply_markup=keyboard_menu())


async def verify_order(order_id: str):
    row = get_order(order_id)
    if not row:
        return "NOT_FOUND"
    if str(row["status"]).upper() == "PAID":
        await deliver_order(order_id)
        return row["status"]
    data = await get_pix_status(row["transaction_id"])
    status = str(data.get("status") or "").upper()
    if status:
        with closing(db()) as conn:
            conn.execute("UPDATE pix_orders SET status=?, paid_at=CASE WHEN ?='PAID' THEN COALESCE(paid_at, ?) ELSE paid_at END WHERE order_id=?", (status, status, now().isoformat(), order_id))
            conn.commit()
    if status == "PAID":
        await deliver_order(order_id)
    return status or "UNKNOWN"


def webhook_signature_valid(raw_body: bytes, signature: str) -> bool:
    if not BRAVOPAY_WEBHOOK_SECRET:
        return False
    try:
        parts = dict(item.split("=", 1) for item in signature.strip().split(",") if "=" in item)
        timestamp = int(parts.get("t", "0"))
        received = parts.get("v1", "")
    except Exception:
        return False
    if not timestamp or not received:
        return False
    if abs(int(now().timestamp()) - timestamp) > 300:
        return False
    expected = hmac.new(
        BRAVOPAY_WEBHOOK_SECRET.encode(),
        f"{timestamp}.".encode() + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, received)


@router.message(CommandStart())
async def start(message: Message):
    upsert_user(message)
    await message.answer(promo_text(), reply_markup=keyboard_menu()) if not VIDEO_FILE_ID else await message.answer_video(video=VIDEO_FILE_ID, caption=promo_text(), reply_markup=keyboard_menu())


@router.message(F.video)
async def receive_video(message: Message):
    if message.video:
        log.info("VIDEO_FILE_ID=%s", message.video.file_id)
        await message.reply("Vídeo recebido. O identificador foi registrado nos logs do Render para configurar o vídeo automático do /start.")


@router.callback_query(F.data == "buy")
async def buy(callback: CallbackQuery):
    await callback.answer()
    upsert_user(callback.message)
    if active_subscription(callback.from_user.id):
        await callback.message.answer("Você já possui uma assinatura ativa. Use 'Meu acesso' para consultar a validade.", reply_markup=keyboard_menu())
        return
    await callback.message.answer("<b>Escolha seu acesso</b>\n\nSelecione uma opção abaixo para continuar:", reply_markup=plans_keyboard())


@router.callback_query(F.data.startswith("plan:"))
async def choose_plan(callback: CallbackQuery):
    await callback.answer()
    upsert_user(callback.message)
    plan_key = callback.data.split(":", 1)[1]
    if plan_key not in PLANS:
        await callback.message.answer("Opção inválida. Tente novamente.", reply_markup=plans_keyboard())
        return
    if active_subscription(callback.from_user.id):
        await callback.message.answer("Você já possui uma assinatura ativa.", reply_markup=keyboard_menu())
        return
    if not configured_pix():
        log.error("BravoPay is not configured on the server")
        await callback.message.answer("O pagamento por PIX está temporariamente indisponível. Tente novamente mais tarde.", reply_markup=keyboard_menu())
        return

    plan = PLANS[plan_key]
    await callback.message.answer("⏳ Gerando seu PIX...")
    try:
        order_id, pix_code = await create_pix_order(callback.from_user.id, plan_key)
    except Exception as exc:
        log.exception("Could not create BravoPay PIX order: %s", exc)
        await callback.message.answer("Não foi possível gerar o PIX agora. Tente novamente em alguns instantes.", reply_markup=plans_keyboard())
        return
    text_out = (f"<b>PIX gerado com sucesso</b> ✅\n\n<b>Plano:</b> {plan['label']}\n<b>Valor:</b> R$ {plan['amount_cents']/100:.2f}\n\n<b>Código PIX copia e cola:</b>\n<code>{pix_code}</code>\n\nCopie o código, faça o pagamento no seu banco e depois toque em <b>🔄 Verificar pagamento</b>.")
    await callback.message.answer(text_out, reply_markup=pix_keyboard(order_id, pix_code))


@router.message(Command("stats"))
async def stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    with closing(db()) as conn:
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        active = conn.execute("SELECT COUNT(DISTINCT telegram_id) c FROM subscriptions WHERE expires_at > ?", (now().isoformat(),)).fetchone()["c"]
        payments = conn.execute("SELECT COUNT(*) c FROM subscriptions").fetchone()["c"]
        pending = conn.execute("SELECT COUNT(*) c FROM pix_orders WHERE status NOT IN ('PAID','FAILED','EXPIRED','REFUNDED','CANCELED','CHARGEBACK')").fetchone()["c"]
    await message.answer(f"<b>Dashboard</b>\nUsuários: {users}\nAssinaturas ativas: {active}\nPagamentos processados: {payments}\nPIX pendentes: {pending}")


@router.callback_query(F.data.startswith("pixcheck:"))
async def pix_check(callback: CallbackQuery):
    await callback.answer("Verificando...")
    order_id = callback.data.split(":", 1)[1]
    row = get_order(order_id)
    if not row:
        try:
            row = await recover_order_from_bravopay(order_id, callback.from_user.id)
        except Exception as exc:
            log.exception("Could not recover PIX order %s from BravoPay: %s", order_id, exc)
            row = None
    if not row or row["telegram_id"] != callback.from_user.id:
        await callback.message.answer("Pagamento não encontrado. Tente novamente em alguns segundos.", reply_markup=plans_keyboard())
        return
    try:
        status = await verify_order(order_id)
    except Exception as exc:
        log.exception("BravoPay status check failed: %s", exc)
        await callback.message.answer("Ainda não consegui consultar o pagamento. Tente novamente em alguns segundos.", reply_markup=pix_keyboard(order_id))
        return
    if status == "PAID":
        await callback.message.answer("Pagamento confirmado. Seu acesso está sendo liberado.", reply_markup=keyboard_menu())
    elif status in ("FAILED", "EXPIRED", "CANCELED", "REFUNDED", "CHARGEBACK"):
        await callback.message.answer("Esse PIX não está mais disponível. Gere um novo pagamento.", reply_markup=plans_keyboard())
    else:
        await callback.message.answer("Pagamento ainda não identificado. Se você acabou de pagar, aguarde alguns segundos e toque novamente em <b>🔄 Verificar pagamento</b>.", reply_markup=pix_keyboard(order_id))


@router.callback_query(F.data == "back")
async def back(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(promo_text(), reply_markup=keyboard_menu())


@router.callback_query(F.data == "status")
async def status(callback: CallbackQuery):
    await callback.answer()
    upsert_user(callback.message)
    with closing(db()) as conn:
        row = conn.execute("SELECT expires_at FROM subscriptions WHERE telegram_id=? ORDER BY expires_at DESC LIMIT 1", (callback.from_user.id,)).fetchone()
    if not row:
        await callback.message.answer("Você ainda não possui uma assinatura.", reply_markup=keyboard_menu())
        return
    expires = datetime.fromisoformat(row["expires_at"])
    if expires <= now():
        await callback.message.answer("Sua assinatura expirou. Você pode contratar um novo período.", reply_markup=keyboard_menu())
        return
    await callback.message.answer(f"<b>Seu acesso está ativo.</b>\n\nValidade: {expires.strftime('%d/%m/%Y %H:%M UTC')}\nTempo restante: aproximadamente {(expires-now()).days} dia(s).", reply_markup=keyboard_menu())


@router.callback_query(F.data == "support")
async def support(callback: CallbackQuery):
    await callback.answer()
    upsert_user(callback.message)
    await callback.message.answer("<b>Regras e suporte</b>\n\n• Serviço exclusivo para maiores de 18 anos.\n• Não compartilhe links privados.\n• O acesso é pessoal e pode ser revogado em caso de abuso ou violação das regras.\n\n" + support_text(), reply_markup=keyboard_menu())


async def cleanup_expired_access():
    while True:
        try:
            with closing(db()) as conn:
                rows = conn.execute("SELECT DISTINCT telegram_id FROM subscriptions WHERE expires_at <= ?", (now().isoformat(),)).fetchall()
            for row in rows:
                for chat_id in (CHANNEL_ID, GROUP_ID):
                    if not chat_id:
                        continue
                    try:
                        await bot.ban_chat_member(chat_id=chat_id, user_id=row["telegram_id"])
                        await bot.unban_chat_member(chat_id=chat_id, user_id=row["telegram_id"], only_if_banned=True)
                    except Exception:
                        pass
        except Exception:
            log.exception("Expiration cleanup failed")
        await asyncio.sleep(3600)


async def send_subscription_reminders():
    while True:
        try:
            cutoff = now() - timedelta(minutes=30)
            with closing(db()) as conn:
                rows = conn.execute("SELECT u.telegram_id FROM users u LEFT JOIN reminders r ON r.telegram_id=u.telegram_id WHERE u.updated_at <= ? AND r.telegram_id IS NULL", (cutoff.isoformat(),)).fetchall()
            for row in rows:
                user_id = row["telegram_id"]
                if active_subscription(user_id):
                    continue
                try:
                    await bot.send_message(user_id, reminder_text(), reply_markup=keyboard_menu())
                    with closing(db()) as conn:
                        conn.execute("INSERT OR IGNORE INTO reminders(telegram_id, sent_at) VALUES(?,?)", (user_id, now().isoformat()))
                        conn.commit()
                except Exception as exc:
                    log.warning("Could not send reminder to %s: %s", user_id, exc)
        except Exception:
            log.exception("Reminder task failed")
        await asyncio.sleep(1800)


async def poll_pending_pix():
    while True:
        try:
            with closing(db()) as conn:
                rows = conn.execute("SELECT order_id FROM pix_orders WHERE status NOT IN ('PAID','FAILED','EXPIRED','REFUNDED','CANCELED','CHARGEBACK') AND created_at > ? ORDER BY created_at ASC LIMIT 20", ((now() - timedelta(hours=24)).isoformat(),)).fetchall()
            for row in rows:
                try:
                    await verify_order(row["order_id"])
                except Exception as exc:
                    log.warning("Pending BravoPay PIX poll failed for %s: %s", row["order_id"], exc)
                await asyncio.sleep(0.2)
        except Exception:
            log.exception("Pending PIX poller failed")
        await asyncio.sleep(20)


@app.post("/webhooks/bravopay")
async def bravopay_webhook(request: Request):
    raw = await request.body()
    signature = request.headers.get("BravoPay-Signature") or request.headers.get("X-Bravopay-Signature") or ""
    if not webhook_signature_valid(raw, signature):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)

    event_id = str(envelope.get("id") or "")
    event_type = str(envelope.get("type") or "")
    transaction = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    transaction_id = str(transaction.get("id") or "")
    external_id = str(transaction.get("external_reference") or "")
    if not external_id and transaction_id:
        with closing(db()) as conn:
            row = conn.execute("SELECT order_id FROM pix_orders WHERE transaction_id=?", (transaction_id,)).fetchone()
        external_id = row["order_id"] if row else ""
    row = get_order(external_id) if external_id else None
    if not row and external_id:
        metadata = transaction.get("metadata") if isinstance(transaction.get("metadata"), dict) else {}
        telegram_user_id = str(metadata.get("telegram_user_id") or "")
        if telegram_user_id.isdigit():
            try:
                row = await recover_order_from_bravopay(external_id, int(telegram_user_id))
            except Exception as exc:
                log.exception("Could not recover webhook PIX order %s: %s", external_id, exc)
    if not row:
        return JSONResponse({"ok": True, "ignored": True, "event_id": event_id})

    status = str(transaction.get("status") or "").upper()
    if event_type == "transaction.paid" or status == "PAID":
        status = "PAID"
    elif event_type == "transaction.expired":
        status = "EXPIRED"
    elif event_type == "transaction.failed":
        status = "FAILED"
    elif event_type == "transaction.refunded":
        status = "REFUNDED"
    elif event_type == "transaction.chargeback":
        status = "CHARGEBACK"

    if status:
        with closing(db()) as conn:
            conn.execute("UPDATE pix_orders SET status=?, paid_at=CASE WHEN ?='PAID' THEN COALESCE(paid_at, ?) ELSE paid_at END WHERE order_id=?", (status, status, now().isoformat(), external_id))
            conn.commit()
    if status == "PAID":
        await deliver_order(external_id)
    return JSONResponse({"ok": True, "event_id": event_id})


@app.get("/")
async def root():
    return {"service": "vip-telegram-bot", "status": "ok"}


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "bot": BOT_USERNAME, "pix_configured": configured_pix()})


async def bot_runner():
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.set_my_commands([
        BotCommand(command="start", description="Iniciar"),
        BotCommand(command="assinar", description="Assinar acesso VIP"),
        BotCommand(command="meuacesso", description="Consultar meu acesso"),
    ])
    asyncio.create_task(cleanup_expired_access())
    asyncio.create_task(send_subscription_reminders())
    asyncio.create_task(poll_pending_pix())
    log.info("Bot started as @%s", BOT_USERNAME)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


@router.message(Command("assinar"))
async def command_assinar(message: Message):
    upsert_user(message)
    if active_subscription(message.from_user.id):
        await message.answer("Você já possui uma assinatura ativa.", reply_markup=keyboard_menu())
        return
    await message.answer("<b>Escolha seu acesso</b>\n\nSelecione uma opção abaixo para continuar:", reply_markup=plans_keyboard())


@router.message(Command("meuacesso"))
async def command_meuacesso(message: Message):
    upsert_user(message)
    with closing(db()) as conn:
        row = conn.execute("SELECT expires_at FROM subscriptions WHERE telegram_id=? ORDER BY expires_at DESC LIMIT 1", (message.from_user.id,)).fetchone()
    if not row:
        await message.answer("Você ainda não possui uma assinatura.", reply_markup=keyboard_menu())
        return
    expires = datetime.fromisoformat(row["expires_at"])
    if expires <= now():
        await message.answer("Sua assinatura expirou. Você pode contratar um novo período.", reply_markup=keyboard_menu())
        return
    await message.answer(f"<b>Seu acesso está ativo.</b>\n\nValidade: {expires.strftime('%d/%m/%Y %H:%M UTC')}\nTempo restante: aproximadamente {(expires-now()).days} dia(s).", reply_markup=keyboard_menu())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
