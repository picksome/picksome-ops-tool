"""금액 계산 헬퍼.

원칙 (CLAUDE.md):
- float 절대 사용 금지
- 원 단위 정수(int) 또는 Decimal 만 사용
- BIGINT 컬럼 ↔ Python int 매핑
"""

from decimal import ROUND_HALF_UP, Decimal

KRW = Decimal(1)
RATE_QUANT = Decimal("0.0001")  # NUMERIC(7,4) 정밀도


def to_krw(value: int | Decimal) -> int:
    """KRW 정수(원). Decimal 입력은 반올림(HALF_UP)하여 정수화."""
    if isinstance(value, int):
        return value
    return int(value.quantize(KRW, rounding=ROUND_HALF_UP))


def apply_rate(amount: int, rate: Decimal) -> int:
    """`amount` 에 `rate` 적용 후 정수 원 반환."""
    return to_krw(Decimal(amount) * rate)


def split_vat(gross: int, vat_rate: Decimal, inclusion: str) -> tuple[int, int]:
    """공급가액·부가세 분리.

    inclusion = 'INCLUDED' : gross 안에 vat 포함 → supply = gross / (1+rate)
    inclusion = 'EXCLUDED' : gross 가 공급가액 → vat = gross * rate
    inclusion = 'EXEMPT'   : vat = 0

    반환: (supply_amount, vat_amount)
    """
    if inclusion == "EXEMPT":
        return gross, 0
    if inclusion == "EXCLUDED":
        vat = apply_rate(gross, vat_rate)
        return gross, vat
    # INCLUDED (기본)
    divisor = Decimal(1) + vat_rate
    supply = to_krw(Decimal(gross) / divisor)
    vat = gross - supply
    return supply, vat


def quantize_rate(value: Decimal) -> Decimal:
    return value.quantize(RATE_QUANT, rounding=ROUND_HALF_UP)
