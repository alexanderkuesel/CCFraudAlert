from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from fraudalert import pipeline, report
from fraudalert.config import get_settings
from fraudalert.models import Transaction
from fraudalert.web.app import create_app

from .bac import bac_eml

CR = ZoneInfo("America/Costa_Rica")
NOW = datetime(2026, 9, 30, 2, 5, tzinfo=timezone.utc)  # 20:05 on Sep 29 in Costa Rica


class FakeSMTP:
    sent: list = []
    fail: Exception | None = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.calls = host, port, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        if FakeSMTP.fail:
            raise FakeSMTP.fail
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        FakeSMTP.sent.append((self.host, self.port, self.calls, msg))


@pytest.fixture
def env(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FRAUDALERT_HOME_COUNTRY", "Costa Rica")
    monkeypatch.setenv("FRAUDALERT_NORMAL_CURRENCIES", "CRC,USD")
    monkeypatch.setenv("FRAUDALERT_TIMEZONE", "America/Costa_Rica")
    monkeypatch.setenv("FRAUDALERT_IMAP_USER", "me@gmail.com")
    monkeypatch.setenv("FRAUDALERT_IMAP_PASSWORD", "app-pw")
    get_settings.cache_clear()
    FakeSMTP.sent, FakeSMTP.fail = [], None
    monkeypatch.setattr(report.smtplib, "SMTP", FakeSMTP)
    yield db
    get_settings.cache_clear()


def load(tmp_path, specs):
    files = []
    for i, (sent_utc, kw) in enumerate(specs):
        p = tmp_path / f"{i}.eml"
        p.write_bytes(bac_eml(sent_utc, date=(sent_utc - timedelta(hours=6)).strftime("%b %d, %Y, %H:%M"), **kw))
        files.append(p)
    pipeline.import_eml_files(files)


def scenario(db, tmp_path):
    card = ("AMEX", "***********4321")
    load(tmp_path, [
        (NOW - timedelta(hours=11), dict(merchant="AMAZON.COM LLC", place=", Estados Unidos", amount="USD .00", card=card)),
        (NOW - timedelta(hours=8), dict(merchant="BEST BUY & CO", place=", Estados Unidos", amount="USD 480.00", card=card)),
        (NOW - timedelta(hours=5), dict(merchant="SODA TICA", place="HEREDIA, Costa Rica", amount="CRC 4,500.00", card=card)),
        (NOW - timedelta(days=6), dict(merchant="HOTEL LTD", place="LONDRES, Reino Unido", amount="EUR 640.00", card=card)),
    ])
    with db.session_scope() as s:
        s.query(Transaction).filter_by(merchant="HOTEL LTD").one().label_fraud = True
        report.save_prefs(s, {"enabled": "1", "time": "20:00", "to": "", "bank_name": "BAC Credomatic",
                              "bank_phone": "+506 2295-9898", "dashboard_url": "http://192.168.1.20:8000/"})


def test_report_content_is_ready_for_calling_the_bank(env, tmp_path):
    scenario(env, tmp_path)
    with env.session_scope() as s:
        prefs = report.get_prefs(s, get_settings())
        assert prefs["to"] == "me@gmail.com"  # defaults to the IMAP address
        r = report.build_report(s, get_settings(), NOW - timedelta(days=1), NOW, prefs)
    # SODA TICA is on the same card within 48h of the $0.00 test, so it's escalated too
    assert r.subject == "Finance Trends & Alarms · Sep 29: 3 unacknowledged (3 High)"
    assert r.unacknowledged == 3 and r.transactions == 3  # the hotel is 6 days old: not "since the last report"
    t = r.text
    assert "Call BAC Credomatic at +506 2295-9898 and quote the authorization code" in t
    # every alarm line carries local time, amount, card and the bank's authorization code
    assert "[P1 High] 2026-09-29 09:05  0.00 USD  AMAZON.COM LLC  card …4321  auth 657401" in t
    assert "480.00 USD  BEST BUY & CO" in t
    assert "MARKED AS FRAUD" in t and "640.00 EUR  HOTEL LTD" in t
    assert "Open the dashboard: http://192.168.1.20:8000/alarms?view=unack" in t
    h = r.html
    assert 'href="tel:+5062295-9898"' in h and "BEST BUY &amp; CO" in h and "BEST BUY & CO" not in h
    assert "657401" in h and "PASSIVE MONITOR" in h


def test_all_clear_and_missing_phone(env, tmp_path):
    with env.session_scope() as s:
        r = report.build_report(s, get_settings(), NOW - timedelta(days=1), NOW, report.get_prefs(s, get_settings()))
    assert r.subject.endswith(": all clear") and "All clear" in r.text
    scenario(env, tmp_path)
    with env.session_scope() as s:
        prefs = report.get_prefs(s, get_settings()) | {"bank_phone": ""}
        r = report.build_report(s, get_settings(), NOW - timedelta(days=1), NOW, prefs)
    assert "Add your bank's phone number" in r.text


@pytest.mark.parametrize("local_now,previous_local,due", [
    ("2026-09-29 19:59", None, False),                   # before 20:00
    ("2026-09-29 20:00", None, True),                    # at 20:00
    ("2026-09-29 23:30", "2026-09-28 20:01", True),      # yesterday's sent, today's not yet
    ("2026-09-29 23:30", "2026-09-29 20:02", False),     # already sent today
    ("2026-09-29 08:00", "2026-09-27 20:00", False),     # missed yesterday: wait for tonight (it'll cover both days)
])
def test_is_due(local_now, previous_local, due):
    parse = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=CR)  # noqa: E731
    prefs = {"enabled": True, "time": "20:00"}
    assert report.is_due(prefs, parse(previous_local) if previous_local else None, parse(local_now), CR) is due
    assert not report.is_due(prefs | {"enabled": False}, None, parse("2026-09-29 21:00"), CR)


