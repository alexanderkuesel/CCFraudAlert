import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from fraudalert import pipeline, prefs
from fraudalert.config import get_settings
from fraudalert.db import init_db, session_scope
from fraudalert.models import Alert, RawEmail, Rule, SyncState, Transaction
from fraudalert.web.filters import PRIORITIES, PRIORITY_RANK, STATES, VIEWS, Filters, txn_priority
from fraudalert.rules.engine import FIELDS, OPS, RuleError, RuleSpec, describe, validate_rule

HERE = Path(__file__).parent
PAGE_SIZE = 50
SEVERITIES = ["high", "medium", "low"]
APP_NAME = "CC Transaction Alarm Dashboard"
COMMENT_MAX = 1000
NETWORK_RANGES = {"30": 30, "90": 90, "365": 365, "all": None}

_basic = HTTPBasic(auto_error=False)


def require_auth(creds: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    s = get_settings()
    if not s.web_username:
        return
    ok = creds is not None and (
        secrets.compare_digest(creds.username.encode(), s.web_username.encode())
        and secrets.compare_digest(creds.password.encode(), s.web_password.encode())
    )
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    yield


# ---- ISA-18.2 alarm vocabulary --------------------------------------------------------------
# Rules store severity as low/medium/high; the UI shows it as alarm priority P1-P3.
PRIORITY_NAME = {1: "High", 2: "Medium", 3: "Low"}
# ISA-18.2 guidance for a healthy system: roughly 5% high, 15% medium, 80% low priority alarms.
PRIORITY_TARGET = {1: 5, 2: 15, 3: 80}


BULK_ACTIONS = {
    "legit": "Acknowledged {n} transaction{s} as legit.",
    "fraud": "Acknowledged {n} transaction{s} as fraud.",
    "clear": "Cleared the acknowledgement on {n} transaction{s}.",
    "comment": "Updated the comment on {n} transaction{s}.",
}


def apply_bulk(s, ids: list[int], action: str, comment: str = "") -> int:
    """Returns how many transactions were changed."""
    if not ids:
        return 0
    rows = s.scalars(select(Transaction).where(Transaction.id.in_(ids))).all()
    text = comment.strip()[:COMMENT_MAX] or None
    for t in rows:
        if action == "legit":
            t.label_fraud = False
        elif action == "fraud":
            t.label_fraud = True
        elif action == "clear":
            t.label_fraud = None
        elif action == "comment":
            t.comment = text
    return len(rows)


def unack_by_priority(s) -> dict[int, int]:
    """Unacknowledged alarms per priority, counting each transaction once at its highest priority."""
    best: dict[int, int] = {}
    for txn_id, severity in s.execute(
        select(Alert.transaction_id, Alert.severity).join(Transaction)
        .where(Transaction.flagged.is_(True), Transaction.label_fraud.is_(None))
    ):
        rank = PRIORITY_RANK.get(severity, 3)
        best[txn_id] = min(best.get(txn_id, 9), rank)
    counts = {1: 0, 2: 0, 3: 0}
    for rank in best.values():
        counts[rank] += 1
    return counts


def alarm_kpis(s) -> dict:
    """ISA-18.2 style performance indicators over the last 30 days of transactions."""
    since = datetime.now(timezone.utc) - timedelta(days=30)
    best: dict[int, int] = {}
    for txn_id, severity in s.execute(
        select(Alert.transaction_id, Alert.severity).join(Transaction).where(Transaction.occurred_at >= since)
    ):
        best[txn_id] = min(best.get(txn_id, 9), PRIORITY_RANK.get(severity, 3))
    n = len(best)
    mix = {p: round(100 * sum(1 for r in best.values() if r == p) / n) if n else 0 for p in (1, 2, 3)}
    week = datetime.now(timezone.utc) - timedelta(days=7)
    last7 = s.scalar(select(func.count(Transaction.id)).where(
        Transaction.flagged.is_(True), Transaction.occurred_at >= week))
    return {"alarms_30d": n, "mix": mix, "target": PRIORITY_TARGET, "per_day_7d": round(last7 / 7, 1)}


def _local(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(get_settings().timezone)).strftime(fmt)


def create_app(init: bool = True) -> FastAPI:
    app = FastAPI(title=APP_NAME, dependencies=[Depends(require_auth)], lifespan=_lifespan if init else None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(describe=lambda r: describe(RuleSpec(r.id, r.name, r.match, r.conditions)))
    templates.env.filters["local"] = _local

    def banner() -> dict:
        with session_scope() as s:
            counts = unack_by_priority(s)
        return {"counts": counts, "total": sum(counts.values())}

    templates.env.globals.update(
        alarm_banner=banner, txn_priority=txn_priority,
        alarms_by_priority=lambda t: sorted(t.alerts, key=lambda a: PRIORITY_RANK.get(a.severity, 3)), PRIORITY_NAME=PRIORITY_NAME, PRIORITY_RANK=PRIORITY_RANK,
        VIEWS=VIEWS, APP_NAME=APP_NAME,
    )

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        """Block cross-site form posts. Browsers resend basic-auth credentials automatically, so
        without this any web page you visit could POST to e.g. /rules/1/delete on your network."""
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            source = request.headers.get("origin") or request.headers.get("referer")
            if source and urlsplit(source).netloc != request.headers.get("host"):
                return PlainTextResponse("Cross-site request blocked", status_code=403)
        return await call_next(request)

    def redirect(path: str, msg: str | None = None, error: str | None = None) -> RedirectResponse:
        q = {k: v for k, v in {"msg": msg, "error": error}.items() if v}
        sep = "&" if "?" in path else "?"
        return RedirectResponse(path + ((sep + urlencode(q)) if q else ""), status_code=303)

    # ---------- HTML ----------

    @app.get("/")
    def alarm_summary(request: Request, page: int = 1):
        """ISA-18.2-style alarm summary: view tabs (unack / alarms / journal) refined by filters."""
        f = Filters.from_params(request.query_params)
        page = max(page, 1)
        settings = get_settings()
        with session_scope() as s:
            env = pipeline.Env.load(s, settings)
            matches = f.apply(s, env)
            total = len(matches)
            rows = matches[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
            counts = {
                "unack": s.scalar(select(func.count(Transaction.id)).where(
                    Transaction.flagged.is_(True), Transaction.label_fraud.is_(None))),
                "alarms": s.scalar(select(func.count(Transaction.id)).where(Transaction.flagged.is_(True))),
                "journal": s.scalar(select(func.count(Transaction.id))),
                "unparsed": s.scalar(select(func.count(RawEmail.id)).where(RawEmail.parse_status == "failed")),
            }
            rule_names = dict(s.execute(select(Rule.id, Rule.name).order_by(Rule.id)).all())
            cards = sorted(c for c in s.scalars(select(Transaction.card_last4).distinct()) if c)
            last_sync = s.get(SyncState, "last_imap_sync")
            kpi = alarm_kpis(s)
        return templates.TemplateResponse(
            request,
            "transactions.html",
            {
                "rows": rows, "f": f, "view": f.view, "page": page, "total": total, "counts": counts,
                "pages": max(1, -(-total // PAGE_SIZE)), "kpi": kpi,
                "chips": f.chips(rule_names, env.home_currency), "rule_names": rule_names, "cards": cards,
                "currency": env.home_currency, "PRIORITIES": PRIORITIES, "STATES": STATES,
                "last_sync": _local(datetime.fromisoformat(last_sync.value)) if last_sync else None,
                "tz": settings.timezone,
                "normal": env.normal_currencies,
                "is_foreign": env.is_foreign,
            },
        )

    @app.post("/transactions/bulk")
    async def bulk_edit(request: Request):
        """Apply one action to the selected rows, or to every row matching the page's filters."""
        form = await request.form()
        qs = str(form.get("filters", ""))
        back = "/?" + qs if qs else "/"
        action = str(form.get("action", ""))
        if action not in BULK_ACTIONS:
            return redirect(back, error="choose a bulk action")
        with session_scope() as s:
            if form.get("all_matching") == "1":
                f = Filters.from_query_string(qs)
                ids = [t.id for t in f.apply(s, pipeline.Env.load(s, get_settings()))]
            else:
                ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
            n = apply_bulk(s, ids, action, str(form.get("comment", "")))
        if not n:
            return redirect(back, error="nothing selected")
        return redirect(back, msg=BULK_ACTIONS[action].format(n=n, s="" if n == 1 else "s"))

    @app.post("/transactions/{txn_id}/label")
    async def label_transaction(txn_id: int, request: Request):
        form = await request.form()
        value = {"fraud": True, "legit": False}.get(str(form.get("label")))
        with session_scope() as s:
            txn = s.get(Transaction, txn_id)
            if not txn:
                raise HTTPException(404)
            txn.label_fraud = value
        return RedirectResponse(request.headers.get("referer") or "/", status_code=303)

    @app.post("/transactions/{txn_id}/comment")
    async def comment_transaction(txn_id: int, request: Request):
        form = await request.form()
        text = str(form.get("comment", "")).strip()[:COMMENT_MAX]
        with session_scope() as s:
            txn = s.get(Transaction, txn_id)
            if not txn:
                raise HTTPException(404)
            txn.comment = text or None
        if request.headers.get("x-requested-with") == "fetch":  # inline save from the table
            return PlainTextResponse("saved")
        return RedirectResponse(request.headers.get("referer") or "/", status_code=303)

    @app.get("/network")
    def network_page(request: Request, days: str = "90"):
        from fraudalert.network import NEW_MERCHANT_DAYS

        return templates.TemplateResponse(request, "network.html", {
            "days": days if days in NETWORK_RANGES else "90", "new_days": NEW_MERCHANT_DAYS,
        })

    @app.get("/api/network")
    def api_network(days: str = "90"):
        from fraudalert.network import build_network

        if days not in NETWORK_RANGES:
            raise HTTPException(422, f"days must be one of {sorted(NETWORK_RANGES)}")
        with session_scope() as s:
            env = pipeline.Env.load(s, get_settings())
            return build_network(s, env, NETWORK_RANGES[days])

    # ---- spend historian ----

    @app.get("/spending")
    def spending_page(request: Request):
        return templates.TemplateResponse(request, "spending.html", {})

    @app.get("/api/spending/overview")
    def api_spending_overview():
        from fraudalert import spending

        with session_scope() as s:
            return spending.overview(s, pipeline.Env.load(s, get_settings()))

    @app.get("/api/spending/series")
    def api_spending_series(category: str | None = None, merchant: str | None = None,
                            bucket: str = "day", days: int = 90, month: str | None = None):
        from fraudalert import spending

        if category not in (None, "", "uncategorized") and not str(category).isdigit():
            raise HTTPException(422, "category must be an id or 'uncategorized'")
        try:
            with session_scope() as s:
                return spending.series(s, pipeline.Env.load(s, get_settings()), category=category or None,
                                       merchant=merchant or None, bucket=bucket, days=max(7, min(days, 730)),
                                       month=month or None)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    class CategoryIn(BaseModel):
        name: str | None = None
        budget: str | float | None = None

    @app.post("/api/spending/categories", status_code=201)
    def api_create_category(body: CategoryIn):
        from fraudalert import spending

        try:
            with session_scope() as s:
                c = spending.create_category(s, body.name or "", body.budget)
                return {"id": c.id, "name": c.name}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.patch("/api/spending/categories/{category_id}")
    def api_update_category(category_id: int, body: CategoryIn):
        from fraudalert import spending

        changes = body.model_dump(exclude_unset=True)
        try:
            with session_scope() as s:
                c = spending.update_category(s, category_id, name=changes.get("name"),
                                             budget=changes["budget"] if "budget" in changes else "__keep__")
                return {"id": c.id, "name": c.name, "budget": float(c.budget_monthly) if c.budget_monthly is not None else None}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.delete("/api/spending/categories/{category_id}")
    def api_delete_category(category_id: int):
        from fraudalert import spending

        try:
            with session_scope() as s:
                return {"uncategorized": spending.delete_category(s, category_id)}
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    class ExpenseIn(BaseModel):
        name: str | None = None
        amount: str | float | None = None
        currency: str | None = None
        category_id: int | str | None = None
        day_of_month: int | str | None = None
        start_month: str | None = None
        end_month: str | None = None
        note: str | None = None

    @app.get("/api/spending/expenses")
    def api_list_expenses():
        from fraudalert import spending

        with session_scope() as s:
            return spending.list_expenses(s, pipeline.Env.load(s, get_settings()))

    @app.post("/api/spending/expenses", status_code=201)
    def api_create_expense(body: ExpenseIn):
        from fraudalert import spending

        try:
            with session_scope() as s:
                e = spending.create_expense(s, pipeline.Env.load(s, get_settings()), body.model_dump(exclude_unset=True))
                return {"id": e.id, "key": f"{spending.MANUAL}{e.id}", "name": e.name}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.patch("/api/spending/expenses/{expense_id}")
    def api_update_expense(expense_id: int, body: ExpenseIn):
        from fraudalert import spending

        try:
            with session_scope() as s:
                e = spending.update_expense(s, pipeline.Env.load(s, get_settings()), expense_id,
                                            body.model_dump(exclude_unset=True))
                return {"id": e.id, "name": e.name}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.delete("/api/spending/expenses/{expense_id}", status_code=204)
    def api_delete_expense(expense_id: int):
        from fraudalert import spending

        try:
            with session_scope() as s:
                spending.delete_expense(s, expense_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    class AssignIn(BaseModel):
        keys: list[str]
        category_id: int | None = None

    @app.post("/api/spending/assign")
    def api_assign(body: AssignIn):
        from fraudalert import spending

        if not body.keys:
            raise HTTPException(422, "no merchants given")
        try:
            with session_scope() as s:
                return {"assigned": spending.assign(s, body.keys, body.category_id)}
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/rules")
    def rules_page(request: Request):
        with session_scope() as s:
            rules = s.scalars(select(Rule).order_by(Rule.id)).all()
            counts = dict(
                s.execute(
                    select(Rule.id, func.count()).join(Rule.alerts).group_by(Rule.id)
                ).all()
            )
        return templates.TemplateResponse(
            request, "rules.html",
            {"rules": rules, "counts": counts, "fields": FIELDS, "ops": OPS, "severities": SEVERITIES},
        )

    @app.post("/rules")
    async def create_rule(request: Request):
        form = await request.form()
        conditions = [
            {"field": f, "op": o, "value": v}
            for f, o, v in zip(form.getlist("field"), form.getlist("op"), form.getlist("value"))
            if f and str(v).strip()
        ]
        name = str(form.get("name", "")).strip()
        try:
            if not name:
                raise RuleError("give the rule a name")
            if str(form.get("severity", "low")) not in SEVERITIES:
                raise RuleError(f"priority must be one of {', '.join(SEVERITIES)}")
            clean = validate_rule(str(form.get("match", "all")), conditions)
        except RuleError as exc:
            return redirect("/rules", error=str(exc))
        with session_scope() as s:
            s.add(Rule(
                name=name, description=str(form.get("description", "")), match=str(form.get("match")),
                conditions=clean, severity=str(form.get("severity", "low")),
            ))
        result = pipeline.reevaluate_all()
        return redirect("/rules", msg=f"Rule '{name}' added and applied to history ({result.flagged} flagged).")

    @app.post("/rules/{rule_id}/toggle")
    def toggle_rule(rule_id: int):
        with session_scope() as s:
            rule = s.get(Rule, rule_id) or _404()
            rule.enabled = not rule.enabled
        pipeline.reevaluate_all()
        return redirect("/rules", msg="Rule updated and history re-evaluated.")

    @app.post("/rules/{rule_id}/priority")
    async def set_rule_priority(rule_id: int, request: Request):
        severity = str((await request.form()).get("severity", ""))
        if severity not in SEVERITIES:
            return redirect("/rules", error=f"priority must be one of {', '.join(SEVERITIES)}")
        with session_scope() as s:
            rule = s.get(Rule, rule_id) or _404()
            rule.severity = severity
            name = rule.name
        pipeline.reevaluate_all()
        return redirect("/rules", msg=f"'{name}' is now {PRIORITY_NAME[PRIORITY_RANK[severity]]} priority.")

    @app.post("/rules/{rule_id}/delete")
    def delete_rule(rule_id: int):
        with session_scope() as s:
            s.delete(s.get(Rule, rule_id) or _404())
        pipeline.reevaluate_all()
        return redirect("/rules", msg="Rule deleted.")

    @app.post("/settings/report")
    async def save_report_settings(request: Request):
        from fraudalert import report

        form = await request.form()
        try:
            with session_scope() as s:
                saved = report.save_prefs(s, {k: form.get(k, "") for k in report.DEFAULTS})
        except ValueError as exc:
            return redirect("/settings", error=str(exc))
        state = f"on, daily at {saved['time']}" if saved["enabled"] else "off"
        return redirect("/settings", msg=f"Daily report saved ({state}).")

    @app.post("/report/test")
    def send_test_report():
        from fraudalert import report

        try:
            r = report.send_report(test=True)
        except Exception as exc:  # noqa: BLE001 - show mail/login problems to the user
            return redirect("/settings", error=f"Test report not sent: {exc}")
        with session_scope() as s:
            to = report.get_prefs(s, get_settings())["to"]
        return redirect("/settings", msg=f"Test report sent to {to} ({r.transactions} transactions, "
                                         f"{r.unacknowledged} unacknowledged).")

    @app.post("/model/train")
    def train_model():
        from fraudalert.anomaly.training import NotEnoughData

        try:
            info = pipeline.retrain_anomaly_model()
        except NotEnoughData as exc:
            return redirect("/settings", error=f"Not trained yet: {exc}.")
        return redirect("/settings", msg=f"Trained the anomaly model on {info['n_samples']} transactions "
                                         "and re-scored your history.")

    @app.get("/settings")
    def settings_page(request: Request):
        from fraudalert.anomaly.iforest import MIN_SAMPLES
        from fraudalert.anomaly.training import latest_model_info

        settings = get_settings()
        with session_scope() as s:
            normal = prefs.normal_currencies(s, settings)
            model = latest_model_info(s)
            from fraudalert import report as report_mod

            report_prefs = report_mod.get_prefs(s, settings)
            report_last = report_mod.last_sent(s)
            report_error = report_mod.last_error(s)
            scored = s.scalar(select(func.count(Transaction.id)).where(Transaction.features.is_not(None)))
        return templates.TemplateResponse(request, "settings.html", {
            "normal": ", ".join(normal),
            "default_normal": ", ".join(prefs.default_normal_currencies(settings)),
            "settings": settings,
            "model": model, "scored": scored, "min_samples": MIN_SAMPLES,
            "report": report_prefs, "report_last": report_last, "report_error": report_error,
        })

    @app.post("/settings")
    async def save_settings(request: Request):
        form = await request.form()
        try:
            codes = prefs.parse_currency_list(str(form.get("normal_currencies", "")))
        except ValueError as exc:
            return redirect("/settings", error=str(exc))
        with session_scope() as s:
            prefs.set_normal_currencies(s, codes)
        r = pipeline.reevaluate_all()
        return redirect("/settings", msg=f"Normal currencies: {', '.join(codes)}. Re-applied rules: {r.flagged} flagged.")

    @app.get("/emails")
    def emails_page(request: Request, status_: str = Query("failed", alias="status")):
        with session_scope() as s:
            rows = s.scalars(
                select(RawEmail).where(RawEmail.parse_status == status_)
                .order_by(RawEmail.received_at.desc()).limit(200)
            ).all()
        return templates.TemplateResponse(request, "emails.html", {"rows": rows, "status": status_})

    @app.post("/emails/reparse")
    def reparse():
        r = pipeline.reevaluate_all(reparse="failed")
        return redirect("/emails", msg=f"Re-parsed: {r.parsed} recovered, {r.failed} still failing.")

    @app.post("/emails/reparse-all")
    def reparse_all():
        r = pipeline.reevaluate_all(reparse="all")
        return redirect("/emails", msg=f"Re-parsed every email: {r.parsed} transactions, {r.failed} unparsed.")

    @app.post("/sync")
    def sync(background: BackgroundTasks):
        background.add_task(pipeline.sync_inbox)
        return redirect("/", msg="Inbox sync started in the background — refresh in a moment.")

    # ---------- JSON API ----------

    class ConditionIn(BaseModel):
        field: str
        op: str
        value: str | float | int | bool | list

    class RuleIn(BaseModel):
        name: str
        description: str = ""
        match: str = "all"
        severity: str = "medium"
        enabled: bool = True
        conditions: list[ConditionIn]

    def _txn_json(t: Transaction) -> dict:
        return {
            "id": t.id, "occurred_at": t.occurred_at.isoformat(), "amount": str(t.amount),
            "currency": t.currency, "merchant": t.merchant, "card_last4": t.card_last4,
            "auth_code": t.auth_code, "reference": t.reference,
            "anomaly_reasons": [r["text"] for r in (t.anomaly_reasons or [])],
            "is_foreign": t.is_foreign, "anomaly_score": t.anomaly_score, "flagged": t.flagged,
            "label_fraud": t.label_fraud, "comment": t.comment, "alerts": [a.reason for a in t.alerts],
        }

    def _rule_json(r: Rule) -> dict:
        return {
            "id": r.id, "name": r.name, "description": r.description, "match": r.match,
            "conditions": r.conditions, "severity": r.severity, "enabled": r.enabled,
        }

    @app.get("/api/transactions")
    def api_transactions(flagged: bool = False, limit: int = 100, offset: int = 0):
        with session_scope() as s:
            stmt = select(Transaction).options(selectinload(Transaction.alerts))
            if flagged:
                stmt = stmt.where(Transaction.flagged.is_(True))
            rows = s.scalars(
                stmt.order_by(Transaction.occurred_at.desc()).offset(offset).limit(min(limit, 1000))
            ).all()
            return [_txn_json(t) for t in rows]

    class BulkIn(BaseModel):
        ids: list[int]
        action: str
        comment: str = ""

    @app.post("/api/transactions/bulk")
    def api_bulk(body: BulkIn):
        if body.action not in BULK_ACTIONS:
            raise HTTPException(422, f"action must be one of {sorted(BULK_ACTIONS)}")
        with session_scope() as s:
            return {"updated": apply_bulk(s, body.ids, body.action, body.comment)}

    @app.get("/api/rules")
    def api_rules():
        with session_scope() as s:
            return [_rule_json(r) for r in s.scalars(select(Rule).order_by(Rule.id))]

    @app.post("/api/rules", status_code=201)
    def api_create_rule(body: RuleIn):
        try:
            clean = validate_rule(body.match, [c.model_dump() for c in body.conditions])
        except RuleError as exc:
            raise HTTPException(422, str(exc)) from exc
        if body.severity not in SEVERITIES:
            raise HTTPException(422, f"severity must be one of {SEVERITIES}")
        with session_scope() as s:
            rule = Rule(
                name=body.name, description=body.description, match=body.match,
                conditions=clean, severity=body.severity, enabled=body.enabled,
            )
            s.add(rule)
            s.flush()
            out = _rule_json(rule)
        pipeline.reevaluate_all()
        return out

    @app.delete("/api/rules/{rule_id}", status_code=204)
    def api_delete_rule(rule_id: int):
        with session_scope() as s:
            s.delete(s.get(Rule, rule_id) or _404())
        pipeline.reevaluate_all()

    @app.post("/api/sync")
    def api_sync():
        r = pipeline.sync_inbox()
        return {"fetched": r.fetched, "parsed": r.parsed, "failed": r.failed, "flagged": r.flagged, "errors": r.errors}

    return app


def _404():
    raise HTTPException(404)

