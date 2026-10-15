from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .features import Txn


class Channel(str, Enum):
    card_present = "card_present"
    online = "online"
    atm = "atm"
    wallet = "wallet"


class Transaction(BaseModel):
    """Inbound transaction. Never send raw card numbers: ``account_id`` must be a token."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    transaction_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-:.]+$")
    account_id: str = Field(min_length=1, max_length=64)
    amount: float = Field(gt=0, le=1e9)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    merchant_id: str = Field(min_length=1, max_length=64)
    merchant_category: str = Field(pattern=r"^\d{4}$", description="ISO 18245 MCC")
    country: str = Field(min_length=2, max_length=2, description="ISO 3166 alpha-2 of transaction")
    channel: Channel
    timestamp: datetime
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    device_id: str | None = Field(default=None, max_length=128)
    ip: str | None = Field(default=None, max_length=45)
    counterparty_name: str | None = Field(default=None, max_length=140, description="Beneficiary / payee name, screened against sanctions lists")
    counterparty_country: str | None = Field(default=None, min_length=2, max_length=2)

    @field_validator("currency", "country", "counterparty_country")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return v.upper() if v is not None else v

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc)

    def to_internal(self, fx_rates: dict[str, float]) -> Txn:
        rate = fx_rates.get(self.currency)
        if rate is None:
            raise ValueError(f"unsupported currency: {self.currency}")
        return Txn(
            id=self.transaction_id, account=self.account_id, amount=self.amount * rate,
            merchant=self.merchant_id, mcc=self.merchant_category, country=self.country,
            channel=self.channel.value, ts=self.timestamp.timestamp(),
            lat=self.lat, lon=self.lon, device=self.device_id, ip=self.ip,
            cp_name=self.counterparty_name, cp_country=self.counterparty_country,
        )


DecisionType = Literal["APPROVE", "REVIEW", "DECLINE"]


class Reason(BaseModel):
    code: str
    description: str
    weight: float


class Decision(BaseModel):
    transaction_id: str
    decision: DecisionType
    risk_score: float = Field(description="0-100 combined risk")
    ml_score: float | None = Field(description="Model fraud probability, null if no model loaded")
    rule_score: float
    reasons: list[Reason]
    model_version: str | None
    latency_ms: float
    scored_at: datetime
    duplicate: bool = False


AlertStatus = Literal["open", "confirmed_fraud", "false_positive"]


class AlertUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: AlertStatus
    analyst: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=1000)


BlockKind = Literal["account", "device", "ip", "merchant"]


class BlockEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: BlockKind
    value: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=300)
