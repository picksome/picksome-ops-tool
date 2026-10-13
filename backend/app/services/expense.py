"""사업비용 CRUD (D-284).

vat_inclusion_type 3가지 분기:
  INCLUDED:  gross 안에 vat 포함  → supply = gross / (1+rate), vat = gross - supply
  EXCLUDED:  사용자가 supply 입력 → vat = supply * rate, gross = supply + vat
  EXEMPT:    면세                 → supply = gross, vat = 0

is_manual_tax_override=True 면 자동 분리 skip → 사용자 입력값 그대로 + 합계 검증.

tax_policy_id 박제: expense 행 생성 시 effective tax_policy 의 id 를 컬럼에 저장.
→ 추후 정책이 변경돼도 본 비용은 박제된 vat_rate 기준으로 재현 가능.

DB CHECK:
  ck_expenses_total:      gross = supply + vat
  ck_expenses_exempt_vat: EXEMPT 면 vat_amount = 0

LOCKED 정산 (linked_settlement_id) 에 묶인 비용은 수정/삭제 금지 → SettlementLockedError.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import InvalidVatInputError, NotFoundError
from app.core.ids import new_id
from app.core.month_lock import assert_period_writable
from app.domain.expense import ExpenseCreate, ExpenseOut, ExpenseUpdate


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _resolve_tax_policy(
    db: Session, *, org_id: UUID, tax_type: str, at: date
) -> tuple[UUID | None, Decimal]:
    """effective tax_policy 의 id + vat_rate 반환. 없으면 (None, 0.1)."""
    row = db.execute(
        text("""
          SELECT id, vat_rate
            FROM tax_policy
           WHERE org_id = :o AND tax_type = CAST(:tt AS enum_tax_type)
             AND effective_from <= :at AND (effective_to IS NULL OR effective_to >= :at)
             AND is_active = TRUE AND deleted_at IS NULL
           ORDER BY effective_from DESC LIMIT 1
        """),
        {"o": str(org_id), "tt": tax_type, "at": at},
    ).first()
    if row is None:
        # MVP 안전 기본값 — 부가세 10%
        return None, Decimal("0.1000")
    return UUID(str(row.id)), Decimal(str(row.vat_rate))


def _split_amounts(
    *,
    vat_inclusion: str,
    gross: int,
    supply: int | None,
    vat: int | None,
    vat_rate: Decimal,
    is_manual_override: bool,
) -> tuple[int, int]:
    """(supply_amount, vat_amount) 반환.

    is_manual_override=True 면 사용자 입력 그대로 사용하되 합계 검증.
    아니면 vat_inclusion_type 에 따라 자동 분리.
    """
    if is_manual_override:
        s = supply if supply is not None else gross
        v = vat if vat is not None else 0
        if s + v != gross:
            raise InvalidVatInputError(
                f"수동 입력 검증 실패: supply({s}) + vat({v}) != gross({gross})",
                detail={"gross": gross, "supply": s, "vat": v},
            )
        return s, v

    if vat_inclusion == "EXEMPT":
        return gross, 0
    if vat_inclusion == "EXCLUDED":
        # 사용자가 supply 입력 — gross 는 (supply + vat) 로 클라이언트가 미리 계산해서 보낼 수도, 자동 분리할 수도.
        # 우리는 supply 입력이 있으면 그것을 신뢰 + vat = supply * rate.
        if supply is not None:
            v = int(Decimal(supply) * vat_rate)
            return supply, v
        # supply 미입력 → gross 를 supply 로 간주 + vat = supply * rate, 합계는 supply + vat 로 재계산
        v = int((Decimal(gross) * vat_rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return gross, v
    # INCLUDED 기본
    if vat_rate == 0:
        return gross, 0
    v = int((Decimal(gross) * vat_rate / (Decimal(1) + vat_rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    s = gross - v
    return s, v


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

# 이름은 여기서 조인해서 같이 내려준다.
#
# 화면은 UUID 를 사람에게 보여줄 수 없고, 목록에 있는 카테고리·결제자 이름을
# 행마다 따로 조회하면 한 페이지에 N+1 이 난다. 파트너 목록을 통째로 받아
# 프론트에서 붙이는 방법도 있지만, 그러면 삭제된 파트너의 과거 비용이
# 이름 없이 뜬다 — 조인은 그 행이 실제로 가리키는 값을 그대로 보여준다.
#
# ⚠️ 이 쿼리를 쓰는 곳은 WHERE 절 컬럼을 반드시 `e.` 로 한정해야 한다.
#    `org_id` 는 조인된 세 테이블에 모두 있어 한정하지 않으면 ambiguous 다.
_SELECT = """
  SELECT e.id, e.expense_date, e.category_id, e.description,
         e.gross_amount, e.supply_amount, e.vat_amount,
         e.vat_inclusion_type, e.tax_type, e.tax_policy_id, e.is_manual_tax_override,
         e.payment_method, e.payment_card_holder_id, e.is_personal_card, e.is_business_use,
         e.evidence_type, e.evidence_status,
         e.is_tax_deductible, e.is_settlement_deductible,
         e.attributable_partner_id, e.supplier_id, e.memo,
         e.linked_settlement_id, e.created_at,
         c.name  AS category_name,
         h.name  AS card_holder_name,
         ap.name AS attributable_partner_name,
         s.name  AS supplier_name
    FROM expenses e
    LEFT JOIN expense_category_policy c ON c.id = e.category_id
    LEFT JOIN partner h                 ON h.id = e.payment_card_holder_id
    LEFT JOIN partner ap                ON ap.id = e.attributable_partner_id
    LEFT JOIN external_partner s        ON s.id = e.supplier_id
