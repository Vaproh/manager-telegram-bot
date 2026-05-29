# Credential Vault Bot

Private Telegram bot built with python-telegram-bot.

## Setup

Create a `.env` file:

```env
BOT_TOKEN=your_token_here
ALLOWED_USER_ID=123456789
BOT_NAME=Credential Vault
SERVICE_NAME=Service Name
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Run:

```bash
python bot.py
```

## Commands

- /start
- /add
- /bulkadd
- /getaccounts
- /search
- /delete
- /categories
- /addcategory
- /deletecategory
- /logs
- /unused
- /markused
- /markunused
- /stats
- /export
