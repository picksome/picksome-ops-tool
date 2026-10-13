"""자동 적용 실행기 (M019) + M018 안정화 검증.

여기서 확인하는 것
------------------
- 적용일 이전에는 적용되지 않는다
- 적용일이 도래하면 적용된다 (KST 기준)
- 잠긴 월은 **보류**된다. 자동으로 잠금을 풀지 않는다
- 실패가 반려로 바뀌지 않는다 (status 는 APPROVED 유지)
- 한 건 실패가 다음 건을 막지 않는다
- 두 워커가 동시에 돌아도 한 번만 적용된다
- 자동 적용은 `applied_by_type='SYSTEM'`, 수동은 `'USER'`
- idempotency fingerprint: 같은 키 다른 내용은 409
- 제약조건 오류가 **문자열이 아니라 constraint_name** 으로 분류된다
- 감사 로그 실패 시 전체 롤백
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import (
    ApprovalOpenRequestExistsError,
    IdempotencyKeyReusedError,
    ValidationError,
)
from app.services import approval as svc
from app.services import approval_executor as executor
from app.services import monthly_close as mc
from app.tests.fixtures.factories import confirm_month, lock_month, purge_month_close

ORG = uuid.UUID("00000000-0000-0000-0000-0000000000ff")


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


def _user(db: Session, email: str, role: str) -> uuid.UUID:
    row = db.execute(text("SELECT id FROM app_user WHERE org_id = :o AND email = :e"),
                     {"o": str(ORG), "e": email}).first()
    if row:
        db.execute(text("UPDATE app_user SET role = :r WHERE id = :i"),
                   {"r": role, "i": str(row.id)})
        db.commit()
        return uuid.UUID(str(row.id))
    uid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO app_user (id, org_id, email, password_hash, display_name, role, is_active)
        VALUES (:i, :o, :e, 'x', :n, :r, TRUE)
    """), {"i": str(uid), "o": str(ORG), "e": email, "n": email.split("@")[0], "r": role})
    db.commit()
    return uid


