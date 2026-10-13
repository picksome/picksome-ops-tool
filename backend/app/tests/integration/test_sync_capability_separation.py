"""주문 동기화와 클레임 수집을 섞지 않는다 (M057).

무엇이 잘못돼 있었나
------------------
`external_sync_run.sync_type` CHECK 에 `CLAIMS` 가 없어서 클레임 동기화가
`ORDERS` 로 기록했다. "무엇을 읽었는지는 자동화 실행 기록이 구분한다" 는
전제였는데, `worker_run_id` 가 비어 있어 실제로는 구분되지 않았다.

실측에서 이렇게 보였다.

```
08:19  ORDERS  read=16 conflict=14   ← 실제로는 클레임 16건
08:14  ORDERS  read=0                ← 실제 주문 동기화
```

주문 화면은 "조회 16건" 을 주문 숫자로 보여줬고, 클레임이 실패하면
"주문 동기화 실패" 라고 말했다.

둘은 다른 endpoint 이고 따로 켠다. 하나가 다른 하나의 상태로 보이면 사용자는
멀쩡한 쪽을 고치러 간다.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.services import claim_sync, exception_center, order_sync
from app.tests.fixtures.factories import make_app_user, make_organization


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


def _account(db: Session, org: uuid.UUID, actor: uuid.UUID) -> uuid.UUID:
    sys_id = db.execute(text("""
        INSERT INTO external_system (code, name, system_type, supports_api_key)
        VALUES ('NEXT_ENGINE', 'NEXT ENGINE', 'SELLER_TOOL', TRUE)
        ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id
    """)).scalar()
    return uuid.UUID(str(db.execute(text("""
        INSERT INTO external_account (org_id, external_system_id, account_code,
               display_name, credential_kind, status, created_by, updated_by)
        VALUES (:o, :s, 'DEFAULT', 'NE 본사', 'API_KEY', 'CONNECTED', :a, :a)
        RETURNING id
    """), {"o": str(org), "s": str(sys_id), "a": str(actor)}).scalar()))


def _run(db, org, account, *, sync_type, status, read=0, conflict=0,
         error_code=None):
    return db.execute(text("""
        INSERT INTO external_sync_run
               (org_id, external_account_id, sync_type, trigger, status,
                started_at, finished_at, read_count, conflict_count, error_code)
        VALUES (:o, :a, :st, 'SCHEDULED', :status, now(), now(), :rd, :cf, :ec)
        RETURNING id
    """), {"o": str(org), "a": str(account), "st": sync_type, "status": status,
           "rd": read, "cf": conflict, "ec": error_code}).scalar()


# ──────────────────────────────────────────────────────────────────────

def test_claim_sync_records_its_own_type(db: Session):
    """클레임은 `CLAIMS` 로 기록한다 — 주문과 같은 값을 쓰지 않는다."""
    assert claim_sync.SYNC_TYPE == "CLAIMS"
    assert order_sync.SYNC_TYPE == "ORDERS"
    assert claim_sync.SYNC_TYPE != order_sync.SYNC_TYPE


def test_claims_is_an_allowed_sync_type(db: Session, org, actor):
    account = _account(db, org, actor)
    assert _run(db, org, account, sync_type="CLAIMS", status="SUCCESS") is not None


def test_unknown_sync_type_is_still_rejected(db: Session, org, actor):
    """어휘를 넓혔다고 아무 값이나 받지 않는다."""
    account = _account(db, org, actor)
    sp = db.begin_nested()
    with pytest.raises(IntegrityError):
        _run(db, org, account, sync_type="WHATEVER", status="SUCCESS")
    sp.rollback()


def test_order_status_ignores_claim_runs(db: Session, org, actor):
    """**주문 화면이 클레임 실행의 숫자를 보여주지 않는다.**

    실측에서 나온 그대로 재현한다 — 주문은 0건 읽었고 클레임이 16건을
    읽었는데, 예전 코드는 주문 화면에 16 을 띄웠다.
    """
    account = _account(db, org, actor)
    _run(db, org, account, sync_type="ORDERS", status="SUCCESS", read=0)
    _run(db, org, account, sync_type="CLAIMS", status="SUCCESS",
         read=16, conflict=14)

    status = order_sync.status(db, org_id=org)
    accounts = {a["external_account_id"]: a for a in status["accounts"]}
    got = accounts.get(str(account))
    assert got is not None, "계정이 상태에 안 나온다"
    assert got["last_counts"]["fetched"] == 0, "클레임 조회 건수가 주문으로 샜다"


def test_claim_failure_is_not_reported_as_an_order_failure(
        db: Session, org, actor):
    """클레임이 실패했는데 "주문 동기화 실패" 라고 말하면 멀쩡한 쪽을 고치러 간다."""
    account = _account(db, org, actor)
    _run(db, org, account, sync_type="ORDERS", status="SUCCESS")
    _run(db, org, account, sync_type="CLAIMS", status="FAILED",
         error_code="AUTH_FAILED")

    kinds = {i["kind"]: i for i in exception_center.build(db, org_id=org)["items"]}
    assert "CLAIM_SYNC_AUTH_FAILED" in kinds
    assert "ORDER_SYNC_AUTH_FAILED" not in kinds
    assert "취소·반품" in kinds["CLAIM_SYNC_AUTH_FAILED"]["title"]


def test_order_failure_is_still_reported(db: Session, org, actor):
    account = _account(db, org, actor)
    _run(db, org, account, sync_type="ORDERS", status="FAILED",
         error_code="TIMEOUT")
    _run(db, org, account, sync_type="CLAIMS", status="SUCCESS")

    kinds = {i["kind"] for i in exception_center.build(db, org_id=org)["items"]}
    assert "ORDER_SYNC_TIMEOUT" in kinds
    assert "CLAIM_SYNC_TIMEOUT" not in kinds


def test_both_can_fail_independently(db: Session, org, actor):
    """§45 — 두 capability 는 따로 산다."""
    account = _account(db, org, actor)
    _run(db, org, account, sync_type="ORDERS", status="FAILED",
         error_code="SERVER_ERROR")
    _run(db, org, account, sync_type="CLAIMS", status="FAILED",
         error_code="RATE_LIMITED")

    kinds = {i["kind"] for i in exception_center.build(db, org_id=org)["items"]}
    assert "ORDER_SYNC_SERVER_ERROR" in kinds
    assert "CLAIM_SYNC_RATE_LIMITED" in kinds


def test_claim_capability_is_not_order_capability(db: Session):
    """클레임을 읽으려면 `CLAIM_IMPORT` 가 필요하다 — 주문 권한으로 못 읽는다."""
    from app.services.connector import SYNC_TYPE_CAPABILITY

    assert SYNC_TYPE_CAPABILITY["CLAIMS"] == "CLAIM_IMPORT"
    assert SYNC_TYPE_CAPABILITY["ORDERS"] == "ORDER_IMPORT"


def test_cross_org_sync_runs_are_not_mixed(db: Session, org, actor):
    """§60 — 남의 조직 실행이 우리 상태로 새지 않는다."""
    account = _account(db, org, actor)
    other = make_organization(db)
    other_actor = make_app_user(db, org_id=other, role="ADMIN")
    other_account = _account(db, other, other_actor)
    _run(db, other, other_account, sync_type="ORDERS", status="FAILED",
         error_code="AUTH_FAILED")
    _run(db, org, account, sync_type="ORDERS", status="SUCCESS")

    kinds = {i["kind"] for i in exception_center.build(db, org_id=org)["items"]}
    assert "ORDER_SYNC_AUTH_FAILED" not in kinds
