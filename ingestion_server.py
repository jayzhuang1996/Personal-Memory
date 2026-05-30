import os
import re
import json
import html as html_mod
import hashlib
import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).parent / ".env")

app = FastAPI(title="GBrain Ingestion Server")

# OpenAI client (text summarization)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", "").strip())

# OpenRouter client (vision — Qwen2.5-VL)
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
vision_client = None
if OPENROUTER_KEY:
    vision_client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_KEY,
        default_headers={"HTTP-Referer": "gbrain", "X-Title": "GBrain Ingestion"},
    )

VISION_MODEL = os.getenv("VISION_MODEL", "qwen/qwen2.5-vl-72b-instruct")
VISION_ENABLED = vision_client is not None

VAULT = Path.home() / ".gbrain_vault" / "markdown"
LOG_FILE = VAULT / "log" / "log.md"
SEEN_HASHES_FILE = VAULT / "log" / "seen_hashes.txt"

# httpx client for link resolution (mimics a browser)
http_client = httpx.Client(timeout=15, follow_redirects=True, headers={
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*",
})

# ─────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────

SOURCE_TO_CATEGORY = {
    "x": "sources/x",
    "twitter": "sources/x",
    "web": "sources/web",
    "article": "sources/web",
    "audio": "sources/audio",
    "telegram": "sources/audio",
}

class CapturePayload(BaseModel):
    source: str                    # "x" | "web" | "audio" | "telegram"
    url: str = ""
    author: str = ""
    text: str
    captured_at: str = ""         # ISO 8601
    capture_method: str = "manual"
    category_override: str = ""   # optional: force a specific category
    media_urls: list[str] = []    # image/video URLs for vision model description

# ─────────────────────────────────────────────
# DEDUPLICATION
# ─────────────────────────────────────────────

def content_hash(payload: CapturePayload) -> str:
    key = (payload.url or payload.text[:200]).strip()
    return hashlib.sha256(key.encode()).hexdigest()[:16]

def already_seen(h: str) -> bool:
    if not SEEN_HASHES_FILE.exists():
        return False
    return h in SEEN_HASHES_FILE.read_text()

def mark_seen(h: str):
    SEEN_HASHES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_HASHES_FILE, "a") as f:
        f.write(h + "\n")

# ─────────────────────────────────────────────
# VISION: Qwen2.5-VL via OpenRouter
# ─────────────────────────────────────────────

def describe_media(media_urls: list[str], context_text: str = "") -> str:
    """Send image URLs to Qwen2.5-VL for visual description.
    Returns a text description to merge with the tweet text.
    Skips video URLs (not supported by VL models directly).
    """
    if not VISION_ENABLED or not media_urls:
        return ""

    # Filter: only image URLs (skip video, GIF, etc.)
    image_urls = [u for u in media_urls if _is_image_url(u)]
    if not image_urls:
        return ""

    # Build vision API message with image content blocks
    content_blocks = [
        {"type": "text", "text": "Describe what you see in these images. Be concise but capture key visual details, text shown, people, objects, and the overall scene. Respond in English."}
    ]
    if context_text.strip():
        content_blocks[0]["text"] += f"\n\nContext from the post: {context_text[:500]}"

    for img_url in image_urls[:4]:  # Max 4 images per call
        content_blocks.append({
            "type": "image_url",
            "image_url": {"url": img_url},
        })

    try:
        resp = vision_client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{"role": "user", "content": content_blocks}],
            max_tokens=200,
        )
        choice = resp.choices[0] if resp.choices else None
        if choice and choice.message and choice.message.content:
            return choice.message.content.strip()
        print(f"[vision] Empty response: {resp}")
    except Exception as e:
        print(f"[vision] Qwen2.5-VL error: {e}")
        import traceback
        traceback.print_exc()
    return ""


