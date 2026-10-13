"""정책 CRUD 서비스 (11개 정책 종류 generic).

services/policy.py 는 resolve / snapshot 박제 전담. 본 파일은 CRUD + 수명주기.

안전 원칙 (raw SQL 동적 테이블/컬럼 사용 시 SQL injection 방어):
- table_name  은 KIND_TABLE 화이트리스트만 사용
- column_name 은 ALLOWED_COLUMNS 화이트리스트만 사용
- 동적 값은 항상 `text(":name")` 바인딩으로 전달

수명주기 (⚠️ 실제 동작 — 이 주석은 예전에 코드와 달랐다):

1. POST   /policies               → **바로 활성이다** (`is_active=True`).
                                     유효기간(effective_from/to)에 들어오면 곧바로 계산에 쓰인다.
2. POST   /policies/{}/approve    → `reliability_status='CONFIRMED'` + 승인자·시각 기록.
                                     **활성 여부를 바꾸지 않는다** — 이미 활성이다.
                                     "이 수치를 사람이 확인했다" 를 남기는 절차다.
3. POST   /policies/{}/deprecate  → effective_to 종료 + soft delete
   GET    /policies/{}/history    → policy_history JSON before/after

이 문서가 왜 중요한가
------------------
예전 주석은 "생성 = DRAFT(비활성), 승인해야 활성" 이라고 적혀 있었고 구현은 정반대였다.
그 문장을 믿고 만든 화면은 "승인해야 적용됩니다" 라고 안내하게 되는데,
실제로는 **저장하는 순간 계산이 바뀐다.** 수익배분 비율처럼 합이 1.0 이어야 하는
정책에서는 중간 상태가 곧바로 월 마감을 막는다(`DISTRIBUTION_RATIO_INVALID`).
승인 게이트가 필요하다고 판단되면 그건 별도 결정 사항이다 — 주석만 고쳐서 될 일이 아니다.

박제 (CLAUDE.md): version 컬럼 + effective_from/to → 과거 시점 재현 가능.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import NotFoundError, ValidationError
from app.core.ids import new_id
from app.domain.policy_crud import (
    PolicyApprove,
    PolicyCreate,
    PolicyDeprecate,
    PolicyHistoryEntry,
    PolicyKind,
    PolicyOut,
    PolicyUpdate,
)
from app.services.sale_channel import resolve_sale_channel

KIND_TABLE: dict[str, str] = {
    "marketplace_fee": "marketplace_fee_policy",
    "payment_fee": "payment_fee_policy",
    "tax": "tax_policy",
    "fulfillment": "fulfillment_fee_policy",
    "return_risk": "return_risk_policy",
    "ad_cost": "ad_cost_policy",
    "target_margin": "target_margin_policy",
    "settlement": "settlement_policy",
    "partner_share": "partner_share_policy",
    "expense_category": "expense_category_policy",
    "evidence_type": "evidence_type_policy",
}

# 종류별 추가 컬럼 화이트리스트 (M001 ERD 기준).
ALLOWED_COLUMNS: dict[str, set[str]] = {
    "marketplace_fee": {"marketplace", "category_path", "fee_type", "rate", "fixed_amount", "tier_json",
                        # M036 — 틀리면 수수료가 통째로 10% 어긋난다. NULL 은 미확인.
                        "fee_vat_included"},
    "payment_fee": {"method", "rate"},
    "tax": {"tax_type", "business_type", "vat_rate", "calculation_method", "default_vat_inclusion", "applies_to",
            # M036 — 마진 엔진이 읽는 값. NULL 은 "아직 확인하지 않았다".
            "input_vat_credit_policy", "import_vat_credit_policy"},
    "fulfillment": {"provider", "inbound_fee", "outbound_fee", "storage_fee_per_day", "return_handling_fee", "extra_json"},
    "return_risk": {"marketplace", "category_path", "return_rate", "reason_json"},
    "ad_cost": {"marketplace", "category_path", "target_roas", "default_ad_ratio"},
    "target_margin": {"category_path", "margin_type", "target_rate", "warning_lower_rate"},
    "settlement": {"loss_policy", "priority_distribution_json", "close_day"},
    "partner_share": {"partner_id", "share_ratio", "applies_year_month"},
    "expense_category": {"name", "kind", "default_tax_deductible", "default_settlement_deductible", "sort_order"},
    "evidence_type": {"code", "label", "is_official", "description"},
}

# JSONB 컬럼 → CAST 필요
JSONB_COLUMNS = {
    "tier_json", "extra_json", "reason_json", "priority_distribution_json",
}

META_COLUMNS = (
    "version", "effective_from", "effective_to",
    "source_type", "source_url", "source_checked_at",
    "approved_by", "approved_at", "reliability_status", "memo", "is_active",
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _assert_kind(kind: str) -> str:
    if kind not in KIND_TABLE:
        raise NotFoundError(f"알 수 없는 정책 종류: {kind}")
    return KIND_TABLE[kind]


def _select_columns(kind: str) -> str:
    extra = ALLOWED_COLUMNS[kind]
    cols = [
        "id", "version", "effective_from", "effective_to",
        "source_type", "source_url", "reliability_status",
        "memo", "is_active", "approved_by", "approved_at", "created_at",
    ] + sorted(extra)
    return ", ".join(cols)


def _row_to_out(kind: str, row: Any) -> PolicyOut:
    d = dict(row._mapping)
    extra = ALLOWED_COLUMNS[kind]
    fields = {k: d.pop(k, None) for k in extra}
    return PolicyOut(**d, fields=fields)


def _bind_value(col: str, value: Any) -> Any:
    # JSONB 컬럼은 호출측에서 json.dumps 처리 필요. 여기는 값 그대로.
    if col in JSONB_COLUMNS and value is not None and not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def list_policies(
    db: Session,
    *,
    org_id: UUID,
    kind: PolicyKind,
    only_active: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[PolicyOut], int]:
    table = _assert_kind(kind)
    where = ["org_id = :o", "deleted_at IS NULL"]
    if only_active:
        where.append("is_active = TRUE")
    where_sql = " AND ".join(where)

    count_row = db.execute(
        text(f"SELECT COUNT(*) AS c FROM {table} WHERE {where_sql}"),
        {"o": str(org_id)},
    ).first()
    total = int(count_row.c) if count_row else 0

    rows = db.execute(
        text(f"""
          SELECT {_select_columns(kind)} FROM {table}
           WHERE {where_sql}
           ORDER BY version DESC, effective_from DESC
           LIMIT :limit OFFSET :offset
        """),
        {"o": str(org_id), "limit": limit, "offset": offset},
    ).all()
    return [_row_to_out(kind, r) for r in rows], total


def get_active_policies(
    db: Session, *, org_id: UUID, kind: PolicyKind, at: date | None = None
) -> list[PolicyOut]:
    """effective 한 정책 row 들 (kind 가 종류별 여러 row 일 수 있음 — e.g. marketplace 별)."""
    table = _assert_kind(kind)
    at_v = at or date.today()
    rows = db.execute(
        text(f"""
          SELECT {_select_columns(kind)} FROM {table}
           WHERE org_id = :o AND deleted_at IS NULL AND is_active = TRUE
             AND effective_from <= :at
             AND (effective_to IS NULL OR effective_to >= :at)
           ORDER BY version DESC
        """),
        {"o": str(org_id), "at": at_v},
    ).all()
    return [_row_to_out(kind, r) for r in rows]


def get_policy(db: Session, *, org_id: UUID, kind: PolicyKind, policy_id: UUID) -> PolicyOut:
    table = _assert_kind(kind)
    row = db.execute(
        text(f"""
          SELECT {_select_columns(kind)} FROM {table}
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        {"i": str(policy_id), "o": str(org_id)},
    ).first()
    if row is None:
        raise NotFoundError(f"{kind} 정책을 찾을 수 없습니다.")
    return _row_to_out(kind, row)


