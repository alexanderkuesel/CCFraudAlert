"""A transparent statistical baseline. Good enough to be useful on day one and a yardstick that a
future neural model has to beat."""

import math

from fraudalert.anomaly.base import AnomalyDetector, register

MIN_HISTORY = 10


@register("baseline")
class BaselineDetector(AnomalyDetector):
    name = "baseline-v1"

    def score(self, features: dict[str, float]) -> float | None:
        if math.expm1(features.get("history_size_log", 0.0)) < MIN_HISTORY:
            return None
        seen = math.expm1(features.get("merchant_seen_log", 0.0))
        if seen >= 3:
            # Known merchant: judge the amount against what you usually spend there.
            amount_signal = max(features.get("amount_z_merchant", 0.0) - 1.5, 0.0)
        else:
            # New merchant: only a clearly large amount relative to all spending counts, since
            # everyday spend is multimodal (coffee vs. groceries) and inflates global z-scores.
            amount_signal = max(features.get("amount_z_global", 0.0) - 2.0, 0.0)
        new_merchant = seen == 0
        burst = max(features.get("txns_last_24h", 0.0) - 5.0, 0.0)
        # Roughly midnight-5am local: angle in the first quadrant with cos > 0.25.
        night = features.get("hour_sin", 0.0) >= 0 and features.get("hour_cos", 0.0) > 0.25

        raw = (
            0.8 * amount_signal
            + 0.8 * features.get("is_foreign", 0.0)
            + 0.5 * new_merchant
            + 0.3 * burst
            + 0.4 * night
        )
        # Squash to [0, 1): raw 1 -> 0.39, 2 -> 0.63, 3 -> 0.78, 5 -> 0.92
        return round(1 - math.exp(-raw / 2), 4)
