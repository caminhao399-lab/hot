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

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
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

# PIX configuration. Identity data is now collected from the customer at checkout.
GGPIX_API_KEY = os.getenv("GGPIX_API_KEY", "").strip()
GGPIX_BASE_URL = os.getenv("GGPIX_BASE_URL", "https://ggpixapi.com/api/v1").strip().rstrip("/")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://hot-1-ih2f.onrender.com").strip().rstrip("/")
GGPIX_WEBHOOK_SECRET = os.getenv("GGPIX_WEBHOOK_SECRET", "").strip()

PLANS = {
    "essential": {"label": "VIP Essencial", "amount_cents": 800, "days": 30},
    "premium": {"label": "VIP Premium", "amount_cents": 1490, "days": 30},
    "acervo": {"label": "VIP Premium + Acervo", "amount_cents": 1690, "days": 30},
    "full": {"label": "Acesso Full + Bônus", "amount_cents": 2390, "days": 30},
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

# Temporary checkout data only. Name/document are not written to the database or logs.
pending_payer = {}


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
        [InlineKeyboardButton(text="ℹ️ Regras e suporte", callback_data="support")],
    ])


def plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 VIP Essencial — R$ 8,00", callback_data="plan:essential")],
        [InlineKeyboardButton(text="🔴 VIP Premium — R$ 14,90", callback_data="plan:premium")],
        [InlineKeyboardButton(text="🔒 VIP Premium + Acervo — R$ 16,90", callback_data="plan:acervo")],
        [InlineKeyboardButton(text="🎁 Acesso Full + Bônus — R$ 23,90", callback_data="plan:full")],
        [InlineKeyboardButton(text="⬅️ Voltar", callback_data="back")],
    ])


def pix_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Verificar pagamento", callback_data=f"pixcheck:{order_id}")],
        [InlineKeyboardButton(text="❌ Cancelar", callback_data="back")],
    ])


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
    return bool(GGPIX_API_KEY)


def order_payload(order_id: str) -> str:
    return f"vip:{order_id}"


def normalize_document(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def valid_cpf(value: str) -> bool:
    cpf = normalize_document(value)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    total = sum(int(cpf[i]) * (10 - i) for i in range(9))
    d1 = (total * 10) % 11
    if d1 == 10:
        d1 = 0
    if d1 != int(cpf[9]):
        return False
    total = sum(int(cpf[i]) * (11 - i) for i in range(10))
    d2 = (total * 10) % 11
    if d2 == 10:
        d2 = 0
    return d2 == int(cpf[10])


def valid_cnpj(value: str) -> bool:
    cnpj = normalize_document(value)
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False
    weights1 = [5,4,3,2,9,8,7,6,5,4,3,2]
    total = sum(int(cnpj[i]) * weights1[i] for i in range(12))
    d1 = 11 - (total % 11)
    if d1 >= 10:
        d1 = 0
    weights2 = [6,5,4,3,2,9,8,7,6,5,4,3,2]
    total = sum(int(cnpj[i]) * weights2[i] for i in range(13))
    d2 = 11 - (total % 11)
    if d2 >= 10:
        d2 = 0
    return d1 == int(cnpj[12]) and d2 == int(cnpj[13])


def valid_document(value: str) -> bool:
    document = normalize_document(value)
    return valid_cpf(document) or valid_cnpj(document)


async def ggpix_request(method: str, path: str, payload=None):
    headers = {"X-API-Key": GGPIX_API_KEY, "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    url = f"{GGPIX_BASE_URL}{path}"
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, url, headers=headers, json=payload) as response:
            raw = await response.text()
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"raw": raw[:1000]}
            if response.status >= 400:
                log.error("PIX provider HTTP %s: %s", response.status, data)
                raise RuntimeError(f"PIX HTTP {response.status}")
            return data


