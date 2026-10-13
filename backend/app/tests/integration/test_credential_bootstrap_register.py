"""평문 파일 → SecretStore 부트스트랩의 **등록 경로** (M052).

`test_credential_bootstrap.py`(unit)가 파일을 어떻게 읽는지를 본다면, 이
파일은 읽은 것을 **어디에 넣는지**를 본다.

이 파일이 고정하는 것
------------------
1. 셀러툴은 `external_account` 로 간다 — 판매처 계정과 섞이지 않는다
2. 두 번 돌려도 계정이 하나다 — 중복 생성이 아니라 **교체**다
3. 평문은 DB 어디에도 없다 — 감사 기록에도 없다
4. 반환값에 값도 참조 문자열도 없다

2 가 특히 중요하다
---------------
부트스트랩은 "일단 다시 돌려 보는" 스크립트다. 돌릴 때마다 계정이 하나씩
늘면 어느 계정으로 동기화가 도는지 아무도 모르게 된다.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.core.secret_store import LocalEncryptedSecretStore, generate_master_key, set_secret_store
from app.tests.fixtures.factories import make_app_user, make_organization

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.credential_bootstrap import (  # noqa: E402
    ServiceCredential,
    register_external_system,
)

#: 가짜 키. 실제 키를 fixture 에 넣지 않는다.
FAKE_KEY = "test-not-a-real-key-0000"


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


@pytest.fixture
def store(db: Session, monkeypatch):
    """테스트 세션을 물린 저장소. 실제 구현은 자기 세션을 연다."""
    from app.config import get_settings

    monkeypatch.setattr(
        get_settings(), "CREDENTIAL_MASTER_KEY", generate_master_key(), raising=False)
    monkeypatch.setattr(get_settings(), "CREDENTIAL_RETIRED_KEYS", "", raising=False)
    factory = sessionmaker(
        bind=db.connection(), join_transaction_mode="create_savepoint")
    instance = LocalEncryptedSecretStore(session_factory=factory)
    set_secret_store(instance)
    yield instance
    set_secret_store(None)


@pytest.fixture
def cred() -> ServiceCredential:
    return ServiceCredential("NEXT_ENGINE", fields={"api_key": FAKE_KEY})


def _accounts(db: Session, org: uuid.UUID) -> list[uuid.UUID]:
    rows = db.execute(text("""
        SELECT a.id FROM external_account a
          JOIN external_system s ON s.id = a.external_system_id
         WHERE a.org_id = :o AND s.code = 'NEXT_ENGINE' AND a.deleted_at IS NULL
    """), {"o": str(org)}).scalars().all()
    return [uuid.UUID(str(r)) for r in rows]


def test_seller_tool_goes_to_external_account(db, org, actor, store, cred):
    out = register_external_system(db, org_id=org, actor_user_id=actor, cred=cred)

    assert out["registered"] is True
    assert out["system_code"] == "NEXT_ENGINE"
    assert out["credential_kind"] == "API_KEY"
    assert out["secretstore_exists"] is True
    assert len(_accounts(db, org)) == 1

    # 판매처 계정에는 아무것도 생기지 않는다.
    assert db.execute(text("""
        SELECT COUNT(*) FROM integration_account WHERE org_id = :o
    """), {"o": str(org)}).scalar() == 0


def test_running_twice_keeps_one_account(db, org, actor, store, cred):
    """부트스트랩은 다시 돌리는 스크립트다. 돌릴 때마다 계정이 늘면 안 된다."""
    first = register_external_system(db, org_id=org, actor_user_id=actor, cred=cred)
    second = register_external_system(db, org_id=org, actor_user_id=actor, cred=cred)

    assert first["account_reused"] is False
    assert second["account_reused"] is True
    assert first["external_account_id"] == second["external_account_id"]
    assert len(_accounts(db, org)) == 1

    # 살아 있는 비밀도 하나다 — 옛 것은 교체되며 물러난다.
    assert db.execute(text("""
        SELECT COUNT(*) FROM credential_secret
         WHERE org_id = :o AND owner_type = 'external_account' AND deleted_at IS NULL
    """), {"o": str(org)}).scalar() == 1


def test_incomplete_credential_is_not_registered(db, org, actor, store):
    """"등록됐다" 고 보고해 놓고 호출할 때 실패하는 상태를 만들지 않는다."""
    empty = ServiceCredential("NEXT_ENGINE")
    out = register_external_system(db, org_id=org, actor_user_id=actor, cred=empty)

    assert out["registered"] is False
    assert "api_key" in out["reason"]
    assert _accounts(db, org) == []


def test_plaintext_is_nowhere_in_the_database(db, org, actor, store, cred):
    register_external_system(db, org_id=org, actor_user_id=actor, cred=cred)

    dumped = db.execute(text("""
        SELECT string_agg(t::text, ' ') FROM credential_secret t WHERE org_id = :o
    """), {"o": str(org)}).scalar() or ""
    assert FAKE_KEY not in dumped
    # 항목 이름조차 남지 않는다 — payload 를 통째로 암호화한다.
    assert "api_key" not in dumped

    account_row = db.execute(text("""
        SELECT t::text FROM external_account t WHERE org_id = :o
    """), {"o": str(org)}).scalar() or ""
    assert FAKE_KEY not in account_row


def test_audit_records_the_fact_not_the_value(db, org, actor, store, cred):
    """무엇을 했는지는 남기고, 무엇이었는지는 남기지 않는다."""
    register_external_system(db, org_id=org, actor_user_id=actor, cred=cred)

    rows = db.execute(text("""
        SELECT reason, COALESCE(before_json::text, '') || COALESCE(after_json::text, '')
          FROM audit_log
         WHERE org_id = :o AND domain_table = 'external_account'
    """), {"o": str(org)}).all()

    assert any(r[0] == "CREDENTIAL_CONFIGURED" for r in rows)
    for _, payload in rows:
        assert FAKE_KEY not in payload


def test_return_value_has_neither_value_nor_reference(db, org, actor, store, cred):
    """이 dict 는 그대로 콘솔에 찍힌다."""
    out = register_external_system(db, org_id=org, actor_user_id=actor, cred=cred)
    dumped = json.dumps(out, ensure_ascii=False)

    assert FAKE_KEY not in dumped
    # `cs_…` 참조를 알면 무엇을 노려야 하는지 알게 된다.
    assert "cs_" not in dumped
    assert "credential_secret_ref" not in dumped


def test_round_trip_returns_what_was_stored(db, org, actor, store, cred):
    """등록이 "됐다" 고만 말하고 실제로는 못 읽는 상태를 잡는다."""
    from app.services.credential_resolver import resolve_for_account

    out = register_external_system(db, org_id=org, actor_user_id=actor, cred=cred)
    resolved = resolve_for_account(
        db, org_id=org, account_id=uuid.UUID(out["external_account_id"]))

    assert resolved.credential_kind == "API_KEY"
    assert resolved.as_dict() == {"api_key": FAKE_KEY}
    # 그 객체를 찍어도 값은 나오지 않는다.
    assert FAKE_KEY not in repr(resolved)
