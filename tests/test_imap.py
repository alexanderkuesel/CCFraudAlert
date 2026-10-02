from datetime import date

from fraudalert.ingest.imap_client import build_search


def test_build_search():
    d = date(2026, 9, 1)
    assert build_search(d, []) == "SINCE 01-Sep-2026"
    assert build_search(d, ["a@x.com"]) == 'SINCE 01-Sep-2026 FROM "a@x.com"'
    assert build_search(d, ["a", "b", "c"]) == 'SINCE 01-Sep-2026 OR FROM "a" OR FROM "b" FROM "c"'


class FakeIMAP:
    """Just enough of imaplib.IMAP4_SSL for fetch_messages()."""

    mailbox: dict[bytes, bytes] = {}
    fetched_bodies: list[bytes] = []

    def __init__(self, host, port):
        pass

    def login(self, user, pw):
        assert (user, pw) == ("me@example.com", "app-pw")

    def select(self, folder, readonly=False):
        assert readonly
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [b" ".join(self.mailbox)]
        uid, what = args
        raw = self.mailbox[uid]
        if "HEADER.FIELDS" in what:
            head = raw.split(b"\n\n", 1)[0]
            keep = b"\r\n".join(l for l in head.splitlines() if l.lower().startswith((b"message-id", b"subject")))
            return "OK", [(b"hdr", keep + b"\r\n")]
        assert "PEEK" in what  # never mark mail as read
        self.fetched_bodies.append(uid)
        return "OK", [(b"body", raw)]

    def logout(self):
        pass


