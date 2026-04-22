import os
import sys
import datetime
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters
from openai import OpenAI

# Ensure archivist can be imported from same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import archivist

# Load environment variables
load_dotenv(".env")
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
USER_CHAT_ID = os.getenv('USER_CHAT_ID', '').strip()

# Init Synchronous OpenAI Client
openai_key = os.getenv('OPENAI_API_KEY', '').strip()
openai_client = OpenAI(api_key=openai_key)

# Directories for deep storage
AUDIO_DIR = Path("raw_audio")
IMAGE_DIR = Path("raw_images")
TRANSCRIPT_DIR = Path("transcripts/raw")
AUDIO_DIR.mkdir(exist_ok=True)
IMAGE_DIR.mkdir(exist_ok=True)
TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────
# SESSION STATE MACHINE
# Holds the entire day's conversation in memory.
# Resets each time a new daily interview begins.
# ─────────────────────────────────────────────
SESSION = {
    "active": False,
    "date": None,           # today's date string (YYYY-MM-DD)
    "turns": [],            # list of {"role": ..., "content": ...}
    "opening_question": "", # the question that started this session
}

MAX_TURNS = 6  # Max total assistant+user turns before forcing completion

def reset_session():
    SESSION["active"] = False
    SESSION["date"] = None
    SESSION["turns"] = []
    SESSION["opening_question"] = ""

# ─────────────────────────────────────────────
# SYNC HELPERS
# ─────────────────────────────────────────────

def transcribe_sync(path_str: str) -> str:
    """Robust synchronous wrapper for OpenAI Whisper."""
    with open(path_str, "rb") as audio_file:
        return openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file
        ).text

def vision_sync(path_str: str) -> str:
    """Robust synchronous wrapper for OpenAI Vision."""
    import base64
    with open(path_str, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image in detail. If it's a whiteboard or text, transcribe it. If it's a memory, describe the scene. We are adding this to a personal life documentary."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded_string}"}}
                ],
            }
        ]
    )
    return response.choices[0].message.content

# ─────────────────────────────────────────────
# DYNAMIC QUESTION GENERATOR
# Reads the actual memory bank to find knowledge gaps.
# ─────────────────────────────────────────────

def generate_dynamic_question_sync() -> str:
    """Read existing memory bank and use LLM to generate a targeted, non-repetitive question."""
    # Collect a sample of existing memory bank content
    memory_bank = Path("memory_bank")
    memory_snippets = []
    
    if memory_bank.exists():
        for md_file in list(memory_bank.rglob("*.md"))[:10]:  # Read up to 10 nodes
            try:
                content = md_file.read_text()[:500]  # First 500 chars per file
                memory_snippets.append(f"### {md_file.name}\n{content}")
            except Exception:
                pass

    # Also read LIFE_BUCKETS for taxonomy reference
    try:
        buckets = Path("LIFE_BUCKETS.md").read_text()
    except Exception:
        buckets = ""

    context = "\n\n".join(memory_snippets) if memory_snippets else "No memory bank entries yet. Start from scratch."

    prompt = f"""You are Jay's personal AI biographer conducting a daily life documentary interview.

# EXISTING MEMORY BANK (what you already know):
{context}

# LIFE DOMAINS TO COVER (your taxonomy):
{buckets}

# YOUR TASK:
Generate ONE highly specific, emotionally rich, open-ended interview question for tonight.
- Do NOT ask about topics already well-covered in the memory bank above.
- Focus on domains that have sparse or zero coverage.
- Ask about specific events, people, feelings, or turning points — not generic topics.
- Be warm and human, not robotic.
- Output ONLY the question itself. No prefix, no explanation."""

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150
    )
    return response.choices[0].message.content.strip()

# ─────────────────────────────────────────────
# FOLLOW-UP ENGINE
# Given the conversation so far, either asks a follow-up
# or outputs STATUS: COMPLETE to end the session.
# ─────────────────────────────────────────────

