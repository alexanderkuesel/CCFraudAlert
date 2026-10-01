"""Plain-language reasons for an anomaly score.

Method: for each group of related features, put that group back to a *typical* value (your median,
for the Isolation Forest) and measure how much less unusual the transaction becomes. The groups whose
reset lowers the score most are the reasons, e.g. "first purchase at this merchant · at 3 am".
Hour and weekday are stored as sin/cos pairs, so each pair is reset together.
"""

import math
from collections.abc import Callable

GROUPS: dict[str, list[str]] = {
    "amount_merchant": ["amount_z_merchant"],
    "amount": ["log_amount", "amount_z_global"],
    "merchant": ["merchant_seen_log"],
    "time": ["hour_sin", "hour_cos"],
    "weekday": ["dow_sin", "dow_cos"],
    "gap": ["hours_since_prev_log"],
    "burst": ["txns_last_24h"],
    "foreign": ["is_foreign"],
}
# Used by detectors that have no training data to take medians from (the baseline).
NEUTRAL = {
    "log_amount": math.log1p(40), "amount_z_merchant": 0.0, "amount_z_global": 0.0, "merchant_seen_log": math.log1p(10),
    "hour_sin": 0.0, "hour_cos": -1.0,  # noon
    "dow_sin": math.sin(2 * math.pi * 2 / 7), "dow_cos": math.cos(2 * math.pi * 2 / 7),  # Wednesday
    "hours_since_prev_log": math.log1p(24), "txns_last_24h": 1.0, "is_foreign": 0.0,
}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _hour(f: dict) -> float:
    return (math.atan2(f.get("hour_sin", 0.0), f.get("hour_cos", 1.0)) / (2 * math.pi) * 24) % 24


def _hour_distance(a: float, b: float) -> float:
    d = abs(a - b) % 24
    return min(d, 24 - d)


def is_atypical(group: str, f: dict, typical: dict) -> bool:
    """Only a genuinely unusual value may be named as a reason: resetting a feature always lowers the
    score a little, and "at 1 pm" shouldn't be offered as a reason when 1 pm is when you shop."""
    if group == "amount_merchant":
        return abs(f.get("amount_z_merchant", 0.0)) >= 1.5
    if group == "amount":
        return math.expm1(f.get("log_amount", 0.0)) < 1.0 or abs(f.get("amount_z_global", 0.0)) >= 1.5
    if group == "merchant":
        return math.expm1(f.get("merchant_seen_log", 0.0)) <= 2.5
    if group == "time":
        return _hour_distance(_hour(f), _hour(typical)) >= 3
    if group == "weekday":
        return True
    if group == "gap":
        hours = math.expm1(f.get("hours_since_prev_log", 0.0))
        return hours < 0.5 or hours >= 72
    if group == "burst":
        return f.get("txns_last_24h", 0.0) >= 3
    if group == "foreign":
        return f.get("is_foreign", 0.0) >= 0.5
    return True


def _clock(hour: float) -> str:
    h = int(round(hour)) % 24
    return f"{h % 12 or 12} {'am' if h < 12 else 'pm'}"


def describe(group: str, f: dict, typical: dict | None = None) -> str:
    if group == "amount_merchant":
        return "much more than you usually spend here" if f.get("amount_z_merchant", 0) > 0 \
            else "much less than you usually spend here"
    if group == "amount":
        amount = math.expm1(f.get("log_amount", 0.0))
        if amount < 1.0:
            return "zero or near-zero amount (a typical card test)"
        return "large amount for you" if f.get("amount_z_global", 0) > 0 else "unusually small amount for you"
    if group == "merchant":
        seen = round(math.expm1(f.get("merchant_seen_log", 0.0)))
        return "first purchase at this merchant" if seen == 0 else f"only {seen} earlier purchase{'s' * (seen != 1)} here"
    if group == "time":
        usual = f" (you usually shop around {_clock(_hour(typical))})" if typical else ""
        return f"at {_clock(_hour(f))}{usual}"
    if group == "weekday":
        angle = math.atan2(f.get("dow_sin", 0.0), f.get("dow_cos", 1.0)) % (2 * math.pi)
        return f"on a {WEEKDAYS[round(angle / (2 * math.pi) * 7) % 7]}"
    if group == "gap":
        hours = math.expm1(f.get("hours_since_prev_log", 0.0))
        if hours < 0.5:
            return "minutes after your previous transaction"
        return f"first transaction in {hours / 24:.0f} days" if hours >= 72 else "unusual gap since your previous transaction"
    if group == "burst":
        n = int(f.get("txns_last_24h", 0))
        return f"{n} other transaction{'s' * (n != 1)} in the past 24 hours"
    if group == "foreign":
        return "abroad or in an unusual currency"
    return group


def explain(
    raw_score: Callable[[list[dict]], list[float]],
    features: dict[str, float],
    typical: dict[str, float],
    top: int = 3,
    min_share: float = 0.08,
) -> list[dict]:
    """Return up to `top` reasons as {"key", "text", "weight"}; weight = share of the score explained.

    `raw_score` scores a batch of feature dicts (higher = more unusual). Groups already at a typical
    value, or whose reset barely moves the score, are skipped.
    """
    variants, keys = [], []
    for key, names in GROUPS.items():
        if all(abs(features.get(n, 0.0) - typical.get(n, 0.0)) < 1e-9 for n in names):
            continue
        variants.append({**features, **{n: typical.get(n, 0.0) for n in names}})
        keys.append(key)
    if not variants:
        return []
    base, *others = raw_score([features, *variants])
    drops = [max(base - s, 0.0) for s in others]
    total = sum(drops)
    if total <= 0:
        return []
    ranked = sorted(zip(keys, drops), key=lambda kv: -kv[1])
    reasons = [
        {"key": k, "text": describe(k, features, typical), "weight": round(d / total, 2)}
        for k, d in ranked if d / total >= min_share and is_atypical(k, features, typical)
    ]
    return reasons[:top]


def summary(reasons: list[dict] | None) -> str:
    return " · ".join(r["text"] for r in reasons or [])
