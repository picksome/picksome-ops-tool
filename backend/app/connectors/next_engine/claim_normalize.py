"""NEXT ENGINE 클레임 행 → 내부 DTO.

이 파일도 존재 이유가 **개인정보를 버리는 것**이다
---------------------------------------------
클레임 응답에는 주문과 같은 개인정보가 오고, 실측에서 **전부 채워져 있었다.**

```
buyer_name        10/10 행에 값 있음
receiver_name     10/10
receiver_addr     10/10
receiver_hp_phone 10/10
receiver_msg      10/10
receiver_zip_code 10/10
```

주문 쪽과 같은 방식으로 막는다 — `ExternalClaim` 에 담을 그릇 자체를 두지
않는다. 반품 사유(`return_req_msg`)와 교환 요청 메시지도 고객이 쓴 자유
문장이라 개인정보로 취급한다.

취소 사유는 왜 받나
----------------
`cancel_reason` 은 `필요 없어짐 (단순 변심)`, `판매자귀책; 재고없음` 처럼
**마켓이 정한 분류 문구**다(실측). 고객이 쓴 문장이 아니라 사유 코드에 가깝다.
누가 왜 취소했는지를 운영에서 봐야 하므로 받되, 자유 서술 메시지는 받지 않는다.

금액을 0 으로 만들지 않는다
-----------------------
공식 스키마에서 클레임 금액·수량은 **문자열**이다(주문은 정수다). 그리고
실측에서 `취소완료` 인데 `cancel_price` 가 `0` 으로 왔다 — `cancel_cnt` 는
1~2 였다. 3만원짜리 주문을 전량 취소했는데 0원일 리 없다.

**그 0 을 그대로 믿지 않는다.** 벤더가 안 채운 값이라고 보고, 금액 확정은
상위 서비스가 근거를 갖췄을 때만 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.connectors.next_engine.normalize import PII_FIELDS, _dt, _money, _text

#: 클레임 응답에만 있는 개인정보·자유서술. 주문 쪽 목록에 더해 함께 버린다.
CLAIM_PII_FIELDS = frozenset({
    # 고객이 직접 쓴 자유 문장. 주소·연락처가 섞여 들어오는 자리다.
    "return_delivery_msg", "return_req_msg", "change_cllct_req_msg",
})

#: 이 커넥터가 버리는 필드 전부.
DISCARDED_FIELDS = PII_FIELDS | CLAIM_PII_FIELDS

#: `claim_divi` 코드 → (유형, 정규화 상태).
#:
#: **공식 스키마의 개별 코드 설명만 근거로 쓴다.** 같은 설명에 붙어 있는 그룹
#: 라벨(`취소전체: ...`)은 코드 범위와 어긋나 있다 — 예컨대 `반품전체` 에
#: 교환 코드(10~15)가 들어가 있다. 그룹 라벨을 믿으면 반품을 교환으로 읽는다.
#:
#: 코드 `20`(반품수거철회)은 문서에 있지만 **벤더가 조회를 거부한다**(400,
#: "조회 하려는 클레임 상태가 잘못 되었습니다"). 조회할 수 없으므로 목록에서
#: 뺀다. 코드 `22` 는 설명이 없는데 조회는 받아 준다 — 의미를 모르므로
#: `UNKNOWN` 이다.
CLAIM_DIVI = {
    "06": ("CANCEL", "REQUESTED", "취소요청"),
    "07": ("CANCEL", "IN_PROGRESS", "최소중"),
    "08": ("CANCEL", "COMPLETED", "취소완료"),
    "09": ("CANCEL", "WITHDRAWN", "취소철회"),
    "10": ("EXCHANGE", "REQUESTED", "교환요청"),
    "11": ("EXCHANGE", "IN_PROGRESS", "교환수거중"),
    "12": ("EXCHANGE", "IN_PROGRESS", "교환수거완료"),
    "13": ("EXCHANGE", "IN_PROGRESS", "교환재배송중"),
    "14": ("EXCHANGE", "COMPLETED", "교환완료"),
    "15": ("EXCHANGE", "WITHDRAWN", "교환철회"),
    "16": ("RETURN", "REQUESTED", "반품요청"),
    "17": ("RETURN", "IN_PROGRESS", "반품수거중"),
    "18": ("RETURN", "IN_PROGRESS", "반품수거완료"),
    "19": ("RETURN", "COMPLETED", "반품완료"),
    "21": ("RETURN", "WITHDRAWN", "반품철회"),
    "22": ("UNKNOWN", "UNKNOWN", "(공식 설명 없음)"),
    "23": ("CANCEL", "COMPLETED", "판매자취소완료"),
    "24": ("RETURN", "COMPLETED", "판매자반품완료"),
}

#: 실제로 조회할 수 있는 코드. `claim_divi` 는 **필수 파라미터**라 상태별로
#: 나눠 불러야 한다 — "전체 클레임" 을 한 번에 받는 방법이 없다.
QUERYABLE_DIVI = tuple(CLAIM_DIVI)

#: 문서에는 있지만 벤더가 거부하는 코드. 왜 빠졌는지를 남긴다.
REJECTED_BY_VENDOR = ("20",)


@dataclass(slots=True)
class ExternalClaim:
    """클레임 한 건. **개인정보 필드가 아예 없다.**

    담을 그릇이 없으면 저장하는 실수를 할 수 없다. 주문 쪽
    `ExternalOrderLine` 과 같은 선택이다.
    """

    #: 클레임 식별자. 이것이 멱등성의 근거다.
    ne_claim_no: str | None = None
    #: 원 주문 식별자. 주문 커넥터가 라인 식별자로 쓰는 값과 같다.
    ne_order_no: str | None = None
    mall_order_no: str | None = None

    mall_cd: str | None = None
    mall_name: str | None = None

    ne_product_cd: str | None = None
    ne_partner_product_cd: str | None = None
    #: 상품명. 실측에서 공급처 URL 이 들어오기도 한다 — 개인정보는 아니다.
    product_name: str | None = None
    option_name: str | None = None

    #: 조회에 쓴 상태 코드. 응답 본문에는 이 값이 없다.
    claim_divi: str | None = None
    #: 벤더가 준 한글 상태.
    claim_status_txt: str | None = None
    return_status_divi: str | None = None
    change_status_divi: str | None = None

    #: 원 주문 사실. 금액 파생의 유일한 근거다.
    order_price: int | None = None
    order_cnt: int | None = None
    sell_price: int | None = None
    delivery_price: int | None = None

    cancel_cnt: int | None = None
    cancel_price: int | None = None
    cancel_reason: str | None = None
    return_cnt: int | None = None
    return_price: int | None = None
    return_delivery_price: int | None = None
    return_reason: str | None = None
    change_cnt: int | None = None
    change_price: int | None = None
    change_delivery_price: int | None = None
    change_reason: str | None = None

    order_date: datetime | None = None
    pay_date: datetime | None = None
    cancel_req_date: datetime | None = None
    cancel_upd_date: datetime | None = None
    return_req_date: datetime | None = None
    return_upd_date: datetime | None = None
    change_req_date: datetime | None = None
    change_upd_date: datetime | None = None
    claim_reg_date: datetime | None = None

    # ── 해석 ────────────────────────────────────────────────────────

    @property
    def claim_type(self) -> str:
        """취소/반품/교환. 모르는 코드는 **추측하지 않고** UNKNOWN 이다."""
        entry = CLAIM_DIVI.get(self.claim_divi or "")
        return entry[0] if entry else "UNKNOWN"

    @property
    def claim_status(self) -> str:
        entry = CLAIM_DIVI.get(self.claim_divi or "")
        return entry[1] if entry else "UNKNOWN"

    @property
    def quantity(self) -> int | None:
        """유형에 맞는 수량. **없으면 None 이다.**

        실측에서 `취소완료` 인데 `cancel_cnt` 가 0 으로 온 행이 있었다. 0 을
        "0개 취소" 로 읽으면 전량 취소가 아무 일도 아닌 것이 된다 —
        상위 서비스가 원 주문 수량과 맞춰 판단하도록 그대로 넘긴다.
        """
        return {
            "CANCEL": self.cancel_cnt,
            "RETURN": self.return_cnt,
            "EXCHANGE": self.change_cnt,
        }.get(self.claim_type)

    @property
    def vendor_amount(self) -> int | None:
        """벤더가 말한 클레임 금액. 유형에 맞는 칸만 본다."""
        return {
            "CANCEL": self.cancel_price,
            "RETURN": self.return_price,
            "EXCHANGE": self.change_price,
        }.get(self.claim_type)

    @property
    def vendor_shipping_amount(self) -> int | None:
        """클레임 배송비. 취소에는 이 칸 자체가 없다 — 0 이 아니라 없다."""
        return {
            "RETURN": self.return_delivery_price,
            "EXCHANGE": self.change_delivery_price,
        }.get(self.claim_type)

    @property
    def reason(self) -> str | None:
        return {
            "CANCEL": self.cancel_reason,
            "RETURN": self.return_reason,
            "EXCHANGE": self.change_reason,
        }.get(self.claim_type)

    @property
    def status_changed_at(self) -> datetime | None:
        return {
            "CANCEL": self.cancel_upd_date,
            "RETURN": self.return_upd_date,
            "EXCHANGE": self.change_upd_date,
        }.get(self.claim_type)

    @property
    def requested_at(self) -> datetime | None:
        return {
            "CANCEL": self.cancel_req_date,
            "RETURN": self.return_req_date,
            "EXCHANGE": self.change_req_date,
        }.get(self.claim_type)

    @property
    def sale_date(self) -> date | None:
        """원 주문의 판매일. 주문 커넥터와 **같은 기준**을 쓴다."""
        for value in (self.pay_date, self.order_date):
            if value is not None:
                return value.date()
        return None

    def identity(self) -> str | None:
        return self.ne_claim_no


def normalize_claim(row: dict[str, Any], *, claim_divi: str | None = None) -> ExternalClaim:
    """벤더 행 하나를 DTO 로. **개인정보 필드는 읽지도 않는다.**

    `claim_divi` 는 응답에 없다 — 조회할 때 쓴 값을 호출부가 넘긴다. 그게
    이 행의 상태다.
    """
    return ExternalClaim(
        ne_claim_no=_text(row.get("ne_claim_no")),
        ne_order_no=_text(row.get("ne_order_no")),
        mall_order_no=_text(row.get("mall_order_no")),
        mall_cd=_text(row.get("mall_cd")),
        mall_name=_text(row.get("mall_name")),
        ne_product_cd=_text(row.get("ne_product_cd")),
        ne_partner_product_cd=_text(row.get("ne_partner_product_cd")),
        product_name=_text(row.get("product_name")),
        option_name=_text(row.get("option_name")),
        claim_divi=_text(claim_divi),
        claim_status_txt=_text(row.get("claim_status_txt")),
        return_status_divi=_text(row.get("return_status_divi")),
        change_status_divi=_text(row.get("change_status_divi")),
        order_price=_money(row.get("order_price")),
        order_cnt=_money(row.get("order_cnt")),
        sell_price=_money(row.get("sell_price")),
        delivery_price=_money(row.get("delivery_price")),
        cancel_cnt=_money(row.get("cancel_cnt")),
        cancel_price=_money(row.get("cancel_price")),
        cancel_reason=_text(row.get("cancel_reason")),
        return_cnt=_money(row.get("return_cnt")),
        return_price=_money(row.get("return_price")),
        return_delivery_price=_money(row.get("return_delivery_price")),
        return_reason=_text(row.get("return_reason")),
        change_cnt=_money(row.get("change_cnt")),
        change_price=_money(row.get("change_price")),
        change_delivery_price=_money(row.get("change_delivery_price")),
        change_reason=_text(row.get("change_reason")),
        order_date=_dt(row.get("order_date")),
        pay_date=_dt(row.get("pay_date")),
        cancel_req_date=_dt(row.get("cancel_req_date")),
        cancel_upd_date=_dt(row.get("cancel_upd_date")),
        return_req_date=_dt(row.get("return_req_date")),
        return_upd_date=_dt(row.get("return_upd_date")),
        change_req_date=_dt(row.get("change_req_date")),
        change_upd_date=_dt(row.get("change_upd_date")),
        claim_reg_date=_dt(row.get("claim_reg_date")),
    )
