"""Daily report email.

Sent once a day at the user's chosen local time (default 20:00) by the worker. It covers everything
received since the previous report and is laid out for reporting fraud to the bank: the bank's phone
number (entered in Settings) next to each alarm's time, amount, card and the bank's own authorization
code / reference, so the user can read them straight off the email.

Like the rest of the dashboard it is passive: it reports, it never acts on a card.
"""

import html
import json
import logging
import smtplib
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from fraudalert.anomaly.explain import summary
from fraudalert.config import Settings, get_settings
from fraudalert.db import session_scope
from fraudalert.models import Alert, SyncState, Transaction

log = logging.getLogger(__name__)

APP_NAME = "Finance Trends & Alarms"
PREFS_KEY = "pref:daily_report"
LAST_SENT_KEY = "daily_report_last_sent"
LAST_ERROR_KEY = "daily_report_last_error"
DEFAULTS = {"enabled": True, "time": "20:00", "to": "", "bank_name": "", "bank_phone": "", "dashboard_url": ""}
LATE_ARRIVAL = timedelta(days=3)  # include emails that arrive up to 3 days after the purchase
MAX_ROWS = 100
PRIORITY = {"high": 1, "medium": 2, "low": 3}
PRIORITY_LABEL = {1: "P1 High", 2: "P2 Medium", 3: "P3 Low"}
PRIORITY_COLOR = {1: "#c62828", 2: "#b7860b", 3: "#5d7d9c"}  # text colours that read on white


# ---- preferences ---------------------------------------------------------------------------------

def get_prefs(session: Session, settings: Settings) -> dict:
    row = session.get(SyncState, PREFS_KEY)
    prefs = DEFAULTS | (json.loads(row.value) if row else {})
    prefs["to"] = prefs["to"] or settings.imap_user
    return prefs


def save_prefs(session: Session, data: dict) -> dict:
    """Validate and store. Raises ValueError with a user-facing message."""
    clean = {k: str(data.get(k, DEFAULTS[k])).strip() for k in DEFAULTS if k != "enabled"}
    clean["enabled"] = bool(data.get("enabled"))
    try:
        hh, mm = (int(x) for x in clean["time"].split(":"))
        time(hh, mm)
    except ValueError:
        raise ValueError("report time must look like 20:00") from None
    clean["time"] = f"{hh:02d}:{mm:02d}"
    if clean["to"] and "@" not in clean["to"]:
        raise ValueError("the recipient must be an email address")
    if clean["dashboard_url"] and not clean["dashboard_url"].startswith(("http://", "https://")):
        raise ValueError("the dashboard link must start with http:// or https://")
    clean["dashboard_url"] = clean["dashboard_url"].rstrip("/")
    _set(session, PREFS_KEY, json.dumps(clean))
    return clean


def _get(session: Session, key: str) -> str | None:
    row = session.get(SyncState, key)
    return row.value if row else None


def _set(session: Session, key: str, value: str) -> None:
    row = session.get(SyncState, key)
    if row:
        row.value = value
    else:
        session.add(SyncState(key=key, value=value))


def last_sent(session: Session) -> datetime | None:
    value = _get(session, LAST_SENT_KEY)
    return datetime.fromisoformat(value) if value else None


def last_error(session: Session) -> str | None:
    return _get(session, LAST_ERROR_KEY) or None


# ---- building the report --------------------------------------------------------------------------

@dataclass
class Report:
    subject: str
    text: str
    html: str
    unacknowledged: int
    transactions: int


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _priority(t: Transaction) -> int | None:
    return min((PRIORITY.get(a.severity, 3) for a in t.alerts), default=None)


def _alarm_names(t: Transaction) -> str:
    alerts = sorted(t.alerts, key=lambda a: PRIORITY.get(a.severity, 3))
    return "; ".join(a.rule.name if a.rule else a.reason for a in alerts)


