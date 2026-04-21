import os
import datetime
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters
from openai import OpenAI

# Load environment variables
load_dotenv(".env")
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
USER_CHAT_ID = os.getenv('USER_CHAT_ID')  # Needed to proactively send messages at 11 PM

# Init Synchronous OpenAI Client 
openai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Directories for deep storage
AUDIO_DIR = Path("raw_audio")
IMAGE_DIR = Path("raw_images")
TRANSCRIPT_DIR = Path("transcripts/raw")
AUDIO_DIR.mkdir(exist_ok=True)
IMAGE_DIR.mkdir(exist_ok=True)
TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)

def transcribe_sync(path_str: str) -> str:
    """Robust synchronous wrapper for OpenAI API to prevent threading loops"""
    with open(path_str, "rb") as audio_file:
        return openai_client.audio.transcriptions.create(
            model="whisper-1", 
            file=audio_file
        ).text

def vision_sync(path_str: str) -> str:
    """Robust synchronous wrapper for OpenAI Vision"""
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

async def build_system_prompt():
    """
    CRITICAL STEP: We read the local Markdown instruction files from disk EVERY time.
    If we don't do this, the LLM deployed to Railway will 'forget' your custom rules.
    """
    with open("BRAIN_SCHEMA.md", "r") as f:
        schema = f.read()
        
    with open("LIFE_BUCKETS.md", "r") as f:
        buckets = f.read()

    return f"""
    You are Jay's proactive Autobiographer. You are following an aggressive, back-and-forth interview format.
    
    # YOUR DIRECTIVES:
    {schema}
    
    # YOUR DOMAINS (WHAT TO ASK ABOUT):
    {buckets}
    
    # CONVERSATION RULES:
    1. If the user's answer is shallow, push back and ask ONE deep follow-up question.
    2. NEVER ask more than 3 follow-up questions total. 
    3. Once you successfully extract the emotional or factual root of the data, you MUST append "STATUS: COMPLETE" to your final message. This tells the Python script that the interview is over and it should compile the markdown files.
    """

async def generate_dynamic_question():
    """
    Reads the existing Memory Bank (index.md) to find gaps in the timeline or un-explored projects.
    Passes this context to an LLM to generate a highly specific, non-repetitive prompt.
    (Stubbed here, will map to Claude/OpenAI API)
    """
    return "Jay, looking at your projects, I see you built the FreightIQ MVP but there is no context on your co-founders or team dynamics. What was the most challenging interpersonal moment when building that?"

async def send_daily_interview(context: ContextTypes.DEFAULT_TYPE):
    """Fired daily at 11PM EST by the Application JobQueue"""
    question = await generate_dynamic_question()
    
    msg = f"🕰️ <b>11:00 PM - Daily Biographer Sync</b>\n\n{question}\n\n<i>🎤 (Reply to this message with a Voice Note)</i>"
    await context.bot.send_message(chat_id=USER_CHAT_ID, text=msg, parse_mode='HTML')

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming voice messages, stores the raw audio, and transcribes it"""
    voice = update.message.voice
    if not voice:
        return
        
    await update.message.reply_text("📥 Downloading secure voice archive...")
    
    # 1. Download and Keep the Raw Audio (.ogg)
    file = await context.bot.get_file(voice.file_id)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    audio_path = AUDIO_DIR / f"voice_{timestamp}.ogg"
    
    await file.download_to_drive(audio_path)
    
    await update.message.reply_text(f"✅ Raw audio archived to `raw_audio/voice_{timestamp}.ogg`. Processing transcription...")
    
    # 2. Transcribe (Whisper API directly on a clean worker thread)
    try:
        transcript_text = await asyncio.to_thread(transcribe_sync, str(audio_path))
        
        # Save transcript
        transcript_path = TRANSCRIPT_DIR / f"transcript_{timestamp}.md"
        with open(transcript_path, "w") as f:
            f.write(f"# Voice Note: {timestamp}\n\n{transcript_text}\n")
            
        await update.message.reply_text(f"✅ Text conversion complete.\n\n_{transcript_text}_", parse_mode="Markdown")
    except Exception as e:
        import traceback
        err_msg = traceback.format_exc()
        await update.message.reply_text(f"❌ Transcription failed:\n```\n{err_msg[:3000]}\n```", parse_mode="MarkdownV2")
        return
        
    # 3. Send transcript to the LLM along with the Conversation History
    # system_prompt = await build_system_prompt()
    
    # 4. Check LLM Output for the stopping condition
    # llm_response = call_llm(system_prompt, user_transcript_text)
    #
    # if "STATUS: COMPLETE" in llm_response:
    #     await update.message.reply_text("✅ Interview Complete. Compiling into Memory Bank...")
    #     # Trigger the Archivist Agent to parse the transcript into memory_bank/
    # else:
    #     await update.message.reply_text(f"🤖 {llm_response}")
    #     # Wait for the next voice note from the user

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming photos, downloads them, and runs OpenAI Vision description"""
    photo = update.message.photo[-1] # Get the highest resolution photo
    
    await update.message.reply_text("📸 Image received. Archiving to disk and generating Vision context...")
    
    file = await context.bot.get_file(photo.file_id)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    photo_path = IMAGE_DIR / f"image_{timestamp}.jpg"
    
    await file.download_to_drive(photo_path)
    
    # Run OpenAI Vision
    try:
        vision_text = await asyncio.to_thread(vision_sync, str(photo_path))
        
        # Save transcript
        transcript_path = TRANSCRIPT_DIR / f"image_{timestamp}.md"
        with open(transcript_path, "w") as f:
            f.write(f"# Image Capture: {timestamp}\n\n**Source File:** `raw_images/image_{timestamp}.jpg`\n\n**Vision Analysis:**\n{vision_text}\n")
            
        await update.message.reply_text(f"✅ Image securely saved. Vision Output:\n\n_{vision_text}_", parse_mode="Markdown")
    except Exception as e:
        import traceback
        err_msg = traceback.format_exc()
        await update.message.reply_text(f"❌ Vision Analysis failed:\n```\n{err_msg[:3000]}\n```", parse_mode="MarkdownV2")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    msg = "✅ Biographer Systems Online.\n\nI am listening. Send me a voice note right now or wait for the 11 PM prompt."
    await update.message.reply_text(msg)

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN missing in .env")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Handle incoming commands, voice notes, and photos
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    # Schedule the 11 PM EST daily job (UTC -5)
    # In production, timezone should be explicitly passed to job_queue
    if USER_CHAT_ID:
        import datetime
        import pytz
        est = pytz.timezone('US/Eastern')
        target_time = datetime.time(hour=23, minute=0, tzinfo=est)
        application.job_queue.run_daily(send_daily_interview, target_time)

    print("🤖 Telegram Biographer Backend Started.")
    print("  - Listening for Voice Notes")
    print("  - 11 PM EST Scheduler Active")
    
    application.run_polling()

if __name__ == '__main__':
    main()
