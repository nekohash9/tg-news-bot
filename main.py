import asyncio
import os
import logging
import hashlib
import re
from html import unescape
from datetime import datetime, timedelta
from urllib.parse import urlparse

import aiohttp
import aiosqlite
import feedparser
import yaml
from dotenv import load_dotenv

load_dotenv()

# ---------------- Config (env / defaults) ----------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DB_PATH = os.getenv("DB_PATH", "/app/data/state.db")
SOURCES_FILE = os.getenv("SOURCES_FILE", "/app/sources.yaml")

# scheduling & throttling (more frequent defaults)
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))  # every 30 minutes
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "3"))             # per run hard cap
MIN_DELAY_BETWEEN_POSTS = float(os.getenv("MIN_DELAY_BETWEEN_POSTS", "2"))  # seconds

# "smart" limits
DAILY_MAX_POSTS = int(os.getenv("DAILY_MAX_POSTS", "30"))  # desired posts per rolling 24h
# domain rule: max N posts per domain per 24h
DOMAIN_MAX_PER_24H = int(os.getenv("DOMAIN_MAX_PER_24H", "2"))

# night mode (server local time by default)
NIGHT_START_HOUR = int(os.getenv("NIGHT_START_HOUR", "0"))
NIGHT_END_HOUR = int(os.getenv("NIGHT_END_HOUR", "7"))

