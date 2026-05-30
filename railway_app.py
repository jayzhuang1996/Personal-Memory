"""
Unified Railway deployment for GBrain.
Runs three things in one process:
  1. Ingestion server (FastAPI — /ingest endpoint)
  2. Telegram bot (webhook mode — /telegram-webhook)
  3. X bookmarks scraper (background task every 12h)
"""

import os
import sys
import asyncio
import datetime
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import Application

# Import the existing ingestion server's FastAPI app
from ingestion_server import app

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("railway")

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
PORT = int(os.getenv("PORT", "8000"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN env var is required")

# ─────────────────────────────────────────────
# Telegram Bot Setup
# ─────────────────────────────────────────────

from telegram.ext import CommandHandler, MessageHandler, filters

# Import handler functions from the bot module
import telegram_biographer as bot_module

telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

telegram_app.add_handler(CommandHandler("start", bot_module.start_command))
telegram_app.add_handler(CommandHandler("search", bot_module.search_command))
telegram_app.add_handler(CommandHandler("recent", bot_module.recent_command))
telegram_app.add_handler(CommandHandler("mode", bot_module.mode_command))
telegram_app.add_handler(CommandHandler("done", bot_module.done_command))
telegram_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, bot_module.handle_text)
)
telegram_app.add_handler(MessageHandler(filters.VOICE, bot_module.handle_voice))
telegram_app.add_handler(MessageHandler(filters.PHOTO, bot_module.handle_photo))

# ─────────────────────────────────────────────
# Telegram Webhook Endpoint
# ─────────────────────────────────────────────

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram updates via webhook and feed them to the bot."""
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# Scheduled Bookmarks Scraper
# ─────────────────────────────────────────────

async def send_bookmarks_digest() -> int:
    """Send a Telegram digest of recently ingested bookmarks. Returns number sent."""
    user_chat_id = os.getenv("USER_CHAT_ID", "").strip()
    if not user_chat_id:
        logger.warning("USER_CHAT_ID not set — skipping Telegram digest")
        return 0

    vault_root = Path.home() / ".gbrain_vault" / "markdown" / "sources"
    if not vault_root.exists():
        return 0

    # Find vault files modified in the last 15 minutes
    cutoff = datetime.datetime.now() - datetime.timedelta(minutes=15)
    recent = sorted(
        [p for p in vault_root.rglob("*.md") if p.stat().st_mtime > cutoff.timestamp()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not recent:
        logger.info("No recent vault files for digest")
        return 0

    lines = [f"New bookmarks ({len(recent)}):\n"]
    for fp in recent[:10]:
        try:
            text = fp.read_text()
            parts = text.split("---", 2)
            if len(parts) < 3:
                continue
            fm = parts[1]
            author = ""
            summary = ""
            url = ""
            for line in fm.strip().split("\n"):
                stripped = line.strip()
                if stripped.startswith("author:"):
                    author = stripped.split(":", 1)[1].strip().strip('"')
                elif stripped.startswith("summary:"):
                    summary = stripped.split(":", 1)[1].strip().strip('"')
                elif stripped.startswith("url:"):
                    url = stripped.split(":", 1)[1].strip().strip('"')
            if summary:
                lines.append(f"• {summary}")
                if url:
                    lines.append(f"  {url}")
        except Exception:
            continue

    message = "\n".join(lines)
    if len(message) > 4000:
        message = message[:4000] + "\n..."

    try:
        import httpx
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(api_url, json={
                "chat_id": user_chat_id,
                "text": message,
                "disable_web_page_preview": True,
            })
            if resp.status_code == 200:
                logger.info(f"Telegram digest sent: {len(recent)} bookmarks")
                return len(recent)
            else:
                logger.error(f"Telegram digest failed: {resp.status_code} {resp.text[:200]}")
                return 0
    except Exception as e:
        logger.error(f"Telegram digest error: {e}")
        return 0


async def run_bookmarks_scraper():
    """Run bookmarks scraper every 12 hours."""
    # Wait 60s on startup before first run (let webhook settle)
    await asyncio.sleep(60)

    while True:
        try:
            logger.info("Running scheduled bookmarks scrape...")
            import subprocess

            result = subprocess.run(
                [sys.executable, str(Path(__file__).parent / "x_bookmarks.py")],
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(Path(__file__).parent),
                env={
                    **os.environ,
                    "HOME": str(Path.home()),
                    "SKIP_SERVER_STARTUP": "true",
                    "PORT": os.getenv("PORT", "8000"),
                },
            )
            if result.returncode == 0:
                logger.info(f"Bookmarks scrape OK: {result.stdout[-300:]}")
                # Send Telegram digest of newly ingested bookmarks
                await send_bookmarks_digest()
            else:
                logger.error(f"Bookmarks scrape failed (rc={result.returncode}): {result.stderr[-500:]}")
        except subprocess.TimeoutExpired:
            logger.error("Bookmarks scrape timed out after 5min")
        except Exception as e:
            logger.error(f"Bookmarks scrape error: {e}")

        # Sleep 12 hours between runs
        await asyncio.sleep(12 * 3600)


# ─────────────────────────────────────────────
# Lifespan (startup / shutdown)
# ─────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    """Initialize Telegram bot, set webhook, start background scraper."""
    logger.info("Starting GBrain Railway app...")

    # Initialize and start the bot
    await telegram_app.initialize()
    await telegram_app.start()

    # Set webhook
    if RAILWAY_PUBLIC_DOMAIN:
        webhook_url = f"https://{RAILWAY_PUBLIC_DOMAIN}/telegram-webhook"
        await telegram_app.bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook set: {webhook_url}")
    else:
        logger.warning("RAILWAY_PUBLIC_DOMAIN not set — webhook not configured!")

    # Start background bookmarks scraper
    asyncio.create_task(run_bookmarks_scraper())
    logger.info("Startup complete.")


@app.on_event("shutdown")
async def on_shutdown():
    """Graceful shutdown."""
    logger.info("Shutting down...")
    await telegram_app.stop()
    await telegram_app.shutdown()
    logger.info("Shutdown complete.")


# ─────────────────────────────────────────────
# Health Check
# ─────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "vault_files": len(list(Path.home().glob(".gbrain_vault/markdown/sources/**/*.md"))),
    }


# ─────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
