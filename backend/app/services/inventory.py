"""재고 원장.

수량은 저장하지 않는다. **움직임을 저장하고 합계로 구한다.**

```
on_hand   = SUM(inventory_transaction.quantity_delta)
inbound   = 발주했지만 아직 안 들어온 수량 (purchase_order_line)
available = on_hand            ← 아래 "RESERVED 를 지어내지 않는다" 참고
```

RESERVED 를 지어내지 않는다
------------------------
교과서적인 재고 모델은 `available = on_hand - reserved` 다. 여기서 reserved 는
"주문은 들어왔는데 아직 안 나간 것" 이다.

**우리는 그걸 알 수 없다.** `sales_order` 는 판매가 **끝난** 기록이고
출고 여부를 담고 있지 않다. 판매처 주문 API 가 붙기 전에는 어떤 주문이
미출고인지 알 방법이 없다.

그래서 `reserved` 를 0 으로 두지 않고 `NOT_CONNECTED` 로 표시한다.
0 이라고 하면 "확인했더니 없더라" 로 읽히는데 그건 거짓말이다.

취득원가는 소싱 원가가 아니다
-------------------------
```
product_source_cost_version   지금 다시 사면 얼마인가
inventory_transaction.unit_cost 이 재고를 얼마에 샀는가   ← 여기
```

공급처가 오늘 값을 올려도 창고에 있는 재고의 취득원가는 안 바뀐다. 그래서
소싱 원가를 재고 평가액으로 쓰지 않는다.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import NotFoundError, ValidationError

#: 원장에 들어갈 수 있는 움직임.
#:
#: `SALE` 과 `RETURN` 을 하나로 합치지 않는다 — 부호만 다른 게 아니라 원인이
#: 다르고, 반품률을 세려면 구분돼 있어야 한다.
TRANSACTION_TYPES = (
    "OPENING",           # 원장을 시작할 때의 기초 재고
    "PURCHASE_RECEIPT",  # 발주 입고
    "SALE",              # 판매 출고
    "RETURN",            # 반품 입고
    "ADJUSTMENT",        # 사람이 센 결과와 맞추기 (사유 필수)
)

#: 취득 성격의 움직임. 여기에만 `unit_cost` 가 의미를 갖는다.
_ACQUIRING = ("OPENING", "PURCHASE_RECEIPT")


def record_transaction(
    db: Session,
    *,
    org_id: UUID,
    product_id: UUID,
    product_variant_id: UUID | None,
    transaction_type: str,
    quantity_delta: int,
    actor_user_id: UUID | None,
    unit_cost: int | None = None,
    reason: str | None = None,
    source_table: str | None = None,
    source_id: UUID | None = None,
    currency: str = "KRW",
) -> UUID:
    """원장에 한 줄 추가. **commit 은 호출자 책임이다.**

    입고와 재고 반영이 한 트랜잭션이어야 하므로 여기서 commit 하지 않는다.
    중간에 commit 하면 입고는 됐는데 재고는 안 늘어난 상태가 남을 수 있다.
    """
    if transaction_type not in TRANSACTION_TYPES:
        raise ValidationError(f"알 수 없는 재고 변동 유형입니다: {transaction_type}")
    if quantity_delta == 0:
        # 0 은 아무 일도 안 일어난 것이다. 기록하면 원장만 지저분해진다.
        raise ValidationError("변동 수량이 0 입니다.")
    if transaction_type == "ADJUSTMENT" and not (reason or "").strip():
        # 조정은 근거가 원장 밖에 있다(사람이 세어 봤다). 사유가 없으면
        # 나중에 아무도 왜 숫자가 바뀌었는지 알 수 없다.
        raise ValidationError("재고 조정에는 사유가 필요합니다.")
    if unit_cost is not None and transaction_type not in _ACQUIRING:
        raise ValidationError(
            f"{transaction_type} 에는 취득 단가를 기록하지 않습니다.")

    # **상품이 이 조직 것인지 확인한다.**
    #
    # 안 하면 남의 상품 id 로 우리 org 에 원장이 생긴다. 그 행은 어느 화면에도
    # 안 보이는데(상품 JOIN 이 걸러낸다) 합계에는 들어가고, 상대 조직 상품에
    # 우리 재고가 붙는다. 역할 테스트가 이걸 잡았다.
    owned = db.execute(text("""
        SELECT 1 FROM product
         WHERE id = :p AND org_id = :o AND deleted_at IS NULL
    """), {"p": str(product_id), "o": str(org_id)}).scalar()
    if not owned:
        raise NotFoundError("상품을 찾을 수 없습니다.")
    if product_variant_id is not None:
        ok = db.execute(text("""
            SELECT 1 FROM product_variant
             WHERE id = :v AND product_id = :p AND org_id = :o
               AND deleted_at IS NULL
        """), {"v": str(product_variant_id), "p": str(product_id),
               "o": str(org_id)}).scalar()
        if not ok:
            raise NotFoundError("상품 옵션을 찾을 수 없습니다.")

    txn_id = db.execute(text("""
        INSERT INTO inventory_transaction (
          org_id, product_id, product_variant_id, transaction_type,
          quantity_delta, unit_cost, currency, reason,
          source_table, source_id, created_by
        ) VALUES (
          :org, :pid, :vid, :ttype, :qty, :cost, :cur, :reason,
          :stable, :sid, :actor
        )
        RETURNING id
    """), {
        "org": str(org_id), "pid": str(product_id),
        "vid": str(product_variant_id) if product_variant_id else None,
        "ttype": transaction_type, "qty": quantity_delta,
        "cost": unit_cost, "cur": currency, "reason": reason,
        "stable": source_table, "sid": str(source_id) if source_id else None,
        "actor": str(actor_user_id) if actor_user_id else None,
    }).scalar_one()
    return UUID(str(txn_id))


def adjust(
    db: Session, *, org_id: UUID, actor_user_id: UUID, product_id: UUID,
    product_variant_id: UUID | None, quantity_delta: int, reason: str,
) -> dict[str, Any]:
    """사람이 직접 재고를 맞춘다.

    **덮어쓰지 않고 차이를 기록한다.** "12개로 맞춰라" 가 아니라 "-3" 이 남는다.
    그래야 나중에 누가 언제 얼마나 손댔는지 원장에서 그대로 읽힌다.
    """
    before = get_balance(
        db, org_id=org_id, product_id=product_id,
        product_variant_id=product_variant_id,
    )
    txn_id = record_transaction(
        db, org_id=org_id, product_id=product_id,
        product_variant_id=product_variant_id,
        transaction_type="ADJUSTMENT", quantity_delta=quantity_delta,
        actor_user_id=actor_user_id, reason=reason,
    )
    after = before["on_hand"] + quantity_delta

    record_audit(
        db, org_id=org_id, domain_table="inventory_transaction",
        domain_row_id=txn_id, action="CREATE", actor_user_id=actor_user_id,
        before={"on_hand": before["on_hand"]}, after={"on_hand": after},
        reason=reason,
    )
    db.commit()
    return {
        "transaction_id": str(txn_id),
        "on_hand_before": before["on_hand"],
        "on_hand_after": after,
        # 음수가 됐다고 막지 않는다. 대신 알려 준다 — 조용히 0 으로 만들면
        # 실제로 몇 개가 비었는지 영영 모른다.
        "negative": after < 0,
    }


#: 재고 한 항목의 현재 상태. 원장 합계 + 미입고 발주.
#:
#: `product_variant` 를 LEFT JOIN 하는 이유: 옵션 없는 상품은 variant 가 없다.
_BALANCE_SELECT = """
    SELECT t.product_id, t.product_variant_id,
           p.name_ko AS product_name, p.business_key AS product_key,
           v.variant_key, v.sku_external,
           SUM(t.quantity_delta)::int AS on_hand,
           MAX(t.occurred_at) AS last_movement_at
      FROM inventory_transaction t
      JOIN product p ON p.id = t.product_id
      LEFT JOIN product_variant v ON v.id = t.product_variant_id
     WHERE t.org_id = :org
