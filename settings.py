"""Загрузка config.yaml и .env в одном месте, чтобы остальные модули не дублировали эту логику."""
from pathlib import Path

import yaml
from dotenv import load_dotenv
import os

ROOT_DIR = Path(__file__).resolve().parent

load_dotenv(ROOT_DIR / ".env")

with open(ROOT_DIR / "config.yaml", "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")

INPUT_DIR = ROOT_DIR / CONFIG["paths"]["input_dir"]
PENDING_DIR = ROOT_DIR / CONFIG["paths"]["pending_dir"]
POSTED_DIR = ROOT_DIR / CONFIG["paths"]["posted_dir"]
REJECTED_DIR = ROOT_DIR / CONFIG["paths"]["rejected_dir"]
DB_PATH = ROOT_DIR / CONFIG["paths"]["db_path"]

YOUTUBE_CLIENT_SECRETS_PATH = ROOT_DIR / CONFIG["publishing"]["youtube"]["client_secrets_path"]
YOUTUBE_TOKEN_PATH = ROOT_DIR / CONFIG["publishing"]["youtube"]["token_path"]
