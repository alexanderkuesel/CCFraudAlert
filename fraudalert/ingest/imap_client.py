"""Minimal read-only IMAP fetcher (works with Gmail, Outlook/365, Fastmail, iCloud...)."""

import imaplib
import logging
import re
from collections.abc import Callable, Iterator
from datetime import date
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
