import os
import json
import time
import requests
import boto3
import urllib.parse
import smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# Load environment variables
load_dotenv('.env.local')

# --- CONFIGURATION ---
IG_USERNAME = os.getenv('IG_USERNAME')
IG_SESSIONID = os.getenv('IG_SESSIONID')
IG_CSRFTOKEN = os.getenv('IG_CSRFTOKEN')
IG_USER_ID = os.getenv('IG_USER_ID')

# AWS Credentials
AWS_ACCESS_KEY = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')

# Email Configuration (SMTP)
SENDER_EMAIL = os.getenv('SENDER_EMAIL')
RECEIVER_EMAIL = os.getenv('RECEIVER_EMAIL')
SMTP_USER = os.getenv('SMTP_USER')
SMTP_PASS = os.getenv('SMTP_PASS')
SMTP_HOST = os.getenv('SMTP_HOST', 'email-smtp.us-east-1.amazonaws.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', 587))

# Forbidden Keywords
KEYWORDS = ["boycottexpo", "boycott", "hate", "scam", "fake", "block", "fraud", "scammer", "fuck", "bitch"]

# Files
CACHE_FILE = "processed_comments.json"

# --- CLIENTS ---
bedrock = boto3.client(
    service_name='bedrock-runtime',
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY
)

# Shared headers for Instagram Web actions (deletions)
IG_HEADERS = {
    'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    'X-CSRFToken': IG_CSRFTOKEN,
    'Cookie': f'sessionid={IG_SESSIONID}; csrftoken={IG_CSRFTOKEN};',
    'X-IG-App-ID': '936619743392459',
    'X-Requested-With': 'XMLHttpRequest',
    'Referer': 'https://www.instagram.com/',
}

# --- UTILITIES ---

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r') as f:
            try:
                return set(json.load(f))
            except:
                return set()
    return set()

def save_cache(cache):
    with open(CACHE_FILE, 'w') as f:
        json.dump(list(cache), f)

def check_ai_sentiment(text):
    """Uses AWS Bedrock (Minimax) via Converse API to check sentiment."""
    # Safety: Ignore very short or emoji-only comments for AI to save costs/tokens
    if len(text.strip()) < 4: return False
    
    prompt = (
        "Analyze this Instagram comment for the 'Daawat-e-Ramzaan' expo.\n"
        f"Comment: '{text}'\n\n"
        "Guidelines:\n"
        "1. Identify Hinglish/Hindi written in Roman script (e.g., 'bakwas', 'bekar', 'chor', 'loot').\n"
        "2. Flag as YES if the comment contains: HATE, SPAM, HARASSMENT, FRUSTRATION, or NEGATIVE CRITICISM (e.g., calling the event rubbish, a scam, or complaining about prices/management in a toxic way).\n"
        "3. Flag as NO if the comment is a simple question, a food review (even if average), neutral, or positive.\n\n"
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
        return "YES" in reply.strip().upper()
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Bedrock error: {e}")
        return False

def send_email_alert(shortcode, comment_text, username, reason):
    """Sends an email notification via SMTP when a comment is deleted."""
    subject = f"IG MODERATOR: Comment Deleted on {shortcode}"
    link = f"https://www.instagram.com/p/{shortcode}/"
    body = f"The following comment was deleted from @{IG_USERNAME}'s post ({link}):\n\nUser: @{username}\nComment: {comment_text}\nReason: {reason}\n\nTime: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    
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
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] SMTP error: {e}")

def delete_comment(media_id, comment_id):
    """Deletes a comment using the Instagram Web API."""
    url = f"https://www.instagram.com/api/v1/web/comments/{media_id}/delete/{comment_id}/"
    try:
        res = requests.post(url, headers=IG_HEADERS)
        if res.status_code == 200:
            result = res.json()
            if result.get('status') == 'ok':
                return True
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Delete failed for {comment_id}: {res.text[:100]}")
        return False
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Delete request error: {e}")
        return False

def scrape_and_moderate(browser_context, cache):
    """Main moderation logic using Playwright context."""
    try:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Fetching latest feed...")
        feed_url = f"https://www.instagram.com/api/v1/feed/user/{IG_USER_ID}/"
        res = browser_context.request.get(feed_url, headers={'X-IG-App-ID': '936619743392459'})
        
        if res.status != 200:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Feed error: {res.status}")
            return

        items = res.json().get('items', [])[:10]
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Checking {len(items)} items...")

        for item in items:
            media_id = item['pk']
            shortcode = item['code']
            
            # Fetch comments via GraphQL
            variables = json.dumps({"shortcode": shortcode, "first": 50})
            url = f"https://www.instagram.com/graphql/query/?query_hash=97b41c52301f77ce508f55e66d17620e&variables={urllib.parse.quote(variables)}"
            
            c_res = browser_context.request.get(
                url, 
                headers={'X-IG-App-ID': '936619743392459', 'Referer': f'https://www.instagram.com/p/{shortcode}/'}
            )
            
            if c_res.status != 200:
                continue
                
            try:
                c_data = c_res.json()
                edges = c_data['data']['shortcode_media']['edge_media_to_parent_comment']['edges']
                
                new_comments_found = 0
                for edge in edges:
                    node = edge['node']
                    comment_id = str(node['id'])
                    
                    if comment_id in cache:
                        continue
                    
                    new_comments_found += 1
                    text = node['text']
                    author = node['owner']['username']
                    
                    reason = None
                    text_lower = text.lower()
                    if any(kw in text_lower for kw in KEYWORDS):
                        reason = "Keyword Match"
                    elif author != IG_USERNAME:
                        if check_ai_sentiment(text):
                            reason = "AI categorized as Negative"
                            
                    if reason:
                        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] !!! DELETING {comment_id} from @{author}: {reason}")
                        print(f"    Comment Text: {text}")
                        if delete_comment(media_id, comment_id):
                            send_email_alert(shortcode, text, author, reason)
                    
                    cache.add(comment_id)
                
                if new_comments_found > 0:
                    save_cache(cache)
                    
            except Exception as e:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Post {shortcode} error: {e}")

    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Moderator error: {e}")

# --- MAIN ---

def main():
    print(f"=== Starting Instagram AI Moderator (Stable GQL Scraper) ===")
    print(f"Target Account: @{IG_USERNAME}")
    
    cache = load_cache()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies([
            {"name": "sessionid", "value": IG_SESSIONID, "domain": ".instagram.com", "path": "/"},
            {"name": "csrftoken", "value": IG_CSRFTOKEN, "domain": ".instagram.com", "path": "/"}
        ])
        
        while True:
            print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Waking up: Performing sweep...")
            scrape_and_moderate(context, cache)
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Sleeping for 15 minutes...")
            time.sleep(15 * 60)

if __name__ == "__main__":
    main()
