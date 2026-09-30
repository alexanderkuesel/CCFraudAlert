from datetime import datetime, timezone
from decimal import Decimal

import pytest

from fraudalert.ingest.message import EmailMessage, html_to_text, parse_rfc822
from fraudalert.ingest.parsers import ParseError, parse_email, parse_number

from .conftest import make_eml

WHEN = datetime(2026, 9, 20, 18, 30, tzinfo=timezone.utc)


def msg(subject, body):
    return EmailMessage("<id@x>", "alerts@bank.example", subject, WHEN, body)


@pytest.mark.parametrize(
    "raw,expected",
    [("1,234.56", "1234.56"), ("1.234,56", "1234.56"), ("12,50", "12.50"), ("100", "100"),
     ("1 234,56", "1234.56"), ("1,000", "1000"), ("7.5", "7.5")],
)
def test_parse_number(raw, expected):
    assert parse_number(raw) == Decimal(expected)


def test_inline_usd_alert():
    p, name = parse_email(
        msg("Your $142.18 transaction with WHOLE FOODS #123",
            "You made a $142.18 transaction with WHOLE FOODS #123 on your card ending in 4321."),
        "USD",
    )
    assert name == "generic"
    assert (p.amount, p.currency, p.card_last4) == (Decimal("142.18"), "USD", "4321")
    assert p.merchant == "WHOLE FOODS #123"
    assert p.occurred_at == WHEN
    assert not p.foreign_hint


def test_labelled_fields_with_foreign_currency_and_date():
    body = """Transaction alert
Card: Visa ****9876
Merchant: CAFE DE FLORE PARIS
Amount: EUR 48,90
Date: 09/19/2026 21:14
This is a foreign transaction."""
    p, _ = parse_email(msg("Transaction alert", body), "USD")
    assert (p.amount, p.currency) == (Decimal("48.90"), "EUR")
    assert p.merchant == "CAFE DE FLORE PARIS"
    assert p.card_last4 == "9876"
    assert p.occurred_at.replace(tzinfo=None) == datetime(2026, 9, 19, 21, 14)
    assert p.foreign_hint


def test_symbol_currency_and_trailing_code():
    p, _ = parse_email(msg("Purchase alert", "A charge of £23.40 was made at TESCO LONDON."), "USD")
    assert (p.amount, p.currency, p.merchant) == (Decimal("23.40"), "GBP", "TESCO LONDON")
    p, _ = parse_email(msg("Purchase alert", "A charge of 5,000 JPY at LAWSON TOKYO."), "USD")
    assert (p.amount, p.currency) == (Decimal("5000"), "JPY")


def test_label_on_next_line_from_html_table():
    html = """<html><body><h2>Transaction alert</h2><table>
      <tr><td>Account ending in</td><td>(...5555)</td></tr>
      <tr><td>Date</td><td>Sep 28, 2026 at 3:12 PM ET</td></tr>
      <tr><td>Merchant</td><td>AMAZON MKTPL*AB12C</td></tr>
      <tr><td>Amount</td><td>$1,249.00</td></tr></table></body></html>"""
    m = parse_rfc822(make_eml("Transaction alert", html, WHEN, html=True))
    p, _ = parse_email(m, "USD")
    assert (p.amount, p.currency, p.merchant, p.card_last4) == (Decimal("1249.00"), "USD", "AMAZON MKTPL*AB12C", "5555")
    assert (p.occurred_at.month, p.occurred_at.day, p.occurred_at.hour) == (9, 28, 15)


def test_dollar_maps_to_home_dollar_currency():
    p, _ = parse_email(msg("Alert", "You spent $20.00 at TIM HORTONS."), "CAD")
    assert p.currency == "CAD"


def test_non_transaction_email_is_rejected():
    with pytest.raises(ParseError):
        parse_email(msg("Your statement is ready", "Your balance is $1,200.00."), "USD")
    with pytest.raises(ParseError):
        parse_email(msg("Hello", "Nothing to see here."), "USD")


def test_html_to_text_skips_style():
    assert html_to_text("<style>p{}</style><p>Hi&nbsp;there</p><br>x") == "Hi there\nx"