USER_AGENT = os.getenv("USER_AGENT", "it-ambient-aggregator/1.0 (+https://example.com)")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------- helpers ----------------
def clean_text(text: str) -> str:
    """Remove HTML tags, comment counters and collapse whitespace."""
    if not text:
        return ""
    text = unescape(text)
    # remove tags
    text = re.sub(r"<[^>]+>", "", text)
    # remove comment counters etc
    text = re.sub(r"\b\d+\s+comments?\b", "", text, flags=re.I)
    text = re.sub(r"\bcomments?\b", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def escape_html_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("&", "&amp;")
    s = s.replace("<", "&lt;")
    s = s.replace(">", "&gt;")
    return s

def domain_from_url(url: str) -> str:
    try:
        p = urlparse(url)
        return (p.hostname or "").lower()
    except Exception:
        return ""

def norm_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def now_utc() -> datetime:
    return datetime.utcnow()

# ---------------- DB ----------------
async def init_db():
    # ensure directory exists for DB_PATH
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sent_items (
                url_hash TEXT PRIMARY KEY,
                url TEXT,
                domain TEXT,
                title TEXT,
                ts TEXT
            )
        """)
        await db.commit()
    logging.info("DB initialized (%s)", DB_PATH)

async def already_sent(url: str) -> bool:
    h = norm_hash(url)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM sent_items WHERE url_hash = ?", (h,))
        r = await cur.fetchone()
        return r is not None

async def mark_sent(url: str, title: str = ""):
    h = norm_hash(url)
    domain = domain_from_url(url)
    ts = now_utc().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO sent_items(url_hash, url, domain, title, ts) VALUES(?,?,?,?,?)",
                         (h, url, domain, title, ts))
        await db.commit()
async def count_sent_last_24h() -> int:
    cutoff = (now_utc() - timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM sent_items WHERE ts >= ?", (cutoff,))
        r = await cur.fetchone()
        return (r[0] if r and r[0] is not None else 0)

async def count_sent_by_domain_last_24h(domain: str) -> int:
    if not domain:
        return 0
    cutoff = (now_utc() - timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM sent_items WHERE domain = ? AND ts >= ?", (domain, cutoff))
        r = await cur.fetchone()
        return (r[0] if r and r[0] is not None else 0)

# ---------------- sources loader ----------------
def load_sources_from_yaml(path: str):
    if not os.path.exists(path):
        logging.warning("Sources file not found: %s", path)
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    sources = data.get("sources") or []
    out = []
    for s in sources:
        url = s.get("url")
        if not url:
            continue
        tag = s.get("category") or s.get("tag") or s.get("name") or ""
        out.append({"url": url, "tag": (tag or "").upper()})
    return out

# ---------------- fetching ----------------
async def fetch_feed_entries(url: str):
    loop = asyncio.get_event_loop()
    parsed = await loop.run_in_executor(None, feedparser.parse, url)
    items = []
    for e in parsed.entries:
        link = e.get("link") or e.get("id")
        if not link:
            continue
        items.append({
            "title": e.get("title", "") or "",
            "summary": e.get("summary", "") or "",
            "url": link,
            "published": e.get("published", "")
        })
    return items

# ---------------- telegram send w/ 429 ----------------
async def send_message_telegram(payload: dict):
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as sess:
        try:
            async with sess.post(TELEGRAM_API, json=payload, timeout=30) as resp:
                text = await resp.text()
                if resp.status == 200:
                    return True, None
                elif resp.status == 429:
                    try:
                        js = await resp.json()
                        retry_after = int(js.get("parameters", {}).get("retry_after", 30))
                    except Exception:
                        retry_after = 30
                    return False, ("rate_limit", retry_after)
                else:
                    return False, ("error", f"{resp.status} {text[:400]}")
        except Exception as e:
            return False, ("exception", str(e))

async def safe_send_html(text: str):
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    ok, info = await send_message_telegram(payload)
    if ok:
        return True
    if info and info[0] == "rate_limit":
        retry_after = info[1]
        logging.warning("Telegram rate limit encountered, sleeping %s seconds", retry_after)
        await asyncio.sleep(retry_after + 1)
        ok2, info2 = await send_message_telegram(payload)
        if ok2:
            return True
        logging.warning("Telegram failed after retry: %s", info2)
        return False
    else:
        logging.warning("Telegram send failed: %s", info)
        return False

# ---------------- Night mode helper ----------------
def in_night_mode() -> bool:
    now = datetime.now()
    h = now.hour
    start = NIGHT_START_HOUR
    end = NIGHT_END_HOUR
    if start < end:
        return (h >= start and h < end)
    else:
        # night spans midnight (e.g., 22..7)
        return (h >= start or h < end)

# ---------------- Core processing ----------------
async def process_source(src: dict, posts_left: int):
    sent = 0
    try:
        entries = await fetch_feed_entries(src["url"])
    except Exception as e:
        logging.warning("Failed to fetch feed %s: %s", src["url"], e)
        return 0

    for e in entries:
        if posts_left <= 0:
            break

        url = e.get("url")
        if not url:
            continue

        # night mode: don't post
        if in_night_mode():
            logging.info("Night mode active — skipping posting for now")
            break

        # global daily limit
        sent_last24 = await count_sent_last_24h()
        remaining_today = max(0, DAILY_MAX_POSTS - sent_last24)
        if remaining_today <= 0:
            logging.info("Daily limit reached (%s posts in last 24h). Stopping for this run.", sent_last24)
            break

        # effective allowed this iteration
        effective_allowed = min(posts_left, remaining_today, MAX_POSTS_PER_RUN)
        if effective_allowed <= 0:
            break

        if await already_sent(url):
            continue

        domain = domain_from_url(url)
        domain_count = await count_sent_by_domain_last_24h(domain)
        if domain_count >= DOMAIN_MAX_PER_24H:
            logging.info("Skipping %s because domain %s already posted %s times in 24h", url, domain, domain_count)
            continue

        title = clean_text(e.get("title", "")) or url
        summary = clean_text(e.get("summary", ""))

        if summary and summary.lower() in title.lower():
            summary = ""

        if len(summary) > 300:
            summary = summary[:297] + "..."

        tag = src.get("tag") or "IT"

        title_html = escape_html_text(title)
        summary_html = escape_html_text(summary)
        tag_html = escape_html_text(tag)

        msg = f"<b>[{tag_html}] {title_html}</b>"
        if summary_html:
            msg += f"\n\n{summary_html}"
        msg += f'\n\n<a href="{escape_html_text(url)}">source</a>'

        ok = await safe_send_html(msg)
        if ok:
            # mark after successful send
            await mark_sent(url, title)
            sent += 1
            posts_left -= 1
            await asyncio.sleep(MIN_DELAY_BETWEEN_POSTS)
        else:
            await asyncio.sleep(5)

    return sent

async def main_job():
    logging.info("Job started")
    sources = load_sources_from_yaml(SOURCES_FILE)
    if not sources:
        logging.warning("No sources loaded from %s", SOURCES_FILE)
        return

    if in_night_mode():
        logging.info("Night mode active — skipping job run")
        return

    sent_last24 = await count_sent_last_24h()
    remaining_today = max(0, DAILY_MAX_POSTS - sent_last24)
    if remaining_today <= 0:
        logging.info("Daily target already reached (%s posts in last 24h). Skipping run.", sent_last24)
        return

    posts_remaining = min(MAX_POSTS_PER_RUN, remaining_today)

    for s in sources:
        if posts_remaining <= 0:
            break
        try:
            sent = await process_source(s, posts_remaining)
            posts_remaining -= sent
        except Exception as ex:
            logging.exception("Error processing %s: %s", s.get("url"), ex)

    logging.info("Job finished; posts remaining (this run): %s", posts_remaining)

# ---------------- Runner ----------------
async def start_loop():
    if not BOT_TOKEN or not CHAT_ID:
        logging.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        raise SystemExit(1)
    await init_db()
    # initial run immediately
    await main_job()
    # periodic loop
    while True:
        await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)
        await main_job()

if __name__ == "__main__":
    try:
        asyncio.run(start_loop())
    except KeyboardInterrupt:
        pass
