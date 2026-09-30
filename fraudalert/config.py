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

    home_currency: str = "USD"  # rules and anomaly features compare amounts in this currency
    # Currencies you normally pay in, e.g. "CRC,USD"; any other currency counts as foreign.
    # Defaults to the home currency. Editable on the web UI's Settings page (which takes precedence).
    normal_currencies: str = ""
    home_country: str = ""  # e.g. "Costa Rica"; when the email names a country, others count as foreign
    fx_rates: str = ""  # overrides for fraudalert/fx.py, e.g. "CRC=0.00195" (1 CRC in home currency)
    # Card-test detection: an authorisation at or below this amount (home currency) is a likely card
    # test, and a larger charge on the same card within `test_followup_hours` is escalated.
    test_amount_max: float = 1.0
    test_followup_hours: int = 48
    timezone: str = "America/New_York"  # your local time: email dates, hour-of-day rules and features
    detector: str = "iforest"  # "iforest" (falls back to "baseline" until trained) or "baseline"
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
