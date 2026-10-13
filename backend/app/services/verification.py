"""검증센터 — **원천과 파생을 맞대어 본다.**

무엇을 하는 모듈인가
------------------
화면이 보여주는 숫자를 믿어 달라고 하지 않는다. 원천자료(NextEngine 수집
기록·쿠팡 정산서·장부)와 Seller OS 가 만든 결과를 맞대어, **차이가 있는
것만** 사람에게 남긴다.

자기 자신과 비교하지 않는다
------------------------
이 모듈이 지켜야 할 첫 번째 규칙이다.

```
안 됨   Seller OS 계산값 → expected 로 복사 → 같은 값과 비교 → PASS
맞음    수집 기록의 fetched   vs  적재된 주문 수
        정산서 원본 금액      vs  우리가 재구성한 금액
        정책·장부 원 성분     vs  저장된 마진
```

세 상태를 절대 섞지 않는다
-----------------------
```
PASS          검증했고 맞다
FAIL          검증했고 틀리다
NOT_VERIFIED  검증할 근거가 없다   ← PASS 로 세지 않는다
```

`NOT_VERIFIED` 를 `PASS` 에 합치면 "환불 정산 표본이 없다" 가 "환불 정산이
맞다" 로 보인다. 그 순간 이 화면은 거짓말이 된다.

아무것도 고치지 않는다
-------------------
읽기 전용이다. 재계산·원가 수정·정산 재매칭을 하지 않는다. 근거를 보여주고,
고치는 것은 기존의 명시적 흐름이 한다.

새 예외 체계를 만들지 않는다
------------------------
운영 이슈는 `operation_issue` 가 이미 갖고 있다. 여기서는 그것을 읽어
합치고, 대사로만 알 수 있는 것을 더한다.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

#: 검증 결과 3상태. 이 셋을 서로 더하지 않는다.
PASS, FAIL, NOT_VERIFIED = "PASS", "FAIL", "NOT_VERIFIED"
#: 검증할 대상 자체가 없는 경우 (외부 API 미제공 등). 실패가 아니다.
NOT_AVAILABLE = "NOT_AVAILABLE"

#: 성분 신뢰도 — `margin._calculation_evidence` 와 같은 어휘를 쓴다.
ACTUAL, ESTIMATED, UNKNOWN = "ACTUAL", "ESTIMATED", "UNKNOWN"

#: 손익 근거로 쓸 수 있는 정산 매칭 상태. `settlement_components` 와 같은 값.
_POSTABLE = ("MATCHED_EXACT", "MATCHED_MANUAL")

#: 정산 레코드의 기간 축.
#:
#: `settled_at` 을 쓰지 않는다 — 실데이터 67행이 전부 NULL 이다(쿠팡 파일이
#: 실제 지급일을 주지 않는다). `settlement_period_start` 는 전부 채워져 있다.
_SETTLEMENT_DATE = "settlement_period_start"

#: 환불 행 판별.
#:
#: **전용 플래그가 없다.** M056 이 적재한 컬럼에는 환불 표시가 없고, 실데이터
#: 환불 표본도 0건이라 벤더가 환불을 어떤 모양으로 주는지 확인된 바 없다.
#: 금액 부호로 짐작하는 것이 지금 할 수 있는 최선이고, 그래서 이 판별에
#: 걸린 행은 검증 대상에서 **빼고 NOT_VERIFIED 로 센다** — 맞다고도
#: 틀리다고도 말하지 않는다.
_REFUND_PREDICATE = "(m.gross_revenue < 0 OR m.net_settlement < 0)"


def _period_clause(alias: str, column: str = "sale_date") -> str:
    """기간 필터. 안 주면 전체."""
    return (f" AND ({alias}.{column} >= :from_date OR CAST(:from_date AS date) IS NULL)"
            f" AND ({alias}.{column} <= :to_date OR CAST(:to_date AS date) IS NULL)")


def _period_params(period_from: date | None, period_to: date | None) -> dict[str, Any]:
    return {"from_date": period_from, "to_date": period_to}


# ══════════════════════════════════════════════════════════════════════
# 1. 데이터 최신성 — 정확성 이전에 "원천이 최신인가"
# ══════════════════════════════════════════════════════════════════════

def data_freshness(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """주문·클레임·정산·백업이 언제 마지막으로 들어왔나.

    **연결 안 됨과 0건을 구분한다.** 켜지 않은 수집을 "정상 0건" 으로 적으면
    사용자는 들어올 것이 없다고 믿는다.
    """
    from app.services import claim_sync as csync
    from app.services import order_sync as osync

    out: dict[str, Any] = {}
    for key, mod in (("orders", osync), ("claims", csync)):
        status = mod.status(db, org_id=org_id)
        accounts = status["accounts"]
        configured = [a for a in accounts if a.get("configured")]
        if not accounts:
            state = "NOT_CONNECTED"
        elif not status["enabled"] or not configured:
            state = "NOT_CONFIGURED"
        elif any(a.get("last_status") not in (None, "SUCCESS") for a in configured):
            state = "LAST_RUN_NOT_SUCCESS"
        elif all(a.get("last_success_at") is None for a in configured):
            state = "NEVER_RAN"
        else:
            state = "OK"
        out[key] = {
            "state": state,
            "enabled": status["enabled"],
            "accounts": [
                {
                    "account": a["display_name"],
                    "configured": a.get("configured"),
                    "blocked_by": a.get("blocked_by"),
                    "last_success_at": a.get("last_success_at"),
                    "last_attempt_at": a.get("last_attempt_at"),
                    "last_status": a.get("last_status"),
                    "last_error_code": a.get("last_error_code"),
                }
                for a in accounts
            ],
        }

    # 정산은 파일 import 다. 마지막으로 들어온 정산일이 어디까지인가.
    settlement = db.execute(text("""
        SELECT max(settlement_period_start) AS latest_date,
               count(*)             AS rows_total,
               count(DISTINCT import_batch_id) AS imports
          FROM marketplace_settlement_record
         WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org_id)}).mappings().one()
    out["settlement"] = {
        "state": "OK" if settlement["rows_total"] else "NOT_IMPORTED",
        "latest_settlement_date": settlement["latest_date"],
        "rows_total": int(settlement["rows_total"]),
        "imports": int(settlement["imports"]),
    }
    return out


# ══════════════════════════════════════════════════════════════════════
# 2. 수집 대사 — 읽은 것과 남긴 것이 맞는가
# ══════════════════════════════════════════════════════════════════════

#: 주문 수집의 대사 등식. `next_engine_order_sync.IngestResult.reconciles` 가
#: 계산 시점에 검사하는 것과 **같은 등식**을 저장된 값으로 다시 본다.
#:
#: 관문에서 걸러진 것(`protected`)과 변화 없음이 `skipped_count` 에 함께
#: 들어간다 — 그래서 이 식이 성립한다.
_ORDER_IDENTITY = "read = created + updated + skipped + conflict + rejected"