def _is_image_url(url: str) -> bool:
    """Heuristic: detect image URLs vs video/GIF."""
    lower = url.lower()
    if any(lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".heic")):
        return True
    # X media URLs use format= jpg/png even for video thumbnails, accept them
    if "twimg.com" in lower or "pbs.twimg.com" in lower:
        if "video_thumb" in lower or "_video_" in lower:
            return False  # Skip video thumbnails — no context to describe
        return True
    # Generic: if it looks like an image
    if any(kw in lower for kw in ["image", "photo", "img", "picture"]):
        return True
    return False


# ─────────────────────────────────────────────
# LLM: SUMMARY + TOPICS
# ─────────────────────────────────────────────

def extract_metadata(text: str, visual_description: str = "") -> tuple[str, list[str]]:
    """One LLM call: returns (summary, [topic1, topic2, topic3]).
    Optionally includes a visual description from the vision model."""
    combined = text[:2000]
    if visual_description:
        combined = f"[Post text]: {text[:1500]}\n\n[Image description]: {visual_description}"

    prompt = f"""Extract a 1-sentence summary and up to 3 topic tags from this content.
If an image description is included, incorporate it into the summary.

Output ONLY valid JSON, no explanation:
{{"summary": "...", "topics": ["tag1", "tag2"]}}

Content:
{combined}"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=150,
    )
    import json
    data = json.loads(response.choices[0].message.content)
    summary = data.get("summary", "").strip()
    topics = data.get("topics", [])[:3]
    return summary, topics

# ─────────────────────────────────────────────
# LINK RESOLUTION & DEEP ANALYSIS
# ─────────────────────────────────────────────

def extract_links(text: str) -> list[str]:
    """Extract all unique URLs from text. Handles t.co, expanded URLs, etc."""
    urls = re.findall(r'https?://[^\s]+', text)
    cleaned = []
    for u in urls:
        u = re.sub(r'[.,;:!?)\]]+$', '', u)
        if u not in cleaned:
            cleaned.append(u)
    return cleaned[:5]  # Max 5 links


# ── Link type detection ────────────────────────────────────────────

def _detect_link_type(url: str, parsed) -> str:
    """Classify a URL by its domain and path pattern."""
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    # Video platforms
    if any(d in host for d in ['youtube.com', 'youtu.be', 'vimeo.com']):
        return 'video'
    if any(d in host for d in ['twitch.tv', 'bilibili.com']):
        return 'video'

    # Code forges
    if 'github.com' in host:
        parts = path.strip('/').split('/')
        if len(parts) >= 2 and parts[0] not in ('issues', 'pulls', 'blob', 'tree', 'wiki',
                                                  'releases', 'actions', 'discussions', 'sponsors'):
            return 'github_repo'
        return 'github_page'
    if any(d in host for d in ['gitlab.com', 'bitbucket.org', 'gitee.com']):
        return 'github_repo'  # same API pattern-ish, generic handling

    # Package registries
    if any(d in host for d in ['pypi.org', 'npmjs.com', 'crates.io', 'pkg.go.dev']):
        return 'package'
    if 'huggingface.co' in host:
        return 'huggingface'

    # Documentation
    if any(d in host for d in ['readthedocs.io', 'gitbook.io', 'docs.rs']):
        return 'documentation'

    # Research / papers
    if any(d in host for d in ['arxiv.org', 'openreview.net', 'paperswithcode.com']):
        return 'paper'

    # News / articles (major publishers)
    news_domains = ['medium.com', 'substack.com', 'dev.to', 'hackernoon.com',
                    'theverge.com', 'techcrunch.com', 'wired.com', 'arstechnica.com',
                    'nytimes.com', 'wsj.com', 'bloomberg.com', 'reuters.com',
                    'nature.com', 'science.org', 'pnas.org']
    if any(d in host for d in news_domains):
        return 'article'

    # Twitter/X threads
    if any(d in host for d in ['x.com', 'twitter.com']):
        if '/status/' in path:
            return 'tweet'
        return 'social'

    return 'webpage'


# ── Specialized fetchers ────────────────────────────────────────────

def _fetch_github_repo(owner: str, repo: str) -> dict:
    """Fetch GitHub repo metadata + README excerpt via public API."""
    result = {"url": f"https://github.com/{owner}/{repo}", "title": f"{owner}/{repo}",
              "description": "", "type": "github", "error": ""}
    try:
        resp = http_client.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers={"Accept": "application/vnd.github+json"},
        )
        if resp.status_code == 200:
            data = resp.json()
            result["title"] = data.get("full_name", f"{owner}/{repo}")
            result["description"] = (data.get("description") or "")[:500]
            result["stars"] = data.get("stargazers_count", 0)
            result["language"] = data.get("language", "")
            result["topics"] = data.get("topics", [])[:10]
            result["updated_at"] = data.get("updated_at", "")
            result["license"] = (data.get("license") or {}).get("spdx_id", "")
            result["forks"] = data.get("forks_count", 0)

            # Fetch README for real content summary
            try:
                readme_resp = http_client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/readme",
                    headers={"Accept": "application/vnd.github+json"},
                )
                if readme_resp.status_code == 200:
                    readme_data = readme_resp.json()
                    import base64
                    content = base64.b64decode(readme_data.get("content", "")).decode("utf-8", errors="replace")
                    result["readme_excerpt"] = content[:2000]
                elif readme_resp.status_code == 404:
                    pass  # No README found, that's fine
            except Exception:
                pass  # README fetch is best-effort

        elif resp.status_code == 404:
            result["error"] = "repo not found"
    except Exception as e:
        result["error"] = str(e)[:200]
    return result


def _fetch_youtube_video(url: str) -> dict:
    """Fetch YouTube video metadata via oEmbed + page scrape.
    No API key required for oEmbed.
    """
    result = {"url": url, "title": "", "description": "", "type": "video",
              "platform": "youtube", "error": ""}
    try:
        # oEmbed for basic metadata
        oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
        resp = http_client.get(oembed_url)
        if resp.status_code == 200:
            data = resp.json()
            result["title"] = data.get("title", "")
            result["author"] = data.get("author_name", "")
            result["thumbnail"] = data.get("thumbnail_url", "")

        # Scrape watch page for description
        page_resp = http_client.get(url)
        html = page_resp.text[:100000]

        # Extract description from ytInitialData or meta tags
        desc_match = re.search(
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
            html, re.IGNORECASE
        )
        if desc_match:
            full_desc = desc_match.group(1).strip()
            result["description"] = full_desc[:800]  # YouTube descriptions can be long
            # Check for timestamps (indicates structured content)
            timestamps = re.findall(r'(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—]\s*(.+)', full_desc)
            if timestamps:
                result["chapters"] = [{"time": t[0], "title": t[1][:80]} for t in timestamps[:15]]

        # Duration
        duration_match = re.search(r'"lengthSeconds"\s*:\s*"(\d+)"', html)
        if not duration_match:
            duration_match = re.search(r'"lengthSeconds":\s*(\d+)', html)
        if duration_match:
            secs = int(duration_match.group(1))
            if secs >= 3600:
                result["duration"] = f"{secs//3600}h {(secs%3600)//60}m"
            else:
                result["duration"] = f"{secs//60}m {secs%60}s"

        # View count
        view_match = re.search(r'"viewCount"\s*:\s*"(\d+)"', html)
        if view_match:
            result["views"] = int(view_match.group(1))

    except Exception as e:
        result["error"] = str(e)[:200]
    return result


def _fetch_webpage(url: str) -> dict:
    """Generic web page scraper. Extracts title, description, type signals."""
    result = {"url": url, "title": "", "description": "", "type": "webpage", "error": ""}
    try:
        resp = http_client.get(url)
        resp.raise_for_status()
        html_text = resp.text[:80000]

        # Title
        title_match = re.search(r'<title[^>]*>(.*?)</title>', html_text, re.IGNORECASE | re.DOTALL)
        if title_match:
            result["title"] = html_mod.unescape(title_match.group(1).strip())[:200]
        # og:title as fallback
        if not result["title"]:
            og_title = re.search(
                r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                html_text, re.IGNORECASE
            )
            if og_title:
                result["title"] = og_title.group(1).strip()[:200]

        # Description: try meta description, then og:description
        desc = None
        for pattern in [
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']',
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
        ]:
            m = re.search(pattern, html_text, re.IGNORECASE)
            if m:
                desc = m.group(1).strip()[:500]
                break
        if desc:
            result["description"] = desc

        # Paywall detection
        if any(kw in html_text.lower() for kw in ['paywall', 'subscribe to read', 'premium article',
                                                     'metered paywall', 'subscribers-only']):
            result["paywall"] = True

        # Infer richer type from og:type and content signals
        og_type = re.search(r'<meta[^>]+property=["\']og:type["\'][^>]+content=["\']([^"\']+)',
                            html_text, re.IGNORECASE)
        if og_type:
            og_type_val = og_type.group(1).lower()
            if 'article' in og_type_val:
                result["type"] = 'article'
            elif 'video' in og_type_val:
                result["type"] = 'video'
            elif 'product' in og_type_val:
                result["type"] = 'product'

    except Exception as e:
        result["error"] = str(e)[:200]
    return result


# ── Main resolver router ────────────────────────────────────────────

def resolve_link(url: str) -> dict:
    """Resolve a URL: detect type, fetch metadata from the best source."""
    parsed = urlparse(url)
    link_type = _detect_link_type(url, parsed)

    # Route to specialized fetcher
    if link_type == 'github_repo':
        gh_match = re.match(r'^/([^/]+)/([^/]+)', parsed.path)
        if gh_match:
            return _fetch_github_repo(gh_match.group(1), re.sub(r'\.git$', '', gh_match.group(2)))

    if link_type == 'video':
        host = parsed.netloc.lower()
        if any(d in host for d in ['youtube.com', 'youtu.be']):
            return _fetch_youtube_video(url)
        # Vimeo / other video platforms: scrape the page
        result = _fetch_webpage(url)
        result["type"] = "video"
        return result

    if link_type in ('tweet', 'social'):
        host = parsed.netloc.lower()
        if any(d in host for d in ['x.com', 'twitter.com']):
            return {
                "url": url,
                "title": url.rstrip("/").rsplit("/", 1)[-1] if "/status/" in url else "",
                "description": "",
                "type": link_type,
                "text_content": "",
                "note": "X/Twitter content requires authentication — using tweet text only",
            }
        result = _fetch_webpage(url)
        result["type"] = link_type
        return result

    # Everything else: generic web scraper
    result = _fetch_webpage(url)
    result["type"] = link_type  # preserve detected type
    return result


def generate_analysis(text: str, visual_description: str = "",
                      link_contexts: list[dict] | None = None) -> str:
    """Generate a deep analysis: key takeaways, link breakdown, hype check.
    Returns a markdown string ready to append to the vault file.
    """
    if link_contexts is None:
        link_contexts = []

    # Build a rich context string for the LLM
    context_parts = [f"POST TEXT:\n{text[:1500]}"]

    if visual_description:
        context_parts.append(f"IMAGE DESCRIPTION:\n{visual_description}")

    if link_contexts:
        links_text = "LINKS FOUND IN POST:\n"
        for lc in link_contexts:
            links_text += f"- {lc['url']}\n"
            if lc.get('title'):
                links_text += f"  Title: {lc['title']}\n"
            if lc.get('description'):
                links_text += f"  Description: {lc['description']}\n"
            if lc.get('stars'):
                links_text += f"  GitHub: {lc['stars']} stars, language: {lc.get('language', '?')}, "
                links_text += f"topics: {', '.join(lc.get('topics', []))}\n"
            if lc.get('readme_excerpt'):
                links_text += f"  README excerpt: {lc['readme_excerpt'][:1500]}\n"
            if lc.get('error'):
                links_text += f"  (could not fetch: {lc['error']})\n"
            links_text += "\n"
        context_parts.append(links_text)

    combined = "\n\n".join(context_parts)

    prompt = f"""You are an analyst for a personal knowledge management system. Analyze this social media post and its linked content. Be honest, critical, and substantive.

