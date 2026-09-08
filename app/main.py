import asyncio
import logging
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from fastapi import FastAPI
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hot-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "porno_sexy_bot").strip().lstrip("@")
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
GROUP_ID = os.getenv("GROUP_ID", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
PRICE_STARS = int(os.getenv("PRICE_STARS", "100") or 100)
SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30") or 30)
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")
DB_PATH = os.getenv("DB_PATH", "/tmp/hot_bot.sqlite3")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

if not CHANNEL_ID:
    log.warning("CHANNEL_ID is not configured yet")

router = Router()
dp = Dispatcher()
dp.include_router(router)
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
app = FastAPI(title="HOT Telegram Bot")


def now() -> datetime:
    return datetime.now(timezone.utc)


def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            age_confirmed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            charge_id TEXT UNIQUE,
            payload TEXT UNIQUE,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS invite_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL,
            invite_link TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def upsert_user(message: Message):
    u = message.from_user
    ts = now().isoformat()
    with closing(db()) as conn:
        existing = conn.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (u.id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET username=?, first_name=?, age_confirmed=1, updated_at=? WHERE telegram_id=?",
                (u.username, u.first_name, ts, u.id),
            )
        else:
            conn.execute(
                "INSERT INTO users(telegram_id, username, first_name, age_confirmed, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (u.id, u.username, u.first_name, 1, ts, ts),
            )
        conn.commit()


def active_subscription(user_id: int) -> bool:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT expires_at FROM subscriptions WHERE telegram_id=? ORDER BY expires_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return bool(row and datetime.fromisoformat(row["expires_at"]) > now())


def add_subscription(user_id: int, charge_id: str, payload: str) -> datetime:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT expires_at FROM subscriptions WHERE telegram_id=? ORDER BY expires_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        base = now()
        if row:
            previous = datetime.fromisoformat(row["expires_at"])
            if previous > base:
                base = previous
        expires = base + timedelta(days=SUBSCRIPTION_DAYS)
        conn.execute(
            "INSERT INTO subscriptions(telegram_id, expires_at, charge_id, payload, created_at) VALUES(?,?,?,?,?)",
            (user_id, expires.isoformat(), charge_id, payload, now().isoformat()),
        )
        conn.commit()
    return expires


def save_invite(user_id: int, chat_id: str, link: str, expires: datetime):
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO invite_links(telegram_id, chat_id, invite_link, expires_at, created_at) VALUES(?,?,?,?,?)",
            (user_id, chat_id, link, expires.isoformat(), now().isoformat()),
        )
        conn.commit()


def keyboard_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="⭐ Assinar acesso VIP", callback_data="buy")],
        [InlineKeyboardButton(text="📅 Meu acesso", callback_data="status")],
        [InlineKeyboardButton(text="ℹ️ Regras e suporte", callback_data="support")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def support_text() -> str:
    if SUPPORT_USERNAME:
        return f"Suporte: @{SUPPORT_USERNAME}"
    return "Suporte: configure SUPPORT_USERNAME no Render."


@router.message(CommandStart())
async def start(message: Message):
    upsert_user(message)
    await message.answer(
        "<b>Bem-vindo à Área VIP.</b>\n\nServiço destinado exclusivamente a maiores de 18 anos.\n\nEscolha uma opção abaixo:",
        reply_markup=keyboard_menu(),
    )


@router.callback_query(F.data == "buy")
async def buy(callback: CallbackQuery):
    await callback.answer()
    if active_subscription(callback.from_user.id):
        await callback.message.answer("Você já possui uma assinatura ativa. Use 'Meu acesso' para consultar a validade.", reply_markup=keyboard_menu())
        return
    payload = f"vip:{callback.from_user.id}:{secrets.token_urlsafe(12)}"
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Acesso VIP — 30 dias",
        description="Assinatura de acesso digital à área VIP por 30 dias.",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label="Acesso VIP — 30 dias", amount=PRICE_STARS)],
        provider_token="",
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    if not query.invoice_payload.startswith(f"vip:{query.from_user.id}:"):
        await query.answer(ok=False, error_message="Pedido inválido. Tente iniciar a compra novamente.")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message):
    payment = message.successful_payment
    try:
        expires = add_subscription(
            message.from_user.id,
            payment.telegram_payment_charge_id,
            payment.invoice_payload,
        )
    except sqlite3.IntegrityError:
        await message.answer("Este pagamento já foi processado. Use 'Meu acesso'.", reply_markup=keyboard_menu())
        return

    links: list[str] = []
    invite_expiry = now() + timedelta(hours=48)
    for chat_id in (CHANNEL_ID, GROUP_ID):
        if not chat_id:
            continue
        try:
            invite = await bot.create_chat_invite_link(
                chat_id=chat_id,
                name=f"VIP {message.from_user.id}",
                expire_date=int(invite_expiry.timestamp()),
                member_limit=1,
            )
            save_invite(message.from_user.id, chat_id, invite.invite_link, invite_expiry)
            links.append(invite.invite_link)
        except Exception as exc:
            log.exception("Could not create invite for %s: %s", chat_id, exc)

    text = f"<b>Pagamento confirmado!</b> ⭐\n\nSeu acesso está válido até <b>{expires.strftime('%d/%m/%Y %H:%M UTC')}</b>.\n\n"
    if links:
        text += "<b>Seus links de acesso:</b>\n" + "\n".join(f"• <a href=\"{link}\">Entrar na área VIP</a>" for link in links)
        text += "\n\nNão compartilhe esses links."
    else:
        text += "O pagamento foi registrado, mas os links ainda não estão configurados. Configure CHANNEL_ID/GROUP_ID e fale com o suporte."
    await message.answer(text, reply_markup=keyboard_menu())


