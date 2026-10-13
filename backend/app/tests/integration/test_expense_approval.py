"""사업비용 승인 + 잠긴 월 조정.

여기서 지키는 것
--------------
· 승인 대상 필드를 일반 수정 API 로 못 바꾼다 (상품에서 겪은 우회 구멍의 재발 방지)
· **잠긴 달의 비용은 승인으로도 못 바꾼다** — 승인이 월 잠금을 우회하지 않는다
· 잠긴 달 정정은 열린 달의 **조정**으로 처리하고 원본은 그대로 둔다
· 승인 적용 · 감사 로그 · 재계산 표시가 한 트랜잭션이다
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import (
    ApprovalRequiredError,
    ConflictError,
    NotFoundError,
    SettlementLockedError,
    ValidationError,
)
from app.domain.expense import ExpenseUpdate
from app.services import approval as approval_svc
from app.services import expense as expense_svc
from app.services import expense_adjustment as adj_svc
from app.services import period_recalc
from app.tests.fixtures.factories import purge_month_close

ORG = uuid.UUID("00000000-0000-0000-0000-0000000000ff")


@pytest.fixture
def db(_test_db_ready: str) -> Iterator[Session]:
    eng = create_engine(_test_db_ready, future=True)
    s = sessionmaker(bind=eng, future=True, expire_on_commit=False)()
    try:
        yield s
    finally:
        s.rollback()
        s.close()
        eng.dispose()


def _user(db: Session, email: str, role: str) -> uuid.UUID:
    row = db.execute(text("SELECT id FROM app_user WHERE org_id = :o AND email = :e"),
                     {"o": str(ORG), "e": email}).first()
    if row:
        return uuid.UUID(str(row.id))
    uid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO app_user (id, org_id, email, password_hash, display_name, role, is_active)
        VALUES (:i, :o, :e, 'x', :n, :r, TRUE)
    """), {"i": str(uid), "o": str(ORG), "e": email, "n": email.split("@")[0], "r": role})
    db.commit()
    return uid


def _category(db: Session) -> uuid.UUID:
    row = db.execute(text("""
        SELECT id FROM expense_category_policy
         WHERE org_id = :o AND deleted_at IS NULL AND is_active LIMIT 1
    """), {"o": str(ORG)}).first()
    assert row is not None, "비용 카테고리 정책이 seed 에 없다"
    return uuid.UUID(str(row.id))


