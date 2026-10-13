"""자격증명 저장소 (M042, core/secret_store.py).

이 파일이 고정하는 것
------------------
1. 평문은 **DB 어디에도 없다** — 저장된 것은 암호문뿐이다
2. **키가 틀리면 못 읽는다** — DB 덤프만으로는 아무것도 안 나온다
3. **변조를 잡는다** — 암호문 한 바이트만 바뀌어도 복호화가 실패한다
4. 같은 값을 두 번 넣어도 암호문이 다르다 (IV 가 매번 새로 난다)
5. **마스터 키가 없으면 실패한다** — 평문 fallback 은 없다
6. 운영에 테스트 저장소를 등록할 수 없다
7. 교체하면 옛 비밀이 사라진다
8. 조직을 넘지 않는다

2~3 이 이 파일의 이유다
--------------------
"암호화했다" 는 주장은 이 두 가지가 실제로 성립할 때만 의미가 있다.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.core.secret_store import (
    CredentialStoreNotConfiguredError,
    InMemorySecretStore,
    LocalEncryptedSecretStore,
    SecretDecryptionError,
    SecretNotFoundError,
    generate_master_key,
    load_master_keys,
    set_secret_store,
    validate_payload,
)
from app.tests.fixtures.factories import make_organization

SECRET = "SELLER_OS_SUPER_SECRET_12345"


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def key(monkeypatch) -> str:
    """이 테스트 동안만 쓰는 마스터 키. 실제 키를 fixture 에 넣지 않는다."""
    from app.config import get_settings

    value = generate_master_key()
    settings = get_settings()
    monkeypatch.setattr(settings, "CREDENTIAL_MASTER_KEY", value, raising=False)
    monkeypatch.setattr(settings, "CREDENTIAL_RETIRED_KEYS", "", raising=False)
    return value


@pytest.fixture
def store(db: Session, key: str) -> LocalEncryptedSecretStore:
    """테스트 세션을 주입한다.

    실제 구현은 자기 세션을 연다(업무 트랜잭션과 수명이 다르다). 그러면 테스트
    롤백 밖에 쓰게 되므로 여기서만 테스트 연결을 물려 준다.
    """
    factory = sessionmaker(bind=db.connection(), join_transaction_mode="create_savepoint")
    return LocalEncryptedSecretStore(session_factory=factory)


def _account_id() -> uuid.UUID:
    return uuid.uuid4()


# ── 저장된 것은 암호문뿐 ──────────────────────────────────────────

def test_plaintext_is_nowhere_in_the_database(db, org, store):
    owner = _account_id()
    store.put(org_id=org, owner_type="external_account", owner_id=owner,
              credential_kind="ACCESS_SECRET",
              payload={"access_key": "AK", "secret_key": SECRET})

    dumped = db.execute(text("""
        SELECT string_agg(t::text, ' ') FROM credential_secret t WHERE org_id = :o
    """), {"o": str(org)}).scalar() or ""
    assert SECRET not in dumped
    # 항목 이름조차 평문으로 남지 않는다 — payload 통째로 암호화한다.
    assert "secret_key" not in dumped


def test_round_trip(db, org, store):
    owner = _account_id()
    ref = store.put(org_id=org, owner_type="external_account", owner_id=owner,
                    credential_kind="API_KEY", payload={"api_key": SECRET})
    assert store.get(org_id=org, secret_ref=ref) == {"api_key": SECRET}


def test_same_value_encrypts_differently_each_time(db, org, store):
    """암호문이 같으면 "이 둘은 같은 비밀" 이라는 사실이 DB 에서 드러난다."""
    a = store.put(org_id=org, owner_type="external_account", owner_id=_account_id(),
                  credential_kind="API_KEY", payload={"api_key": SECRET})
    b = store.put(org_id=org, owner_type="external_account", owner_id=_account_id(),
                  credential_kind="API_KEY", payload={"api_key": SECRET})

    rows = db.execute(text("""
        SELECT ciphertext FROM credential_secret
         WHERE org_id = :o AND secret_ref IN (:a, :b)
    """), {"o": str(org), "a": a, "b": b}).scalars().all()
    assert len(rows) == 2
    assert bytes(rows[0]) != bytes(rows[1])


# ── 키가 없으면 / 틀리면 ──────────────────────────────────────────

def test_wrong_key_cannot_decrypt(db, org, store, monkeypatch):
    """DB 덤프만 유출돼도 못 푼다 — 이 분리가 이 구현의 전부다."""
    from app.config import get_settings

    ref = store.put(org_id=org, owner_type="external_account", owner_id=_account_id(),
                    credential_kind="API_KEY", payload={"api_key": SECRET})

    monkeypatch.setattr(get_settings(), "CREDENTIAL_MASTER_KEY",
                        generate_master_key(), raising=False)
    with pytest.raises(SecretDecryptionError):
        store.get(org_id=org, secret_ref=ref)


def test_tampered_ciphertext_is_rejected(db, org, store):
    """인증된 암호화의 요점 — 변조를 복호화 **전에** 잡는다."""
    ref = store.put(org_id=org, owner_type="external_account", owner_id=_account_id(),
                    credential_kind="API_KEY", payload={"api_key": SECRET})
    raw = bytes(db.execute(text("""
        SELECT ciphertext FROM credential_secret WHERE secret_ref = :r
    """), {"r": ref}).scalar())

    # 마지막 바이트 하나만 바꾼다.
    broken = raw[:-1] + bytes([raw[-1] ^ 0x01])
    db.execute(text("""
        UPDATE credential_secret SET ciphertext = :c WHERE secret_ref = :r
    """), {"c": broken, "r": ref})

    with pytest.raises(SecretDecryptionError):
        store.get(org_id=org, secret_ref=ref)


def test_missing_master_key_fails_closed(monkeypatch):
    """평문으로 대신 저장하지 않는다.

    그런 fallback 은 한 번 켜지면 아무도 다시 끄지 않는다.
    """
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "CREDENTIAL_MASTER_KEY", None, raising=False)
    with pytest.raises(CredentialStoreNotConfiguredError) as err:
        load_master_keys()
    # 운영자가 무엇을 해야 하는지 알 수 있다.
    assert "CREDENTIAL_MASTER_KEY" in str(err.value)
    assert err.value.http_status == 503


def test_malformed_master_key_is_rejected(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "CREDENTIAL_MASTER_KEY",
                        "not-a-real-key", raising=False)
    with pytest.raises(CredentialStoreNotConfiguredError):
        load_master_keys()


def test_retired_key_still_decrypts_after_rotation(db, org, store, monkeypatch):
    """키를 바꾸는 순간 기존 암호문을 전부 다시 쓸 수는 없다.

    운영 중이고, 그 작업이 실패하면 자격증명을 잃는다. 옛 키를 읽기 전용으로
    남겨 두는 이유다.
    """
    from app.config import get_settings

    settings = get_settings()
    old_key = settings.CREDENTIAL_MASTER_KEY
    ref = store.put(org_id=org, owner_type="external_account", owner_id=_account_id(),
                    credential_kind="API_KEY", payload={"api_key": SECRET})

    monkeypatch.setattr(settings, "CREDENTIAL_MASTER_KEY",
                        generate_master_key(), raising=False)
    monkeypatch.setattr(settings, "CREDENTIAL_RETIRED_KEYS", old_key, raising=False)

    assert store.get(org_id=org, secret_ref=ref) == {"api_key": SECRET}


def test_key_fingerprint_is_not_the_key(db, org, store, key):
    """지문으로 키를 되찾을 수 없어야 한다."""
    ref = store.put(org_id=org, owner_type="external_account", owner_id=_account_id(),
                    credential_kind="API_KEY", payload={"api_key": SECRET})
    fp = db.execute(text("""
        SELECT key_fingerprint FROM credential_secret WHERE secret_ref = :r
    """), {"r": ref}).scalar()

    assert fp and len(fp) == 16
    assert fp not in key
    assert key not in fp


# ── 수명 ──────────────────────────────────────────────────────────

def test_put_twice_for_one_owner_replaces_the_previous(db, org, store):
    """한 계정에 살아 있는 비밀은 하나다.

    둘이 되면 어느 것으로 접속하는지 아무도 모른다.
    """
    owner = _account_id()
    first = store.put(org_id=org, owner_type="external_account", owner_id=owner,
                      credential_kind="API_KEY", payload={"api_key": "FIRST_TEST"})
    second = store.put(org_id=org, owner_type="external_account", owner_id=owner,
                       credential_kind="API_KEY", payload={"api_key": "SECOND_TEST"})

    assert store.exists(org_id=org, secret_ref=first) is False
    assert store.get(org_id=org, secret_ref=second) == {"api_key": "SECOND_TEST"}
    alive = db.execute(text("""
        SELECT COUNT(*) FROM credential_secret
         WHERE org_id = :o AND owner_id = :w AND deleted_at IS NULL
    """), {"o": str(org), "w": str(owner)}).scalar()
    assert alive == 1


def test_delete_is_idempotent(db, org, store):
    """재시도가 안전해야 한다."""
    ref = store.put(org_id=org, owner_type="external_account", owner_id=_account_id(),
                    credential_kind="API_KEY", payload={"api_key": SECRET})
    store.delete(org_id=org, secret_ref=ref)
    store.delete(org_id=org, secret_ref=ref)
    assert store.exists(org_id=org, secret_ref=ref) is False
    with pytest.raises(SecretNotFoundError):
        store.get(org_id=org, secret_ref=ref)


def test_exists_does_not_need_the_key(db, org, store, monkeypatch):
    """"설정됨이라는데 실제로 있나" 를 묻는 데 평문이 필요하지는 않다."""
    from app.config import get_settings

    ref = store.put(org_id=org, owner_type="external_account", owner_id=_account_id(),
                    credential_kind="API_KEY", payload={"api_key": SECRET})
    monkeypatch.setattr(get_settings(), "CREDENTIAL_MASTER_KEY", None, raising=False)

    assert store.exists(org_id=org, secret_ref=ref) is True


def test_org_isolation(db, org, store):
    ref = store.put(org_id=org, owner_type="external_account", owner_id=_account_id(),
                    credential_kind="API_KEY", payload={"api_key": SECRET})
    other = make_organization(db)

    assert store.exists(org_id=other, secret_ref=ref) is False
    with pytest.raises(SecretNotFoundError):
        store.get(org_id=other, secret_ref=ref)


def test_reference_does_not_contain_the_owner_id(db, org, store):
    """참조가 새도 그 자체로는 아무것도 알려 주지 않아야 한다."""
    owner = _account_id()
    ref = store.put(org_id=org, owner_type="external_account", owner_id=owner,
                    credential_kind="API_KEY", payload={"api_key": SECRET})
    assert str(owner) not in ref
    assert str(org) not in ref


# ── 검증 ──────────────────────────────────────────────────────────

def test_required_fields_are_checked_before_storing():
    """실행 시점에야 알게 되면 그때는 새벽 배치가 실패한 뒤다."""
    with pytest.raises(ValueError) as err:
        validate_payload("ACCESS_SECRET", {"access_key": "AK"})
    assert "secret_key" in str(err.value)


def test_validation_message_never_contains_the_value():
    """어떤 **칸**이 비었는지는 알려 주되 값은 말하지 않는다."""
    with pytest.raises(ValueError) as err:
        validate_payload("BASIC", {"username": "u", "password": "   "})
    assert SECRET not in str(err.value)
    assert "password" in str(err.value)


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        validate_payload("NEXT_ENGINE_SPECIAL", {"x": "y"})


def test_oversized_payload_is_rejected():
    """인증정보는 작다. 큰 값이 들어오면 다른 것을 넣고 있다."""
    with pytest.raises(ValueError):
        validate_payload("CUSTOM", {"blob": "x" * 20_000})


# ── 운영 차단 ─────────────────────────────────────────────────────

def test_test_store_cannot_be_registered_in_production(monkeypatch):
    """운영에서 메모리 저장소가 켜지면 재시작마다 자격증명이 사라지는데
    화면은 "설정됨" 이라고 계속 말한다.

    커넥터 registry 의 운영 차단과 같은 이유다.
    """
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "APP_ENV", "prod", raising=False)
    with pytest.raises(RuntimeError) as err:
        set_secret_store(InMemorySecretStore())
    assert "운영" in str(err.value)


def test_production_check_accepts_both_spellings(monkeypatch):
    """설정 표기가 `prod` 든 `production` 이든 막힌다."""
    from app.config import get_settings

    for value in ("prod", "production", "PRODUCTION"):
        monkeypatch.setattr(get_settings(), "APP_ENV", value, raising=False)
        with pytest.raises(RuntimeError):
            set_secret_store(InMemorySecretStore())


def test_resolved_credential_does_not_print_its_values():
    """`print(dto)` 한 번, 로깅 한 줄이면 CI 로그에 남는다."""
    from app.services.credential_resolver import ResolvedCredential

    dto = ResolvedCredential("API_KEY", {"api_key": SECRET})
    assert SECRET not in repr(dto)
    assert SECRET not in str(dto)
    assert SECRET not in f"{dto}"
    # 무엇이 들었는지(칸 이름)는 보인다 — 디버깅에 필요하고 값이 아니다.
    assert "api_key" in repr(dto)
    assert dto.as_dict() == {"api_key": SECRET}


def test_failed_put_leaves_the_previous_secret_intact(db, org, store):
    """교체 중간에 죽어도 기존 자격증명을 잃지 않는다.

    옛 행을 먼저 물리고 새 행을 넣지만 **한 트랜잭션**이라, INSERT 가 실패하면
    앞의 UPDATE 도 함께 롤백된다.
    """
    owner = _account_id()
    ref = store.put(org_id=org, owner_type="external_account", owner_id=owner,
                    credential_kind="API_KEY", payload={"api_key": "KEEP_TEST"})

    class Boom(Exception):
        pass

    healthy = store._session

    def failing_session():
        session = healthy()
        original = session.execute

        def execute(statement, *args, **kwargs):
            if "INSERT INTO credential_secret" in str(statement):
                raise Boom("저장소 장애")
            return original(statement, *args, **kwargs)

        session.execute = execute  # type: ignore[method-assign]
        return session

    store._session = failing_session  # type: ignore[method-assign]
    try:
        with pytest.raises(Boom):
            store.put(org_id=org, owner_type="external_account", owner_id=owner,
                      credential_kind="API_KEY", payload={"api_key": "NEW_TEST"})
    finally:
        store._session = healthy  # type: ignore[method-assign]

    assert store.get(org_id=org, secret_ref=ref) == {"api_key": "KEEP_TEST"}
