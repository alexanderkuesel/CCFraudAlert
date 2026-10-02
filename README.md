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

## Filtering and bulk edits

The alarm summary's **Filters** panel narrows whichever view you're in (Unacknowledged, All alarms, Journal):
priority, state (unacknowledged / fraud / legit / no alarm), date range (local time), amount (home
currency, the same converted amount rules use), merchant or currency text, card, foreign, alarm rule,
and minimum anomaly score. Filters live in the URL, so back/forward work and a filtered view can be
bookmarked. Each active filter shows as a chip; click its ✕ to drop just that one.

Tick rows (or the header box for the whole page) to act on several at once: **Ack · Legit**, **Ack ·
Fraud**, **Clear ack**, or **Set comment** (empty clears it). When a whole page is selected and more rows
match, **Select all N matching** extends the action to every row the filters match, across pages. Bulk
fraud, and any "all matching" action, asks for confirmation first. Typical use: filter to `netflix`,
select all, **Ack · Legit**.

The same is available over the API:

```bash
curl -X POST localhost:8000/api/transactions/bulk -H 'content-type: application/json' \
     -d '{"ids": [12, 13, 14], "action": "legit"}'        # legit | fraud | clear | comment (+ "comment")
```

## Network map

The **Network** page complements the alarm summary. It draws your cards (squares) and the merchants they were used at (circles, sized by
total spend) as a graph, so unusual patterns stand out at a glance:

* **Colour + glyph = alarm state**, using the same ISA-18.2 priorities as the alarm summary: red **1**,
  amber **2** and slate **3** = an unacknowledged High, Medium or Low alarm; red **✕** with a dark ring =
  acknowledged as fraud; grey **✓** = acknowledged legit; plain grey = normal (ISA-101: normal and handled
  items stay grey). Colour is never the only signal: every state also has a glyph, a label, and a row in
  the table below the map.
* **Dashed ring** = new merchant (first purchase in the last 14 days). **Blue ring** = foreign.
* **Anomaly model on the map:**
  * A **violet halo** shows how unusual a merchant is overall: the share of its transactions that
    score in the top ~15% of your history (≥ 0.85). Everyday merchants stay grey; a merchant that's
    mostly odd transactions glows.
  * A **dotted violet ring** marks merchants with at least one transaction **beyond the anomaly
    limit**. Set the limit with the **Anomaly limit** slider. It starts at your *Unusual pattern*
    alarm threshold (0.97), and the line next to it counts the merchants and transactions beyond it.
  * Tooltips and the merchant table add the score, how many transactions are beyond the limit,
    and **why** the model finds it unusual.
  * The map's positions show who-buys-where, not the model's feature space, so the limit is drawn
    as rings and halos rather than as a line.
* **Hover** a node to see only its connections (which cards used this merchant, or where this card
  was used). **Click** it to pin the details, with a link to its transactions.
* **Highlight anomalies** fades everything normal, leaving flagged, new and foreign merchants.
* Typical things to look for: a card test (a **1** at a new merchant) followed by another **1** on the same
  card, a new foreign merchant hanging off one card, or a merchant only one card has ever used with a large total.

## Spend historian (budget tracking)

Since every card transaction already lands here, the **Spending** page turns it into a SCADA-style
*historian* for your budget:

| SCADA | Here |
|---|---|
| Device | **Category** (Groceries, Dining, Transport, ...) |
| Tag | **Merchant** (one tag per business) |
| Tag value | **Spend** in your home currency (other currencies converted) |
| Setpoint | The category's optional **monthly budget** |
| HI / HIHI limit | **80% / 100%** of that budget, month to date |

