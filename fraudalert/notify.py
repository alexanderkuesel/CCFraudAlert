import logging

import httpx

from fraudalert.config import Settings
from fraudalert.models import Transaction

log = logging.getLogger(__name__)


def notify(settings: Settings, txn: Transaction, reasons: list[str]) -> bool:
    """POST to the configured webhook. The `text` key works with Slack/Discord/Mattermost-style hooks."""
    if not settings.notify_webhook_url:
        return False
    rank = min(({"high": 1, "medium": 2, "low": 3}.get(a.severity, 3) for a in txn.alerts), default=3)
    priority = {1: "HIGH", 2: "MEDIUM", 3: "LOW"}[rank]
    text = (
        f"[{priority}] Transaction alarm: {txn.amount} {txn.currency} at {txn.merchant or 'unknown merchant'}"
        f" on {txn.occurred_at:%Y-%m-%d %H:%M}"
        + (f" (card …{txn.card_last4})" if txn.card_last4 else "")
        + "\n" + "\n".join(f"• {r}" for r in reasons)
    )
    payload = {
        "text": text,
        "content": text,  # Discord
        "transaction": {
            "id": txn.id,
            "amount": str(txn.amount),
            "currency": txn.currency,
            "merchant": txn.merchant,
            "occurred_at": txn.occurred_at.isoformat(),
            "card_last4": txn.card_last4,
            "anomaly_score": txn.anomaly_score,
        },
        "priority": priority,
        "reasons": reasons,
    }
    try:
        httpx.post(settings.notify_webhook_url, json=payload, timeout=10).raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("webhook notification failed: %s", exc)
        return False
