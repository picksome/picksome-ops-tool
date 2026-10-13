"""환율 API schemas (M008, docs/27 D-288)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class ExchangeRateOut(BaseModel):
    currency: str
    rate_to_krw: Decimal


class SyncRatesRequest(BaseModel):
    rate_date: date | None = None  # 미지정 시 오늘
    currencies: list[str] = Field(default_factory=lambda: ["USD", "CNY", "JPY"])


class SyncRatesOut(BaseModel):
    rate_date: date
    rates: dict[str, Decimal]


class ManualRateUpsert(BaseModel):
    currency: str = Field(min_length=1, max_length=8)
    rate_date: date
    rate_to_krw: Decimal = Field(gt=0)
