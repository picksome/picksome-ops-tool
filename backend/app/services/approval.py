"""승인 워크플로 서비스 (M018, docs/43 §8).

상태 머신
---------
    DRAFT ──submit──> PENDING ──approve──> APPROVED ──apply──> APPLIED
      │                  ├──reject──> REJECTED
      └──cancel──> CANCELLED  (PENDING/APPROVED/REJECTED 에서도 취소 가능)

설계 판단
---------
**APPROVED 와 APPLIED 를 분리한다.** 승인은 사람의 판단이고, 적용은 데이터 변경이다.
승인 후 적용까지 사이에 대상이 바뀌거나 월이 잠길 수 있다. 그래서 **적용 시점에
모든 조건을 다시 검증**한다. 적용에 실패해도 승인 사실은 지우지 않는다 —
`status` 는 APPROVED 로 남고 `apply_error` 에 사유가 들어간다.
(43 §8-3 의 결정. 실패를 별도 상태로 만들지 않는다.)

**월 잠금은 승인으로 우회되지 않는다.** 적용은 `APPLY_APPROVAL` 이라는
일반 사용자 쓰기 연산으로 `assert_period_writable` 를 통과해야 한다.
승인만으로 잠긴 달을 바꿀 수 있으면 잠금이 의미를 잃는다.

**SAVEPOINT 로 거부를 격리한다.** 서비스가 요청 트랜잭션을 소유하지 않으므로
(deps.get_db 는 세션만 넘긴다) 거부 경로에서 `db.rollback()` 을 부르면 같은 요청의
무관한 변경까지 되돌린다. monthly_close 와 같은 패턴을 쓴다.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db_errors import translate_integrity_error
from app.core.exceptions import (
    ApprovalInvalidTransitionError,
    ApprovalOpenRequestExistsError,
    ApprovalPermissionDeniedError,
    ApprovalSelfApprovalForbiddenError,
    ApprovalVersionConflictError,
    ConflictError,
    ForbiddenError,
    IdempotencyKeyReusedError,
    NotFoundError,
    ValidationError,
)
from app.core.month_lock import assert_period_writable
from app.domain.approval import ProposedChanges, canonical_fingerprint
from app.services import approval_targets as targets
from app.services import notification_events

# 허용 전이표. 여기 없는 전이는 409.
_ALLOWED: dict[str, tuple[str, ...]] = {
    "DRAFT": ("PENDING", "CANCELLED"),
    "PENDING": ("APPROVED", "REJECTED", "CANCELLED"),
    "APPROVED": ("APPLIED", "CANCELLED"),   # 취소는 적용 전까지만
    "REJECTED": ("CANCELLED",),
    "APPLIED": (),                          # 종착. 되돌리려면 새 요청
    "CANCELLED": (),
}

# 승인 권한 (docs/43 §8-3). 새 역할을 만들지 않고 기존 RBAC 6종을 쓴다.
_APPROVE_ROLES: dict[str, frozenset[str]] = {
    "product": frozenset({"ADMIN", "OPERATOR"}),
    # 가격은 마진을 직접 바꾼다 — 운영자와 관리자가 판단한다.
    "product_channel_listing": frozenset({"ADMIN", "OPERATOR"}),
    # 사업비용은 세무·정산 담당의 업무 대상이다. 소싱·운영은 승인 주체가 아니다.
    "expense": frozenset({"ADMIN", "ACCOUNTING"}),
    # 월 재오픈은 **확정한 숫자를 무효화**한다. 파트너 배분이 이미 계산됐을 수
    # 있으므로 ACCOUNTING 도 단독으로 결정하지 않는다. ADMIN 만.
    "monthly_close": frozenset({"ADMIN"}),
    # 재무 정책(Phase A-2). 세율·수수료·배분 비율은 계산의 입력값이고 그 결과가
    # 파트너에게 실제로 나가는 금액이다. 세무·정산 담당과 관리자가 판단한다 —
    # 운영·소싱은 승인 주체가 아니다.
    "policy_tax": frozenset({"ADMIN", "ACCOUNTING"}),
    "policy_marketplace_fee": frozenset({"ADMIN", "ACCOUNTING"}),
    "policy_payment_fee": frozenset({"ADMIN", "ACCOUNTING"}),
    # 배분 비율은 누가 얼마를 가져가는지를 정한다. ADMIN 만.
    "policy_partner_share": frozenset({"ADMIN"}),
    "policy_settlement": frozenset({"ADMIN"}),
}
_DEFAULT_APPROVE_ROLES = frozenset({"ADMIN"})


def _now() -> datetime:
    return datetime.now(tz=UTC)


# ──────────────────────────────────────────────────────────────
# 승인 정책 (조직 설정)
# ──────────────────────────────────────────────────────────────

def load_approval_policy(db: Session, org_id: UUID, target_type: str) -> dict[str, Any]:
    """조직 설정에서 승인 정책을 읽는다.

    ⚠️ 임계값들은 **미결정**이다(docs/43 §8-4). 기본값을 두되 그게 정답이라고
    가정하지 않는다. 사업 구조를 알아야 정할 수 있는 값이라 설정으로 남긴다.
    """
    settings = db.execute(
        text("SELECT settings_json FROM organization WHERE id = :org"),
        {"org": str(org_id)},
    ).scalar() or {}
    approval = (settings.get("approval") or {}) if isinstance(settings, dict) else {}

    return {
        # 자기 승인 — 기본 금지. 1인 사업자는 이걸 켜야 운영이 된다.
        "self_approve_allowed": bool(approval.get("self_approve_allowed", False)),
        "required_roles": sorted(_APPROVE_ROLES.get(target_type, _DEFAULT_APPROVE_ROLES)),
        # 아래는 전부 unresolved. 화면·서비스가 "미확정" 으로 취급해야 한다.
        "thresholds": {
            "cost_change_threshold_rate": approval.get("cost_change_threshold_rate"),
            "price_change_threshold_rate": approval.get("price_change_threshold_rate"),
            "min_margin_rate_for_auto": approval.get("min_margin_rate_for_auto"),
            "expense_approval_threshold": approval.get("expense_approval_threshold"),
        },
        "thresholds_resolved": bool(approval),
        "policy_source": "organization.settings_json.approval" if approval else "default",
    }


# ──────────────────────────────────────────────────────────────
# 조회
# ──────────────────────────────────────────────────────────────

_SELECT = """
    SELECT id, business_key, org_id, target_type, target_id, request_type, status,
           requested_by, requested_at, submitted_at, reviewed_by, reviewed_at,
           applied_by, applied_at, cancelled_at,
           reason, review_comment, apply_error,
           before_snapshot_json, proposed_changes_json, approval_policy_snapshot_json,
           base_version, effective_from, version, idempotency_key,
           request_fingerprint, attempt_count, last_attempt_at,
           last_error_code, last_error_message, next_retry_at,
           auto_apply_status, applied_by_type, decision_outcome,
           created_at, updated_at
      FROM approval_request