def build_report(session: Session, settings: Settings, since: datetime, until: datetime, prefs: dict) -> Report:
    from fraudalert.pipeline import Env

    env = Env.load(session, settings)
    tz = ZoneInfo(settings.timezone)
    since, until = _utc(since), _utc(until)
    load = select(Transaction).options(selectinload(Transaction.alerts).selectinload(Alert.rule))

    # Everything received since the last report (purchases up to 3 days old, for late bank emails).
    period = session.scalars(load.where(
        Transaction.occurred_at < until,
        or_(Transaction.occurred_at >= since,
            (Transaction.created_at >= since) & (Transaction.occurred_at >= since - LATE_ARRIVAL)),
    ).order_by(Transaction.occurred_at.desc())).all()
    unack = sorted(
        session.scalars(load.where(Transaction.flagged.is_(True), Transaction.label_fraud.is_(None))).all(),
        key=lambda t: (_priority(t) or 9, -_utc(t.occurred_at).timestamp()),
    )
    fraud = session.scalars(load.where(
        Transaction.label_fraud.is_(True), Transaction.occurred_at >= until - timedelta(days=30),
    ).order_by(Transaction.occurred_at.desc())).all()

    spend = sum(env.fx.to_home(float(t.amount), t.currency) for t in period)
    new_alarms = {1: 0, 2: 0, 3: 0}
    for t in period:
        if t.flagged and (p := _priority(t)):
            new_alarms[p] += 1
    unack_by = {p: sum(1 for t in unack if _priority(t) == p) for p in (1, 2, 3)}
    local_day = until.astimezone(tz)

    if unack:
        top = ", ".join(f"{n} {PRIORITY_LABEL[p].split()[1]}" for p, n in unack_by.items() if n)
        subject = f"{APP_NAME} · {local_day:%b %d}: {len(unack)} unacknowledged ({top})"
    else:
        subject = f"{APP_NAME} · {local_day:%b %d}: all clear"

    bank = prefs.get("bank_name") or "your bank"
    phone = prefs.get("bank_phone")
    call = (f"Not yours? Call {bank} at {phone} and quote the authorization code." if phone
            else "Not yours? Contact your bank and quote the authorization code. "
                 "(Add your bank's phone number on the dashboard's Settings page to show it here.)")
    link = prefs.get("dashboard_url")

    def when(t):
        return _utc(t.occurred_at).astimezone(tz).strftime("%Y-%m-%d %H:%M")

    def money(t):
        return f"{t.amount:,.2f} {t.currency}"

    # ---- plain text ----
    lines = [subject, "", f"Covering {since.astimezone(tz):%Y-%m-%d %H:%M} to {until.astimezone(tz):%Y-%m-%d %H:%M} "
             f"({settings.timezone}).",
             f"{len(period)} transaction(s), {spend:,.2f} {env.home_currency}. New alarms: "
             f"{new_alarms[1]} High, {new_alarms[2]} Medium, {new_alarms[3]} Low.", ""]
    if unack:
        lines += [f"NEEDS YOUR ATTENTION ({len(unack)} unacknowledged)", call, ""]
        for t in unack[:MAX_ROWS]:
            lines.append(f"  [{PRIORITY_LABEL[_priority(t) or 3]}] {when(t)}  {money(t)}  {t.merchant}  "
                         f"card …{t.card_last4 or '????'}  auth {t.auth_code or '-'}  ref {t.reference or '-'}  "
                         f"({_alarm_names(t)})")
            if t.anomaly_reasons:
                lines.append(f"      why unusual: {summary(t.anomaly_reasons)}")
        lines.append("")
    else:
        lines += ["No unacknowledged alarms. All clear.", ""]
    if fraud:
        lines += ["MARKED AS FRAUD (last 30 days), for reporting to the bank:"]
        lines += [f"  {when(t)}  {money(t)}  {t.merchant}  card …{t.card_last4 or '????'}  "
                  f"auth {t.auth_code or '-'}  ref {t.reference or '-'}" for t in fraud[:MAX_ROWS]]
        lines.append("")
    if period:
        lines += ["TRANSACTIONS SINCE THE LAST REPORT"]
        def state(t):
            p = _priority(t)
            return ("FRAUD" if t.label_fraud else "-" if p is None
                    else f"{PRIORITY_LABEL[p]}{' legit' if t.label_fraud is False else ''}")
        lines += [f"  {when(t)}  {money(t)}  {t.merchant}  card …{t.card_last4 or '????'}  auth {t.auth_code or '-'}"
                  f"  [{state(t)}]" for t in period[:MAX_ROWS]]
        lines.append("")
    if link:
        lines.append(f"Open the dashboard: {link}/alarms?view=unack")
    lines.append(f"{APP_NAME} is a passive monitor: it never blocks cards, contacts your bank or moves money.")
    text = "\n".join(lines)

    # ---- HTML (inline styles: mail clients ignore stylesheets) ----
    e = html.escape
    def td(extra: str = "") -> str:
        return f'style="padding:6px 8px;border-bottom:1px solid #d5d7da;vertical-align:top;{extra}"'
    th = 'style="padding:6px 8px;text-align:left;font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:#5b5e63;background:#eaebec"'

    def status_html(t):
        p = _priority(t)
        if t.label_fraud:
            return '<b style="color:#c62828">FRAUD</b>'
        if p is None:
            return '<span style="color:#8d9095">—</span>'
        if t.label_fraud is False:
            return f'<span style="color:#5b5e63">{PRIORITY_LABEL[p]} · legit</span>'
        return f'<b style="color:{PRIORITY_COLOR[p]}">{PRIORITY_LABEL[p]}</b>'

    def pri_cell(t):
        return f'<td {td("white-space:nowrap")}>{status_html(t)}</td>'

    def blocks(rows, colour_by_priority=True):
        """One stacked block per alarm: fits a phone screen, key facts first."""
        out = []
        for t in rows[:MAX_ROWS]:
            p = _priority(t) or 3
            edge = PRIORITY_COLOR[p] if colour_by_priority else "#c62828"
            ref = f' · Reference <b style="font-family:monospace">{e(t.reference)}</b>' if t.reference else ""
            out.append(
                f'<div style="border-left:4px solid {edge};background:#f4f4f5;padding:8px 12px;margin:0 0 8px">'
                f'<div>{status_html(t)} &nbsp;<b style="font-size:15px">'
                f'{e(money(t))}</b> · {e(t.merchant or "—")}</div>'
                f'<div style="color:#5b5e63;font-size:13px">{e(when(t))} · card …{e(t.card_last4 or "????")}</div>'
                f'<div style="font-size:13px">Authorization <b style="font-family:monospace;font-size:15px">'
                f'{e(t.auth_code or "—")}</b>{ref}</div>'
                + (f'<div style="color:#5b5e63;font-size:12px">{e(_alarm_names(t))}</div>' if t.alerts else "")
                + (f'<div style="font-size:12px"><b>Why unusual:</b> {e(summary(t.anomaly_reasons))}</div>'
                   if t.anomaly_reasons else "")
                + "</div>"
            )
        if len(rows) > MAX_ROWS:
            out.append(f'<p style="color:#5b5e63">…and {len(rows) - MAX_ROWS} more.</p>')
        return "".join(out)

    def table(rows, with_priority=True, with_alarm=True):
        with_ref = any(t.reference for t in rows)
        cols = ("Time", "Amount", "Merchant", "Card", "Authorization") + (("Reference",) if with_ref else ())
        head = (f"<th {th}>Priority</th>" if with_priority else "") + "".join(
            f"<th {th}>{h}</th>" for h in cols
        ) + (f"<th {th}>Alarm</th>" if with_alarm else "")
        body = "".join(
            "<tr>" + (pri_cell(t) if with_priority else "")
            + f'<td {td("white-space:nowrap")}>{e(when(t))}</td>'
            + f'<td {td("white-space:nowrap;text-align:right")}><b>{e(money(t))}</b></td>'
            + f"<td {td()}>{e(t.merchant or '—')}</td><td {td('white-space:nowrap')}>…{e(t.card_last4 or '????')}</td>"
            + f'<td {td()}><b style="font-family:monospace;font-size:14px">{e(t.auth_code or "—")}</b></td>'
            + (f'<td {td("font-family:monospace")}>{e(t.reference or "—")}</td>' if with_ref else "")
            + (f'<td {td("font-size:12px;color:#5b5e63")}>{e(_alarm_names(t))}</td>' if with_alarm else "")
            + "</tr>"
            for t in rows[:MAX_ROWS]
        )
        more = f'<p style="color:#5b5e63">…and {len(rows) - MAX_ROWS} more.</p>' if len(rows) > MAX_ROWS else ""
        return (f'<div style="overflow-x:auto"><table cellspacing="0" style="border-collapse:collapse;width:100%;'
                f'font-size:13px">{head}{body}</table></div>{more}')

    phone_html = (f'<a href="tel:{e(phone.replace(" ", ""))}" style="color:#1d1e20"><b>{e(phone)}</b></a>' if phone else "")
    call_html = (f"Not yours? Call <b>{e(bank)}</b> at {phone_html} and quote the <b>authorization code</b>." if phone
                 else e(call))
    parts = [
        '<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#1d1e20;max-width:900px">',
        f'<div style="background:#3a3d42;color:#e9eaeb;padding:12px 16px"><b>{e(APP_NAME)}</b> '
        f'<span style="font-size:11px;border:1px solid #b3b6ba;padding:1px 5px;margin-left:6px">PASSIVE MONITOR</span>'
        f'<div style="font-size:13px;color:#b3b6ba;margin-top:4px">Daily report · {e(local_day.strftime("%A %d %B %Y"))}</div></div>',
        f'<div style="background:#eaebec;padding:10px 16px;font-size:13px">'
        f'<b>{len(period)}</b> transaction(s) · <b>{spend:,.2f} {e(env.home_currency)}</b> · new alarms: '
        f'<b style="color:{PRIORITY_COLOR[1]}">{new_alarms[1]} High</b>, <b style="color:{PRIORITY_COLOR[2]}">{new_alarms[2]} Medium</b>, '
        f'<b style="color:{PRIORITY_COLOR[3]}">{new_alarms[3]} Low</b>'
        f'<div style="color:#5b5e63;font-size:12px">{e(since.astimezone(tz).strftime("%Y-%m-%d %H:%M"))} → '
        f'{e(until.astimezone(tz).strftime("%Y-%m-%d %H:%M"))} ({e(settings.timezone)})</div></div>',
    ]
    if unack:
        parts += [
            f'<h3 style="margin:18px 16px 6px">Needs your attention · {len(unack)} unacknowledged</h3>',
            f'<div style="margin:0 16px 8px;padding:10px 12px;border-left:4px solid #c62828;background:#f7e3e1">{call_html}</div>',
            f'<div style="margin:0 16px">{blocks(unack)}</div>',
        ]
    else:
        parts.append('<p style="margin:18px 16px;font-size:15px"><b>All clear.</b> No unacknowledged alarms.</p>')
    if fraud:
        parts += ['<h3 style="margin:18px 16px 6px">Marked as fraud · last 30 days</h3>',
                  f'<div style="margin:0 16px">{blocks(fraud, colour_by_priority=False)}</div>']
    if period:
        parts += ['<h3 style="margin:18px 16px 6px">Transactions since the last report</h3>',
                  f'<div style="margin:0 16px">{table(period, with_alarm=False)}</div>']
    if link:
        parts.append(f'<p style="margin:18px 16px"><a href="{e(link)}/alarms?view=unack" '
                     f'style="background:#3a3d42;color:#fff;padding:8px 12px;text-decoration:none">Review alarms</a></p>')
    parts.append(f'<p style="margin:18px 16px;font-size:12px;color:#5b5e63">{e(APP_NAME)} is a passive monitor: it reads '
                 f'your bank\'s alert emails and never blocks cards, contacts your bank or moves money.</p></div>')
    return Report(subject, text, "".join(parts), len(unack), len(period))


