"""환율 자동화 (M008, docs/27 D-288) — 한국수출입은행 Open API 고시환율.

product.cost_fx_rate(M005) 는 지금까지 수동 입력만 가능했다. 이 모듈은 그 값을
"오늘의 고시환율"로 자동 채워주는 근거 데이터(exchange_rate_snapshot)를 만든다.

API 계약(한국수출입은행 Open API, https://www.koreaexim.go.kr/ir/HPHKIR019M01):
  GET {base_url}?authkey={key}&searchdate=YYYYMMDD&data=AP01
  응답: JSON 배열, 각 원소 {result, cur_unit, deal_bas_r(매매기준율), cur_nm, ...}
  result: 1=성공, 2=DATA코드 오류, 3=인증코드 오류, 4=일일제한횟수 초과
  cur_unit 은 통화에 따라 "USD" 처럼 1단위거나 "JPY(100)"처럼 100단위 표기가 섞여있어,
  100단위 표기는 rate 를 100 으로 나눠 1단위 기준으로 정규화한다.
  중국 위안화는 국내 고시 관행상 역외(CNH) 로 나온다 — "CNY" 요청 시 "CNH" 로도 매칭.

KOREAEXIM_API_KEY 미설정 시 sync_daily_rates() 는 명시적으로 에러를 낸다(SMTP_HOST
미설정 패턴과 동일하게 "설정 없으면 조용히 실패"가 아니라 "설정 없으면 명확히 막힘").
수동 입력 fallback(upsert_manual_rate)은 키 없이도 항상 동작한다.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.exceptions import ValidationError

_JPY_STYLE_SUFFIX = "(100)"

# cur_unit 이 요청한 통화코드와 정확히 안 맞을 때의 별칭(국내 고시 관행).
_CURRENCY_ALIASES: dict[str, tuple[str, ...]] = {
    "CNY": ("CNY", "CNH"),
}


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _parse_rate(raw: str) -> Decimal:
    try:
        return Decimal(raw.replace(",", ""))
    except (InvalidOperation, AttributeError) as e:
        raise ValidationError(f"환율 값 파싱 실패: {raw!r}") from e


def fetch_daily_rates_from_api(
    rate_date: date, currencies: list[str]
) -> dict[str, Decimal]:
    """한국수출입은행 API 호출 → {currency: rate_to_krw(1단위 기준)}.

    주말/공휴일은 그날 고시가 없어 빈 배열이 돌아올 수 있다 — 호출자가 직전
    영업일로 재시도하거나 에러로 처리할지 결정.
    """
    settings = get_settings()
    if not settings.KOREAEXIM_API_KEY:
        raise ValidationError(
            "KOREAEXIM_API_KEY 가 설정되지 않았습니다. 환율 자동조회를 쓰려면 "
            "한국수출입은행 Open API 키를 발급받아 .env 에 설정하거나, "
            "upsert_manual_rate() 로 수동 입력하세요."
        )

    resp = httpx.get(
        settings.KOREAEXIM_API_BASE_URL,
        params={
            "authkey": settings.KOREAEXIM_API_KEY,
            "searchdate": rate_date.strftime("%Y%m%d"),
            "data": "AP01",
        },
        timeout=10,
    )
    resp.raise_for_status()
    rows = resp.json()

    wanted = {c.upper() for c in currencies}
    alias_lookup: dict[str, str] = {}
    for code in wanted:
        for alias in _CURRENCY_ALIASES.get(code, (code,)):
            alias_lookup[alias] = code

    out: dict[str, Decimal] = {}
    for row in rows:
        cur_unit = (row.get("cur_unit") or "").strip()
        if not cur_unit:
            continue  # result != 1 인 행(인증 실패 등)은 cur_unit 이 null

        base_unit = cur_unit.replace(_JPY_STYLE_SUFFIX, "").strip()
        target_code = alias_lookup.get(base_unit)
        if target_code is None:
            continue

        rate = _parse_rate(row["deal_bas_r"])
        if _JPY_STYLE_SUFFIX in cur_unit:
            rate = rate / 100
        out[target_code] = rate

    return out


def upsert_rate(
    db: Session, *, currency: str, rate_date: date, rate_to_krw: Decimal, source: str
) -> None:
    db.execute(
        text(
            """
            INSERT INTO exchange_rate_snapshot (currency, rate_to_krw, rate_date, source, fetched_at)
            VALUES (:cur, :rate, :d, :src, :now)
            ON CONFLICT (currency, rate_date) DO UPDATE SET
              rate_to_krw = EXCLUDED.rate_to_krw,
              source = EXCLUDED.source,
              fetched_at = EXCLUDED.fetched_at,
              updated_at = :now
            """
        ),
        {
            "cur": currency.upper(),
            "rate": rate_to_krw,
            "d": rate_date,
            "src": source,
            "now": _now(),
        },
    )


def upsert_manual_rate(
    db: Session, *, currency: str, rate_date: date, rate_to_krw: Decimal
) -> None:
    """API 키가 없거나 실패했을 때의 수동 입력 fallback."""
    upsert_rate(db, currency=currency, rate_date=rate_date, rate_to_krw=rate_to_krw, source="MANUAL")


def sync_daily_rates(
    db: Session, *, rate_date: date, currencies: list[str]
) -> dict[str, Decimal]:
    """API 조회 + 저장을 한 번에. 호출자가 commit 책임."""
    rates = fetch_daily_rates_from_api(rate_date, currencies)
    for currency, rate in rates.items():
        upsert_rate(db, currency=currency, rate_date=rate_date, rate_to_krw=rate, source="KOREAEXIM_API")
    return rates


def get_latest_rate(db: Session, *, currency: str, at: date | None = None) -> Decimal | None:
    """cost_fx_rate 자동 채움용 — 지정일(or 오늘) 이하 가장 최근 스냅샷."""
    row = db.execute(
        text(
            """
            SELECT rate_to_krw FROM exchange_rate_snapshot
             WHERE currency = :cur AND rate_date <= :at
             ORDER BY rate_date DESC LIMIT 1
            """
        ),
        {"cur": currency.upper(), "at": at or date.today()},
    ).first()
    return Decimal(str(row.rate_to_krw)) if row else None