def get_followup_or_complete_sync(turns: list) -> str:
    """Call LLM to decide whether to ask a follow-up or end the session."""
    system = """You are Jay's personal AI biographer conducting a structured life documentary interview.

Your job:
1. Read the conversation so far.
2. If the user's last response was substantive and there is a natural follow-up question, ask it. Keep it short and specific.
3. If you have collected enough depth (3+ solid responses, or the topic is exhausted), output exactly: STATUS: COMPLETE

Rules:
- Ask at most 2-3 follow-up questions per session before completing.
- Never be repetitive.
- Be warm and conversational.
- If outputting STATUS: COMPLETE, output ONLY those words, nothing else."""

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": system}] + turns,
        max_tokens=200
    )
    return response.choices[0].message.content.strip()

# ─────────────────────────────────────────────
# SESSION FINALIZATION
# Consolidates all turns into ONE daily transcript.
# ─────────────────────────────────────────────

async def finalize_session(context: ContextTypes.DEFAULT_TYPE):
    """Consolidate the full session turns into one transcript and run Archivist."""
    today = SESSION["date"] or datetime.datetime.now().strftime("%Y-%m-%d")
    
    # Build consolidated transcript
    lines = [f"# Daily Interview: {today}\n", f"**Opening Question:** {SESSION['opening_question']}\n"]
    for turn in SESSION["turns"]:
        role = "🤖 Biographer" if turn["role"] == "assistant" else "🎙️ Jay"
        lines.append(f"\n**{role}:** {turn['content']}")
    
    consolidated = "\n".join(lines)
    
    # Write to transcripts/raw/ as a single daily file
    daily_path = TRANSCRIPT_DIR / f"daily_{today}.md"
    with open(daily_path, "w") as f:
        f.write(consolidated)
    
    print(f"📄 Consolidated daily transcript saved: {daily_path}")
    
    # Reset session before archiving
    reset_session()
    
    await context.bot.send_message(
        chat_id=USER_CHAT_ID,
        text="✅ *Interview complete!* Archiving your memories now...",
        parse_mode="Markdown"
    )
    
    # Run archivist on the single daily transcript
    await asyncio.to_thread(archivist.main)
    
    await context.bot.send_message(
        chat_id=USER_CHAT_ID,
        text="🧠 *Memory Bank updated!* Your reflections have been archived. See you tomorrow night.",
        parse_mode="Markdown"
    )

