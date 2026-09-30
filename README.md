# CC Transaction Alarm Dashboard

A **passive**, SCADA-style alarm dashboard for your credit-card transactions.

It reads the transaction alert emails your bank already sends you, stores each transaction in PostgreSQL,
and raises **alarms** when a transaction looks wrong. You then acknowledge each alarm as legit or fraud,
the way an operator works an alarm list in a control room.

> **Passive by design.** The dashboard only *observes*: it reads your mailbox read-only (it never
> marks, moves or sends mail), and it never blocks a card, contacts your bank or moves money. Acting on
> an alarm (calling the bank, freezing the card) is always your decision. It is also **not real-time**:
> it sees a transaction only once the bank's email arrives and the next inbox sync runs (every 5
> minutes by default).

## Inspired by SCADA alarm management

Industrial control rooms have spent decades learning how to alert a human without drowning them in
noise. This project deliberately borrows that discipline and applies it to card transactions:

* **ISA-18.2 (alarm management)** defines what an alarm is, its lifecycle and its priorities.
* **ISA-101 (HMI design)** defines the "high-performance HMI" look: grey and quiet when things are
  normal, with colour reserved for abnormal conditions, so an alarm stands out the moment it appears.

(ISA-95, often mentioned alongside these, covers integrating business and control systems; it doesn't
define alarm handling, so the alarm behaviour here follows ISA-18.2.)

| ISA-18.2 / SCADA concept | In this dashboard |
|---|---|
| Process event | A card transaction parsed from a bank alert email |
| Alarm | A rule matching a transaction (see **Alarm rules**) |
| Alarm priority | **1 High** (act now), **2 Medium** (check today), **3 Low** (review when convenient); shown by colour, shape *and* number (red square, amber triangle, slate diamond) |
| Unacknowledged alarm | Flashes in the alarm summary and counts in the banner at the top of every page |
| Acknowledge | **Ack · Legit** / **Ack · Fraud**: your review. The disposition is kept, and doubles as a training label for the anomaly model |
| Latched alarm | Transactions are discrete events, so there is no "return to normal": an alarm stays active until you acknowledge it |
| Alarm summary / journal | **Alarm summary** page: *Unacknowledged* (default), *All alarms*, and *Journal* (every transaction) |
| Rationalization | Each rule carries a rationale (why it exists) and a priority. Keep High rare so it keeps its meaning |
| Alarm system KPIs | Alarm rate per day, and priority mix vs. the ISA-18.2 guideline of roughly 5% High / 15% Medium / 80% Low |

Built-in alarms:

* **Card test (zero/near-zero amount)** (High): a `$0.00`-style authorisation. Fraudsters verify a
  stolen card this way right before using it.
* **Charge after a card test** (High): a real charge on the same card within 48 hours of a test-sized one.
* **Large or foreign purchase** (Medium): over 100 in your home currency, made abroad, or in a currency
  you don't normally use.

Existing installs receive new built-in alarms automatically on upgrade (once; if you delete one, it stays deleted).

```
 IMAP inbox ──► parse email ──► transaction ──► features ──► anomaly score ──► alarm rules ──► alarm summary
 (read-only)     (parsers.py)   (PostgreSQL)   (features.py)  (anomaly/*.py)   (engine.py)     (UI + webhook)
```

The internal names (`fraudalert` package and CLI, `FRAUDALERT_*` settings) are unchanged so existing
installs keep working.

## Quick start (Docker)

```bash
cp .env.example .env        # fill in IMAP credentials + sender filter
docker compose up -d        # postgres + web UI (http://localhost:8000) + worker (syncs every 5 min)
```

The compose database is published on host port **5433**, so it doesn't clash with a Postgres you may
already run on 5432. Change `FRAUDALERT_DB_HOST_PORT` / `FRAUDALERT_WEB_HOST_PORT` in `.env` if those
ports are taken too.

