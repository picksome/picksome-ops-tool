"""마스터 키 분리와 복구 (P2).

무엇을 확인하나
------------
```
DB      → 암호문만
환경변수 → 마스터 키
```

이 분리가 실제로 성립하는지, 그리고 **DB 만 복구하고 키를 잃으면 어떻게
되는지**를 확인한다.

왜 중요한가
---------
백업/복원은 이미 검증됐다. 하지만 DB 를 되살려도 마스터 키가 없으면 자격증명은
영영 못 푼다. 그 사실을 재해 상황에서 처음 알게 되면 늦다.

반대로 키가 백업에 함께 들어가 있으면 암호화가 무의미하다 — 백업 하나가 새면
전부 풀린다.

실제 운영 비밀을 쓰지 않는다
-----------------------
전부 테스트용 키·테스트용 값이고, 어떤 값도 출력하지 않는다.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.secret_store import (
    CredentialStoreNotConfiguredError,
    MasterKeySet,
    SecretDecryptionError,
    generate_master_key,
    load_master_keys,
)

SENTINEL = b"test-secret-DO-NOT-LOG-8f3a1c"


def _key() -> bytes:
    return generate_master_key().encode("ascii")


# ── 1. 암호화가 실제로 되는가 ──────────────────────────────────


def test_ciphertext_does_not_contain_plaintext():
    keys = MasterKeySet(_key())
    token = keys.encrypt(SENTINEL)
    assert SENTINEL not in token
    assert keys.decrypt(token) == SENTINEL


def test_fingerprint_is_not_the_key():
    """지문은 어느 키로 암호화했는지 찾는 용도지 키 자체가 아니다."""
    key = _key()
    keys = MasterKeySet(key)
    assert key.decode() not in keys.fingerprint
    assert len(keys.fingerprint) < len(key)


# ── 2. 키를 잃으면 ─────────────────────────────────────────────


def test_wrong_key_cannot_decrypt(caplog):
    """다른 키로는 **복호화되지 않는다.** 평문 fallback 이 없다.

    여기서 어떤 형태로든 값이 나오면 암호화가 장식이라는 뜻이다.
    """
    token = MasterKeySet(_key()).encrypt(SENTINEL)
    other = MasterKeySet(_key())
    with pytest.raises(SecretDecryptionError) as exc:
        other.decrypt(token)
    # 예외 메시지에도 값이 새지 않아야 한다.
    assert SENTINEL.decode() not in str(exc.value)
    assert SENTINEL.decode() not in caplog.text


def test_tampered_ciphertext_is_rejected():
    """변조되면 조용히 이상한 값을 내놓지 않고 거부한다 (Fernet 인증 암호)."""
    keys = MasterKeySet(_key())
    token = bytearray(keys.encrypt(SENTINEL))
    token[20] ^= 0x01
    with pytest.raises(SecretDecryptionError):
        keys.decrypt(bytes(token))


# ── 3. 회전 ────────────────────────────────────────────────────


def test_retired_key_still_decrypts_old_ciphertext():
    """키를 바꿔도 **옛 암호문은 읽힌다.**

    이게 없으면 키 회전이 곧 자격증명 손실이다.
    """
    old = _key()
    token = MasterKeySet(old).encrypt(SENTINEL)

    new = _key()
    rotated = MasterKeySet(new, retired=(old,))
    assert rotated.decrypt(token) == SENTINEL

    # 새로 암호화한 것은 **새 키**로 된다 — 옛 키만 가진 쪽은 못 읽는다.
    fresh = rotated.encrypt(SENTINEL)
    with pytest.raises(SecretDecryptionError):
        MasterKeySet(old).decrypt(fresh)


# ── 4. 키가 없으면 ─────────────────────────────────────────────


def test_missing_key_fails_closed(monkeypatch):
    """키가 없으면 **실패한다.** 평문 저장으로 떨어지지 않는다."""
    from app import config

    class _NoKey:
        CREDENTIAL_MASTER_KEY = None
        CREDENTIAL_RETIRED_KEYS = ""

    monkeypatch.setattr(config, "get_settings", lambda: _NoKey())
    with pytest.raises(CredentialStoreNotConfiguredError):
        load_master_keys()


def test_malformed_key_is_rejected(monkeypatch):
    """형식이 틀린 키를 조용히 무시하고 넘어가지 않는다."""
    from app import config

    class _BadKey:
        CREDENTIAL_MASTER_KEY = "not-a-fernet-key"
        CREDENTIAL_RETIRED_KEYS = ""

    monkeypatch.setattr(config, "get_settings", lambda: _BadKey())
    with pytest.raises(CredentialStoreNotConfiguredError):
        load_master_keys()


# ── 5. DB 에 키가 없는가 ───────────────────────────────────────


def test_master_key_is_not_stored_anywhere_in_the_database(db: Session):
    """어느 텍스트 컬럼에도 현재 마스터 키가 없다.

    `credential_secret` 만 보지 않는다 — 설정·감사·로그 테이블에 흘렸을 수 있다.
    """
    from app.config import get_settings

    key = str(getattr(get_settings(), "CREDENTIAL_MASTER_KEY", "") or "")
    if not key:
        pytest.skip("이 환경에 마스터 키가 설정돼 있지 않다")

    columns = db.execute(text("""
        SELECT table_name, column_name FROM information_schema.columns
         WHERE table_schema = 'public'
           AND data_type IN ('text', 'character varying')
    """)).all()
    for table, column in columns:
        hit = db.execute(text(
            f'SELECT 1 FROM "{table}" WHERE "{column}" = :k LIMIT 1'),
            {"k": key}).scalar()
        assert hit is None, f"{table}.{column} 에 마스터 키가 있다"


def test_stored_credentials_are_ciphertext_only(db: Session):
    """실제 저장된 자격증명 행이 전부 암호문인지.

    개발 DB 에 행이 없으면 건너뛴다 — 없는 것을 통과로 만들지 않는다.
    """
    rows = db.execute(text("""
        SELECT ciphertext FROM credential_secret WHERE deleted_at IS NULL
    """)).scalars().all()
    if not rows:
        pytest.skip("이 DB 에 저장된 자격증명이 없다")
    for blob in rows:
        raw = bytes(blob)
        assert raw, "암호문이 비어 있다"
        # Fernet 토큰은 base64url 이고 버전 바이트 0x80 으로 시작한다.
        import base64

        assert base64.urlsafe_b64decode(raw)[0] == 0x80