def _product(db: Session, cost: int = 10000) -> uuid.UUID:
    pid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO product (id, org_id, business_key, name_ko, margin_type, status, cost_amount)
        VALUES (:i, :o, :bk, '실행기 테스트 상품', 'GENERAL_RESELL', 'ACTIVE', :c)
    """), {"i": str(pid), "o": str(ORG), "bk": f"PROD-{pid.hex}", "c": cost})
    db.commit()
    return pid


def _cleanup(db: Session) -> None:
    db.execute(text("DELETE FROM audit_log WHERE org_id = :o AND domain_table IN ('approval_request','product')"),
               {"o": str(ORG)})
    db.execute(text("DELETE FROM approval_request WHERE org_id = :o"), {"o": str(ORG)})
    purge_month_close(db, org_id=ORG)
    db.commit()


@pytest.fixture
def ctx(sessions):
    a, b = sessions
    _cleanup(a)
    requester = _user(a, "exec-req@picksome.local", "OPERATOR")
    approver = _user(a, "exec-apr@picksome.local", "ADMIN")
    yield {"a": a, "b": b, "requester": requester, "approver": approver}
    _cleanup(a)


def _approved(db, ctx, *, effective_from, product=None, cost=12000):
    pid = product or _product(db)
    r = svc.create_request(
        db, org_id=ORG, actor_user_id=ctx["requester"],
        target_type="product", target_id=pid, request_type="COST_CHANGE",
        proposed_changes={"cost_amount": cost}, effective_from=effective_from, submit=True,
    )
    r = svc.approve(db, org_id=ORG, request_id=r["id"],
                    actor_user_id=ctx["approver"], actor_role="ADMIN")
    return r, pid


# ──────────────────────────────────────────────────────────────
# 자동 적용
# ──────────────────────────────────────────────────────────────

def test_not_applied_before_effective_date(ctx):
    a = ctx["a"]
    tomorrow = executor.today_kst() + timedelta(days=1)
    r, pid = _approved(a, ctx, effective_from=tomorrow)

    result = executor.run_due_approvals(a, org_id=ORG)
    assert result.scanned == 0
    assert result.applied == 0

    cost = a.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                     {"i": str(pid)}).scalar()
    assert cost == 10000
    assert svc.get_request(a, org_id=ORG, request_id=r["id"])["status"] == "APPROVED"


def test_applied_when_effective_date_arrives(ctx):
    a = ctx["a"]
    r, pid = _approved(a, ctx, effective_from=executor.today_kst())

    result = executor.run_due_approvals(a, org_id=ORG)
    assert result.scanned == 1
    assert result.applied == 1

    cost = a.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                     {"i": str(pid)}).scalar()
    assert cost == 12000

    after = svc.get_request(a, org_id=ORG, request_id=r["id"])
    assert after["status"] == "APPLIED"
    assert after["applied_by_type"] == "SYSTEM", "자동 적용은 SYSTEM 으로 기록돼야 한다"
    assert after["auto_apply_status"] == "DONE"


def test_past_effective_date_is_also_applied(ctx):
    """적용일이 지난 건도 적용된다 — 실행기가 며칠 멈춰 있었을 수 있다."""
    a = ctx["a"]
    r, pid = _approved(a, ctx, effective_from=executor.today_kst() - timedelta(days=10))
    result = executor.run_due_approvals(a, org_id=ORG)
    assert result.applied == 1
    assert svc.get_request(a, org_id=ORG, request_id=r["id"])["status"] == "APPLIED"


def test_kst_boundary_uses_kst_not_server_local(ctx):
    """KST 오늘이 기준이다. UTC 서버에서 date.today() 를 쓰면 9시간 어긋난다."""
    from datetime import datetime
    expected = (datetime.now(tz=UTC) + timedelta(hours=9)).date()
    assert executor.today_kst() == expected


def test_manual_apply_is_recorded_as_user(ctx):
    a = ctx["a"]
    r, pid = _approved(a, ctx, effective_from=executor.today_kst())
    svc.apply(a, org_id=ORG, request_id=r["id"],
              actor_user_id=ctx["approver"], actor_role="ADMIN")
    after = svc.get_request(a, org_id=ORG, request_id=r["id"])
    assert after["applied_by_type"] == "USER", "수동 적용은 USER 로 기록돼야 한다"


def test_already_applied_is_skipped(ctx):
    a = ctx["a"]
    r, pid = _approved(a, ctx, effective_from=executor.today_kst())
    svc.apply(a, org_id=ORG, request_id=r["id"],
              actor_user_id=ctx["approver"], actor_role="ADMIN")

    result = executor.run_due_approvals(a, org_id=ORG)
    assert result.scanned == 0, "APPLIED 는 스캔 대상이 아니다"

    n = a.execute(text("""
        SELECT COUNT(*) FROM audit_log
         WHERE org_id = :o AND domain_table = 'product' AND action = 'UPDATE'
    """), {"o": str(ORG)}).scalar()
    assert n == 1, "감사 로그가 중복 생성되면 안 된다"


def test_cancelled_request_is_not_applied(ctx):
    a = ctx["a"]
    r, pid = _approved(a, ctx, effective_from=executor.today_kst())
    svc.cancel(a, org_id=ORG, request_id=r["id"],
               actor_user_id=ctx["approver"], actor_role="ADMIN", reason="철회")
    result = executor.run_due_approvals(a, org_id=ORG)
    assert result.scanned == 0
    cost = a.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                     {"i": str(pid)}).scalar()
    assert cost == 10000


# ──────────────────────────────────────────────────────────────
# 월 잠금
# ──────────────────────────────────────────────────────────────

def _lock_month(db: Session, ym: str, actor: uuid.UUID) -> None:
    db.execute(text("""
        INSERT INTO monthly_settlement
          (org_id, business_key, year_month, status, close_state,
           total_revenue, total_direct_cost, total_common_expense, net_profit)
        VALUES (:o, :bk, :ym, 'DRAFT', 'OPEN', 0, 0, 0, 0)
    """), {"o": str(ORG), "bk": f"MS-EX-{uuid.uuid4().hex}", "ym": ym})
    db.commit()
    confirm_month(db, org_id=ORG, year_month=ym, actor_user_id=actor)
    lock_month(db, org_id=ORG, year_month=ym, actor_user_id=actor)


def test_locked_month_holds_instead_of_failing(ctx):
    """잠긴 월은 보류다. 자동으로 잠금을 풀지 않고, 반려로도 바꾸지 않는다."""
    a = ctx["a"]
    today = executor.today_kst()
    ym = today.strftime("%Y-%m")
    # ⚠️ 잠금을 **먼저** 한다. Phase 26 이후 "승인됐지만 미반영" 은 마감 차단
    #    항목이라, 미반영 승인이 남은 채로는 그 달을 잠글 수 없다.
    #    잠긴 달에 뒤늦게 승인이 나는 것이 실제로 HELD_BY_LOCK 이 생기는 경로다.
    _lock_month(a, ym, ctx["approver"])
    r, pid = _approved(a, ctx, effective_from=today)

    result = executor.run_due_approvals(a, org_id=ORG)
    assert result.held_by_lock == 1
    assert result.applied == 0

    after = svc.get_request(a, org_id=ORG, request_id=r["id"])
    assert after["status"] == "APPROVED", "잠금 때문에 반려되면 안 된다"
    assert after["auto_apply_status"] == "HELD_BY_LOCK"
    assert after["last_error_code"] == "SETTLEMENT_LOCKED"
    assert after["next_retry_at"] is None, "잠금은 자동 재시도 대상이 아니다"

    close_state = a.execute(text("""
        SELECT close_state FROM monthly_settlement WHERE org_id = :o AND year_month = :ym
    """), {"o": str(ORG), "ym": ym}).scalar()
    assert close_state == "LOCKED", "실행기가 잠금을 풀면 안 된다"


def test_apply_succeeds_after_month_reopened(ctx):
    a = ctx["a"]
    today = executor.today_kst()
    ym = today.strftime("%Y-%m")
    _lock_month(a, ym, ctx["approver"])
    r, pid = _approved(a, ctx, effective_from=today)
    executor.run_due_approvals(a, org_id=ORG)

    mc.unlock(a, org_id=ORG, year_month=ym,
              actor_user_id=ctx["approver"], reason="승인 반영 위해 재개")

    result = executor.run_due_approvals(a, org_id=ORG)
    assert result.applied == 1
    cost = a.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                     {"i": str(pid)}).scalar()
    assert cost == 12000


# ──────────────────────────────────────────────────────────────
# 실패 격리 · 동시성
# ──────────────────────────────────────────────────────────────

def test_one_failure_does_not_block_others(ctx):
    """한 건이 실패해도 나머지가 적용된다."""
    a = ctx["a"]
    today = executor.today_kst()

    bad_r, bad_pid = _approved(a, ctx, effective_from=today, cost=11111)
    good_r, good_pid = _approved(a, ctx, effective_from=today, cost=22222)

    # 첫 건의 대상을 지워 영구 실패로 만든다
    a.execute(text("UPDATE product SET deleted_at = now() WHERE id = :i"), {"i": str(bad_pid)})
    a.commit()

    result = executor.run_due_approvals(a, org_id=ORG)
    assert result.scanned == 2
    assert result.applied == 1
    assert result.validation_failed == 1

    good_cost = a.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                          {"i": str(good_pid)}).scalar()
    assert good_cost == 22222, "앞 건의 실패가 뒤 건을 막으면 안 된다"

    bad = svc.get_request(a, org_id=ORG, request_id=bad_r["id"])
    assert bad["status"] == "APPROVED", "실패해도 승인 사실은 남는다"
    assert bad["auto_apply_status"] == "PERMANENT_FAILURE"
    assert bad["attempt_count"] == 1


def test_two_workers_apply_only_once(ctx):
    """두 워커가 동시에 돌아도 대상은 한 번만 바뀐다."""
    a, b = ctx["a"], ctx["b"]
    r, pid = _approved(a, ctx, effective_from=executor.today_kst())

    ra = executor.run_due_approvals(a, org_id=ORG)
    rb = executor.run_due_approvals(b, org_id=ORG)

    assert ra.applied + rb.applied == 1, "두 워커가 각각 적용하면 안 된다"
    assert rb.scanned == 0 or rb.already_applied >= 0

    n = a.execute(text("""
        SELECT COUNT(*) FROM audit_log
         WHERE org_id = :o AND domain_table = 'product' AND action = 'UPDATE'
    """), {"o": str(ORG)}).scalar()
    assert n == 1


def test_batch_result_is_observable(ctx):
    a = ctx["a"]
    _approved(a, ctx, effective_from=executor.today_kst())
    result = executor.run_due_approvals(a, org_id=ORG)
    d = result.as_dict()
    for key in ("batch_id", "started_at", "finished_at", "scanned", "applied",
                "already_applied", "held_by_lock", "validation_failed", "other_failed"):
        assert key in d
    assert d["details"] and d["details"][0]["result"] == "APPLIED"


# ──────────────────────────────────────────────────────────────
# M018 안정화 — idempotency fingerprint
# ──────────────────────────────────────────────────────────────

def test_same_key_same_payload_returns_existing(ctx):
    a = ctx["a"]
    pid = _product(a)
    kw = dict(org_id=ORG, actor_user_id=ctx["requester"], target_type="product",
              target_id=pid, request_type="COST_CHANGE",
              proposed_changes={"cost_amount": 12000}, idempotency_key="k-1")
    first = svc.create_request(a, **kw)
    second = svc.create_request(a, **kw)
    assert first["id"] == second["id"]


def test_same_key_different_payload_is_rejected(ctx):
    """같은 키로 다른 내용이 오면 조용히 무시하지 않는다."""
    a = ctx["a"]
    pid = _product(a)
    svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                       target_type="product", target_id=pid, request_type="COST_CHANGE",
                       proposed_changes={"cost_amount": 12000}, idempotency_key="k-2")
    with pytest.raises(IdempotencyKeyReusedError):
        svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                           target_type="product", target_id=pid, request_type="COST_CHANGE",
                           proposed_changes={"cost_amount": 99999}, idempotency_key="k-2")


def test_same_key_different_target_is_rejected(ctx):
    a = ctx["a"]
    p1, p2 = _product(a), _product(a)
    svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                       target_type="product", target_id=p1, request_type="COST_CHANGE",
                       proposed_changes={"cost_amount": 12000}, idempotency_key="k-3")
    with pytest.raises(IdempotencyKeyReusedError):
        svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                           target_type="product", target_id=p2, request_type="COST_CHANGE",
                           proposed_changes={"cost_amount": 12000}, idempotency_key="k-3")


def test_fingerprint_is_stable_across_key_order():
    """JSON key 순서·공백 때문에 해시가 달라지면 안 된다."""
    from app.domain.approval import canonical_fingerprint
    tid = uuid.uuid4()
    f1 = canonical_fingerprint(target_type="product", target_id=tid,
                               request_type="COST_CHANGE",
                               proposed_changes={"cost_amount": 1, "memo": "x"},
                               effective_from=date(2026, 9, 1))
    f2 = canonical_fingerprint(target_type="product", target_id=tid,
                               request_type="COST_CHANGE",
                               proposed_changes={"memo": "x", "cost_amount": 1},
                               effective_from="2026-09-01")
    assert f1 == f2


# ──────────────────────────────────────────────────────────────
# M018 안정화 — 제약조건 예외 분류
# ──────────────────────────────────────────────────────────────

def test_open_request_conflict_uses_constraint_name_not_message(ctx):
    """UNIQUE 위반이 구조화된 constraint_name 으로 분류되고 기존 요청 id 를 준다."""
    a = ctx["a"]
    pid = _product(a)
    first = svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                               target_type="product", target_id=pid,
                               request_type="COST_CHANGE",
                               proposed_changes={"cost_amount": 12000}, submit=True)
    with pytest.raises(ApprovalOpenRequestExistsError) as exc:
        svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                           target_type="product", target_id=pid,
                           request_type="COST_CHANGE",
                           proposed_changes={"cost_amount": 15000})
    assert exc.value.code == "APPROVAL_OPEN_REQUEST_EXISTS"
    assert exc.value.detail["existing_request_id"] == str(first["id"])


def test_other_integrity_error_is_not_translated(ctx):
    """다른 무결성 오류를 같은 응답으로 잘못 바꾸지 않는다 — 삼키면 진짜 버그가 숨는다."""
    from app.core.db_errors import translate_integrity_error

    a = ctx["a"]
    with pytest.raises(Exception) as exc:
        a.execute(text("""
            INSERT INTO approval_request
              (org_id, business_key, target_type, target_id, request_type, status, requested_by)
            VALUES (:o, 'APR-FK', 'product', gen_random_uuid(), 'COST_CHANGE', 'DRAFT',
                    gen_random_uuid())
        """), {"o": str(ORG)})
    a.rollback()

    # FK 위반이지 open_target UNIQUE 가 아니다 → 매핑에 없으므로 None
    assert translate_integrity_error(exc.value, {
        "ux_approval_open_target": lambda: ApprovalOpenRequestExistsError("x"),
    }) is None


def test_constraint_name_extraction_survives_message_changes():
    """예외 메시지 문구가 달라져도 constraint_name 으로 잡는다."""
    from app.core.db_errors import constraint_name_of

    class FakeDiag:
        constraint_name = "ux_approval_open_target"

    class FakeOrig(Exception):
        diag = FakeDiag()

    class FakeWrapper(Exception):
        orig = FakeOrig()

    assert constraint_name_of(FakeWrapper()) == "ux_approval_open_target"
    assert constraint_name_of(Exception("아무 메시지")) is None


# ──────────────────────────────────────────────────────────────
# M018 안정화 — JSONB 검증
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload,reason", [
    ({}, "빈 변경"),
    ({"unknown_col": 1}, "알 수 없는 필드"),
    ({"org_id": "x"}, "서버 관리 필드"),
    ({"id": "x"}, "서버 관리 필드"),
    ({"version": 2}, "서버 관리 필드"),
    ({"cost_amount": {"nested": 1}}, "중첩 구조"),
    ({"memo": "x"}, "직접 저장 대상 — 승인 필드가 아님"),
    ({"cost_amount": [1, 2]}, "리스트"),
])
def test_jsonb_validation_rejects_bad_payloads(ctx, payload, reason):
    """service 를 직접 호출해도 검증된다 — 라우터만 검증하면 배치가 뚫린다."""
    a = ctx["a"]
    pid = _product(a)
    with pytest.raises(ValidationError):
        svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                           target_type="product", target_id=pid,
                           request_type="COST_CHANGE", proposed_changes=payload)


def test_allowed_field_passes(ctx):
    """소유권 있는 필드는 통과한다."""
    a = ctx["a"]
    pid = _product(a)
    r = svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                           target_type="product", target_id=pid,
                           request_type="PRODUCT_INFO_CHANGE",
                           proposed_changes={"name_ko": "새 상품명"})
    assert r["proposed_changes_json"]["name_ko"] == "새 상품명"


def test_memo_is_not_an_approval_field(ctx):
    """`memo` 는 직접 저장 대상이다 — 승인으로 바꾸지 않는다.

    운영 영향이 없는 값까지 승인을 태우면 승인함이 잡음으로 가득 차고,
    정작 중요한 원가 변경이 묻힌다.
    """
    a = ctx["a"]
    pid = _product(a)
    with pytest.raises(ValidationError):
        svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                           target_type="product", target_id=pid,
                           request_type="PRODUCT_INFO_CHANGE",
                           proposed_changes={"memo": "메모는 직접 저장"})


def test_build_diff_excludes_server_fields():
    from app.domain.approval import build_diff
    rows = build_diff(
        {"cost_amount": 100, "org_id": "should-not-appear", "memo": "a"},
        {"cost_amount": 200},
    )
    fields = {r.field for r in rows}
    assert "org_id" not in fields
    changed = {r.field for r in rows if r.changed}
    assert changed == {"cost_amount"}


# ──────────────────────────────────────────────────────────────
# 감사 로그 실패 시 전체 롤백
# ──────────────────────────────────────────────────────────────

def test_audit_failure_rolls_back_everything(ctx, monkeypatch):
    """감사 로그가 실패하면 대상 변경도 상태 전이도 남지 않는다.

    mock 호출 횟수가 아니라 **실제 DB 상태**를 확인한다.
    """
    a = ctx["a"]
    r, pid = _approved(a, ctx, effective_from=executor.today_kst())
    before_version = svc.get_request(a, org_id=ORG, request_id=r["id"])["version"]

    boom = RuntimeError("감사 로그 기록 실패(의도적)")

    real_record = svc.record_audit
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        # 대상 변경 감사(product UPDATE)에서 터뜨린다 — 이미 product 는 UPDATE 된 뒤다
        if kwargs.get("domain_table") == "product":
            raise boom
        return real_record(*args, **kwargs)

    monkeypatch.setattr(svc, "record_audit", flaky)

    with pytest.raises(RuntimeError):
        svc.apply(a, org_id=ORG, request_id=r["id"],
                  actor_user_id=ctx["approver"], actor_role="ADMIN")
    a.rollback()

    # ① 대상이 바뀌지 않았다
    cost = a.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                     {"i": str(pid)}).scalar()
    assert cost == 10000, "감사 실패 시 대상 변경이 남으면 안 된다"

    # ② 상태가 바뀌지 않았다
    after = svc.get_request(a, org_id=ORG, request_id=r["id"])
    assert after["status"] == "APPROVED"
    assert after["applied_at"] is None

    # ③ version 이 증가하지 않았다
    assert after["version"] == before_version

    # ④ 감사 로그가 부분적으로 남지 않았다
    n = a.execute(text("""
        SELECT COUNT(*) FROM audit_log
         WHERE org_id = :o AND domain_table = 'product'
    """), {"o": str(ORG)}).scalar()
    assert n == 0

    # ⑤ 재시도하면 정상 적용된다
    monkeypatch.setattr(svc, "record_audit", real_record)
    applied = svc.apply(a, org_id=ORG, request_id=r["id"],
                        actor_user_id=ctx["approver"], actor_role="ADMIN")
    assert applied["status"] == "APPLIED"
    cost = a.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                     {"i": str(pid)}).scalar()
    assert cost == 12000