## Quick start (local)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                     # point FRAUDALERT_DATABASE_URL at your Postgres
fraudalert init-db                       # creates tables + the default rule
fraudalert sync                          # pull bank emails once
fraudalert serve --sync-interval 300     # web UI on http://127.0.0.1:8000, syncing every 5 min
```

Try it without touching your inbox:

```bash
python examples/generate_samples.py examples/sample_emails
fraudalert import-eml examples/sample_emails
fraudalert serve
```

### Inbox setup

* **Gmail**: turn on 2-step verification, create an [App Password](https://myaccount.google.com/apppasswords),
  and use it as `FRAUDALERT_IMAP_PASSWORD`. Host `imap.gmail.com`.
* **Outlook / iCloud / Fastmail**: use their IMAP host and an app-specific password.
* Set `FRAUDALERT_SENDER_FILTER` to your bank's alert address (e.g. `no.reply.alerts@chase.com`).
  You can list several, separated by commas. Optionally set `FRAUDALERT_SUBJECT_FILTER` too.
* The mailbox is opened **read-only** and messages are fetched with `BODY.PEEK`, so nothing is marked as
  read. The first sync looks back `FRAUDALERT_LOOKBACK_DAYS` (90 by default). Later syncs are incremental,
  and emails are de-duplicated by Message-ID.
* Your bank also has to send the alerts: in its app, set the "transaction alert" threshold to $0.01
  so every purchase produces an email.

## Alarm rules

Rules are stored in the database, so you can add, disable or delete them from the **Alarm rules** page or the
API while the pipeline is running. Every change is applied to your whole history straight away.

A rule is a list of conditions joined by **ALL** (AND) or **ANY** (OR), plus a priority. For example:

> **Large or foreign purchase** (Medium): `amount > 100 OR is_foreign = true`
> **Card test** (High): `is_test_amount = true`

| field           | type   | notes                                                      |
|-----------------|--------|------------------------------------------------------------|
| `amount`        | number | converted to `FRAUDALERT_HOME_CURRENCY` (approximate rates, see `fraudalert/fx.py`) |
| `amount_original` | number | as charged, in `currency`                                |
| `currency`      | text   | ISO code, e.g. `EUR`                                       |
| `merchant`      | text   | case-insensitive                                           |
| `card_last4`    | text   |                                                            |
| `is_foreign`    | bool   | bought outside `FRAUDALERT_HOME_COUNTRY` (when the email names a country, or says "foreign transaction"), **or** in a currency that isn't one of your normal currencies |
| `unusual_currency` | bool | currency isn't one of your normal currencies (Settings page / `FRAUDALERT_NORMAL_CURRENCIES`) |
| `is_test_amount` | bool  | amount (home currency) ≤ `FRAUDALERT_TEST_AMOUNT_MAX` (default 1.0), e.g. a `$0.00` authorisation |
| `follows_test`  | bool   | the same card had a test-sized transaction within `FRAUDALERT_TEST_FOLLOWUP_HOURS` (default 48) before this one |
| `hour`          | number | 0–23 in `FRAUDALERT_TIMEZONE`                              |
| `weekday`       | number | 0 = Monday                                                 |
| `anomaly_score` | number | 0–1 from the anomaly detector (empty until ~10 transactions of history) |

Operators: `gt gte lt lte eq ne contains not_contains in not_in regex`.

API example:

```bash
curl -X POST localhost:8000/api/rules -H 'content-type: application/json' -d '{
  "name": "Late-night online", "match": "all", "severity": "high",
  "conditions": [{"field": "hour", "op": "lt", "value": 5},
                 {"field": "merchant", "op": "regex", "value": "amazon|paypal|apple"}]}'
