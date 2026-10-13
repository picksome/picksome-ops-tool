"""M018 승인 워크플로 (docs/43 §8).

검증 범위: 상태 전이 / 권한·자기승인 / 동시성·멱등성 / 월 마감 연동 / 감사 / 트랜잭션.

동시성을 재현해야 해서 공용 SAVEPOINT fixture 대신 **독립 커넥션 2개**를 쓴다.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    SettlementLockedError,
    ValidationError,
)
from app.services import approval as svc
from app.services import monthly_close as mc
from app.tests.fixtures.factories import confirm_month, lock_month, purge_month_close

ORG = uuid.UUID("00000000-0000-0000-0000-0000000000ff")
YM = "2026-06"


# ──────────────────────────────────────────────────────────────
# fixtures
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def sessions(_test_db_ready: str) -> Iterator[tuple[Session, Session]]:
    eng = create_engine(_test_db_ready, future=True)
    Maker = sessionmaker(bind=eng, future=True, expire_on_commit=False)
    a, b = Maker(), Maker()
    try:
        yield a, b
    finally:
        for s in (a, b):
            s.rollback()
            s.close()
        eng.dispose()


def _user(db: Session, email: str, role: str, org: uuid.UUID = ORG) -> uuid.UUID:
    row = db.execute(
        text("SELECT id FROM app_user WHERE org_id = :o AND email = :e"),
        {"o": str(org), "e": email},
    ).first()
    if row:
        db.execute(text("UPDATE app_user SET role = :r WHERE id = :i"),
                   {"r": role, "i": str(row.id)})
        db.commit()
        return uuid.UUID(str(row.id))
    uid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO app_user (id, org_id, email, password_hash, display_name, role, is_active)
        VALUES (:i, :o, :e, 'x', :n, :r, TRUE)
    """), {"i": str(uid), "o": str(org), "e": email, "n": email.split("@")[0], "r": role})
    db.commit()
    return uid


