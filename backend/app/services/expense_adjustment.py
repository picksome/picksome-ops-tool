"""잠긴 월 비용 조정 (M026).

원칙
----
**잠긴 달의 비용은 고치지 않는다.** 그 달의 순이익과 수익배분이 이미 확정됐고,
파트너에게 정산까지 끝났을 수 있다. 과거를 바꾸면 그 배분이 소급해서 틀린 값이 된다.

대신 **지금 열린 달에 조정 항목을 만든다.** 회계에서 하는 방식 그대로다.

  7월 광고비가 10만원 적게 잡혔다
    → 7월 비용을 고치지 않는다
    → 8월(열린 달)에 +10만원 조정 비용을 만들고 7월 원본을 가리킨다
    → 7월 확정 순이익·배분은 그대로, 8월 순이익에 차액이 반영된다

조정도 승인을 거친다
------------------
조정은 결국 돈을 움직인다. 직접 만들 수 있게 열어두면 잠긴 월 보호를
"조정을 만들면 된다" 로 우회하게 된다.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.ids import new_id
from app.core.month_lock import get_close_state, year_month_of

ADJUSTMENT_TYPES = ("LATE_ENTRY", "CORRECTION", "REVERSAL", "RECLASSIFICATION")

# 조정 대상이 되는 상태. 열린 달은 그냥 고치면 되므로 조정이 필요 없다.
_ADJUSTABLE_STATES = ("CONFIRMED", "LOCKED")


def requires_adjustment(db: Session, *, org_id: UUID, at: date | str) -> bool:
    """이 날짜의 비용을 고치려면 조정을 거쳐야 하는가."""
    return get_close_state(
        db, org_id=org_id, year_month=year_month_of(at)
    ) in _ADJUSTABLE_STATES


def _open_period(db: Session, org_id: UUID, at: date) -> str:
    """조정이 들어갈 달. 지금 열린 달이어야 한다."""
    ym = year_month_of(at)
    state = get_close_state(db, org_id=org_id, year_month=ym)
    if state in _ADJUSTABLE_STATES:
        raise ConflictError(
            f"{ym} 도 잠겨 있어 조정을 넣을 수 없습니다. "
            "열린 월의 날짜로 조정을 만들어 주세요.",
            detail={"year_month": ym, "close_state": state},
        )
    return ym


def create_adjustment(
    db: Session, *, org_id: UUID, actor: UUID,
    original_expense_id: UUID | None,
    adjustment_type: str,
    adjustment_reason: str,
    adjustment_date: date,
    amount_delta: int,
    payload: dict[str, Any] | None = None,
    approval_request_id: UUID | None = None,
    _commit: bool = True,
) -> dict[str, Any]:
    """조정 비용을 만든다. **원본은 건드리지 않는다.**

    실제 `expenses` 행을 하나 만들고 `expense_adjustment` 가 그 행이 왜 생겼는지를
    설명한다. 비용 행을 만드는 이유는 월별 집계·세무 리포트가 조정을 **자동으로**
    포함하게 하기 위해서다 — 집계 쿼리마다 "조정도 더해라" 를 넣으면 언젠가 빠뜨린다.
    """
    if adjustment_type not in ADJUSTMENT_TYPES:
        raise ValidationError(
            f"알 수 없는 조정 유형입니다: {adjustment_type}",
            detail={"allowed": list(ADJUSTMENT_TYPES)},
        )
    if not adjustment_reason.strip():
        raise ValidationError("조정 사유는 필수입니다. 사유 없는 조정은 나중에 설명할 수 없습니다.")

    original: dict[str, Any] | None = None
    original_period: str | None = None
    if original_expense_id is not None:
        row = db.execute(text("""
            SELECT id, expense_date, category_id, description, gross_amount,
                   vat_inclusion_type, tax_type, payment_method,
                   payment_card_holder_id, is_personal_card, is_business_use,
                   evidence_type, evidence_status,
                   is_tax_deductible, is_settlement_deductible,
                   attributable_partner_id, supplier_id
              FROM expenses
             WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """), {"i": str(original_expense_id), "o": str(org_id)}).mappings().first()
        if row is None:
            raise NotFoundError("원본 사업비용을 찾을 수 없습니다.")
        original = dict(row)
        original_period = year_month_of(original["expense_date"])
    elif adjustment_type != "LATE_ENTRY":
        raise ValidationError(
            "정정·되돌림·재분류는 원본 비용을 지정해야 합니다.",
            detail={"adjustment_type": adjustment_type},
        )

    period = _open_period(db, org_id, adjustment_date)

    # 조정 비용의 필드는 원본을 따라가되, payload 로 덮어쓸 수 있다
    # (재분류는 카테고리가 바뀌는 게 목적이다).
    base: dict[str, Any] = dict(original or {})
    base.pop("id", None)
    base.update(payload or {})

    if base.get("category_id") is None:
        raise ValidationError("조정 비용의 카테고리가 필요합니다.")

    # 금액은 정수(원). 음수 조정은 gross_amount 를 음수로 두지 않고
    # **차액 자체**를 amount_delta 에 담고, 비용 행은 절대값으로 만든다.
    # `expenses.gross_amount` 에 CHECK(>=0) 가 있고, 음수 비용은 집계에서
    # "환급" 인지 "오류" 인지 구분되지 않는다.
    if amount_delta == 0:
        raise ValidationError("조정 금액이 0 입니다. 바뀌는 것이 없습니다.")

    adj_expense_id = new_id()
    db.execute(text("""
        INSERT INTO expenses
               (id, org_id, expense_date, category_id, description,
                gross_amount, supply_amount, vat_amount, vat_inclusion_type,
                tax_type, payment_method, payment_card_holder_id, is_personal_card,
                is_business_use, evidence_type, evidence_status,
                is_tax_deductible, is_settlement_deductible,
                attributable_partner_id, supplier_id, memo, created_by, updated_by)
        VALUES (:i, :o, :d, :cat, :desc,
                :gross, :gross, 0, :vit,
                :tt, :pm, :holder, :personal,
                :biz, :ev_type, :ev_status,
                :tax_ded, :set_ded,
                :partner, :supplier, :memo, :actor, :actor)
    """), {
        "i": str(adj_expense_id), "o": str(org_id), "d": adjustment_date,
        "cat": str(base["category_id"]),
        "desc": (base.get("description") or "조정")[:400],
        "gross": abs(amount_delta),
        "vit": base.get("vat_inclusion_type") or "INCLUDED",
        "tt": base.get("tax_type") or "GENERAL",
        "pm": base.get("payment_method") or "OTHER",
        "holder": str(base["payment_card_holder_id"]) if base.get("payment_card_holder_id") else None,
        "personal": bool(base.get("is_personal_card", False)),
        "biz": bool(base.get("is_business_use", True)),
        "ev_type": base.get("evidence_type") or "OTHER",
        "ev_status": base.get("evidence_status") or "NONE",
        "tax_ded": bool(base.get("is_tax_deductible", True)),
        "set_ded": bool(base.get("is_settlement_deductible", True)),
        "partner": str(base["attributable_partner_id"]) if base.get("attributable_partner_id") else None,
        "supplier": str(base["supplier_id"]) if base.get("supplier_id") else None,
        "memo": f"[{adjustment_type}] {adjustment_reason}"[:400],
        "actor": str(actor),
    })

    adj_id = new_id()
    db.execute(text("""
        INSERT INTO expense_adjustment
               (id, org_id, original_expense_id, adjustment_expense_id,
                adjustment_type, adjustment_reason, original_period,
                adjustment_period, amount_delta, approval_request_id,
                created_by, updated_by)
        VALUES (:i, :o, :orig, :adj, :type, :reason, :op, :ap, :delta, :arid,
                :actor, :actor)
    """), {
        "i": str(adj_id), "o": str(org_id),
        "orig": str(original_expense_id) if original_expense_id else None,
        "adj": str(adj_expense_id), "type": adjustment_type,
        "reason": adjustment_reason, "op": original_period, "ap": period,
        "delta": amount_delta,
        "arid": str(approval_request_id) if approval_request_id else None,
        "actor": str(actor),
    })

    record_audit(db, org_id=org_id, domain_table="expense_adjustment",
                 domain_row_id=adj_id, action="CREATE", actor_user_id=actor,
                 before=None,
                 after={"adjustment_type": adjustment_type,
                        "original_expense_id": str(original_expense_id) if original_expense_id else None,
                        "adjustment_period": period, "amount_delta": amount_delta})

    # 조정이 들어간 **열린 달**만 재계산 대상이다. 원본 달은 확정이라 건드리지 않는다.
    from app.services.period_recalc import mark_period_dirty

    mark_period_dirty(db, org_id=org_id, at=adjustment_date,
                      reason=f"비용 조정({adjustment_type})", actor=actor)

    if _commit:
        db.commit()

    return {
        "id": str(adj_id),
        "adjustment_expense_id": str(adj_expense_id),
        "original_expense_id": str(original_expense_id) if original_expense_id else None,
        "adjustment_type": adjustment_type,
        "original_period": original_period,
        "adjustment_period": period,
        "amount_delta": amount_delta,
    }


def list_adjustments(
    db: Session, *, org_id: UUID, original_expense_id: UUID | None = None,
    adjustment_period: str | None = None,
) -> list[dict[str, Any]]:
    where = ["org_id = :o", "deleted_at IS NULL"]
    params: dict[str, Any] = {"o": str(org_id)}
    if original_expense_id:
        where.append("original_expense_id = :oe")
        params["oe"] = str(original_expense_id)
    if adjustment_period:
        where.append("adjustment_period = :ap")
        params["ap"] = adjustment_period

    rows = db.execute(text(f"""
        SELECT id, original_expense_id, adjustment_expense_id, adjustment_type,
               adjustment_reason, original_period, adjustment_period,
               amount_delta, approval_request_id, created_at, created_by
          FROM expense_adjustment
         WHERE {' AND '.join(where)}
         ORDER BY created_at DESC
    """), params).mappings().all()
    return [dict(r) for r in rows]
