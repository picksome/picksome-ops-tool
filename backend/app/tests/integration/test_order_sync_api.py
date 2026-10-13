"""주문 동기화 HTTP 계층.

이 파일이 고정하는 것
------------------
1. 상태 화면이 "마지막 시도" 와 "마지막 성공" 을 **따로** 준다
2. 주문이 자동으로 돌아도 클레임은 연결됐다고 말하지 않는다
3. 수동 실행은 자동과 같은 잠금을 쓴다 — 겹치면 409
4. 설정이 없으면 성공한 척하지 않는다
5. 권한 · 조직 격리
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.deps import get_db
from app.main import app
from app.services import automation as auto
from app.services import next_engine_order_sync as nes
from app.services import order_sync as osync
from app.tests.fixtures.factories import (
    make_app_user,
    make_integration_account,
    make_organization,
)

CUTOVER = "2026-07-26"


@pytest.fixture
def client(db: Session):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def admin(db: Session, org) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


def _auth(user_id, org_id, role="ADMIN") -> dict:
    token = create_access_token(user_id=user_id, org_id=org_id, role=role)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def ne_account(db: Session, org, admin, secret_store) -> uuid.UUID:
    sys_id = db.execute(text("""
        INSERT INTO external_system (code, name, system_type, supports_api_key)
        VALUES ('NEXT_ENGINE', 'NEXT ENGINE', 'SELLER_TOOL', TRUE)
        ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id
    """)).scalar()
    account_id = uuid.UUID(str(db.execute(text("""
        INSERT INTO external_account (org_id, external_system_id, account_code,
               display_name, credential_kind, created_by, updated_by)
        VALUES (:o, :s, 'DEFAULT', 'NE 본사', 'API_KEY', :a, :a) RETURNING id
    """), {"o": str(org), "s": str(sys_id), "a": str(admin)}).scalar()))
    ref = secret_store.put(
        org_id=org, owner_type="EXTERNAL_ACCOUNT", owner_id=account_id,
        credential_kind="API_KEY", payload={"api_key": "k"})
    db.execute(text("""
        UPDATE external_account SET credential_secret_ref = :r WHERE id = :i
    """), {"r": ref, "i": str(account_id)})
    db.commit()
    return account_id


@pytest.fixture
def configured(db: Session, org, admin, ne_account):
    market = make_integration_account(db, org_id=org, provider="COUPANG")
    db.execute(text("""
        INSERT INTO external_entity_mapping
               (org_id, external_account_id, external_entity_type, external_entity_id,
                internal_entity_type, internal_entity_id, mapping_status, first_seen_at)
        VALUES (:o, :a, :et, 'mall02400', :et, :iid, 'ACTIVE', now())
    """), {"o": str(org), "a": str(ne_account),
           "et": nes.ENTITY_MARKETPLACE_ACCOUNT, "iid": str(market)})
    db.commit()
    return auto.upsert_policy(
        db, org_id=org, actor_user_id=admin, automation_type="ORDER_SYNC",
        enabled=True, mode="AUTO_EXECUTE", interval_minutes=30,
        guardrails={"overlap_days": 3},
        scope={"accounts": [{"external_account_id": str(ne_account),
                             "initial_sync_from": CUTOVER}]})


# ── 1·2) 상태 ───────────────────────────────────────────────────────────

def test_status_separates_last_attempt_from_last_success(
    client, db, org, admin, ne_account, configured,
):
    """하나로 합치면 몇 시간째 못 읽고 있는 상태가 성공 시각 뒤에 숨는다."""
    res = client.get("/api/v1/external/order-sync/status",
                     headers=_auth(admin, org))
    assert res.status_code == 200
    body = res.json()["data"]

    assert body["enabled"] is True
    assert body["interval_minutes"] == 30
    account = body["accounts"][0]
    assert account["configured"] is True
    assert account["initial_sync_from"] == CUTOVER
    assert account["overlap_days"] == 3
    assert "last_attempt_at" in account
    assert "last_success_at" in account


def test_status_does_not_claim_that_claims_are_connected(
    client, db, org, admin, ne_account, configured,
):
    """주문 동기화만 켰다. 취소·반품은 아직 반영되지 않는다.

    한 덩어리로 보이면 사용자는 정산이 맞는다고 믿는다.
    """
    body = client.get("/api/v1/external/order-sync/status",
                      headers=_auth(admin, org)).json()["data"]
    assert body["claim_ingest_implemented"] is False


def test_a_claim_policy_that_is_on_but_empty_still_counts_as_not_reflected(
    client, db, org, admin, ne_account, configured,
):
    """켜 두기만 하고 대상 계정을 안 넣으면 한 건도 안 들어온다.

    `enabled` 만 보고 경고를 지우면, 아무것도 수집되지 않는데 화면은
    "반영되고 있다" 고 말한다.
    """
    from app.services import automation as auto

    auto.upsert_policy(
        db, org_id=org, actor_user_id=admin, automation_type="CLAIM_SYNC",
        enabled=True, mode="AUTO_EXECUTE")

    body = client.get("/api/v1/external/order-sync/status",
                      headers=_auth(admin, org)).json()["data"]
    assert body["claim_ingest_implemented"] is False


def test_status_stops_warning_once_claim_collection_is_actually_configured(
    client, db, org, admin, ne_account, configured,
):
    """수집이 실제로 돌 수 있게 됐는데 계속 "반영 안 됨" 이라고 하면 거짓이다."""
    from app.services import automation as auto

    auto.upsert_policy(
        db, org_id=org, actor_user_id=admin, automation_type="CLAIM_SYNC",
        enabled=True, mode="AUTO_EXECUTE",
        guardrails={"claim_lookback_days": 90},
        scope={"accounts": [{"external_account_id": str(ne_account),
                             "initial_sync_from": "2026-05-01"}]})

    body = client.get("/api/v1/external/order-sync/status",
                      headers=_auth(admin, org)).json()["data"]
    assert body["claim_ingest_implemented"] is True


def test_an_unconfigured_account_is_marked_not_configured(
    client, db, org, admin, ne_account,
):
    """설정 안 된 것과 "0건 정상" 은 다르다."""
    body = client.get("/api/v1/external/order-sync/status",
                      headers=_auth(admin, org)).json()["data"]
    assert body["enabled"] is False
    assert body["accounts"][0]["configured"] is False


def test_status_carries_no_personal_data(
    client, db, org, admin, ne_account, configured,
):
    blob = client.get("/api/v1/external/order-sync/status",
                      headers=_auth(admin, org)).text
    for leaked in ("api_key", "Api-Auth-Key", "credential_secret_ref"):
        assert leaked not in blob


# ── 3·4) 수동 실행 ──────────────────────────────────────────────────────

def test_manual_run_without_configuration_is_rejected(
    client, db, org, admin, ne_account,
):
    """가짜 성공을 만들지 않는다."""
    res = client.post("/api/v1/external/order-sync/run",
                      headers=_auth(admin, org))
    assert res.status_code == 422


def test_manual_run_while_a_sync_holds_the_lock_does_not_pretend_success(
    client, db, org, admin, ne_account, configured,
):
    """자동 실행 중에 사람이 눌러도 두 번 돌지 않는다."""
    with osync.single_flight(db, ne_account):
        res = client.post("/api/v1/external/order-sync/run",
                          headers=_auth(admin, org))

    assert res.status_code == 200
    outcome = res.json()["data"][0]
    assert outcome["status"] == osync.SKIPPED
    assert outcome["reason"] == osync.ALREADY_RUNNING
    # 실행 기록을 만들지 않았다 = 벤더를 부르지 않았다.
    assert db.execute(text("""
        SELECT count(*) FROM external_sync_run WHERE org_id = :o
    """), {"o": str(org)}).scalar() == 0


def test_manual_run_targeting_an_unconfigured_account_is_rejected(
    client, db, org, admin, ne_account, configured,
):
    other = uuid.uuid4()
    res = client.post(f"/api/v1/external/order-sync/run?account_id={other}",
                      headers=_auth(admin, org))
    assert res.status_code == 422


# ── 5) 권한 · 조직 ──────────────────────────────────────────────────────

def test_read_only_role_cannot_trigger_a_sync(
    client, db, org, ne_account, configured,
):
    viewer = make_app_user(db, org_id=org, role="READONLY")
    res = client.post("/api/v1/external/order-sync/run",
                      headers=_auth(viewer, org, "READONLY"))
    assert res.status_code == 403


def test_another_org_does_not_see_this_account(
    client, db, org, admin, ne_account, configured,
):
    other_org = make_organization(db)
    other_admin = make_app_user(db, org_id=other_org, role="ADMIN")

    body = client.get("/api/v1/external/order-sync/status",
                      headers=_auth(other_admin, other_org)).json()["data"]

    ids = {a["external_account_id"] for a in body["accounts"]}
    assert str(ne_account) not in ids
    assert body["enabled"] is False
