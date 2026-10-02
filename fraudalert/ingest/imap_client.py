"""Minimal read-only IMAP fetcher (works with Gmail, Outlook/365, Fastmail, iCloud...)."""

import imaplib
import logging
import re
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from email.header import decode_header, make_header

from fraudalert.config import Settings
from fraudalert.ingest.message import EmailMessage, parse_rfc822

log = logging.getLogger(__name__)


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_search(since: date, senders: list[str]) -> str:
    criteria = f"SINCE {since.strftime('%d-%b-%Y')}"
    if not senders:
        return criteria
    # IMAP OR is binary prefix notation: OR FROM a OR FROM b FROM c
    expr = f"FROM {_quote(senders[-1])}"
    for s in reversed(senders[:-1]):
        expr = f"OR FROM {_quote(s)} {expr}"
    return f"{criteria} {expr}"


def fetch_messages(
    settings: Settings,
    since: date,
    already_seen: Callable[[str], bool],
) -> Iterator[EmailMessage]:
    """Yield new messages. Headers are fetched first so known mails are never downloaded twice.

    The mailbox is opened read-only and bodies are fetched with BODY.PEEK, so nothing gets
    marked as read.
    """
    if not settings.imap_user or not settings.imap_password:
        raise RuntimeError("IMAP credentials not configured (FRAUDALERT_IMAP_USER / FRAUDALERT_IMAP_PASSWORD)")

    conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        conn.login(settings.imap_user, settings.imap_password)
        status, _ = conn.select(_quote(settings.imap_folder), readonly=True)
        if status != "OK":
            raise RuntimeError(f"cannot open folder {settings.imap_folder!r}")

        query = build_search(since, settings.senders)
        status, data = conn.uid("SEARCH", None, query)
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {data!r}")
        uids = data[0].split()
        log.info("IMAP search %r matched %d messages", query, len(uids))
        subjects = [s.lower() for s in settings.subjects]

        for uid in uids:
            status, hdr = conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT)])")
            if status != "OK" or not hdr or not isinstance(hdr[0], tuple):
                continue
            header_text = hdr[0][1].decode("utf-8", "replace")
            mid = re.search(r"^Message-ID:\s*(\S+)", header_text, re.I | re.M)
            if mid and already_seen(mid.group(1).strip()):
                continue
            if subjects:
                subj = re.search(r"^Subject:\s*(.*)$", header_text, re.I | re.M)
                subject = str(make_header(decode_header(subj.group(1)))) if subj else ""
                if not any(s in subject.lower() for s in subjects):
                    continue
            status, body = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if status != "OK" or not body or not isinstance(body[0], tuple):
                log.warning("could not fetch uid %s", uid)
                continue
            yield parse_rfc822(body[0][1])
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


# ---- diagnostics ------------------------------------------------------------------------------------

def _internaldate(conn, ref: bytes, by_uid=True):
    status, data = (conn.uid("FETCH", ref, "(INTERNALDATE)") if by_uid else conn.fetch(ref, "(INTERNALDATE)"))
    if status != "OK" or not data or data[0] is None:
        return None
    raw = data[0] if isinstance(data[0], bytes) else data[0][0]
    t = imaplib.Internaldate2tuple(raw)
    return date(t.tm_year, t.tm_mon, t.tm_mday) if t else None


def _search(conn, query: str) -> list[bytes]:
    status, data = conn.uid("SEARCH", None, query)
    if status != "OK":
        raise RuntimeError(f"IMAP search failed: {data!r}")
    return data[0].split() if data and data[0] else []


def _headers(conn, uids: list[bytes], field: str) -> list[str]:
    out = []
    for i in range(0, len(uids), 200):
        status, data = conn.uid("FETCH", b",".join(uids[i:i + 200]), f"(BODY.PEEK[HEADER.FIELDS ({field.upper()})])")
        if status != "OK":
            continue
        for part in data:
            if isinstance(part, tuple):
                text = part[1].decode("utf-8", "replace")
                m = re.search(rf"^{field}:\s*(.*)$", text, re.I | re.M)
                out.append(str(make_header(decode_header(m.group(1)))).strip() if m else "")
    return out


