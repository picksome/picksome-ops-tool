"""NEXT ENGINE 주문 행 → 내부 DTO.

이 파일의 존재 이유는 **개인정보를 버리는 것**이다
------------------------------------------------
주문 응답 38개 필드 중 11개가 개인정보다.

```
buyer_name  buyer_id  buyer_phone
receiver_name  receiver_hp_phone  receiver_phone  receiver_email
receiver_zip_code  receiver_addr  receiver_msg
individual_clearance_no
```

Seller OS 는 손익·재고·정산 도구다. 이 중 어느 것도 그 계산에 쓰이지 않는다.
그래서 "저장할 때 빼자" 가 아니라 **경계에서 버린다.**

`ExternalOrderLine` 에는 개인정보 필드가 **아예 없다.** 있으면 언젠가 누가
저장한다 — 담을 그릇이 없으면 그 실수를 할 수 없다. vendor 원본 dict 도 들고
다니지 않는 이유가 같다(`ExternalProduct.raw` 와 다른 선택이다. 상품에는
개인정보가 없다).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

#: 응답에 오지만 **우리는 받지 않는** 필드.
#:
#: 상수로 적어 두는 이유는 두 가지다. 테스트가 "이 이름들이 DTO 어디에도 없다"
#: 를 검사할 수 있고, 벤더가 필드를 늘렸을 때 무엇을 검토해야 하는지가 남는다.
PII_FIELDS = frozenset({
    "buyer_name", "buyer_id", "buyer_phone",
    "receiver_name", "receiver_hp_phone", "receiver_phone", "receiver_email",
    "receiver_zip_code", "receiver_addr", "receiver_msg",
    "individual_clearance_no",
})

#: 주문 상태 코드. 공식 문서에 적힌 것만 둔다 — 이름으로 의미를 추측하지 않는다.
ORDER_STATUS = {
    "01": "미매핑",
    "02": "주문확인",
    "03": "배송준비중",
    "04": "배송중",
    "05": "배송완료",
    "25": "재고할당대기",
    "29": "미확인주문",
}


def _text(value: Any) -> str | None:
    """빈 문자열은 **없는 것**으로 본다.

    `ne_stock_cd` 가 `""` 로 오는 것을 확인했다. 그걸 값으로 취급하면 빈 문자열
    옵션 코드로 매핑을 시도하게 된다.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _money(value: Any) -> int | None:
    """금액. **없으면 None 이지 0 이 아니다.**

    0 으로 채우면 "수수료가 0원인 주문" 과 "수수료를 모르는 주문" 이 같아진다.
    그 둘이 같아지는 순간 순이익이 조용히 부풀려진다.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):      # bool 은 int 의 하위형이라 먼저 막는다
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _dt(value: Any) -> datetime | None:
    """`2023-11-01 11:37:47` 형식. 파싱 못 하면 None 이다 — 지어내지 않는다."""
    text = _text(value)
    if text is None:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


@dataclass(slots=True)
class ExternalOrderLine:
    """NEXT ENGINE 주문 **한 줄**. 개인정보 없음.

    응답의 한 행이 곧 한 라인이다(상품 하나 + 수량). 우리 `sales_order` 도
    라인 단위(`line_no`)라 구조가 맞는다.
    """

    # ── 식별자 ────────────────────────────────────────────────
    ne_order_no: str
    mall_order_no: str | None
    #: NEXT ENGINE 이 스토어마다 발급하는 코드(`mall02400`). 마켓 종류가 아니라
    #: **계정**을 가리킨다 — 쿠팡 본점과 2호점이 다른 값을 받는다.
    mall_cd: str | None
    mall_name: str | None
    mall_id: str | None
    delivery_group_no: str | None

    # ── 상품 ─────────────────────────────────────────────────
    ne_product_cd: str | None
    ne_partner_product_cd: str | None
    #: 옵션(재고) 코드. **비어 있을 수 있다** — 그때 무엇을 하는지가
    #: `next_engine_order_sync` 의 옵션 안전규칙이다.
    ne_stock_cd: str | None
    mall_product_cd: str | None
    #: 상품명은 **표시용 스냅샷**이다. 이걸로 상품을 찾지 않는다(fuzzy 금지).
    product_name: str | None
    option_name: str | None

    # ── 시각 ─────────────────────────────────────────────────
    order_date: datetime | None
    pay_date: datetime | None
    #: 수집일. 실제 응답에서 null 이었다 — watermark 로 쓸 수 없다.
    order_reg_date: datetime | None

    # ── 상태 ─────────────────────────────────────────────────
    order_status_divi: str | None

    # ── 금액 (없으면 None) ────────────────────────────────────
    order_price: int | None
    order_cnt: int | None
    sell_price: int | None
    sell_option_price: int | None
    delivery_price: int | None
    delivery_divi: str | None

    #: 추가구성/사은품 플래그. 라인 성격이 달라 그대로 넣으면 안 되는 경우가 있다.
    add_option_flg: str | None
    free_gift_flg: str | None

    @property
    def sale_date(self) -> date | None:
        """장부에 기록할 날짜. 결제일 우선, 없으면 주문일."""
        source = self.pay_date or self.order_date
        return source.date() if source else None

    @property
    def has_option_code(self) -> bool:
        return bool(self.ne_stock_cd)

    def identity(self) -> str:
        """**정확 식별자.** 같은 라인을 두 번 받아도 하나로 접히는 기준이다.

        `ne_order_no` 하나면 된다 — 실측으로 확인했다
        ------------------------------------------
        처음에는 `(ne_order_no, ne_product_cd)` 로 잡았다. "한 주문에 상품이
        여럿이면 주문번호만으로는 라인을 구분 못 한다" 고 생각해서였다.

        실제 응답은 달랐다. 23행에 `ne_order_no` 가 **23개** 였고
        `mall_order_no` 는 22개였다. 즉 쇼핑몰 주문 하나가 라인마다 **다른
        NEXT ENGINE 주문번호**를 받는다 — `ne_order_no` 가 이미 라인 식별자다.

        상품코드를 섞어 두면 같은 주문·같은 상품의 두 라인이 하나로 접힌다.
        실제로 그 일이 일어나 대사에서 거짓 불일치가 나왔다.
        """
        return self.ne_order_no


def normalize_order_line(row: dict[str, Any]) -> ExternalOrderLine:
    """vendor 행 하나 → DTO. **개인정보는 여기서 사라진다.**

    `row` 에서 읽는 키를 명시적으로 하나씩 적는다. `**row` 로 넘기거나 남은
    필드를 `raw` 에 담아 두면 개인정보가 같이 따라온다.
    """
    return ExternalOrderLine(
        ne_order_no=_text(row.get("ne_order_no")) or "",
        mall_order_no=_text(row.get("mall_order_no")),
        mall_cd=_text(row.get("mall_cd")),
        mall_name=_text(row.get("mall_name")),
        mall_id=_text(row.get("mall_id")),
        delivery_group_no=_text(row.get("delivery_group_no")),
        ne_product_cd=_text(row.get("ne_product_cd")),
        ne_partner_product_cd=_text(row.get("ne_partner_product_cd")),
        ne_stock_cd=_text(row.get("ne_stock_cd")),
        mall_product_cd=_text(row.get("mall_product_cd")),
        product_name=_text(row.get("product_name")),
        option_name=_text(row.get("option_name")),
        order_date=_dt(row.get("order_date")),
        pay_date=_dt(row.get("pay_date")),
        order_reg_date=_dt(row.get("order_reg_date")),
        order_status_divi=_text(row.get("order_status_divi")),
        order_price=_money(row.get("order_price")),
        order_cnt=_money(row.get("order_cnt")),
        sell_price=_money(row.get("sell_price")),
        sell_option_price=_money(row.get("sell_option_price")),
        delivery_price=_money(row.get("delivery_price")),
        delivery_divi=_text(row.get("delivery_divi")),
        add_option_flg=_text(row.get("add_option_flg")),
        free_gift_flg=_text(row.get("free_gift_flg")),
    )
