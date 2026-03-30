"""ML ensemble pre-filter script.

Generic script that runs an ensemble of LightGBM models and returns
the max risk score. The pipeline JSON configures which domains to run
and the threshold values via script_params.
"""

import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np

from backend.scam.tools import register_script

logger = logging.getLogger(__name__)

from backend.config import DATA_DIR

_BUNDLE_MODELS_DIR = Path(__file__).parent.parent / "ml_models"  # shipped with app
_DATA_MODELS_DIR = DATA_DIR / "ml_models"  # runtime location (writable, syncable)
_ensemble: Optional["MLEnsemble"] = None


def _ensure_models_in_data_dir():
    """Copy bundled models to DATA_DIR on first launch."""
    if not _BUNDLE_MODELS_DIR.exists():
        return
    import shutil
    for domain_dir in _BUNDLE_MODELS_DIR.iterdir():
        if not domain_dir.is_dir():
            continue
        target = _DATA_MODELS_DIR / domain_dir.name
        if not target.exists():
            shutil.copytree(domain_dir, target)
            logger.info("Copied bundled ML model '%s' to %s", domain_dir.name, target)


class _DomainModel:
    """A single domain's TF-IDF + features + LightGBM model."""

    def __init__(self, domain: str, path: Path):
        self.domain = domain
        self.tfidf = None
        self.scaler = None
        self.clf = None
        self.low_threshold = 0.3
        self.high_threshold = 0.99

        try:
            import joblib
            self.tfidf = joblib.load(path / "tfidf.joblib")
            self.scaler = joblib.load(path / "scaler.joblib")
            self.clf = joblib.load(path / "lgbm.joblib")

            thresholds_file = path / "thresholds.json"
            if thresholds_file.exists():
                t = json.loads(thresholds_file.read_text())
                self.low_threshold = t.get("low_threshold", 0.3)
                self.high_threshold = t.get("high_threshold", 0.99)

            logger.info("Loaded ML model: %s (low=%.4f, high=%.4f)",
                        domain, self.low_threshold, self.high_threshold)
        except Exception as e:
            logger.warning("Failed to load ML model %s: %s", domain, e)
            self.clf = None

    @property
    def is_loaded(self) -> bool:
        return self.clf is not None

    def predict_proba(self, texts: list[str]) -> np.ndarray:
        """Return P(deceptive) for each text."""
        from scipy.sparse import hstack, csr_matrix

        X_tfidf = self.tfidf.transform(texts)
        X_hand = _extract_features(texts)
        X_hand = self.scaler.transform(X_hand)
        X = hstack([X_tfidf, csr_matrix(X_hand)])
        probs = self.clf.predict_proba(X)
        return probs[:, 1]  # P(deceptive)


def _extract_features(texts: list[str]) -> np.ndarray:
    """Extract 7 handcrafted text features (same as ScamDetectionPoC)."""
    features = []
    for text in texts:
        t = str(text)
        n_chars = max(len(t), 1)
        words = t.split()
        n_words = max(len(words), 1)
        avg_word_len = sum(len(w) for w in words) / n_words
        n_sentences = max(t.count('.') + t.count('!') + t.count('?'), 1)
        caps_ratio = sum(1 for c in t if c.isupper()) / n_chars
        digit_ratio = sum(1 for c in t if c.isdigit()) / n_chars
        features.append([
            caps_ratio, avg_word_len, n_words / n_sentences,
            digit_ratio, len(t), n_words, n_sentences,
        ])
    return np.array(features, dtype=np.float32)


class MLEnsemble:
    """Ensemble of domain-specific ML models. Returns max score across selected models."""

    def __init__(self, models_dir: Path):
        self._all_models: dict[str, _DomainModel] = {}
        if not models_dir.exists():
            logger.warning("ML models directory not found: %s", models_dir)
            return

        for domain_dir in sorted(models_dir.iterdir()):
            if domain_dir.is_dir() and (domain_dir / "lgbm.joblib").exists():
                model = _DomainModel(domain_dir.name, domain_dir)
                if model.is_loaded:
                    self._all_models[domain_dir.name] = model

        logger.info("ML ensemble loaded: %d models (%s)",
                    len(self._all_models), ", ".join(self._all_models.keys()))

    @property
    def is_loaded(self) -> bool:
        return len(self._all_models) > 0

    @property
    def available_domains(self) -> list[str]:
        return list(self._all_models.keys())

    def predict(self, texts: list[str], domains: Optional[list[str]] = None,
                low_threshold: float = 0.30, high_threshold: float = 0.95) -> dict:
        if not self._all_models:
            return {"risk_score": 0.0, "models": [], "verdict": "no_models"}

        if domains:
            models = [self._all_models[d] for d in domains if d in self._all_models]
        else:
            models = list(self._all_models.values())

        if not models:
            return {"risk_score": 0.0, "models": [], "verdict": "no_models",
                    "error": f"None of the requested domains available. Available: {self.available_domains}"}

        domain_results = []
        max_score = 0.0
        max_domain = ""

        for model in models:
            try:
                probs = model.predict_proba(texts)
                score = float(probs.max())
                domain_results.append({
                    "domain": model.domain,
                    "score": round(score, 4),
                    "low_threshold": model.low_threshold,
                    "high_threshold": model.high_threshold,
                })
                if score > max_score:
                    max_score = score
                    max_domain = model.domain
            except Exception as e:
                logger.warning("ML model %s prediction failed: %s", model.domain, e)
                domain_results.append({"domain": model.domain, "score": 0.0, "error": str(e)})

        verdict = "safe"
        if max_score >= high_threshold:
            verdict = "auto_scam"
        elif max_score >= low_threshold:
            verdict = "borderline"

        return {
            "risk_score": round(max_score, 4),
            "verdict": verdict,
            "top_domain": max_domain,
            "models": domain_results,
        }


def _get_ensemble() -> MLEnsemble:
    global _ensemble
    if _ensemble is None:
        _ensure_models_in_data_dir()
        _ensemble = MLEnsemble(_DATA_MODELS_DIR)
    return _ensemble


def reload_ensemble():
    """Reload the ML ensemble after model sync. Called by cloud_sync."""
    global _ensemble
    _ensemble = None  # force re-init on next call


@register_script("ml_ensemble")
async def ml_ensemble_script(args: dict) -> dict:
    """Generic ML ensemble script. Pipeline JSON configures domains and thresholds."""
    ensemble = _get_ensemble()

    # Extract text
    text = args.get("text", "")
    if not text and "$message" in args:
        msg = args["$message"]
        if "Messages to analyze:" in msg:
            lines = msg.split("Messages to analyze:")[1].strip().split("\n")
            texts = []
            for line in lines:
                line = line.strip()
                if line.startswith("[") and "]: " in line:
                    texts.append(line.split("]: ", 1)[1])
                elif line:
                    texts.append(line)
            text = " ".join(texts)
        else:
            text = msg

    if not text:
        return {"risk_score": 0.0, "verdict": "no_input", "models": []}

    domains = args.get("domains")
    low_threshold = args.get("ensemble_low_threshold", 0.30)
    high_threshold = args.get("ensemble_high_threshold", 0.95)

    return ensemble.predict([text], domains=domains,
                            low_threshold=low_threshold, high_threshold=high_threshold)
