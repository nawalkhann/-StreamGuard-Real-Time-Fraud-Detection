from __future__ import annotations

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_HIGH_RISK_MCCS = "4829,6051,7995,5967,6211,5816"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STREAMGUARD_", env_file=".env", extra="ignore")

    # --- security ---
    api_keys: str = ""  # comma-separated
    auth_disabled: bool = False  # local development only

    # --- storage / model ---
    db_path: str = "data/streamguard.db"
    model_path: str = "models/model.joblib"
    warm_start_days: int = 30

    # --- decisioning ---
    review_threshold: float = 0.50
    decline_threshold: float = 0.85
    high_risk_mccs: str = DEFAULT_HIGH_RISK_MCCS
    # Hard per-transaction ceiling (USD): anything above is at least sent to review.
    review_amount_usd: float = 25_000.0
    # Static FX table to USD. Replace with a rates feed for production use.
    fx_rates: dict[str, float] = {
        "USD": 1.0, "EUR": 1.08, "GBP": 1.27, "INR": 0.012, "AED": 0.272,
        "SGD": 0.74, "AUD": 0.66, "CAD": 0.73, "BRL": 0.20, "CNY": 0.14, "JPY": 0.0067,
    }

    # --- counter-terrorism financing / sanctions ---
    sanctions_dir: str = "data/sanctions"          # see README: sdn.csv, alt.csv, consolidated.xml, custom*.csv
    sanctions_fuzzy_threshold: float = 0.90        # similarity for non-exact name matches (review, not auto-decline)
    sanctions_max_age_days: int = 7                # /readyz warns when lists are older than this
    # Jurisdictions FATF calls for action on / your policy treats as high risk. VERIFY against the current
    # FATF publication and your own sanctions policy; this default is only a starting point.
    high_risk_jurisdictions: str = "KP,IR,MM"
    structuring_threshold_usd: float = 10_000.0    # reporting threshold used to detect structuring

    # --- ingestion hygiene ---
    max_future_skew_seconds: int = 300
    max_accounts_in_memory: int = 200_000

    # --- optional Kafka ---
    kafka_bootstrap: str = ""
    kafka_in_topic: str = "transactions"
    kafka_out_topic: str = "fraud-decisions"
    kafka_dlq_topic: str = "transactions-dlq"
    kafka_group: str = "streamguard"

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if not 0 < self.review_threshold < self.decline_threshold <= 1:
            raise ValueError("require 0 < review_threshold < decline_threshold <= 1")
        return self

    @property
    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

    @property
    def high_risk_jurisdiction_set(self) -> frozenset[str]:
        return frozenset(c.strip().upper() for c in self.high_risk_jurisdictions.split(",") if c.strip())

    @property
    def high_risk_mcc_set(self) -> frozenset[str]:
        return frozenset(m.strip() for m in self.high_risk_mccs.split(",") if m.strip())
