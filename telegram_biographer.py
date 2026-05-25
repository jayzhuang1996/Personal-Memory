"""
GBrain Companion — Conversational Telegram bot with vault awareness and session archiving.

- Accepts text / voice messages anytime
- First message: searches GBrain vault, responds with connections + follow-up
- Follow-ups: deepens the conversation, draws out thoughts
- After ~5 turns or natural end: summarizes session, stores in vault, responds with insights
- /done: manually end and archive current session
- /search, /recent, /mode: vault browsing
"""

import os
import sys
import datetime
import asyncio
import hashlib
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
MAX_USER_TURNS = 5  # soft cap — LLM can end earlier

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
# Session State
# ─────────────────────────────────────────────

SESSIONS: dict[int, dict] = {}

def _get_session(chat_id: int) -> dict | None:
    s = SESSIONS.get(chat_id)
    if s and s.get("active"):
        return s
    return None

def _create_session(chat_id: int) -> dict:
    s = {
        "active": True,
        "started_at": datetime.datetime.now(),
        "turns": [],
        "topic": "",
    }
    SESSIONS[chat_id] = s
    return s

def _end_session(chat_id: int):
    SESSIONS.pop(chat_id, None)

# ─────────────────────────────────────────────
# Vault Search
# ─────────────────────────────────────────────

def _extract_keywords(text: str, max_keywords: int = 5) -> list[str]:
    words = text.lower().split()
    candidates = [w.strip(".,!?;:()[]{}'\"") for w in words]
    candidates = [w for w in candidates if len(w) > 2 and w not in STOPWORDS and w.isalpha()]
    candidates.sort(key=len, reverse=True)
    seen = set()
    unique = []
    for w in candidates:
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return unique[:max_keywords]


def _search_vault(keywords: list[str]) -> list[Path]:
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
    ranked = sorted(scores.keys(), key=lambda p: (-scores[p], p))
    return [Path(p) for p in ranked[:MAX_VAULT_FILES_TO_READ]]


def _read_vault_files(filepaths: list[Path]) -> list[dict]:
    results = []
    for fp in filepaths:
        try:
            content = fp.read_text()
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
    if not vault_items:
        return "(No relevant items found in the knowledge vault.)"
    parts = []
    for i, item in enumerate(vault_items, 1):
        fm = item["frontmatter"]
        source = fm.get("source", "unknown")
        mode = fm.get("mode", "general")
        summary = fm.get("summary", "")
        topics = fm.get("topics", [])
        topics_str = ", ".join(topics) if isinstance(topics, list) else str(topics)
        url = fm.get("url", "")
        excerpt = item["excerpt"]
        parts.append(
            f"[{i}] ({source}/{mode}) {summary}\n"
            f"    Topics: {topics_str}\n"
            f"    URL: {url}\n"
            f"    Excerpt: {excerpt[:600]}..."
        )
    return "\n\n".join(parts)


# ─────────────────────────────────────────────
# LLM Synthesis
# ─────────────────────────────────────────────

def synthesize_initial_response(user_message: str, vault_items: list[dict]) -> str:
    """First response: vault-aware, includes a follow-up question to deepen the conversation."""
    vault_context = _format_vault_for_prompt(vault_items)

    system = """You are Jay's GBrain Companion — a warm, insightful AI that has access to Jay's personal knowledge vault and conducts thoughtful conversations to draw out his ideas.

Your role on this FIRST message:
1. Respond to Jay's message conversationally. Weave in 1-2 relevant vault items naturally if they connect.
2. Ask ONE specific, open-ended follow-up question that pushes his thinking deeper — about his opinions, experiences, or plans related to what he said.
3. Keep it to 1-2 paragraphs + the question. Don't list things.

Jay's interests: AI/ML engineering, startups, high agency, personal knowledge management, crypto/defi, tools for thought."""

    vault_section = f"\n\nRELEVANT ITEMS FROM JAY'S KNOWLEDGE VAULT:\n{vault_context}" if vault_items else ""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_message + vault_section},
        ],
        max_tokens=400,
        temperature=0.7,
    )
    return response.choices[0].message.content.strip()