"""


def list_expenses(
    db: Session,
    *,
    org_id: UUID,
    from_dt: date | None = None,
    to_dt: date | None = None,
    category_id: UUID | None = None,
    evidence_status: str | None = None,
    q: str | None = None,
    payment_method: str | None = None,
    payment_card_holder_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ExpenseOut], int]:
    where = ["e.org_id = :o", "e.deleted_at IS NULL"]
    params: dict[str, Any] = {"o": str(org_id)}
    if from_dt is not None:
        where.append("e.expense_date >= :from_dt")
        params["from_dt"] = from_dt
    if to_dt is not None:
        where.append("e.expense_date <= :to_dt")
        params["to_dt"] = to_dt
    if category_id is not None:
        where.append("e.category_id = :cid")
        params["cid"] = str(category_id)
    if evidence_status:
        where.append("e.evidence_status = :es")
        params["es"] = evidence_status
    if payment_method:
        where.append("e.payment_method = CAST(:pm AS enum_payment_method)")
        params["pm"] = payment_method
    if payment_card_holder_id is not None:
        where.append("e.payment_card_holder_id = :holder")
        params["holder"] = str(payment_card_holder_id)
    if q and q.strip():
        # 내용·메모만 본다. 금액을 검색어로 받으면 "12000" 이 메모의 일부와도 걸려
        # 사용자가 기대한 결과와 어긋난다 — 금액은 기간·카테고리로 좁힌다.
        where.append("(e.description ILIKE :q OR e.memo ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    where_sql = " AND ".join(where)

    count_row = db.execute(
        text(f"SELECT COUNT(*) AS c FROM expenses e WHERE {where_sql}"),
        params,
    ).first()
    total = int(count_row.c) if count_row else 0
    rows = db.execute(
        text(f"""
          {_SELECT}
           WHERE {where_sql}
           ORDER BY e.expense_date DESC, e.created_at DESC
           LIMIT :limit OFFSET :offset
        """),
        {**params, "limit": limit, "offset": offset},
    ).all()
    return [ExpenseOut(**r._mapping) for r in rows], total


def get_expense(db: Session, *, org_id: UUID, expense_id: UUID) -> ExpenseOut:
    row = db.execute(
        text(f"{_SELECT} WHERE e.id = :i AND e.org_id = :o AND e.deleted_at IS NULL"),
        {"i": str(expense_id), "o": str(org_id)},
    ).first()
    if row is None:
        raise NotFoundError("사업비용을 찾을 수 없습니다.")
    return ExpenseOut(**row._mapping)


def create_expense(
    db: Session, *, org_id: UUID, actor: UUID, body: ExpenseCreate
) -> ExpenseOut:
    # 잠긴 월에는 비용을 추가할 수 없다 (docs/43 Phase 2).
    # 기준은 등록 시각이 아니라 expense_date 다 — 8월에 7월 비용을 입력하는 일이 흔하고,
    # 그건 7월이 잠겼는지로 판단해야 한다.
    assert_period_writable(
        db, org_id=org_id, effective_date=body.expense_date,
        operation="CREATE", what="사업비용",
    )
    tax_policy_id, vat_rate = _resolve_tax_policy(
        db, org_id=org_id, tax_type=body.tax_type, at=body.expense_date
    )
    supply, vat = _split_amounts(
        vat_inclusion=body.vat_inclusion_type,
        gross=body.gross_amount,
        supply=body.supply_amount,
        vat=body.vat_amount,
        vat_rate=vat_rate,
        is_manual_override=body.is_manual_tax_override,
    )
    # gross 일치 재조정 (INCLUDED/EXEMPT 는 gross 보존, EXCLUDED 는 gross = supply+vat 로 재계산)
    new_gross = supply + vat if body.vat_inclusion_type == "EXCLUDED" else body.gross_amount
    if body.vat_inclusion_type != "EXCLUDED" and supply + vat != new_gross:
        # INCLUDED 반올림 보정 — 1원 오차 시 supply 조정
        supply = new_gross - vat

    new_eid = new_id()
    db.execute(
        text("""
          INSERT INTO expenses (
            id, org_id, expense_date, category_id, description,
            gross_amount, supply_amount, vat_amount,
            vat_inclusion_type, tax_type, tax_policy_id, is_manual_tax_override,
            payment_method, payment_card_holder_id, is_personal_card, is_business_use,
            evidence_type, evidence_status,
            is_tax_deductible, is_settlement_deductible,
            attributable_partner_id, supplier_id, memo,
            created_by, updated_by
          ) VALUES (
            :id, :o, :ed, :cid, :desc,
            :gross, :supply, :vat,
            CAST(:vit AS enum_vat_inclusion_type),
            CAST(:tt AS enum_tax_type), :tpid, :imto,
            CAST(:pm AS enum_payment_method), :pch, :ipc, :ibu,
            CAST(:et AS enum_evidence_type), CAST(:es AS enum_evidence_status),
            :itd, :isd,
            :app, :sup, :memo,
            :a, :a
          )
        """),
        {
            "id": str(new_eid),
            "o": str(org_id),
            "ed": body.expense_date,
            "cid": str(body.category_id),
            "desc": body.description,
            "gross": new_gross,
            "supply": supply,
            "vat": vat,
            "vit": body.vat_inclusion_type,
            "tt": body.tax_type,
            "tpid": str(tax_policy_id) if tax_policy_id else None,
            "imto": body.is_manual_tax_override,
            "pm": body.payment_method,
            "pch": str(body.payment_card_holder_id) if body.payment_card_holder_id else None,
            "ipc": body.is_personal_card,
            "ibu": body.is_business_use,
            "et": body.evidence_type,
            "es": body.evidence_status,
            "itd": body.is_tax_deductible,
            "isd": body.is_settlement_deductible,
            "app": str(body.attributable_partner_id) if body.attributable_partner_id else None,
            "sup": str(body.supplier_id) if body.supplier_id else None,
            "memo": body.memo,
            "a": str(actor),
        },
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="expenses",
        domain_row_id=new_eid,
        action="CREATE",
        actor_user_id=actor,
        after={
            "expense_date": str(body.expense_date),
            "gross_amount": new_gross,
            "vat_inclusion_type": body.vat_inclusion_type,
        },
    )
    db.commit()
    return get_expense(db, org_id=org_id, expense_id=new_eid)


def update_expense(
    db: Session, *, org_id: UUID, actor: UUID, expense_id: UUID, body: ExpenseUpdate,
    _internal_approval_apply: bool = False,
) -> ExpenseOut:
    """사업비용 수정.

    ⚠️ **승인 대상 필드는 이 경로로 바꿀 수 없다.**
    비용 금액은 순이익의 입력값이고 순이익이 곧 파트너 수익배분이다. 승인 화면을
    만들어 놓고 일반 수정 API 를 열어두면 승인은 장식이 된다(상품에서 겪은 그대로).

    `_internal_approval_apply` 는 승인 적용 경로 전용이다. **API 입력이 아니다** —
    라우터가 노출하지 않으므로 클라이언트가 설정할 수 없다.
    """
    before = get_expense(db, org_id=org_id, expense_id=expense_id)

    if not _internal_approval_apply:
        from app.core.exceptions import ApprovalRequiredError
        from app.services.approval_targets import all_approval_fields

        requested = set(body.model_dump(exclude_unset=True))
        blocked = sorted(requested & all_approval_fields("expense"))
        if blocked:
            # 섞어 보내면 **전부 거부**한다. 일부만 저장하면 무엇이 반영됐는지 알 수 없다.
            raise ApprovalRequiredError(
                f"승인이 필요한 필드는 직접 수정할 수 없습니다: {blocked}. "
                "승인 요청을 만들어 주세요.",
                detail={
                    "approval_required_fields": blocked,
                    "target_type": "expense",
                    "target_id": str(expense_id),
                },
            )

    assert_period_writable(
        db, org_id=org_id, effective_date=before.expense_date,
        operation="UPDATE", what="사업비용",
    )
    if before.linked_settlement_id is not None:
        # 정산 LOCKED 된 비용은 수정 금지 — settlement 검증 시 처리. 여기는 차단만.
        from app.core.exceptions import SettlementLockedError

        raise SettlementLockedError("LOCKED 정산에 묶인 비용은 수정할 수 없습니다.")

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return before

    # 금액/VAT 관련 필드가 변경되면 재계산
    needs_recalc = bool(
        {"gross_amount", "supply_amount", "vat_amount", "vat_inclusion_type",
         "tax_type", "is_manual_tax_override", "expense_date"} & fields.keys()
    )
    if needs_recalc:
        gross = fields.get("gross_amount", before.gross_amount)
        supply_in = fields.get("supply_amount", None)
        vat_in = fields.get("vat_amount", None)
        vit = fields.get("vat_inclusion_type", before.vat_inclusion_type)
        tt = fields.get("tax_type", before.tax_type)
        imto = fields.get("is_manual_tax_override", before.is_manual_tax_override)
        ed = fields.get("expense_date", before.expense_date)

        tax_policy_id, vat_rate = _resolve_tax_policy(db, org_id=org_id, tax_type=tt, at=ed)
        supply, vat = _split_amounts(
            vat_inclusion=vit, gross=gross, supply=supply_in, vat=vat_in,
            vat_rate=vat_rate, is_manual_override=imto,
        )
        new_gross = supply + vat if vit == "EXCLUDED" else gross
        if vit != "EXCLUDED" and supply + vat != new_gross:
            supply = new_gross - vat

        fields["gross_amount"] = new_gross
        fields["supply_amount"] = supply
        fields["vat_amount"] = vat
        fields["tax_policy_id"] = tax_policy_id

    set_parts: list[str] = []
    params: dict[str, Any] = {
        "i": str(expense_id), "o": str(org_id), "now": _now(), "actor": str(actor),
    }
    enum_casts = {
        "vat_inclusion_type": "enum_vat_inclusion_type",
        "tax_type": "enum_tax_type",
        "payment_method": "enum_payment_method",
        "evidence_type": "enum_evidence_type",
        "evidence_status": "enum_evidence_status",
    }
    for k, v in fields.items():
        if k in enum_casts and v is not None:
            set_parts.append(f"{k} = CAST(:{k} AS {enum_casts[k]})")
        else:
            set_parts.append(f"{k} = :{k}")
        if isinstance(v, UUID):
            params[k] = str(v)
        else:
            params[k] = v
    set_parts.extend(["updated_at = :now", "updated_by = :actor"])

    db.execute(
        text(f"""
          UPDATE expenses SET {', '.join(set_parts)}
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        params,
    )
    # 승인 적용 경로에서는 감사 로그를 여기서 남기지 않는다 —
    # approval.apply() 가 승인 요청 번호와 함께 before/after 를 이미 기록한다.
    # 여기서 또 쓰면 같은 변경이 두 줄로 남아 "몇 번 바뀌었나" 를 셀 수 없다.
    if not _internal_approval_apply:
        record_audit(
            db,
            org_id=org_id,
            domain_table="expenses",
            domain_row_id=expense_id,
            action="UPDATE",
            actor_user_id=actor,
            before=before.model_dump(mode="json"),
            after=fields,
        )

    # ⚠️ 승인 적용 경로에서는 **커밋하지 않는다.**
    # approval.apply() 가 "비용 변경 + 승인 상태 전이 + 감사 로그 + 재계산 표시" 를
    # 한 트랜잭션으로 묶는다. 여기서 커밋하면 뒤이어 감사 기록이 실패해도
    # 비용 변경만 남는다 (상품에서 겪은 그대로 — 원자성 테스트가 이걸 잡았다).
    if not _internal_approval_apply:
        db.commit()
    return get_expense(db, org_id=org_id, expense_id=expense_id)


def delete_expense(db: Session, *, org_id: UUID, actor: UUID, expense_id: UUID) -> None:
    before = get_expense(db, org_id=org_id, expense_id=expense_id)
    assert_period_writable(
        db, org_id=org_id, effective_date=before.expense_date,
        operation="UPDATE", what="사업비용",
    )
    if before.linked_settlement_id is not None:
        from app.core.exceptions import SettlementLockedError

        raise SettlementLockedError("LOCKED 정산에 묶인 비용은 삭제할 수 없습니다.")
    db.execute(
        text("""
          UPDATE expenses SET deleted_at = :now, deleted_by = :actor
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        {"now": _now(), "actor": str(actor), "i": str(expense_id), "o": str(org_id)},
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="expenses",
        domain_row_id=expense_id,
        action="DELETE",
        actor_user_id=actor,
    )
    db.commit()
