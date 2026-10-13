"""상품 소싱 후보 CRUD 서비스 (docs/22_api_spec.md §4).

상태 전이:
  CANDIDATE → APPROVED  (approve_sourcing)
  CANDIDATE → REJECTED  (reject_sourcing, rejected_reason 필수)

multi-tenant 격리: 모든 쿼리에 org_id = :o 조건 필수.
soft-delete: deleted_at IS NULL 조건으로 논리 삭제 구분.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import ConflictError, NotFoundError
from app.core.ids import new_id
from app.domain.sourcing import (
    SourcingCandidateCreate,
    SourcingCandidateOut,
    SourcingCandidateUpdate,
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


_SELECT = """
  SELECT id, product_id,
         supplier_url, supplier_name_snapshot,
         supply_price, moq, category_path,
         candidate_status::text AS candidate_status,
         rejected_reason, memo,
         created_at
    FROM product_sourcing_candidate
"""


def _row_to_out(row: Any) -> SourcingCandidateOut:
    return SourcingCandidateOut(**row._mapping)


def list_sourcing(
    db: Session,
    *,
    org_id: UUID,
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    where = ["org_id = :o", "deleted_at IS NULL"]
    params: dict[str, Any] = {"o": str(org_id)}

    if status:
        where.append("candidate_status = CAST(:status AS enum_sourcing_status)")
        params["status"] = status

    where_sql = " AND ".join(where)
    offset = (page - 1) * page_size

    count_row = db.execute(
        text(f"SELECT COUNT(*) AS c FROM product_sourcing_candidate WHERE {where_sql}"),
        params,
    ).first()
    total = int(count_row.c) if count_row else 0

    rows = db.execute(
        text(f"""
          {_SELECT}
           WHERE {where_sql}
           ORDER BY created_at DESC
           LIMIT :limit OFFSET :offset
        """),
        {**params, "limit": page_size, "offset": offset},
    ).all()

    return {
        "items": [_row_to_out(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _get_raw(db: Session, *, org_id: UUID, item_id: UUID) -> SourcingCandidateOut:
    row = db.execute(
        text(f"{_SELECT} WHERE id = :i AND org_id = :o AND deleted_at IS NULL"),
        {"i": str(item_id), "o": str(org_id)},
    ).first()
    if row is None:
        raise NotFoundError("소싱 후보를 찾을 수 없습니다.")
    return _row_to_out(row)


def create_sourcing(
    db: Session, *, org_id: UUID, actor: UUID, body: SourcingCandidateCreate
) -> SourcingCandidateOut:
    new_cid = new_id()
    db.execute(
        text("""
          INSERT INTO product_sourcing_candidate (
            id, org_id, product_id,
            supplier_url, supplier_name_snapshot,
            supply_price, moq, category_path,
            candidate_status, memo,
            created_by, updated_by
          ) VALUES (
            :id, :o, :product_id,
            :url, :snap_name,
            :supply_price, :moq, :category_path,
            'CANDIDATE', :memo,
            :actor, :actor
          )
        """),
        {
            "id": str(new_cid),
            "o": str(org_id),
            "product_id": str(body.product_id) if body.product_id else None,
            "url": body.supplier_url,
            "snap_name": body.supplier_name_snapshot,
            "supply_price": body.supply_price,
            "moq": body.moq,
            "category_path": body.category_path,
            "memo": body.memo,
            "actor": str(actor),
        },
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="product_sourcing_candidate",
        domain_row_id=new_cid,
        action="CREATE",
        actor_user_id=actor,
        after={
            "supplier_url": body.supplier_url,
            "supply_price": body.supply_price,
            "candidate_status": "CANDIDATE",
        },
    )
    db.commit()
    return _get_raw(db, org_id=org_id, item_id=new_cid)


def update_sourcing(
    db: Session, *, org_id: UUID, actor: UUID, item_id: UUID, body: SourcingCandidateUpdate
) -> SourcingCandidateOut:
    before = _get_raw(db, org_id=org_id, item_id=item_id)

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return before

    set_parts: list[str] = []
    params: dict[str, Any] = {
        "i": str(item_id),
        "o": str(org_id),
        "now": _now(),
        "actor": str(actor),
    }
    for k, v in fields.items():
        set_parts.append(f"{k} = :{k}")
        params[k] = str(v) if isinstance(v, UUID) else v

    set_parts.extend(["updated_at = :now", "updated_by = :actor"])

    db.execute(
        text(f"""
          UPDATE product_sourcing_candidate SET {', '.join(set_parts)}
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        params,
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="product_sourcing_candidate",
        domain_row_id=item_id,
        action="UPDATE",
        actor_user_id=actor,
        before=before.model_dump(mode="json"),
        after=fields,
    )
    db.commit()
    return _get_raw(db, org_id=org_id, item_id=item_id)


def approve_sourcing(
    db: Session, *, org_id: UUID, actor: UUID, item_id: UUID
) -> SourcingCandidateOut:
    before = _get_raw(db, org_id=org_id, item_id=item_id)
    if before.candidate_status == "APPROVED":
        raise ConflictError("이미 승인된 소싱 후보입니다.")

    db.execute(
        text("""
          UPDATE product_sourcing_candidate
             SET candidate_status = 'APPROVED',
                 rejected_reason = NULL,
                 updated_at = :now, updated_by = :actor
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        {"now": _now(), "actor": str(actor), "i": str(item_id), "o": str(org_id)},
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="product_sourcing_candidate",
        domain_row_id=item_id,
        action="STATUS_CHANGE",
        actor_user_id=actor,
        before={"candidate_status": before.candidate_status},
        after={"candidate_status": "APPROVED"},
    )
    db.commit()
    return _get_raw(db, org_id=org_id, item_id=item_id)


def reject_sourcing(
    db: Session, *, org_id: UUID, actor: UUID, item_id: UUID, reason: str
) -> SourcingCandidateOut:
    before = _get_raw(db, org_id=org_id, item_id=item_id)
    if before.candidate_status == "REJECTED":
        raise ConflictError("이미 거절된 소싱 후보입니다.")

    db.execute(
        text("""
          UPDATE product_sourcing_candidate
             SET candidate_status = 'REJECTED',
                 rejected_reason = :reason,
                 updated_at = :now, updated_by = :actor
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        {
            "reason": reason,
            "now": _now(),
            "actor": str(actor),
            "i": str(item_id),
            "o": str(org_id),
        },
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="product_sourcing_candidate",
        domain_row_id=item_id,
        action="STATUS_CHANGE",
        actor_user_id=actor,
        before={"candidate_status": before.candidate_status},
        after={"candidate_status": "REJECTED", "rejected_reason": reason},
    )
    db.commit()
    return _get_raw(db, org_id=org_id, item_id=item_id)


def delete_sourcing(db: Session, *, org_id: UUID, actor: UUID, item_id: UUID) -> None:
    _get_raw(db, org_id=org_id, item_id=item_id)  # 존재 확인

    db.execute(
        text("""
          UPDATE product_sourcing_candidate
             SET deleted_at = :now, updated_by = :actor
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        {"now": _now(), "actor": str(actor), "i": str(item_id), "o": str(org_id)},
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="product_sourcing_candidate",
        domain_row_id=item_id,
        action="DELETE",
        actor_user_id=actor,
    )
    db.commit()
