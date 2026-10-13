"""발주 · 입고.

```
DRAFT → ORDERED → PARTIALLY_RECEIVED → RECEIVED
                → CANCELLED
```

입고와 재고는 한 트랜잭션이다
--------------------------
입고를 기록하면 재고 원장에도 같은 트랜잭션 안에서 줄이 들어간다. 나눠서
commit 하면 "입고는 됐는데 재고는 안 늘어난" 상태가 남고, 그 상태를 나중에
발견해도 어느 쪽이 맞는지 알 수 없다.

같은 줄을 두 번 넣는 것은 DB 가 막는다
--------------------------------
`uq_inv_txn_source` 가 `(org, source_table, source_id)` 를 유일하게 만든다.
그래서 같은 발주 라인으로 재고를 두 번 늘릴 수 없다 — 부분 입고는 라인이 아닌
**입고 건**을 출처로 삼아야 한다는 뜻이고, 그래서 아래에서 `source_id` 를
매번 새로 만드는 입고 이벤트 id 로 쓴다.

발주 단가는 여기서 정하고 박제한다
------------------------------
`product_source` 의 현재 원가를 **기본값으로 제안**하되, 라인에 복사해서
저장한다. 나중에 소싱 원가가 바뀌어도 과거 발주 금액은 안 바뀐다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import NotFoundError, ValidationError
from app.services import inventory

#: 상태 전이. 여기 없는 이동은 거부한다.
#:
#: `RECEIVED` 와 `CANCELLED` 에서 나가는 길이 없다 — 끝난 발주를 되살리면
#: 이미 반영된 재고와 어긋난다. 되돌려야 하면 재고 조정으로 한다.
_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "DRAFT": ("ORDERED", "CANCELLED"),
    "ORDERED": ("PARTIALLY_RECEIVED", "RECEIVED", "CANCELLED"),
    "PARTIALLY_RECEIVED": ("RECEIVED", "CANCELLED"),
    "RECEIVED": (),
    "CANCELLED": (),
}


def _next_business_key(db: Session, *, org_id: UUID) -> str:
    """`PO-20260811-003` 형태. 사람이 부르는 번호다.

    같은 날 몇 번째인지 센다. 동시에 두 건이 만들어지면 같은 번호가 나올 수
    있지만, `uq_purchase_order_key` 가 막고 호출자가 다시 시도한다 — 번호를
    미리 잠그자고 별도 시퀀스 테이블을 두는 것보다 이쪽이 단순하다.
    """
    today = datetime.now().strftime("%Y%m%d")
    n = db.execute(text("""
        SELECT COUNT(*) FROM purchase_order
         WHERE org_id = :org AND business_key LIKE :pfx
    """), {"org": str(org_id), "pfx": f"PO-{today}-%"}).scalar_one()
    return f"PO-{today}-{n + 1:03d}"


def create_order(
    db: Session, *, org_id: UUID, actor_user_id: UUID | None,
    supplier_site_id: UUID, lines: list[dict[str, Any]],
    expected_at: date | None = None, memo: str | None = None,
    currency: str = "KRW", automation_run_id: UUID | None = None,
    service_account_id: UUID | None = None,
) -> dict[str, Any]:
    """발주 초안 생성.

    만든 주체는 셋 중 하나다. **셋 다 없으면 만들지 않는다** — 나중에 "누가
    이 발주를 만들었나" 에 답할 수 없는 행이 생기면 안 된다.

    ```
    actor_user_id        사람이 화면에서
    automation_run_id    자동화 실행이
    service_account_id   AI·외부 에이전트가 (MCP)
    ```

    에이전트를 사람으로 위장시키지 않는다. `created_by` 는 비워 두고
    `created_by_service_account` 에 남긴다.
    """
    if not lines:
        raise ValidationError("발주 품목이 없습니다.")
    if actor_user_id is None and automation_run_id is None             and service_account_id is None:
        raise ValidationError("발주를 만든 주체를 알 수 없습니다.")

    exists = db.execute(text("""
        SELECT 1 FROM supplier_site
         WHERE id = :s AND org_id = :org AND deleted_at IS NULL
    """), {"s": str(supplier_site_id), "org": str(org_id)}).scalar()
    if not exists:
        raise NotFoundError("공급처를 찾을 수 없습니다.")

    # **한 줄도 쓰기 전에** 전부 검증한다.
    #
    # 헤더를 먼저 넣고 품목 루프에서 터지면 롤백에 기대야 하는데, 그 기대가
    # 틀리면 주인 없는 발주가 남는다. 검증을 앞으로 당기면 롤백 동작과
    # 무관하게 안전하다.
    for ln in lines:
        if int(ln["quantity"]) <= 0:
            raise ValidationError("발주 수량은 1 이상이어야 합니다.")
        if ln.get("unit_price") is None:
            # 값을 지어내지 않는다. 원가를 모르면 사람이 넣어야 한다.
            raise ValidationError("발주 단가가 없습니다. 소싱 원가를 확인하세요.")
        _assert_owned(db, org_id=org_id, line=ln)

    order_id = db.execute(text("""
        INSERT INTO purchase_order (
          org_id, business_key, supplier_site_id, status, currency,
          expected_at, memo, automation_run_id, created_by_service_account,
          created_by, updated_by
        ) VALUES (
          :org, :key, :site, 'DRAFT', :cur, :exp, :memo, :run, :svc,
          :actor, :actor
        )
        RETURNING id
    """), {
        "org": str(org_id), "key": _next_business_key(db, org_id=org_id),
        "site": str(supplier_site_id), "cur": currency, "exp": expected_at,
        "memo": memo,
        "run": str(automation_run_id) if automation_run_id else None,
        "svc": str(service_account_id) if service_account_id else None,
        "actor": str(actor_user_id) if actor_user_id else None,
    }).scalar_one()

    for ln in lines:
        db.execute(text("""
            INSERT INTO purchase_order_line (
              org_id, purchase_order_id, product_id, product_variant_id,
              quantity, unit_price, product_source_id
            ) VALUES (:org, :po, :pid, :vid, :qty, :price, :src)
        """), {
            "org": str(org_id), "po": str(order_id),
            "pid": str(ln["product_id"]),
            "vid": str(ln["product_variant_id"]) if ln.get("product_variant_id") else None,
            "qty": int(ln["quantity"]), "price": int(ln["unit_price"]),
            "src": str(ln["product_source_id"]) if ln.get("product_source_id") else None,
        })

    # 사람이 만들었을 때만 여기서 감사를 남긴다.
    #
    # 에이전트·자동화가 부르면 `actor_user_id` 가 없는데, 그대로 남기면
    # **주체가 비어 있는 감사 행**이 생긴다. "누가 이 발주를 만들었나" 에
    # 답하지 못하는 감사는 없느니만 못하다 — 있으면 확인했다고 착각하게 된다.
    #
    # 그 경우 기록은 호출자가 한다:
    #   MCP        `service_account.record_agent_audit` (actor_type=SERVICE_ACCOUNT)
    #   자동화      `automation_action` + `purchase_order.automation_run_id`
    #
    # MCP protocol 스모크가 이걸 잡았다.
    if actor_user_id is not None:
        record_audit(
            db, org_id=org_id, domain_table="purchase_order",
            domain_row_id=UUID(str(order_id)), action="CREATE",
            actor_user_id=actor_user_id,
            after={"status": "DRAFT", "lines": len(lines)},
            reason="발주 초안 생성",
        )
    db.commit()
    return get_order(db, org_id=org_id, order_id=UUID(str(order_id)))


def _assert_owned(db: Session, *, org_id: UUID, line: dict[str, Any]) -> None:
    """발주 품목이 참조하는 것이 전부 이 조직 소유인지.

    공급처만 확인하고 품목을 안 보면 남의 상품 id 를 실은 요청이 통과했다.
    발주 상세 응답이 그 상품의 **이름을 그대로 돌려주므로** UUID 만 알면
    남의 상품명을 읽어낼 수 있었다 — 재고 조정에 있던 것과 같은 결함이다.

    상품·옵션·소싱처를 각각 본다. 하나라도 남의 것이면 발주를 만들지 않는다.

    "찾을 수 없다" 로 답한다 — "권한이 없다" 라고 하면 그 id 가 **존재한다는
    사실**을 알려주게 된다.
    """
    owned = db.execute(text("""
        SELECT 1 FROM product
         WHERE id = :p AND org_id = :o AND deleted_at IS NULL
    """), {"p": str(line["product_id"]), "o": str(org_id)}).scalar()
    if not owned:
        raise NotFoundError("상품을 찾을 수 없습니다.")

    variant_id = line.get("product_variant_id")
    if variant_id:
        ok = db.execute(text("""
            SELECT 1 FROM product_variant
             WHERE id = :v AND product_id = :p AND org_id = :o
               AND deleted_at IS NULL
        """), {"v": str(variant_id), "p": str(line["product_id"]),
               "o": str(org_id)}).scalar()
        if not ok:
            raise NotFoundError("상품 옵션을 찾을 수 없습니다.")

    source_id = line.get("product_source_id")
    if source_id:
        ok = db.execute(text("""
            SELECT 1 FROM product_source
             WHERE id = :s AND org_id = :o AND deleted_at IS NULL
        """), {"s": str(source_id), "o": str(org_id)}).scalar()
        if not ok:
            raise NotFoundError("소싱처를 찾을 수 없습니다.")


def set_status(
    db: Session, *, org_id: UUID, actor_user_id: UUID, order_id: UUID,
    status: str, reason: str | None = None,
) -> dict[str, Any]:
    """상태 변경. 허용된 전이만 통과한다."""
    current = db.execute(text("""
        SELECT status FROM purchase_order
         WHERE id = :id AND org_id = :org AND deleted_at IS NULL
    """), {"id": str(order_id), "org": str(org_id)}).scalar()
    if current is None:
        raise NotFoundError("발주를 찾을 수 없습니다.")
    if status not in _TRANSITIONS.get(current, ()):
        raise ValidationError(f"{current} 에서 {status} 로 바꿀 수 없습니다.")

    db.execute(text("""
        UPDATE purchase_order
           SET status = :st, updated_at = now(), updated_by = :actor,
               ordered_at = CASE WHEN :st = 'ORDERED' THEN now() ELSE ordered_at END
         WHERE id = :id
    """), {"st": status, "id": str(order_id), "actor": str(actor_user_id)})

    record_audit(
        db, org_id=org_id, domain_table="purchase_order", domain_row_id=order_id,
        action="UPDATE", actor_user_id=actor_user_id,
        before={"status": current}, after={"status": status}, reason=reason,
    )
    db.commit()
    return get_order(db, org_id=org_id, order_id=order_id)


def receive(
    db: Session, *, org_id: UUID, actor_user_id: UUID, order_id: UUID,
    receipts: list[dict[str, Any]], reason: str | None = None,
    receipt_key: str | None = None,
) -> dict[str, Any]:
    """입고 처리 — **재고 원장까지 한 트랜잭션 안에서** 반영한다.

    `receipts` 는 `[{"line_id": ..., "quantity": 3}, ...]`.

    입고 수량은 발주 수량을 넘을 수 있다. 덤이나 오배송으로 실제로 일어나는
    일이라 막지 않고, 그대로 기록한다 — 실물과 다른 장부는 쓸모가 없다.

    ### `receipt_key` 가 필요한 이유

    "같은 수량을 두 번 입고" 는 **정상**이다. 3개짜리 상자가 두 번 오면 6개가
    맞다. 그래서 수량만 보고 자동으로 중복을 걸러내면 진짜 입고를 잃는다.

    걸러야 하는 것은 *같은 입고 사건이 두 번 전송된 것*(버튼 두 번, 네트워크
    재시도)뿐이고, 그건 **보낸 쪽만 구분할 수 있다.** 그래서 호출자가
    `receipt_key` 를 준다.

    ```
    receipt_key 있음   같은 키 재전송 → uq_inv_txn_source 가 막는다
    receipt_key 없음   매번 새 사건으로 처리한다 (중복 방지 없음)
    ```

    없을 때 조용히 막는 척하지 않는다.
    """
    order = db.execute(text("""
        SELECT status FROM purchase_order
         WHERE id = :id AND org_id = :org AND deleted_at IS NULL
    """), {"id": str(order_id), "org": str(org_id)}).scalar()
    if order is None:
        raise NotFoundError("발주를 찾을 수 없습니다.")
    if order not in ("ORDERED", "PARTIALLY_RECEIVED"):
        raise ValidationError(f"{order} 상태에서는 입고할 수 없습니다.")
    if not receipts:
        raise ValidationError("입고 수량이 없습니다.")

    # 입고 사건 하나를 가리키는 id. 호출자가 키를 줬으면 그 키에서 **결정적으로**
    # 만들어 재전송이 같은 id 가 되게 하고, 안 줬으면 새 사건으로 취급한다.
    receipt_id = (
        _key_to_uuid(f"{order_id}:{receipt_key}") if receipt_key else uuid4()
    )
    recorded = 0

    for r in receipts:
        qty = int(r["quantity"])
        if qty <= 0:
            raise ValidationError("입고 수량은 1 이상이어야 합니다.")
        line = db.execute(text("""
            SELECT product_id, product_variant_id, unit_price
              FROM purchase_order_line
             WHERE id = :id AND purchase_order_id = :po AND org_id = :org
        """), {
            "id": str(r["line_id"]), "po": str(order_id), "org": str(org_id),
        }).mappings().first()
        if line is None:
            raise NotFoundError("발주 품목을 찾을 수 없습니다.")

        db.execute(text("""
            UPDATE purchase_order_line
               SET received_quantity = received_quantity + :q, updated_at = now()
             WHERE id = :id
        """), {"q": qty, "id": str(r["line_id"])})

        inventory.record_transaction(
            db, org_id=org_id, product_id=UUID(str(line["product_id"])),
            product_variant_id=(
                UUID(str(line["product_variant_id"]))
                if line["product_variant_id"] else None
            ),
            transaction_type="PURCHASE_RECEIPT", quantity_delta=qty,
            actor_user_id=actor_user_id,
            # 발주 라인의 단가가 곧 취득원가다. 소싱 원가를 다시 읽지 않는다 —
            # 그 사이에 값이 바뀌었을 수 있고, 우리가 실제로 지불한 것은 이쪽이다.
            unit_cost=int(line["unit_price"]),
            reason=reason,
            source_table="purchase_order_receipt",
            # 라인마다 다른 출처가 되도록 라인 id 를 섞는다. 같은 입고 이벤트
            # 안에서 라인이 여러 개면 각각 한 줄씩 들어가야 한다.
            source_id=_receipt_source(receipt_id, r["line_id"]),
        )
        recorded += 1

    # 전부 채워졌는지 보고 상태를 옮긴다. 사람이 따로 누르게 하지 않는다.
    remaining = db.execute(text("""
        SELECT COUNT(*) FROM purchase_order_line
         WHERE purchase_order_id = :po AND received_quantity < quantity
    """), {"po": str(order_id)}).scalar_one()
    new_status = "RECEIVED" if remaining == 0 else "PARTIALLY_RECEIVED"
    db.execute(text("""
        UPDATE purchase_order
           SET status = :st, updated_at = now(), updated_by = :actor
         WHERE id = :id
    """), {"st": new_status, "id": str(order_id), "actor": str(actor_user_id)})

    record_audit(
        db, org_id=org_id, domain_table="purchase_order", domain_row_id=order_id,
        action="UPDATE", actor_user_id=actor_user_id,
        before={"status": order}, after={"status": new_status, "lines": recorded},
        reason=reason or "입고 처리",
    )
    db.commit()
    return get_order(db, org_id=org_id, order_id=order_id)


def _key_to_uuid(seed: str) -> UUID:
    """문자열을 안정된 UUID 로. 같은 입력이면 항상 같은 값이다.

    해시를 쓰는 이유는 충돌 회피지 비밀 유지가 아니다 — 여기 들어오는 값은
    비밀이 아니다.
    """
    import hashlib

    return UUID(bytes=hashlib.sha256(seed.encode()).digest()[:16], version=4)


def _receipt_source(receipt_id: UUID, line_id: Any) -> UUID:
    """입고 사건 + 라인 → 원장 출처 id.

    한 사건 안에 라인이 여럿이면 각각 원장 한 줄이 되어야 하므로 라인 id 를
    섞는다. `receipt_id` 가 결정적이면 이 값도 결정적이다.
    """
    return _key_to_uuid(f"{receipt_id}:{line_id}")


def get_order(db: Session, *, org_id: UUID, order_id: UUID) -> dict[str, Any]:
    head = db.execute(text("""
        SELECT o.id, o.business_key, o.status, o.currency, o.ordered_at,
               o.expected_at, o.memo, o.created_at, o.automation_run_id,
               o.created_by_service_account,
               s.name AS supplier_name, s.id AS supplier_site_id
          FROM purchase_order o
          JOIN supplier_site s ON s.id = o.supplier_site_id
         WHERE o.id = :id AND o.org_id = :org AND o.deleted_at IS NULL
    """), {"id": str(order_id), "org": str(org_id)}).mappings().first()
    if head is None:
        raise NotFoundError("발주를 찾을 수 없습니다.")

    lines = db.execute(text("""
        SELECT l.id, l.product_id, l.product_variant_id, l.quantity,
               l.received_quantity, l.unit_price, p.name_ko AS product_name,
               v.variant_key
          FROM purchase_order_line l
          JOIN product p ON p.id = l.product_id
          LEFT JOIN product_variant v ON v.id = l.product_variant_id
         WHERE l.purchase_order_id = :po
         ORDER BY p.name_ko
    """), {"po": str(order_id)}).mappings().all()

    return {
        "id": str(head["id"]),
        "business_key": head["business_key"],
        "status": head["status"],
        "currency": head["currency"],
        "supplier_site_id": str(head["supplier_site_id"]),
        "supplier_name": head["supplier_name"],
        "ordered_at": head["ordered_at"].isoformat() if head["ordered_at"] else None,
        "expected_at": head["expected_at"].isoformat() if head["expected_at"] else None,
        "memo": head["memo"],
        "created_at": head["created_at"].isoformat(),
        # 자동화가 만든 초안인지 화면이 구분할 수 있어야 한다.
        "created_by_automation": head["automation_run_id"] is not None,
        "created_by_agent": head["created_by_service_account"] is not None,
        "lines": [
            {
                "id": str(ln["id"]),
                "product_id": str(ln["product_id"]),
                "product_variant_id": (
                    str(ln["product_variant_id"]) if ln["product_variant_id"] else None
                ),
                "product_name": ln["product_name"],
                "variant_key": ln["variant_key"],
                "quantity": ln["quantity"],
                "received_quantity": ln["received_quantity"],
                "unit_price": ln["unit_price"],
                "line_total": ln["quantity"] * ln["unit_price"],
            }
            for ln in lines
        ],
        "total_amount": sum(ln["quantity"] * ln["unit_price"] for ln in lines),
    }


def list_orders(
    db: Session, *, org_id: UUID, status: list[str] | None = None,
    limit: int = 50, offset: int = 0,
) -> dict[str, Any]:
    where = ["o.org_id = :org", "o.deleted_at IS NULL"]
    params: dict[str, Any] = {"org": str(org_id), "lim": limit, "off": offset}
    if status:
        where.append("o.status = ANY(:st)")
        params["st"] = status

    rows = db.execute(text(f"""
        SELECT o.id, o.business_key, o.status, o.currency, o.expected_at,
               o.created_at, o.automation_run_id, s.name AS supplier_name,
               COALESCE(SUM(l.quantity * l.unit_price), 0)::bigint AS total_amount,
               COUNT(l.id)::int AS line_count
          FROM purchase_order o
          JOIN supplier_site s ON s.id = o.supplier_site_id
          LEFT JOIN purchase_order_line l ON l.purchase_order_id = o.id
         WHERE {' AND '.join(where)}
         GROUP BY o.id, s.name
         ORDER BY o.created_at DESC
         LIMIT :lim OFFSET :off
    """), params).mappings().all()

    total = db.execute(
        text(f"SELECT COUNT(*) FROM purchase_order o WHERE {' AND '.join(where)}"),
        {k: v for k, v in params.items() if k not in ("lim", "off")},
    ).scalar_one()

    return {
        "items": [
            {
                "id": str(r["id"]),
                "business_key": r["business_key"],
                "status": r["status"],
                "currency": r["currency"],
                "supplier_name": r["supplier_name"],
                "expected_at": (
                    r["expected_at"].isoformat() if r["expected_at"] else None
                ),
                "created_at": r["created_at"].isoformat(),
                "created_by_automation": r["automation_run_id"] is not None,
                "line_count": r["line_count"],
                "total_amount": int(r["total_amount"]),
            }
            for r in rows
        ],
        "total": total,
    }
