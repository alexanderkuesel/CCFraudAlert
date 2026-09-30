"""Turn raw RFC822 bytes into a small, parser-friendly structure."""

import email
import email.policy
import re
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage as StdEmailMessage
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

_BLOCK_TAGS = {"br", "p", "div", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
_CELL_TAGS = {"td", "th"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in _BLOCK_TAGS or tag in _CELL_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS or tag in _CELL_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


@dataclass
class EmailMessage:
    message_id: str
    sender: str
    subject: str
    received_at: datetime | None
    body: str  # plain text (HTML is converted)


def _body_text(msg: StdEmailMessage) -> str:
    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    # Bank alerts often ship a useless plain part ("view this email in a browser"); prefer HTML
    # when it carries noticeably more content.
    plain_text = plain.get_content() if plain is not None else ""
    html_text = html_to_text(html.get_content()) if html is not None else ""
    if len(html_text) > len(plain_text) * 1.2:
        return html_text
    return plain_text or html_text


def parse_rfc822(raw: bytes) -> EmailMessage:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    received_at = None
    if msg["Date"]:
        try:
            received_at = parsedate_to_datetime(str(msg["Date"]))
        except (TypeError, ValueError):
            pass
    message_id = str(msg["Message-ID"] or "").strip()
    if not message_id:
        # Fall back to a content hash so re-imports still dedupe.
        import hashlib

        message_id = "<sha256-" + hashlib.sha256(raw).hexdigest() + ">"
    return EmailMessage(
        message_id=message_id,
        sender=str(msg["From"] or ""),
        subject=str(msg["Subject"] or ""),
        received_at=received_at,
        body=_body_text(msg),
    )
