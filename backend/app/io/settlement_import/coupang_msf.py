"""쿠팡 WING 매출·정산 상세(`MSF_PURCHASE_REVENUE_DETAIL`) 파서.

실측으로만 만들었다
-----------------
실제 WING 파일 3개(2026-01-22 · 2026-02-25 · 2026-03-23 정산분, 67행)를 읽어
헤더 25개를 전수 확인한 뒤에 썼다. 예상 컬럼명으로 먼저 만들지 않았다.

파일이 실제로 이렇게 생겼다
------------------------
```
주문번호 A
├─ 상품 행          옵션 ID = 숫자 11자리
├─ <기본배송료> 행   옵션 ID = '<기본배송료>'
└─ <추가배송료> 행   옵션 ID = '<추가배송료>'
```

`옵션 ID` 가 **행 종류 판별자**다. 실측 22개 주문 전부 이 구조였고, 그중
한 주문만 상품 행이 2개였다.

실측에서 나온 함정 넷
------------------
```
주문번호가 float 로 온다        4100157762225.0  → str() 하면 '.0' 이 붙는다
금액이 문자열로 온다             '24000' · ''      → '' 는 0 이 아니라 "해당 없음"
배송료 행은 상품 컬럼이 빈 문자열   쿠폰·소급분·마이샵할인
날짜가 문자열이거나 빈 문자열       '2026-01-22' · ''
```

`''` 를 0 으로 바꾸지 않는다. 배송료 행에 "쿠폰 0원" 이 아니라 "쿠폰이라는
개념이 없음" 이다. 둘을 같게 만들면 나중에 쿠폰 합계가 행 수만큼 부풀거나,
0원 쿠폰이 실제로 있었던 것처럼 보인다.

개인정보는 읽지 않는다
-------------------
`구매자명` 컬럼이 존재하고 실제로 채워져 있다. **이 파서는 그 열을 값으로
가져오지 않는다** — 헤더 존재만 확인하고 건너뛴다. 담을 그릇이 없으면
실수로 저장할 수도 없다.

관측한 관계 (채택하지 않음)
-----------------------
실측 67행 전부에서 성립했다.

```
정산금액 = 판매액 - COALESCE(판매자 할인쿠폰(A+B), 0) - 판매수수료
```

**이걸 계산식으로 쓰지 않는다.** 벤더가 준 `정산금액` 을 그대로 싣는다 —
관계가 깨지는 행(환불·조정)이 언젠가 오고, 그때 우리가 계산한 값이 벤더
값과 달라지면 어느 쪽이 사실인지 알 수 없게 된다. 관계는 검증에만 쓴다.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SOURCE_SYSTEM = "COUPANG_WING"
SOURCE_DOCUMENT = "MSF_PURCHASE_REVENUE_DETAIL"

# ── 행 종류 (M056 CHECK 와 같은 어휘) ────────────────────────────────
PRODUCT_SALE = "PRODUCT_SALE"
SHIPPING_FEE_BASE = "SHIPPING_FEE_BASE"
SHIPPING_FEE_EXTRA = "SHIPPING_FEE_EXTRA"
UNKNOWN = "UNKNOWN"

#: `옵션 ID` 표식 → 행 종류. 실측한 둘만 있다.
_MARKER_ROW_TYPE = {
    "<기본배송료>": SHIPPING_FEE_BASE,
    "<추가배송료>": SHIPPING_FEE_EXTRA,
}

# ── 헤더 ────────────────────────────────────────────────────────────
#
# **필수**: 없으면 이 파일을 정산서로 읽을 수 없다. 하나라도 빠지면
# `UnsupportedSchemaError` — 그럴듯하게 반쯤 읽어서 적재하지 않는다.
H_ORDER_NO = "주문번호"
H_OPTION_ID = "옵션 ID"
H_SALE_AMOUNT = "판매액"
H_COMMISSION = "판매수수료"
H_SETTLEMENT = "정산금액"
H_DUE_DATE = "정산예정일"

REQUIRED_HEADERS: tuple[str, ...] = (
    H_ORDER_NO, H_OPTION_ID, H_SALE_AMOUNT, H_COMMISSION, H_SETTLEMENT, H_DUE_DATE,
)

# 있으면 쓰고 없으면 넘어가는 것. 벤더가 컬럼을 늘리거나 줄여도 적재는 돈다.
H_PRODUCT_ID = "상품 ID"
H_QTY = "판매수량"
H_REFUND_QTY = "환불수량"
H_UNIT_PRICE = "판매가"
H_COUPON = "판매자 할인쿠폰(A+B)"
H_PAID_AT = "결제완료일"
H_DELIVERED_AT = "배송완료일"
H_CONFIRMED_AT = "구매확정일"
H_CANCELLED_AT = "취소완료일"

KNOWN_OPTIONAL_HEADERS: tuple[str, ...] = (
    "과세유형", H_PRODUCT_ID, "상품명", "옵션명", H_UNIT_PRICE, H_QTY, H_REFUND_QTY,
    H_COUPON, "판매자 할인쿠폰(A.즉시할인)", "판매자 할인쿠폰(B.다운로드)",
    "서비스이용율(%,VAT별도)", "서비스이용료소급분", "마이샵수수료할인",
    H_PAID_AT, H_DELIVERED_AT, H_CONFIRMED_AT, H_CANCELLED_AT, "구매확정(출고)유형",
)

#: **값을 읽지 않는 컬럼.** 존재해도 건너뛴다.
PII_HEADERS: frozenset[str] = frozenset({"구매자명"})

#: 빈 값 표기. 실측에서 `''` 와 `'-'` 둘 다 나왔다(후자는 스냅샷 파일).
_BLANKS = frozenset({"", "-"})

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


class SettlementParseError(Exception):
    """파일 전체를 읽을 수 없다."""


class UnsupportedSchemaError(SettlementParseError):
    """필수 헤더가 없다. 반쯤 읽지 않고 여기서 멈춘다."""


@dataclass(frozen=True, slots=True)
class ParsedRow:
    """정규화된 정산 행 1건. **DB 컬럼이 아니라 파일의 사실**이다."""

    source_row_no: int          # 시트 상의 행 번호(1-based, 헤더 포함)
    order_no: str
    line_reference: str         # 옵션 ID 또는 `<기본배송료>` 같은 표식
    row_type: str

    gross_revenue: int          # 판매액
    marketplace_fee: int        # 판매수수료
    net_settlement: int         # 정산금액
    seller_coupon: int | None   # 판매자 할인쿠폰(A+B). 배송료 행은 None

    quantity: int | None
    refund_quantity: int | None
    product_reference: str | None

    paid_at: date | None
    delivered_at: date | None
    confirmed_at: date | None
    cancelled_at: date | None
    expected_settlement_date: date | None

    row_fingerprint: str

    @property
    def is_refund_like(self) -> bool:
        """환불 성격 신호. **판정이 아니라 관측이다.**"""
        return bool(
            (self.refund_quantity or 0) > 0
            or self.cancelled_at is not None
            or self.gross_revenue < 0
            or self.net_settlement < 0
        )


@dataclass(slots=True)
class RejectedRow:
    """정산 행으로 만들 수 없던 행. **버리지 않고 센다**(대사에 들어간다)."""

    source_row_no: int
    reason: str
    detail: str | None = None


@dataclass(slots=True)
class ParseResult:
    source_rows: int = 0
    rows: list[ParsedRow] = field(default_factory=list)
    rejected: list[RejectedRow] = field(default_factory=list)
    sheet_name: str = ""
    unknown_headers: list[str] = field(default_factory=list)
    #: 파일이 담고 있는 정산 예정일들. 보통 1개다.
    due_dates: list[date] = field(default_factory=list)
    #: 관측 관계가 깨진 행 수. 적재를 막지는 않는다.
    relationship_mismatches: int = 0

    def reconciles(self) -> bool:
        return self.source_rows == len(self.rows) + len(self.rejected)


# ── 값 변환 ─────────────────────────────────────────────────────────

def _text(v: Any) -> str | None:
    """빈 값은 None. **0 으로 만들지 않는다.**"""
    if v is None:
        return None
    s = str(v).strip()
    return None if s in _BLANKS else s


def _order_no(v: Any) -> str:
    """주문번호. Excel 이 숫자로 주면 `float` 로 들어온다.

    `str(4100157762225.0)` 는 `'4100157762225.0'` 이다. 그대로 쓰면 우리
    장부의 `'4100157762225'` 와 영원히 안 붙는다 — 실측에서 34행 전부
    float 였다.
    """
    if v is None:
        raise ValueError("주문번호가 비어 있습니다.")
    if isinstance(v, bool):
        raise ValueError("주문번호 형식이 올바르지 않습니다.")
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not v.is_integer():
            raise ValueError("주문번호에 소수점이 있습니다.")
        # float 는 2^53 까지 정수를 정확히 담는다. 실측 최대값은 2.8e13 이라
        # 안전하지만, 넘으면 조용히 다른 번호가 되므로 막는다.
        if abs(v) > 2 ** 53:
            raise ValueError("주문번호가 정밀도 한계를 넘습니다.")
        return str(int(v))
    s = str(v).strip()
    if not s:
        raise ValueError("주문번호가 비어 있습니다.")
    return s


def _money(v: Any, *, field_name: str) -> int | None:
    """금액 → 원 단위 정수. 빈 값은 **None**(0 이 아니다).

    `float` 를 거치지 않는다. 계산 규칙(CLAUDE.md)이기도 하고, 실측에서
    금액이 문자열로 오기 때문에 굳이 float 를 경유할 이유가 없다.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError(f"{field_name}: 금액이 아닙니다.")
    if isinstance(v, int):
        return v
    if isinstance(v, Decimal):
        d = v
    else:
        s = str(v).strip().replace(",", "")
        if s in _BLANKS:
            return None
        try:
            d = Decimal(s)
        except InvalidOperation as exc:
            raise ValueError(f"{field_name}: 금액을 읽을 수 없습니다.") from exc
    if d != d.to_integral_value():
        # 원 단위가 아닌 값. 반올림해서 조용히 넘기지 않는다 —
        # 정산 금액이 1원씩 틀어지는 것을 아무도 못 본다.
        raise ValueError(f"{field_name}: 원 단위 정수가 아닙니다.")
    return int(d)


