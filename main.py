import os
import re
import json
import time
import random
import signal
import sys
import sqlite3
import logging
import argparse
import boto3
import urllib.parse
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# Load environment variables
load_dotenv()

# --- CONFIGURATION ---
IG_USERNAME = os.getenv('IG_USERNAME', '')
IG_SESSIONID = os.getenv('IG_SESSIONID', '')
IG_CSRFTOKEN = os.getenv('IG_CSRFTOKEN', '')
IG_USER_ID = os.getenv('IG_USER_ID', '')

# AWS Credentials
AWS_ACCESS_KEY = os.getenv('AWS_ACCESS_KEY_ID', '')
AWS_SECRET_KEY = os.getenv('AWS_SECRET_ACCESS_KEY', '')
AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')

# Email Configuration (SMTP)
SENDER_EMAIL = os.getenv('SENDER_EMAIL', '')
RECEIVER_EMAIL = os.getenv('RECEIVER_EMAIL', '')
SMTP_USER = os.getenv('SMTP_USER', '')
SMTP_PASS = os.getenv('SMTP_PASS', '')
SMTP_HOST = os.getenv('SMTP_HOST', 'email-smtp.us-east-1.amazonaws.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))

# Sweep & quarantine settings
SWEEP_INTERVAL = int(os.getenv('SWEEP_INTERVAL_MINUTES', '15')) * 60
AUTO_DELETE_HOURS = int(os.getenv('AUTO_DELETE_HOURS', '4'))

# Cache & database
CACHE_FILE = "processed_comments.json"
MAX_CACHE_SIZE = 5000
DB_FILE = "moderator.db"
KEYWORDS_FILE = "keywords.txt"

# --- LOGGING SETUP ---
logger = logging.getLogger('ig_moderator')
logger.setLevel(logging.INFO)

file_handler = RotatingFileHandler('moderator.log', maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
logger.addHandler(console_handler)

# --- EMOJI DETECTION ---
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "\U0000FE0F"             # variation selector
    "\U0000200D"             # zero width joiner
    "\U00002600-\U000026FF"  # misc symbols
    "\U00002700-\U000027BF"  # dingbats
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "]+", re.UNICODE
)

# --- SAFE RELIGIOUS PHRASES (Tier 2 — skip AI for these) ---
SAFE_PHRASES = [
    "masha allah", "mashallah", "ma sha allah",
    "alhamdulillah", "alhumdulillah",
    "jazakallah", "jazak allah",
    "subhanallah", "subhan allah",
    "inshallah", "insha allah", "inshaallah",
    "ameen", "aameen", "amin",
    "walaikum assalam", "wa alaikum assalam", "walaikum salam",
    "assalamualaikum", "assalamu alaikum", "salam",
    "wa rahmatullahi", "wa barakatuhu",
    "barakallah", "barak allah",
    "allahu akbar",
    "ramadan mubarak", "ramzan mubarak",
    "eid mubarak",
    "mubarak ho", "mubarakbaad",
]

# --- DATABASE SETUP ---

def init_database():
    """Create the SQLite database and tables if they don't exist."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quarantined_comments (
            comment_id TEXT PRIMARY KEY,
            post_shortcode TEXT NOT NULL,
            media_id TEXT NOT NULL,
            author TEXT NOT NULL,
            comment_text TEXT NOT NULL,
            reason TEXT NOT NULL,
            confidence TEXT NOT NULL,
            tier TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'quarantined',
            flagged_at TIMESTAMP NOT NULL,
            auto_delete_at TIMESTAMP NOT NULL,
            reviewed_at TIMESTAMP,
            reviewed_action TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id TEXT NOT NULL,
            post_shortcode TEXT NOT NULL,
            author TEXT NOT NULL,
            comment_text TEXT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL,
            tier TEXT NOT NULL,
            acted_at TIMESTAMP NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sweep_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TIMESTAMP NOT NULL,
            posts_checked INTEGER DEFAULT 0,
            comments_new INTEGER DEFAULT 0,
            tier1_keyword_delete INTEGER DEFAULT 0,
            tier1_keyword_quarantine INTEGER DEFAULT 0,
            tier2_safe_skipped INTEGER DEFAULT 0,
            tier3_ai_calls INTEGER DEFAULT 0,
            tier3_ai_quarantined INTEGER DEFAULT 0,
            auto_deleted INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()
    logger.info("Database initialized.")

# --- KEYWORD LOADING ---

def load_keywords():
    """Load keywords from keywords.txt, split into instant_delete and quarantine lists."""
    instant_delete = []
    quarantine = []

    if not os.path.exists(KEYWORDS_FILE):
        logger.warning(f"{KEYWORDS_FILE} not found! No keywords loaded.")
        return instant_delete, quarantine

    current_section = None
    with open(KEYWORDS_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                # Detect section headers from comments
                if 'INSTANT DELETE' in line.upper():
                    current_section = 'instant_delete'
                elif 'QUARANTINE' in line.upper():
                    current_section = 'quarantine'
                continue
            if current_section == 'instant_delete':
                instant_delete.append(line.lower())
            elif current_section == 'quarantine':
                quarantine.append(line.lower())

    logger.info(f"Keywords loaded: {len(instant_delete)} instant-delete, {len(quarantine)} quarantine")
    return instant_delete, quarantine

# --- CACHE ---

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r') as f:
            try:
                return set(json.load(f))
            except (json.JSONDecodeError, TypeError, ValueError):
                logger.warning("Cache file corrupted, starting fresh.")
                return set()
    return set()

def save_cache(cache):
    trimmed = list(cache)
    if len(trimmed) > MAX_CACHE_SIZE:
        trimmed = trimmed[-MAX_CACHE_SIZE:]
    with open(CACHE_FILE, 'w') as f:
        json.dump(trimmed, f)

# --- AWS BEDROCK CLIENT ---
bedrock = boto3.client(
    service_name='bedrock-runtime',
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY
)

# ============================================================
#  TIER 2 — LOCAL SAFE-CHECK (Free, instant)
# ============================================================

def is_locally_safe(text):
    """
    Tier 2: Quick local checks to determine if a comment is obviously safe.
    Returns True if the comment should SKIP the AI call entirely.
    """
    stripped = text.strip()

    # 1. Too short to analyze meaningfully
    if len(stripped) < 4:
        return True

    # 2. Emoji-only comment (hearts, fire, claps, etc.)
    text_without_emoji = EMOJI_PATTERN.sub('', stripped).strip()
    if len(text_without_emoji) == 0:
        return True

    # 3. Common safe religious expressions (duas, greetings)
    text_lower = stripped.lower()
    for phrase in SAFE_PHRASES:
        if text_lower == phrase or text_lower.startswith(phrase):
            # Make sure there's no negative twist after the greeting
            remainder = text_lower[len(phrase):].strip()
            if not remainder or not any(neg in remainder for neg in ['but', 'lekin', 'magar', 'par ', 'not', 'however']):
                return True

    # 4. Pure @mention tags (e.g., "@friend1 @friend2 check this")
    without_mentions = re.sub(r'@\w+', '', stripped).strip()
    if len(without_mentions) == 0:
        return True

    # 5. Single-word positive reactions
    single_word_safe = {
        'beautiful', 'amazing', 'wow', 'gorgeous', 'love', 'lovely',
        'nice', 'great', 'awesome', 'perfect', 'best', 'superb',
        'congratulations', 'congrats', 'thanks', 'thankyou', 'shukriya',
        'zabardast', 'kamaal', 'lajawab', 'shandar', 'behtareen',
    }
    if text_lower in single_word_safe:
        return True

    # Not obviously safe — needs further analysis
    return False

# ============================================================
#  TIER 3 — AI SENTIMENT ANALYSIS (Paid, slower)
# ============================================================

def check_ai_sentiment(text):
    """
    Tier 3: Uses AWS Bedrock (Minimax) with few-shot examples for accurate classification.
    Only called for comments that pass Tier 1 (no keyword) and Tier 2 (not obviously safe).
    """
    prompt = (
        "You are an AI Instagram moderator for the 'Daawat-e-Ramzaan' expo.\n"
        "Analyze the comment below and determine if it should be FLAGGED for removal.\n\n"
        "FLAG (YES) if the comment is:\n"
        "- Hateful (insults, slurs, personal attacks)\n"
        "- Harassing (targeting individuals or the event organizers)\n"
        "- Scam/Bot Spam (crypto, fake followers, promotional spam)\n"
        "- Religious Shaming / Moral Policing (guilt-tripping the event for entertainment during Ramadan)\n"
        "- Negative remarks that undermine, discredit, or discourage the event\n\n"
        "KEEP (NO) if the comment is:\n"
        "- A genuine question about the event\n"
        "- Valid logistics complaint (queue, parking, food)\n"
        "- Positive feedback or religious expression\n"
        "- Neutral observation\n\n"
        "--- EXAMPLES ---\n"
        "Comment: 'Astagfirullah Ramzan ke Naam per Khel Tamasha Gana'\n"
        "Answer: YES (religious shaming — guilt-tripping the event)\n\n"
        "Comment: 'Entry fee kitni hai?'\n"
        "Answer: NO (genuine question about the event)\n\n"
        "Comment: 'Follow me for free iPhone 📱 click link in bio'\n"
        "Answer: YES (spam/bot comment)\n\n"
        "Comment: 'Queue was so long but food was absolutely amazing 🔥'\n"
        "Answer: NO (valid feedback with praise)\n\n"
        "Comment: 'Ye sab haram hai sharam karo Ramzan mein ye khel tamasha'\n"
        "Answer: YES (moral policing — shaming the event)\n\n"
        "Comment: 'Masha Allah bohot acha event tha, will come again next year'\n"
        "Answer: NO (positive feedback)\n\n"
        "Comment: 'What a waste of money, complete fraud expo bakwas'\n"
        "Answer: YES (hateful, undermining the event)\n\n"
        "Comment: 'Parking was a nightmare but stalls were really good'\n"
        "Answer: NO (valid complaint with positive note)\n"
        "--- END EXAMPLES ---\n\n"
        f"Comment: '{text}'\n"
        "Answer ONLY 'YES' or 'NO'."
    )

    try:
        response = bedrock.converse(
            modelId='minimax.minimax-m2.1',
            messages=[{"role": "user", "content": [{"text": prompt}]}]
        )
        reply = ""
        for part in response['output']['message']['content']:
            if 'text' in part:
                reply += part['text']
        result = "YES" in reply.strip().upper()
        logger.info(f"      AI verdict: {'FLAG' if result else 'SAFE'}")
        return result
    except Exception as e:
        logger.error(f"Bedrock error: {e}")
        return False

# ============================================================
#  QUARANTINE DATABASE OPERATIONS
# ============================================================

def quarantine_comment(comment_id, post_shortcode, media_id, author, text, reason, confidence, tier):
    """Add a comment to the quarantine database."""
    conn = sqlite3.connect(DB_FILE)
    now = datetime.now()
    auto_delete_at = now + timedelta(hours=AUTO_DELETE_HOURS)

    try:
        conn.execute("""
            INSERT OR REPLACE INTO quarantined_comments
            (comment_id, post_shortcode, media_id, author, comment_text, reason, confidence, tier, status, flagged_at, auto_delete_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'quarantined', ?, ?)
        """, (comment_id, post_shortcode, media_id, author, text, reason, confidence, tier, now.isoformat(), auto_delete_at.isoformat()))
        conn.commit()
        logger.info(f"    📋 Quarantined (auto-delete in {AUTO_DELETE_HOURS}h)")
    except Exception as e:
        logger.error(f"Database error during quarantine: {e}")
    finally:
        conn.close()

def log_action(comment_id, post_shortcode, author, text, action, reason, tier):
    """Log a deletion or approval to the permanent audit log."""
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute("""
            INSERT INTO action_log (comment_id, post_shortcode, author, comment_text, action, reason, tier, acted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (comment_id, post_shortcode, author, text, action, reason, tier, datetime.now().isoformat()))
        conn.commit()
    except Exception as e:
        logger.error(f"Database error during action log: {e}")
    finally:
        conn.close()

def log_sweep(stats):
    """Log sweep statistics to the database."""
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute("""
            INSERT INTO sweep_logs (started_at, posts_checked, comments_new, tier1_keyword_delete,
                tier1_keyword_quarantine, tier2_safe_skipped, tier3_ai_calls, tier3_ai_quarantined,
                auto_deleted, errors)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(),
            stats.get('posts', 0), stats.get('new', 0),
            stats.get('t1_delete', 0), stats.get('t1_quarantine', 0),
            stats.get('t2_safe', 0), stats.get('t3_calls', 0),
            stats.get('t3_quarantine', 0), stats.get('auto_deleted', 0),
            stats.get('errors', 0)
        ))
        conn.commit()
    except Exception as e:
        logger.error(f"Database error logging sweep: {e}")
    finally:
        conn.close()

def process_auto_deletes(browser_context):
    """Delete quarantined comments that have passed the review window."""
    conn = sqlite3.connect(DB_FILE)
    now = datetime.now().isoformat()
    deleted_count = 0

    try:
        cursor = conn.execute("""
            SELECT comment_id, post_shortcode, media_id, author, comment_text, reason, tier
            FROM quarantined_comments
            WHERE status = 'quarantined' AND auto_delete_at <= ?
        """, (now,))

        expired = cursor.fetchall()
        if not expired:
            return 0

        logger.info(f"Processing {len(expired)} expired quarantined comments for auto-deletion...")

        for row in expired:
            comment_id, shortcode, media_id, author, text, reason, tier = row
            logger.info(f"  Auto-deleting {comment_id} from @{author}: {text[:40]}...")

            if delete_comment(browser_context, media_id, comment_id):
                conn.execute("UPDATE quarantined_comments SET status = 'deleted', reviewed_at = ?, reviewed_action = 'auto_deleted' WHERE comment_id = ?",
                             (now, comment_id))
                log_action(comment_id, shortcode, author, text, 'auto_deleted', reason, tier)
                send_email_alert(shortcode, text, author, f"{reason} (auto-deleted after {AUTO_DELETE_HOURS}h)")
                deleted_count += 1
                logger.info(f"  ✓ Auto-deleted successfully")
            else:
                logger.error(f"  ✗ Auto-deletion FAILED for {comment_id}")

        conn.commit()
    except Exception as e:
        logger.error(f"Auto-delete processing error: {e}")
    finally:
        conn.close()

    return deleted_count

def get_pending_quarantined():
    """Get all comments currently in quarantine awaiting review."""
    conn = sqlite3.connect(DB_FILE)
    try:
        cursor = conn.execute("""
            SELECT comment_id, post_shortcode, media_id, author, comment_text, reason, confidence, tier, flagged_at, auto_delete_at
            FROM quarantined_comments
            WHERE status = 'quarantined'
            ORDER BY flagged_at DESC
        """)
        return cursor.fetchall()
    finally:
        conn.close()

# ============================================================
#  EMAIL NOTIFICATIONS
# ============================================================

def send_email_alert(shortcode, comment_text, username, reason):
    """Sends an email notification via SMTP when a comment is deleted."""
    subject = f"IG MODERATOR: Comment Deleted on {shortcode}"
    link = f"https://www.instagram.com/p/{shortcode}/"
    body = (
        f"The following comment was deleted from @{IG_USERNAME}'s post ({link}):\n\n"
        f"User: @{username}\n"
        f"Comment: {comment_text}\n"
        f"Reason: {reason}\n\n"
        f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = SENDER_EMAIL
    msg['To'] = RECEIVER_EMAIL

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
    except Exception as e:
        logger.error(f"SMTP error: {e}")

def send_quarantine_digest():
    """Send a digest email of all pending quarantined comments."""
    pending = get_pending_quarantined()
    if not pending:
        return

    subject = f"IG MODERATOR: {len(pending)} Comments Awaiting Review"
    lines = [
        f"There are {len(pending)} quarantined comments awaiting your review.\n",
        f"They will be AUTO-DELETED after {AUTO_DELETE_HOURS} hours if not reviewed.\n",
        "To review, run: python main.py --review\n",
        "=" * 60 + "\n"
    ]

    for row in pending:
        comment_id, shortcode, media_id, author, text, reason, confidence, tier, flagged_at, auto_delete_at = row
        link = f"https://www.instagram.com/p/{shortcode}/"
        lines.append(
            f"Post: {link}\n"
            f"Author: @{author}\n"
            f"Comment: {text}\n"
            f"Reason: {reason} ({confidence} confidence, {tier})\n"
            f"Flagged at: {flagged_at}\n"
            f"Auto-delete at: {auto_delete_at}\n"
            + "-" * 40 + "\n"
        )

    body = "\n".join(lines)
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = SENDER_EMAIL
    msg['To'] = RECEIVER_EMAIL

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        logger.info(f"Quarantine digest sent: {len(pending)} pending comments")
    except Exception as e:
        logger.error(f"SMTP error sending digest: {e}")

def send_session_expiry_alert():
    """Sends an urgent email alert when the Instagram session has expired."""
    subject = "🚨 IG MODERATOR: SESSION EXPIRED — ACTION REQUIRED"
    body = (
        f"The Instagram session for @{IG_USERNAME} has EXPIRED.\n\n"
        "The bot is unable to access the Instagram API and moderation has STOPPED.\n\n"
        "To fix this:\n"
        "1. Log into Instagram in your browser\n"
        "2. Extract the new 'sessionid' and 'csrftoken' cookies\n"
        "3. Update the IG_SESSIONID and IG_CSRFTOKEN values in your .env file\n"
        "4. Restart the bot\n\n"
        f"Time of failure: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = SENDER_EMAIL
    msg['To'] = RECEIVER_EMAIL

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        logger.info("Session expiry alert email sent.")
    except Exception as e:
        logger.error(f"SMTP error while sending session expiry alert: {e}")

# ============================================================
#  INSTAGRAM API
# ============================================================

def delete_comment(browser_context, media_id, comment_id):
    """Deletes a comment using Instagram Web API via Playwright."""
    urls = [
        f"https://www.instagram.com/api/v1/web/comments/{media_id}/delete/{comment_id}/",
        f"https://www.instagram.com/api/v1/comments/{media_id}/delete/"
    ]

    for url in urls:
        try:
            if "web" not in url:
                res = browser_context.request.post(
                    url,
                    headers={
                        'X-CSRFToken': IG_CSRFTOKEN,
                        'X-IG-App-ID': '936619743392459',
                        'X-Requested-With': 'XMLHttpRequest',
                        'Referer': 'https://www.instagram.com/',
                    },
                    data=f"comment_ids_to_delete={comment_id}"
                )
            else:
                res = browser_context.request.post(
                    url,
                    headers={
                        'X-CSRFToken': IG_CSRFTOKEN,
                        'X-IG-App-ID': '936619743392459',
                        'X-Requested-With': 'XMLHttpRequest',
                        'Referer': 'https://www.instagram.com/',
                    }
                )

            if res.status == 200:
                result = res.json()
                if result.get('status') == 'ok':
                    return True
            logger.warning(f"Delete endpoint failed {url}: HTTP {res.status}")
        except Exception as e:
            logger.error(f"Delete request error for {url}: {e}")

    return False

# ============================================================
#  MAIN MODERATION ENGINE (3-TIER PIPELINE)
# ============================================================

def scrape_and_moderate(browser_context, cache, kw_instant, kw_quarantine):
    """
    Main moderation loop with 3-tier pipeline:
      Tier 1: Keyword match (instant, free) → delete or quarantine
      Tier 2: Local safe-check (instant, free) → skip AI if obviously safe
      Tier 3: AI sentiment analysis (paid, slower) → quarantine if flagged
    """
    stats = {
        "posts": 0, "new": 0,
        "t1_delete": 0, "t1_quarantine": 0,
        "t2_safe": 0,
        "t3_calls": 0, "t3_quarantine": 0,
        "auto_deleted": 0, "errors": 0
    }

    try:
        # --- Process auto-deletes from previous quarantines ---
        stats["auto_deleted"] = process_auto_deletes(browser_context)

        logger.info("Fetching latest feed...")
        feed_url = f"https://www.instagram.com/api/v1/feed/user/{IG_USER_ID}/"
        res = browser_context.request.get(feed_url, headers={'X-IG-App-ID': '936619743392459'})

        if res.status == 401:
            logger.critical("SESSION EXPIRED! Instagram returned 401.")
            send_session_expiry_alert()
            return "SESSION_EXPIRED", stats

        if res.status == 429:
            wait_time = random.randint(300, 600)
            logger.warning(f"Rate limited (429)! Backing off {wait_time}s...")
            time.sleep(wait_time)
            return "RATE_LIMITED", stats

        if res.status != 200:
            logger.error(f"Feed error: HTTP {res.status}")
            stats["errors"] += 1
            return "ERROR", stats

        items = res.json().get('items', [])[:10]
        stats["posts"] = len(items)
        logger.info(f"Checking {len(items)} posts...")

        for item in items:
            media_id = item['pk']
            shortcode = item['code']
            logger.info(f"  Post: {shortcode}")

            variables = json.dumps({"shortcode": shortcode, "first": 50})
            url = f"https://www.instagram.com/graphql/query/?query_hash=97b41c52301f77ce508f55e66d17620e&variables={urllib.parse.quote(variables)}"

            c_res = browser_context.request.get(
                url,
                headers={'X-IG-App-ID': '936619743392459', 'Referer': f'https://www.instagram.com/p/{shortcode}/'}
            )

            if c_res.status == 429:
                logger.warning(f"Rate limited on comments for {shortcode}.")
                return "RATE_LIMITED", stats

            if c_res.status != 200:
                if c_res.status == 400:
                    logger.error(f"GraphQL hash may be rotated (HTTP 400 on {shortcode}). Update query_hash!")
                else:
                    logger.warning(f"Comments fetch failed for {shortcode}: HTTP {c_res.status}")
                stats["errors"] += 1
                continue

            try:
                c_data = c_res.json()
                edges = c_data['data']['shortcode_media']['edge_media_to_parent_comment']['edges']

                for edge in edges:
                    node = edge['node']
                    comment_id = str(node['id'])

                    if comment_id in cache:
                        continue

                    stats["new"] += 1
                    text = node['text']
                    author = node['owner']['username']
                    text_lower = text.lower()

                    # Skip own comments entirely
                    if author == IG_USERNAME:
                        cache.add(comment_id)
                        continue

                    logger.info(f"    @{author}: {text[:60]}")

                    # =============================================
                    #  TIER 1: KEYWORD MATCH (instant, free)
                    # =============================================
                    matched_keyword = None
                    action = None

                    # Check instant-delete keywords first
                    for kw in kw_instant:
                        if kw in text_lower:
                            matched_keyword = kw
                            action = "instant_delete"
                            break

                    # Check quarantine keywords
                    if not matched_keyword:
                        for kw in kw_quarantine:
                            if kw in text_lower:
                                matched_keyword = kw
                                action = "quarantine"
                                break

                    if matched_keyword:
                        reason = f"Keyword: '{matched_keyword}'"
                        if action == "instant_delete":
                            # Tier 1 hard delete — unambiguous profanity
                            stats["t1_delete"] += 1
                            logger.warning(f"    🔴 INSTANT DELETE — {reason}")
                            if delete_comment(browser_context, media_id, comment_id):
                                log_action(comment_id, shortcode, author, text, 'deleted', reason, 'tier1')
                                send_email_alert(shortcode, text, author, reason)
                                logger.info(f"    ✓ Deleted instantly")
                            else:
                                stats["errors"] += 1
                                logger.error(f"    ✗ Deletion FAILED")
                        else:
                            # Tier 1 quarantine — context-dependent keyword
                            stats["t1_quarantine"] += 1
                            logger.warning(f"    🟡 QUARANTINED — {reason}")
                            quarantine_comment(comment_id, shortcode, str(media_id), author, text, reason, 'high', 'tier1')

                        cache.add(comment_id)
                        continue

                    # =============================================
                    #  TIER 2: LOCAL SAFE-CHECK (instant, free)
                    # =============================================
                    if is_locally_safe(text):
                        stats["t2_safe"] += 1
                        logger.info(f"      ✅ Tier 2: Locally safe — skipping AI")
                        cache.add(comment_id)
                        continue

                    # =============================================
                    #  TIER 3: AI ANALYSIS (paid, slow)
                    # =============================================
                    stats["t3_calls"] += 1
                    logger.info(f"      🤖 Tier 3: Sending to AI...")

                    if check_ai_sentiment(text):
                        stats["t3_quarantine"] += 1
                        reason = "AI categorized as Negative"
                        logger.warning(f"    🟡 QUARANTINED — {reason}")
                        quarantine_comment(comment_id, shortcode, str(media_id), author, text, reason, 'medium', 'tier3')
                    else:
                        logger.info(f"      ✅ AI: Safe")

                    cache.add(comment_id)

                # Save cache after each post
                save_cache(cache)

            except KeyError as e:
                logger.error(f"Unexpected response structure for {shortcode}: missing key {e}")
                stats["errors"] += 1
            except Exception as e:
                logger.error(f"Post {shortcode} error: {e}")
                stats["errors"] += 1

    except Exception as e:
        logger.error(f"Moderator error: {e}")
        stats["errors"] += 1

    # --- Sweep summary ---
    logger.info("─" * 50)
    logger.info(f"SWEEP SUMMARY:")
    logger.info(f"  Posts checked:           {stats['posts']}")
    logger.info(f"  New comments:            {stats['new']}")
    logger.info(f"  Tier 1 — Instant delete: {stats['t1_delete']}")
    logger.info(f"  Tier 1 — Quarantined:    {stats['t1_quarantine']}")
    logger.info(f"  Tier 2 — Safe (no AI):   {stats['t2_safe']}")
    logger.info(f"  Tier 3 — AI calls:       {stats['t3_calls']}")
    logger.info(f"  Tier 3 — AI quarantine:  {stats['t3_quarantine']}")
    logger.info(f"  Auto-deleted:            {stats['auto_deleted']}")
    logger.info(f"  Errors:                  {stats['errors']}")
    logger.info("─" * 50)

    # AI cost savings estimate
    total_could_have_called = stats['new']
    actual_ai_calls = stats['t3_calls']
    saved = total_could_have_called - actual_ai_calls
    if total_could_have_called > 0:
        pct = (saved / total_could_have_called) * 100
        logger.info(f"  💰 AI calls saved: {saved}/{total_could_have_called} ({pct:.0f}% cost reduction)")

    log_sweep(stats)

    # Send digest if there are pending quarantined comments
    pending = get_pending_quarantined()
    if pending:
        send_quarantine_digest()

    return "OK", stats

# ============================================================
#  CLI REVIEW MODE
# ============================================================

def review_quarantine():
    """Interactive CLI to review quarantined comments."""
    pending = get_pending_quarantined()

    if not pending:
        print("\n✅ No comments in quarantine. All clear!\n")
        return

    print(f"\n{'=' * 60}")
    print(f"  📋 QUARANTINE REVIEW — {len(pending)} comments pending")
    print(f"{'=' * 60}\n")

    # We need a browser context for deletions
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies([
            {"name": "sessionid", "value": IG_SESSIONID, "domain": ".instagram.com", "path": "/"},
            {"name": "csrftoken", "value": IG_CSRFTOKEN, "domain": ".instagram.com", "path": "/"}
        ])

        for i, row in enumerate(pending):
            comment_id, shortcode, media_id, author, text, reason, confidence, tier, flagged_at, auto_delete_at = row
            link = f"https://www.instagram.com/p/{shortcode}/"

            print(f"  [{i+1}/{len(pending)}]")
            print(f"  Post:       {link}")
            print(f"  Author:     @{author}")
            print(f"  Comment:    {text}")
            print(f"  Reason:     {reason}")
            print(f"  Confidence: {confidence} ({tier})")
            print(f"  Flagged:    {flagged_at}")
            print(f"  Auto-delete: {auto_delete_at}")
            print()

            while True:
                choice = input("  [D]elete  /  [K]eep (approve)  /  [S]kip  /  [Q]uit  > ").strip().lower()
                if choice in ('d', 'k', 's', 'q'):
                    break
                print("  Invalid choice. Enter D, K, S, or Q.")

            conn = sqlite3.connect(DB_FILE)
            now = datetime.now().isoformat()

            if choice == 'd':
                if delete_comment(context, media_id, comment_id):
                    conn.execute("UPDATE quarantined_comments SET status = 'deleted', reviewed_at = ?, reviewed_action = 'manual_delete' WHERE comment_id = ?", (now, comment_id))
                    log_action(comment_id, shortcode, author, text, 'manual_delete', reason, tier)
                    print("  ✓ Deleted.\n")
                else:
                    print("  ✗ Deletion failed. Comment may already be gone.\n")
                    conn.execute("UPDATE quarantined_comments SET status = 'delete_failed', reviewed_at = ? WHERE comment_id = ?", (now, comment_id))

            elif choice == 'k':
                conn.execute("UPDATE quarantined_comments SET status = 'approved', reviewed_at = ?, reviewed_action = 'approved' WHERE comment_id = ?", (now, comment_id))
                log_action(comment_id, shortcode, author, text, 'approved', reason, tier)
                print("  ✓ Approved — comment will be kept.\n")

            elif choice == 'q':
                conn.commit()
                conn.close()
                print("\n  Review ended. Remaining comments still in quarantine.\n")
                browser.close()
                return

            # 's' = skip, do nothing
            conn.commit()
            conn.close()
            print("-" * 40)

        browser.close()

    print(f"\n✅ Review complete.\n")

# ============================================================
#  MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Instagram AI Moderator')
    parser.add_argument('--review', action='store_true', help='Review quarantined comments interactively')
    parser.add_argument('--stats', action='store_true', help='Show recent sweep statistics')
    args = parser.parse_args()

    init_database()

    if args.review:
        review_quarantine()
        return

    if args.stats:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.execute("SELECT * FROM sweep_logs ORDER BY id DESC LIMIT 10")
        rows = cursor.fetchall()
        if not rows:
            print("No sweep logs yet.")
        else:
            print(f"\n{'=' * 80}")
            print(f"  Last {len(rows)} sweeps")
            print(f"{'=' * 80}")
            for row in rows:
                print(f"  {row[1]} | Posts: {row[2]} | New: {row[3]} | T1-Del: {row[4]} | T1-Q: {row[5]} | T2-Safe: {row[6]} | AI: {row[7]} | AI-Q: {row[8]} | Auto-Del: {row[9]} | Err: {row[10]}")
            print()

        pending = get_pending_quarantined()
        print(f"  📋 Currently in quarantine: {len(pending)} comments")
        conn.close()
        return

    # --- Normal bot mode ---
    kw_instant, kw_quarantine = load_keywords()

    logger.info("=== Starting Instagram AI Moderator (v2 — Quarantine + Smart Pipeline) ===")
    logger.info(f"Target Account: @{IG_USERNAME}")
    logger.info(f"Sweep interval: {SWEEP_INTERVAL // 60} minutes")
    logger.info(f"Auto-delete window: {AUTO_DELETE_HOURS} hours")
    logger.info(f"Cache limit: {MAX_CACHE_SIZE} entries")
    logger.info(f"Keywords: {len(kw_instant)} instant-delete, {len(kw_quarantine)} quarantine")

    cache = load_cache()
    logger.info(f"Loaded {len(cache)} cached comment IDs")

    browser = None

    def shutdown_handler(sig, frame):
        logger.info("Shutdown signal received. Saving cache and cleaning up...")
        save_cache(cache)
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        logger.info("Goodbye!")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies([
            {"name": "sessionid", "value": IG_SESSIONID, "domain": ".instagram.com", "path": "/"},
            {"name": "csrftoken", "value": IG_CSRFTOKEN, "domain": ".instagram.com", "path": "/"}
        ])

        consecutive_errors = 0

        while True:
            logger.info(f"--- Waking up: Starting sweep ---")
            result, stats = scrape_and_moderate(context, cache, kw_instant, kw_quarantine)

            if result == "SESSION_EXPIRED":
                logger.critical("Bot stopping due to expired session.")
                break

            if result == "RATE_LIMITED":
                consecutive_errors += 1
                backoff = min(SWEEP_INTERVAL * (2 ** consecutive_errors), 3600)
                logger.warning(f"Exponential backoff: sleeping {backoff}s (attempt #{consecutive_errors})")
                time.sleep(backoff)
                continue

            if result == "ERROR":
                consecutive_errors += 1
            else:
                consecutive_errors = 0

            logger.info(f"Sleeping for {SWEEP_INTERVAL // 60} minutes...")
            time.sleep(SWEEP_INTERVAL)

if __name__ == "__main__":
    main()