def _product(db: Session, org: uuid.UUID = ORG, cost: int = 10000) -> uuid.UUID:
    pid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO product (id, org_id, business_key, name_ko, margin_type, status, cost_amount)
        VALUES (:i, :o, :bk, :n, 'GENERAL_RESELL', 'ACTIVE', :c)
    """), {"i": str(pid), "o": str(org), "bk": f"PROD-{pid.hex}",
           "n": "승인 테스트 상품", "c": cost})
    db.commit()
    return pid


def _cleanup(db: Session) -> None:
    db.execute(text("DELETE FROM audit_log WHERE org_id = :o AND domain_table IN ('approval_request','product')"),
               {"o": str(ORG)})
    db.execute(text("DELETE FROM approval_request WHERE org_id = :o"), {"o": str(ORG)})
    purge_month_close(db, org_id=ORG, year_month=YM)
    db.commit()


def _set_self_approve(db: Session, allowed: bool, org: uuid.UUID = ORG) -> None:
    # jsonb_set 은 **마지막 단계만** 만든다. {approval} 이 없으면 중첩 경로를 못 만드므로
    # approval 객체를 통째로 병합한다.
    db.execute(text("""
        UPDATE organization
           SET settings_json =
                 COALESCE(settings_json, '{}'::jsonb)
                 || jsonb_build_object(
                      'approval',
                      COALESCE(settings_json -> 'approval', '{}'::jsonb)
                        || jsonb_build_object('self_approve_allowed', CAST(:v AS BOOLEAN))
                    )
         WHERE id = :o
    """), {"v": allowed, "o": str(org)})
    db.commit()


@pytest.fixture
def ctx(sessions):
    a, b = sessions
    _cleanup(a)
    _set_self_approve(a, False)
    requester = _user(a, "req@picksome.local", "OPERATOR")
    approver = _user(a, "apr@picksome.local", "ADMIN")
    pid = _product(a)
    yield {"a": a, "b": b, "requester": requester, "approver": approver, "product": pid}
    _cleanup(a)


def _create(db, ctx, *, submit=False, changes=None, **kw):
    return svc.create_request(
        db, org_id=ORG, actor_user_id=ctx["requester"],
        target_type="product", target_id=ctx["product"],
        request_type="COST_CHANGE",
        proposed_changes=changes or {"cost_amount": 12000},
        submit=submit, **kw,
    )


# ──────────────────────────────────────────────────────────────
# 상태 전이
# ──────────────────────────────────────────────────────────────

def test_full_happy_path_draft_to_applied(ctx):
    a = ctx["a"]
    r = _create(a, ctx)
    assert r["status"] == "DRAFT"
    assert r["base_version"], "요청 생성 시 대상 지문이 박제돼야 한다"
    assert r["before_snapshot_json"]["cost_amount"] == 10000

    r = svc.submit(a, org_id=ORG, request_id=r["id"],
                   actor_user_id=ctx["requester"], actor_role="OPERATOR")
    assert r["status"] == "PENDING"

    r = svc.approve(a, org_id=ORG, request_id=r["id"],
                    actor_user_id=ctx["approver"], actor_role="ADMIN", comment="확인함")
    assert r["status"] == "APPROVED"

    # 승인만으로는 대상이 바뀌지 않는다
    cost = a.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                     {"i": str(ctx["product"])}).scalar()
    assert cost == 10000, "승인 단계에서 대상이 바뀌면 안 된다"

    r = svc.apply(a, org_id=ORG, request_id=r["id"],
                  actor_user_id=ctx["approver"], actor_role="ADMIN")
    assert r["status"] == "APPLIED"
    assert r["applied_at"] is not None

    cost = a.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                     {"i": str(ctx["product"])}).scalar()
    assert cost == 12000


def test_disallowed_transitions_are_rejected(ctx):
    a = ctx["a"]
    r = _create(a, ctx)
    # DRAFT 에서 바로 승인 불가
    with pytest.raises(ConflictError):
        svc.approve(a, org_id=ORG, request_id=r["id"],
                    actor_user_id=ctx["approver"], actor_role="ADMIN")
    # DRAFT 에서 바로 적용 불가
    with pytest.raises(ConflictError):
        svc.apply(a, org_id=ORG, request_id=r["id"],
                  actor_user_id=ctx["approver"], actor_role="ADMIN")


def test_proposal_is_immutable_after_submit(ctx):
    a = ctx["a"]
    r = _create(a, ctx, submit=True)
    with pytest.raises(ConflictError, match="수정할 수 없습니다"):
        svc.update_draft(a, org_id=ORG, actor_user_id=ctx["requester"],
                         request_id=r["id"], proposed_changes={"cost_amount": 99999})


def test_draft_can_be_edited_by_owner_only(ctx):
    a = ctx["a"]
    r = _create(a, ctx)
    r2 = svc.update_draft(a, org_id=ORG, actor_user_id=ctx["requester"],
                          request_id=r["id"], proposed_changes={"cost_amount": 13000})
    assert r2["proposed_changes_json"]["cost_amount"] == 13000

    with pytest.raises(ForbiddenError):
        svc.update_draft(a, org_id=ORG, actor_user_id=ctx["approver"],
                         request_id=r["id"], proposed_changes={"cost_amount": 1})


def test_rejected_cannot_be_applied(ctx):
    a = ctx["a"]
    r = _create(a, ctx, submit=True)
    r = svc.reject(a, org_id=ORG, request_id=r["id"], actor_user_id=ctx["approver"],
                   actor_role="ADMIN", comment="원가 근거 부족")
    assert r["status"] == "REJECTED"
    with pytest.raises(ConflictError):
        svc.apply(a, org_id=ORG, request_id=r["id"],
                  actor_user_id=ctx["approver"], actor_role="ADMIN")


def test_reject_requires_reason(ctx):
    a = ctx["a"]
    r = _create(a, ctx, submit=True)
    with pytest.raises(ValidationError, match="사유"):
        svc.reject(a, org_id=ORG, request_id=r["id"], actor_user_id=ctx["approver"],
                   actor_role="ADMIN", comment="  ")


def test_cancelled_is_terminal(ctx):
    a = ctx["a"]
    r = _create(a, ctx)
    r = svc.cancel(a, org_id=ORG, request_id=r["id"],
                   actor_user_id=ctx["requester"], actor_role="OPERATOR")
    assert r["status"] == "CANCELLED"
    with pytest.raises(ConflictError):
        svc.submit(a, org_id=ORG, request_id=r["id"],
                   actor_user_id=ctx["requester"], actor_role="OPERATOR")


# ──────────────────────────────────────────────────────────────
# 권한 · 자기 승인
# ──────────────────────────────────────────────────────────────

def test_self_approval_blocked_by_default(ctx):
    a = ctx["a"]
    # 요청자에게 승인 권한(ADMIN)을 줘도 자기 요청은 승인할 수 없다
    _user(a, "req@picksome.local", "ADMIN")
    r = _create(a, ctx, submit=True)
    with pytest.raises(ForbiddenError, match="본인이 요청한 건"):
        svc.approve(a, org_id=ORG, request_id=r["id"],
                    actor_user_id=ctx["requester"], actor_role="ADMIN")


def test_self_approval_allowed_when_org_setting_enabled(ctx):
    """1인 사업자는 자기 승인 외에 방법이 없다 — 설정으로 열 수 있어야 한다."""
    a = ctx["a"]
    _set_self_approve(a, True)
    _user(a, "req@picksome.local", "ADMIN")
    r = _create(a, ctx, submit=True)
    r = svc.approve(a, org_id=ORG, request_id=r["id"],
                    actor_user_id=ctx["requester"], actor_role="ADMIN")
    assert r["status"] == "APPROVED"


def test_role_without_approve_permission_is_rejected(ctx):
    a = ctx["a"]
    r = _create(a, ctx, submit=True)
    with pytest.raises(ForbiddenError, match="승인 권한이 없습니다"):
        svc.approve(a, org_id=ORG, request_id=r["id"],
                    actor_user_id=ctx["approver"], actor_role="READONLY")


def test_permission_is_checked_at_review_time_not_request_time(ctx):
    """권한이 회수된 사람은 예전 정책으로 승인할 수 없다."""
    a = ctx["a"]
    r = _create(a, ctx, submit=True)
    # 승인자의 역할이 READONLY 로 강등된 상황
    with pytest.raises(ForbiddenError):
        svc.approve(a, org_id=ORG, request_id=r["id"],
                    actor_user_id=ctx["approver"], actor_role="READONLY")


def test_other_organization_cannot_access(ctx):
    """다른 조직의 요청은 404 — 403 으로 구분하면 ID 존재가 새어나간다."""
    a = ctx["a"]
    r = _create(a, ctx)
    other_org = uuid.uuid4()
    a.execute(text("""
        INSERT INTO organization (id, business_name, business_type, primary_currency, timezone)
        VALUES (:i, 'OTHER', 'SOLE_PROPRIETOR', 'KRW', 'Asia/Seoul')
    """), {"i": str(other_org)})
    a.commit()
    with pytest.raises(NotFoundError):
        svc.get_request(a, org_id=other_org, request_id=r["id"])


def test_unsupported_target_type_is_422(ctx):
    a = ctx["a"]
    with pytest.raises(ValidationError, match="지원하지 않는"):
        svc.create_request(
            a, org_id=ORG, actor_user_id=ctx["requester"],
            target_type="marketplace_settlement", target_id=uuid.uuid4(),
            request_type="FORCE_MATCH", proposed_changes={"x": 1},
        )


def test_unknown_field_is_rejected(ctx):
    a = ctx["a"]
    with pytest.raises(ValidationError, match="변경할 수 없는 필드"):
        _create(a, ctx, changes={"secret_column": 1})


# ──────────────────────────────────────────────────────────────
# 동시성 · 멱등성
# ──────────────────────────────────────────────────────────────

def test_concurrent_approve_and_reject_only_one_wins(ctx):
    a, b = ctx["a"], ctx["b"]
    r = _create(a, ctx, submit=True)
    svc.approve(a, org_id=ORG, request_id=r["id"],
                actor_user_id=ctx["approver"], actor_role="ADMIN")
    with pytest.raises(ConflictError):
        svc.reject(b, org_id=ORG, request_id=r["id"], actor_user_id=ctx["approver"],
                   actor_role="ADMIN", comment="동시 반려 시도")


def test_double_approve_is_rejected(ctx):
    a, b = ctx["a"], ctx["b"]
    r = _create(a, ctx, submit=True)
    svc.approve(a, org_id=ORG, request_id=r["id"],
                actor_user_id=ctx["approver"], actor_role="ADMIN")
    with pytest.raises(ConflictError):
        svc.approve(b, org_id=ORG, request_id=r["id"],
                    actor_user_id=ctx["approver"], actor_role="ADMIN")


def test_double_apply_changes_target_only_once(ctx):
    """두 번 적용해도 대상 변경은 한 번만. 두 번째는 기존 결과를 그대로 돌려준다."""
    a, b = ctx["a"], ctx["b"]
    r = _create(a, ctx, submit=True)
    svc.approve(a, org_id=ORG, request_id=r["id"],
                actor_user_id=ctx["approver"], actor_role="ADMIN")
    first = svc.apply(a, org_id=ORG, request_id=r["id"],
                      actor_user_id=ctx["approver"], actor_role="ADMIN")
    second = svc.apply(b, org_id=ORG, request_id=r["id"],
                       actor_user_id=ctx["approver"], actor_role="ADMIN")
    assert first["status"] == second["status"] == "APPLIED"
    assert first["applied_at"] == second["applied_at"], "재적용이 시각을 갱신하면 안 된다"

    n = a.execute(text("""
        SELECT COUNT(*) FROM audit_log
         WHERE org_id = :o AND domain_table = 'product' AND action = 'UPDATE'
    """), {"o": str(ORG)}).scalar()
    assert n == 1, "재적용이 감사 로그를 중복 생성하면 안 된다"


def test_duplicate_open_request_for_same_target_is_blocked(ctx):
    a = ctx["a"]
    _create(a, ctx, submit=True)
    with pytest.raises(ConflictError, match="이미 진행 중인"):
        _create(a, ctx, changes={"cost_amount": 15000})


def test_idempotency_key_returns_existing_request(ctx):
    a = ctx["a"]
    first = _create(a, ctx, idempotency_key="req-abc-1")
    second = _create(a, ctx, idempotency_key="req-abc-1")
    assert first["id"] == second["id"], "같은 키 재요청은 새 행을 만들지 않는다"

    n = a.execute(text("""
        SELECT COUNT(*) FROM audit_log
         WHERE org_id = :o AND domain_table = 'approval_request' AND action = 'CREATE'
    """), {"o": str(ORG)}).scalar()
    assert n == 1, "멱등 재요청이 감사 로그를 중복 생성하면 안 된다"


def test_stale_base_version_blocks_apply(ctx):
    """요청 이후 대상이 바뀌면 자동 적용하지 않는다."""
    a = ctx["a"]
    r = _create(a, ctx, submit=True)
    svc.approve(a, org_id=ORG, request_id=r["id"],
                actor_user_id=ctx["approver"], actor_role="ADMIN")

    # 그 사이 누군가 상품을 직접 바꿨다
    a.execute(text("UPDATE product SET cost_amount = 11111 WHERE id = :i"),
              {"i": str(ctx["product"])})
    a.commit()

    with pytest.raises(ConflictError, match="변경되어 자동 적용할 수 없습니다"):
        svc.apply(a, org_id=ORG, request_id=r["id"],
                  actor_user_id=ctx["approver"], actor_role="ADMIN")

    after = svc.get_request(a, org_id=ORG, request_id=r["id"])
    assert after["status"] == "APPROVED", "적용 실패가 승인 사실을 지우면 안 된다"
    assert after["apply_error"], "실패 사유가 기록돼야 한다"

    cost = a.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                     {"i": str(ctx["product"])}).scalar()
    assert cost == 11111, "충돌 시 대상이 덮어써지면 안 된다"


# ──────────────────────────────────────────────────────────────
# 월 마감 연동
# ──────────────────────────────────────────────────────────────

def _lock_month(db: Session, ym: str, actor: uuid.UUID) -> None:
    db.execute(text("""
        INSERT INTO monthly_settlement
          (org_id, business_key, year_month, status, close_state,
           total_revenue, total_direct_cost, total_common_expense, net_profit)
        VALUES (:o, :bk, :ym, 'DRAFT', 'OPEN', 0, 0, 0, 0)
    """), {"o": str(ORG), "bk": f"MS-APR-{uuid.uuid4().hex}", "ym": ym})
    db.commit()
    confirm_month(db, org_id=ORG, year_month=ym, actor_user_id=actor)
    lock_month(db, org_id=ORG, year_month=ym, actor_user_id=actor)


def test_apply_blocked_when_month_is_locked(ctx):
    """승인만으로 잠긴 달을 바꿀 수 없다."""
    a = ctx["a"]
    # ⚠️ 잠금을 **먼저** 한다. Phase 26 이후 "승인됐지만 미반영" 은 마감 차단
    #    항목이라, 미반영 승인이 남은 채로는 그 달을 잠글 수 없다.
    _lock_month(a, YM, ctx["approver"])
    r = _create(a, ctx, submit=True, effective_from=__import__("datetime").date(2026, 6, 15))
    svc.approve(a, org_id=ORG, request_id=r["id"],
                actor_user_id=ctx["approver"], actor_role="ADMIN")

    with pytest.raises(SettlementLockedError):
        svc.apply(a, org_id=ORG, request_id=r["id"],
                  actor_user_id=ctx["approver"], actor_role="ADMIN")

    cost = a.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                     {"i": str(ctx["product"])}).scalar()
    assert cost == 10000, "잠긴 달에는 대상이 바뀌면 안 된다"


def test_apply_succeeds_after_month_reopened(ctx):
    a = ctx["a"]
    import datetime
    _lock_month(a, YM, ctx["approver"])
    r = _create(a, ctx, submit=True, effective_from=datetime.date(2026, 6, 15))
    svc.approve(a, org_id=ORG, request_id=r["id"],
                actor_user_id=ctx["approver"], actor_role="ADMIN")

    with pytest.raises(SettlementLockedError):
        svc.apply(a, org_id=ORG, request_id=r["id"],
                  actor_user_id=ctx["approver"], actor_role="ADMIN")

    mc.unlock(a, org_id=ORG, year_month=YM,
              actor_user_id=ctx["approver"], reason="승인 건 반영 위해 재개")

    applied = svc.apply(a, org_id=ORG, request_id=r["id"],
                        actor_user_id=ctx["approver"], actor_role="ADMIN")
    assert applied["status"] == "APPLIED"


def test_state_change_itself_is_not_blocked_by_month_lock(ctx):
    """승인·반려 같은 상태 변경은 대상 데이터를 쓰지 않는다 → 월 잠금과 무관."""
    a = ctx["a"]
    import datetime
    r = _create(a, ctx, submit=True, effective_from=datetime.date(2026, 6, 15))
    _lock_month(a, YM, ctx["approver"])

    # 잠긴 상태에서도 승인 자체는 된다
    approved = svc.approve(a, org_id=ORG, request_id=r["id"],
                           actor_user_id=ctx["approver"], actor_role="ADMIN")
    assert approved["status"] == "APPROVED"


# ──────────────────────────────────────────────────────────────
# 감사 · 트랜잭션
# ──────────────────────────────────────────────────────────────

def test_every_transition_writes_audit_log(ctx):
    a = ctx["a"]
    r = _create(a, ctx)
    svc.submit(a, org_id=ORG, request_id=r["id"],
               actor_user_id=ctx["requester"], actor_role="OPERATOR")
    svc.approve(a, org_id=ORG, request_id=r["id"],
                actor_user_id=ctx["approver"], actor_role="ADMIN")
    svc.apply(a, org_id=ORG, request_id=r["id"],
              actor_user_id=ctx["approver"], actor_role="ADMIN")

    rows = a.execute(text("""
        SELECT action, after_json FROM audit_log
         WHERE org_id = :o AND domain_table = 'approval_request'
         ORDER BY occurred_at
    """), {"o": str(ORG)}).mappings().all()
    actions = [r["action"] for r in rows]
    assert actions[0] == "CREATE"
    statuses = [r["after_json"].get("status") for r in rows if r["after_json"]]
    assert "PENDING" in statuses and "APPROVED" in statuses and "APPLIED" in statuses


def test_apply_records_target_before_and_after(ctx):
    a = ctx["a"]
    r = _create(a, ctx, submit=True)
    svc.approve(a, org_id=ORG, request_id=r["id"],
                actor_user_id=ctx["approver"], actor_role="ADMIN")
    svc.apply(a, org_id=ORG, request_id=r["id"],
              actor_user_id=ctx["approver"], actor_role="ADMIN")

    row = a.execute(text("""
        SELECT before_json, after_json FROM audit_log
         WHERE org_id = :o AND domain_table = 'product' AND action = 'UPDATE'
         ORDER BY occurred_at DESC LIMIT 1
    """), {"o": str(ORG)}).mappings().first()
    assert row["before_json"]["cost_amount"] == 10000
    assert row["after_json"]["cost_amount"] == 12000


def test_failed_transition_writes_no_success_audit(ctx):
    a = ctx["a"]
    r = _create(a, ctx)
    before = a.execute(text("""
        SELECT COUNT(*) FROM audit_log
         WHERE org_id = :o AND domain_table = 'approval_request' AND action = 'STATUS_CHANGE'
    """), {"o": str(ORG)}).scalar()

    with pytest.raises(ConflictError):
        svc.approve(a, org_id=ORG, request_id=r["id"],
                    actor_user_id=ctx["approver"], actor_role="ADMIN")
    a.rollback()

    after = a.execute(text("""
        SELECT COUNT(*) FROM audit_log
         WHERE org_id = :o AND domain_table = 'approval_request' AND action = 'STATUS_CHANGE'
    """), {"o": str(ORG)}).scalar()
    assert after == before


def test_rejected_transition_does_not_rollback_unrelated_work(ctx):
    """전이 거부가 같은 트랜잭션의 무관한 변경을 되돌리지 않는다.

    서비스가 요청 트랜잭션을 **소유하지 않으므로** 거부 경로에서 db.rollback() 을
    부르면 앞서 한 작업까지 날아간다. SAVEPOINT 로 격리되어 있는지 확인한다.
    """
    a = ctx["a"]
    r = _create(a, ctx)

    marker = uuid.uuid4()
    a.execute(text("""
        INSERT INTO product (id, org_id, business_key, name_ko, margin_type, status)
        VALUES (:i, :o, :bk, '무관한 상품', 'GENERAL_RESELL', 'ACTIVE')
    """), {"i": str(marker), "o": str(ORG), "bk": f"PROD-{marker.hex}"})

    with pytest.raises(ConflictError):
        svc.approve(a, org_id=ORG, request_id=r["id"],
                    actor_user_id=ctx["approver"], actor_role="ADMIN")

    survived = a.execute(text("SELECT 1 FROM product WHERE id = :i"),
                         {"i": str(marker)}).first()
    assert survived is not None, "전이 거부가 무관한 변경을 롤백하면 안 된다"
    a.rollback()


def test_apply_failure_rolls_back_target_change(ctx):
    """적용 도중 실패하면 대상 변경도 함께 롤백된다."""
    a = ctx["a"]
    r = _create(a, ctx, submit=True)
    svc.approve(a, org_id=ORG, request_id=r["id"],
                actor_user_id=ctx["approver"], actor_role="ADMIN")

    # 대상 상품을 지워서 적용 단계의 validate_target 이 실패하게 만든다
    a.execute(text("UPDATE product SET deleted_at = now() WHERE id = :i"),
              {"i": str(ctx["product"])})
    a.commit()

    with pytest.raises(NotFoundError):
        svc.apply(a, org_id=ORG, request_id=r["id"],
                  actor_user_id=ctx["approver"], actor_role="ADMIN")

    after = svc.get_request(a, org_id=ORG, request_id=r["id"])
    assert after["status"] == "APPROVED", "적용 실패 시 상태가 APPLIED 로 넘어가면 안 된다"


# ──────────────────────────────────────────────────────────────
# 조회
# ──────────────────────────────────────────────────────────────

def test_list_filters_and_pagination(ctx):
    a = ctx["a"]
    r1 = _create(a, ctx)
    svc.submit(a, org_id=ORG, request_id=r1["id"],
               actor_user_id=ctx["requester"], actor_role="OPERATOR")

    pending, total = svc.list_requests(db=a, org_id=ORG, status="PENDING")
    assert total == 1 and pending[0]["id"] == r1["id"]

    mine, _ = svc.list_requests(db=a, org_id=ORG, requested_by=ctx["requester"])
    assert len(mine) == 1

    none, n = svc.list_requests(db=a, org_id=ORG, status="APPLIED")
    assert n == 0 and none == []

    by_target, n2 = svc.list_requests(db=a, org_id=ORG,
                                      target_type="product", target_id=ctx["product"])
    assert n2 == 1