def _date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if s in _BLANKS:
        return None
    m = _DATE_RE.match(s)
    if not m:
        # 형식을 모르면 **추측해서 만들지 않는다.** 날짜 하나를 잘못 읽으면
        # 그 정산이 다른 달로 간다.
        raise ValueError(f"날짜 형식을 알 수 없습니다: {s[:20]!r}")
    return date(int(m[1]), int(m[2]), int(m[3]))


def _row_type_of(line_reference: str) -> str:
    if line_reference.isdigit():
        return PRODUCT_SALE
    return _MARKER_ROW_TYPE.get(line_reference, UNKNOWN)


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bytes_sha256(raw: bytes) -> str:
    """업로드 내용의 해시. **파일명이 아니라 내용**에서 만든다 —
    파일명은 바뀌고, 엑셀은 열기만 해도 수정 시각이 바뀐다."""
    return hashlib.sha256(raw).hexdigest()


def _fingerprint(values: dict[str, Any]) -> str:
    """행 내용 지문. 재발행 파일에서 **금액이 바뀌었는지** 보는 데 쓴다.

    PII 는 애초에 이 dict 에 없다(파서가 읽지 않는다).
    """
    payload = "|".join(f"{k}={values[k]!r}" for k in sorted(values))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def idempotency_key(*, due_date: date | None, order_no: str, line_reference: str) -> str:
    """같은 정산 행을 두 번 넣지 못하게 하는 키.

    벤더가 행 고유 id 를 주지 않는다(실측 25컬럼에 없다). 그래서 결정적
    조합을 쓴다.

    ```
    (정산예정일, 주문번호, 라인참조)
    ```

    실측 3파일 67행에서 **파일 안에서도 파일 사이에서도 유일**했다(67/67).

    `정산예정일` 을 넣는 이유: 같은 주문·같은 옵션이 다음 정산 회차에 환불로
    다시 나올 수 있다. 빼면 그 환불 행이 중복으로 취급돼 조용히 사라진다.
    같은 회차를 다시 받으면 예정일도 같으므로 멱등성은 그대로다.

    엑셀 행 번호·파일명·정렬 순서는 쓰지 않는다 — 다음 다운로드에서 바뀐다.
    """
    due = due_date.isoformat() if due_date else "NO_DUE_DATE"
    return f"{SOURCE_DOCUMENT}:{due}:{order_no}:{line_reference}"


