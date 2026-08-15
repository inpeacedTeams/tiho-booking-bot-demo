import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    webapp_url: str = os.getenv("WEBAPP_URL", "http://localhost:8000")
    admin_id: int | None = int(os.getenv("ADMIN_TELEGRAM_ID")) if os.getenv("ADMIN_TELEGRAM_ID") else None
    database_path: str = os.getenv("DATABASE_PATH", "./tiho.db")
    demo_mode: bool = os.getenv("DEMO_MODE", "true").lower() == "true"

settings = Settings()