def synthesize_followup(turns: list[dict]) -> str:
    """Continue the conversation: respond to latest turn, decide whether to end or ask more."""
    system = """You are Jay's GBrain Companion — a warm, thoughtful conversationalist helping Jay explore his ideas.

The conversation so far is below. Your job:

1. Respond to Jay's last message. Acknowledge what he said. If something connects to earlier parts of the conversation, point that out.
2. Then decide:
   - If Jay seems to be wrapping up (said something conclusive like "that's it", "yeah that covers it", "I think that's all") → respond warmly and include the phrase SESSION_COMPLETE at the very end of your message.
   - If there's still depth to explore → ask ONE specific follow-up question.
   - If this is already a deep thread (4+ exchanges) → gently wrap up and include SESSION_COMPLETE.
3. Keep it concise. 1-2 paragraphs. Natural tone.

Rules:
- Never repeat earlier questions.
- Don't force it — if Jay gave a short or unengaged answer, wrap up.
- If including SESSION_COMPLETE, it must be the LAST thing in your message, on its own line."""

    # Format conversation history
    convo = []
    for t in turns:
        role = "Jay" if t["role"] == "user" else "Companion"
        convo.append(f"{role}: {t['content']}")
    history = "\n\n".join(convo)

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"Conversation so far:\n\n{history}\n\nRespond to Jay's last message."},
        ],
        max_tokens=400,
        temperature=0.7,
    )
    return response.choices[0].message.content.strip()


def synthesize_session_archive(turns: list[dict]) -> dict:
    """Summarize the full conversation: summary, topics, insights, mode. Returns vault-ready dict."""
    convo = []
    for t in turns:
        role = "Jay" if t["role"] == "user" else "Companion"
        convo.append(f"{role}: {t['content']}")
    history = "\n\n".join(convo)

    prompt = f"""Summarize this conversation between Jay and his GBrain Companion. Output ONLY valid JSON, no explanation.

CONVERSATION:
{history}

JSON format:
{{
    "summary": "2-3 sentence summary of what was discussed and what Jay shared",
    "topics": ["topic1", "topic2", "topic3"],
    "insights": ["insight or pattern noticed in Jay's thinking", "another insight"],
    "mode": "operating_philosophy"  // or "operating_system"
}}

Mode rules:
- operating_philosophy: identity, meaning, consciousness, human nature, society, relationships, emotions, personal growth, art, philosophy
- operating_system: technology, business, strategy, engineering, career, tools, startups, finance, code, product

Return ONLY the JSON object."""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=400,
        temperature=0.3,
        response_format={"type": "json_object"},
    )
    try:
        import json
        return json.loads(response.choices[0].message.content)
    except (json.JSONDecodeError, Exception):
        return {
            "summary": "Conversation with GBrain Companion",
            "topics": ["reflection"],
            "insights": [],
            "mode": "operating_philosophy",
        }


# ─────────────────────────────────────────────
# Vault Writer (stores archived sessions)
# ─────────────────────────────────────────────

def _write_session_to_vault(turns: list[dict], archive: dict) -> Path | None:
    """Write conversation session as a .md file in the GBrain vault."""
    try:
        today = datetime.datetime.now()
        date_str = today.strftime("%Y-%m-%d")
        timestamp = today.strftime("%Y-%m-%d_%H-%M-%S")
        unique_id = hashlib.sha256(timestamp.encode()).hexdigest()[:12]

        category = "sources/audio"
        mode = archive.get("mode", "operating_philosophy")
        summary = archive.get("summary", "Conversation with GBrain Companion")
        topics = archive.get("topics", [])
        insights = archive.get("insights", [])

        # Build conversation transcript
        transcript_lines = []
        for t in turns:
            role = "**Jay**" if t["role"] == "user" else "**Companion**"
            transcript_lines.append(f"{role}: {t['content']}")

        transcript = "\n\n".join(transcript_lines)
        insights_md = "\n".join(f"- {ins}" for ins in insights)

        frontmatter = f"""---
id: telegram_{date_str}_{unique_id}
type: source_post
source: telegram
category: {category}
mode: {mode}
date: {date_str}
author: "@jayzhuang"
url: ""
topics:
"""
        for t in topics:
            frontmatter += f"  - {t}\n"

        frontmatter += f"""summary: "{summary}"
status: inbox
captured_via: telegram_companion
---

# {summary}

## Insights
{insights_md}

## Full Transcript

{transcript}
"""

        # Write to vault
        date_path = f"{today.year}/{today.month:02d}"
        folder = VAULT_ROOT.parent / category / date_path
        folder.mkdir(parents=True, exist_ok=True)
        filepath = folder / f"telegram_{date_str}_{unique_id}.md"
        filepath.write_text(frontmatter)

        return filepath
    except Exception as e:
        print(f"[vault write error] {e}")
        return None


# ─────────────────────────────────────────────
# Voice Transcription
# ─────────────────────────────────────────────

def transcribe_sync(path_str: str) -> str:
    with open(path_str, "rb") as audio_file:
        return client.audio.transcriptions.create(
            model="whisper-1", file=audio_file
        ).text


# ─────────────────────────────────────────────
# Core Conversation Handler
# ─────────────────────────────────────────────