async def create_pix_order(user_id: int, plan_key: str, payer_name: str, payer_document: str):
    if not configured_pix():
        raise RuntimeError("PIX_NOT_CONFIGURED")
    plan = PLANS[plan_key]
    order_id = secrets.token_urlsafe(18)
    payload = {
        "amountCents": int(plan["amount_cents"]),
        "description": f"Acesso VIP - {plan['label']}",
        "payerName": payer_name,
        "payerDocument": normalize_document(payer_document),
        "externalId": order_id,
        "webhookUrl": f"{PUBLIC_BASE_URL}/webhooks/ggpix",
        "metadata": {"telegramUserId": str(user_id), "plan": plan_key},
    }
    data = await ggpix_request("POST", "/pix/in", payload)
    transaction_id = str(data.get("id") or data.get("transactionId") or "")
    pix_copy = data.get("pixCopyPaste") or data.get("pixCode") or ""
    if not transaction_id or not pix_copy:
        log.error("PIX response missing transaction id/code")
        raise RuntimeError("PIX_INVALID_RESPONSE")
    with closing(db()) as conn:
        conn.execute("""INSERT INTO pix_orders
            (order_id, telegram_id, plan_key, transaction_id, status, amount_cents, pix_code, created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (order_id, user_id, plan_key, transaction_id, str(data.get("status") or "PENDING").upper(), int(plan["amount_cents"]), pix_copy, now().isoformat()))
        conn.commit()
    return order_id, pix_copy


async def get_pix_status(transaction_id: str):
    return await ggpix_request("GET", f"/transactions/{transaction_id}")


def get_order(order_id: str):
    with closing(db()) as conn:
        return conn.execute("SELECT * FROM pix_orders WHERE order_id=?", (order_id,)).fetchone()


async def deliver_order(order_id: str):
    row = get_order(order_id)
    if not row or row["delivered_at"]:
        return
    if str(row["status"]).upper() not in ("COMPLETE", "PAID"):
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
    if str(row["status"]).upper() in ("COMPLETE", "PAID"):
        await deliver_order(order_id)
        return row["status"]
    data = await get_pix_status(row["transaction_id"])
    status = str(data.get("status") or "").upper()
    if status:
        with closing(db()) as conn:
            conn.execute("UPDATE pix_orders SET status=?, paid_at=CASE WHEN ? IN ('COMPLETE','PAID') THEN COALESCE(paid_at, ?) ELSE paid_at END WHERE order_id=?", (status, status, now().isoformat(), order_id))
            conn.commit()
    if status in ("COMPLETE", "PAID"):
        await deliver_order(order_id)
    return status or "UNKNOWN"


def webhook_signature_valid(raw_body: bytes, signature: str) -> bool:
    if not GGPIX_WEBHOOK_SECRET:
        return True
    expected = hmac.new(GGPIX_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    received = signature.strip()
    if received.startswith("sha256="):
        received = received[7:]
    return hmac.compare_digest(expected, received)


@router.message(CommandStart())
async def start(message: Message):
    pending_payer.pop(message.from_user.id, None)
    upsert_user(message)
    if VIDEO_FILE_ID:
        await message.answer_video(video=VIDEO_FILE_ID, caption=promo_text(), reply_markup=keyboard_menu())
    else:
        await message.answer(promo_text(), reply_markup=keyboard_menu())


@router.message(F.video)
async def receive_video(message: Message):
    if message.video:
        log.info("VIDEO_FILE_ID=%s", message.video.file_id)
        await message.reply("Vídeo recebido. O identificador foi registrado nos logs do Render para configurar o vídeo automático do /start.")


@router.callback_query(F.data == "buy")
async def buy(callback: CallbackQuery):
    await callback.answer()
    pending_payer.pop(callback.from_user.id, None)
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
        log.error("PIX is not configured on the server")
        await callback.message.answer("O pagamento por PIX está temporariamente indisponível. Tente novamente mais tarde.", reply_markup=keyboard_menu())
        return
    pending_payer[callback.from_user.id] = {"plan_key": plan_key, "step": "name"}
    await callback.message.answer("Para gerar seu PIX, informe seu <b>nome completo</b>.")


@router.message(Command("stats"))
async def stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    with closing(db()) as conn:
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        active = conn.execute("SELECT COUNT(DISTINCT telegram_id) c FROM subscriptions WHERE expires_at > ?", (now().isoformat(),)).fetchone()["c"]
        payments = conn.execute("SELECT COUNT(*) c FROM subscriptions").fetchone()["c"]
        pending = conn.execute("SELECT COUNT(*) c FROM pix_orders WHERE status NOT IN ('COMPLETE','PAID','FAILED','CANCELED','CANCELLED')").fetchone()["c"]
    await message.answer(f"<b>Dashboard</b>\nUsuários: {users}\nAssinaturas ativas: {active}\nPagamentos processados: {payments}\nPIX pendentes: {pending}")


@router.message(F.text)
async def collect_payer_details(message: Message):
    user_id = message.from_user.id
    data = pending_payer.get(user_id)
    if not data:
        return
    text = (message.text or "").strip()
    if data["step"] == "name":
        if len(text) < 3 or len(text.split()) < 2 or len(text) > 120:
            await message.answer("Informe seu <b>nome completo</b> para continuar.")
            return
        data["payer_name"] = text
        data["step"] = "document"
        await message.answer("Agora informe seu <b>CPF ou CNPJ</b> (pode enviar com ou sem pontuação).")
        return
    if data["step"] == "document":
        document = normalize_document(text)
        if not valid_document(document):
            await message.answer("CPF/CNPJ inválido. Confira os números e envie novamente.")
            return
        plan_key = data["plan_key"]
        payer_name = data["payer_name"]
        pending_payer.pop(user_id, None)
        plan = PLANS[plan_key]
        await message.answer("⏳ Gerando seu PIX...")
        try:
            order_id, pix_code = await create_pix_order(user_id, plan_key, payer_name, document)
        except Exception as exc:
            log.exception("Could not create PIX order: %s", exc)
            await message.answer("Não foi possível gerar o PIX agora. Tente novamente em alguns instantes.", reply_markup=plans_keyboard())
            return
        text_out = (f"<b>PIX gerado com sucesso</b> ✅\n\n<b>Plano:</b> {plan['label']}\n<b>Valor:</b> R$ {plan['amount_cents']/100:.2f}\n\n<b>Código PIX copia e cola:</b>\n<code>{pix_code}</code>\n\nCopie o código, faça o pagamento no seu banco e depois toque em <b>🔄 Verificar pagamento</b>.")
        await message.answer(text_out, reply_markup=pix_keyboard(order_id))


@router.callback_query(F.data.startswith("pixcheck:"))
async def pix_check(callback: CallbackQuery):
    await callback.answer("Verificando...")
    order_id = callback.data.split(":", 1)[1]
    row = get_order(order_id)
    if not row or row["telegram_id"] != callback.from_user.id:
        await callback.message.answer("Pagamento não encontrado.", reply_markup=plans_keyboard())
        return
    try:
        status = await verify_order(order_id)
    except Exception as exc:
        log.exception("PIX status check failed: %s", exc)
        await callback.message.answer("Ainda não consegui consultar o pagamento. Tente novamente em alguns segundos.", reply_markup=pix_keyboard(order_id))
        return
    if status in ("COMPLETE", "PAID"):
        await callback.message.answer("Pagamento confirmado. Seu acesso está sendo liberado.", reply_markup=keyboard_menu())
    elif status in ("FAILED", "CANCELED", "CANCELLED"):
        await callback.message.answer("Esse PIX não está mais disponível. Gere um novo pagamento.", reply_markup=plans_keyboard())
    else:
        await callback.message.answer("Pagamento ainda não identificado. Se você acabou de pagar, aguarde alguns segundos e toque novamente em <b>🔄 Verificar pagamento</b>.", reply_markup=pix_keyboard(order_id))


@router.callback_query(F.data == "back")
async def back(callback: CallbackQuery):
    await callback.answer()
    pending_payer.pop(callback.from_user.id, None)
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
            cutoff = now() - timedelta(days=1)
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
        await asyncio.sleep(3600)


async def poll_pending_pix():
    while True:
        try:
            with closing(db()) as conn:
                rows = conn.execute("SELECT order_id FROM pix_orders WHERE status NOT IN ('COMPLETE','PAID','FAILED','CANCELED','CANCELLED') AND created_at > ? ORDER BY created_at ASC LIMIT 20", ((now() - timedelta(hours=24)).isoformat(),)).fetchall()
            for row in rows:
                try:
                    await verify_order(row["order_id"])
                except Exception as exc:
                    log.warning("Pending PIX poll failed for %s: %s", row["order_id"], exc)
                await asyncio.sleep(0.2)
        except Exception:
            log.exception("Pending PIX poller failed")
        await asyncio.sleep(20)


@app.post("/webhooks/ggpix")
async def ggpix_webhook(request: Request):
    raw = await request.body()
    signature = request.headers.get("X-Webhook-Signature", "")
    if not webhook_signature_valid(raw, signature):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    transaction_id = str(data.get("transactionId") or data.get("id") or "")
    external_id = str(data.get("externalId") or "")
    status = str(data.get("status") or "").upper()
    if not external_id and transaction_id:
        with closing(db()) as conn:
            row = conn.execute("SELECT order_id FROM pix_orders WHERE transaction_id=?", (transaction_id,)).fetchone()
        external_id = row["order_id"] if row else ""
    row = get_order(external_id) if external_id else None
    if not row:
        return JSONResponse({"ok": True, "ignored": True})
    if status:
        with closing(db()) as conn:
            conn.execute("UPDATE pix_orders SET status=?, paid_at=CASE WHEN ? IN ('COMPLETE','PAID') THEN COALESCE(paid_at, ?) ELSE paid_at END WHERE order_id=?", (status, status, now().isoformat(), external_id))
            conn.commit()
    if status in ("COMPLETE", "PAID"):
        await deliver_order(external_id)
    return JSONResponse({"ok": True})


@app.get("/")
async def root():
    return {"service": "vip-telegram-bot", "status": "ok"}


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "bot": BOT_USERNAME, "pix_configured": configured_pix()})


async def bot_runner():
    await bot.delete_webhook(drop_pending_updates=False)
    asyncio.create_task(cleanup_expired_access())
    asyncio.create_task(send_subscription_reminders())
    asyncio.create_task(poll_pending_pix())
    log.info("Bot started as @%s", BOT_USERNAME)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


@app.on_event("startup")
async def startup():
    asyncio.create_task(bot_runner())


@app.on_event("shutdown")
async def shutdown():
    await bot.session.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