{combined}

Generate a JSON object with this structure:
{{
  "key_takeaways": [
    "First specific insight — 1-2 sentences with concrete detail",
    "Second specific insight",
    "Third specific insight"
  ],
  "links_breakdown": [
    {{
      "url": "the URL",
      "what_it_is": "concrete description of what this link actually contains — read the README excerpt if provided and describe what the project/tool/paper really does",
      "why_care": "why this matters (or doesn't) — what problem does it solve? who is it for? is it production-ready or experimental?",
      "credibility": "credible / mixed / unverified — based on stars, author, substance"
    }}
  ],
  "hype_verdict": "substance / mixed / hype",
  "hype_rationale": "1-2 sentences explaining the verdict"
}}

Rules:
- 3-5 key takeaways. Make them SPECIFIC. Do NOT prefix with 'Takeaway N —', just write the insight directly.
- IMPORTANT: If a README excerpt is provided for a GitHub repo, USE IT. Summarize what the project actually does based on the README, not just the one-line description. What features does it have? What problem does it solve?
- For GitHub repos: assess maturity based on stars, update recency, documentation, and README substance.
- For articles/papers: assess whether it's original research or a rehash.
- Hype check: "substance" = real value, "mixed" = some value buried in marketing, "hype" = empty claims or recycled content.
- Be critical. If a link goes to a low-quality source, say so.
- If no links are provided, still generate takeaways.

