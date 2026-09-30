import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from fraudalert import pipeline, prefs
from fraudalert.config import get_settings
from fraudalert.db import init_db, session_scope
from fraudalert.models import RawEmail, Rule, SyncState, Transaction
from fraudalert.rules.engine import FIELDS, OPS, RuleError, RuleSpec, describe, validate_rule

HERE = Path(__file__).parent
PAGE_SIZE = 50
SEVERITIES = ["low", "medium", "high"]
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


def _local(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(get_settings().timezone)).strftime(fmt)


def create_app(init: bool = True) -> FastAPI:
    app = FastAPI(title="Fraud Alert", dependencies=[Depends(require_auth)], lifespan=_lifespan if init else None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(describe=lambda r: describe(RuleSpec(r.id, r.name, r.match, r.conditions)))
    templates.env.filters["local"] = _local

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
        return RedirectResponse(path + (("?" + urlencode(q)) if q else ""), status_code=303)

    # ---------- HTML ----------

    @app.get("/")
    def transactions(request: Request, flagged: bool = False, q: str = "", page: int = 1):
        page = max(page, 1)
        with session_scope() as s:
            stmt = select(Transaction).options(selectinload(Transaction.alerts))
            if flagged:
                stmt = stmt.where(Transaction.flagged.is_(True))
            if q:
                like = f"%{q}%"
                stmt = stmt.where(or_(Transaction.merchant.ilike(like), Transaction.currency.ilike(like)))
            total = s.scalar(select(func.count()).select_from(stmt.subquery()))
            rows = s.scalars(
                stmt.order_by(Transaction.occurred_at.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
            ).all()
            stats = {
                "total": s.scalar(select(func.count(Transaction.id))),
                "flagged": s.scalar(select(func.count(Transaction.id)).where(Transaction.flagged.is_(True))),
                "unparsed": s.scalar(select(func.count(RawEmail.id)).where(RawEmail.parse_status == "failed")),
            }
            last_sync = s.get(SyncState, "last_imap_sync")
            normal = set(prefs.normal_currencies(s, get_settings()))
        return templates.TemplateResponse(
            request,
            "transactions.html",
            {
                "rows": rows, "flagged": flagged, "q": q, "page": page, "total": total,
                "pages": max(1, -(-total // PAGE_SIZE)), "stats": stats,
                "last_sync": _local(datetime.fromisoformat(last_sync.value)) if last_sync else None,
                "tz": get_settings().timezone,
                "normal": normal,
            },
        )

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
            clean = validate_rule(str(form.get("match", "all")), conditions)
        except RuleError as exc:
            return redirect("/rules", error=str(exc))
        with session_scope() as s:
            s.add(Rule(
                name=name, description=str(form.get("description", "")), match=str(form.get("match")),
                conditions=clean, severity=str(form.get("severity", "medium")),
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

    @app.post("/rules/{rule_id}/delete")
    def delete_rule(rule_id: int):
        with session_scope() as s:
            s.delete(s.get(Rule, rule_id) or _404())
        pipeline.reevaluate_all()
        return redirect("/rules", msg="Rule deleted.")

    @app.get("/settings")
    def settings_page(request: Request):
        settings = get_settings()
        with session_scope() as s:
            normal = prefs.normal_currencies(s, settings)
        return templates.TemplateResponse(request, "settings.html", {
            "normal": ", ".join(normal),
            "default_normal": ", ".join(prefs.default_normal_currencies(settings)),
            "settings": settings,
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