"""


def get_balance(
    db: Session, *, org_id: UUID, product_id: UUID,
    product_variant_id: UUID | None,
) -> dict[str, Any]:
    """한 항목의 현재 수량.

    원장에 한 줄도 없으면 `on_hand = 0` 이고 `tracked = False` 다. 둘은 다르다 —
    "세어 봤더니 0" 과 "재고를 관리한 적이 없다" 를 화면에서 구분해야 한다.
    """
    row = db.execute(text("""
        SELECT COALESCE(SUM(quantity_delta), 0)::int AS on_hand,
               COUNT(*)::int AS movements
          FROM inventory_transaction
         WHERE org_id = :org AND product_id = :pid
           AND product_variant_id IS NOT DISTINCT FROM :vid
    """), {
        "org": str(org_id), "pid": str(product_id),
        "vid": str(product_variant_id) if product_variant_id else None,
    }).mappings().one()
    return {
        "on_hand": row["on_hand"],
        "tracked": row["movements"] > 0,
        # RESERVED 를 계산할 근거가 없다. 0 이라고 하지 않는다.
        "reserved_status": "NOT_CONNECTED",
    }


def list_balances(
    db: Session, *, org_id: UUID, search: str | None = None,
    only_negative: bool = False, limit: int = 100, offset: int = 0,
) -> dict[str, Any]:
    """재고 현황 목록.

    `inbound` 는 **아직 안 들어온 발주 잔량**이다. 발주는 했는데 재고에는
    아직 없는 수량이라 재주문 판단에 반드시 필요하다 — 이게 없으면 이미
    주문한 것을 또 주문하라고 권하게 된다.
    """
    where = ["t.org_id = :org", "p.deleted_at IS NULL"]
    params: dict[str, Any] = {"org": str(org_id), "lim": limit, "off": offset}
    if search:
        where.append("(p.name_ko ILIKE :q OR v.sku_external ILIKE :q)")
        params["q"] = f"%{search}%"

    having = "HAVING SUM(t.quantity_delta) < 0" if only_negative else ""

    rows = db.execute(text(f"""
        WITH bal AS (
            {_BALANCE_SELECT} AND {' AND '.join(where[1:])}
             GROUP BY t.product_id, t.product_variant_id, p.name_ko,
                      p.business_key, v.variant_key, v.sku_external
             {having}
        ),
        inbound AS (
            SELECT l.product_id, l.product_variant_id,
                   SUM(l.quantity - l.received_quantity)::int AS qty
              FROM purchase_order_line l
              JOIN purchase_order o ON o.id = l.purchase_order_id
             WHERE l.org_id = :org AND o.deleted_at IS NULL
               AND o.status IN ('ORDERED', 'PARTIALLY_RECEIVED')
               AND l.quantity > l.received_quantity
             GROUP BY l.product_id, l.product_variant_id
        )
        SELECT bal.*, COALESCE(i.qty, 0) AS inbound
          FROM bal
          LEFT JOIN inbound i
                 ON i.product_id = bal.product_id
                AND i.product_variant_id IS NOT DISTINCT FROM bal.product_variant_id
         ORDER BY bal.on_hand ASC, bal.product_name
         LIMIT :lim OFFSET :off
    """), params).mappings().all()

    return {
        "items": [
            {
                "product_id": str(r["product_id"]),
                "product_variant_id": (
                    str(r["product_variant_id"]) if r["product_variant_id"] else None
                ),
                "product_name": r["product_name"],
                "product_key": r["product_key"],
                "variant_key": r["variant_key"],
                "sku": r["sku_external"],
                "on_hand": r["on_hand"],
                "inbound": r["inbound"],
                "negative": r["on_hand"] < 0,
                "last_movement_at": (
                    r["last_movement_at"].isoformat() if r["last_movement_at"] else None
                ),
            }
            for r in rows
        ],
        # 화면이 "재고 0" 과 "재고 미연동" 을 섞지 않게 한다.
        "reserved_status": "NOT_CONNECTED",
        "reserved_note": "미출고 주문을 알 수 없어 예약 수량은 계산하지 않습니다.",
    }


def list_transactions(
    db: Session, *, org_id: UUID, product_id: UUID,
    product_variant_id: UUID | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    """한 항목의 움직임 이력. 재고가 왜 지금 숫자인지 되짚는 화면이다."""
    rows = db.execute(text("""
        SELECT t.id, t.transaction_type, t.quantity_delta, t.unit_cost,
               t.currency, t.occurred_at, t.reason, t.source_table, t.source_id,
               u.display_name AS actor_name
          FROM inventory_transaction t
          LEFT JOIN app_user u ON u.id = t.created_by
         WHERE t.org_id = :org AND t.product_id = :pid
           -- CAST 가 필요하다. 바인딩된 NULL 만으로는 Postgres 가 이 파라미터의
           -- 타입을 정하지 못해 AmbiguousParameter 로 거절한다.
           AND (CAST(:vid AS uuid) IS NULL OR t.product_variant_id = CAST(:vid AS uuid))
         ORDER BY t.occurred_at DESC, t.id DESC
         LIMIT :lim
    """), {
        "org": str(org_id), "pid": str(product_id),
        "vid": str(product_variant_id) if product_variant_id else None,
        "lim": limit,
    }).mappings().all()
    return [
        {
            "id": str(r["id"]),
            "transaction_type": r["transaction_type"],
            "quantity_delta": r["quantity_delta"],
            "unit_cost": r["unit_cost"],
            "currency": r["currency"],
            "occurred_at": r["occurred_at"].isoformat(),
            "reason": r["reason"],
            "source_table": r["source_table"],
            "source_id": str(r["source_id"]) if r["source_id"] else None,
            # 자동화가 넣은 줄은 사람 이름이 없다. "시스템" 으로 표시한다.
            "actor_name": r["actor_name"] or "시스템",
        }
        for r in rows
    ]