Respond with ONLY valid JSON, no markdown wrapping."""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=1000,
        )
        data = json.loads(resp.choices[0].message.content)
        return _format_analysis_markdown(data)
    except Exception as e:
        print(f"[analysis] Error: {e}")
        return ""


def _format_analysis_markdown(data: dict) -> str:
    """Convert the analysis JSON into a clean markdown section."""
    md = "## Analysis\n\n"

    # Key Takeaways
    md += "### Key Takeaways\n"
    for i, t in enumerate(data.get("key_takeaways", []), 1):
        md += f"{i}. {t}\n"
    md += "\n"

    # Links Breakdown
    links = data.get("links_breakdown", [])
    if links:
        md += "### Links Breakdown\n"
        for link in links:
            url = link.get("url", "")
            # Shorten display URL
            display_url = url if len(url) < 80 else url[:77] + "..."
            md += f"**[{link.get('what_it_is', 'Link')}]({url})**\n"
            md += f"- **What**: {link.get('what_it_is', 'N/A')}\n"
            md += f"- **Why care**: {link.get('why_care', 'N/A')}\n"
            md += f"- **Credibility**: {link.get('credibility', 'unknown')}\n"
            md += "\n"

    # Hype Check
    verdict = data.get("hype_verdict", "unknown").upper()
    rationale = data.get("hype_rationale", "")
    md += f"### Hype Check: {verdict}\n"
    md += f"{rationale}\n"

    return md


# ─────────────────────────────────────────────
# FILE WRITER
# ─────────────────────────────────────────────

def classify_mode(category: str, topics: list[str], summary: str) -> str:
    """Classify content into operating_philosophy / operating_system / general."""
    prompt = f"""Classify this saved content into exactly one mode. Return ONLY the word.

