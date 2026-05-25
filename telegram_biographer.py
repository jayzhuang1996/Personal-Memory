"""
GBrain Companion — Conversational Telegram bot with vault awareness.

Replaces the old daily-interview biographer with an on-demand bot that:
- Accepts text / voice messages anytime
- Searches the GBrain vault for relevant bookmarks and notes
- Synthesizes connections between vault content and user's current thinking
- Supports /search, /recent, /mode commands
"""

import os
import sys
import datetime
import asyncio
import subprocess
from pathlib import Path
from collections import Counter

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters
from openai import OpenAI

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

load_dotenv(".env")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
USER_CHAT_ID = os.getenv("USER_CHAT_ID", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

client = OpenAI(api_key=OPENAI_API_KEY)

VAULT_ROOT = Path.home() / ".gbrain_vault" / "markdown" / "sources"
AUDIO_DIR = Path("raw_audio")
IMAGE_DIR = Path("raw_images")
AUDIO_DIR.mkdir(exist_ok=True)
IMAGE_DIR.mkdir(exist_ok=True)

MAX_VAULT_FILES_TO_READ = 5
MAX_VAULT_CHARS_PER_FILE = 1500

STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "under", "again",
    "further", "then", "once", "here", "there", "when", "where", "why",
    "how", "all", "both", "each", "few", "more", "most", "other", "some",
    "such", "only", "own", "same", "so", "than", "too", "very", "just",
    "that", "this", "i", "me", "my", "we", "our", "you", "your", "he",
    "she", "it", "its", "they", "them", "their", "what", "which", "who",
    "about", "also", "not", "but", "and", "or", "if", "because", "until",
    "while", "these", "those", "thing", "things", "really", "still",
    "maybe", "probably", "actually", "always", "never", "something",
    "anything", "nothing", "everything", "lot", "lots", "kind", "kinda",
    "sort", "like", "know", "think", "feel", "feeling", "thought",
    "going", "gonna", "want", "wanted", "need", "needed", "get", "got",
    "make", "made", "see", "saw", "come", "came", "take", "took",
    "way", "day", "time", "people", "year", "work", "good", "bad",
    "new", "old", "big", "small", "right", "left", "many", "much",
    "well", "even", "back", "around", "any", "ever", "yet", "already",
    "yeah", "yes", "no", "oh", "hey", "hi", "hello", "ok", "okay",
}

# ─────────────────────────────────────────────
# Vault Search
# ─────────────────────────────────────────────

def _extract_keywords(text: str, max_keywords: int = 5) -> list[str]:
    """Extract meaningful keywords from user text, filtered by stopwords."""
    words = text.lower().split()
    # Filter: >2 chars, not a stopword, alphabetic
    candidates = [w.strip(".,!?;:()[]{}'\"") for w in words]
    candidates = [w for w in candidates if len(w) > 2 and w not in STOPWORDS and w.isalpha()]
    # Prefer longer words (more specific)
    candidates.sort(key=len, reverse=True)
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for w in candidates:
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return unique[:max_keywords]