def create_policy(
    db: Session, *, org_id: UUID, actor: UUID, kind: PolicyKind, body: PolicyCreate
) -> PolicyOut:
    table = _assert_kind(kind)
    extra_allowed = ALLOWED_COLUMNS[kind]
    bad = set(body.fields.keys()) - extra_allowed
    if bad:
        raise ValidationError(f"허용되지 않은 컬럼: {sorted(bad)}")
    if body.fields.get("marketplace"):
        resolve_sale_channel(db, org_id=org_id, code=body.fields["marketplace"])

    new_pid = new_id()
    cols = [
        "id", "org_id",
        "version", "effective_from", "effective_to",
        "source_type", "source_url", "reliability_status", "memo",
        "is_active", "created_by", "updated_by",
    ] + sorted(body.fields.keys())

    # placeholder 만들기
    placeholders: list[str] = []
    params: dict[str, Any] = {
        "id": str(new_pid),
        "org_id": str(org_id),
        "version": body.version,
        "effective_from": body.effective_from,
        "effective_to": body.effective_to,
        "source_type": body.source_type,
        "source_url": body.source_url,
        "reliability_status": body.reliability_status,
        "memo": body.memo,
        "is_active": True,
        "created_by": str(actor),
        "updated_by": str(actor),
    }
    for c in cols:
        if c in body.fields:
            params[c] = _bind_value(c, body.fields[c])
        if c in JSONB_COLUMNS:
            placeholders.append(f"CAST(:{c} AS JSONB)")
        else:
            placeholders.append(f":{c}")

    db.execute(
        text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})"),
        params,
    )

    _record_change(
        db,
        org_id=org_id,
        actor=actor,
        table=table,
        row_id=new_pid,
        version=body.version,
        action="CREATE",
        after={"kind": kind, **body.fields, "version": body.version, "effective_from": str(body.effective_from)},
        reason=None,
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table=table,
        domain_row_id=new_pid,
        action="CREATE",
        actor_user_id=actor,
        after={"kind": kind, "version": body.version},
    )
    db.commit()
    return get_policy(db, org_id=org_id, kind=kind, policy_id=new_pid)


