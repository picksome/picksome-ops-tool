"""주문·운영 예외 센터 — **사람이 손대야 하는 것만.**

새 테이블을 만들지 않는다
----------------------
예외는 이미 여러 곳에 기록돼 있다. 별도 `exception` 테이블을 만들면 원본과
어긋나는 두 번째 진실이 생기고, 어느 쪽이 맞는지 정하는 일이 시작된다.

```
operation_issue        운영 이슈 스캐너가 찾은 것
approval_request       사람 승인을 기다리는 것
inventory_transaction  음수 재고
purchase_order         기한이 지난 발주
marketplace_settlement 대사 미매칭
external_account       자격증명·커넥터 문제
```

이 모듈은 **읽기 모델**이다. 위의 것들을 한 화면 어휘로 모아 주기만 한다.

연결 안 된 것을 0 건이라고 하지 않는다
--------------------------------
클레임(취소·반품)은 판매처 API 가 붙어야 알 수 있다. 지금은 커넥터가 없다.

```
취소 요청 0건        ← 거짓말. 확인해 봤다는 뜻으로 읽힌다
클레임 API 미연결     ← 사실
```

그래서 카테고리마다 `status` 를 둔다: `OK` / `ATTENTION` / `NOT_CONNECTED` /
`NOT_CONFIGURED`. 건수가 0 인 것과 셀 수 없는 것은 다른 상태다.

재주문 기준은 자동화 정책이 갖는다
------------------------------
`velocity_days` / `coverage_days` 에 기본값이 없다는 원칙 때문에, 이 화면도
그 값을 스스로 정할 수 없다. **`REORDER_MONITOR` 정책의 guardrail 이 유일한
출처다** — 화면 쿼리 파라미터는 탐색용이고, 예외 센터·일일 리포트처럼 저절로
도는 것들은 정책을 읽는다.

정책이 없으면 `NOT_CONFIGURED` 다. 임의로 30일을 넣지 않는다.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

#: 화면 묶음. 순서가 곧 표시 순서다 — 되돌릴 수 없는 것이 위로 온다.
CATEGORIES = ("ORDER", "INVENTORY", "SETTLEMENT", "APPROVAL", "INTEGRATION")

CATEGORY_LABEL = {
    "ORDER": "주문·클레임",
    "INVENTORY": "재고·발주",
    "SETTLEMENT": "정산",
    "APPROVAL": "승인",
    "INTEGRATION": "연동",
}

#: 심각도는 `operation_issue` 의 것을 그대로 쓴다. 새 점수를 만들지 않는다 —
#: 두 체계가 생기면 같은 일이 화면마다 다른 급으로 보인다.
_SEVERITY_ORDER = {"BLOCKING": 0, "WARNING": 1, "INFO": 2}


def _item(
    *, category: str, kind: str, severity: str, title: str,
    detail: str | None = None, link: str | None = None,
    entity_type: str | None = None, entity_id: str | None = None,
    fix_label: str | None = None, fix_link: str | None = None,
) -> dict[str, Any]:
    return {
        "category": category, "kind": kind, "severity": severity,
        "title": title, "detail": detail, "link": link,
        "entity_type": entity_type, "entity_id": entity_id,
        # 무엇을 하면 없어지는지. 이게 없으면 사용자가 목록만 보고 막힌다.
        "fix_label": fix_label, "fix_link": fix_link,
    }


def build(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """예외 센터 전체.

    한 구획이 실패해도 나머지는 살린다 — 연동 하나가 깨졌다고 승인 대기가
    안 보이면 안 된다.
    """
    categories: dict[str, dict[str, Any]] = {}

    for name, fn in (
        ("ORDER", _orders),
        ("INVENTORY", _inventory),
        ("SETTLEMENT", _settlement),
        ("APPROVAL", _approvals),
        ("INTEGRATION", _integrations),
    ):
        try:
            categories[name] = fn(db, org_id=org_id)
        except Exception as exc:  # noqa: BLE001
            # 한 구획의 실패가 트랜잭션을 오염시키면 뒤 구획이 전부 죽는다.
            db.rollback()
            categories[name] = {
                "status": "ERROR", "items": [],
                "note": f"불러오지 못했습니다: {type(exc).__name__}",
            }

    items = [i for c in categories.values() for i in c["items"]]
    items.sort(key=lambda i: (
        _SEVERITY_ORDER.get(i["severity"], 9),
        CATEGORIES.index(i["category"]),
    ))

    return {
        "total": len(items),
        "by_severity": {
            s: sum(1 for i in items if i["severity"] == s)
            for s in ("BLOCKING", "WARNING", "INFO")
        },
        "categories": [
            {
                "category": name,
                "label": CATEGORY_LABEL[name],
                "status": categories[name]["status"],
                "count": len(categories[name]["items"]),
                "note": categories[name].get("note"),
            }
            for name in CATEGORIES
        ],
        "items": items,
    }


def summary(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """숫자만. 일일 리포트가 이걸 쓴다 — 두 화면의 숫자가 갈라지지 않게."""
    full = build(db, org_id=org_id)
    return {
        "total": full["total"],
        "by_severity": full["by_severity"],
        "categories": full["categories"],
    }


# ── 주문·클레임 ────────────────────────────────────────────────


def _orders(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """클레임은 판매처 API 가 있어야 안다.

    커넥터가 없으면 0 건이라고 쓰지 않는다 — "확인했더니 없다" 로 읽히고,
    실제로 반품이 쌓이고 있어도 화면은 조용하다.

    **연결됐으면 이제 진짜 숫자를 쓴다.** Phase 3 로 클레임 수집이 붙었는데
    화면이 계속 "미연결" 이라고 말하면, 그것도 같은 종류의 거짓이다 —
    이번에는 반대 방향으로.

    판정 근거는 `CLAIM_SYNC` 자동화가 **실제로 실행된 적이 있는가**다.
    정책이 켜져 있다는 것만으로는 부족하다. 켜 두고 한 번도 안 돈 상태와
    돌아서 0건인 상태는 다르다.
    """
    executed = db.execute(text("""
        SELECT max(created_at) FROM automation_action
         WHERE org_id = :o AND action_type = 'CLAIM_SYNC' AND outcome = 'EXECUTED'
    """), {"o": str(org_id)}).scalar()

    if executed is None:
        return {
            "status": "NOT_CONNECTED", "items": [],
            "note": "클레임 수집이 아직 실행되지 않았습니다 — 취소·반품 건수를 "
                    "알 수 없습니다.",
        }

    row = db.execute(text("""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE financial_effect = 'EFFECTIVE') AS effective,
               COUNT(*) FILTER (WHERE financial_effect = 'PENDING') AS pending,
               COUNT(*) FILTER (WHERE financial_effect = 'UNRESOLVED') AS unresolved
          FROM sales_claim WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org_id)}).mappings().one()

    items: list[dict[str, Any]] = []
    if row["unresolved"]:
        # 확정된 취소·반품인데 매출을 못 줄였다. **그만큼 매출이 부풀어 있다.**
        # 보류된 개별 클레임은 연동 묶음이 이미 보여 주므로, 여기서는 매출에
        # 미치는 영향만 한 줄로 말한다.
        items.append(_item(
            category="ORDER", kind="CLAIM_FINANCIAL_UNRESOLVED", severity="WARNING",
            title=f"매출 반영을 못 한 취소·반품 {row['unresolved']}건",
            detail="그만큼 순매출이 실제보다 크게 잡혀 있습니다. 이 상태로는 "
                   "그 달을 마감할 수 없습니다.",
            fix_label="외부 시스템", fix_link="/integrations/external",
        ))

    note = None
    if not row["total"]:
        note = "수집했고 취소·반품이 0건입니다."
    elif not items:
        # `NO_LEDGER_IMPACT` 는 여기서 세지 않는다 — 되돌릴 매출이 없어
        # 장부가 정확한 상태다. 개별 확인은 연동 묶음에 남아 있다.
        note = (f"취소·반품 {row['total']}건 수집됨 "
                f"(매출 반영 {row['effective']}건 · 확정 전 {row['pending']}건).")

    return {
        "status": "ATTENTION" if items else "OK",
        "items": items,
        "note": note,
    }


