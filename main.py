import os
import json
import time
import requests
import boto3
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

# Email Configuration
SENDER_EMAIL = os.getenv('SENDER_EMAIL')
RECEIVER_EMAIL = os.getenv('RECEIVER_EMAIL')

# Forbidden Keywords
KEYWORDS = ["boycottexpo", "boycott", "hate", "scam", "fake"]

# Files
CACHE_FILE = "processed_comments.json"

# --- CLIENTS ---
bedrock = boto3.client(
    service_name='bedrock-runtime',
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY
)

ses = boto3.client(
    service_name='ses',
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY
)

# Shared headers for Instagram Web calls
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
    """Uses AWS Bedrock (Minimax) to check if a comment is negative or toxic."""
    prompt = f"Analyze the following Instagram comment. Is it negative, toxic, hateful, or spam? Answer ONLY 'YES' or 'NO'.\n\nComment: {text}"
    
    try:
        # Minimax M2.1 ID in us-east-1
        response = bedrock.invoke_model(
            modelId='minimax.minimax-m2.1',
            contentType='application/json',
            accept='application/json',
            body=json.dumps({
                "messages": [{"role": "user", "content": [{"text": prompt}]}]
            })
        )
        body = json.loads(response['body'].read())
        reply = body['output']['message']['content'][0]['text']
        return "YES" in reply.strip().upper()
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Bedrock error: {e}")
        return False

def send_email_alert(shortcode, comment_text, username, reason):
    """Sends an email notification via SES when a comment is deleted."""
    subject = f"IG MODERATOR: Comment Deleted on {shortcode}"
    body = f"The following comment was deleted from @{IG_USERNAME}'s post (https://instagram.com/p/{shortcode}/):\n\nUser: @{username}\nComment: {comment_text}\nReason: {reason}\n\nTime: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    
    try:
        ses.send_email(
            Source=SENDER_EMAIL,
            Destination={'ToAddresses': [RECEIVER_EMAIL]},
            Message={
                'Subject': {'Data': subject},
                'Body': {'Text': {'Data': body}}
            }
        )
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] SES error: {e}")

# --- INSTAGRAM ACTIONS ---

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
    """Fetches posts and comments via Playwright's authenticated context."""
    try:
        # 1. Fetch Posts
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Fetching latest feed items...")
        feed_url = f"https://www.instagram.com/api/v1/feed/user/{IG_USER_ID}/"
        res = browser_context.request.get(feed_url, headers={'X-IG-App-ID': '936619743392459'})
        
        if res.status != 200:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Feed fetch failed: {res.status}")
            return

        data = res.json()
        items = data.get('items', [])[:5] # Check latest 5 posts/reels
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Scanning {len(items)} posts/reels...")

        for item in items:
            media_id = item['pk']
            shortcode = item['code']
            
            # 2. Fetch Comments
            c_url = f"https://www.instagram.com/api/v1/media/{media_id}/comments/"
            c_res = browser_context.request.get(
                c_url, 
                headers={'X-IG-App-ID': '936619743392459', 'Referer': f'https://www.instagram.com/p/{shortcode}/'}
            )
            
            if c_res.status != 200:
                # Some posts might not return comments in JSON format if challenged, we skip those
                continue
                
            try:
                c_data = c_res.json()
                comments = c_data.get('comments', [])
                
                for comment in comments:
                    comment_id = str(comment['pk'])
                    if comment_id in cache:
                        continue
                        
                    text = comment['text']
                    author = comment['user']['username']
                    
                    reason = None
                    text_lower = text.lower()
                    if any(kw in text_lower for kw in KEYWORDS):
                        reason = "Keyword Match"
                    elif author != IG_USERNAME:
                        if check_ai_sentiment(text):
                            reason = "AI categorized as Negative/Toxic"
                            
                    if reason:
                        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] !!! DELETING {comment_id} from @{author}: {reason}")
                        if delete_comment(media_id, comment_id):
                            send_email_alert(shortcode, text, author, reason)
                            
                    cache.add(comment_id)
                save_cache(cache)
                time.sleep(1)
            except:
                continue

    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Loop error: {e}")

# --- MAIN ---

def main():
    print(f"=== Starting Instagram AI Moderator (Stable Web Scraper) ===")
    print(f"Target Account: @{IG_USERNAME} ({IG_USER_ID})")
    
    cache = load_cache()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies([
            {"name": "sessionid", "value": IG_SESSIONID, "domain": ".instagram.com", "path": "/"},
            {"name": "csrftoken", "value": IG_CSRFTOKEN, "domain": ".instagram.com", "path": "/"}
        ])
        
        while True:
            print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Waking up: Checking new comments...")
            scrape_and_moderate(context, cache)
            
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Sleeping for 15 minutes...")
            time.sleep(15 * 60)

if __name__ == "__main__":
    main()