# ─────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming voice messages with full session conversation management."""
    voice = update.message.voice
    if not voice:
        return
    
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    
    # Download and archive raw audio
    await update.message.reply_text("🎙️ Got it! Transcribing...")
    voice_file = await context.bot.get_file(voice.file_id)
    audio_path = AUDIO_DIR / f"voice_{timestamp}.ogg"
    await voice_file.download_to_drive(str(audio_path))
    
    # Transcribe
    try:
        transcript_text = await asyncio.to_thread(transcribe_sync, str(audio_path))
    except Exception as e:
        import traceback
        await update.message.reply_text(f"❌ Transcription failed:\n```\n{traceback.format_exc()[:2000]}\n```")
        return
    
    # Add user turn to session
    SESSION["turns"].append({"role": "user", "content": transcript_text})
    
    # Echo the transcription back
    await update.message.reply_text(f"_\"{transcript_text}\"_", parse_mode="Markdown")
    
    # Check if we should end or continue
    user_turn_count = sum(1 for t in SESSION["turns"] if t["role"] == "user")
    
    if user_turn_count >= (MAX_TURNS // 2):
        # Force completion after max turns
        await finalize_session(context)
        return
    
    # Ask the LLM for either a follow-up or STATUS: COMPLETE
    response = await asyncio.to_thread(get_followup_or_complete_sync, SESSION["turns"])
    
    if "STATUS: COMPLETE" in response:
        await finalize_session(context)
    else:
        # Add assistant follow-up to session
        SESSION["turns"].append({"role": "assistant", "content": response})
        await update.message.reply_text(
            f"🤖 {response}\n\n_🎤 (Reply with a Voice Note)_",
            parse_mode="Markdown"
        )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming photos — extract vision analysis and add to session."""
    photo = update.message.photo[-1]  # Highest resolution
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    
    await update.message.reply_text("📸 Photo received! Analyzing...")
    
    photo_file = await context.bot.get_file(photo.file_id)
    photo_path = IMAGE_DIR / f"image_{timestamp}.jpg"
    await photo_file.download_to_drive(str(photo_path))
    
    # Run OpenAI Vision
    try:
        vision_text = await asyncio.to_thread(vision_sync, str(photo_path))
        
        # Add image analysis as a user turn in the session
        SESSION["turns"].append({"role": "user", "content": f"[Image shared] {vision_text}"})
        
        await update.message.reply_text(
            f"✅ Image analyzed!\n\n_{vision_text}_\n\n⌛ Waking up Archivist...",
            parse_mode="Markdown"
        )
        
        # Images immediately trigger archiving (no back-and-forth needed)
        await asyncio.to_thread(archivist.main)
        
    except Exception as e:
        import traceback
        err_msg = traceback.format_exc()
        await update.message.reply_text(f"❌ Vision Analysis failed:\n```\n{err_msg[:2000]}\n```", parse_mode="MarkdownV2")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually start an interview session."""
    reset_session()
    SESSION["active"] = True
    SESSION["date"] = datetime.datetime.now().strftime("%Y-%m-%d")
    
    await update.message.reply_text("🤖 *Biographer Systems Online.*\n\nGenerating your interview question...", parse_mode="Markdown")
    
    question = await asyncio.to_thread(generate_dynamic_question_sync)
    SESSION["opening_question"] = question
    SESSION["turns"].append({"role": "assistant", "content": question})
    
    await update.message.reply_text(
        f"🕰️ *Daily Biographer Sync*\n\n{question}\n\n_🎤 (Reply with a Voice Note)_",
        parse_mode="Markdown"
    )

async def send_daily_interview(context: ContextTypes.DEFAULT_TYPE):
    """Fired daily at 11PM EST by the Application JobQueue."""
    reset_session()
    SESSION["active"] = True
    SESSION["date"] = datetime.datetime.now().strftime("%Y-%m-%d")
    
    question = await asyncio.to_thread(generate_dynamic_question_sync)
    SESSION["opening_question"] = question
    SESSION["turns"].append({"role": "assistant", "content": question})
    
    msg = f"🕰️ <b>11:00 PM - Daily Biographer Sync</b>\n\n{question}\n\n<i>🎤 (Reply to this message with a Voice Note)</i>"
    await context.bot.send_message(chat_id=USER_CHAT_ID, text=msg, parse_mode='HTML')

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    # Schedule the 11 PM EST daily job
    if USER_CHAT_ID:
        import pytz
        est = pytz.timezone('US/Eastern')
        target_time = datetime.time(hour=23, minute=0, tzinfo=est)
        application.job_queue.run_daily(send_daily_interview, target_time)
    
    print("🤖 Telegram Biographer Backend Started.")
    print("  - Conversational Session Engine Active")
    print("  - 11 PM EST Scheduler Active")
    
    # Use webhook on Railway, polling locally
    railway_domain = os.getenv('RAILWAY_PUBLIC_DOMAIN', '').strip()
    if railway_domain:
        webhook_url = f"https://{railway_domain}/{TELEGRAM_BOT_TOKEN}"
        port = int(os.getenv('PORT', 8443))
        print(f"  - Webhook mode: {webhook_url}")
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TELEGRAM_BOT_TOKEN,
            webhook_url=webhook_url,
            drop_pending_updates=True
        )
    else:
        print("  - Polling mode (local dev)")
        application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