# ── 재고·발주 ──────────────────────────────────────────────────


def _inventory(db: Session, *, org_id: UUID) -> dict[str, Any]:
    items: list[dict[str, Any]] = []

    # 1) 음수 재고 — 장부가 실물과 어긋났다는 확실한 신호다.
    negatives = db.execute(text("""
        SELECT t.product_id, p.name_ko, SUM(t.quantity_delta)::int AS on_hand
          FROM inventory_transaction t
          JOIN product p ON p.id = t.product_id AND p.deleted_at IS NULL
         WHERE t.org_id = :o
         GROUP BY t.product_id, p.name_ko
        HAVING SUM(t.quantity_delta) < 0
         ORDER BY SUM(t.quantity_delta)
         LIMIT 50
    """), {"o": str(org_id)}).mappings().all()
    for r in negatives:
        items.append(_item(
            category="INVENTORY", kind="NEGATIVE_INVENTORY", severity="WARNING",
            title=f"{r['name_ko']} 재고 {r['on_hand']}개",
            detail="판매가 재고보다 많이 기록됐습니다. 기초 재고나 입고가 "
                   "누락됐을 수 있습니다.",
            entity_type="product", entity_id=str(r["product_id"]),
            fix_label="재고 원장 보기", fix_link=f"/inventory?product={r['product_id']}",
        ))

    # 2) 예상 입고일이 지난 발주 — 안 왔는데 아무도 모르고 있는 상태다.
    overdue = db.execute(text("""
        SELECT o.id, o.business_key, s.name AS supplier, o.expected_at
          FROM purchase_order o
          JOIN supplier_site s ON s.id = o.supplier_site_id
         WHERE o.org_id = :o AND o.deleted_at IS NULL
           AND o.status IN ('ORDERED', 'PARTIALLY_RECEIVED')
           AND o.expected_at IS NOT NULL AND o.expected_at < CURRENT_DATE
         ORDER BY o.expected_at
         LIMIT 50
    """), {"o": str(org_id)}).mappings().all()
    for r in overdue:
        items.append(_item(
            category="INVENTORY", kind="PURCHASE_OVERDUE", severity="WARNING",
            title=f"{r['business_key']} 입고 지연",
            detail=f"{r['supplier']} · 예상 입고 {r['expected_at']}",
            entity_type="purchase_order", entity_id=str(r["id"]),
            fix_label="발주 보기", fix_link="/purchase-orders",
        ))

    # 3) 재주문 — 정책이 기준을 갖고 있을 때만 계산한다.
    reorder_block = _reorder_items(db, org_id=org_id)
    items.extend(reorder_block["items"])

    status = "ATTENTION" if items else reorder_block["status"]
    return {"status": status, "items": items, "note": reorder_block.get("note")}