def _expense(db: Session, *, at: date, amount: int = 120000) -> uuid.UUID:
    eid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO expenses
               (id, org_id, expense_date, category_id, description,
                gross_amount, supply_amount, vat_amount, vat_inclusion_type,
                tax_type, payment_method, is_personal_card, is_business_use,
                evidence_type, evidence_status,
                is_tax_deductible, is_settlement_deductible)
        VALUES (:i, :o, :d, :c, '테스트 비용',
                :g, :g, 0, 'INCLUDED',
                'GENERAL', 'BUSINESS_CARD', FALSE, TRUE,
                'CARD_SLIP', 'PRESENT', TRUE, TRUE)
    """), {"i": str(eid), "o": str(ORG), "d": at, "c": str(_category(db)), "g": amount})
    db.commit()
    return eid


def _lock_month(db: Session, ym: str, approver: uuid.UUID, state: str = "LOCKED") -> None:
    db.execute(text("""
        INSERT INTO monthly_settlement
               (id, org_id, business_key, year_month, status, close_state,
                confirmed_at, confirmed_by)
        VALUES (gen_random_uuid(), :o, :bk, :ym, 'LOCKED', :cs, now(), :by)
        ON CONFLICT DO NOTHING
    """), {"o": str(ORG), "bk": f"MS-EXP-{uuid.uuid4().hex}", "ym": ym,
           "cs": state, "by": str(approver)})
    db.commit()


@pytest.fixture
def ctx(db: Session):
    actor = _user(db, "exp-op@picksome.local", "OPERATOR")
    approver = _user(db, "exp-admin@picksome.local", "ADMIN")
    yield {"db": db, "actor": actor, "approver": approver}

    db.rollback()
    db.execute(text("DELETE FROM expense_adjustment WHERE org_id = :o"), {"o": str(ORG)})
    db.execute(text("DELETE FROM period_recalc_flag WHERE org_id = :o"), {"o": str(ORG)})
    db.execute(text("DELETE FROM approval_request WHERE org_id = :o"), {"o": str(ORG)})
    db.execute(text("DELETE FROM expenses WHERE org_id = :o"), {"o": str(ORG)})
    purge_month_close(db, org_id=ORG)
    db.commit()


def _request(ctx, expense_id, request_type="EXPENSE_FINANCIAL_CHANGE", **changes):
    return approval_svc.create_request(
        ctx["db"], org_id=ORG, actor_user_id=ctx["actor"],
        target_type="expense", target_id=expense_id, request_type=request_type,
        proposed_changes=changes, reason="비용 정정", submit=True,
    )


def _approve_and_apply(ctx, request_id):
    db = ctx["db"]
    approval_svc.approve(db, org_id=ORG, request_id=request_id,
                         actor_user_id=ctx["approver"], actor_role="ADMIN")
    return approval_svc.apply(db, org_id=ORG, request_id=request_id,
                              actor_user_id=ctx["approver"], actor_role="ADMIN")


# ──────────────────────────────────────────────────────────────
# 1. 승인 우회 차단
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("field,value", [
    ("gross_amount", 185000),
    ("expense_date", date(2026, 9, 1)),
    ("is_tax_deductible", False),
    ("is_settlement_deductible", False),
    ("is_personal_card", True),
    ("description", "우회 시도"),
])
def test_approval_fields_cannot_be_changed_directly(ctx, field, value):
    """**핵심.** 승인 대상 필드는 일반 수정 API 로 못 바꾼다.

    비용 금액은 순이익의 입력값이고 순이익이 곧 파트너 수익배분이다.
    승인 화면을 만들어 놓고 일반 API 를 열어두면 승인은 장식이 된다.
    """
    db = ctx["db"]
    eid = _expense(db, at=date(2026, 8, 10))

    with pytest.raises(ApprovalRequiredError) as exc:
        expense_svc.update_expense(db, org_id=ORG, actor=ctx["actor"], expense_id=eid,
                                   body=ExpenseUpdate(**{field: value}))
    assert field in exc.value.detail["approval_required_fields"]
    db.rollback()


def test_memo_is_directly_savable(ctx):
    """메모는 승인 대상이 아니다 — 운영 영향이 없는 값까지 승인을 태우면
    승인함이 잡음으로 차고 정작 중요한 금액 변경이 묻힌다."""
    db = ctx["db"]
    eid = _expense(db, at=date(2026, 8, 10))
    out = expense_svc.update_expense(db, org_id=ORG, actor=ctx["actor"], expense_id=eid,
                                     body=ExpenseUpdate(memo="운영 메모"))
    assert out.memo == "운영 메모"


def test_mixed_payload_is_fully_rejected(ctx):
    """섞어 보내면 **전부 거부**한다. 일부만 저장하면 무엇이 반영됐는지 알 수 없다."""
    db = ctx["db"]
    eid = _expense(db, at=date(2026, 8, 10))
    with pytest.raises(ApprovalRequiredError):
        expense_svc.update_expense(
            db, org_id=ORG, actor=ctx["actor"], expense_id=eid,
            body=ExpenseUpdate(memo="이건 되지만", gross_amount=999999))
    db.rollback()

    after = expense_svc.get_expense(db, org_id=ORG, expense_id=eid)
    assert after.memo != "이건 되지만", "일부만 저장됐다"
    assert after.gross_amount == 120000


# ──────────────────────────────────────────────────────────────
# 2. 승인 흐름
# ──────────────────────────────────────────────────────────────

def test_expense_unchanged_until_applied(ctx):
    db = ctx["db"]
    eid = _expense(db, at=date(2026, 8, 10))
    _request(ctx, eid, gross_amount=185000)

    assert expense_svc.get_expense(db, org_id=ORG, expense_id=eid).gross_amount == 120000


def test_approval_apply_changes_expense(ctx):
    db = ctx["db"]
    eid = _expense(db, at=date(2026, 8, 10))
    req = _request(ctx, eid, gross_amount=185000)
    _approve_and_apply(ctx, uuid.UUID(str(req["id"])))

    assert expense_svc.get_expense(db, org_id=ORG, expense_id=eid).gross_amount == 185000


def test_self_approval_is_blocked(ctx):
    """요청자 본인은 승인할 수 없다.

    비용 승인 역할은 ADMIN·ACCOUNTING 이라 OPERATOR 요청자는 역할 검사에서 먼저
    걸린다. 어느 쪽이든 **본인이 자기 요청을 통과시킬 수 없다**는 게 요점이다.
    """
    from app.core.exceptions import ForbiddenError

    db = ctx["db"]
    eid = _expense(db, at=date(2026, 8, 10))
    req = _request(ctx, eid, gross_amount=185000)

    with pytest.raises(ForbiddenError):
        approval_svc.approve(db, org_id=ORG, request_id=uuid.UUID(str(req["id"])),
                             actor_user_id=ctx["actor"], actor_role="OPERATOR")
    db.rollback()


def test_accounting_can_approve_expense(ctx):
    """정산·세무 담당이 비용 승인 주체다."""
    db = ctx["db"]
    accountant = _user(db, "exp-acc@picksome.local", "ACCOUNTING")
    eid = _expense(db, at=date(2026, 8, 10))
    req = _request(ctx, eid, gross_amount=185000)
    rid = uuid.UUID(str(req["id"]))

    approval_svc.approve(db, org_id=ORG, request_id=rid,
                         actor_user_id=accountant, actor_role="ACCOUNTING")
    approval_svc.apply(db, org_id=ORG, request_id=rid,
                       actor_user_id=accountant, actor_role="ACCOUNTING")
    assert expense_svc.get_expense(db, org_id=ORG, expense_id=eid).gross_amount == 185000


def test_field_not_owned_by_request_type_rejected(ctx):
    """증빙 변경 요청으로 금액을 바꿀 수 없다 — 두 승인이 서로를 덮어쓴다."""
    db = ctx["db"]
    eid = _expense(db, at=date(2026, 8, 10))
    with pytest.raises(ValidationError):
        _request(ctx, eid, request_type="EXPENSE_EVIDENCE_CHANGE", gross_amount=999)
    db.rollback()


def test_unknown_field_rejected(ctx):
    db = ctx["db"]
    eid = _expense(db, at=date(2026, 8, 10))
    with pytest.raises(ValidationError):
        _request(ctx, eid, linked_settlement_id=str(uuid.uuid4()))
    db.rollback()


def test_negative_amount_rejected(ctx):
    """되돌림은 음수 금액이 아니라 **조정**으로 처리한다."""
    db = ctx["db"]
    eid = _expense(db, at=date(2026, 8, 10))
    with pytest.raises(ValidationError) as exc:
        _request(ctx, eid, gross_amount=-1000)
    assert "조정" in str(exc.value) or "음수" in str(exc.value)
    db.rollback()


def test_org_isolation(ctx):
    db = ctx["db"]
    eid = _expense(db, at=date(2026, 8, 10))
    other = uuid.UUID("00000000-0000-0000-0000-0000000000fe")
    with pytest.raises(NotFoundError):
        approval_svc.create_request(
            db, org_id=other, actor_user_id=ctx["actor"],
            target_type="expense", target_id=eid,
            request_type="EXPENSE_FINANCIAL_CHANGE",
            proposed_changes={"gross_amount": 1}, submit=True)
    db.rollback()


def test_amount_is_sensitive(ctx):
    """금액·결제자는 사내 민감정보다. 권한 없는 사용자에게 값을 내려주지 않는다."""
    from app.core.sensitive import build_redacted_diff, is_sensitive

    assert is_sensitive("expense", "gross_amount")
    assert is_sensitive("expense", "payment_card_holder_id")
    assert is_sensitive("expense", "is_personal_card")
    # 비용 내용은 승인자가 판단하려면 봐야 한다.
    assert not is_sensitive("expense", "description")

    rows = build_redacted_diff("expense", {"gross_amount": 120000},
                               {"gross_amount": 185000}, allowed=False)
    row = next(r for r in rows if r["field"] == "gross_amount")
    assert row["redacted"] is True
    assert row["before"] is None and row["after"] is None
    assert row["label"] == "총액"


# ──────────────────────────────────────────────────────────────
# 3. 월 잠금 — 승인으로도 우회되지 않는다
# ──────────────────────────────────────────────────────────────

def test_locked_month_expense_cannot_be_changed_directly(ctx):
    db = ctx["db"]
    eid = _expense(db, at=date(2026, 7, 10))
    _lock_month(db, "2026-07", ctx["approver"])

    with pytest.raises(SettlementLockedError):
        expense_svc.update_expense(db, org_id=ORG, actor=ctx["actor"], expense_id=eid,
                                   body=ExpenseUpdate(memo="잠긴 달 메모"))
    db.rollback()


def test_locked_month_expense_cannot_be_changed_by_approval(ctx):
    """**핵심.** 승인이 월 잠금을 우회하지 않는다.

    승인만으로 잠긴 달을 바꿀 수 있으면 잠금이 의미를 잃는다.
    요청은 만들 수 있지만 **적용에서 막힌다** — 승인 사실은 유효하고,
    사람이 조정으로 처리할지 월을 재오픈할지 결정한다.
    """
    db = ctx["db"]
    eid = _expense(db, at=date(2026, 7, 10))
    req = _request(ctx, eid, gross_amount=185000)
    _lock_month(db, "2026-07", ctx["approver"])

    rid = uuid.UUID(str(req["id"]))
    approval_svc.approve(db, org_id=ORG, request_id=rid,
                         actor_user_id=ctx["approver"], actor_role="ADMIN")
    with pytest.raises(SettlementLockedError):
        approval_svc.apply(db, org_id=ORG, request_id=rid,
                           actor_user_id=ctx["approver"], actor_role="ADMIN")
    db.rollback()

    # 원본은 그대로다.
    assert expense_svc.get_expense(db, org_id=ORG, expense_id=eid).gross_amount == 120000


def test_moving_date_out_of_locked_month_is_blocked(ctx):
    """잠긴 7월 비용을 열린 8월로 옮기는 것도 막힌다.

    옮기면 7월 합계가 줄어든다 — 확정된 달의 숫자가 바뀌는 것과 같다.
    `get_effective_date` 가 **더 이른 쪽**을 돌려주기 때문에 잡힌다.
    """
    db = ctx["db"]
    eid = _expense(db, at=date(2026, 7, 10))
    req = _request(ctx, eid, expense_date=date(2026, 8, 10))
    _lock_month(db, "2026-07", ctx["approver"])

    rid = uuid.UUID(str(req["id"]))
    approval_svc.approve(db, org_id=ORG, request_id=rid,
                         actor_user_id=ctx["approver"], actor_role="ADMIN")
    with pytest.raises(SettlementLockedError):
        approval_svc.apply(db, org_id=ORG, request_id=rid,
                           actor_user_id=ctx["approver"], actor_role="ADMIN")
    db.rollback()


# ──────────────────────────────────────────────────────────────
# 4. 조정 비용
# ──────────────────────────────────────────────────────────────

def test_requires_adjustment_only_for_closed_months(ctx):
    db = ctx["db"]
    assert adj_svc.requires_adjustment(db, org_id=ORG, at=date(2026, 8, 10)) is False
    _lock_month(db, "2026-07", ctx["approver"])
    assert adj_svc.requires_adjustment(db, org_id=ORG, at=date(2026, 7, 10)) is True


def test_adjustment_creates_expense_in_open_month(ctx):
    """조정은 **열린 달**에 실제 비용 행을 만든다. 집계가 자동으로 포함하도록."""
    db = ctx["db"]
    original = _expense(db, at=date(2026, 7, 10), amount=120000)
    _lock_month(db, "2026-07", ctx["approver"])

    out = adj_svc.create_adjustment(
        db, org_id=ORG, actor=ctx["actor"], original_expense_id=original,
        adjustment_type="CORRECTION", adjustment_reason="7월 광고비 과소 계상",
        adjustment_date=date(2026, 8, 15), amount_delta=65000,
    )

    assert out["original_period"] == "2026-07"
    assert out["adjustment_period"] == "2026-08"

    adj_expense = db.execute(text("""
        SELECT expense_date, gross_amount FROM expenses WHERE id = :i
    """), {"i": out["adjustment_expense_id"]}).first()
    assert adj_expense.expense_date == date(2026, 8, 15)
    assert adj_expense.gross_amount == 65000


def test_adjustment_leaves_original_untouched(ctx):
    """**핵심.** 원본 비용은 금액도 날짜도 그대로다."""
    db = ctx["db"]
    original = _expense(db, at=date(2026, 7, 10), amount=120000)
    _lock_month(db, "2026-07", ctx["approver"])

    adj_svc.create_adjustment(
        db, org_id=ORG, actor=ctx["actor"], original_expense_id=original,
        adjustment_type="CORRECTION", adjustment_reason="정정",
        adjustment_date=date(2026, 8, 15), amount_delta=65000,
    )

    row = db.execute(text("SELECT gross_amount, expense_date FROM expenses WHERE id = :i"),
                     {"i": str(original)}).first()
    assert row.gross_amount == 120000
    assert row.expense_date == date(2026, 7, 10)


def test_adjustment_into_locked_month_is_rejected(ctx):
    """조정도 잠긴 달에는 못 넣는다 — 그러면 잠금 우회가 된다."""
    db = ctx["db"]
    original = _expense(db, at=date(2026, 7, 10))
    _lock_month(db, "2026-07", ctx["approver"])

    with pytest.raises(ConflictError):
        adj_svc.create_adjustment(
            db, org_id=ORG, actor=ctx["actor"], original_expense_id=original,
            adjustment_type="CORRECTION", adjustment_reason="정정",
            adjustment_date=date(2026, 7, 20), amount_delta=1000,
        )
    db.rollback()


def test_late_entry_needs_no_original(ctx):
    """누락분 신규 등록은 원본이 없다."""
    db = ctx["db"]
    out = adj_svc.create_adjustment(
        db, org_id=ORG, actor=ctx["actor"], original_expense_id=None,
        adjustment_type="LATE_ENTRY", adjustment_reason="7월 누락 비용",
        adjustment_date=date(2026, 8, 15), amount_delta=50000,
        payload={"category_id": _category(db)},
    )
    assert out["original_expense_id"] is None
    assert out["original_period"] is None


def test_correction_requires_original(ctx):
    db = ctx["db"]
    with pytest.raises(ValidationError):
        adj_svc.create_adjustment(
            db, org_id=ORG, actor=ctx["actor"], original_expense_id=None,
            adjustment_type="CORRECTION", adjustment_reason="원본 없이 정정",
            adjustment_date=date(2026, 8, 15), amount_delta=1000,
            payload={"category_id": _category(db)},
        )
    db.rollback()


def test_adjustment_reason_is_required(ctx):
    db = ctx["db"]
    original = _expense(db, at=date(2026, 7, 10))
    with pytest.raises(ValidationError):
        adj_svc.create_adjustment(
            db, org_id=ORG, actor=ctx["actor"], original_expense_id=original,
            adjustment_type="CORRECTION", adjustment_reason="   ",
            adjustment_date=date(2026, 8, 15), amount_delta=1000,
        )
    db.rollback()


def test_zero_delta_rejected(ctx):
    db = ctx["db"]
    original = _expense(db, at=date(2026, 7, 10))
    with pytest.raises(ValidationError):
        adj_svc.create_adjustment(
            db, org_id=ORG, actor=ctx["actor"], original_expense_id=original,
            adjustment_type="CORRECTION", adjustment_reason="변화 없음",
            adjustment_date=date(2026, 8, 15), amount_delta=0,
        )
    db.rollback()


def test_adjustment_links_to_original(ctx):
    db = ctx["db"]
    original = _expense(db, at=date(2026, 7, 10))
    _lock_month(db, "2026-07", ctx["approver"])
    adj_svc.create_adjustment(
        db, org_id=ORG, actor=ctx["actor"], original_expense_id=original,
        adjustment_type="REVERSAL", adjustment_reason="중복 계상 취소",
        adjustment_date=date(2026, 8, 15), amount_delta=-120000,
    )

    rows = adj_svc.list_adjustments(db, org_id=ORG, original_expense_id=original)
    assert len(rows) == 1
    assert rows[0]["adjustment_type"] == "REVERSAL"
    assert rows[0]["amount_delta"] == -120000


# ──────────────────────────────────────────────────────────────
# 5. 재계산 표시 — 과거 확정 데이터는 건드리지 않는다
# ──────────────────────────────────────────────────────────────

def test_apply_marks_period_dirty(ctx):
    """승인 적용이 그 달을 '재계산 필요' 로 표시한다. **재계산 자체는 하지 않는다.**"""
    db = ctx["db"]
    eid = _expense(db, at=date(2026, 8, 10))
    req = _request(ctx, eid, gross_amount=185000)
    _approve_and_apply(ctx, uuid.UUID(str(req["id"])))

    assert period_recalc.is_dirty(db, org_id=ORG, year_month="2026-08")


def test_date_change_marks_both_months(ctx):
    """날짜를 옮기면 **두 달 모두** 낡는다 — 이전 달 합계도 줄어든다."""
    db = ctx["db"]
    eid = _expense(db, at=date(2026, 8, 10))
    req = _request(ctx, eid, expense_date=date(2026, 9, 5))
    _approve_and_apply(ctx, uuid.UUID(str(req["id"])))

    assert period_recalc.is_dirty(db, org_id=ORG, year_month="2026-08")
    assert period_recalc.is_dirty(db, org_id=ORG, year_month="2026-09")


def test_locked_month_is_never_marked_dirty(ctx):
    """확정된 달을 재계산 대상으로 표시하지 않는다.

    표시해 두면 언젠가 배치가 그걸 실행해 확정 숫자를 덮어쓴다.
    """
    db = ctx["db"]
    _lock_month(db, "2026-07", ctx["approver"])
    marked = period_recalc.mark_period_dirty(
        db, org_id=ORG, at=date(2026, 7, 10), reason="시도", actor=ctx["actor"])
    db.commit()

    assert marked is None
    assert period_recalc.is_dirty(db, org_id=ORG, year_month="2026-07") is False


def test_adjustment_marks_only_open_month(ctx):
    """조정은 열린 달만 낡게 한다. 원본 달은 확정이라 건드리지 않는다."""
    db = ctx["db"]
    original = _expense(db, at=date(2026, 7, 10))
    _lock_month(db, "2026-07", ctx["approver"])
    adj_svc.create_adjustment(
        db, org_id=ORG, actor=ctx["actor"], original_expense_id=original,
        adjustment_type="CORRECTION", adjustment_reason="정정",
        adjustment_date=date(2026, 8, 15), amount_delta=65000,
    )

    assert period_recalc.is_dirty(db, org_id=ORG, year_month="2026-08")
    assert period_recalc.is_dirty(db, org_id=ORG, year_month="2026-07") is False


def test_apply_and_dirty_flag_are_atomic(ctx):
    """감사 로그가 실패하면 비용 변경도 재계산 표시도 남지 않는다."""
    db = ctx["db"]
    eid = _expense(db, at=date(2026, 8, 10))
    req = _request(ctx, eid, gross_amount=185000)
    rid = uuid.UUID(str(req["id"]))
    approval_svc.approve(db, org_id=ORG, request_id=rid,
                         actor_user_id=ctx["approver"], actor_role="ADMIN")

    import app.services.approval as approval_module

    original_audit = approval_module.record_audit

    def boom(*a, **k):
        raise RuntimeError("감사 로그 실패")

    approval_module.record_audit = boom
    try:
        with pytest.raises(RuntimeError):
            approval_svc.apply(db, org_id=ORG, request_id=rid,
                               actor_user_id=ctx["approver"], actor_role="ADMIN")
    finally:
        approval_module.record_audit = original_audit
    db.rollback()

    assert expense_svc.get_expense(db, org_id=ORG, expense_id=eid).gross_amount == 120000
    assert period_recalc.is_dirty(db, org_id=ORG, year_month="2026-08") is False