def _search_vault(keywords: list[str]) -> list[Path]:
    """Search vault .md files for matching keywords. Returns ranked file paths."""
    if not VAULT_ROOT.exists():
        return []

    all_md_files = list(VAULT_ROOT.rglob("*.md"))
    if not all_md_files:
        return []

    scores: Counter = Counter()
    for kw in keywords:
        try:
            result = subprocess.run(
                ["grep", "-ril", kw, str(VAULT_ROOT)],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split("\n"):
                if line:
                    scores[line] += 1
        except (subprocess.TimeoutExpired, Exception):
            continue

    # Sort by match count (desc), then by path
    ranked = sorted(scores.keys(), key=lambda p: (-scores[p], p))
    return [Path(p) for p in ranked[:MAX_VAULT_FILES_TO_READ]]


def _read_vault_files(filepaths: list[Path]) -> list[dict]:
    """Read frontmatter + body excerpt from vault files."""
    results = []
    for fp in filepaths:
        try:
            content = fp.read_text()
            # Split frontmatter and body
            parts = content.split("---", 2)
            frontmatter = {}
            body = ""
            if len(parts) >= 3:
                frontmatter = _parse_frontmatter(parts[1])
                body = parts[2].strip()
            excerpt = body[:MAX_VAULT_CHARS_PER_FILE] if body else ""
            results.append({
                "file": str(fp.relative_to(VAULT_ROOT.parent)),
                "frontmatter": frontmatter,
                "excerpt": excerpt,
            })
        except Exception:
            continue
    return results


def _parse_frontmatter(text: str) -> dict:
    """Parse YAML frontmatter into a simple dict (no pyyaml dependency)."""
    fm = {}
    in_topics = False
    topics = []
    for line in text.strip().split("\n"):
        stripped = line.strip()
        if stripped.startswith("topics:"):
            in_topics = True
            continue
        if in_topics:
            if stripped.startswith("- "):
                topics.append(stripped[2:])
            elif stripped and not stripped.startswith("  "):
                in_topics = False
        if ":" in line and not stripped.startswith("- "):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"')
            if key and val:
                fm[key] = val
    if topics:
        fm["topics"] = topics
    return fm


def _format_vault_for_prompt(vault_items: list[dict]) -> str:
    """Format vault items for the LLM synthesis prompt."""
    if not vault_items:
        return "(No relevant items found in the knowledge vault.)"

    parts = []
    for i, item in enumerate(vault_items, 1):
        fm = item["frontmatter"]
        source = fm.get("source", "unknown")
        mode = fm.get("mode", "general")
        summary = fm.get("summary", "")
        topics = fm.get("topics", [])
        if isinstance(topics, list):
            topics_str = ", ".join(topics)
        else:
            topics_str = str(topics)
        author = fm.get("author", "")
        url = fm.get("url", "")
        excerpt = item["excerpt"]

        parts.append(
            f"[{i}] ({source}/{mode}) {summary}\n"
            f"    Topics: {topics_str}\n"
            f"    Author: {author} | URL: {url}\n"
            f"    Excerpt: {excerpt[:600]}..."
        )
    return "\n\n".join(parts)


# ─────────────────────────────────────────────
# LLM Synthesis
# ─────────────────────────────────────────────

def synthesize_response(user_message: str, vault_items: list[dict]) -> str:
    """Generate a conversational response using vault context."""
    vault_context = _format_vault_for_prompt(vault_items)

    system = """You are Jay's GBrain Companion — a warm, insightful AI that has access to Jay's personal knowledge vault. The vault contains his bookmarked articles, tweets, papers, and saved content organized by mode (operating_philosophy for human condition / society / meaning; operating_system for tech / business / practical tools).

Your role:
1. Respond conversationally to Jay's message. Be warm and direct — like a thoughtful friend who knows his interests.
2. If the vault contains relevant items, naturally weave 1-2 of them into your response. Point out connections, patterns, or related ideas he might find interesting.
3. If nothing in the vault is relevant, just have a normal conversation. Don't force vault references.
4. Keep responses concise (1-3 paragraphs). Don't list things unless asked.
5. Never prefix responses with labels or markdown headings. Just talk.

Jay's interests (from his vault): AI/ML engineering, startups, high agency, personal knowledge management, crypto/defi, content creation, tools for thought."""

    vault_section = f"\n\nRELEVANT ITEMS FROM JAY'S KNOWLEDGE VAULT:\n{vault_context}" if vault_items else ""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_message + vault_section},
        ],
        max_tokens=500,
        temperature=0.7,
    )
    return response.choices[0].message.content.strip()


# ─────────────────────────────────────────────
# Voice Transcription
# ─────────────────────────────────────────────

def transcribe_sync(path_str: str) -> str:
    with open(path_str, "rb") as audio_file:
        return client.audio.transcriptions.create(
            model="whisper-1", file=audio_file
        ).text