def update_policy(
    db: Session,
    *,
    org_id: UUID,
    actor: UUID,
    kind: PolicyKind,
    policy_id: UUID,
    body: PolicyUpdate,
    _internal_approval_apply: bool = False,
) -> PolicyOut:
    table = _assert_kind(kind)
    extra_allowed = ALLOWED_COLUMNS[kind]
    before = get_policy(db, org_id=org_id, kind=kind, policy_id=policy_id)

    # ⚠️ **금액을 움직이는 필드는 이 경로로 바꿀 수 없다** (Phase A-2).
    #
    # 세율·수수료·배분 비율은 마진 → 순이익 → 수익배분으로 그대로 이어진다.
    # 승인 화면을 만들어 놓고 일반 PATCH 를 열어두면 승인은 장식이 된다
    # (상품·비용에서 이미 겪은 그대로).
    #
    # 그리고 이 경로는 **그 자리에서 UPDATE** 한다 — 승인 경로가 새 version 을
    # 만드는 것과 달리 과거 계산 근거를 덮어쓴다. 그것만으로도 막을 이유가 된다.
    if not _internal_approval_apply and body.fields:
        from app.core.exceptions import ApprovalRequiredError
        from app.services.approval_policy import (
            FINANCIAL_POLICIES,
            approval_fields_for_kind,
        )

        blocked = sorted(set(body.fields) & approval_fields_for_kind(kind))
        if blocked:
            # 섞어 보내면 **전부 거부**한다. 일부만 저장하면 무엇이 반영됐는지 알 수 없다.
            raise ApprovalRequiredError(
                f"승인이 필요한 정책 항목은 직접 수정할 수 없습니다: {blocked}. "
                "승인 요청을 만들어 주세요.",
                detail={
                    "approval_required_fields": blocked,
                    "target_type": FINANCIAL_POLICIES[kind][0],
                    "target_id": str(policy_id),
                    "request_type": FINANCIAL_POLICIES[kind][1],
                },
            )

    set_parts: list[str] = []
    params: dict[str, Any] = {"i": str(policy_id), "o": str(org_id), "now": _now(), "actor": str(actor)}

    meta = body.model_dump(exclude_unset=True, exclude={"fields"})
    for k, v in meta.items():
        set_parts.append(f"{k} = :{k}")
        params[k] = v

    field_updates: dict[str, Any] = body.fields or {}
    if field_updates:
        bad = set(field_updates.keys()) - extra_allowed
        if bad:
            raise ValidationError(f"허용되지 않은 컬럼: {sorted(bad)}")
        if field_updates.get("marketplace"):
            resolve_sale_channel(db, org_id=org_id, code=field_updates["marketplace"])
        for c, v in field_updates.items():
            if c in JSONB_COLUMNS:
                set_parts.append(f"{c} = CAST(:{c} AS JSONB)")
            else:
                set_parts.append(f"{c} = :{c}")
            params[c] = _bind_value(c, v)

    if not set_parts:
        return before

    set_parts.extend(["updated_at = :now", "updated_by = :actor"])
    db.execute(
        text(f"""
          UPDATE {table} SET {', '.join(set_parts)}
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        params,
    )

    _record_change(
        db,
        org_id=org_id,
        actor=actor,
        table=table,
        row_id=policy_id,
        version=before.version,
        action="UPDATE",
        before=before.model_dump(mode="json"),
        after={**meta, **(field_updates or {})},
        reason=None,
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table=table,
        domain_row_id=policy_id,
        action="UPDATE",
        actor_user_id=actor,
    )
    db.commit()
    return get_policy(db, org_id=org_id, kind=kind, policy_id=policy_id)


def approve_policy(
    db: Session, *, org_id: UUID, actor: UUID, kind: PolicyKind, policy_id: UUID, body: PolicyApprove
) -> PolicyOut:
    table = _assert_kind(kind)
    before = get_policy(db, org_id=org_id, kind=kind, policy_id=policy_id)
    db.execute(
        text(f"""
          UPDATE {table}
             SET reliability_status = 'CONFIRMED',
                 approved_by = :actor, approved_at = :now,
                 updated_at = :now, updated_by = :actor
           WHERE id = :i AND org_id = :o
        """),
        {"actor": str(actor), "now": _now(), "i": str(policy_id), "o": str(org_id)},
    )
    _record_change(
        db,
        org_id=org_id,
        actor=actor,
        table=table,
        row_id=policy_id,
        version=before.version,
        action="STATUS_CHANGE",
        before={"reliability_status": before.reliability_status},
        after={"reliability_status": "CONFIRMED"},
        reason=body.note,
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table=table,
        domain_row_id=policy_id,
        action="STATUS_CHANGE",
        actor_user_id=actor,
        reason=body.note,
    )
    db.commit()
    return get_policy(db, org_id=org_id, kind=kind, policy_id=policy_id)


def deprecate_policy(
    db: Session, *, org_id: UUID, actor: UUID, kind: PolicyKind, policy_id: UUID, body: PolicyDeprecate
) -> PolicyOut:
    table = _assert_kind(kind)
    before = get_policy(db, org_id=org_id, kind=kind, policy_id=policy_id)
    effective_to = body.effective_to or date.today()

    db.execute(
        text(f"""
          UPDATE {table}
             SET is_active = FALSE, effective_to = :et,
                 updated_at = :now, updated_by = :actor
           WHERE id = :i AND org_id = :o
        """),
        {"et": effective_to, "now": _now(), "actor": str(actor), "i": str(policy_id), "o": str(org_id)},
    )
    _record_change(
        db,
        org_id=org_id,
        actor=actor,
        table=table,
        row_id=policy_id,
        version=before.version,
        action="STATUS_CHANGE",
        before={"is_active": True, "effective_to": str(before.effective_to) if before.effective_to else None},
        after={"is_active": False, "effective_to": str(effective_to)},
        reason=body.reason,
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table=table,
        domain_row_id=policy_id,
        action="DELETE",  # enum 제약상 DEPRECATE 가 없어 DELETE 매핑
        actor_user_id=actor,
        reason=body.reason,
    )
    db.commit()
    return get_policy(db, org_id=org_id, kind=kind, policy_id=policy_id)


def list_history(
    db: Session, *, org_id: UUID, kind: PolicyKind, policy_id: UUID
) -> list[PolicyHistoryEntry]:
    table = _assert_kind(kind)
    rows = db.execute(
        text("""
          SELECT id, version, change_action, before_json, after_json, reason,
                 changed_by, changed_at
            FROM policy_change_history
           WHERE org_id = :o AND policy_table = :t AND policy_row_id = :i
           ORDER BY changed_at DESC
        """),
        {"o": str(org_id), "t": table, "i": str(policy_id)},
    ).all()
    return [PolicyHistoryEntry(**r._mapping) for r in rows]


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------


def _record_change(
    db: Session,
    *,
    org_id: UUID,
    actor: UUID,
    table: str,
    row_id: UUID,
    version: int,
    action: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
) -> None:
    db.execute(
        text("""
          INSERT INTO policy_change_history (
            org_id, policy_table, policy_row_id, version, change_action,
            before_json, after_json, reason, changed_by
          ) VALUES (
            :o, :t, :i, :v, :a,
            CAST(:b AS JSONB), CAST(:af AS JSONB), :r, :u
          )
        """),
        {
            "o": str(org_id),
            "t": table,
            "i": str(row_id),
            "v": version,
            "a": action,
            "b": json.dumps(before, ensure_ascii=False, default=str) if before else None,
            "af": json.dumps(after, ensure_ascii=False, default=str) if after else None,
            "r": reason,
            "u": str(actor),
        },
    )
