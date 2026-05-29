from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))

BOT_NAME = os.getenv("BOT_NAME", "Credential Vault").strip() or "Credential Vault"
SERVICE_NAME = os.getenv("SERVICE_NAME", "Service").strip() or "Service"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in .env")

if not ALLOWED_USER_ID:
    raise RuntimeError("ALLOWED_USER_ID is missing or invalid in .env")