# ─────────────────────────────────────────────
# Telegram Handlers
# ─────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hey Jay — I'm your GBrain Companion.\n\n"
        "Send me a message about what you're thinking, working on, or curious about. "
        "I'll search your knowledge vault and connect dots across your bookmarks, "
        "articles, and saved content.\n\n"
        "Commands:\n"
        "/search <query> — deep search your vault\n"
        "/recent — see recent bookmarks\n"
        "/mode <philosophy|system> — browse vault by mode\n\n"
        "You can also send voice notes — I'll transcribe and respond."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main conversational handler for text messages."""
    user_message = update.message.text.strip()
    if not user_message:
        return

    # Extract keywords and search vault
    keywords = _extract_keywords(user_message)
    matched_files = _search_vault(keywords) if keywords else []
    vault_items = _read_vault_files(matched_files) if matched_files else []

    # Synthesize response
    response = await asyncio.to_thread(synthesize_response, user_message, vault_items)

    await update.message.reply_text(response)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Transcribe voice note, then process as text."""
    voice = update.message.voice
    if not voice:
        return

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    await update.message.reply_text("Transcribing...")

    voice_file = await context.bot.get_file(voice.file_id)
    audio_path = AUDIO_DIR / f"voice_{timestamp}.ogg"
    await voice_file.download_to_drive(str(audio_path))

    try:
        transcript = await asyncio.to_thread(transcribe_sync, str(audio_path))
    except Exception as e:
        await update.message.reply_text(f"Transcription failed: {e}")
        return

    await update.message.reply_text(f'"{transcript}"')

    # Process the transcribed text through the same pipeline
    keywords = _extract_keywords(transcript)
    matched_files = _search_vault(keywords) if keywords else []
    vault_items = _read_vault_files(matched_files) if matched_files else []

    response = await asyncio.to_thread(synthesize_response, transcript, vault_items)
    await update.message.reply_text(response)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Analyze photo with vision, then respond conversationally."""
    import base64

    photo = update.message.photo[-1]
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    await update.message.reply_text("Analyzing photo...")

    photo_file = await context.bot.get_file(photo.file_id)
    photo_path = IMAGE_DIR / f"image_{timestamp}.jpg"
    await photo_file.download_to_drive(str(photo_path))

    try:
        with open(photo_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")

        vision_resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image in detail. If it has text, transcribe it. Be thorough but concise."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
                ],
            }],
            max_tokens=300,
        )
        description = vision_resp.choices[0].message.content.strip()

        # Search vault for connections to the image content
        keywords = _extract_keywords(description)
        matched_files = _search_vault(keywords) if keywords else []
        vault_items = _read_vault_files(matched_files) if matched_files else []

        synthesis = await asyncio.to_thread(
            synthesize_response,
            f"[User shared a photo. Vision description:] {description}",
            vault_items,
        )
        await update.message.reply_text(f"{description}\n\n{synthesis}")

    except Exception as e:
        await update.message.reply_text(f"Photo analysis failed: {e}")


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explicit /search <query> — deep vault search."""
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("Usage: /search <query>\nExample: /search crypto trading bots")
        return

    keywords = _extract_keywords(query, max_keywords=8)
    matched_files = _search_vault(keywords) if keywords else []
    vault_items = _read_vault_files(matched_files) if matched_files else []

    if not vault_items:
        await update.message.reply_text(f"No vault matches for: {query}")
        return

    # For /search, provide a more detailed listing
    lines = [f"Vault matches for *{query}*:\n"]
    for i, item in enumerate(vault_items, 1):
        fm = item["frontmatter"]
        summary = fm.get("summary", "No summary")
        source = fm.get("source", "?")
        mode = fm.get("mode", "?")
        date = fm.get("date", "?")
        url = fm.get("url", "")
        lines.append(f"{i}. [{source}/{mode}] {date}: {summary}")
        if url:
            lines.append(f"   {url}")

    response = "\n".join(lines)
    await update.message.reply_text(response, disable_web_page_preview=True)


async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show 10 most recent vault entries."""
    all_files = sorted(
        VAULT_ROOT.rglob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:10]

    if not all_files:
        await update.message.reply_text("No vault entries found.")
        return

    lines = ["*Recent vault entries:*\n"]
    for fp in all_files:
        try:
            content = fp.read_text()
            parts = content.split("---", 2)
            if len(parts) >= 3:
                fm = _parse_frontmatter(parts[1])
                summary = fm.get("summary", "No summary")
                source = fm.get("source", "?")
                mode = fm.get("mode", "?")
                date = fm.get("date", "?")
                lines.append(f"- [{source}/{mode}] {date}: {summary}")
        except Exception:
            continue

    await update.message.reply_text("\n".join(lines))


async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Browse vault by mode: /mode philosophy or /mode system."""
    mode_arg = " ".join(context.args).lower() if context.args else ""
    if "philosophy" in mode_arg:
        target = "operating_philosophy"
        label = "Operating Philosophy"
    elif "system" in mode_arg:
        target = "operating_system"
        label = "Operating System"
    else:
        await update.message.reply_text(
            "Usage: /mode <philosophy|system>\n"
            "- operating_philosophy: human condition, society, meaning, art, philosophy\n"
            "- operating_system: tech, business, tools, engineering, startups"
        )
        return

    matched = []
    for fp in VAULT_ROOT.rglob("*.md"):
        try:
            content = fp.read_text()
            if f"mode: {target}" in content:
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    fm = _parse_frontmatter(parts[1])
                    matched.append((fp, fm))
        except Exception:
            continue

    if not matched:
        await update.message.reply_text(f"No entries found for mode: {label}")
        return

    matched.sort(key=lambda x: x[1].get("date", ""), reverse=True)
    lines = [f"*{label}* ({len(matched)} entries):\n"]
    for fp, fm in matched[:15]:
        summary = fm.get("summary", "No summary")
        date = fm.get("date", "?")
        source = fm.get("source", "?")
        lines.append(f"- [{source}] {date}: {summary}")

    if len(matched) > 15:
        lines.append(f"\n... and {len(matched) - 15} more.")

    await update.message.reply_text("\n".join(lines))


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("recent", recent_command))
    app.add_handler(CommandHandler("mode", mode_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("GBrain Companion started.")
    print(f"  Vault: {VAULT_ROOT} ({len(list(VAULT_ROOT.rglob('*.md')))} files indexed)")
    print("  Mode: conversational (on-demand)")

    # Polling locally; webhook on Railway
    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if railway_domain:
        webhook_url = f"https://{railway_domain}/{TELEGRAM_BOT_TOKEN}"
        port = int(os.getenv("PORT", "8443"))
        print(f"  Webhook mode: {webhook_url}")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TELEGRAM_BOT_TOKEN,
            webhook_url=webhook_url,
            drop_pending_updates=True,
        )
    else:
        print("  Polling mode (local dev)")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