# ---- sending --------------------------------------------------------------------------------------

def send_email(settings: Settings, to: str, subject: str, text: str, html_body: str) -> None:
    user = settings.smtp_user or settings.imap_user
    password = settings.smtp_password or settings.imap_password
    if not (user and password):
        raise RuntimeError("no mail login configured (FRAUDALERT_SMTP_USER/PASSWORD or the IMAP ones)")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)


def send_report(settings: Settings | None = None, now: datetime | None = None, test: bool = False) -> Report:
    """Build and send a report covering everything since the previous one (24h for the first).
    A test report doesn't move the "last sent" marker."""
    settings = settings or get_settings()
    now = _utc(now or datetime.now(timezone.utc))
    with session_scope() as s:
        prefs = get_prefs(s, settings)
        if not prefs["to"]:
            raise RuntimeError("no recipient: set one on the Settings page")
        previous = last_sent(s)
        since = previous if previous and now - previous < timedelta(days=7) else now - timedelta(days=1)
        report = build_report(s, settings, since, now, prefs)
    subject = ("[TEST] " if test else "") + report.subject
    try:
        send_email(settings, prefs["to"], subject, report.text, report.html)
    except Exception as exc:
        with session_scope() as s:
            _set(s, LAST_ERROR_KEY, f"{now.isoformat()} {exc}")
        raise
    with session_scope() as s:
        _set(s, LAST_ERROR_KEY, "")
        if not test:
            _set(s, LAST_SENT_KEY, now.isoformat())
    log.info("sent %sdaily report to %s: %s", "test " if test else "", prefs["to"], subject)
    return report


def is_due(prefs: dict, previous: datetime | None, now: datetime, tz: ZoneInfo) -> bool:
    """Due once the chosen local time has passed today and no report has gone out since then."""
    if not prefs.get("enabled"):
        return False
    hh, mm = (int(x) for x in prefs["time"].split(":"))
    local_now = _utc(now).astimezone(tz)
    today_at = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if local_now < today_at:
        return False
    return previous is None or _utc(previous) < today_at.astimezone(timezone.utc)


def maybe_send_daily_report(settings: Settings | None = None, now: datetime | None = None) -> bool:
    """Called by the worker after each inbox sync."""
    settings = settings or get_settings()
    now = _utc(now or datetime.now(timezone.utc))
    with session_scope() as s:
        prefs = get_prefs(s, settings)
        due = is_due(prefs, last_sent(s), now, ZoneInfo(settings.timezone))
    if due:
        send_report(settings, now)
    return due