# ── 파싱 ────────────────────────────────────────────────────────────

def parse_workbook(source: str | Path | bytes) -> ParseResult:
    """WING 정산 Excel → `ParseResult`. **DB 를 건드리지 않는다.**

    경로도 받고 바이트도 받는다. 업로드는 **디스크에 쓰지 않는다** — 정산서에는
    구매자명이 들어 있고, 저장하면 지워야 할 것이 하나 더 생긴다.
    """
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - 배포 환경엔 있다
        raise SettlementParseError("openpyxl 이 필요합니다.") from exc

    handle: Any = io.BytesIO(source) if isinstance(source, bytes) else source
    try:
        wb = load_workbook(handle, read_only=True, data_only=True)
    except Exception as exc:
        raise SettlementParseError(f"엑셀 파일을 열 수 없습니다: {exc}") from exc

    try:
        if not wb.worksheets:
            raise UnsupportedSchemaError("시트가 없습니다.")
        ws = wb.worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
        result = ParseResult(sheet_name=ws.title)
        if not rows:
            raise UnsupportedSchemaError("빈 시트입니다.")

        header = [(_text(h) or "") for h in rows[0]]
        missing = [h for h in REQUIRED_HEADERS if h not in header]
        if missing:
            raise UnsupportedSchemaError(
                f"정산서 필수 컬럼이 없습니다: {missing}. "
                "쿠팡 WING 매출·정산 상세(MSF) 파일이 맞는지 확인하세요."
            )
        known = set(REQUIRED_HEADERS) | set(KNOWN_OPTIONAL_HEADERS) | PII_HEADERS
        result.unknown_headers = sorted({h for h in header if h and h not in known})

        ix = {h: i for i, h in enumerate(header) if h}

        def cell(row: tuple, name: str) -> Any:
            i = ix.get(name)
            return row[i] if i is not None and i < len(row) else None

        # ── 1차: 행을 읽는다 ────────────────────────────────────────
        staged: list[ParsedRow] = []
        for n, raw in enumerate(rows[1:], start=2):
            # 완전히 빈 행은 원본 행 수에도 넣지 않는다. Excel 이 붙이는
            # 꼬리 행이고, 세면 대사가 영원히 안 맞는다.
            if all(_text(v) is None for v in raw):
                continue
            result.source_rows += 1
            try:
                staged.append(_parse_row(raw, cell, n))
            except ValueError as exc:
                result.rejected.append(RejectedRow(n, "INVALID_VALUE", str(exc)[:200]))

        # ── 정산 회차는 **파일 단위 사실**이다 ───────────────────────
        #
        # 배송료 행은 정산예정일이 비어 있는 경우가 많다(실측 22행 중 19행).
        # 그 행의 식별자에 예정일이 빠지면, 같은 주문이 다음 회차에 다시
        # 나왔을 때 두 행이 같은 키가 되어 **뒤엣것이 조용히 사라진다.**
        #
        # 실측 3파일 모두 파일 하나에 예정일이 정확히 하나였다. 그래서
        # 날짜가 빠진 행에 **그 회차 날짜를 채운다** — 값을 지어내는 것이
        # 아니라, 그 행이 속한 회차를 명시하는 것이다.
        #
        # 예정일이 둘 이상인 파일이면 채우지 않는다. 어느 회차인지 우리가
        # 정할 수 없고, 추측해서 채우면 틀린 회차에 붙는다.
        due_dates = sorted({r.expected_settlement_date for r in staged
                            if r.expected_settlement_date})
        result.due_dates = due_dates
        file_due = due_dates[0] if len(due_dates) == 1 else None

        # ── 2차: 식별자를 정하고 중복을 잡는다 ──────────────────────
        seen_keys: dict[str, int] = {}
        for parsed in staged:
            if parsed.expected_settlement_date is None and file_due is not None:
                parsed = replace(parsed, expected_settlement_date=file_due)
            if parsed.expected_settlement_date is None:
                # 회차를 못 정하면 멱등 키가 불안정해진다. 적재하지 않고
                # 사람에게 보인다 — 조용히 넣으면 다음 회차와 충돌한다.
                result.rejected.append(RejectedRow(
                    parsed.source_row_no, "NO_SETTLEMENT_CYCLE",
                    "정산예정일이 없고 파일에서도 회차를 하나로 정할 수 없습니다.",
                ))
                continue

            key = idempotency_key(
                due_date=parsed.expected_settlement_date,
                order_no=parsed.order_no,
                line_reference=parsed.line_reference,
            )
            if key in seen_keys:
                # 한 파일 안에서 같은 식별자가 두 번. 조용히 합치면 한 행이
                # 사라지고 대사가 안 맞는다 — 거부하고 사람에게 보인다.
                result.rejected.append(RejectedRow(
                    parsed.source_row_no, "DUPLICATE_IN_FILE",
                    f"{seen_keys[key]}행과 같은 식별자입니다.",
                ))
                continue
            seen_keys[key] = parsed.source_row_no

            if not _relationship_holds(parsed):
                result.relationship_mismatches += 1
            result.rows.append(parsed)

        return result
    finally:
        wb.close()