def test_worker_sends_once_per_day_and_test_sends_do_not_count(env, tmp_path):
    scenario(env, tmp_path)
    assert not report.maybe_send_daily_report(now=NOW - timedelta(hours=1))  # 19:05 local: not yet
    report.send_report(now=NOW - timedelta(minutes=30), test=True)
    assert FakeSMTP.sent[-1][3]["Subject"].startswith("[TEST] ")
    assert report.maybe_send_daily_report(now=NOW)  # the test didn't consume today's report
    host, port, calls, msg = FakeSMTP.sent[-1]
    assert (host, port) == ("smtp.gmail.com", 587)
    assert calls == ["starttls", ("login", "me@gmail.com", "app-pw")]  # reuses the IMAP app password
    assert msg["To"] == "me@gmail.com" and not msg["Subject"].startswith("[TEST]")
    assert {p.get_content_type() for p in msg.iter_parts()} == {"text/plain", "text/html"}
    assert not report.maybe_send_daily_report(now=NOW + timedelta(hours=2))  # once per day
    assert report.maybe_send_daily_report(now=NOW + timedelta(days=1))  # tomorrow evening
    assert len(FakeSMTP.sent) == 3


def test_settings_card_save_validate_test_and_errors(env, tmp_path):
    client = TestClient(create_app(init=False))
    page = client.get("/settings").text
    assert "Daily report" in page and 'value="20:00"' in page and 'value="me@gmail.com"' in page
    r = client.post("/settings/report", data={"enabled": "1", "time": "21:30", "to": "me@gmail.com",
                                              "bank_name": "BAC", "bank_phone": "2295-9898", "dashboard_url": ""})
    assert "Daily report saved (on, daily at 21:30)." in r.text
    assert "report time must look like 20:00" in client.post("/settings/report", data={"time": "9pm"}).text
    assert "must be an email address" in client.post("/settings/report", data={"time": "20:00", "to": "me"}).text
    assert "Daily report saved (off)." in client.post("/settings/report", data={"time": "20:00"}).text

    FakeSMTP.fail = OSError("535 Username and Password not accepted")
    r = client.post("/report/test")
    assert "Test report not sent: 535 Username and Password not accepted" in r.text
    assert "Last send failed" in r.text
    FakeSMTP.fail = None
    r = client.post("/report/test")
    assert "Test report sent to me@gmail.com" in r.text and "Last send failed" not in r.text


def test_cli_report(env, tmp_path, capsys):
    from fraudalert.cli import main

    scenario(env, tmp_path)
    assert main(["report", "--test"]) == 0
    assert "sent:" in capsys.readouterr().out and FakeSMTP.sent[-1][3]["Subject"].startswith("[TEST]")


def test_period_table_shows_state_not_a_made_up_priority(env, tmp_path):
    scenario(env, tmp_path)
    with env.session_scope() as s:
        r = report.build_report(s, get_settings(), NOW - timedelta(days=1), NOW, report.get_prefs(s, get_settings()))
    period = r.text.split("TRANSACTIONS SINCE THE LAST REPORT")[1]
    soda = next(line for line in period.splitlines() if "SODA TICA" in line)
    assert soda.endswith("[P1 High]")  # escalated: same card within 48h of the card test
    assert "Reference" not in r.html.split("Transactions since the last report")[1]  # no refs -> no column


def test_report_includes_why_unusual(env, tmp_path):
    scenario(env, tmp_path)
    with env.session_scope() as s:
        t = s.query(Transaction).filter_by(merchant="AMAZON.COM LLC").one()
        t.anomaly_reasons = [{"key": "amount", "text": "zero or near-zero amount (a typical card test)", "weight": 0.7}]
        r = report.build_report(s, get_settings(), NOW - timedelta(days=1), NOW, report.get_prefs(s, get_settings()))
    assert "why unusual: zero or near-zero amount (a typical card test)" in r.text
    assert "<b>Why unusual:</b> zero or near-zero amount (a typical card test)" in r.html
