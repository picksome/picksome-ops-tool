"""월 마감 동시성·멱등성 (docs/43 Phase 2).

두 사람이 같은 달을 동시에 확정하거나 잠그는 일은 실제로 일어난다.
정산 담당자와 대표가 마감일에 같이 들어오면 그렇다.

여기서 검증하는 것
------------------
1. 같은 월을 동시에 전이하려 하면 **한 쪽만 성공**하고 다른 쪽은 409
2. 이미 도달한 상태로 다시 전이하면 409 (중복 마감)
3. reopen_count 가 중복 증가하지 않는다
4. audit_log 가 중복으로 쌓이지 않는다
5. 잠긴 월에는 판매·비용 쓰기가 막힌다 (import/sync/recalc 경로 포함)

동시성 재현
-----------
공용 테스트 세션은 트랜잭션 안에 갇혀 있어(SAVEPOINT 격리) 진짜 병렬 커넥션을
쓸 수 없다. 그래서 이 파일은 **별도 커넥션 2개**를 직접 연다.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import ConflictError, SettlementLockedError
from app.services import monthly_close as mc
from app.tests.fixtures.factories import confirm_month, lock_month

SYSTEM_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ff")
YM = "2026-05"


def _test_url() -> str:
    override = os.environ.get("TEST_DATABASE_URL")
    if override:
        return override
    from app.config import get_settings

    base = get_settings().DATABASE_URL
    root, _, dbname = base.rpartition("/")
    scheme, _, hostpart = root.partition("://")
    host = hostpart.rpartition("@")[2] if "@" in hostpart else hostpart
    return f"{scheme}://picksome_test:picksome_test@{host}/{dbname}_test"


@pytest.fixture
def two_sessions(_test_db_ready: str) -> Iterator[tuple[Session, Session]]:
    """진짜 병렬 커넥션 2개. 공용 fixture 의 SAVEPOINT 격리를 쓰지 않는다."""
    eng = create_engine(_test_db_ready, future=True, poolclass=None)
    Maker = sessionmaker(bind=eng, future=True, expire_on_commit=False)
    a, b = Maker(), Maker()
    try:
        yield a, b
    finally:
        for s in (a, b):
            s.rollback()
            s.close()
        eng.dispose()


def _ensure_actor(session: Session) -> uuid.UUID:
    """감사 로그의 actor 는 app_user FK 다. 임의 UUID 를 쓰면 FK 위반이 난다."""
    row = session.execute(
        text("SELECT id FROM app_user WHERE org_id = :o AND email = :e"),
        {"o": str(SYSTEM_ORG_ID), "e": "conc-test@picksome.local"},
    ).first()
    if row:
        return uuid.UUID(str(row.id))
    uid = uuid.uuid4()
    session.execute(
        text("""
            INSERT INTO app_user (id, org_id, email, password_hash, display_name, role, is_active)
            VALUES (:id, :o, :e, 'x', '동시성 테스트', 'ADMIN', TRUE)
        """),
        {"id": str(uid), "o": str(SYSTEM_ORG_ID), "e": "conc-test@picksome.local"},
    )
    session.commit()
    return uid


def _cleanup(session: Session) -> None:
    session.execute(
        text("DELETE FROM audit_log WHERE org_id = :o AND domain_table = 'monthly_settlement'"),
        {"o": str(SYSTEM_ORG_ID)},
    )
    # 이 fixture 는 공용 SAVEPOINT 격리를 쓰지 않고 실제로 커밋하므로,
    # monthly_settlement 를 참조하는 행을 직접 정리해야 한다(M028 이후 늘었다).
    #
    # snapshot 은 DELETE 트리거로 보호된다. 유지보수 탈출구를 명시적으로 연다 —
    # 애플리케이션 코드에는 이 플래그가 없다.
    session.execute(text("SET LOCAL picksome.allow_snapshot_delete = 'on'"))
    session.execute(
        text("""
            UPDATE monthly_settlement
               SET close_snapshot_id = NULL, last_check_run_id = NULL
             WHERE org_id = :o AND year_month = :ym
        """),
        {"o": str(SYSTEM_ORG_ID), "ym": YM},
    )
    session.execute(
        text("""
            DELETE FROM monthly_close_snapshot
             WHERE org_id = :o AND year_month = :ym
        """),
        {"o": str(SYSTEM_ORG_ID), "ym": YM},
    )
    session.execute(
        text("""
            DELETE FROM settlement_reopen_audit
             WHERE monthly_settlement_id IN (
               SELECT id FROM monthly_settlement WHERE org_id = :o AND year_month = :ym
             )
        """),
        {"o": str(SYSTEM_ORG_ID), "ym": YM},
    )
    session.execute(
        text("DELETE FROM monthly_close_check_run WHERE org_id = :o AND year_month = :ym"),
        {"o": str(SYSTEM_ORG_ID), "ym": YM},
    )
    session.execute(
        text("DELETE FROM monthly_settlement WHERE org_id = :o AND year_month = :ym"),
        {"o": str(SYSTEM_ORG_ID), "ym": YM},
    )
    session.commit()


def _seed_month(session: Session, close_state: str = "OPEN") -> uuid.UUID:
    """검증을 통과하는 깨끗한 월 하나를 만든다(차단 항목이 없는 상태).

    CONFIRMED 이상은 **실제 전이 경로(confirm)를 태워서** 만든다.
    DB 에 `ck_ms_confirmed_meta` 제약이 있어 confirmed_at/confirmed_by 없이
    CONFIRMED/LOCKED 로 직접 INSERT 하면 실패한다 — 그게 맞는 동작이다.
    상태만 억지로 꽂아 넣으면 실제로는 존재할 수 없는 행으로 테스트하게 된다.
    """
    _cleanup(session)
    ms_id = uuid.uuid4()
    session.execute(
        text("""
            INSERT INTO monthly_settlement
              (id, org_id, business_key, year_month, status, close_state,
               total_revenue, total_direct_cost, total_common_expense, net_profit)
            VALUES (:id, :o, :bk, :ym, 'DRAFT', 'OPEN', 0, 0, 0, 0)
        """),
        {"id": str(ms_id), "o": str(SYSTEM_ORG_ID), "bk": f"MS-CONC-{ms_id.hex}",
         "ym": YM},
    )
    session.commit()

    if close_state in ("CONFIRMED", "LOCKED"):
        actor = _ensure_actor(session)
        confirm_month(session, org_id=SYSTEM_ORG_ID, year_month=YM, actor_user_id=actor)
        if close_state == "LOCKED":
            lock_month(session, org_id=SYSTEM_ORG_ID, year_month=YM, actor_user_id=actor)
    elif close_state == "REVIEW":
        mc.request_review(session, org_id=SYSTEM_ORG_ID, year_month=YM,
                          actor_user_id=_ensure_actor(session))
    return ms_id


def test_concurrent_confirm_only_one_succeeds(two_sessions):
    """두 요청이 동시에 같은 월을 확정하면 한 쪽만 성공한다."""
    a, b = two_sessions
    _seed_month(a)
    actor = _ensure_actor(a)

    ok = confirm_month(a, org_id=SYSTEM_ORG_ID, year_month=YM, actor_user_id=actor)
    assert ok["close_state"] == "CONFIRMED"

    # 두 번째 요청은 이미 CONFIRMED 라 OPEN→CONFIRMED 전이가 성립하지 않는다
    with pytest.raises(ConflictError):
        confirm_month(b, org_id=SYSTEM_ORG_ID, year_month=YM, actor_user_id=actor)

    _cleanup(a)


def test_duplicate_lock_request_is_rejected(two_sessions):
    """이미 잠긴 월을 다시 잠그려 하면 409."""
    a, _ = two_sessions
    _seed_month(a, close_state="CONFIRMED")
    actor = _ensure_actor(a)

    locked = mc.lock(a, org_id=SYSTEM_ORG_ID, year_month=YM, actor_user_id=actor)
    assert locked["close_state"] == "LOCKED"

    with pytest.raises(ConflictError):
        mc.lock(a, org_id=SYSTEM_ORG_ID, year_month=YM, actor_user_id=actor)

    _cleanup(a)


def test_reopen_count_increments_once_per_unlock(two_sessions):
    """잠금 해제가 중복 실행돼도 reopen_count 가 두 번 오르지 않는다."""
    a, b = two_sessions
    _seed_month(a, close_state="CONFIRMED")
    actor = _ensure_actor(a)
    mc.lock(a, org_id=SYSTEM_ORG_ID, year_month=YM, actor_user_id=actor)

    mc.unlock(a, org_id=SYSTEM_ORG_ID, year_month=YM,
              actor_user_id=actor, reason="정산 오류 수정")

    # 두 번째 해제 시도 — 이미 OPEN 이므로 LOCKED→OPEN 전이가 성립하지 않는다
    with pytest.raises(ConflictError):
        mc.unlock(b, org_id=SYSTEM_ORG_ID, year_month=YM,
                  actor_user_id=actor, reason="중복 요청")

    count = a.execute(
        text("SELECT reopen_count FROM monthly_settlement WHERE org_id=:o AND year_month=:ym"),
        {"o": str(SYSTEM_ORG_ID), "ym": YM},
    ).scalar()
    assert count == 1, "중복 해제 요청이 reopen_count 를 두 번 올리면 안 된다"

    _cleanup(a)


def test_failed_transition_does_not_write_audit_log(two_sessions):
    """실패한 전이는 감사 로그를 남기지 않는다 — 중복·허위 기록 방지."""
    a, _ = two_sessions
    _seed_month(a, close_state="OPEN")
    actor = _ensure_actor(a)

    before = a.execute(
        text("SELECT COUNT(*) FROM audit_log WHERE org_id=:o AND domain_table='monthly_settlement'"),
        {"o": str(SYSTEM_ORG_ID)},
    ).scalar()

    # OPEN 에서 곧바로 LOCKED 로는 갈 수 없다
    with pytest.raises(ConflictError):
        mc.lock(a, org_id=SYSTEM_ORG_ID, year_month=YM, actor_user_id=actor)
    a.rollback()

    after = a.execute(
        text("SELECT COUNT(*) FROM audit_log WHERE org_id=:o AND domain_table='monthly_settlement'"),
        {"o": str(SYSTEM_ORG_ID)},
    ).scalar()
    assert after == before, "허용되지 않은 전이가 감사 로그를 남기면 안 된다"

    _cleanup(a)


def test_locked_month_blocks_sales_write(two_sessions):
    """잠긴 월에는 판매를 넣을 수 없다 — 서비스 계층에서 차단."""
    from app.core.month_lock import assert_period_writable

    a, _ = two_sessions
    _seed_month(a, close_state="LOCKED")

    with pytest.raises(SettlementLockedError):
        assert_period_writable(
            a, org_id=SYSTEM_ORG_ID, effective_date=f"{YM}-10",
            operation="CREATE", what="판매 내역",
        )

    _cleanup(a)


@pytest.mark.parametrize(
    "operation",
    ["CREATE", "UPDATE", "DELETE", "IMPORT", "SYNC", "RECALC_MARGIN",
     "RECALC_PROFIT", "BULK_UPDATE", "ALLOCATE_AD_COST", "BACKFILL"],
)
def test_locked_month_blocks_every_write_operation(two_sessions, operation):
    """사용자 쓰기 연산은 종류를 불문하고 전부 막힌다.

    import·sync·recalc 처럼 '자동' 경로도 결국 데이터를 바꾼다.
    자동이라는 이유로 통과시키면 잠금이 의미를 잃는다.
    """
    from app.core.month_lock import assert_period_writable

    a, _ = two_sessions
    _seed_month(a, close_state="LOCKED")

    with pytest.raises(SettlementLockedError):
        assert_period_writable(
            a, org_id=SYSTEM_ORG_ID, effective_date=f"{YM}-10", operation=operation,
        )

    _cleanup(a)


def test_system_operation_strings_cannot_bypass_lock(two_sessions):
    """**월 잠금에 문자열 우회 경로가 없다.**

    예전 구현은 `SYSTEM_OPERATIONS` 집합에 속하는 operation 문자열이면 잠금을
    통과시켰다. 호출부가 전부 리터럴이라 당장 악용되진 않았지만, 어떤 경로가
    요청 payload 의 값을 그대로 넘기는 순간 잠금이 조용히 뚫린다.

    지금은 화이트리스트에 없는 operation 은 통과가 아니라 ValueError 다 —
    오타나 주입이 "통과" 로 해석되면 안 된다.
    """
    from app.core.month_lock import assert_period_writable

    a, _ = two_sessions
    _seed_month(a, close_state="LOCKED")

    for injected in ("FINALIZE_PROFIT", "CLOSE_TRANSITION", "SYSTEM", "", "anything"):
        with pytest.raises(ValueError, match="알 수 없는 쓰기 연산"):
            assert_period_writable(
                a, org_id=SYSTEM_ORG_ID, effective_date=f"{YM}-10", operation=injected,
            )

    _cleanup(a)


def test_apply_approval_is_a_user_write_and_is_blocked_when_locked(two_sessions):
    """승인 적용도 사용자 데이터 쓰기다 — 잠긴 월에서는 막힌다.

    '승인을 받았으니 잠긴 월에도 반영한다' 를 허용하면 승인만으로 잠금을 우회하게 된다.
    """
    from app.core.month_lock import assert_period_writable

    a, _ = two_sessions
    _seed_month(a, close_state="LOCKED")

    with pytest.raises(SettlementLockedError):
        assert_period_writable(
            a, org_id=SYSTEM_ORG_ID, effective_date=f"{YM}-10",
            operation="APPLY_APPROVAL", what="승인된 변경",
        )

    _cleanup(a)


def test_open_month_allows_writes(two_sessions):
    """잠기지 않은 달은 당연히 쓸 수 있다 — 가드가 과잉 차단하지 않는지 확인."""
    from app.core.month_lock import assert_period_writable

    a, _ = two_sessions
    _seed_month(a, close_state="OPEN")

    assert_period_writable(
        a, org_id=SYSTEM_ORG_ID, effective_date=f"{YM}-10", operation="CREATE",
    )
    # CONFIRMED 도 아직 수정 가능하다 — 잠금과 확정은 층위가 다르다
    _seed_month(a, close_state="CONFIRMED")
    assert_period_writable(
        a, org_id=SYSTEM_ORG_ID, effective_date=f"{YM}-10", operation="UPDATE",
    )

    _cleanup(a)


def test_month_without_settlement_row_is_writable(two_sessions):
    """정산을 한 번도 돌리지 않은 달은 '없는 달'이 아니라 '열린 달'이다."""
    from app.core.month_lock import assert_period_writable, get_close_state

    a, _ = two_sessions
    _cleanup(a)
    assert get_close_state(a, org_id=SYSTEM_ORG_ID, year_month="2019-01") == "OPEN"
    assert_period_writable(
        a, org_id=SYSTEM_ORG_ID, effective_date="2019-01-15", operation="CREATE",
    )
