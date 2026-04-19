# Incremental Setup Plan: Personal Autobiographer & Memory Bank

**Rule:** Execute one phase at a time. Do not skip ahead to prevent hallucination. Each task must be verified working locally before proceeding.

## Phase 1: Environment & Schema Lockdown [✅ COMPLETE]
- [x] Establish the 3-Layer concept (`Raw Sources`, `Wiki`, `Schema`).
- [x] Create `BRAIN_SCHEMA.md` (Wiki internal layout and file conventions).
- [x] Create `LIFE_BUCKETS.md` (Taxonomy of autobiographical data targets).
- [x] Create core directories (`memory_bank/`, `raw_audio/`, `transcripts/raw/`).
- [x] Initialize Python Virtual Environment, install base dependencies (`python-telegram-bot`, `python-dotenv`).

## Phase 2: Local Telegram Interviewer Loop [✅ COMPLETE]
- [x] Create `.env` template for `TELEGRAM_BOT_TOKEN`, `USER_CHAT_ID`, and `OPENAI_API_KEY`.
- [x] Refine `telegram_biographer.py` to correctly load `TELEGRAM_BOT_TOKEN`.
- [x] Test 1: Start bot locally. Ensure it accepts a raw voice note or test ping.
- [x] Test 2: Send a raw Voice Note to the bot from phone. Ensure it downloads `.ogg` to `raw_audio/` successfully.
- [x] Test 3: Implement local Whisper/OpenAI transcription to write standard `.md` file to `transcripts/raw/`.

## Phase 3: The LLM Archivist [✅ COMPLETE]
- [x] Implement the `build_system_prompt()` function to dynamically read `BRAIN_SCHEMA.md` and `LIFE_BUCKETS.md`.
- [x] Implement the API call pipeline (OpenAI or Claude) to parse the raw transcript.
- [x] Write the `Archivist` script logic: The LLM outputs parsed strings -> Python physically creates or appends `memory_bank/` Markdown files.
- [x] End-to-end local test: Talk into Telegram -> Bot transcribes -> Archivist extracts entities -> `memory_bank/` files appear in IDE.

## Phase 4: Cloud & Sync Execution (Railway) [✅ IN PROGRESS]
- [x] Implement Git automation loop inside script (Bot runs `git commit` and `git push` upon ending the interview).
- [ ] Create `Procfile` / deployment files necessary for Railway.
- [ ] Push to Railway and conduct a live cloud test at 11:00 PM EST.

## Phase 5: Historical Backlog Crawl (Optional future step)
- [ ] Run scanner on `~/Desktop/Personal Agent projects`.
- [ ] Feed past codebases to the Archivist to populate the `memory_bank` history.