```

Other endpoints: `GET /api/transactions?flagged=true`, `GET /api/rules`, `DELETE /api/rules/{id}`, `POST /api/sync`.

## Notifications

Set `FRAUDALERT_NOTIFY_WEBHOOK_URL` to get a POST for every new alarm. The message leads with the
priority (`[HIGH] Transaction alarm: ...`). The payload has `text` (Slack/Mattermost), `content` (Discord),
`priority`, and structured `transaction` fields. Transactions older
than 2 days are not sent, so a historical backfill won't flood you.

## Anomaly detection: the road to a neural net

Everything a model needs is already collected:

* **Features.** `fraudalert/anomaly/features.py` turns each transaction plus its history into a fixed
  vector (`FEATURE_NAMES`): log amount, foreign flag, cyclical hour and weekday, how often you've used
  the merchant, robust z-scores against the merchant's history and your overall history, time since
  the previous transaction, and the count in the last 24h. Each transaction is stored with the exact
  vector it was scored on.
* **Labels.** In the UI you can mark any transaction ✓ legit or ✗ fraud (`transactions.label_fraud`).
* **Training data.** `fraudalert export-features data.csv` writes the feature vectors, scores and labels.
* **Pluggable detector.** `fraudalert/anomaly/base.py` defines `AnomalyDetector` (`fit(rows)` and
  `score(features) -> 0..1`). Register an implementation and choose it with `FRAUDALERT_DETECTOR`.
  Scores go into `anomaly_score`, so rules like `anomaly_score >= 0.8` work with any model and the
  pipeline doesn't change.

The shipped `baseline` detector is a transparent heuristic (unusual amount for that merchant, a large
amount at a new merchant, foreign currency, a new merchant, bursts of transactions, 0–5am activity). It
works from the first day and gives a future model something to beat. A natural next step is an
autoencoder (or an Isolation Forest to start) trained on transactions labelled legit, with the score
taken from the reconstruction error. Once you have enough ✗ fraud labels, a supervised classifier.

## Parsing emails

`GenericAlertParser` handles the common formats: inline ("You made a $42.10 transaction with
MERCHANT"), labelled fields ("Merchant: …", "Amount: …"), HTML tables, currency symbols and ISO codes
(`$ € £ ¥ …`, `EUR 48,90`, `5,000 JPY`), US and European number formats, and card numbers ("ending in
1234", "****1234"). Statement, payment and login emails are rejected.

`SpanishAlertParser` reads label/value alerts in Spanish (Comercio, Monto, Fecha, Ciudad y país, Tipo de
Transacción), as sent by BAC Credomatic and similar banks. Refunds and reversals are skipped. For a Costa
Rica setup:

```bash
FRAUDALERT_HOME_CURRENCY=USD            # or CRC; colones/dollars are converted either way
FRAUDALERT_NORMAL_CURRENCIES=CRC,USD    # anything else counts as foreign (also on the Settings page)
FRAUDALERT_HOME_COUNTRY=Costa Rica
FRAUDALERT_TIMEZONE=America/Costa_Rica
```

Emails from your bank that couldn't be parsed are listed on the **Emails** page with the reason. If
your bank uses an unusual format, add a `BaseParser` subclass to `fraudalert/ingest/parsers.py`
(ahead of the generic one). A specific parser that recognises an email has the final say: the
generic heuristics only run for emails no specific parser claims.

After updating the app, click **Re-parse all emails** on the Emails page (or run
`fraudalert reevaluate --reparse-all`) to re-read every stored email with the new parsers. Transactions
are updated in place, so your ✓ legit / ✗ fraud labels are kept. **Retry parsing** (`--reparse`) only
retries emails that failed.

## Development

```bash
pytest                                                   # SQLite
FRAUDALERT_TEST_DATABASE_URL=postgresql+psycopg://…/fraudalert_test pytest   # against Postgres
```

Layout: `ingest/` (IMAP, MIME, parsers) · `rules/engine.py` · `anomaly/` (features, detectors) ·
`pipeline.py` (orchestration) · `web/` (FastAPI + Jinja) · `cli.py`.

## Security notes

* The web UI is only reachable from the machine it runs on by default (`127.0.0.1`).
* Changes (POST/DELETE) coming from another website are rejected, so a page you visit can't use
  your saved login to edit rules behind your back.
* Basic auth over plain HTTP is fine on a trusted home network. Don't forward the port to the
  internet; use a VPN such as Tailscale or WireGuard, or put it behind HTTPS.

## Accessing the UI from other computers on your network

Add to `.env`:

```bash
FRAUDALERT_WEB_BIND=0.0.0.0
FRAUDALERT_WEB_USERNAME=you
FRAUDALERT_WEB_PASSWORD=a-long-random-password
```

Then run `docker compose up -d`, and browse to `http://<this-machine's-LAN-IP>:8000` from another
computer. The app refuses to start on the network without a username and password. Outside Docker,
the same applies to `fraudalert serve --host 0.0.0.0`.

If it still doesn't load, the host firewall is usually the cause. Allow inbound TCP 8000: on Windows
use "Allow an app through firewall"; on Linux with ufw run `sudo ufw allow 8000/tcp`; on macOS allow
Docker in System Settings → Network → Firewall.
* Use an app password, never your main email password. `.env` is git-ignored.
* Raw email bodies are stored in the database so they can be re-parsed later. Treat the database as
  sensitive.
