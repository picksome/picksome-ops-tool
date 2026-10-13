"""환율 라우터 (M008, docs/27 D-288).

endpoints:
  GET  /exchange-rates/{currency}   — 최신(또는 at 기준) 고시환율
  POST /exchange-rates/sync         — 한국수출입은행 API 조회 + 저장(KOREAEXIM_API_KEY 필요)
  POST /exchange-rates/manual       — 수동 입력(fallback, 키 없어도 항상 가능)

권한: 정책 라우터와 동일 패턴.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.domain.exchange_rate_api import (
    ExchangeRateOut,
    ManualRateUpsert,
    SyncRatesOut,
    SyncRatesRequest,
)
from app.services.exchange_rate import get_latest_rate, sync_daily_rates, upsert_manual_rate

router = APIRouter(prefix="/exchange-rates", tags=["exchange-rates"])

_read = require_roles("ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY")
_write = require_roles("ADMIN", "OPERATOR", "ACCOUNTING")


@router.get("/{currency}")
def get_rate(
    currency: str,
    at: date | None = None,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    rate = get_latest_rate(db, currency=currency, at=at)
    if rate is None:
        raise NotFoundError(f"{currency} 환율 데이터가 없습니다. /exchange-rates/sync 또는 /manual 로 먼저 채워주세요.")
    return envelope(ExchangeRateOut(currency=currency.upper(), rate_to_krw=rate).model_dump())


@router.post("/sync")
def sync(
    body: SyncRatesRequest,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    rate_date = body.rate_date or date.today()
    rates = sync_daily_rates(db, rate_date=rate_date, currencies=body.currencies)
    db.commit()
    return envelope(SyncRatesOut(rate_date=rate_date, rates=rates).model_dump())


@router.post("/manual", status_code=201)
def manual(
    body: ManualRateUpsert,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    upsert_manual_rate(
        db, currency=body.currency, rate_date=body.rate_date, rate_to_krw=body.rate_to_krw
    )
    db.commit()
    return envelope(
        ExchangeRateOut(currency=body.currency.upper(), rate_to_krw=body.rate_to_krw).model_dump()
    )
