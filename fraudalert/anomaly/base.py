"""Anomaly detector interface and registry.

A detector maps a feature dict (see `features.FEATURE_NAMES`) to a score in [0, 1], or None when it
cannot judge yet (e.g. not enough history). Scores land in `Transaction.anomaly_score`, which rules
can reference ("anomaly_score > 0.9"), so swapping in a neural net needs no pipeline changes:

    class AutoencoderDetector(AnomalyDetector):
        name = "autoencoder-v1"
        def fit(self, rows): ...            # rows: feature dicts of known-good transactions
        def score(self, features): ...      # reconstruction error squashed to [0, 1]

    DETECTORS["autoencoder"] = AutoencoderDetector

then set FRAUDALERT_DETECTOR=autoencoder.
"""

from abc import ABC, abstractmethod


class AnomalyDetector(ABC):
    name: str = "abstract"

    def fit(self, rows: list[dict[str, float]]) -> None:
        """Train on historical feature dicts. Optional for stateless detectors."""

    @abstractmethod
    def score(self, features: dict[str, float]) -> float | None: ...

    def explain(self, features: dict[str, float]) -> list[dict]:
        """Plain-language reasons for the score (see anomaly/explain.py). Optional."""
        return []


DETECTORS: dict[str, type[AnomalyDetector]] = {}


def register(key: str):
    def deco(cls: type[AnomalyDetector]) -> type[AnomalyDetector]:
        DETECTORS[key] = cls
        return cls

    return deco


def get_detector(key: str = "iforest") -> AnomalyDetector:
    from fraudalert.anomaly import baseline, iforest  # noqa: F401  (registers built-ins)

    try:
        return DETECTORS[key]()
    except KeyError:
        raise ValueError(f"unknown detector {key!r}; available: {', '.join(DETECTORS)}") from None