#: 클레임은 **식이 다르다.** `conflict_count` 가 배타적 분류가 아니라
#: "사람이 봐야 하는 건" 을 가로질러 세는 값이라(원 주문 미매칭 + 금액 미해결)
#: 위 식에 넣으면 항상 어긋난다. `claim_ingest.IngestResult.reconciles` 가
#: 쓰는 식이 진짜다.
_CLAIM_IDENTITY = "read = rejected + created + updated + unchanged"


def sync_reconciliation(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """수집 실행마다 "읽은 수 = 처리한 수" 가 맞는지.

    어긋난 실행은 라인 하나가 조용히 사라졌다는 뜻이다. 그 상태를 성공으로
    두면 화면과 장부가 갈라진다.

    M057 이전 기록은 판정하지 않는다
    ----------------------------
    클레임 수집이 `ORDERS` 로 기록되던 시절의 행이 남아 있다(docs/84 §7).
    그 행에 주문 등식을 대면 당연히 어긋나는데, 그건 **수집이 실패한 것이
    아니라 유형이 잘못 적힌 것**이다. FAIL 로 세면 고칠 수 없는 과거가
    영구히 빨간불로 남고, 사용자는 진짜 실패를 그 속에서 놓친다.

    경계는 지어내지 않는다 — **첫 `CLAIMS` 기록 시각**이 두 유형이 갈라진
    시점이고, 그 이전 `ORDERS` 행을 `NOT_VERIFIED` 로 뺀다.
    """
    split_at = db.execute(text("""
        SELECT min(started_at) FROM external_sync_run
         WHERE org_id = :o AND sync_type = 'CLAIMS'
    """), {"o": str(org_id)}).scalar()

    rows = db.execute(text("""
        SELECT id, sync_type, status, started_at, error_code,
               read_count, created_count, updated_count,
               skipped_count, conflict_count, error_count
          FROM external_sync_run
         WHERE org_id = :o AND read_count IS NOT NULL
         ORDER BY started_at DESC
         LIMIT 500
    """), {"o": str(org_id)}).mappings().all()

    checked = mismatched = 0
    failures: list[dict[str, Any]] = []
    legacy: list[dict[str, Any]] = []
    for r in rows:
        read = int(r["read_count"] or 0)
        created = int(r["created_count"] or 0)
        updated = int(r["updated_count"] or 0)
        skipped = int(r["skipped_count"] or 0)
        conflict = int(r["conflict_count"] or 0)
        rejected = int(r["error_count"] or 0)
        if r["sync_type"] == "CLAIMS":
            expected, identity = rejected + created + updated + skipped, _CLAIM_IDENTITY
        else:
            expected = created + updated + skipped + conflict + rejected
            identity = _ORDER_IDENTITY

        is_legacy = (
            r["sync_type"] != "CLAIMS"
            and split_at is not None
            and r["started_at"] is not None
            and r["started_at"] < split_at
        )
        if expected != read and is_legacy:
            legacy.append({
                "sync_run_id": str(r["id"]), "started_at": r["started_at"],
                "read": read, "accounted": expected,
            })
            continue

        checked += 1
        if expected != read:
            mismatched += 1
            failures.append({
                "sync_run_id": str(r["id"]), "sync_type": r["sync_type"],
                "started_at": r["started_at"], "status": r["status"],
                "identity": identity, "read": read, "accounted": expected,
                "difference": read - expected,
            })

    # 실패로 끝난 실행은 대사 이전의 문제다. 따로 센다.
    failed_runs = [
        {"sync_run_id": str(r["id"]), "sync_type": r["sync_type"],
         "started_at": r["started_at"], "error_code": r["error_code"]}
        for r in rows if r["status"] not in ("SUCCESS",)
    ]

    if mismatched or failed_runs:
        status = FAIL
    elif not checked:
        status = NOT_VERIFIED
    else:
        status = PASS
    return {
        "runs_checked": checked,
        "reconciliation_failures": mismatched,
        "failures": failures[:20],
        "runs_not_succeeded": len(failed_runs),
        "not_succeeded": failed_runs[:20],
        # 판정하지 않은 과거 기록. PASS 에도 FAIL 에도 넣지 않는다.
        "legacy_unclassified_runs": len(legacy),
        "legacy_reason": (
            "M057 이전에는 클레임 수집이 ORDERS 로 기록됐다 — 유형이 잘못 적힌 "
            "것이지 수집이 실패한 것이 아니다(docs/84 §7). 재분류 불가."
        ) if legacy else None,
        "type_split_at": split_at,
        "status": status,
    }


# ══════════════════════════════════════════════════════════════════════
# 3. 정산 대사 — 행 수와 금액
# ══════════════════════════════════════════════════════════════════════

def settlement_reconciliation(
    db: Session, *, org_id: UUID,
    period_from: date | None = None, period_to: date | None = None,
) -> dict[str, Any]:
    """정산서 행이 전부 어떤 상태로든 분류돼 있는가, 그리고 금액이 맞는가.

    행 수 대사
    ---------
    적재된 행은 반드시 다섯 상태 중 하나다(M055 CHECK). 어디에도 안 속한
    행이 있으면 분류 자체가 깨진 것이다.

    금액 대사
    --------
    확정된 관계(docs/83 D1~D3)를 **정산서 원본 숫자만으로** 다시 세운다.

    ```
    순매출 = 상품매출 + 배송비매출 - 판매자쿠폰
    순매출 - 실제 수수료 = 벤더 정산금액
    ```

    우리 계산 결과를 쓰지 않는다 — 정산서가 준 값끼리의 관계다. 그래서
    이것은 자기검증이 아니다.

    환불 행은 제외한다
    ---------------
    환불 정산 실표본이 아직 0건이라 이 식이 환불에도 성립하는지 **모른다.**
    모르는 것을 PASS 로 세지 않는다 — `NOT_VERIFIED_REFUND` 로 뺀다.
    """
    params = {"o": str(org_id), **_period_params(period_from, period_to)}

    by_status = db.execute(text("""
        SELECT matching_status, count(*) AS n
          FROM marketplace_settlement_record m
         WHERE m.org_id = :o AND m.deleted_at IS NULL
    """ + _period_clause("m", _SETTLEMENT_DATE) + """
         GROUP BY 1
    """), params).mappings().all()
    counts = {r["matching_status"]: int(r["n"]) for r in by_status}
    known = ("UNMATCHED", "AMBIGUOUS", "MATCHED_EXACT", "MATCHED_MANUAL", "EXCLUDED")
    persisted = sum(counts.values())
    classified = sum(counts.get(k, 0) for k in known)

    # 주문 단위 금액 대사. 행 단위로 보면 배송료 행의 매출이 0 이라 늘 어긋난다 —
    # 쿠팡은 주문 하나를 상품·기본배송료·추가배송료 세 행으로 준다.
    orders = db.execute(text("""
        SELECT m.matched_sales_order_id AS sales_order_id,
               SUM(m.gross_revenue) FILTER (WHERE m.row_type = 'PRODUCT_SALE') AS product_revenue,
               SUM(m.gross_revenue) FILTER (WHERE m.row_type <> 'PRODUCT_SALE') AS delivery_revenue,
               SUM(m.other_deductions) AS seller_coupon,
               SUM(m.marketplace_fee)  AS marketplace_fee,
               SUM(m.net_settlement)   AS vendor_net,
               bool_or(""" + _REFUND_PREDICATE + """) AS has_refund
          FROM marketplace_settlement_record m
         WHERE m.org_id = :o AND m.deleted_at IS NULL
           AND m.matched_sales_order_id IS NOT NULL
           AND m.matching_status = ANY(:st)
    """ + _period_clause("m", _SETTLEMENT_DATE) + """
         GROUP BY 1
    """), {**params, "st": list(_POSTABLE)}).mappings().all()

    verified = 0
    amount_failures: list[dict[str, Any]] = []
    refund_excluded = 0
    for r in orders:
        if r["has_refund"]:
            # 환불 표본이 없어 이 식의 성립 여부를 모른다.
            refund_excluded += 1
            continue
        product = int(r["product_revenue"] or 0)
        delivery = int(r["delivery_revenue"] or 0)
        coupon = int(r["seller_coupon"] or 0)
        fee = int(r["marketplace_fee"] or 0)
        vendor = int(r["vendor_net"] or 0)
        net_revenue = product + delivery - coupon
        # **원 단위로 정확히** 본다. 오차 허용치로 덮지 않는다.
        difference = net_revenue - fee - vendor
        if difference == 0:
            verified += 1
            continue
        amount_failures.append({
            "sales_order_id": str(r["sales_order_id"]),
            "product_revenue": product, "delivery_revenue": delivery,
            "seller_coupon": coupon, "marketplace_fee": fee,
            "reconstructed_net": net_revenue - fee,
            "vendor_net_settlement": vendor, "difference": difference,
        })

    status = PASS
    if amount_failures or persisted != classified:
        status = FAIL
    elif not orders:
        status = NOT_VERIFIED

    return {
        "rows_persisted": persisted,
        "rows_classified": classified,
        "unclassified": persisted - classified,
        "by_status": counts,
        "orders_checked": len(orders),
        "amount_verified": verified,
        "amount_failures": amount_failures[:20],
        "amount_failure_count": len(amount_failures),
        "refund_excluded": refund_excluded,
        "refund_formula": NOT_VERIFIED,
        "status": status,
        "formula": "net_revenue = product + delivery - seller_coupon; "
                   "net_revenue - marketplace_fee = vendor_net_settlement",
    }


# ══════════════════════════════════════════════════════════════════════
# 4. 마진 신뢰도 — 무엇이 실측이고 무엇이 추정인가
# ══════════════════════════════════════════════════════════════════════

def margin_confidence(
    db: Session, *, org_id: UUID,
    period_from: date | None = None, period_to: date | None = None,
) -> dict[str, Any]:
    """주문마다 마진 근거가 실측인지 추정인지 모르는지.

    **분류가 서로 겹치지 않는다.** 한 주문은 정확히 한 칸에만 들어간다 —
    겹치면 합계가 주문 수를 넘고 화면이 이상해진다.

    ```
    NO_MARGIN_ROW  아직 계산되지 않았다
    ACTUAL         정산 실측 기준
    ESTIMATED      장부 + 정책 추정
    UNKNOWN        원가나 수수료를 모른 채 0 으로 계산됐다
    ```
    """
    params = {"o": str(org_id), **_period_params(period_from, period_to)}
    rows = db.execute(text("""
        SELECT so.id AS sales_order_id,
               pma.id AS margin_id,
               pma.calculation_snapshot_json AS snap,
               pma.actual_revenue, pma.actual_cost, pma.actual_marketplace_fee,
               so.purchase_cost,
               COALESCE(pv.cost_amount, p.cost_amount) AS master_cost
          FROM sales_order so
          LEFT JOIN product_margin_actual pma
                 ON pma.sales_order_id = so.id AND pma.deleted_at IS NULL
          LEFT JOIN product_variant pv ON pv.id = so.product_variant_id
          LEFT JOIN product p ON p.id = so.product_id
         WHERE so.org_id = :o AND so.deleted_at IS NULL
    """ + _period_clause("so")), params).mappings().all()

    buckets = {"NO_MARGIN_ROW": 0, ACTUAL: 0, ESTIMATED: 0, UNKNOWN: 0}
    no_evidence = 0
    unknown_orders: list[str] = []
    for r in rows:
        if r["margin_id"] is None:
            buckets["NO_MARGIN_ROW"] += 1
            continue
        snap = r["snap"] or {}
        if not snap:
            # 근거가 저장돼 있지 않다. 값은 있지만 왜 그런지 모른다.
            no_evidence += 1
        confidences = _component_confidences(snap, r)
        if UNKNOWN in confidences:
            buckets[UNKNOWN] += 1
            unknown_orders.append(str(r["sales_order_id"]))
        elif snap.get("basis") == "SETTLEMENT_ACTUAL":
            buckets[ACTUAL] += 1
        else:
            buckets[ESTIMATED] += 1

    total = len(rows)
    return {
        "orders_total": total,
        "no_margin_row": buckets["NO_MARGIN_ROW"],
        "actual": buckets[ACTUAL],
        "estimated": buckets[ESTIMATED],
        "unknown": buckets[UNKNOWN],
        "unknown_sample": unknown_orders[:20],
        # 근거 없이 값만 있는 행. 자동으로 다시 계산하지 않는다.
        "without_calculation_evidence": no_evidence,
        # 합이 전체와 같아야 한다. 다르면 분류가 겹친 것이다.
        "buckets_sum_matches_total": (
            buckets["NO_MARGIN_ROW"] + buckets[ACTUAL]
            + buckets[ESTIMATED] + buckets[UNKNOWN] == total),
    }


#: 마진 신뢰도를 가르는 성분.
#:
#: 반품비·풀필먼트·결제수수료·VAT 는 **모든 주문에서 똑같이 UNKNOWN** 이다
#: (아직 원천이 없다). 그것까지 세면 전 주문이 UNKNOWN 으로 뭉개져 신뢰도
#: 구분이 사라진다. 그 넷은 evidence gate 로 따로 보고한다(`evidence_gates`).
CORE_COMPONENTS = ("revenue", "cost", "marketplace_fee")


def _component_confidences(snap: dict[str, Any], row: Any) -> set[str]:
    """이 마진에 쓰인 핵심 성분들의 신뢰도 집합.

    근거가 저장돼 있으면 그것을 읽는다. 없으면(근거를 남기기 전에 계산된
    과거 행) **현재 원천으로 다시 판정한다** — 그때 무엇을 썼는지는 알 수
    없으므로 재구성이고, 호출부가 그 사실을 함께 보고한다.
    """
    components = snap.get("components")
    if isinstance(components, dict):
        out = set()
        for name in CORE_COMPONENTS:
            c = components.get(name)
            if isinstance(c, dict) and c.get("confidence") in (ACTUAL, ESTIMATED, UNKNOWN):
                out.add(c["confidence"])
        if out:
            return out

    # 근거 없음 — 지금 원천에서 원가 근거만이라도 판정한다.
    if row["purchase_cost"] is not None:
        return {ACTUAL}
    if row["master_cost"] is not None:
        return {ESTIMATED}
    return {UNKNOWN}


# ══════════════════════════════════════════════════════════════════════
# 5. 주문 대사 · coverage
# ══════════════════════════════════════════════════════════════════════

def order_reconciliation(
    db: Session, *, org_id: UUID,
    period_from: date | None = None, period_to: date | None = None,
) -> dict[str, Any]:
    """수집 원천이 만든 주문 수 vs 장부에 있는 주문 수.

    **매 화면마다 벤더 API 를 다시 부르지 않는다.** 수집 실행 기록이 이미
    "몇 건을 읽어 몇 건을 만들었나" 를 갖고 있다 — 그것과 장부를 맞댄다.
    """
    params = {"o": str(org_id), **_period_params(period_from, period_to)}
    ledger = db.execute(text("""
        SELECT count(*) AS n,
               count(*) FILTER (WHERE so.import_source = 'NEXTENGINE_API') AS from_api
          FROM sales_order so
         WHERE so.org_id = :o AND so.deleted_at IS NULL
    """ + _period_clause("so")), params).mappings().one()

    # 수집이 만든 누적 신규 건수.
    #
    # 기간 필터를 걸지 않는다 — 실행 시각과 매출일은 다른 축이고, 섞으면
    # 뺄셈이 무의미해진다.
    #
    # **M057 이전 기록은 빼고 센다.** 그 시절 `ORDERS` 행에는 클레임 실행이
    # 섞여 있어(docs/84 §7) `created_count` 가 주문 신규 건수가 아니다.
    # 그대로 더하면 "장부보다 12건 많다" 같은 없는 차이가 만들어진다.
    split_at = db.execute(text("""
        SELECT min(started_at) FROM external_sync_run
         WHERE org_id = :o AND sync_type = 'CLAIMS'
    """), {"o": str(org_id)}).scalar()

    created = db.execute(text("""
        SELECT COALESCE(SUM(created_count), 0)
          FROM external_sync_run
         WHERE org_id = :o AND sync_type = 'ORDERS' AND status = 'SUCCESS'
           AND (CAST(:split AS timestamptz) IS NULL OR started_at >= :split)
    """), {"o": str(org_id), "split": split_at}).scalar_one()

    comparable = split_at is not None
    return {
        "ledger_orders": int(ledger["n"]),
        "ledger_orders_from_api": int(ledger["from_api"]),
        "sync_created_since_split": int(created),
        "type_split_at": split_at,
        # 두 숫자를 그냥 빼서 "차이" 라고 부르지 않는다. 엑셀로 들어온 초기
        # 장부가 섞여 있어 같을 이유가 없다 — API 유입분만, 그것도 유형이
        # 갈라진 뒤의 기록만 비교한다.
        "status": NOT_VERIFIED if not comparable else PASS,
        "note": (
            "엑셀 초기 장부는 수집 대상이 아니라 API 유입분만 비교한다. "
            "M057 이전 실행 기록은 유형이 섞여 있어 비교에서 제외했다."
        ),
    }


def coverage(
    db: Session, *, org_id: UUID,
    period_from: date | None = None, period_to: date | None = None,
) -> dict[str, Any]:
    """무엇이 얼마나 검증됐나. **분모가 다른 비율을 섞지 않는다.**"""
    params = {"o": str(org_id), **_period_params(period_from, period_to)}
    orders = db.execute(text("""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM product_margin_actual pma
                    WHERE pma.sales_order_id = so.id AND pma.deleted_at IS NULL
               )) AS with_margin,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM marketplace_settlement_record m
                    WHERE m.matched_sales_order_id = so.id AND m.deleted_at IS NULL
                      AND m.matching_status = ANY(:st)
               )) AS with_settlement
          FROM sales_order so
         WHERE so.org_id = :o AND so.deleted_at IS NULL
    """ + _period_clause("so")), {**params, "st": list(_POSTABLE)}).mappings().one()

    rows = db.execute(text("""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE matching_status = ANY(:st)) AS matched
          FROM marketplace_settlement_record m
         WHERE m.org_id = :o AND m.deleted_at IS NULL
    """ + _period_clause("m", _SETTLEMENT_DATE)),
        {**params, "st": list(_POSTABLE)}).mappings().one()

    return {
        # 분모: 이 기간의 주문
        "margin_calculated": {"n": int(orders["with_margin"]), "of": int(orders["total"])},
        "settlement_linked": {"n": int(orders["with_settlement"]), "of": int(orders["total"])},
        # 분모: 이 기간의 정산서 행 — **위와 다른 분모다**
        "settlement_rows_matched": {"n": int(rows["matched"]), "of": int(rows["total"])},
    }


def revenue_split(
    db: Session, *, org_id: UUID,
    period_from: date | None = None, period_to: date | None = None,
) -> dict[str, Any]:
    """매출을 **실정산 확인분과 추정분으로 나눈다** (§11).

    왜 나누나
    -------
    "장부 매출과 정산 매출이 다르다" 를 오류로 보면 안 된다. 최근 주문은
    정산서가 아직 안 나왔을 뿐이고, 그건 정상이다. 차이가 아니라 **coverage**
    로 보여줘야 사용자가 무엇을 기다리면 되는지 안다.

    ```
    장부 매출        판매장부에 적힌 것 전부
    실정산 확인분    정산서가 붙어 금액이 확인된 몫
    추정분          아직 정산서가 없어 장부 금액을 그대로 쓴 몫
    ```

    둘을 더하면 장부 매출이 된다 — 겹치지 않는다.
    """
    params = {"o": str(org_id), **_period_params(period_from, period_to)}
    row = db.execute(text("""
        SELECT count(*) AS orders,
               COALESCE(SUM(so.sale_price), 0) AS ledger_revenue,
               count(*) FILTER (WHERE s.sid IS NOT NULL) AS settled_orders,
               COALESCE(SUM(so.sale_price) FILTER (WHERE s.sid IS NOT NULL), 0)
                 AS settled_ledger_revenue,
               COALESCE(SUM(s.net_revenue) FILTER (WHERE s.sid IS NOT NULL), 0)
                 AS settlement_net_revenue
          FROM sales_order so
          LEFT JOIN (
              SELECT m.matched_sales_order_id AS sid,
                     SUM(m.gross_revenue) - SUM(m.other_deductions) AS net_revenue
                FROM marketplace_settlement_record m
               WHERE m.org_id = :o AND m.deleted_at IS NULL
                 AND m.matching_status = ANY(:st)
               GROUP BY 1
          ) s ON s.sid = so.id
         WHERE so.org_id = :o AND so.deleted_at IS NULL
    """ + _period_clause("so")), {**params, "st": list(_POSTABLE)}).mappings().one()

    ledger = int(row["ledger_revenue"])
    settled_ledger = int(row["settled_ledger_revenue"])
    return {
        "orders": int(row["orders"]),
        "ledger_revenue": ledger,
        # 정산이 붙은 주문의 장부 금액 · 그리고 정산서가 말하는 금액
        "settled_orders": int(row["settled_orders"]),
        "settled_ledger_revenue": settled_ledger,
        "settlement_net_revenue": int(row["settlement_net_revenue"]),
        # 정산서와 장부가 다른 만큼. **오류가 아니다** — 쿠폰·배송비 때문에
        # 다를 수 있고, 그 차이를 D1~D4 가 어떻게 다루는지는 이미 정해져 있다.
        "settled_difference": int(row["settlement_net_revenue"]) - settled_ledger,
        # 아직 정산서가 없어 장부 금액을 그대로 쓰는 몫.
        "estimated_orders": int(row["orders"]) - int(row["settled_orders"]),
        "estimated_revenue": ledger - settled_ledger,
        "note": "정산서가 아직 안 온 주문이 있는 것은 정상이다 — 차이가 아니라 coverage 다",
    }


def inventory_integrity(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """재고 (§35).

    외부 대사는 **불가능하다**
    ----------------------
    판매처가 재고 API 를 주지 않는다. 없는 것을 "차이 0" 으로 적으면 대사한
    척이 된다 — `NOT_AVAILABLE` 이다.

    "원장 합계 vs 잔량" 도 검사하지 않는다
    --------------------------------
    이 저장소에 저장된 잔량 칸이 **없다.** 잔량은 `SUM(quantity_delta)` 로
    그때그때 구한다(`inventory._BALANCE_SELECT`). 즉 둘이 어긋날 구조가
    아니고, 그걸 검사해 PASS 라고 적는 것은 아무것도 확인하지 않고 통과
    도장을 찍는 것과 같다.

    대신 **실제로 깨질 수 있는 것**을 본다.

    ```
    음수 잔량        받은 것보다 많이 나갔다 — 입고 누락이거나 중복 출고다
    고아 출처        원본 행이 사라졌는데 재고 이동만 남았다
    중복 적재        같은 출처가 두 번 들어왔다 (import 멱등 위반)
    ```
    """
    def one(sql: str) -> int:
        return int(db.execute(text(sql), {"o": str(org_id)}).scalar() or 0)

    checks = {
        "negative_on_hand": one("""
            SELECT count(*) FROM (
              SELECT product_id, product_variant_id
                FROM inventory_transaction WHERE org_id = :o
               GROUP BY 1,2 HAVING SUM(quantity_delta) < 0) d
        """),
        "orphan_product_reference": one("""
            SELECT count(*) FROM inventory_transaction t
             WHERE t.org_id = :o
               AND NOT EXISTS (SELECT 1 FROM product p
                                WHERE p.id = t.product_id AND p.deleted_at IS NULL)
        """),
        "duplicate_source_rows": one("""
            SELECT count(*) FROM (
              SELECT source_table, source_id
                FROM inventory_transaction
               WHERE org_id = :o AND source_id IS NOT NULL
               GROUP BY 1,2 HAVING count(*) > 1) d
        """),
    }
    return {
        "external_reconciliation": NOT_AVAILABLE,
        "external_reason": "판매처가 재고 API 를 제공하지 않아 외부 대사가 불가능하다",
        "balance_is_derived": True,
        "balance_note": "잔량은 저장하지 않고 원장 합계로 구한다 — 어긋날 구조가 아니다",
        "checks": checks,
        "status": PASS if not any(checks.values()) else FAIL,
    }


def settlement_margin_coverage(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """정산이 붙은 주문이 전부 실측 기준으로 다시 계산됐는가 (§37).

    붙어 있는데 추정 마진 그대로면, 화면은 실측이라고 믿게 하면서 숫자는
    정책 추정이다. 이 어긋남은 재계산을 안 돌렸을 때 생긴다.
    """
    row = db.execute(text("""
        WITH matched AS (
            SELECT DISTINCT m.matched_sales_order_id AS sid
              FROM marketplace_settlement_record m
             WHERE m.org_id = :o AND m.deleted_at IS NULL
               AND m.matched_sales_order_id IS NOT NULL
               AND m.matching_status = ANY(:st)
        )
        SELECT count(*) AS matched_orders,
               count(*) FILTER (
                   WHERE pma.calculation_snapshot_json->>'basis' = 'SETTLEMENT_ACTUAL'
               ) AS on_actual_basis
          FROM matched
          LEFT JOIN product_margin_actual pma
                 ON pma.sales_order_id = matched.sid AND pma.deleted_at IS NULL
    """), {"o": str(org_id), "st": list(_POSTABLE)}).mappings().one()

    matched = int(row["matched_orders"])
    on_actual = int(row["on_actual_basis"])
    return {
        "settlement_matched_orders": matched,
        "margin_on_actual_basis": on_actual,
        "difference": matched - on_actual,
        "status": PASS if matched == on_actual else (FAIL if matched else NOT_VERIFIED),
    }


# ══════════════════════════════════════════════════════════════════════
# 6. 무결성 — 중복·고아·조직 경계
# ══════════════════════════════════════════════════════════════════════

def integrity(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """DB 제약이 있어도 **실제로 0 인지 눈으로 확인한다.**

    제약이 있다는 사실과 데이터가 깨끗하다는 사실은 다르다 — 제약은 나중에
    추가되기도 하고, 그 전에 들어온 행은 남는다.
    """
    def one(sql: str, **extra: Any) -> int:
        return int(db.execute(text(sql), {"o": str(org_id), **extra}).scalar() or 0)

    checks = {
        "duplicate_orders": one("""
            SELECT count(*) FROM (
              SELECT marketplace, marketplace_order_no, line_no
                FROM sales_order WHERE org_id = :o AND deleted_at IS NULL
               GROUP BY 1,2,3 HAVING count(*) > 1) d
        """),
        "duplicate_claims": one("""
            SELECT count(*) FROM (
              SELECT external_account_id, external_claim_id
                FROM sales_claim
               WHERE org_id = :o AND deleted_at IS NULL AND external_claim_id IS NOT NULL
               GROUP BY 1,2 HAVING count(*) > 1) d
        """),
        # 적재기가 선언한 식별자로 센다.
        #
        # `line_reference` 로 세면 안 된다 — 배송료 행의 값은 `<기본배송료>`
        # 같은 **라벨**이라 한 파일에 여러 번 나오는 것이 정상이다. 그걸
        # 중복으로 세면 멀쩡한 정산서가 매번 FAIL 로 뜨고, 그 순간 이 화면을
        # 아무도 안 믿게 된다(실측에서 8건이 그렇게 잡혔다).
        "duplicate_settlement_rows": one("""
            SELECT count(*) FROM (
              SELECT import_idempotency_key
                FROM marketplace_settlement_record
               WHERE org_id = :o AND deleted_at IS NULL
                 AND import_idempotency_key IS NOT NULL
               GROUP BY 1 HAVING count(*) > 1) d
        """),
        "duplicate_settlement_source_rows": one("""
            SELECT count(*) FROM (
              SELECT source_row_fingerprint
                FROM marketplace_settlement_record
               WHERE org_id = :o AND deleted_at IS NULL
                 AND source_row_fingerprint IS NOT NULL
               GROUP BY 1 HAVING count(*) > 1) d
        """),
        # 매칭이 다른 조직의 주문을 가리키면 조직 격리가 깨진 것이다.
        "cross_org_settlement_match": one("""
            SELECT count(*) FROM marketplace_settlement_record m
              JOIN sales_order so ON so.id = m.matched_sales_order_id
             WHERE m.org_id = :o AND m.deleted_at IS NULL AND so.org_id <> m.org_id
        """),
        "cross_org_margin_rows": one("""
            SELECT count(*) FROM product_margin_actual pma
              JOIN sales_order so ON so.id = pma.sales_order_id
             WHERE pma.org_id = :o AND pma.deleted_at IS NULL AND so.org_id <> pma.org_id
        """),
        # 원 주문이 사라졌는데 매칭만 남은 행.
        "orphan_settlement_match": one("""
            SELECT count(*) FROM marketplace_settlement_record m
             WHERE m.org_id = :o AND m.deleted_at IS NULL
               AND m.matched_sales_order_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM sales_order so
                                WHERE so.id = m.matched_sales_order_id
                                  AND so.deleted_at IS NULL)
        """),
        "orphan_margin_rows": one("""
            SELECT count(*) FROM product_margin_actual pma
             WHERE pma.org_id = :o AND pma.deleted_at IS NULL
               AND NOT EXISTS (SELECT 1 FROM sales_order so
                                WHERE so.id = pma.sales_order_id AND so.deleted_at IS NULL)
        """),
    }
    return {"checks": checks, "status": PASS if not any(checks.values()) else FAIL}


def unknown_treated_as_zero(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """모르는 값이 0 으로 계상돼 있는 곳 (§44).

    소스코드를 훑는 것이 아니라 **저장된 데이터**에서 위험 상태를 센다.
    자동으로 고치지 않는다 — 근거를 보여주고 사람이 정한다.
    """
    def one(sql: str) -> int:
        return int(db.execute(text(sql), {"o": str(org_id)}).scalar() or 0)

    unknown_cost = one("""
        SELECT count(*) FROM sales_order so
          LEFT JOIN product_variant pv ON pv.id = so.product_variant_id
          LEFT JOIN product p ON p.id = so.product_id
          JOIN product_margin_actual pma
            ON pma.sales_order_id = so.id AND pma.deleted_at IS NULL
         WHERE so.org_id = :o AND so.deleted_at IS NULL
           AND so.purchase_cost IS NULL
           AND COALESCE(pv.cost_amount, p.cost_amount) IS NULL
           AND pma.actual_cost = 0
    """)
    # 매출이 있는데 수수료가 0 이고 정산 실측도 아니면, 정책을 못 찾았을 수 있다.
    unknown_fee = one("""
        SELECT count(*) FROM product_margin_actual pma
         WHERE pma.org_id = :o AND pma.deleted_at IS NULL
           AND pma.actual_revenue > 0 AND pma.actual_marketplace_fee = 0
           AND COALESCE(pma.calculation_snapshot_json->>'basis', '') <> 'SETTLEMENT_ACTUAL'
    """)
    # 클레임은 있는데 환불 정산 근거가 없는 주문 — 마진이 확정이 아니다.
    unverified_refund = one("""
        SELECT count(DISTINCT c.sales_order_id) FROM sales_claim c
         WHERE c.org_id = :o AND c.deleted_at IS NULL
           AND c.sales_order_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM marketplace_settlement_record m
                            WHERE m.matched_sales_order_id = c.sales_order_id
                              AND m.deleted_at IS NULL
                              AND """ + _REFUND_PREDICATE + """)
    """)
    checks = {
        "unknown_cost_as_zero": unknown_cost,
        "unknown_fee_as_zero": unknown_fee,
        "claim_without_refund_settlement": unverified_refund,
    }
    return {
        "checks": checks,
        # 이것들은 **실패가 아니라 미검증**이다. 자동 수정 대상이 아니고,
        # 원천이 생겨야 해소된다.
        "status": PASS if not any(checks.values()) else NOT_VERIFIED,
    }


# ══════════════════════════════════════════════════════════════════════
# 7. Evidence gate — 아직 검증할 수 없는 영역을 숨기지 않는다
# ══════════════════════════════════════════════════════════════════════

def evidence_gates(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """docs/84 §6 의 게이트를 검증센터에서도 그대로 보여준다.

    화면에서 이걸 빼면 "전부 정상" 으로 보인다. 그건 사실이 아니다.
    """
    refund_samples = int(db.execute(text("""
        SELECT count(*) FROM marketplace_settlement_record
         WHERE org_id = :o AND deleted_at IS NULL AND (gross_revenue < 0 OR net_settlement < 0)
    """), {"o": str(org_id)}).scalar() or 0)

    return {
        "refund_financial": {
            "state": PASS if refund_samples else NOT_VERIFIED,
            "reason": ("환불 정산 실표본이 없어 환불 손익 식을 검증하지 못했다"
                       if not refund_samples else None),
            "samples": refund_samples,
        },
        "cash_receipt": {
            "state": NOT_VERIFIED,
            "reason": "입금 증거가 없어 정산예정 → 현금 전환을 검증하지 못했다",
        },
        "external_inventory": {
            "state": NOT_AVAILABLE,
            "reason": "판매처가 재고 API 를 제공하지 않아 외부 대사 자체가 불가능하다",
        },
        "adjustment_posting": {
            "state": NOT_VERIFIED,
            "reason": "조정 3종의 순이익 반영은 아직 결정되지 않았다",
        },
    }


def month_close_reconciliation(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """확정 snapshot vs 지금 원천 (§34).

    **차이가 있어도 확정 결과를 고치지 않는다.** 확정은 그 시점의 사실이고,
    지금 원천이 달라졌다는 것은 별개의 사실이다. 둘 다 보여준다.
    """
    rows = db.execute(text("""
        SELECT year_month, generation, net_profit, content_hash, created_at
          FROM monthly_close_snapshot
         WHERE org_id = :o AND superseded_at IS NULL
         ORDER BY year_month DESC
    """), {"o": str(org_id)}).mappings().all()
    if not rows:
        return {"closed_months": 0, "status": NOT_VERIFIED,
                "reason": "확정된 달이 없어 비교할 snapshot 이 없다", "months": []}
    return {
        "closed_months": len(rows),
        "status": NOT_VERIFIED,
        "reason": "확정 결과는 불변이다. drift 판정은 마감 화면의 drift 조회를 쓴다",
        "months": [
            {"year_month": r["year_month"], "generation": r["generation"],
             "net_profit": r["net_profit"], "immutable": True}
            for r in rows
        ],
    }


# ══════════════════════════════════════════════════════════════════════
# 8. 주문 하나의 계산 추적 — "이 숫자가 왜 이런가"
# ══════════════════════════════════════════════════════════════════════

#: 근거의 출처. 이 구분이 사용자에게 중요하다.
FROM_SNAPSHOT = "SNAPSHOT"        # 계산 시점에 박제된 근거 — 그때 실제로 쓴 값
RECONSTRUCTED = "RECONSTRUCTED"   # 지금 원천에서 되짚은 값 — 그동안 원천이 바뀌었을 수 있다


def order_trace(db: Session, *, org_id: UUID, sales_order_id: UUID) -> dict[str, Any]:
    """주문 하나의 매출·수수료·원가·마진이 각각 어디서 왔는지.

    **개인정보를 꺼내지 않는다.** 검증에 필요한 것은 주문 식별자와 금액이지
    수취인·주소·연락처가 아니다.

    근거의 두 종류
    ------------
    ```
    SNAPSHOT       계산할 때 박제해 둔 것. 그때 실제로 쓴 값이다
    RECONSTRUCTED  지금 원천에서 되짚은 것. 원천이 그동안 바뀌었으면 다르다
    ```

    둘을 같은 것처럼 보여주면, 원가를 고친 뒤에도 "이 마진은 이 원가로
    계산됐다" 는 틀린 설명을 하게 된다.

    화면용 계산식을 새로 만들지 않는다
    -----------------------------
    산식 문자열은 `margin.MARGIN_FORMULA` 하나뿐이다. 여기서 다시 쓰면 코드가
    바뀌어도 화면은 옛 식을 보여준다.
    """
    from app.services import margin as margin_svc
    from app.services import settlement_components as sc

    row = db.execute(text("""
        SELECT so.id, so.business_key, so.sale_date, so.marketplace,
               so.marketplace_order_no, so.line_no, so.quantity, so.sale_price,
               so.purchase_cost, so.shipping_fee, so.ad_cost, so.import_source,
               so.profit_status, so.product_snapshot_name, so.product_id,
               pma.id AS margin_id, pma.actual_revenue, pma.actual_cost,
               pma.actual_shipping_cost, pma.actual_marketplace_fee,
               pma.actual_payment_fee, pma.actual_ad_cost,
               pma.actual_fulfillment_fee, pma.actual_return_cost, pma.actual_vat,
               pma.actual_margin_amount, pma.actual_margin_rate,
               pma.calculation_snapshot_json AS snap,
               pma.updated_at AS margin_updated_at
          FROM sales_order so
          LEFT JOIN product_margin_actual pma
                 ON pma.sales_order_id = so.id AND pma.deleted_at IS NULL
         WHERE so.id = :id AND so.org_id = :o AND so.deleted_at IS NULL
    """), {"id": str(sales_order_id), "o": str(org_id)}).mappings().first()
    if row is None:
        from app.core.exceptions import NotFoundError
        raise NotFoundError("판매 건을 찾을 수 없습니다.")

    components = sc.load_for_order(db, org_id=org_id, sales_order_id=sales_order_id)
    snap = row["snap"] or {}
    stored = isinstance(snap.get("components"), dict)

    lineage = ["판매처 주문", "sales_order"]
    if components:
        lineage += ["판매처 정산서", "marketplace_settlement_record"]
    lineage += ["product_margin_actual"]

    trace: dict[str, Any] = {
        "order": {
            # 사업용 식별자만. 구매자 정보는 검증에 필요 없다.
            "sales_order_id": str(row["id"]),
            "business_key": row["business_key"],
            "marketplace": row["marketplace"],
            "marketplace_order_no": row["marketplace_order_no"],
            "line_no": row["line_no"],
            "sale_date": row["sale_date"],
            "quantity": row["quantity"],
            "product_name": row["product_snapshot_name"],
            "import_source": row["import_source"],
            "profit_status": row["profit_status"],
        },
        "evidence_kind": FROM_SNAPSHOT if stored else RECONSTRUCTED,
        "formula": margin_svc.MARGIN_FORMULA,
        "margin_calculated": row["margin_id"] is not None,
        "margin_updated_at": row["margin_updated_at"],
        "lineage": lineage,
        "settlement_linked": components is not None,
    }

    if row["margin_id"] is None:
        trace["status"] = NOT_VERIFIED
        trace["reason"] = "이 주문은 아직 실 마진이 계산되지 않았다"
        trace["components"] = {}
        return trace

    if stored:
        trace["components"] = snap["components"]
        trace["warnings"] = snap.get("warnings", [])
    else:
        # 근거가 없다. 지금 원천으로 되짚되 **그 사실을 숨기지 않는다.**
        trace["components"] = _reconstruct_components(row, components)
        trace["warnings"] = [
            "이 주문에는 계산 근거가 박제돼 있지 않다 — 아래 출처는 지금 "
            "원천에서 되짚은 것이며, 계산 당시와 다를 수 있다"
        ]

    if components:
        trace["settlement"] = components.as_snapshot()

    stored_total = (
        int(row["actual_revenue"]) - int(row["actual_cost"])
        - int(row["actual_shipping_cost"]) - int(row["actual_marketplace_fee"])
        - int(row["actual_ad_cost"]) - int(row["actual_return_cost"])
        - int(row["actual_fulfillment_fee"]) - int(row["actual_payment_fee"])
        - int(row["actual_vat"])
    )
    # 저장된 성분들을 산식에 다시 넣어 저장된 마진과 맞는지 본다. 같은 행
    # 안에서의 검산이라 대단한 검증은 아니지만, 성분과 합계가 어긋난 행은
    # 여기서 반드시 걸린다.
    trace["stored_amounts"] = {
        "revenue": int(row["actual_revenue"]),
        "cost": int(row["actual_cost"]),
        "shipping_cost": int(row["actual_shipping_cost"]),
        "marketplace_fee": int(row["actual_marketplace_fee"]),
        "ad_cost": int(row["actual_ad_cost"]),
        "return_cost": int(row["actual_return_cost"]),
        "fulfillment_fee": int(row["actual_fulfillment_fee"]),
        "payment_fee": int(row["actual_payment_fee"]),
        "vat": int(row["actual_vat"]),
        "margin_amount": int(row["actual_margin_amount"]),
        "margin_rate": row["actual_margin_rate"],
    }
    trace["recomputed_margin"] = stored_total
    trace["margin_difference"] = stored_total - int(row["actual_margin_amount"])
    trace["status"] = PASS if trace["margin_difference"] == 0 else FAIL
    return trace


def _reconstruct_components(row: Any, components: Any) -> dict[str, Any]:
    """근거가 없는 과거 행을 지금 원천으로 되짚는다.

    **저장된 마진 값을 그대로 베끼지 않는다.** 그러면 무엇을 검증하든 항상
    맞는다. 여기서는 "어디서 왔을 값인가" 만 판정하고 금액은 원천을 읽는다.
    """
    def comp(value: int, source: str, confidence: str) -> dict[str, Any]:
        return {"value": value, "source": source, "confidence": confidence}

    if components:
        revenue = comp(components.net_revenue, "SETTLEMENT", ACTUAL)
        fee = comp(components.marketplace_fee, "SETTLEMENT", ACTUAL)
    else:
        revenue = comp(int(row["sale_price"]), "ORDER_LEDGER", ESTIMATED)
        # 정책 요율을 여기서 다시 계산하지 않는다 — 그 시점 정책을 알 수 없다.
        fee = comp(int(row["actual_marketplace_fee"]), "POLICY_OR_UNKNOWN", UNKNOWN)

    if row["purchase_cost"] is not None:
        cost = comp(int(row["purchase_cost"]), "ORDER_PURCHASE_COST", ACTUAL)
    else:
        cost = comp(int(row["actual_cost"]), "MASTER_OR_UNKNOWN", UNKNOWN)

    return {
        "revenue": revenue,
        "cost": cost,
        "shipping_cost": comp(int(row["shipping_fee"] or 0), "ORDER_LEDGER", ACTUAL),
        "marketplace_fee": fee,
        "ad_cost": comp(int(row["ad_cost"] or 0), "ORDER_LEDGER", ACTUAL),
        "return_cost": comp(0, "NOT_AVAILABLE", UNKNOWN),
        "fulfillment_fee": comp(0, "NOT_AVAILABLE", UNKNOWN),
        "payment_fee": comp(0, "NOT_AVAILABLE", UNKNOWN),
        "vat": comp(0, "NOT_AVAILABLE", UNKNOWN),
    }


# ══════════════════════════════════════════════════════════════════════
# 9. 요약 — 화면 상단
# ══════════════════════════════════════════════════════════════════════

def summary(
    db: Session, *, org_id: UUID,
    period_from: date | None = None, period_to: date | None = None,
) -> dict[str, Any]:
    """검증센터 한 판. **점수 하나로 뭉개지 않는다.**

    "정확도 98점" 같은 숫자는 무엇을 확인해야 하는지 알려주지 않는다.
    PASS / FAIL / NOT_VERIFIED 를 그대로 보여주고, 근거로 내려갈 길을 남긴다.
    """
    from app.services import operation_issue as issues

    period = {"from": period_from, "to": period_to}
    freshness = data_freshness(db, org_id=org_id)
    sync = sync_reconciliation(db, org_id=org_id)
    settlement = settlement_reconciliation(
        db, org_id=org_id, period_from=period_from, period_to=period_to)
    confidence = margin_confidence(
        db, org_id=org_id, period_from=period_from, period_to=period_to)
    orders = order_reconciliation(
        db, org_id=org_id, period_from=period_from, period_to=period_to)
    cov = coverage(db, org_id=org_id, period_from=period_from, period_to=period_to)
    sm_cov = settlement_margin_coverage(db, org_id=org_id)
    integ = integrity(db, org_id=org_id)
    unknown = unknown_treated_as_zero(db, org_id=org_id)
    gates = evidence_gates(db, org_id=org_id)

    # 기존 이슈 센터를 그대로 쓴다. 새 예외 체계를 만들지 않는다.
    issue_summary = issues.summary(db, org_id=org_id)

    # 검증이 판정한 것만 센다. NOT_VERIFIED 는 어느 쪽에도 넣지 않는다.
    inventory = inventory_integrity(db, org_id=org_id)
    revenue = revenue_split(
        db, org_id=org_id, period_from=period_from, period_to=period_to)

    verdicts = {
        "sync_reconciliation": sync["status"],
        "settlement_reconciliation": settlement["status"],
        "settlement_margin_coverage": sm_cov["status"],
        "integrity": integ["status"],
        "inventory_integrity": inventory["status"],
        "calculation_evidence": (
            PASS if confidence["without_calculation_evidence"] == 0 else NOT_VERIFIED),
        "unknown_as_zero": unknown["status"],
    }
    counts = {
        PASS: sum(1 for v in verdicts.values() if v == PASS),
        FAIL: sum(1 for v in verdicts.values() if v == FAIL),
        NOT_VERIFIED: sum(1 for v in verdicts.values() if v == NOT_VERIFIED),
    }
    overall = "ATTENTION"
    if counts[FAIL]:
        overall = "FAIL"
    elif counts[NOT_VERIFIED] == 0:
        overall = "PASS"

    return {
        "period": period,
        "overall": overall,
        "verdicts": verdicts,
        "counts": counts,
        "freshness": freshness,
        "orders": orders,
        "sync": sync,
        "settlement": settlement,
        "confidence": confidence,
        "coverage": cov,
        "revenue_split": revenue,
        "settlement_margin_coverage": sm_cov,
        "integrity": integ,
        "inventory": inventory,
        "unknown_as_zero": unknown,
        "evidence_gates": gates,
        "month_close": month_close_reconciliation(db, org_id=org_id),
        "open_issues": issue_summary,
    }