* **Tag browser:** every category with a moving-bar indicator (fill = month to date against the budget,
  ticks at HI and the setpoint), a **HI**/**HIHI** badge when a limit is reached, and a 6-month
  sparkline. Expand a category to see its merchants with their month-to-date spend and count.
* **Trend:** pick anything in the browser (everything, a category, or one merchant) to trend it by
  **day, week or month** over 30 days, 90 days or a year. On the month view, HI and HIHI limit lines are
  drawn, and months over a limit are coloured. Every chart has hover tooltips and a table view.
* **Actual vs expected** (setpoint trajectory against process value): the running total for a month
  is plotted against an **expected** curve, which is how your card spending usually builds up through a
  month (the average of the previous 3 complete months, stretched to the month's length) plus that
  month's fixed expenses on their due days. Step back with ‹ › to see how any past month tracked its
  expectation. For the current month, the **forecast** continues from today's actual along the
  expected path. Until there's a complete month on record, it falls back to a straight line from day 7.
* **Expected vs actual, by month:** a bar for what you spent and a tick for what was expected for each
  of the last 6 months (this month shows the forecast too), labelled with the difference in %. Click a
  month to open its running total. Your first partial month of email history is never used as "usual".
* **Categories & tags:** add, rename or delete categories and set or clear their budgets. Merchants are
  categorised automatically from their names the first time they're seen (*auto*). Move a merchant
  from its row, from the trend view, or select several and move them together. What you set is
  marked *you* and never overwritten. Deleting a category moves its merchants to *Uncategorized*.
* **Fixed expenses:** for monthly costs that never reach your card (rent, school fees, transfers,
  cash), add a row on the *Fixed expenses* tab with its amount, currency, category, day of the month and
  the months it applies to (*Until* is optional). Each one is booked on that day every month (the last
  day in shorter months) and shows up as its own tag, marked *fixed*, counting toward its category's
  budget. Nothing is booked in the future, but the month-end projection adds this month's fixed
  expenses at face value and paces only your card spending, so rent on the 1st doesn't inflate it. For
  a price change, set *Until* on the old row and add a new one, so past months keep the old amount.
* In keeping with ISA-101, everything is grey until a budget limit is reached. Transactions you
  acknowledged as **fraud** don't count as spending, and nor do zero-amount card tests.

## Daily report email

Once a day at the time you choose (default **20:00** in `FRAUDALERT_TIMEZONE`), the worker emails a
report laid out for calling your bank:

* **Summary:** transactions and spend since the previous report, and new alarms by priority.
* **Needs your attention:** every unacknowledged alarm, each with the local **time**, **amount**,
  merchant, card, and the bank's own **authorization code** (and **reference** when the email has
  one), right under *"Not yours? Call {your bank} at {phone} and quote the authorization code"*.
* **Marked as fraud (last 30 days):** the same details, ready to report.
* **Transactions since the last report:** each with its alarm state.
* **All clear:** a one-line "all clear" when nothing needs attention.

Set it up on **Settings → Daily report**: on/off, time, recipient (defaults to your IMAP address),
bank name and phone, and an optional dashboard link. Use **Send test report now** to check it.
`fraudalert report [--test]` does the same from the command line.

It's sent through Gmail's SMTP with your existing IMAP login and app password, so there's nothing new
to configure. Set `FRAUDALERT_SMTP_*` to use a different mail server or account. The report is sent
once per day. If the machine was off at report time, the next report covers everything since the
last one.

The authorization code and reference (`Autorización` / `Referencia` in BAC alerts; "Authorization
code" / "Reference number" in English ones) are also shown under the merchant on the alarm summary.
Existing installs fill them in automatically on upgrade, by re-reading the stored emails; your
labels and comments are kept.

## Notifications

Set `FRAUDALERT_NOTIFY_WEBHOOK_URL` to get a POST for every new alarm. The message leads with the
priority (`[HIGH] Transaction alarm: ...`). The payload has `text` (Slack/Mattermost), `content` (Discord),
`priority`, and structured `transaction` fields. Transactions older
than 2 days are not sent, so a historical backfill won't flood you.

## Anomaly detection

Every transaction gets an **anomaly score** from 0 to 1 (the *Anom.* column). Rules can use it, and a
built-in **Low**-priority alarm, *Unusual pattern (anomaly model)*, fires at `anomaly_score >= 0.97`.

**Isolation Forest (default, `FRAUDALERT_DETECTOR=iforest`).** An unsupervised model
(scikit-learn) that learns what *your* normal spending looks like, and isolates transactions that
don't fit. The score is a percentile, so **0.97** means more unusual than 97% of your history.

* **Training data:** every stored transaction except those you acknowledged as fraud, using the
  features in `fraudalert/anomaly/features.py` (amount vs. that merchant and vs. all spending, time
  of day, weekday, how often you use the merchant, time since the previous transaction, bursts,
  foreign).
* **When it trains:** needs **50** transactions. Until then scores come from the statistical
  baseline, and the *Anom.* source is recorded per transaction (`anomaly_model`). The worker
  retrains it **nightly** when new transactions have arrived; you can also run
  `fraudalert train` or click **Retrain now** on **Settings**. Training re-scores your whole history.
* **Storage:** the model is stored in Postgres (`anomaly_models`, newest 5 kept), so web and worker
  share it.
* **Sanity check:** **Settings** shows how well it separates what you acknowledged as fraud from
  legit (AUC), next to the baseline, and how many alarms it would raise per 30 days. Legit rows
  are also training data, so this is a sanity check rather than a benchmark.

**Why is it unusual?** For every score of 0.8 or more, the model states up to three reasons in plain
words, e.g. *"5 other transactions in the past 24 hours · minutes after your previous transaction"*
or *"first purchase at this merchant · at 3 am (you usually shop around 7 pm)"*. It does this by
putting each feature group back to your typical value (the training median) and measuring how much
less unusual the transaction becomes. A group is only named when its value really is atypical. The
reasons appear on the alarm summary, the network map, the API (`anomaly_reasons`) and the daily report.

**Baseline (`FRAUDALERT_DETECTOR=baseline`).** A transparent heuristic: an unusual amount for the
merchant, a large amount at a new merchant, foreign, a new merchant, bursts, and 0–5am.

**Next steps.** Your **Ack · Legit / Ack · Fraud** clicks are stored as labels, and `fraudalert
export-features data.csv` exports features + labels. Once there's plenty of history, an autoencoder
(trained on legit transactions) or, with enough confirmed fraud, a supervised classifier can be
added. `fraudalert/anomaly/base.py` shows the small interface a new model implements.

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
