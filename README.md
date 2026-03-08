# Instagram AI Moderation Bot

An enterprise-ready Python bot designed to run on AWS EC2, monitor your Instagram account's recent posts, delete negative/spam comments using Keyword matching and AWS Bedrock (Minimax M2.1 AI), and notify you via Amazon SES emails.

## Features
- **Keyword Filtering:** Instantly deletes comments containing specific substrings (e.g., "boycottexpo").
- **AI Sentiment Analysis:** Uses AWS Bedrock's Minimax M2.1 foundation model to dynamically evaluate and delete negative or abusive comments.
- **Email Alerts:** Sends rich email notifications via AWS SES whenever a comment is deleted.
- **Smart Caching:** Keeps track of processed comment IDs in a local `processed_comments.json` file so it exclusively evaluates *new* comments on subsequent runs.
- **Session Persistence:** Retains Instagram session cookies to prevent login blocks.

---

## 🛠️ Deployment Instructions (AWS EC2)

### 1. Initialize the Environment
Connect to your EC2 instance (Ubuntu/Debian recommended) and clone the repository.
```bash
# Install Python 3, pip, venv, and PM2
sudo apt update
sudo apt install python3-pip python3-venv npm -y
sudo npm install pm2 -g

# Go to your repository folder
cd anam-ig-moderator

# Setup Python Virtual Environment
python3 -m venv venv
source venv/bin/activate

# Install Dependencies
pip install -r requirements.txt
```

### 2. Configure Settings
```bash
cp .env.example .env
nano .env
```
Fill in your Instagram credentials, and verify the AWS SMTP settings that have been pre-filled.

**IMPORTANT AWS Requirements:**
1. The EC2 instance must have an **IAM Role** attached with permissions for `bedrock:InvokeModel`.
2. Ensure you have requested access to the Minimax text model in the AWS Bedrock Console.

### 3. First-Time Authentication (Required)
Because your Instagram account has Two-Factor Authentication (2FA) enabled, **you must run the script manually the very first time.**

```bash
python main.py
```
- The script will attempt to log in.
- `instagrapi` will detect the 2FA requirement and prompt you in the terminal.
- Enter the 6-digit code sent to your phone/auth app.
- Once authenticated, the script will create a `session.json` file. 
- Allow it to run through the first batch of comments, then press `Ctrl+C` to stop it.

### 4. Run Continuously with PM2
Now that `session.json` exists, the bot can run entirely unattended in the background.

```bash
# Start the bot using PM2
pm2 start main.py --interpreter ./venv/bin/python --name ig-moderator

# Save PM2 state so it restarts on server reboot
pm2 save
pm2 startup
```

## Logs and Monitoring
To view the bot's real-time logs (e.g., seeing it wake up every 15 minutes to delete comments):
```bash
pm2 logs ig-moderator
```
