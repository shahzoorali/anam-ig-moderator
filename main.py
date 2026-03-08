import os
import json
import time
import smtplib
from email.message import EmailMessage
import boto3
from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import LoginRequired

if os.path.exists(".env.local"):
    load_dotenv(".env.local")
else:
    load_dotenv()

IG_USERNAME = os.getenv("IG_USERNAME", "")
IG_PASSWORD = os.getenv("IG_PASSWORD", "")
IG_SESSIONID = os.getenv("IG_SESSIONID", "")
IG_CSRFTOKEN = os.getenv("IG_CSRFTOKEN", "")

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_HOST = os.getenv("SMTP_HOST", "email-smtp.us-east-1.amazonaws.com")
SMTP_PORT = os.getenv("SMTP_PORT", "587")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL", "")

# Bedrock Minimax setup
# The Minimax M2.1 model ID on Bedrock is typically "minimax.text-v1" or similar
# Ensure your EC2 Instance Profile has 'bedrock:InvokeModel' IAM permission.
MODEL_ID = "minimax.text-v1" 

KEYWORDS = ["boycottexpo", "boycotexpo", "banexpo", "bannexpo", "boycott"]
CACHE_FILE = "processed_comments.json"
SESSION_FILE = "session.json"

bedrock_client = boto3.client(
    'bedrock-runtime', 
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY
)

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(list(cache), f)

def send_email_alert(media, comment, reason):
    msg = EmailMessage()
    msg['Subject'] = 'IG Comment Deleted Notification'
    msg['From'] = SENDER_EMAIL
    msg['To'] = RECEIVER_EMAIL
    
    body = f"""
The Moderation Bot automatically deleted a comment from your Instagram account.

Reason for deletion: {reason}
Time: {comment.created_at_utc}
Post Link: https://instagram.com/p/{media.code}/

Comment Details:
Username: {comment.user.username}
Comment Text: "{comment.text}"
"""
    msg.set_content(body)
    
    try:
        with smtplib.SMTP(SMTP_HOST, int(SMTP_PORT)) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Email alert sent to {RECEIVER_EMAIL}")
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Failed to send email alert: {e}")

def check_ai_sentiment(text):
    prompt = f"Evaluate the following text and determine if it is highly negative, abusive, or spammy. Answer strictly with 'YES' if it is negative/abusive/spammy, or 'NO' if it is acceptable.\nText: \"{text}\""
    
    try:
        # We use Converse API which handles standard payload formats across models
        response = bedrock_client.converse(
            modelId=MODEL_ID,
            messages=[
                {
                    "role": "user",
                    "content": [{"text": prompt}]
                }
            ]
        )
        reply_text = response['output']['message']['content'][0]['text']
        return "YES" in reply_text.strip().upper()
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Bedrock Error for text '{text}': {e}")
        return False

def check_and_delete_comments(cl, cache):
    try:
        # Fetching recent posts/reels (5 recent posts)
        recent_media = cl.user_medias(cl.user_id, amount=5)
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Error fetching recent posts: {e}")
        return

    for media in recent_media:
        try:
            comments = cl.media_comments(media.pk)
        except Exception as e:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Error fetching comments for media {media.pk}: {e}")
            continue

        for comment in comments:
            if comment.pk in cache:
                continue

            text_lower = comment.text.lower()
            delete_reason = None
            
            # Phase 1: Keyword match
            if any(kw in text_lower for kw in KEYWORDS):
                delete_reason = f"Keyword Match: Contains a forbidden keyword"
            
            # Phase 2: AI Negative Sentiment detection
            elif comment.user.pk != cl.user_id: # Do not analyze/delete our own comments
                if check_ai_sentiment(comment.text):
                    delete_reason = "AI categorized as Negative/Spammy"
                    
            if delete_reason:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Deleting comment {comment.pk} because: {delete_reason}")
                try:
                    cl.comment_bulk_delete(media.pk, [comment.pk])
                    send_email_alert(media, comment, delete_reason)
                except Exception as del_err:
                    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Failed to delete comment: {del_err}")
            
            # Mark as processed immediately even if deletion failed (to avoid loop) or if valid
            cache.add(comment.pk)
            save_cache(cache)

def login_with_session_or_creds():
    cl = Client()
    # Randomize user agent to look less like a bot
    cl.set_user_agent()
    
    if os.path.exists(SESSION_FILE):
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Found session.json. Logging in using session...")
        try:
            cl.load_settings(SESSION_FILE)
            cl.get_timeline_feed() 
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Session login successful!")
            return cl
        except Exception as e:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] session.json failed: {e}")
            if os.path.exists(SESSION_FILE):
                os.remove(SESSION_FILE)

    if IG_SESSIONID:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Attempting login via Session ID...")
        try:
            cl.login_by_sessionid(IG_SESSIONID)
            # Basic validation
            cl.get_timeline_feed()
            cl.dump_settings(SESSION_FILE)
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Login via Session ID successful!")
            return cl
        except Exception as e:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Session ID login failed: {e}")

    try:
        # Randomize timing and user agent more aggressively
        cl.delay_range = [5, 15]
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Attempting login for {IG_USERNAME}...")
        cl.login(IG_USERNAME, IG_PASSWORD)
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Login failed: {e}")
        print("-" * 50)
        print("CRITICAL: Instagram has blocked this IP address.")
        print("FIX: Run this script ONCE on your personal laptop/PC (not a server).")
        print("1. Copy the 'session.json' it generates.")
        print("2. Upload 'session.json' to this server.")
        print("-" * 50)
        return None
        
    cl.dump_settings(SESSION_FILE)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Login successful! Session saved.")
    return cl

def main():
    if not IG_USERNAME or not IG_PASSWORD:
        print("ERROR: Please configure IG_USERNAME and IG_PASSWORD in .env")
        return

    print("=== Starting Instagram AI Moderator Bot ===")
    cl = login_with_session_or_creds()
    if not cl:
        print("Bot halting unconditionally due to login failure.")
        return

    # To avoid analyzing old comments, let's preload the cache initially if empty.
    # Otherwise, it runs bedock analysis on all existing past comments.
    cache = load_cache()

    while True:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Waking up: Checking new comments...")
        
        # Ensure session is still active
        try:
            check_and_delete_comments(cl, cache)
        except Exception as e:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Unexpected error in loop: {e}")
            
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Sleeping for 15 minutes...")
        time.sleep(15 * 60)

if __name__ == "__main__":
    main()