def _sender_keyword(sender: str) -> str:
    """"alertas@notificacionesbaccr.com" -> "notificacionesbaccr" (the domain's main label)."""
    domain = sender.rsplit("@", 1)[-1]
    labels = [l for l in domain.split(".") if l]
    return labels[-2] if len(labels) >= 2 else (labels[0] if labels else sender)


def diagnose(settings: Settings, since: date) -> dict:
    """What the server actually exposes, to explain a backfill that comes up short. Read-only."""
    report: dict = {"folder": settings.imap_folder, "since": since.isoformat(), "folders": [], "hints": []}
    conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        conn.login(settings.imap_user, settings.imap_password)
        status, listing = conn.list()
        if status == "OK":
            for line in listing or []:
                text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                m = re.search(r'"([^"]*)"\s*$', text) or re.search(r"(\S+)\s*$", text)
                if m:
                    report["folders"].append(m.group(1))
        status, data = conn.select(_quote(settings.imap_folder), readonly=True)
        if status != "OK":
            report["hints"].append(f"Can't open folder {settings.imap_folder!r}. Pick one of the folders listed above "
                                   "(Gmail's All Mail is named after your Gmail language, e.g. \"[Gmail]/Todos\").")
            return report
        report["messages_in_folder"] = int(data[0]) if data and data[0] else 0
        report["oldest_in_folder"] = (_internaldate(conn, b"1", by_uid=False).isoformat()
                                      if report["messages_in_folder"] else None)
        since_q = f"SINCE {since.strftime('%d-%b-%Y')}"
        report["since_any_sender"] = len(_search(conn, since_q))
        matched = _search(conn, build_search(since, settings.senders))
        report["matching_filters"] = len(matched)
        report["earliest_match"] = _internaldate(conn, matched[0]).isoformat() if matched else None
        report["latest_match"] = _internaldate(conn, matched[-1]).isoformat() if matched else None
        if settings.subjects and matched:
            subjects = _headers(conn, matched, "Subject")
            wanted = [s.lower() for s in settings.subjects]
            report["dropped_by_subject_filter"] = sum(not any(w in s.lower() for w in wanted) for s in subjects)

        oldest = report["oldest_in_folder"]
        if oldest and oldest > (since + timedelta(days=31)).isoformat():
            report["hints"].append(
                f"The oldest message the server shows in {settings.imap_folder!r} is from {oldest}, so nothing older "
                "can be fetched from this folder. In Gmail, check Settings > See all settings > Forwarding and "
                "POP/IMAP > Folder size limits and choose \"Do not limit the number of messages in an IMAP folder\". "
                "Archived mail is only in All Mail.")
        if settings.senders:
            keywords = sorted({_sender_keyword(s) for s in settings.senders})
            expr = f"FROM {_quote(keywords[-1])}"
            for k in reversed(keywords[:-1]):
                expr = f"OR FROM {_quote(k)} {expr}"
            similar = _search(conn, f"{since_q} {expr}")
            others = [u for u in similar if u not in set(matched)]
            if others:
                tally: dict[str, int] = {}
                for sender in _headers(conn, others[:500], "From"):
                    m = re.search(r"[\w.+-]+@[\w.-]+", sender)
                    addr = (m.group(0) if m else sender).lower()
                    tally[addr] = tally.get(addr, 0) + 1
                report["other_senders"] = sorted(tally.items(), key=lambda kv: -kv[1])
                report["hints"].append(
                    "Your bank also sent mail from other addresses (listed above) that your FRAUDALERT_SENDER_FILTER "
                    "leaves out. If they're transaction alerts, add them to the filter, separated by commas.")
        if report["matching_filters"] == 0 and report["since_any_sender"]:
            report["hints"].append("Nothing matches your sender filter in this range. Check FRAUDALERT_SENDER_FILTER.")
        if report.get("dropped_by_subject_filter"):
            report["hints"].append(f"{report['dropped_by_subject_filter']} matching emails are skipped by "
                                   "FRAUDALERT_SUBJECT_FILTER (older alerts may have had a different subject).")
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return report