"""


def get_request(db: Session, *, org_id: UUID, request_id: UUID, for_update: bool = False) -> dict:
    row = db.execute(
        text(f"{_SELECT} WHERE id = :id AND org_id = :org AND deleted_at IS NULL"
             + (" FOR UPDATE" if for_update else "")),
        {"id": str(request_id), "org": str(org_id)},
    ).mappings().first()
    if row is None:
        # 조직 격리: 다른 org 의 요청도 404. 403 으로 구분하면 ID 존재가 새어나간다.
        raise NotFoundError("승인 요청을 찾을 수 없습니다.")
    return dict(row)


def list_requests(
    db: Session, *, org_id: UUID,
    status: str | None = None, decision_outcome: str | None = None,
    target_type: str | None = None,
    target_id: UUID | None = None, request_type: str | None = None,
    requested_by: UUID | None = None, reviewed_by: UUID | None = None,
    pending_for_me: UUID | None = None, q: str | None = None,
    from_dt: date | None = None, to_dt: date | None = None,
    limit: int = 50, offset: int = 0,
) -> tuple[list[dict], int]:
    where = ["org_id = :org", "deleted_at IS NULL"]
    params: dict[str, Any] = {"org": str(org_id)}

    if status:
        where.append("status = :status")
        params["status"] = status
    if decision_outcome:
        where.append("decision_outcome = :dc")
        params["dc"] = decision_outcome
    if target_type:
        where.append("target_type = :tt")
        params["tt"] = target_type
    if target_id:
        where.append("target_id = :tid")
        params["tid"] = str(target_id)
    if request_type:
        where.append("request_type = :rt")
        params["rt"] = request_type
    if requested_by:
        where.append("requested_by = :rb")
        params["rb"] = str(requested_by)
    if reviewed_by:
        where.append("reviewed_by = :vb")
        params["vb"] = str(reviewed_by)
    if from_dt:
        where.append("requested_at >= :fd")
        params["fd"] = from_dt
    if to_dt:
        where.append("requested_at < (CAST(:td AS DATE) + 1)")
        params["td"] = to_dt
    if pending_for_me:
        # 내가 승인해야 할 것 = PENDING 이고 내가 요청자가 아닌 건
        # (자기 승인 허용 조직이면 서비스가 승인 시점에 다시 판단한다)
        where.append("status = 'PENDING'")
    if q:
        where.append("(business_key ILIKE :q OR reason ILIKE :q OR request_type ILIKE :q)")
        params["q"] = f"%{q}%"

    where_sql = " AND ".join(where)
    total = int(db.execute(
        text(f"SELECT COUNT(*) FROM approval_request WHERE {where_sql}"), params
    ).scalar() or 0)

    rows = db.execute(
        text(f"{_SELECT} WHERE {where_sql} ORDER BY requested_at DESC, id DESC "
             "LIMIT :lim OFFSET :off"),
        {**params, "lim": limit, "off": offset},
    ).mappings().all()
    return [dict(r) for r in rows], total


# ──────────────────────────────────────────────────────────────
# 생성
# ──────────────────────────────────────────────────────────────

def list_open_approvals_for_target(
    db: Session, *, org_id: UUID, target_type: str, target_id: UUID
) -> list[dict[str, Any]]:
    """이 대상에 걸려 있는 진행 중 승인 요청 요약.

    상품 상세 응답에 실어 보낸다. 이게 없으면 사용자가 승인 요청 버튼을 눌러서야
    "이미 열린 요청이 있다"(409)를 알게 된다 — 입력을 다 끝낸 뒤에.
    화면이 미리 알고 "기존 요청 보기" 로 유도할 수 있어야 한다.

    민감값은 싣지 않는다. 이건 요약이지 diff 가 아니다 — 값이 필요하면
    승인 상세 API 가 권한에 따라 redaction 을 거쳐 내려준다.
    """
    rows = db.execute(text("""
        SELECT id, business_key, request_type, status, requested_at, requested_by,
               effective_from, auto_apply_status
          FROM approval_request
         WHERE org_id = :o AND target_type = :tt AND target_id = :ti
           AND deleted_at IS NULL
           AND status IN ('DRAFT','PENDING','APPROVED')
         ORDER BY requested_at DESC, id DESC
    """), {"o": str(org_id), "tt": target_type, "ti": str(target_id)}).mappings().all()
    return [dict(r) for r in rows]


def _find_open_request_id(
    db: Session, org_id: UUID, target_type: str, target_id: UUID, request_type: str
) -> str | None:
    """충돌한 기존 열린 요청의 id. 사용자가 바로 그 요청으로 이동할 수 있게 돌려준다."""
    row = db.execute(text("""
        SELECT id FROM approval_request
         WHERE org_id = :o AND target_type = :tt AND target_id = :ti
           AND request_type = :rt AND deleted_at IS NULL
           AND status IN ('DRAFT','PENDING','APPROVED')
         LIMIT 1
    """), {"o": str(org_id), "tt": target_type, "ti": str(target_id), "rt": request_type}).first()
    return str(row.id) if row else None


def _next_business_key(db: Session, org_id: UUID) -> str:
    row = db.execute(text("""
        SELECT business_key FROM approval_request
         WHERE org_id = :o AND business_key ~ '^APR-[0-9]{6}$'
         ORDER BY business_key DESC LIMIT 1
    """), {"o": str(org_id)}).first()
    nxt = int(row.business_key.split("-")[1]) + 1 if row else 1
    return f"APR-{nxt:06d}"


def create_request(
    db: Session, *, org_id: UUID, actor_user_id: UUID | None,
    target_type: str, target_id: UUID, request_type: str,
    proposed_changes: dict[str, Any], reason: str | None = None,
    effective_from: date | None = None, submit: bool = False,
    idempotency_key: str | None = None, request_id: str | None = None,
    service_account_id: UUID | None = None,
) -> dict:
    """승인 요청 생성.

    생성 시점에 대상을 읽어 `before_snapshot` 과 `base_version` 을 박제한다.
    적용 직전에 이 지문을 다시 비교해, 그 사이 누가 대상을 바꿨으면 거부한다.

    요청자는 사람이거나 서비스 계정이다 (M046)
    ---------------------------------------
    에이전트가 올린 제안을 사람 이름으로 남기지 않는다. 승인함에 "김대표가
    요청함" 으로 뜨면 승인자가 틀린 전제 위에서 판단한다. 둘 중 하나는 반드시
    있어야 하고, DB CHECK 가 그걸 강제한다.
    """
    if actor_user_id is None and service_account_id is None:
        raise ValidationError("요청자가 필요합니다.")
    adapter = targets.get_adapter(target_type)

    if request_type not in adapter.request_types:
        raise ValidationError(
            f"이 대상에서 지원하지 않는 요청 유형입니다: {request_type}",
            detail={"supported": sorted(adapter.request_types)},
        )

    # 입력 검증은 service 진입점에서 한다. 라우터만 검증하면
    # service 를 직접 부르는 경로(자동 적용기, 배치, 테스트)가 뚫린다.
    # 2단계 검증.
    #  ① 전체 allowlist 로 "아예 승인 대상이 아닌 필드" 를 거른다
    #  ② 유형 소유권으로 "다른 유형이 소유한 필드" 를 거른다
    #     — 나누는 이유는 오류 메시지가 달라야 하기 때문이다.
    #       전자는 "바꿀 수 없는 필드", 후자는 "이 요청 유형으로는 못 바꾼다(소유자는 X)".
    proposed_changes = ProposedChanges.validate_payload(
        proposed_changes, allowed_fields=adapter.allowed_fields
    )
    adapter.validate_proposed_changes(proposed_changes, request_type=request_type)

    fingerprint = canonical_fingerprint(
        target_type=target_type, target_id=target_id, request_type=request_type,
        proposed_changes=proposed_changes, effective_from=effective_from,
    )

    # 멱등성: 같은 키 + 같은 내용이면 기존 요청을 돌려준다.
    # 같은 키 + 다른 내용이면 409 - 조용히 무시하면 클라이언트는 자기 요청이
    # 반영됐다고 믿는다. 유일 범위가 (org_id, idempotency_key) 라 조직이 다르면 충돌하지 않는다.
    if idempotency_key:
        existing = db.execute(
            text(f"{_SELECT} WHERE org_id = :org AND idempotency_key = :k AND deleted_at IS NULL"),
            {"org": str(org_id), "k": idempotency_key},
        ).mappings().first()
        if existing:
            if existing["request_fingerprint"] and existing["request_fingerprint"] != fingerprint:
                raise IdempotencyKeyReusedError(
                    "같은 요청 키로 다른 내용의 요청이 들어왔습니다. "
                    "새 요청에는 새 키를 사용하세요.",
                    detail={"existing_request_id": str(existing["id"]),
                            "idempotency_key": idempotency_key},
                )
            return dict(existing)

    adapter.validate_target(db, org_id=org_id, target_id=target_id)

    snapshot = adapter.build_snapshot(db, org_id=org_id, target_id=target_id)
    base_version = targets.version_of(snapshot)
    policy = load_approval_policy(db, org_id, target_type)

    status = "PENDING" if submit else "DRAFT"
    new_id = db.execute(text("SELECT gen_random_uuid()")).scalar()
    business_key = _next_business_key(db, org_id)

    try:
        db.execute(text("""
            INSERT INTO approval_request
              (id, org_id, business_key, target_type, target_id, request_type, status,
               requested_by, requested_by_type, requested_by_service_account_id,
               submitted_at, reason,
               before_snapshot_json, proposed_changes_json, approval_policy_snapshot_json,
               base_version, effective_from, idempotency_key, request_fingerprint,
               created_by, updated_by)
            VALUES
              (:id, :org, :bk, :tt, :tid, :rt, :st,
               :actor, :actor_type, :sa,
               CASE WHEN :st = 'PENDING' THEN now() ELSE NULL END, :reason,
               CAST(:snap AS JSONB), CAST(:prop AS JSONB), CAST(:pol AS JSONB),
               :bv, :eff, :ik, :fp, :actor, :actor)
        """), {
            "id": str(new_id), "org": str(org_id), "bk": business_key,
            "tt": target_type, "tid": str(target_id), "rt": request_type, "st": status,
            "actor": str(actor_user_id) if actor_user_id else None,
            "actor_type": "USER" if actor_user_id else "SERVICE_ACCOUNT",
            "sa": str(service_account_id) if service_account_id else None,
            "reason": reason,
            "snap": json.dumps(snapshot, ensure_ascii=False, default=str),
            "prop": json.dumps(proposed_changes, ensure_ascii=False, default=str),
            "pol": json.dumps(policy, ensure_ascii=False, default=str),
            "bv": base_version, "eff": effective_from, "ik": idempotency_key,
            "fp": fingerprint,
        })
    except Exception as exc:  # noqa: BLE001
        # 예외 메시지 문자열로 제약조건을 찾지 않는다. PostgreSQL 로케일에 따라
        # 메시지가 바뀌고(이 환경은 한국어), 다른 위반이 우연히 같은 문자열을
        # 포함하면 오분류된다. psycopg 의 diag.constraint_name 을 쓴다.
        db.rollback()
        domain = translate_integrity_error(exc, {
            "ux_approval_open_target": lambda: ApprovalOpenRequestExistsError(
                "이 대상에는 이미 진행 중인 승인 요청이 있습니다. "
                "기존 요청을 확인한 후 적용하거나 취소해 주세요.",
                detail={
                    "existing_request_id": _find_open_request_id(
                        db, org_id, target_type, target_id, request_type),
                    "target_type": target_type,
                    "target_id": str(target_id),
                    "request_type": request_type,
                },
            ),
            "ux_approval_idempotency": lambda: IdempotencyKeyReusedError(
                "같은 요청 키가 이미 사용되었습니다.",
                detail={"idempotency_key": idempotency_key},
            ),
        })
        if domain is not None:
            raise domain from exc
        # 알 수 없는 무결성 오류는 삼키지 않는다 - 409 로 뭉개면 진짜 버그가 숨는다.
        raise

    record_audit(
        db, org_id=org_id, domain_table="approval_request", domain_row_id=new_id,
        action="CREATE", actor_user_id=actor_user_id,
        before=None, after={"status": status, "target_type": target_type,
                            "target_id": str(target_id), "request_type": request_type},
        reason=reason, request_id=request_id,
    )
    db.commit()

    out = get_request(db, org_id=org_id, request_id=new_id)

    # `submit=True` 로 만든 요청은 **`submit()` 을 거치지 않는다** — 여기서 바로
    # PENDING 으로 들어간다. 그래서 알림도 여기서 보내야 한다.
    # (`submit()` 쪽에도 같은 호출이 있지만 dedupe 키가 같아 두 번 생기지 않는다.)
    if status == "PENDING":
        notification_events.approval_requested(
            db, org_id=org_id, approval_id=new_id,
            business_key=str(out.get("business_key") or new_id),
            request_type=request_type,
            requester_name=_actor_name(
                db, org_id=org_id, user_id=actor_user_id,
                service_account_id=service_account_id,
            ),
        )
        db.commit()

    return out


def update_draft(
    db: Session, *, org_id: UUID, actor_user_id: UUID, request_id: UUID,
    proposed_changes: dict[str, Any] | None = None, reason: str | None = None,
    effective_from: date | None = None, correlation_id: str | None = None,
) -> dict:
    """DRAFT 만 수정할 수 있다.

    PENDING 이후에는 proposal 이 immutable 이다 — 승인자가 본 내용과 적용되는 내용이
    달라지면 승인 자체가 무의미해진다. 바꾸려면 취소하고 새로 만든다.
    """
    current = get_request(db, org_id=org_id, request_id=request_id)
    if current["status"] != "DRAFT":
        raise ConflictError(
            f"{current['status']} 상태의 요청은 수정할 수 없습니다. "
            "취소한 뒤 새 요청을 만드세요.",
            detail={"status": current["status"]},
        )
    if current["requested_by"] != actor_user_id:
        raise ForbiddenError("본인이 만든 요청만 수정할 수 있습니다.")

    sets, params = [], {"id": str(request_id), "org": str(org_id), "actor": str(actor_user_id)}
    if proposed_changes is not None:
        _adapter = targets.get_adapter(current["target_type"])
        proposed_changes = ProposedChanges.validate_payload(
            proposed_changes, allowed_fields=_adapter.allowed_fields
        )
        _adapter.validate_proposed_changes(
            proposed_changes, request_type=current["request_type"]
        )
        sets.append("proposed_changes_json = CAST(:prop AS JSONB)")
        params["prop"] = json.dumps(proposed_changes, ensure_ascii=False, default=str)
    if reason is not None:
        sets.append("reason = :reason")
        params["reason"] = reason
    if effective_from is not None:
        sets.append("effective_from = :eff")
        params["eff"] = effective_from
    if not sets:
        return current

    db.execute(text(f"""
        UPDATE approval_request SET {', '.join(sets)},
               version = version + 1, updated_at = now(), updated_by = :actor
         WHERE id = :id AND org_id = :org
    """), params)
    record_audit(
        db, org_id=org_id, domain_table="approval_request", domain_row_id=request_id,
        action="UPDATE", actor_user_id=actor_user_id,
        before={"proposed_changes": current["proposed_changes_json"]},
        after={"proposed_changes": proposed_changes}, reason=reason,
        request_id=correlation_id,
    )
    db.commit()
    return get_request(db, org_id=org_id, request_id=request_id)


# ──────────────────────────────────────────────────────────────
# 상태 전이
# ──────────────────────────────────────────────────────────────

def _transition(
    db: Session, *, org_id: UUID, request_id: UUID, to_status: str,
    actor_user_id: UUID, actor_role: str,
    reason: str | None = None, correlation_id: str | None = None,
    extra_sql: str = "", extra_params: dict[str, Any] | None = None,
    skip_audit: bool = False,
) -> dict:
    """행 잠금 + compare-and-swap 으로 직렬화된 상태 전이.

    거부 시 SAVEPOINT 만 되돌린다 — 상위 트랜잭션의 무관한 변경을 건드리지 않는다.
    """
    savepoint = db.begin_nested()
    current = get_request(db, org_id=org_id, request_id=request_id, for_update=True)
    from_status = current["status"]

    if to_status not in _ALLOWED.get(from_status, ()):
        savepoint.rollback()
        raise ApprovalInvalidTransitionError(
            f"{from_status} 상태에서 {to_status} 로 바꿀 수 없습니다.",
            detail={"from": from_status, "to": to_status,
                    "allowed": list(_ALLOWED.get(from_status, ()))},
        )

    result = db.execute(text(f"""
        UPDATE approval_request
           SET status = :to, version = version + 1,
               updated_at = now(), updated_by = :actor
               {extra_sql}
         WHERE id = :id AND org_id = :org
           AND status = :expected AND version = :expected_version
    """), {
        "to": to_status, "actor": str(actor_user_id), "id": str(request_id),
        "org": str(org_id), "expected": from_status,
        "expected_version": current["version"], **(extra_params or {}),
    })
    if result.rowcount == 0:
        savepoint.rollback()
        raise ApprovalVersionConflictError(
            "다른 요청이 먼저 이 승인 건의 상태를 바꿨습니다. 다시 조회한 뒤 시도하세요.",
            detail={"expected_from": from_status, "attempted_to": to_status},
        )

    if not skip_audit:
        record_audit(
            db, org_id=org_id, domain_table="approval_request", domain_row_id=request_id,
            action="STATUS_CHANGE", actor_user_id=actor_user_id,
            before={"status": from_status},
            after={"status": to_status, "target_type": current["target_type"],
                   "target_id": str(current["target_id"]),
                   "idempotency_key": current["idempotency_key"]},
            reason=reason, request_id=correlation_id,
        )
    savepoint.commit()
    _ = actor_role
    return current


def submit(db, *, org_id, request_id, actor_user_id, actor_role, correlation_id=None):
    current = _transition(
        db, org_id=org_id, request_id=request_id, to_status="PENDING",
        actor_user_id=actor_user_id, actor_role=actor_role, reason="승인 요청 제출",
        correlation_id=correlation_id, extra_sql=", submitted_at = now()",
    )
    if current["requested_by"] != actor_user_id:
        db.rollback()
        raise ForbiddenError("본인이 만든 요청만 제출할 수 있습니다.")
    db.commit()

    out = get_request(db, org_id=org_id, request_id=request_id)
    # 상신 사실을 승인 권한자에게 알린다. **커밋 뒤에** 부른다 —
    # 알림 생성이 실패해도 제출은 이미 확정돼 있어야 한다.
    notification_events.approval_requested(
        db, org_id=org_id, approval_id=request_id,
        business_key=str(out.get("business_key") or request_id),
        request_type=str(out.get("request_type") or "승인 요청"),
        requester_name=_actor_name(db, org_id=org_id, user_id=actor_user_id),
    )
    db.commit()
    return out


def _assert_can_review(
    db: Session, org_id: UUID, current: dict, actor_user_id: UUID, actor_role: str
) -> None:
    """승인·반려 권한 + 자기 승인 정책.

    정책은 **요청 시점에 박제된 것이 아니라 지금 것**을 본다 —
    권한이 회수된 사람이 예전 정책으로 승인하면 안 된다.
    박제본(approval_policy_snapshot_json)은 "그때 무슨 규칙이었나" 를 남기는 용도다.
    """
    policy = load_approval_policy(db, org_id, current["target_type"])
    if actor_role not in policy["required_roles"]:
        raise ApprovalPermissionDeniedError(
            f"승인 권한이 없습니다. 필요한 역할: {policy['required_roles']}",
            detail={"actor_role": actor_role},
        )
    if current["requested_by"] == actor_user_id and not policy["self_approve_allowed"]:
        raise ApprovalSelfApprovalForbiddenError(
            "본인이 요청한 건은 승인할 수 없습니다. "
            "조직 설정에서 self_approve_allowed 를 켜면 허용됩니다.",
        )


def approve(db, *, org_id, request_id, actor_user_id, actor_role,
            comment=None, correlation_id=None):
    savepoint_owner = get_request(db, org_id=org_id, request_id=request_id)
    _assert_can_review(db, org_id, savepoint_owner, actor_user_id, actor_role)
    _transition(
        db, org_id=org_id, request_id=request_id, to_status="APPROVED",
        actor_user_id=actor_user_id, actor_role=actor_role,
        reason=comment or "승인", correlation_id=correlation_id,
        extra_sql=(", reviewed_by = :rb, reviewed_at = now(), review_comment = :rc,"
                   " decision_outcome = 'APPROVED'"),
        extra_params={"rb": str(actor_user_id), "rc": comment},
    )
    db.commit()
    return get_request(db, org_id=org_id, request_id=request_id)


def reject(db, *, org_id, request_id, actor_user_id, actor_role,
           comment: str, correlation_id=None):
    if not comment or not comment.strip():
        raise ValidationError("반려에는 사유가 필요합니다.")
    current = get_request(db, org_id=org_id, request_id=request_id)
    _assert_can_review(db, org_id, current, actor_user_id, actor_role)
    _transition(
        db, org_id=org_id, request_id=request_id, to_status="REJECTED",
        actor_user_id=actor_user_id, actor_role=actor_role,
        reason=comment, correlation_id=correlation_id,
        # 결정 결과를 status 와 별도로 박제한다. 나중에 CANCELLED 로 아카이빙돼도
        # 반려였다는 사실이 유지된다 — 반려 통계는 이 컬럼으로 센다(M020).
        extra_sql=(", reviewed_by = :rb, reviewed_at = now(), review_comment = :rc,"
                   " decision_outcome = 'REJECTED'"),
        extra_params={"rb": str(actor_user_id), "rc": comment},
    )
    db.commit()

    out = get_request(db, org_id=org_id, request_id=request_id)
    # 반려는 **요청자**가 알아야 한다. 승인자에게 보내면 자기가 방금 누른 버튼을
    # 다시 알려주는 꼴이다.
    notification_events.approval_rejected(
        db, org_id=org_id, approval_id=request_id,
        business_key=str(out.get("business_key") or request_id),
        requester_user_id=current["requested_by"], reason=comment,
    )
    db.commit()
    return out


def _actor_name(
    db: Session, *, org_id: UUID, user_id: UUID | None,
    service_account_id: UUID | None = None,
) -> str:
    """알림에 표시할 요청자 이름.

    서비스 계정이면 **그 계정 이름**을 쓴다. 사람 이름을 빌려 오면 알림을 받은
    승인자가 "왜 김대표가 새벽 3시에 원가를 올렸지" 로 읽는다.
    """
    if user_id is not None:
        row = db.execute(text("""
            SELECT display_name FROM app_user WHERE id = :id AND org_id = :org
        """), {"id": str(user_id), "org": str(org_id)}).first()
        return str(row.display_name) if row and row.display_name else "요청자"

    if service_account_id is not None:
        row = db.execute(text("""
            SELECT name FROM service_account WHERE id = :id AND org_id = :org
        """), {"id": str(service_account_id), "org": str(org_id)}).first()
        return f"{row.name} (자동)" if row and row.name else "자동화"
    return "요청자"


def cancel(db, *, org_id, request_id, actor_user_id, actor_role,
           reason: str | None = None, correlation_id=None):
    current = get_request(db, org_id=org_id, request_id=request_id)
    if current["requested_by"] != actor_user_id and actor_role != "ADMIN":
        raise ForbiddenError("본인 요청이거나 관리자만 취소할 수 있습니다.")
    _transition(
        db, org_id=org_id, request_id=request_id, to_status="CANCELLED",
        actor_user_id=actor_user_id, actor_role=actor_role,
        reason=reason or "요청 취소", correlation_id=correlation_id,
        extra_sql=", cancelled_at = now()",
    )
    db.commit()
    return get_request(db, org_id=org_id, request_id=request_id)


# ──────────────────────────────────────────────────────────────
# 적용
# ──────────────────────────────────────────────────────────────

def apply(
    db: Session, *, org_id: UUID, request_id: UUID,
    actor_user_id: UUID, actor_role: str, correlation_id: str | None = None,
) -> dict:
    """승인된 변경을 실제로 반영한다.

    **적용 시점에 모든 조건을 다시 검증한다.** 승인과 적용 사이에 세상이 바뀔 수 있다:
    대상이 변경/삭제되거나, 월이 잠기거나, 다른 요청이 먼저 적용되거나.

    승인 요청 상태 변경과 대상 데이터 변경과 감사 로그가 **같은 트랜잭션**에서 커밋된다.
    하나라도 실패하면 전부 롤백된다 — 데이터만 바뀌고 로그가 빠지는 상황을 만들지 않는다.
    """
    current = get_request(db, org_id=org_id, request_id=request_id)

    if current["status"] == "APPLIED":
        # 멱등: 이미 적용된 건에 다시 적용해도 중복 감사 로그를 남기지 않고 그대로 돌려준다.
        return current
    if current["status"] != "APPROVED":
        raise ConflictError(
            f"승인되지 않은 요청은 적용할 수 없습니다 (현재 {current['status']}).",
            detail={"status": current["status"]},
        )

    policy = load_approval_policy(db, org_id, current["target_type"])
    if actor_role not in policy["required_roles"]:
        raise ApprovalPermissionDeniedError(
            f"적용 권한이 없습니다. 필요한 역할: {policy['required_roles']}")

    adapter = targets.get_adapter(current["target_type"])
    target_id = current["target_id"]

    # ① 대상이 아직 존재하는가 (삭제/비활성 확인)
    adapter.validate_target(db, org_id=org_id, target_id=target_id)

    # ② 대상이 그 사이에 바뀌지 않았는가 (낙관적 잠금)
    now_version = adapter.get_current_version(db, org_id=org_id, target_id=target_id)
    if current["base_version"] and now_version != current["base_version"]:
        record_audit(
            db, org_id=org_id, domain_table="approval_request", domain_row_id=request_id,
            action="STATUS_CHANGE", actor_user_id=actor_user_id,
            before={"base_version": current["base_version"]},
            after={"current_version": now_version, "result": "STALE_CONFLICT"},
            reason="적용 시점에 대상이 변경되어 적용하지 않음", request_id=correlation_id,
        )
        db.execute(text("""
            UPDATE approval_request SET apply_error = :e, updated_at = now()
             WHERE id = :id AND org_id = :org
        """), {"e": "대상이 요청 이후 변경되었습니다.", "id": str(request_id), "org": str(org_id)})
        db.commit()
        raise ApprovalVersionConflictError(
            "요청 이후 대상 데이터가 변경되어 자동 적용할 수 없습니다. "
            "변경 내용을 다시 확인하고 새 요청을 만드세요.",
            detail={"base_version": current["base_version"], "current_version": now_version},
        )

    # ③ 월 잠금 — 승인으로 우회되지 않는다
    effective = adapter.get_effective_date(
        db, org_id=org_id, target_id=target_id,
        proposed=current["proposed_changes_json"] or {},
        effective_from=current["effective_from"],
    )
    # 예외는 **월 재오픈 하나뿐**이다. 재오픈이 바로 잠금을 푸는 연산이라
    # 대상 월이 잠겨 있는 것은 전제 조건이지 거부 사유가 아니다.
    # 가드를 그대로 두면 그 요청은 영원히 HELD_BY_LOCK 으로 남는다.
    #
    # 면제는 **어댑터 클래스 속성**이라 요청 payload 로 설정할 수 없고,
    # approval_targets._assert_single_lock_exemption() 이 import 시점에
    # 면제 대상이 늘지 않았는지 검사한다.
    if not getattr(adapter, "month_lock_exempt", False):
        assert_period_writable(
            db, org_id=org_id, effective_date=effective,
            operation="APPLY_APPROVAL", what="승인된 변경",
        )

    # ④ 적용 + 상태 전이 + 감사 — 한 트랜잭션
    before = adapter.build_snapshot(db, org_id=org_id, target_id=target_id)
    # `approval_request_id` 를 넘긴다 — 적용 결과가 새 레코드를 만드는 대상(가격 등)은
    # 그 레코드가 어느 승인에서 나왔는지 가리켜야 이력에서 "왜 바뀌었나" 를 알 수 있다.
    # 쓰지 않는 어댑터도 있어서 선택 인자로 받는다.
    after = adapter.apply_changes(
        db, org_id=org_id, target_id=target_id,
        proposed=current["proposed_changes_json"] or {}, actor_user_id=actor_user_id,
        request_type=current["request_type"], approval_request_id=request_id,
    )

    _transition(
        db, org_id=org_id, request_id=request_id, to_status="APPLIED",
        actor_user_id=actor_user_id, actor_role=actor_role,
        reason="승인 내용 적용", correlation_id=correlation_id,
        extra_sql=(", applied_by = :ab, applied_at = now(), apply_error = NULL,"
                   " applied_by_type = COALESCE(applied_by_type, 'USER'),"
                   " auto_apply_status = 'DONE'"),
        extra_params={"ab": str(actor_user_id)},
    )
    record_audit(
        db, org_id=org_id, domain_table=current["target_type"], domain_row_id=target_id,
        action="UPDATE", actor_user_id=actor_user_id,
        before=before, after=after,
        reason=f"승인 요청 {current['business_key']} 적용", request_id=correlation_id,
    )
    db.commit()
    return get_request(db, org_id=org_id, request_id=request_id)