def _reorder_items(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """재주문 예외.

    기준(`velocity_days` / `coverage_days`)은 **자동화 정책**에서만 온다.
    정책이 없으면 계산하지 않고 그 사실을 알린다 — 여기서 30일을 지어내면
    "기본값 금지" 원칙이 화면 뒤에서 조용히 깨진다.
    """
    from app.services import automation as auto
    from app.services import reorder as ro

    policy = db.execute(text("""
        SELECT guardrail_json FROM automation_policy
         WHERE org_id = :o AND automation_type = 'REORDER_MONITOR'
           AND deleted_at IS NULL
    """), {"o": str(org_id)}).mappings().first()

    guard = dict(policy["guardrail_json"] or {}) if policy else {}
    velocity = guard.get("velocity_days")
    coverage = guard.get("coverage_days")
    if velocity is None or coverage is None:
        return {
            "status": "NOT_CONFIGURED", "items": [],
            "note": "재주문 기준(관찰 기간·목표 커버 일수)이 설정되지 않아 "
                    "계산하지 않았습니다.",
        }
    _ = auto  # 정책 어휘의 출처를 명시한다

    scan = ro.calculate(
        db, org_id=org_id,
        velocity_days=int(float(velocity)), coverage_days=int(float(coverage)))

    items: list[dict[str, Any]] = []
    for c in scan["candidates"]:
        if not c["overdue"]:
            # 아직 여유가 있는 것은 예외가 아니다. 전부 올리면 목록이 무의미해진다.
            continue
        items.append(_item(
            category="INVENTORY", kind="REORDER_REQUIRED", severity="WARNING",
            title=f"{c['product_name']} 발주 기한 경과",
            detail=f"재고 {c['on_hand']}개 · {c['stockout_date']} 소진 예상 · "
                   f"권장 {c['recommended_quantity']}개",
            entity_type="product", entity_id=c["product_id"],
            fix_label="재주문 보기", fix_link="/inventory",
        ))

    # 추천 못 한 것들은 **한 줄로 묶는다.** 상품마다 올리면 진짜 발주 건이 묻힌다.
    if scan["blocked"]:
        by_reason: dict[str, int] = {}
        for b in scan["blocked"]:
            by_reason[b["block_reason"]] = by_reason.get(b["block_reason"], 0) + 1
        detail = ", ".join(
            f"{ro.BLOCK_REASONS[k]} {v}건" for k, v in sorted(by_reason.items()))
        items.append(_item(
            category="INVENTORY", kind="REORDER_DATA_INCOMPLETE",
            # 기능이 안 되는 것이지 마감을 막는 것은 아니다. BLOCKING 이 아니다.
            severity="INFO",
            title=f"재주문을 계산할 수 없는 상품 {len(scan['blocked'])}건",
            detail=detail,
            fix_label="상품 소싱 설정", fix_link="/sourcing/suppliers",
        ))

    return {"status": "ATTENTION" if items else "OK", "items": items}


# ── 정산 ───────────────────────────────────────────────────────


def _settlement(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """정산 대사에서 사람이 손대야 하는 것.

    **셋을 한 줄로 뭉치지 않는다** (M055·M056). 할 일이 다르다.

    ```
    AMBIGUOUS   후보는 있는데 라인이 여럿   → 고르면 끝난다 (승인 필요)
    UNMATCHED   그 주문번호의 판매가 없다    → 주문을 기다리거나 제외한다
    EXCLUDED    사람이 이미 뺐다            → 할 일 없다. 세지 않는다
    ```

    예전에는 `matched_sales_order_id IS NULL` 하나로 셋을 다 셌다. 그러면
    제외 처리한 광고비 행이 영원히 "미매칭" 으로 남아 숫자가 줄지 않고,
    사용자는 그때부터 이 항목을 안 본다.
    """
    row = db.execute(text("""
        SELECT COUNT(*) FILTER (WHERE matching_status = 'UNMATCHED')  AS unmatched,
               COUNT(*) FILTER (WHERE matching_status = 'AMBIGUOUS')  AS ambiguous
          FROM marketplace_settlement_record
         WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org_id)}).mappings().one()

    items = []
    if row["ambiguous"]:
        items.append(_item(
            category="SETTLEMENT", kind="AMBIGUOUS_SETTLEMENT", severity="WARNING",
            title=f"정산 라인 선택 대기 {row['ambiguous']}건",
            detail="한 주문에 상품 라인이 여러 개라 어느 라인의 정산금인지 "
                   "규칙으로는 정할 수 없습니다. 직접 고르면 됩니다.",
            fix_label="정산 대사", fix_link="/settlements/marketplace",
        ))
    if row["unmatched"]:
        items.append(_item(
            category="SETTLEMENT", kind="UNMATCHED_SETTLEMENT", severity="WARNING",
            title=f"정산 대사 미매칭 {row['unmatched']}건",
            detail="그 주문번호로 장부에 잡힌 판매가 없습니다. 주문이 아직 "
                   "안 들어왔거나, 주문과 무관한 정산행일 수 있습니다.",
            fix_label="정산 대사", fix_link="/settlements/marketplace",
        ))

    # 정산서와 장부 금액이 다른 주문 (docs/83 D4). **장부를 고치지 않는다** —
    # 정산서가 더 정확할 가능성이 높아도 판매 원본을 덮으면 "그때 무엇을
    # 팔았나" 의 근거가 사라진다. 사람이 보고 판단한다.
    mismatch = db.execute(text("""
        SELECT COUNT(*) FROM (
          SELECT m.matched_sales_order_id AS so_id, SUM(m.gross_revenue) AS settled
            FROM marketplace_settlement_record m
           WHERE m.org_id = :o AND m.deleted_at IS NULL
             AND m.matching_status IN ('MATCHED_EXACT', 'MATCHED_MANUAL')
           GROUP BY 1
        ) g
        JOIN sales_order so ON so.id = g.so_id AND so.deleted_at IS NULL
       WHERE g.settled <> so.sale_price
    """), {"o": str(org_id)}).scalar_one()
    if mismatch:
        items.append(_item(
            category="SETTLEMENT", kind="SETTLEMENT_REVENUE_MISMATCH",
            severity="WARNING",
            title=f"정산서와 장부 금액 불일치 {mismatch}건",
            detail="판매처가 정산한 매출과 장부의 판매금액이 다릅니다. "
                   "대개 장부의 수량이나 배송비매출이 빠진 경우입니다 — "
                   "판매 원본은 자동으로 고치지 않았습니다.",
            fix_label="판매 장부", fix_link="/sales",
        ))
    return {"status": "ATTENTION" if items else "OK", "items": items}


# ── 승인 ───────────────────────────────────────────────────────


def _approvals(db: Session, *, org_id: UUID) -> dict[str, Any]:
    rows = db.execute(text("""
        SELECT request_type, COUNT(*) AS n
          FROM approval_request
         WHERE org_id = :o AND deleted_at IS NULL AND status = 'PENDING'
         GROUP BY request_type
    """), {"o": str(org_id)}).mappings().all()
    if not rows:
        return {"status": "OK", "items": []}
    total = sum(r["n"] for r in rows)
    return {"status": "ATTENTION", "items": [_item(
        category="APPROVAL", kind="PENDING_APPROVAL", severity="WARNING",
        title=f"승인 대기 {total}건",
        detail=", ".join(f"{r['request_type']} {r['n']}건" for r in rows),
        fix_label="승인함", fix_link="/approvals",
    )]}


# ── 연동 ───────────────────────────────────────────────────────


def _integrations(db: Session, *, org_id: UUID) -> dict[str, Any]:
    items: list[dict[str, Any]] = []

    broken = db.execute(text("""
        SELECT a.id, a.display_name, s.code,
               a.credential_secret_ref IS NULL AS no_credential
          FROM external_account a
          JOIN external_system s ON s.id = a.external_system_id
         WHERE a.org_id = :o AND a.deleted_at IS NULL AND a.status <> 'DISABLED'
    """), {"o": str(org_id)}).mappings().all()

    from app.services import connector as conn

    for a in broken:
        if a["no_credential"]:
            items.append(_item(
                category="INTEGRATION", kind="CREDENTIAL_BROKEN", severity="INFO",
                title=f"{a['display_name']} 자격증명 없음",
                detail="자격증명이 없어 이 연동은 동작하지 않습니다.",
                entity_type="external_account", entity_id=str(a["id"]),
                fix_label="외부 시스템", fix_link="/integrations/external",
            ))
        elif not conn.has_connector(str(a["code"])):
            items.append(_item(
                category="INTEGRATION", kind="CONNECTOR_UNAVAILABLE", severity="INFO",
                title=f"{a['display_name']} 연동 미구현",
                detail="자격증명은 있지만 이 시스템의 연동이 아직 없습니다.",
                entity_type="external_account", entity_id=str(a["id"]),
                fix_label="외부 시스템", fix_link="/integrations/external",
            ))

    # 클레임 — 취소·반품이 왔는데 장부에 반영하지 못한 것들.
    #
    # 매출이 줄어야 하는데 안 줄어든 상태다. 조용히 두면 그 달 매출이
    # 부풀어 있고, 아무도 그 사실을 모른다.
    for row in db.execute(text("""
        SELECT issue_type, COUNT(*) AS n FROM operation_issue
         WHERE org_id = :o AND deleted_at IS NULL
           AND status = ANY(ARRAY['OPEN','IN_PROGRESS','WAITING'])
           AND issue_type = ANY(:kinds)
         GROUP BY issue_type
    """), {"o": str(org_id), "kinds": list(_CLAIM_ISSUE_LABEL)}).mappings().all():
        items.append(_item(
            category="INTEGRATION", kind=row["issue_type"], severity="WARNING",
            title=f"{_CLAIM_ISSUE_LABEL[row['issue_type']]} {row['n']}건",
            detail=_CLAIM_ISSUE_DETAIL.get(row["issue_type"]),
            fix_label="외부 시스템", fix_link="/integrations/external",
        ))

    # 미매핑 외부 상품 — 동기화가 **성공했는데도** 주문이 안 들어온 경우다.
    # 실패가 아니라 운영 데이터 선행조건이므로 SYNC_FAILED 로 세지 않는다.
    # 적재가 `operation_issue` 에 남긴 것을 읽는다(두 번째 진실을 만들지 않는다).
    unmapped = db.execute(text("""
        SELECT COUNT(*) FROM operation_issue
         WHERE org_id = :o AND deleted_at IS NULL
           AND issue_type = 'UNMAPPED_EXTERNAL_PRODUCT'
           AND status = ANY(ARRAY['OPEN','IN_PROGRESS','WAITING'])
    """), {"o": str(org_id)}).scalar_one()
    if unmapped:
        items.append(_item(
            category="INTEGRATION", kind="UNMAPPED_EXTERNAL_PRODUCT",
            severity="WARNING",
            title=f"연결 안 된 외부 상품 {unmapped}건",
            detail="상품이 연결될 때까지 해당 주문은 적재하지 않습니다. "
                   "이름이 비슷하다고 자동으로 잇지 않습니다.",
            fix_label="외부 시스템", fix_link="/integrations/external",
        ))

    # 동기화의 마지막 결과. **왜 실패했는지까지** 보여 준다 —
    # "동기화 실패" 만 뜨면 화면을 봐도 무엇을 해야 할지 모른다.
    #
    # 계정마다 가장 최근 실행 하나만 본다. 어제 실패하고 오늘 성공했으면
    # 지금은 문제가 없는 것이고, 지난 실패를 계속 띄우면 목록을 안 보게 된다.
    #
    # **주문과 클레임을 따로 본다** (M057). 둘은 다른 endpoint 이고 따로
    # 켜므로, 하나의 실패를 다른 하나의 이름으로 보고하면 사용자는 멀쩡한
    # 쪽을 고치러 간다. 예전에는 클레임 실패가 "주문 동기화 실패" 로 떴다.
    for sync_type, label, prefix in (
        ("ORDERS", "주문 동기화", "ORDER_SYNC"),
        ("CLAIMS", "취소·반품 수집", "CLAIM_SYNC"),
    ):
        for row in db.execute(text("""
            SELECT DISTINCT ON (r.external_account_id)
                   r.external_account_id, r.status, r.error_code, a.display_name
              FROM external_sync_run r
              JOIN external_account a ON a.id = r.external_account_id
             WHERE r.org_id = :o AND r.sync_type = :st
               AND r.finished_at IS NOT NULL
             ORDER BY r.external_account_id, r.started_at DESC
        """), {"o": str(org_id), "st": sync_type}).mappings().all():
            if row["status"] not in ("FAILED", "PARTIAL"):
                continue
            code = row["error_code"] or f"{prefix}_FAILED"
            items.append(_item(
                category="INTEGRATION", kind=f"{prefix}_{code}",
                # 자격증명이 거부된 상태는 그대로 두면 계속 안 들어온다.
                severity="BLOCKING" if code == "AUTH_FAILED" else "WARNING",
                title=f"{row['display_name']} {label} {_SYNC_LABEL.get(code, code)}",
                detail=_SYNC_DETAIL.get(code),
                entity_type="external_account",
                entity_id=str(row["external_account_id"]),
                fix_label="외부 시스템", fix_link="/integrations/external",
            ))

    failed = db.execute(text("""
        SELECT COUNT(*) FROM external_sync_run
         WHERE org_id = :o AND status = 'FAILED'
           AND sync_type <> 'ORDERS'
           AND started_at > now() - INTERVAL '7 days'
    """), {"o": str(org_id)}).scalar_one()
    if failed:
        items.append(_item(
            category="INTEGRATION", kind="SYNC_FAILED", severity="WARNING",
            title=f"최근 7일 동기화 실패 {failed}건",
            fix_label="외부 시스템", fix_link="/integrations/external",
        ))

    return {"status": "ATTENTION" if items else "OK", "items": items}


#: 클레임 보류 종류. 토큰은 `claim_ingest` 가 정한 것을 그대로 쓴다.
_CLAIM_ISSUE_LABEL = {
    "UNMATCHED_CLAIM_ORDER": "원 주문을 못 찾은 취소·반품",
    "UNMATCHED_CLAIM_LINE": "어느 주문 라인인지 모르는 취소·반품",
    "CLAIM_FINANCIAL_UNRESOLVED": "매출 반영을 못 한 취소·반품",
    "CLAIM_STATUS_UNKNOWN": "해석할 수 없는 클레임 상태",
}

_CLAIM_ISSUE_DETAIL = {
    "UNMATCHED_CLAIM_ORDER":
        "원 주문이 아직 장부에 없습니다. **되돌릴 매출이 없으므로 장부 금액은 "
        "정확하고 마감을 막지 않습니다** — 출고 전 취소면 정상입니다. 주문이 "
        "뒤늦게 들어오면 다음 동기화가 자동으로 이어 붙입니다.",
    "UNMATCHED_CLAIM_LINE":
        "한 주문번호에 라인이 여럿이라 어느 쪽인지 알 수 없습니다. 임의로 나눠 "
        "붙이지 않았습니다.",
    "CLAIM_FINANCIAL_UNRESOLVED":
        "취소·반품이 확정됐는데 금액을 확정할 수 없습니다. **그만큼 매출이 아직 "
        "줄지 않았습니다** — 판매처에서 금액을 확인해 주세요.",
    "CLAIM_STATUS_UNKNOWN":
        "판매처가 모르는 상태 코드를 보냈습니다. 잘못 해석해 매출을 건드리지 "
        "않았습니다.",
}

#: 실패 사유를 사람 말로. 토큰은 `order_sync` 가 정한 것을 그대로 쓴다 —
#: 여기서 새 어휘를 만들면 화면과 기록이 다른 말을 하게 된다.
_SYNC_LABEL = {
    "AUTH_FAILED": "인증 실패",
    "RATE_LIMITED": "요청 한도 초과",
    "TIMEOUT": "응답 지연",
    "SERVER_ERROR": "판매처 서버 오류",
    "SCHEMA_CHANGED": "응답 형식 변경",
    "PAGINATION_STALLED": "페이지 이동 실패",
    "RECONCILIATION_FAILED": "건수 대사 실패",
    "READ_FAILED": "조회 실패",
    "CREDENTIAL_NOT_CONFIGURED": "자격증명 없음",
}

_SYNC_DETAIL = {
    "AUTH_FAILED": "자격증명을 다시 등록해야 주문이 들어옵니다. "
                   "그때까지 신규 주문은 장부에 쌓이지 않습니다.",
    "RATE_LIMITED": "다음 주기에 자동으로 다시 시도합니다.",
    "SCHEMA_CHANGED": "판매처 응답이 예상과 달라 주문을 만들지 않았습니다. "
                      "잘못된 주문을 만드는 것보다 멈추는 쪽이 안전합니다.",
    "RECONCILIATION_FAILED": "읽은 건수와 처리 건수가 맞지 않아 성공으로 "
                             "기록하지 않았습니다.",
}