@router.callback_query(F.data == "status")
async def status(callback: CallbackQuery):
    await callback.answer()
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT expires_at FROM subscriptions WHERE telegram_id=? ORDER BY expires_at DESC LIMIT 1",
            (callback.from_user.id,),
        ).fetchone()
    if not row:
        await callback.message.answer("Você ainda não possui uma assinatura.", reply_markup=keyboard_menu())
        return
    expires = datetime.fromisoformat(row["expires_at"])
    if expires <= now():
        await callback.message.answer("Sua assinatura expirou. Você pode contratar um novo período.", reply_markup=keyboard_menu())
        return
    remaining = expires - now()
    days = remaining.days
    await callback.message.answer(
        f"<b>Seu acesso está ativo.</b>\n\nValidade: {expires.strftime('%d/%m/%Y %H:%M UTC')}\nTempo restante: aproximadamente {days} dia(s).",
        reply_markup=keyboard_menu(),
    )


@router.callback_query(F.data == "support")
async def support(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "<b>Regras e suporte</b>\n\n• Serviço exclusivo para maiores de 18 anos.\n• Não compartilhe links privados.\n• O acesso é pessoal e pode ser revogado em caso de abuso ou violação das regras.\n\n" + support_text(),
        reply_markup=keyboard_menu(),
    )


@router.message(Command("stats"))
async def stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    with closing(db()) as conn:
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        active = conn.execute("SELECT COUNT(DISTINCT telegram_id) c FROM subscriptions WHERE expires_at > ?", (now().isoformat(),)).fetchone()["c"]
        payments = conn.execute("SELECT COUNT(*) c FROM subscriptions").fetchone()["c"]
    await message.answer(f"<b>Dashboard</b>\nUsuários: {users}\nAssinaturas ativas: {active}\nPagamentos processados: {payments}")


async def cleanup_expired_access():
    while True:
        try:
            with closing(db()) as conn:
                rows = conn.execute(
                    "SELECT DISTINCT telegram_id FROM subscriptions WHERE expires_at <= ?",
                    (now().isoformat(),),
                ).fetchall()
            for row in rows:
                user_id = row["telegram_id"]
                for chat_id in (CHANNEL_ID, GROUP_ID):
                    if not chat_id:
                        continue
                    try:
                        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
                        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
                    except Exception:
                        pass
        except Exception:
            log.exception("Expiration cleanup failed")
        await asyncio.sleep(3600)


@app.get("/")
async def root():
    return {"service": "hot-telegram-bot", "status": "ok"}


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "bot": BOT_USERNAME})


async def bot_runner():
    await bot.delete_webhook(drop_pending_updates=False)
    asyncio.create_task(cleanup_expired_access())
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