def _parse_row(raw: tuple, cell, row_no: int) -> ParsedRow:
    order_no = _order_no(cell(raw, H_ORDER_NO))
    line_reference = _text(cell(raw, H_OPTION_ID))
    if not line_reference:
        # 라인 참조가 없으면 멱등 키를 만들 수 없다. 실측에서는 67행 전부
        # 값이 있었다 — 없는 파일이 오면 그 사실을 알아야 한다.
        raise ValueError("옵션 ID 가 비어 있어 행을 식별할 수 없습니다.")

    gross = _money(cell(raw, H_SALE_AMOUNT), field_name=H_SALE_AMOUNT)
    fee = _money(cell(raw, H_COMMISSION), field_name=H_COMMISSION)
    net = _money(cell(raw, H_SETTLEMENT), field_name=H_SETTLEMENT)
    # 이 셋은 필수 컬럼이고 실측에서 한 번도 비지 않았다. 비어 있으면
    # 0 으로 채우지 않는다 — 금액을 모르는 정산 행을 0원으로 적재하면
    # 그 달 정산 합계가 조용히 틀린다.
    for name, v in ((H_SALE_AMOUNT, gross), (H_COMMISSION, fee), (H_SETTLEMENT, net)):
        if v is None:
            raise ValueError(f"{name} 이(가) 비어 있습니다.")

    qty = _money(cell(raw, H_QTY), field_name=H_QTY)
    refund_qty = _money(cell(raw, H_REFUND_QTY), field_name=H_REFUND_QTY)
    product_ref = cell(raw, H_PRODUCT_ID)
    if isinstance(product_ref, float) and product_ref.is_integer():
        product_ref = str(int(product_ref))
    else:
        product_ref = _text(product_ref)

    values = {
        "order_no": order_no, "line": line_reference,
        "gross": gross, "fee": fee, "net": net,
        "coupon": _money(cell(raw, H_COUPON), field_name=H_COUPON),
        "qty": qty, "refund_qty": refund_qty, "product": product_ref,
    }
    return ParsedRow(
        source_row_no=row_no,
        order_no=order_no,
        line_reference=line_reference,
        row_type=_row_type_of(line_reference),
        gross_revenue=gross,
        marketplace_fee=fee,
        net_settlement=net,
        seller_coupon=values["coupon"],
        quantity=qty,
        refund_quantity=refund_qty,
        product_reference=product_ref,
        paid_at=_date(cell(raw, H_PAID_AT)),
        delivered_at=_date(cell(raw, H_DELIVERED_AT)),
        confirmed_at=_date(cell(raw, H_CONFIRMED_AT)),
        cancelled_at=_date(cell(raw, H_CANCELLED_AT)),
        expected_settlement_date=_date(cell(raw, H_DUE_DATE)),
        row_fingerprint=_fingerprint(values),
    )


def _relationship_holds(r: ParsedRow) -> bool:
    """`정산금액 = 판매액 - 쿠폰 - 수수료` 인가. **관측용이다.**

    깨져도 적재를 막지 않는다 — 벤더 값이 사실이고, 우리 관계식이 아직
    모르는 경우(환불·조정)를 못 담고 있을 뿐이다. 다만 몇 건이 깨졌는지는
    보고해야 그 경우를 사람이 들여다본다.
    """
    return r.gross_revenue - (r.seller_coupon or 0) - r.marketplace_fee == r.net_settlement
