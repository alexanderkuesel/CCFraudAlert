"""Bank alert email parsers.

Each parser gets an `EmailMessage` and returns a `ParsedTransaction` or raises `ParseError`.
`GenericAlertParser` uses heuristics that cover most US/EU card alert formats. To support a
bank whose emails it misreads, subclass `BaseParser`, implement `matches` + `parse`, and add
it to `PARSERS` *before* the generic parser.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from fraudalert.ingest.message import EmailMessage


class ParseError(Exception):
    pass


@dataclass
class ParsedTransaction:
    amount: Decimal
    currency: str
    merchant: str
    occurred_at: datetime
    card_last4: str | None = None
    foreign_hint: bool = False  # the email itself says "foreign transaction" etc.
    country: str | None = None  # where the purchase happened, when the email says
    auth_code: str | None = None  # the bank's authorization / approval code ("Autorización")
    reference: str | None = None  # the bank's reference number ("Referencia"), if any


# Symbols are checked longest-first so "US$" wins over "$".
SYMBOLS = {
    "US$": "USD", "U$S": "USD", "CA$": "CAD", "C$": "CAD", "AU$": "AUD", "A$": "AUD",
    "NZ$": "NZD", "HK$": "HKD", "S$": "SGD", "MX$": "MXN", "R$": "BRL",
    "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "₩": "KRW", "₪": "ILS", "₺": "TRY",
    "₱": "PHP", "₫": "VND", "฿": "THB", "₡": "CRC", "CHF": "CHF", "$": None,  # "$" -> home dollar
}
ISO_CODES = {
    "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "NZD", "CHF", "CNY", "HKD", "SGD", "INR", "MXN",
    "BRL", "KRW", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "TRY", "ILS", "ZAR", "THB", "PHP",
    "IDR", "MYR", "VND", "AED", "SAR", "ARS", "CLP", "COP", "PEN", "TWD", "RUB", "EGP", "MAD",
    "CRC", "GTQ", "HNL", "NIO", "PAB", "DOP", "UYU", "BOB", "PYG",
}

# 1,234.56 / 1.234,56 / 1234 / and ".00" (banks print zero-amount authorisations as "USD .00")
_NUM = r"\d{1,3}(?:[,.' ]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?|\.\d{2}"
_SYM_RE = "|".join(re.escape(s) for s in sorted(SYMBOLS, key=len, reverse=True))
_CODES_RE = "|".join(sorted(ISO_CODES))
AMOUNT_PATTERNS = [
    re.compile(rf"(?P<sym>{_SYM_RE})\s?(?P<num>{_NUM})"),
    re.compile(rf"\b(?P<code>{_CODES_RE})\s?(?P<num>{_NUM})"),
    re.compile(rf"(?P<num>{_NUM})\s?(?P<code>{_CODES_RE})\b"),
    re.compile(rf"(?P<num>{_NUM})\s?(?P<sym>€|£|¥|₹|₡)"),
]

LABEL_RE = r"(?:transaction\s+)?(?:amount|total|charge|purchase amount)"
MERCHANT_LABEL_RE = re.compile(
    r"^\s*(?:merchant(?:\s+name)?|where|description|payee|store)\s*:?\s*(?:\n\s*)?(?P<m>[^\n]{2,80})$",
    re.IGNORECASE | re.MULTILINE,
)
MERCHANT_INLINE_RE = re.compile(
    r"\b(?:at|with|to|from)\s+(?P<m>[A-Z0-9][A-Za-z0-9 &'*#./\-]{1,60}?)"
    r"(?=\s+(?:on|for|was|has|using|with|in the amount|exceeded|at\s+\d)\b|[.,;!\n]|$)",
)
CARD_RE = re.compile(
    r"(?:ending\s+(?:in|with)|last\s+4(?:\s+digits)?(?:\s+of)?|card\s+(?:no\.?|number)?)"
    r"[^0-9\n]{0,12}(?P<d>\d{4})\b|[x*•.]{2,}\s?(?P<d2>\d{4})\b",
    re.IGNORECASE,
)
DATE_LABEL_RE = re.compile(
    r"^\s*(?:date|transaction date|date and time|time)\s*:?\s*(?:\n\s*)?(?P<d>[^\n]{6,60})$",
    re.IGNORECASE | re.MULTILINE,
)
AUTH_RE = re.compile(
    r"\b(?:authori[sz]ation(?:\s+code)?|auth\.?\s*code|approval\s+code)\s*(?:no\.?|number|#)?\s*[:#]?\s*\n?\s*"
    r"(?P<v>(?=[A-Z0-9-]*\d)[A-Z0-9-]{4,20})\b",
    re.IGNORECASE,
)
REF_RE = re.compile(
    r"\b(?:reference|ref\.?)\s*(?:no\.?|number|#)?\s*[:#]\s*\n?\s*(?P<v>(?=[A-Z0-9-]*\d)[A-Z0-9-]{4,30})\b"
    r"|\breference\s+number\s*\n?\s*(?P<v2>(?=[A-Z0-9-]*\d)[A-Z0-9-]{4,30})\b",
    re.IGNORECASE,
)


def find_code(pattern: re.Pattern, text: str) -> str | None:
    m = pattern.search(text)
    return (m.group("v") or m.groupdict().get("v2")) if m else None


FOREIGN_RE = re.compile(
    r"\b(foreign (?:transaction|purchase|charge|currency)|international (?:transaction|purchase|charge)|"
    r"outside (?:the )?(?:US|U\.S\.|country))\b",
    re.I,
)
DATE_FORMATS = [
    "%b %d, %Y, %H:%M", "%b %d, %Y, %I:%M %p",
    "%b %d, %Y at %I:%M %p", "%B %d, %Y at %I:%M %p", "%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p",
    "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d %b %Y, %H:%M", "%d %b %Y %H:%M", "%d %b %Y", "%d.%m.%Y %H:%M", "%d.%m.%Y",
]
DAY_FIRST_FORMATS = ["%d/%m/%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y - %H:%M", "%d-%m-%Y %H:%M", "%d-%m-%Y"]
_SPANISH_MONTHS = {
    "ene": "Jan", "feb": "Feb", "mar": "Mar", "abr": "Apr", "may": "May", "jun": "Jun", "jul": "Jul",
    "ago": "Aug", "sep": "Sep", "set": "Sep", "oct": "Oct", "nov": "Nov", "dic": "Dec",
}
_STOP_MERCHANTS = {"your", "you", "the", "a", "an", "us", "chase", "your account", "your card"}


def parse_number(raw: str) -> Decimal:
    s = raw.replace(" ", "").replace("'", "")
    # Whichever separator appears last followed by 1-2 digits is the decimal point.
    m = re.search(r"[.,](\d{1,2})$", s)
    if m:
        whole, frac = s[: m.start()], m.group(1)
        whole = re.sub(r"[.,]", "", whole)
        s = f"{whole}.{frac}"
    else:
        s = re.sub(r"[.,]", "", s)
    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise ParseError(f"bad amount {raw!r}") from exc


def _dollar_currency(home: str) -> str:
    return home if home in {"USD", "CAD", "AUD", "NZD", "SGD", "HKD", "MXN"} else "USD"


def find_amount(text: str, home_currency: str) -> tuple[Decimal, str]:
    # Prefer an amount on a labelled line ("Amount: $12.34" or "Amount\n$12.34").
    for m in re.finditer(rf"{LABEL_RE}\s*:?\s*\n?\s*(?P<rest>[^\n]{{1,40}})", text, re.I):
        found = _first_amount(m.group("rest"), home_currency)
        if found:
            return found
    found = _first_amount(text, home_currency)
    if not found:
        raise ParseError("no amount found")
    return found


def _first_amount(text: str, home_currency: str) -> tuple[Decimal, str] | None:
    best = None
    for pat in AMOUNT_PATTERNS:
        m = pat.search(text)
        if m and (best is None or m.start() < best[0]):
            code = m.groupdict().get("code")
            sym = m.groupdict().get("sym")
            currency = code or SYMBOLS.get(sym) or _dollar_currency(home_currency)
            best = (m.start(), parse_number(m.group("num")), currency)
    return (best[1], best[2]) if best else None


def _clean_merchant(raw: str) -> str:
    m = re.sub(r"\s+", " ", raw).strip(" .,:;-")
    return m[:120]


def find_merchant(text: str, subject: str) -> str:
    m = MERCHANT_LABEL_RE.search(text)
    if m and not re.search(r"\d+[.,]\d{2}", m.group("m")):
        return _clean_merchant(m.group("m"))
    for source in (subject, text):
        for m in MERCHANT_INLINE_RE.finditer(source):
            cand = _clean_merchant(m.group("m"))
            if cand.lower() not in _STOP_MERCHANTS and not re.fullmatch(r"[\d\s.,$]+", cand):
                return cand
    return ""


def find_card(text: str) -> str | None:
    m = CARD_RE.search(text)
    return (m.group("d") or m.group("d2")) if m else None


def parse_date(raw: str, day_first: bool = False) -> datetime | None:
    """Parse a date as written in the email. Returns a naive datetime (the pipeline treats it as
    local time in FRAUDALERT_TIMEZONE)."""
    s = re.sub(r"\s+", " ", raw).strip()
    s = re.sub(r"\s+(?:[A-Z]{2,4}|UTC[+-]?\d*)$", "", s)  # drop trailing TZ abbreviation ("ET", "EST")
    s = re.sub(r"\b([A-Za-z]{3})[a-z]*\.?(?=\s)", lambda m: _SPANISH_MONTHS.get(m.group(1).lower(), m.group(0)), s)
    s = re.sub(r"\b(a\.\s?m\.|p\.\s?m\.)", lambda m: "AM" if m.group(1)[0] == "a" else "PM", s)
    formats = DAY_FIRST_FORMATS + DATE_FORMATS if day_first else DATE_FORMATS + DAY_FIRST_FORMATS
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


class BaseParser:
    name = "base"
    # A fallback parser only runs when no specific parser recognised the email. Once a specific
    # parser matches, its verdict is final: falling back to heuristics produced junk transactions.
    fallback = False

    def matches(self, msg: EmailMessage) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def parse(self, msg: EmailMessage, home_currency: str) -> ParsedTransaction:  # pragma: no cover
        raise NotImplementedError


class GenericAlertParser(BaseParser):
    """Heuristic parser for typical 'You made a $X purchase at Y' alert emails."""

    name = "generic"
    fallback = True
    NOT_TRANSACTION = re.compile(
        r"\b(statement is (?:ready|available)|payment (?:is )?due|autopay|payment (?:received|posted)|"
        r"password|sign[- ]in|verify your|credit limit increase)\b",
        re.I,
    )

    def matches(self, msg: EmailMessage) -> bool:
        return True

    def parse(self, msg: EmailMessage, home_currency: str) -> ParsedTransaction:
        if self.NOT_TRANSACTION.search(msg.subject):
            raise ParseError(f"subject does not look like a transaction: {msg.subject!r}")
        text = f"{msg.subject}\n{msg.body}"
        amount, currency = find_amount(text, home_currency)
        occurred_at = None
        m = DATE_LABEL_RE.search(msg.body)
        if m:
            occurred_at = parse_date(m.group("d"))
        occurred_at = occurred_at or msg.received_at
        if occurred_at is None:
            raise ParseError("no transaction date and no email date")
        return ParsedTransaction(
            amount=amount,
            currency=currency,
            merchant=find_merchant(msg.body, msg.subject),
            occurred_at=occurred_at,
            card_last4=find_card(text),
            foreign_hint=bool(FOREIGN_RE.search(text)),
            auth_code=find_code(AUTH_RE, msg.body),
            reference=find_code(REF_RE, msg.body),
        )


def _code(value: str | None) -> str | None:
    """A bank code field: keep it only if it looks like a code (not blank, not another label)."""
    v = (value or "").strip()
    return v[:32] if re.fullmatch(r"[A-Za-z0-9-]{3,32}", v) else None


def _fold(s: str) -> str:
    """Lower-case and strip accents: 'Autorización' -> 'autorizacion'."""
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFD", s.casefold()) if unicodedata.category(c) != "Mn")


class SpanishAlertParser(BaseParser):
    """Label/value alerts in Spanish, as sent by BAC Credomatic and similar Latin American banks:

        Comercio:            GLOBAL-E
        Ciudad y país:       , Reino Unido
        Fecha:               Sep 29, 2026, 17:01
        AMEX:                ***********1234
        Tipo de Transacción: COMPRA
        Monto:               USD 154.64

    Values may follow the label on the same line (tab separated) or on the next line (HTML tables).
    """

    name = "es-labels"
    LABELS = {
        "comercio": "merchant", "establecimiento": "merchant",
        "ciudad y pais": "location", "pais": "location", "lugar": "location",
        "fecha": "date", "fecha y hora": "date",
        "monto": "amount", "importe": "amount", "monto de la transaccion": "amount",
        "tipo de transaccion": "type", "tipo": "type",
        "tarjeta": "card", "amex": "card", "visa": "card", "mastercard": "card", "master card": "card",
        "autorizacion": "auth", "referencia": "ref",
    }
    LABEL_LINE = re.compile(r"^\s*([^\n:]{2,30}?)\s*:\s*(.*)$")
    NON_PURCHASE = re.compile(r"devoluci|anulaci|revers|reembols|nota de cr|pago recibido|abono", re.I)
    SUBJECT_MERCHANT = re.compile(r"transacci[oó]n\s+(?P<m>.+?)\s+\d{1,2}-\d{1,2}-\d{4}", re.I)

    def fields(self, text: str) -> dict[str, str]:
        lines = [line.strip() for line in text.splitlines()]
        out: dict[str, str] = {}
        for i, line in enumerate(lines):
            m = self.LABEL_LINE.match(line)
            key = self.LABELS.get(_fold(m.group(1))) if m else None
            if not key or key in out:
                continue
            value = m.group(2).strip()
            if not value and i + 1 < len(lines):
                nxt = self.LABEL_LINE.match(lines[i + 1])
                if not (nxt and _fold(nxt.group(1)) in self.LABELS):
                    value = lines[i + 1]
            out[key] = value
        return out

    def matches(self, msg: EmailMessage) -> bool:
        f = self.fields(msg.body)
        return "amount" in f and ("merchant" in f or "type" in f)

    def parse(self, msg: EmailMessage, home_currency: str) -> ParsedTransaction:
        f = self.fields(msg.body)
        kind = f.get("type", "")
        if self.NON_PURCHASE.search(kind):
            raise ParseError(f"not a purchase (Tipo de Transacción: {kind})")
        found = _first_amount(f.get("amount", ""), home_currency)
        if not found:
            raise ParseError(f"no amount in {f.get('amount')!r}")
        amount, currency = found

        occurred_at = parse_date(f["date"], day_first=True) if f.get("date") else None
        if occurred_at is None:
            m = re.search(r"\d{1,2}-\d{1,2}-\d{4}\s*-\s*\d{1,2}:\d{2}", msg.subject)
            occurred_at = parse_date(m.group(0), day_first=True) if m else msg.received_at
        if occurred_at is None:
            raise ParseError("no transaction date and no email date")

        merchant = f.get("merchant", "")
        if not merchant:
            m = self.SUBJECT_MERCHANT.search(msg.subject)
            merchant = m.group("m") if m else ""
        card = re.search(r"(\d{4})\D*$", f.get("card", ""))
        country = f.get("location", "").rsplit(",", 1)[-1].strip() or None
        return ParsedTransaction(
            amount=amount,
            currency=currency,
            merchant=_clean_merchant(merchant),
            occurred_at=occurred_at,
            card_last4=card.group(1) if card else find_card(msg.body),
            country=country,
            auth_code=_code(f.get("auth")),
            reference=_code(f.get("ref")),
        )


PARSERS: list[BaseParser] = [SpanishAlertParser(), GenericAlertParser()]


def parse_email(msg: EmailMessage, home_currency: str) -> tuple[ParsedTransaction, str]:
    specific = [p for p in PARSERS if not p.fallback and p.matches(msg)]
    candidates = specific or [p for p in PARSERS if p.fallback and p.matches(msg)]
    errors = []
    for parser in candidates:
        try:
            return parser.parse(msg, home_currency), parser.name
        except ParseError as exc:
            errors.append(f"{parser.name}: {exc}")
    raise ParseError("; ".join(errors) or "no parser matched")