async def _process_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Core logic: handle a user message within the session flow."""
    chat_id = update.effective_chat.id

    # Check for /done command within text
    if text.strip().lower() in ("/done", "done", "i'm done", "that's all"):
        return await _archive_session(update, context)

    session = _get_session(chat_id)

    if session is None:
        # ── FIRST MESSAGE: new session ──
        session = _create_session(chat_id)

        # Vault search for context
        keywords = _extract_keywords(text)
        matched_files = _search_vault(keywords) if keywords else []
        vault_items = _read_vault_files(matched_files) if matched_files else []

        # Generate vault-aware response with follow-up question
        response = await asyncio.to_thread(synthesize_initial_response, text, vault_items)

        # Record turns
        session["turns"].append({"role": "user", "content": text})
        session["turns"].append({"role": "assistant", "content": response})
        session["topic"] = text[:100]

        await update.message.reply_text(response)

    else:
        # ── CONTINUING SESSION ──
        session["turns"].append({"role": "user", "content": text})
        user_turn_count = sum(1 for t in session["turns"] if t["role"] == "user")

        # Force archive if max turns reached
        if user_turn_count >= MAX_USER_TURNS:
            await update.message.reply_text("I've really enjoyed this conversation. Let me save what we discussed...")
            return await _archive_session(update, context)

        # Get follow-up or completion signal
        response = await asyncio.to_thread(synthesize_followup, session["turns"])

        if "SESSION_COMPLETE" in response:
            # Strip the signal from the displayed message
            clean_response = response.replace("SESSION_COMPLETE", "").strip()
            session["turns"].append({"role": "assistant", "content": clean_response})
            await update.message.reply_text(clean_response)
            await update.message.reply_text("Archiving this conversation to your knowledge vault...")
            return await _archive_session(update, context)
        else:
            session["turns"].append({"role": "assistant", "content": response})
            await update.message.reply_text(response)


async def _archive_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Summarize session, write to vault, respond with insights."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)

    if not session or len(session["turns"]) < 2:
        _end_session(chat_id)
        await update.message.reply_text("No active conversation to archive.")
        return

    # Generate archive
    archive = await asyncio.to_thread(synthesize_session_archive, session["turns"])
    filepath = await asyncio.to_thread(_write_session_to_vault, session["turns"], archive)

    _end_session(chat_id)

    # Build response
    summary = archive.get("summary", "Conversation archived.")
    insights = archive.get("insights", [])
    mode = archive.get("mode", "operating_philosophy")
    topics = archive.get("topics", [])

    lines = [
        "🧠 *Session archived to your knowledge vault.*\n",
        f"**Summary:** {summary}",
        f"**Mode:** {mode}",
        f"**Topics:** {', '.join(topics) if topics else 'reflection'}",
    ]
    if insights:
        lines.append(f"\n**Insights:**")
        for ins in insights:
            lines.append(f"  • {ins}")

    if filepath:
        lines.append(f"\n_Stored at: {filepath.relative_to(Path.home())}_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─────────────────────────────────────────────
# Telegram Handlers
# ─────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hey Jay — I'm your GBrain Companion.\n\n"
        "Send me a message about what you're thinking, working on, or curious about. "
        "I'll search your knowledge vault and help you explore ideas. "
        "After a few exchanges, I'll summarize our conversation and save it to your vault.\n\n"
        "Commands:\n"
        "/search <query> — deep search your vault\n"
        "/recent — see recent bookmarks\n"
        "/mode <philosophy|system> — browse vault by mode\n"
        "/done — end and archive the current conversation\n\n"
        "You can also send voice notes — I'll transcribe and respond."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_message = update.message.text.strip()
    if not user_message:
        return
    await _process_message(update, context, user_message)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    await _process_message(update, context, transcript)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

        # Process the vision description as a new message (starts a session)
        await _process_message(update, context, f"[Photo shared: {description}]")

    except Exception as e:
        await update.message.reply_text(f"Photo analysis failed: {e}")


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually end and archive the current session."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)
    if not session:
        await update.message.reply_text("No active conversation to archive. Start one by sending me a message.")
        return
    await _archive_session(update, context)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("Usage: /search <query>")
        return

    keywords = _extract_keywords(query, max_keywords=8)
    matched_files = _search_vault(keywords) if keywords else []
    vault_items = _read_vault_files(matched_files) if matched_files else []

    if not vault_items:
        await update.message.reply_text(f"No vault matches for: {query}")
        return

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

    await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)


async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("GBrain Companion started.")
    print(f"  Vault: {VAULT_ROOT} ({len(list(VAULT_ROOT.rglob('*.md')))} files)")
    print("  Session mode: conversational with auto-archive")
    print(f"  Max turns per session: {MAX_USER_TURNS}")

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
