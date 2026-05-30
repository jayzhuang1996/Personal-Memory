#!/usr/bin/env python3
"""Fetch X (Twitter) bookmarks and pipe them into the GBrain ingestion server.

Authentication via cookies — requires auth_token and ct0 from a logged-in
browser session. Store them in ~/.x_cookies.env:
    X_AUTH_TOKEN=xxx
    X_CT0=xxx

Error handling:
  - Auto-starts the ingestion server if not running
  - Auto-refreshes X GraphQL query ID on 400 errors
  - Retries transient failures with backoff
"""

import os
import json
import sys
import time
import signal
import subprocess
import datetime
import logging
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ── Config ──────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).parent.resolve()
_PORT = os.getenv("PORT", "8000")
INGEST_URL = f"http://localhost:{_PORT}/ingest"
HEALTH_URL = f"http://localhost:{_PORT}/health"
BOOKMARKS_PER_RUN = 50
PAGE_SIZE = 20
MAX_BOOKMARK_AGE_HOURS = 12  # skip bookmarks older than this (Twitter created_at)
DELAY_BETWEEN_PAGES = 1.5
DELAY_BETWEEN_INGESTS = 0.5
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0  # seconds, multiplied by attempt number

SERVER_STARTUP_TIMEOUT = 10  # seconds to wait for ingestion server to start

DEPS_DIR = PROJECT_DIR / "personal-rag"

# Public Bearer token from X's web client (relatively stable).
BEARER_TOKEN = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"

# ── Logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("x_bookmarks")


# ── Query ID management ─────────────────────────────────────────────

def _load_query_config_from_tweety() -> tuple[str, dict] | None:
    """Try to load current query ID and features from a local tweety-ns install."""
    try:
        from tweety.builder import FlowData  # noqa: F401
        import inspect

        # Instantiate a dummy builder to read its constants
        builder = FlowData.__new__(FlowData)
        # We only need the URL constant and features dict
        url = getattr(builder, "URL_AUSER_BOOKMARK", "")
        if not url:
            return None
        # URL format: https://x.com/i/api/graphql/{queryId}/Bookmarks
        qid = url.split("/")[-2] if "/graphql/" in url else ""
        if not qid:
            return None

        # Reconstruct features from the builder's get_bookmarks method
        import re
        src = inspect.getsource(builder.get_bookmarks)
        features_match = re.search(r'"features"\s*:\s*(\{[^}]+\})', src)
        features = {}
        if features_match:
            # Best-effort: use the hardcoded features as fallback anyway
            pass

        return qid, None  # features fall through to hardcoded below
    except Exception:
        return None


def _get_query_config() -> tuple[str, dict]:
    """Return (query_id, features_dict), preferring tweety live config."""
    live = _load_query_config_from_tweety()
    if live and live[0]:
        log.info("Using query ID from tweety-ns: %s", live[0])
        return live[0], _hardcoded_features()

    log.info("Using hardcoded query ID (tweety-ns not available).")
    return "bN6kl72VsPDRIGxDIhVu7A", _hardcoded_features()


def _hardcoded_features() -> dict:
    return {
        "graphql_timeline_v2_bookmark_timeline": True,
        "rweb_lists_timeline_redesign_enabled": True,
        "responsive_web_graphql_exclude_directive_enabled": True,
        "verified_phone_label_enabled": True,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "tweetypie_unmention_optimization_enabled": True,
        "responsive_web_edit_tweet_api_enabled": True,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": True,
        "tweet_awards_web_tipping_enabled": True,
        "freedom_of_speech_not_reach_fetch_enabled": True,
        "standardized_nudges_misinfo": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": True,
        "responsive_web_media_download_video_enabled": True,
        "responsive_web_enhance_cards_enabled": False,
    }