MODES:
- operating_philosophy — content about identity, memory, consciousness, existentialism, human nature, society, meaning, art, literature, philosophy, history, self-understanding, the human condition
- operating_system — content about technology, business, strategy, product, engineering, finance, crypto, tools, frameworks, practical how-to, career, startups, code, data, AI/ML as practical tool

Category: {category}
Topics: {', '.join(topics)}
Summary: {summary}

Which mode? Reply with exactly one word."""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=20,
            temperature=0,
        )
        result = resp.choices[0].message.content.strip().lower()
        if "philosophy" in result:
            return "operating_philosophy"
        elif "system" in result:
            return "operating_system"
        return "general"
    except Exception:
        return "general"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower())[:40].strip("_")

def write_note(payload: CapturePayload, summary: str, topics: list[str], note_id: str,
               visual_description: str = "", analysis_md: str = "",
               mode: str = "general") -> Path:
    category = payload.category_override or SOURCE_TO_CATEGORY.get(payload.source, "sources/web")

    # Parse date
    try:
        dt = datetime.datetime.fromisoformat(payload.captured_at)
    except Exception:
        dt = datetime.datetime.now()

    date_path = f"{dt.year}/{dt.month:02d}"
    folder = VAULT / category / date_path
    folder.mkdir(parents=True, exist_ok=True)

    # Filename
    prefix = category.split("/")[-1]  # "x", "web", "audio"
    filename = f"{prefix}_{dt.strftime('%Y_%m_%d')}_{note_id}.md"
    filepath = folder / filename

    # YAML frontmatter
    topics_yaml = "\n".join(f"  - {t}" for t in topics)
    related = f'  - "[[evergreen/{slugify(topics[0])}]]"' if topics else ""
    has_media = "true" if payload.media_urls else "false"
    has_analysis = "true" if analysis_md else "false"

    content = f"""---
