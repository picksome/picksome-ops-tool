"""재고 · 발주 · 재주문 (M048).

권한
----
```
재고 조회    ADMIN / OPERATOR / SOURCING / ACCOUNTING
재고 조정    ADMIN / OPERATOR              — 수량을 바꾸는 일이라 좁힌다
발주 조회    ADMIN / OPERATOR / SOURCING
발주 생성    ADMIN / OPERATOR / SOURCING   — 소싱 담당이 실제로 발주한다
발주 확정    ADMIN / OPERATOR              — 돈이 나가는 결정이다
입고 처리    ADMIN / OPERATOR
재주문 조회  ADMIN / OPERATOR / SOURCING
```

`ACCOUNTING` 이 재고를 **볼 수** 있는 이유: 재고 평가액이 결산에 들어간다.
바꾸지는 못한다.

재주문 파라미터를 서버가 정하지 않는다
--------------------------------
`velocity_days` 와 `coverage_days` 는 **필수 쿼리 파라미터**다. 기본값을 주면
사용자가 자기 사업에 맞게 정하지 않게 되고, 화면에 뜬 숫자가 어디서 왔는지
아무도 모르게 된다.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.core.sensitive import redact_acquisition_cost
from app.deps import CurrentUser, get_db
from app.services import inventory as inv
from app.services import purchasing, reorder

router = APIRouter(prefix="/inventory", tags=["inventory"])
po_router = APIRouter(prefix="/purchase-orders", tags=["purchasing"])

# 재고 **수량**은 조회 전용까지 본다 — 몇 개 남았는지는 금액이 아니다.
# 취득원가는 응답에서 지운다(`redact_acquisition_cost`). 화면에서 가리지 않는다.
_view = require_roles("ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY")
_adjust = require_roles("ADMIN", "OPERATOR")
# 발주는 금액이 본문이라 조회 전용에게 열지 않는다. 수량만 가려서 보여주는
# 발주 화면은 쓸모가 없다.
_po_view = require_roles("ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING")
_po_create = require_roles("ADMIN", "OPERATOR", "SOURCING")
_po_commit = require_roles("ADMIN", "OPERATOR")


# ── 재고 ───────────────────────────────────────────────────────


@router.get("")
def list_inventory(
    search: str | None = Query(None, max_length=100),
    only_negative: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_view),
) -> dict:
    """재고 현황.

    `reserved_status` 를 함께 준다 — 화면이 "예약 0" 이라고 쓰지 않게 하려는
    것이다. 우리는 미출고 주문을 모른다.
    """
    return envelope(redact_acquisition_cost(inv.list_balances(
        db, org_id=user.org_id, search=search, only_negative=only_negative,
        limit=limit, offset=offset), user.role))


@router.get("/{product_id}/transactions")
def list_transactions(
    product_id: UUID,
    variant_id: UUID | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_view),
) -> dict:
    """움직임 이력 — "재고가 왜 지금 숫자인가" 에 답하는 화면."""
    # 원장에는 취득 단가가 들어 있다. 권한 없으면 그 열만 사라진다.
    return envelope(redact_acquisition_cost(inv.list_transactions(
        db, org_id=user.org_id, product_id=product_id,
        product_variant_id=variant_id, limit=limit), user.role))


class AdjustIn(BaseModel):
    product_id: UUID
    product_variant_id: UUID | None = None
    #: 부호 있는 **차이**다. "12개로 맞춰라" 가 아니라 "-3".
    #: 목표 수량을 받으면 동시에 두 사람이 조정할 때 한쪽이 조용히 지워진다.
    #:
    #: Pydantic v2 에는 `ne=` 가 없다. 예전에 그렇게 써 뒀는데 **조용히
    #: 무시되고 있었다** — 0 을 막는 것은 서비스뿐이었다. validator 로 옮긴다.
    quantity_delta: int

    @field_validator("quantity_delta")
    @classmethod
    def _not_zero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("변동 수량이 0 입니다.")
        return value
    #: 필수다. 조정의 근거는 원장 밖(사람이 세어 봄)에 있다.
    reason: str = Field(..., min_length=1, max_length=500)


@router.post("/adjust")
def adjust(
    body: AdjustIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_adjust),
) -> dict:
    return envelope(inv.adjust(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        product_id=body.product_id, product_variant_id=body.product_variant_id,
        quantity_delta=body.quantity_delta, reason=body.reason))


# ── 재주문 ─────────────────────────────────────────────────────


@router.get("/reorder")
def reorder_candidates(
    # 기본값 없음. 사용자가 정해야 하는 값이다 — 모듈 문서 참고.
    velocity_days: int = Query(..., ge=1, le=365),
    coverage_days: int = Query(..., ge=1, le=365),
    group_by_supplier: bool = Query(False),
    as_of: date | None = Query(None),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_po_view),
) -> dict:
    """재주문 후보.

    `blocked` 를 반드시 함께 준다. 추천할 수 없는 상품을 숨기면 사용자는
    "우리 상품은 다 괜찮구나" 로 잘못 읽는다.
    """
    result = reorder.calculate(
        db, org_id=user.org_id, velocity_days=velocity_days,
        coverage_days=coverage_days, as_of=as_of)
    if group_by_supplier:
        result["groups"] = reorder.group_by_supplier(result["candidates"])
    return envelope(redact_acquisition_cost(result, user.role))


# ── 발주 ───────────────────────────────────────────────────────


class OrderLineIn(BaseModel):
    product_id: UUID
    product_variant_id: UUID | None = None
    quantity: int = Field(..., ge=1)
    #: 필수다. 모르면 0 으로 채우지 않고 사람이 확인한다.
    unit_price: int = Field(..., ge=0)
    product_source_id: UUID | None = None


class OrderIn(BaseModel):
    supplier_site_id: UUID
    lines: list[OrderLineIn] = Field(..., min_length=1)
    expected_at: date | None = None
    memo: str | None = Field(None, max_length=1000)
    currency: str = "KRW"


@po_router.get("")
def list_orders(
    status: list[str] | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_po_view),
) -> dict:
    return envelope(redact_acquisition_cost(purchasing.list_orders(
        db, org_id=user.org_id, status=status, limit=limit, offset=offset),
        user.role))


@po_router.post("")
def create_order(
    body: OrderIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_po_create),
) -> dict:
    """발주 **초안**. 만들었다고 주문이 나간 것은 아니다."""
    return envelope(purchasing.create_order(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        supplier_site_id=body.supplier_site_id,
        lines=[ln.model_dump(mode="json") for ln in body.lines],
        expected_at=body.expected_at, memo=body.memo, currency=body.currency))


@po_router.get("/{order_id}")
def get_order(
    order_id: UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_po_view),
) -> dict:
    return envelope(redact_acquisition_cost(purchasing.get_order(
        db, org_id=user.org_id, order_id=order_id), user.role))


class StatusIn(BaseModel):
    status: str
    reason: str | None = Field(None, max_length=500)


@po_router.post("/{order_id}/status")
def set_status(
    order_id: UUID,
    body: StatusIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_po_commit),
) -> dict:
    return envelope(purchasing.set_status(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        order_id=order_id, status=body.status, reason=body.reason))


class ReceiptLineIn(BaseModel):
    line_id: UUID
    quantity: int = Field(..., ge=1)


class ReceiveIn(BaseModel):
    receipts: list[ReceiptLineIn] = Field(..., min_length=1)
    reason: str | None = Field(None, max_length=500)
    #: 재전송 방지용 키. 화면이 폼마다 하나씩 만들어 보낸다.
    #:
    #: 없으면 매번 새 입고로 처리한다 — 같은 수량을 두 번 받는 것은 정상이라
    #: 수량만으로는 중복인지 알 수 없기 때문이다.
    receipt_key: str | None = Field(None, max_length=100)


@po_router.post("/{order_id}/receive")
def receive(
    order_id: UUID,
    body: ReceiveIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_po_commit),
) -> dict:
    """입고. 재고 원장까지 **한 트랜잭션**으로 반영된다."""
    return envelope(purchasing.receive(
        db, org_id=user.org_id, actor_user_id=user.user_id, order_id=order_id,
        receipts=[r.model_dump(mode="json") for r in body.receipts],
        reason=body.reason, receipt_key=body.receipt_key))
