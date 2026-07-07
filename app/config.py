import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Env file is chosen per environment:
    #   dev  → `CORTEX_ENV_FILE=.env.test uvicorn ...` (reads .env.test directly)
    #   prod → docker-compose injects .env.prod via `env_file` (this setting is
    #          moot there; the vars arrive as real env vars)
    # If the named file is absent, pydantic-settings ignores it and falls back to
    # env vars + defaults (so the app still runs keyless).
    model_config = SettingsConfigDict(
        env_file=os.environ.get("CORTEX_ENV_FILE", ".env"), extra="ignore"
    )

    db_path: str = "cortex.db"

    # Anthropic
    anthropic_api_key: str = ""
    model_fast: str = "claude-haiku-4-5-20251001"   # triage
    model_smart: str = "claude-sonnet-4-6"          # weekly summary

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""       # brief push target
    allowed_chat_ids: str = ""       # comma-separated; gates /reveal and capture; empty = allow all
    public_base_url: str = ""        # https URL the bot webhook points at

    # Loop A schedule
    brief_hour: int = 7
    weekly_review_day: str = "sun"
    weekly_review_hour: int = 18

    # Brief routing
    stale_days: int = 30
    recent_items_for_dedup: int = 40

    tz: str = "Europe/Prague"

    # Dashboard auth — gates only /dashboard and /tables (HTML pages). Empty
    # dashboard_password disables auth entirely, preserving keyless mode.
    dashboard_username: str = "admin"
    dashboard_password: str = ""
    session_secret: str = ""

    def allowed_chat_id_set(self) -> set[str]:
        return {c.strip() for c in self.allowed_chat_ids.split(",") if c.strip()}


settings = Settings()
