from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FRAUDALERT_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://fraudalert:fraudalert@localhost:5432/fraudalert"

    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    sender_filter: str = ""
    subject_filter: str = ""
    lookback_days: int = 90

    home_currency: str = "USD"
    timezone: str = "America/New_York"  # used for hour-of-day in rules and features
    detector: str = "baseline"
    notify_webhook_url: str = ""

    web_bind: str = "127.0.0.1"  # host interface docker compose publishes the UI on
    web_username: str = ""
    web_password: str = ""

    @staticmethod
    def _split(value: str) -> list[str]:
        return [v.strip() for v in value.split(",") if v.strip()]

    @property
    def auth_enabled(self) -> bool:
        return bool(self.web_username and self.web_password)

    @property
    def senders(self) -> list[str]:
        return self._split(self.sender_filter)

    @property
    def subjects(self) -> list[str]:
        return self._split(self.subject_filter)


@lru_cache
def get_settings() -> Settings:
    return Settings()