def test_sync_inbox_with_fake_imap(db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from fraudalert import pipeline
    from fraudalert.config import get_settings
    from fraudalert.ingest import imap_client

    from .conftest import make_eml

    now = datetime.now(timezone.utc)
    FakeIMAP.mailbox = {
        b"1": make_eml("Purchase alert", "You spent $12.00 at DELI.", now - timedelta(days=1)),
        b"2": make_eml("Purchase alert", "You spent $700.00 at JEWELER.", now - timedelta(hours=2)),
        b"3": make_eml("Newsletter", "Save $5.00 today", now),
    }
    FakeIMAP.fetched_bodies = []
    monkeypatch.setattr(imap_client.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setenv("FRAUDALERT_IMAP_USER", "me@example.com")
    monkeypatch.setenv("FRAUDALERT_IMAP_PASSWORD", "app-pw")
    monkeypatch.setenv("FRAUDALERT_SUBJECT_FILTER", "purchase, transaction")
    get_settings.cache_clear()

    r = pipeline.sync_inbox()
    assert (r.fetched, r.parsed, r.flagged, r.errors) == (2, 2, 1, [])
    assert sorted(FakeIMAP.fetched_bodies) == [b"1", b"2"]  # newsletter filtered on headers alone

    FakeIMAP.fetched_bodies = []
    r = pipeline.sync_inbox()
    assert r.fetched == 0 and FakeIMAP.fetched_bodies == []  # known mail is never re-downloaded
    get_settings.cache_clear()


def test_backfill_fetches_history_without_flooding_alarms(db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from fraudalert import pipeline
    from fraudalert.config import get_settings
    from fraudalert.ingest import imap_client
    from fraudalert.models import SyncState, Transaction

    from .conftest import make_eml

    queries, folders = [], []

    class RecordingIMAP(FakeIMAP):
        def select(self, folder, readonly=False):
            folders.append(folder)
            return super().select(folder, readonly)

        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                queries.append(args[1])
            return super().uid(cmd, *args)

    now = datetime.now(timezone.utc)
    FakeIMAP.mailbox = {b"1": make_eml("Purchase alert", "You spent $12.00 at DELI.", now - timedelta(days=1))}
    monkeypatch.setattr(imap_client.imaplib, "IMAP4_SSL", RecordingIMAP)
    monkeypatch.setenv("FRAUDALERT_IMAP_USER", "me@example.com")
    monkeypatch.setenv("FRAUDALERT_IMAP_PASSWORD", "app-pw")
    get_settings.cache_clear()
    pipeline.sync_inbox()
    with db.session_scope() as s:
        position = s.get(SyncState, "last_imap_sync").value

    FakeIMAP.mailbox.update({
        b"2": make_eml("Purchase alert", "You spent $700.00 at JEWELER.", now - timedelta(days=700)),
        b"3": make_eml("Purchase alert", "You spent $15.00 at DELI.", now - timedelta(days=600)),
        b"4": make_eml("Purchase alert", "You spent $500.00 at HOTEL NOVA.", now - timedelta(days=10)),
    })
    r = pipeline.backfill_inbox(date(2024, 1, 1), folder="[Gmail]/All Mail")
    assert (r.fetched, r.parsed, r.errors, r.acknowledged) == (3, 3, [], 1)
    assert queries[-1].startswith("SINCE 01-Jan-2024") and folders[-1] == '"[Gmail]/All Mail"'
    with db.session_scope() as s:
        assert s.get(SyncState, "last_imap_sync").value == position  # the regular sync is untouched
        txns = {t.merchant: t for t in s.scalars(select(Transaction))}
        assert txns["JEWELER"].label_fraud is False and txns["JEWELER"].comment == pipeline.BACKFILL_NOTE
        assert txns["HOTEL NOVA"].flagged and txns["HOTEL NOVA"].label_fraud is None  # recent: still yours to review
        assert txns["DELI"].label_fraud is None

    FakeIMAP.mailbox[b"5"] = make_eml("Purchase alert", "You spent $900.00 at YACHT CLUB.", now - timedelta(days=400))
    r = pipeline.backfill_inbox(date(2024, 1, 1), ack_older_than_days=None)
    assert (r.fetched, r.acknowledged) == (1, 0)  # only the new email; nothing acknowledged when asked not to
    with db.session_scope() as s:
        assert s.scalar(select(Transaction.label_fraud).where(Transaction.merchant == "YACHT CLUB")) is None
    get_settings.cache_clear()


def test_backfill_since_argument():
    from fraudalert.cli import _parse_since

    assert _parse_since("2022-03-01") == date(2022, 3, 1)
    assert _parse_since("3y").year == date.today().year - 3
    assert _parse_since("2 years").year == date.today().year - 2
    assert _parse_since("soon") is None


class SearchingIMAP:
    """A fake server that honours SINCE / FROM / OR, INTERNALDATE and LIST, for diagnose()."""

    folders = {"INBOX": [], "[Gmail]/Todos": []}  # folder -> [(date, from, subject)]

    def __init__(self, host, port):
        self.box = []

    def login(self, user, pw):
        pass

    def list(self):
        return "OK", [f'(\\\\HasNoChildren) "/" "{f}"'.encode() for f in self.folders]

    def select(self, folder, readonly=False):
        name = folder.strip('"')
        if name not in self.folders:
            return "NO", [b"no such folder"]
        self.box = self.folders[name]
        return "OK", [str(len(self.box)).encode()]

    def _match(self, query, msg):
        import re as _re
        from datetime import datetime as _dt

        since = _dt.strptime(_re.search(r"SINCE (\S+)", query).group(1), "%d-%b-%Y").date()
        froms = _re.findall(r'FROM "([^"]+)"', query)
        return msg[0] >= since and (not froms or any(f.lower() in msg[1].lower() for f in froms))

    def _date(self, i):
        return f'{i + 1} (INTERNALDATE "{self.box[i][0].strftime("%d-%b-%Y")} 12:00:00 +0000")'.encode()

    def fetch(self, seq, what):
        return "OK", [self._date(int(seq) - 1)]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [b" ".join(str(i + 1).encode() for i, m in enumerate(self.box) if self._match(args[1], m))]
        ids, what = args
        idx = [int(x) - 1 for x in ids.split(b",")]
        if "INTERNALDATE" in what:
            return "OK", [self._date(idx[0])]
        field = "From" if "FROM" in what else "Subject"
        pos = 1 if field == "From" else 2
        return "OK", [(b"h", f"{field}: {self.box[i][pos]}\r\n".encode()) for i in idx]

    def logout(self):
        pass


def test_imap_check_explains_a_short_backfill(monkeypatch):
    from fraudalert.config import Settings
    from fraudalert.ingest import imap_client

    new, old = "alertas@notificacionesbaccr.com", "notificacion@baccredomatic.cr"
    SearchingIMAP.folders = {
        # Gmail's folder size limit: INBOX only shows the newest few months
        "INBOX": [(date(2026, 6, 1), new, "Notificación de transacción")],
        "[Gmail]/Todos": [(date(2023, 2, 1), old, "Alerta de transacción"), (date(2023, 3, 1), old, "Alerta de transacción"),
                          (date(2024, 5, 1), "promos@baccredomatic.cr", "Ofertas"),
                          (date(2025, 1, 1), new, "Notificación de transacción"),
                          (date(2026, 6, 1), new, "Notificación de transacción")],
    }
    monkeypatch.setattr(imap_client.imaplib, "IMAP4_SSL", SearchingIMAP)
    settings = Settings(imap_user="me", imap_password="pw", sender_filter=new + ", " + old.replace("notificacion@", "x@"))

    r = imap_client.diagnose(settings, date(2023, 1, 1))
    assert r["folders"] == ["INBOX", "[Gmail]/Todos"]
    assert r["oldest_in_folder"] == "2026-06-01" and r["matching_filters"] == 1
    assert any("Folder size limits" in h for h in r["hints"])

    r = imap_client.diagnose(settings.model_copy(update={"imap_folder": "[Gmail]/Todos"}), date(2023, 1, 1))
    assert (r["messages_in_folder"], r["since_any_sender"], r["matching_filters"]) == (5, 5, 2)
    assert r["earliest_match"] == "2025-01-01"
    assert dict(r["other_senders"]) == {old: 2, "promos@baccredomatic.cr": 1}
    assert any("SENDER_FILTER" in h for h in r["hints"]) and not any("Folder size" in h for h in r["hints"])

    r = imap_client.diagnose(settings.model_copy(update={"imap_folder": "[Gmail]/All Mail"}), date(2023, 1, 1))
    assert any("Can't open folder" in h for h in r["hints"])
    assert imap_client._sender_keyword(new) == "notificacionesbaccr"