id: {prefix}_{dt.strftime('%Y_%m_%d')}_{note_id}
type: source_post
source: {payload.source}
category: {category}
mode: {mode}
date: {dt.strftime('%Y-%m-%d')}
author: "{payload.author}"
url: "{payload.url}"
has_media: {has_media}
has_analysis: {has_analysis}
topics:
{topics_yaml}
summary: "{summary}"
status: inbox
captured_via: {payload.capture_method}
related_to:
{related}
---

# {summary}

{payload.text}
"""
    if visual_description:
        content += f"\n## Visual Description\n{visual_description}\n"
    if analysis_md:
        content += f"\n{analysis_md}\n"

    filepath.write_text(content)
    return filepath

# ─────────────────────────────────────────────
# LOG
# ─────────────────────────────────────────────

def append_log(filepath: Path, topics: list[str]):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    rel = filepath.relative_to(VAULT)
    topic_str = " ".join(topics)
    with open(LOG_FILE, "a") as f:
        f.write(f"## [{timestamp}] ingest | {rel} | topics: {topic_str}\n")

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "vault": str(VAULT)}

@app.post("/ingest")
def ingest(payload: CapturePayload):
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="text field is required")

    # Deduplicate
    h = content_hash(payload)
    if already_seen(h):
        return {"status": "skipped", "reason": "duplicate"}

    # Step 1: Vision — describe images if present
    visual_description = ""
    if VISION_ENABLED and payload.media_urls:
        visual_description = describe_media(payload.media_urls, payload.text)

    # Step 2: Resolve links — fetch what's behind each URL
    urls = extract_links(payload.text)
    link_contexts = []
    for url in urls:
        ctx = resolve_link(url)
        link_contexts.append(ctx)
        if ctx.get("title"):
            print(f"[links] {url[:60]} → {ctx['title'][:80]}")

    # Step 3: LLM summarization (with visual context if available)
    try:
        summary, topics = extract_metadata(payload.text, visual_description)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM extraction failed: {e}")

    # Step 4: Mode classification — operating_philosophy / operating_system
    category = payload.category_override or SOURCE_TO_CATEGORY.get(payload.source, "sources/web")
    mode = classify_mode(category, topics, summary)

    # Step 5: Deep analysis — key takeaways, link breakdown, hype check
    analysis_md = ""
    try:
        analysis_md = generate_analysis(payload.text, visual_description, link_contexts)
    except Exception as e:
        print(f"[analysis] Deep analysis failed (non-fatal): {e}")

    # Step 6: Write note
    filepath = write_note(payload, summary, topics, h, visual_description, analysis_md, mode=mode)
    mark_seen(h)
    append_log(filepath, topics)

    return {
        "status": "ok",
        "file": str(filepath.relative_to(VAULT)),
        "summary": summary,
        "topics": topics,
        "mode": mode,
        "has_media_description": bool(visual_description),
        "has_analysis": bool(analysis_md),
        "links_resolved": len([c for c in link_contexts if not c.get("error")]),
    }
