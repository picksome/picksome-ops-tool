"""이익 4단계 서비스 (M016).

EXPECTED → PROVISIONAL → SETTLED → FINAL

각 단계의 의미
--------------
EXPECTED     상품 등록 시점의 추정. `product_margin_expected` 가 이미 들고 있다.
PROVISIONAL  주문이 실제로 발생했다. 판매가·수량·쿠폰은 확정, 수수료는 정책 추정값.
SETTLED      마켓 정산과 매칭됐다. 실제 수수료·광고차감·실입금이 반영된다.
FINAL        반품 기한이 지나고 월이 잠겼다. 더 이상 바뀌지 않는다.

설계 판단
---------
- **전이는 단방향이다.** 되돌리려면 월 잠금 해제가 선행돼야 한다.
  FINAL 을 임의로 낮추면 "확정했다고 보고한 숫자" 가 조용히 달라진다.
- **FINAL 은 자동으로만 만들어진다.** 월 잠금(monthly_close.lock) 시점에 일괄 전환한다.
  사용자가 개별 주문을 FINAL 로 올리는 경로는 두지 않는다 — 월 단위로 맞아야 정산이 성립한다.
- **과거 확정 데이터를 현재 정책으로 재계산하지 않는다.** 계산 시점의 정책 버전을
  `applied_policy_versions_json` 에 박제하고, 이후 정책이 바뀌어도 건드리지 않는다.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import ConflictError, NotFoundError
from app.core.month_lock import assert_period_writable
from app.services.settlement import month_range

# 단계 순서. 인덱스 비교로 역행을 막는다.
_ORDER = ("EXPECTED", "PROVISIONAL", "SETTLED", "FINAL")

# 반품 가능 기간 기본값. org.settings_json.profit.return_window_days 로 재정의 가능.
# 마켓·카테고리마다 실제 기간이 달라 코드에 확정값을 두지 않는다(43번 문서 §6-5).
_DEFAULT_RETURN_WINDOW_DAYS = 15


def _rank(status: str) -> int:
    return _ORDER.index(status) if status in _ORDER else 0


def return_window_days(db: Session, org_id: UUID) -> int:
    row = db.execute(
        text("SELECT settings_json FROM organization WHERE id = :org"),
        {"org": str(org_id)},
    ).scalar()
    if isinstance(row, dict):
        v = (row.get("profit") or {}).get("return_window_days")
        if isinstance(v, int) and v > 0:
            return v
    return _DEFAULT_RETURN_WINDOW_DAYS


def get_profit_detail(db: Session, *, org_id: UUID, sales_order_id: UUID) -> dict[str, Any]:
    """한 주문의 4단계 금액 + 차이 + 미확정 사유.

    화면(판매장부 drawer)이 이 응답만으로 단계 배지·금액·사유를 전부 그릴 수 있어야 한다.
    """
    row = db.execute(text("""
        SELECT so.id, so.business_key, so.sale_date, so.marketplace,
               so.marketplace_order_no, so.product_snapshot_name, so.product_id,
               so.quantity, so.sale_price, so.profit_status,
               so.profit_calculated_at, so.profit_finalized_at, so.return_window_ends_at,
               pma.id AS actual_id,
               pma.provisional_profit_amount, pma.settled_profit_amount,
               pma.final_profit_amount, pma.actual_margin_amount, pma.actual_margin_rate,
               pma.settlement_record_id, pma.applied_policy_versions_json,
               pma.calculation_snapshot_json,
               pme.computed_margin_amount AS expected_profit_amount,
               pme.computed_margin_rate  AS expected_margin_rate
          FROM sales_order so
          LEFT JOIN product_margin_actual pma
                 ON pma.sales_order_id = so.id AND pma.deleted_at IS NULL
          LEFT JOIN product_margin_expected pme
                 ON pme.product_id = so.product_id AND pme.deleted_at IS NULL
         WHERE so.id = :id AND so.org_id = :org AND so.deleted_at IS NULL
         ORDER BY pme.created_at DESC
         LIMIT 1
    """), {"id": str(sales_order_id), "org": str(org_id)}).mappings().first()

    if row is None:
        raise NotFoundError("판매 건을 찾을 수 없습니다.")

    d = dict(row)
    status = d["profit_status"]
    ym = str(d["sale_date"])[:7]

    from app.core.month_lock import get_close_state
    close_state = get_close_state(db, org_id=org_id, year_month=ym)

    expected = d.get("expected_profit_amount")
    provisional = d.get("provisional_profit_amount")
    settled = d.get("settled_profit_amount")
    final = d.get("final_profit_amount")

    # 미확정 사유 — "왜 아직 FINAL 이 아닌가" 를 사람 말로 돌려준다.
    reasons: list[str] = []
    if status != "FINAL":
        if d.get("settlement_record_id") is None:
            reasons.append("쇼핑몰 정산과 아직 매칭되지 않았습니다.")
        window_end = d.get("return_window_ends_at")
        if window_end is None:
            days = return_window_days(db, org_id)
            window_end = d["sale_date"] + timedelta(days=days)
        if window_end >= date.today():
            reasons.append(f"반품 가능 기간이 {window_end} 까지 남아 있습니다.")
        if close_state != "LOCKED":
            reasons.append(f"{ym} 월 마감이 아직 잠기지 않았습니다(현재 {close_state}).")

    def diff(a: int | None, b: int | None) -> int | None:
        return None if a is None or b is None else int(a) - int(b)

    return {
        "sales_order_id": str(d["id"]),
        "business_key": d["business_key"],
        "sale_date": d["sale_date"],
        "marketplace": d["marketplace"],
        "order_no": d["marketplace_order_no"],
        "product_name": d["product_snapshot_name"],
        "quantity": d["quantity"],
        "sale_price": d["sale_price"],
        "profit_status": status,
        "stages": {
            "EXPECTED": expected,
            "PROVISIONAL": provisional,
            "SETTLED": settled,
            "FINAL": final,
        },
        "deltas": {
            # 예상 대비 실제가 얼마나 벌어졌는지 — 이 숫자가 마진 추정의 품질이다
            "provisional_vs_expected": diff(provisional, expected),
            "settled_vs_provisional": diff(settled, provisional),
            "final_vs_settled": diff(final, settled),
        },
        "margin_rate": d.get("actual_margin_rate"),
        "applied_policy_versions": d.get("applied_policy_versions_json"),
        "calculation_snapshot": d.get("calculation_snapshot_json"),
        "return_window_ends_at": d.get("return_window_ends_at"),
        "month_close_state": close_state,
        "not_final_reasons": reasons,
        "profit_calculated_at": d.get("profit_calculated_at"),
        "profit_finalized_at": d.get("profit_finalized_at"),
    }


def _advance(
    db: Session, *, org_id: UUID, sales_order_id: UUID, to_status: str,
    actor_user_id: UUID | None, reason: str | None = None,
) -> str:
    """단계 전진. 역행이면 ConflictError."""
    current = db.execute(
        text("SELECT profit_status FROM sales_order WHERE id = :id AND org_id = :org"),
        {"id": str(sales_order_id), "org": str(org_id)},
    ).scalar()
    if current is None:
        raise NotFoundError("판매 건을 찾을 수 없습니다.")
    if _rank(to_status) < _rank(current):
        raise ConflictError(
            f"이익 단계는 되돌릴 수 없습니다 ({current} → {to_status}). "
            "되돌리려면 월 잠금을 먼저 해제해야 합니다.",
            detail={"from": current, "to": to_status},
        )
    if current == to_status:
        return current

    db.execute(text("""
        UPDATE sales_order
           SET profit_status = :st,
               profit_calculated_at = now(),
               profit_finalized_at = CASE WHEN :st = 'FINAL' THEN now() ELSE profit_finalized_at END,
               updated_at = now()
         WHERE id = :id AND org_id = :org
    """), {"st": to_status, "id": str(sales_order_id), "org": str(org_id)})

    record_audit(
        db, org_id=org_id, domain_table="sales_order", domain_row_id=sales_order_id,
        action="STATUS_CHANGE", actor_user_id=actor_user_id,
        before={"profit_status": current}, after={"profit_status": to_status},
        reason=reason,
    )
    return to_status


def recalculate_provisional(
    db: Session, *, org_id: UUID, sales_order_id: UUID, actor_user_id: UUID | None = None,
) -> str:
    """주문 등록·수정 후 호출. 기존 실 마진 값을 PROVISIONAL 금액으로 복사한다.

    마진 계산 자체는 services/margin.py 가 한다. 여기서 다시 계산하지 않는다 —
    계산 로직이 두 군데로 갈라지면 값이 어긋난다.
    """
    sale_date = db.execute(
        text("SELECT sale_date FROM sales_order WHERE id = :id AND org_id = :org"),
        {"id": str(sales_order_id), "org": str(org_id)},
    ).scalar()
    if sale_date is not None:
        assert_period_writable(
            db, org_id=org_id, effective_date=sale_date,
            operation="RECALC_PROFIT", what="잠정 이익",
        )

    db.execute(text("""
        UPDATE product_margin_actual
           SET provisional_profit_amount = actual_margin_amount,
               updated_at = now()
         WHERE sales_order_id = :id AND deleted_at IS NULL
    """), {"id": str(sales_order_id)})
    status = _advance(
        db, org_id=org_id, sales_order_id=sales_order_id,
        to_status="PROVISIONAL", actor_user_id=actor_user_id,
        reason="주문 확정 — 잠정 이익 산출",
    )
    db.commit()
    return status


def apply_settlement(
    db: Session, *, org_id: UUID, sales_order_id: UUID,
    settlement_record_id: UUID, actor_user_id: UUID | None = None,
    policy_versions: dict[str, int] | None = None,
) -> str:
    """정산 매칭 시 호출. 실제 정산 금액으로 SETTLED 값을 만든다.

    실제 수수료·광고쿠폰 차감·기타 차감을 정산 레코드에서 가져와
    매출에서 빼고, 원가·배송비는 기존 실 마진 계산값을 그대로 쓴다.
    """
    rec = db.execute(text("""
        SELECT gross_revenue, marketplace_fee, ad_cost, return_cost,
               fulfillment_fee, other_deductions, net_settlement
          FROM marketplace_settlement_record
         WHERE id = :id AND org_id = :org AND deleted_at IS NULL
    """), {"id": str(settlement_record_id), "org": str(org_id)}).mappings().first()
    if rec is None:
        raise NotFoundError("정산 레코드를 찾을 수 없습니다.")

    actual = db.execute(text("""
        SELECT id, actual_cost, actual_shipping_cost
          FROM product_margin_actual
         WHERE sales_order_id = :id AND deleted_at IS NULL
         LIMIT 1
    """), {"id": str(sales_order_id)}).mappings().first()
    if actual is None:
        raise ConflictError("실 마진이 먼저 계산되어야 정산을 반영할 수 있습니다.")

    # 실입금(net_settlement)이 있으면 그걸 매출로 본다. 마켓이 실제로 준 돈이 진실이다.
    inflow = int(rec["net_settlement"] or 0)
    cost = int(actual["actual_cost"] or 0) + int(actual["actual_shipping_cost"] or 0)
    settled_profit = inflow - cost

    db.execute(text("""
        UPDATE product_margin_actual
           SET settled_profit_amount = :amt,
               settlement_record_id = :rec,
               applied_policy_versions_json = COALESCE(CAST(:pv AS JSONB), applied_policy_versions_json),
               updated_at = now()
         WHERE id = :pid
    """), {
        "amt": settled_profit, "rec": str(settlement_record_id), "pid": str(actual["id"]),
        "pv": json.dumps(policy_versions, ensure_ascii=False) if policy_versions else None,
    })

    status = _advance(
        db, org_id=org_id, sales_order_id=sales_order_id,
        to_status="SETTLED", actor_user_id=actor_user_id,
        reason="쇼핑몰 정산 매칭 — 실제 수수료 반영",
    )
    db.commit()
    return status


def finalize_month(
    db: Session, *, org_id: UUID, year_month: str, actor_user_id: UUID | None = None,
) -> dict[str, int]:
    """월 잠금 시 호출. 조건을 만족하는 주문만 FINAL 로 올린다.

    조건을 못 채운 주문은 **건너뛴다.** 잠긴 달에 SETTLED 로 남은 건이 있으면
    그건 정산이 안 끝났다는 뜻이고, 억지로 FINAL 로 올리면 틀린 숫자가 확정된다.
    """
    start, end = month_range(year_month)
    days = return_window_days(db, org_id)

    rows = db.execute(text("""
        SELECT so.id, so.sale_date, so.return_window_ends_at,
               pma.id AS pma_id, pma.settled_profit_amount, pma.settlement_record_id
          FROM sales_order so
          LEFT JOIN product_margin_actual pma
                 ON pma.sales_order_id = so.id AND pma.deleted_at IS NULL
         WHERE so.org_id = :org AND so.deleted_at IS NULL
           AND so.sale_date BETWEEN :s AND :e
           AND so.profit_status <> 'FINAL'
    """), {"org": str(org_id), "s": start, "e": end}).mappings().all()

    finalized = 0
    skipped_window = 0
    skipped_unsettled = 0

    for r in rows:
        window_end = r["return_window_ends_at"] or (r["sale_date"] + timedelta(days=days))
        if window_end >= date.today():
            skipped_window += 1
            continue
        if r["settlement_record_id"] is None:
            skipped_unsettled += 1
            continue

        final_amount = r["settled_profit_amount"]
        if r["pma_id"] is not None:
            db.execute(text("""
                UPDATE product_margin_actual
                   SET final_profit_amount = :amt, updated_at = now()
                 WHERE id = :pid
            """), {"amt": final_amount, "pid": str(r["pma_id"])})

        _advance(
            db, org_id=org_id, sales_order_id=r["id"], to_status="FINAL",
            actor_user_id=actor_user_id, reason=f"{year_month} 월 잠금 — 이익 확정",
        )
        finalized += 1

    db.commit()
    return {
        "finalized": finalized,
        "skipped_return_window_open": skipped_window,
        "skipped_not_settled": skipped_unsettled,
        "total_examined": len(rows),
    }


def summarize_month(db: Session, *, org_id: UUID, year_month: str) -> dict[str, Any]:
    """월 단위 단계별 합계. 대시보드가 예상이익과 확정이익을 섞지 않도록 분리해서 준다."""
    start, end = month_range(year_month)
    row = db.execute(text("""
        SELECT
          COUNT(*) AS order_count,
          COUNT(*) FILTER (WHERE so.profit_status = 'PROVISIONAL') AS provisional_count,
          COUNT(*) FILTER (WHERE so.profit_status = 'SETTLED')     AS settled_count,
          COUNT(*) FILTER (WHERE so.profit_status = 'FINAL')       AS final_count,
          COALESCE(SUM(pma.provisional_profit_amount), 0) AS provisional_total,
          COALESCE(SUM(pma.settled_profit_amount), 0)     AS settled_total,
          COALESCE(SUM(pma.final_profit_amount), 0)       AS final_total
        FROM sales_order so
        LEFT JOIN product_margin_actual pma
               ON pma.sales_order_id = so.id AND pma.deleted_at IS NULL
       WHERE so.org_id = :org AND so.deleted_at IS NULL
         AND so.sale_date BETWEEN :s AND :e
    """), {"org": str(org_id), "s": start, "e": end}).mappings().first()
    return dict(row) if row else {}