def _refresh_query_id_from_x_js() -> tuple[str, dict] | None:
    """Emergency fallback: extract current query ID from X's main JS bundle."""
    try:
        import re
        auth_token, ct0 = load_cookies()
        client = httpx.Client(
            cookies={"auth_token": auth_token, "ct0": ct0},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=20,
        )
        r = client.get("https://x.com/home")
        r.raise_for_status()

        js_urls = re.findall(r'(https://abs\.twimg\.com/responsive-web/client-web/main\.[a-f0-9]+\.js)', r.text)
        if not js_urls:
            return None

        r2 = client.get(js_urls[0])
        js = r2.text

        # Find bookmarks query ID
        pattern = r'queryId:"([A-Za-z0-9+/=_-]{20,48})",operationName:"([^"]*[Bb]ookmark[^"]*)"'
        matches = re.findall(pattern, js)
        for qid, op_name in matches:
            if "Bookmark" in op_name and "Delete" not in op_name and "Create" not in op_name and "Search" not in op_name:
                log.info("Auto-discovered query ID from X JS: %s (%s)", qid, op_name)
                return qid, _hardcoded_features()

        # Fallback: use any bookmark query that isn't a mutation
        for qid, op_name in matches:
            log.info("Auto-discovered bookmark query: %s (%s)", qid, op_name)
            return qid, _hardcoded_features()
    except Exception as e:
        log.warning("Failed to auto-discover query ID from X JS: %s", e)
    return None


# ── Cookie loading ──────────────────────────────────────────────────

def load_cookies() -> tuple[str, str]:
    """Load X auth_token and ct0 from ~/.x_cookies.env or environment."""
    env_path = Path.home() / ".x_cookies.env"
    if env_path.exists():
        load_dotenv(env_path)

    auth_token = os.getenv("X_AUTH_TOKEN", "").strip()
    ct0 = os.getenv("X_CT0", "").strip()

    if not auth_token or not ct0:
        log.error("Missing X_AUTH_TOKEN or X_CT0. Set them in ~/.x_cookies.env")
        sys.exit(1)

    return auth_token, ct0


# ── Server lifecycle ────────────────────────────────────────────────

def _is_server_running() -> bool:
    try:
        r = httpx.get(HEALTH_URL, timeout=5)
        return r.status_code == 200
    except httpx.RequestError:
        return False


