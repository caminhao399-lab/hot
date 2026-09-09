import asyncio
import hashlib
import os

from aiogram import Dispatcher
from fastapi import Request
from fastapi.responses import JSONResponse


WEBHOOK_PATH = "/telegram/webhook"
PUBLIC_URL = (
    os.getenv("PUBLIC_BASE_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or "https://hot-1-ih2f.onrender.com"
).strip().rstrip("/")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest() if BOT_TOKEN else ""


async def start_webhook(self, *bots, **kwargs):
    bot = bots[0] if bots else kwargs.get("bot")
    if bot is None:
        raise RuntimeError("Telegram bot is missing")
    await bot.set_webhook(
        f"{PUBLIC_URL}{WEBHOOK_PATH}",
        allowed_updates=self.resolve_used_update_types(),
        drop_pending_updates=False,
        secret_token=WEBHOOK_SECRET or None,
    )
    print(f"Telegram webhook active: {PUBLIC_URL}{WEBHOOK_PATH}")


# Patch aiogram before app.main is imported. This is intentionally done in the
# process entrypoint, where it is guaranteed to run before bot_runner().
Dispatcher.start_polling = start_webhook

import app.main as main  # noqa: E402


# The reminder text already configured in app.main is kept unchanged.
# First reminder: 30 minutes after the user's last activity. After that,
# resend every 30 minutes until the user subscribes. This task is independent
# of PIX generation/status.
async def repeating_subscription_reminders():
    while True:
        try:
            current = main.now()
            first_reminder_cutoff = current - main.timedelta(minutes=30)
            repeat_cutoff = current - main.timedelta(minutes=30)
            with main.closing(main.db()) as conn:
                rows = conn.execute(
                    "SELECT u.telegram_id FROM users u "
                    "LEFT JOIN reminders r ON r.telegram_id=u.telegram_id "
                    "WHERE u.updated_at <= ? "
                    "AND (r.telegram_id IS NULL OR r.sent_at <= ?)",
                    (first_reminder_cutoff.isoformat(), repeat_cutoff.isoformat()),
                ).fetchall()

            for row in rows:
                user_id = row["telegram_id"]
                if main.active_subscription(user_id):
                    continue
                try:
                    await main.bot.send_message(
                        user_id,
                        main.reminder_text(),
                        reply_markup=main.keyboard_menu(),
                    )
                    with main.closing(main.db()) as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO reminders(telegram_id, sent_at) VALUES(?,?)",
                            (user_id, main.now().isoformat()),
                        )
                        conn.commit()
                except Exception as exc:
                    print(f"Could not send reminder to {user_id}: {exc}")
        except Exception as exc:
            print(f"Reminder task failed: {exc}")
        await asyncio.sleep(1800)


@main.app.on_event("startup")
async def start_repeating_subscription_reminders():
    asyncio.create_task(repeating_subscription_reminders())


@main.app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return JSONResponse({"ok": False}, status_code=401)
    payload = await request.json()
    await main.dp.feed_raw_update(main.bot, payload)
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(main.app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
