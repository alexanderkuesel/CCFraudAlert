"""Currency conversion into the home currency, used by rules and anomaly features.

Rules like "amount > 100" must mean the same thing whether you paid 154.64 USD or 12,500 CRC, so
amounts are converted to FRAUDALERT_HOME_CURRENCY before rules and features see them. Rates only
need to be roughly right for thresholds and anomaly scores, so a built-in table of approximate
rates is used. Override or add rates with FRAUDALERT_FX_RATES, e.g. "CRC=0.00195,EUR=1.09",
meaning 1 CRC = 0.00195 units of your home currency.
"""

import logging

log = logging.getLogger(__name__)

# Approximate USD value of one unit of each currency. Only needs to be in the right ballpark;
# override with FRAUDALERT_FX_RATES when precision matters.
APPROX_USD_PER_UNIT: dict[str, float] = {
    "USD": 1.0, "EUR": 1.10, "GBP": 1.30, "CHF": 1.15, "CAD": 0.73, "AUD": 0.66, "NZD": 0.60,
    "JPY": 0.0068, "CNY": 0.14, "HKD": 0.128, "SGD": 0.75, "KRW": 0.00073, "INR": 0.012,
    "MXN": 0.055, "BRL": 0.18, "ARS": 0.001, "CLP": 0.00105, "COP": 0.00025, "PEN": 0.27,
    "CRC": 0.0020, "GTQ": 0.13, "HNL": 0.04, "NIO": 0.027, "PAB": 1.0, "DOP": 0.016,
    "SEK": 0.095, "NOK": 0.093, "DKK": 0.147, "PLN": 0.25, "CZK": 0.043, "HUF": 0.0027,
    "TRY": 0.029, "ILS": 0.27, "ZAR": 0.055, "AED": 0.272, "SAR": 0.267, "THB": 0.029,
    "PHP": 0.017, "IDR": 0.000062, "MYR": 0.22, "VND": 0.00004, "TWD": 0.031,
}


def parse_rates(spec: str) -> dict[str, float]:
    rates = {}
    for part in spec.split(","):
        if not part.strip():
            continue
        code, _, value = part.partition("=")
        try:
            rates[code.strip().upper()] = float(value)
        except ValueError:
            raise ValueError(f"bad FRAUDALERT_FX_RATES entry {part!r}; expected e.g. CRC=0.00195") from None
    return rates


class Converter:
    def __init__(self, home_currency: str, overrides: dict[str, float] | None = None) -> None:
        self.home = home_currency.upper()
        self.overrides = overrides or {}
        self._warned: set[str] = set()

    def rate(self, currency: str) -> float | None:
        """Units of home currency per one unit of `currency`, or None if unknown."""
        currency = currency.upper()
        if currency == self.home:
            return 1.0
        if currency in self.overrides:
            return self.overrides[currency]
        src, dst = APPROX_USD_PER_UNIT.get(currency), APPROX_USD_PER_UNIT.get(self.home)
        if src is None or dst is None:
            return None
        return src / dst

    def to_home(self, amount: float, currency: str) -> float:
        rate = self.rate(currency)
        if rate is None:
            if currency not in self._warned:
                self._warned.add(currency)
                log.warning("no exchange rate for %s -> %s; using the raw amount. Set FRAUDALERT_FX_RATES.",
                            currency, self.home)
            return amount
        return round(amount * rate, 2)