def ensure_server_running() -> None:
    """Start the ingestion server if it's not already up."""
    if _is_server_running():
        log.info("Ingestion server is already running.")
        return

    if os.getenv("SKIP_SERVER_STARTUP", "").strip().lower() in ("true", "1", "yes"):
        log.warning("SKIP_SERVER_STARTUP set — server should be running in-process (Railway)")
        if not _is_server_running():
            raise RuntimeError("Ingestion server not reachable and auto-start is disabled")
        return

    log.info("Ingestion server not running — starting it now...")
    server_script = PROJECT_DIR / "ingestion_server.py"
    if not server_script.exists():
        log.error("Cannot find %s", server_script)
        sys.exit(1)

    # Start uvicorn in the background
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "ingestion_server:app",
            "--host", "0.0.0.0",
            "--port", _PORT,
        ],
        cwd=str(PROJECT_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for it to become ready
    deadline = time.time() + SERVER_STARTUP_TIMEOUT
    while time.time() < deadline:
        if _is_server_running():
            log.info("Ingestion server started (pid=%d).", proc.pid)
            return
        time.sleep(0.5)

    log.error("Ingestion server failed to start within %ds.", SERVER_STARTUP_TIMEOUT)
    proc.kill()
    sys.exit(1)


# ── X API client ────────────────────────────────────────────────────

def build_client(auth_token: str, ct0: str) -> httpx.Client:
    """Create an httpx client authenticated to X's internal API."""
    return httpx.Client(
        cookies={"auth_token": auth_token, "ct0": ct0},
        headers={
            "Authorization": f"Bearer {BEARER_TOKEN}",
            "x-csrf-token": ct0,
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://x.com/i/bookmarks",
        },
        timeout=30,
    )


# ── Bookmark fetching ───────────────────────────────────────────────

def fetch_bookmarks_page(
    client: httpx.Client,
    cursor: str | None,
    query_id: str,
    features: dict,
) -> dict:
    """Fetch a single page of bookmarks from X's GraphQL API."""
    variables = {"count": PAGE_SIZE, "includePromotedContent": False}
    if cursor:
        variables["cursor"] = cursor

    url = f"https://x.com/i/api/graphql/{query_id}/Bookmarks"
    resp = client.get(url, params={
        "variables": json.dumps(variables),
        "features": json.dumps(features),
    })
    resp.raise_for_status()
    return resp.json()


def fetch_with_retry(
    client: httpx.Client,
    cursor: str | None,
    query_id: str,
    features: dict,
) -> dict:
    """Fetch a page with retry logic and query-ID auto-refresh on 400."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fetch_bookmarks_page(client, cursor, query_id, features)
        except httpx.HTTPStatusError as e:
            last_error = e
            if e.response.status_code == 400 and attempt < MAX_RETRIES:
                log.warning(
                    "400 Bad Request (attempt %d/%d) — query ID %s may be stale, "
                    "attempting auto-refresh...",
                    attempt, MAX_RETRIES, query_id,
                )
                refreshed = _refresh_query_id_from_x_js()
                if refreshed:
                    new_qid, new_features = refreshed
                    log.info("Refreshed query ID: %s", new_qid)
                    # Update module-level state via nonlocal-ish mutation
                    fetch_with_retry._cached_qid = new_qid
                    fetch_with_retry._cached_features = new_features
                    return fetch_bookmarks_page(client, cursor, new_qid, new_features)
            elif e.response.status_code == 429:
                wait = RETRY_BACKOFF * attempt * 2
                log.warning("429 rate limited — waiting %.1fs...", wait)
                time.sleep(wait)
            else:
                break
        except httpx.RequestError as e:
            last_error = e
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF * attempt
                log.warning("Network error (attempt %d/%d): %s — retrying in %.1fs",
                            attempt, MAX_RETRIES, e, wait)
                time.sleep(wait)

    raise last_error  # type: ignore[misc]


fetch_with_retry._cached_qid = None  # type: ignore[attr-defined]
fetch_with_retry._cached_features = None  # type: ignore[attr-defined]


def _parse_twitter_date(date_str: str) -> datetime.datetime | None:
    """Parse Twitter's created_at format: 'Mon Apr 28 12:30:00 +0000 2025'."""
    if not date_str:
        return None
    try:
        return datetime.datetime.strptime(date_str, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return None


def extract_tweets(raw: dict, cutoff: datetime.datetime | None = None) -> list[dict]:
    """Extract tweet entries from the raw bookmarks API response.
    If cutoff is provided, tweets older than cutoff are skipped."""
    skipped_old = 0
    instructions = (
        raw.get("data", {})
        .get("bookmark_timeline_v2", {})
        .get("timeline", {})
        .get("instructions", [])
    )
    entries = []
    for instr in instructions:
        if instr.get("type") == "TimelineAddEntries":
            for entry in instr.get("entries", []):
                content = entry.get("content", {})
                if content.get("entryType") != "TimelineTimelineItem":
                    continue
                tweet_result = (
                    content.get("itemContent", {})
                    .get("tweet_results", {})
                    .get("result", {})
                )
                if not tweet_result:
                    continue
                if tweet_result.get("__typename") == "TweetWithVisibilityResults":
                    tweet_result = tweet_result.get("tweet", {})
                legacy = tweet_result.get("legacy", {})
                core = tweet_result.get("core", {})
                user_results = (
                    core.get("user_results", {})
                    .get("result", {})
                    .get("legacy", {})
                )
                if not legacy:
                    continue

                # Skip tweets older than the cutoff
                if cutoff:
                    created = _parse_twitter_date(legacy.get("created_at", ""))
                    if created and created < cutoff:
                        skipped_old += 1
                        continue

                # Extract media URLs from tweet entities
                media_urls = _extract_media_urls(legacy)

                # Expand t.co shortened URLs to their real destinations
                full_text = legacy.get("full_text", "")
                full_text = _expand_tco_urls(full_text, legacy)

                # If this bookmark is an X Article (note_tweet), extract the full article text
                note_text = ""
                note_tweet = tweet_result.get("note_tweet", {})
                if note_tweet:
                    ntr = note_tweet.get("note_tweet_results", {}).get("result", {})
                    note_text = ntr.get("text", "") or ""
                    if note_text:
                        full_text = f"{full_text}\n\n--- ARTICLE ---\n{note_text}"

                # Handle retweets: extract the retweeted source's content
                retweeted = None
                rts = legacy.get("retweeted_status_result", {})
                if rts:
                    rt_result = rts.get("result", {})
                    rt_legacy = rt_result.get("legacy", {})
                    rt_core = rt_result.get("core", {})
                    rt_user = (
                        rt_core.get("user_results", {})
                        .get("result", {})
                        .get("legacy", {})
                    )
                    if rt_legacy:
                        rt_full_text = rt_legacy.get("full_text", "")
                        rt_full_text = _expand_tco_urls(rt_full_text, rt_legacy)

                        # Check for note_tweet in the retweeted tweet
                        rt_note_text = ""
                        rt_note_tweet = rt_result.get("note_tweet", {})
                        if rt_note_tweet:
                            rt_ntr = rt_note_tweet.get("note_tweet_results", {}).get("result", {})
                            rt_note_text = rt_ntr.get("text", "") or ""
                            if rt_note_text:
                                rt_full_text = f"{rt_full_text}\n\n--- ARTICLE ---\n{rt_note_text}"

                        rt_media = _extract_media_urls(rt_legacy)
                        rt_author = f"@{rt_user.get('screen_name', 'unknown')}" if rt_user else "@unknown"
                        rt_url = (
                            f"https://x.com/{rt_user.get('screen_name', 'i')}"
                            f"/status/{rt_legacy.get('id_str', '')}"
                        ) if rt_user else ""

                        retweeted = {
                            "author": rt_author,
                            "url": rt_url,
                            "text": rt_full_text,
                            "media_urls": rt_media,
                            "has_note_tweet": bool(rt_note_text),
                        }

                        # Append retweeted content to the main tweet text for ingestion
                        full_text = (
                            f"{full_text}\n\n--- RETWEETED [{rt_author}] ---\n{rt_full_text}"
                        )
                        media_urls = media_urls + rt_media

                entries.append({
                    "text": full_text,
                    "author": f"@{user_results.get('screen_name', 'unknown')}",
                    "url": (
                        f"https://x.com/{user_results.get('screen_name', 'i')}"
                        f"/status/{legacy.get('id_str', '')}"
                    ),
                    "captured_at": legacy.get("created_at", ""),
                    "media_urls": media_urls[:4],
                    "has_note_tweet": bool(note_text),
                    "retweeted": retweeted,
                })
    return entries, skipped_old


def _expand_tco_urls(text: str, legacy: dict) -> str:
    """Replace t.co shortened URLs in tweet text with their expanded destinations."""
    entities = legacy.get("entities", {})
    url_entities = entities.get("urls", [])
    for u in url_entities:
        tco = u.get("url", "")
        expanded = u.get("expanded_url", "")
        if tco and expanded and tco in text:
            text = text.replace(tco, expanded)
    return text


def _extract_media_urls(legacy: dict) -> list[str]:
    """Extract image/video URLs from tweet legacy entities."""
    urls = []
    entities = legacy.get("entities", {})
    media_list = entities.get("media", [])
    for m in media_list:
        # Prefer media_url_https (direct image/video URL)
        media_url = m.get("media_url_https", "") or m.get("media_url", "")
        if media_url:
            # X returns thumbnails by default; request full-size by stripping :thumb suffix
            media_url = media_url.replace("&name=thumb", "&name=large")
            urls.append(media_url)
    # Also check extended_entities for video info
    extended = legacy.get("extended_entities", {})
    for m in extended.get("media", []):
        if m.get("type") == "video" or m.get("type") == "animated_gif":
            # Video: add preview image URL (thumbnail)
            preview = m.get("media_url_https", "") or m.get("media_url", "")
            if preview:
                preview = preview.replace("&name=thumb", "&name=large")
                urls.append(preview)
    return urls[:4]  # Max 4 URLs


def get_next_cursor(raw: dict) -> str | None:
    """Extract the bottom cursor for the next page of bookmarks."""
    instructions = (
        raw.get("data", {})
        .get("bookmark_timeline_v2", {})
        .get("timeline", {})
        .get("instructions", [])
    )
    for instr in instructions:
        if instr.get("type") == "TimelineAddEntries":
            for entry in instr.get("entries", []):
                content = entry.get("content", {})
                if content.get("entryType") == "TimelineTimelineCursor":
                    if content.get("cursorType") == "Bottom":
                        return content.get("value")
    return None


# ── Ingestion ────────────────────────────────────────────────────────

def ingest_tweet(tweet: dict) -> bool:
    """POST a single tweet to the local ingestion server. Returns True on success."""
    payload = {
        "source": "x",
        "url": tweet["url"],
        "author": tweet["author"],
        "text": tweet["text"],
        "captured_at": tweet["captured_at"],
        "capture_method": "x_bookmarks_script",
        "media_urls": tweet.get("media_urls", []),
        "retweeted": tweet.get("retweeted"),
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = httpx.post(INGEST_URL, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status", "?")
                if status == "ok":
                    log.info("  ingested: %s -- %s", tweet["author"], data.get("summary", ""))
                    return True
                elif status == "skipped":
                    log.info("  skipped (dup): %s", tweet["url"])
                    return True
            else:
                log.warning("  ingest failed (%s): %s", resp.status_code, resp.text[:100])
        except httpx.RequestError as e:
            log.error("  connection error (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    return False


# ── Main ─────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=== X Bookmarks -> GBrain Ingest ===")

    # 1. Ensure ingestion server is running (auto-start if needed)
    ensure_server_running()
    vault = httpx.get(HEALTH_URL, timeout=5).json().get("vault", "?")
    log.info("Vault path: %s", vault)

    # 2. Auth
    auth_token, ct0 = load_cookies()
    client = build_client(auth_token, ct0)

    # 3. Resolve query config
    query_id, features = _get_query_config()

    # 4. Fetch bookmarks (only recent ones)
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=MAX_BOOKMARK_AGE_HOURS)
    log.info("Cutoff date: %s (bookmarks older than %d hours skipped)", cutoff.strftime("%Y-%m-%d %H:%M"), MAX_BOOKMARK_AGE_HOURS)

    cursor = None
    all_tweets: list[dict] = []
    total_skipped_old = 0
    page = 0
    empty_streak = 0

    while len(all_tweets) < BOOKMARKS_PER_RUN:
        page += 1
        log.info("Fetching page %d (cursor=%s)...", page, cursor or "start")
        try:
            raw = fetch_with_retry(client, cursor, query_id, features)
        except httpx.HTTPError as e:
            log.error("X API error after retries: %s", e)
            break

        # If fetch_with_retry auto-refreshed, use the new config
        if fetch_with_retry._cached_qid:
            query_id = fetch_with_retry._cached_qid
            features = fetch_with_retry._cached_features or features

        batch, skipped_old = extract_tweets(raw, cutoff)
        total_skipped_old += skipped_old
        all_tweets.extend(batch)
        log.info("  got %d tweets (total: %d, skipped %d old)", len(batch), len(all_tweets), skipped_old)

        cursor = get_next_cursor(raw)
        if not cursor:
            log.info("No more pages (no cursor).")
            break

        # Stop if pages return nothing — we've passed the recent cutoff window
        if len(batch) == 0:
            empty_streak += 1
            if empty_streak >= 3:
                log.info("Stopping: %d consecutive empty pages — no more recent bookmarks.", empty_streak)
                break
        else:
            empty_streak = 0

        if len(all_tweets) >= BOOKMARKS_PER_RUN:
            break

        time.sleep(DELAY_BETWEEN_PAGES)

    # 5. Ingest
    if not all_tweets:
        log.info("No recent bookmarks to ingest (skipped %d old).", total_skipped_old)
        client.close()
        return

    log.info("Ingesting %d bookmarks (%d skipped as older than %d hours)...", len(all_tweets), total_skipped_old, MAX_BOOKMARK_AGE_HOURS)
    ingested = 0
    for tweet in all_tweets:
        if ingest_tweet(tweet):
            ingested += 1
        time.sleep(DELAY_BETWEEN_INGESTS)

    log.info("Done. Ingested %d/%d bookmarks.", ingested, len(all_tweets))
    client.close()


if __name__ == "__main__":
    main()
