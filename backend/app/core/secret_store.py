"""복원 가능한 비밀 저장소 (M042).

왜 이게 새로 필요한가 — 기존 vault 로는 안 된다
--------------------------------------------
`password_vault_entry` 는 **사람이 읽는 쪽지**다.

```
service_name     "쿠팡 Wing"
vault_provider   "Bitwarden"
vault_item_name  "쿠팡 Wing 항목"
account_masked   "pic****me"
```

비밀이 한 글자도 없고, 들어가지 않도록 CHECK 제약까지 걸려 있다(M001). 사람이
"어디 보면 되는지" 를 찾는 용도로는 옳다. 그런데 **무인 배치는 쪽지를 읽을 수
없다.** 새벽 3시에 도는 동기화가 "Bitwarden 의 그 항목" 을 열지 못한다.

그래서 두 개를 나눈다. 합치지 않는 이유는 목적이 다르기 때문이다.

```
password_vault_entry   사람이 찾아가는 위치      복호화 불가 (애초에 값이 없다)
credential_secret      기계가 쓰는 인증정보      복호화 가능 (암호문으로 보관)
```

핵심 규약
--------
1. **호출자는 저장 위치를 모른다.** `put()` 이 돌려주는 `secret_ref` 는
   불투명 문자열이다. 파싱하면 안 되고, API 응답에 실으면 안 된다.
2. **평문은 프로세스 메모리에만 있다.** DB·로그·감사·응답 어디에도 없다.
3. **마스터 키가 없으면 실패한다.** 평문으로 대신 저장하는 fallback 은 없다.
   그 fallback 이 한 번 켜지면 아무도 다시 끄지 않는다.
4. 구현은 갈아끼운다. Core 는 이 인터페이스만 안다 —
   `core/attachment_storage.py` 와 같은 구조다.

암호 알고리즘을 직접 만들지 않는다
------------------------------
`cryptography` 의 **Fernet** 을 쓴다. 이미 의존성에 있다
(`python-jose[cryptography]`). 고른 이유:

```
AES-128-CBC + HMAC-SHA256   인증된 암호화 — 변조를 복호화 전에 잡는다
IV 를 라이브러리가 생성      nonce 재사용 실수를 할 여지가 없다
토큰에 버전·IV·HMAC 포함     nonce 컬럼을 따로 둘 필요가 없다
MultiFernet                 키 교체 기간에 옛 키로도 읽을 수 있다
```

AES-GCM 도 후보였지만 nonce 를 우리가 만들어야 한다. nonce 재사용은 GCM 에서
치명적이고, 그 실수를 할 수 있는 코드를 굳이 쓸 이유가 없다. Fernet 은
`cryptography` 가 "직접 고르고 싶지 않은 사람을 위해" 내놓은 recipes 계층이다.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.exceptions import DomainError

#: 지원하는 자격증명 형태. vendor 마다 컬럼을 만들지 않기 위한 것이다 —
#: `next_engine_password`, `coupang_secret_key` 같은 컬럼이 생기기 시작하면
#: 셀러툴을 하나 붙일 때마다 스키마가 늘고 Core 가 vendor 를 알게 된다.
CREDENTIAL_KINDS = (
    "API_KEY",
    "ACCESS_SECRET",
    "CLIENT_CREDENTIALS",
    "OAUTH_REFRESH",
    "BASIC",
    "CUSTOM",
)

#: 형태별로 **반드시 있어야 하는** 항목. 없는 채로 저장하면 실행 시점에야
#: 알게 되고, 그때는 새벽 배치가 실패한 뒤다.
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "API_KEY": ("api_key",),
    "ACCESS_SECRET": ("access_key", "secret_key"),
    "CLIENT_CREDENTIALS": ("client_id", "client_secret"),
    "OAUTH_REFRESH": ("client_id", "client_secret", "refresh_token"),
    "BASIC": ("username", "password"),
    "CUSTOM": (),
}

#: payload 한 건의 상한. 인증정보는 작다 — 큰 값이 들어오면 다른 것을 넣고 있다.
MAX_PAYLOAD_BYTES = 8 * 1024


class CredentialStoreNotConfiguredError(DomainError):
    """마스터 키가 없어 비밀을 다룰 수 없다.

    **500 이 아니라 503 이다.** 코드 버그가 아니라 배포 설정이 덜 된 상태고,
    운영자가 할 일이 명확하다 — 키를 넣고 다시 띄운다.
    """

    code = "CREDENTIAL_STORE_NOT_CONFIGURED"
    http_status = 503


class SecretNotFoundError(DomainError):
    """참조는 있는데 실제 비밀이 없다.

    계정이 "설정됨" 이라고 말하는데 저장소에 없는 상태 — 운영 사고다.
    조용히 미설정으로 취급하지 않는다.
    """

    code = "SECRET_NOT_FOUND"
    http_status = 409


class SecretDecryptionError(DomainError):
    """복호화 실패 — 키가 다르거나 암호문이 변조됐다.

    둘을 구분해서 알려 주지 않는다. 구분해 주면 공격자가 키 추측 진행도를
    측정할 수 있다.
    """

    code = "SECRET_DECRYPTION_FAILED"
    http_status = 500


def validate_payload(credential_kind: str, payload: dict[str, Any]) -> dict[str, str]:
    """저장 전 최소 검증. **값을 예외 메시지에 넣지 않는다.**"""
    if credential_kind not in CREDENTIAL_KINDS:
        raise ValueError(f"알 수 없는 자격증명 종류입니다: {credential_kind}")
    if not isinstance(payload, dict) or not payload:
        raise ValueError("자격증명 내용이 비어 있습니다.")

    cleaned: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key:
            raise ValueError("자격증명 항목 이름이 올바르지 않습니다.")
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        cleaned[key] = text

    missing = [f for f in REQUIRED_FIELDS[credential_kind] if f not in cleaned]
    if missing:
        # 어떤 **칸**이 비었는지는 알려 준다. 값은 말하지 않는다.
        raise ValueError(f"필수 항목이 비어 있습니다: {', '.join(missing)}")

    encoded = json.dumps(cleaned, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError("자격증명 내용이 너무 큽니다.")
    return cleaned


class SecretStore(ABC):
    """자격증명 저장소.

    소유자는 `(org_id, owner_type, owner_id)` 로 지정한다. 지금 owner 는
    `external_account` 하나지만, 판매처 계정·발송 대행처럼 같은 문제가 반복될
    자리가 이미 보인다.

    `put()` 이 돌려주는 `secret_ref` 는 **불투명하다.** 호출자는 그대로 보관만
    하고 해석하지 않는다.
    """

    #: 데이터 품질 점검이 화면에 표시할 이름.
    provider_name: str = "UNKNOWN"

    @abstractmethod
    def put(
        self, *, org_id: UUID, owner_type: str, owner_id: UUID,
        credential_kind: str, payload: dict[str, Any],
    ) -> str:
        """저장하고 참조를 돌려준다. 같은 소유자면 교체(rotate)다."""

    @abstractmethod
    def get(self, *, org_id: UUID, secret_ref: str) -> dict[str, str]:
        """복호화. **호출한 쪽이 메모리에서만 쓴다.**"""

    @abstractmethod
    def delete(self, *, org_id: UUID, secret_ref: str) -> None:
        """지운다. 이미 없어도 성공이다 — 재시도가 안전해야 한다."""

    @abstractmethod
    def exists(self, *, org_id: UUID, secret_ref: str) -> bool:
        """복호화하지 않고 존재만 확인한다.

        "설정됨이라는데 실제로 있나" 를 묻는 데 평문이 필요하지는 않다.
        """


# ──────────────────────────────────────────────────────────────────────────
# 마스터 키
# ──────────────────────────────────────────────────────────────────────────

def _fingerprint(key: bytes) -> str:
    """어느 키로 암호화했는지 표시하는 지문.

    키 자체가 아니라 SHA-256 앞 16자다. 32바이트 난수 키의 해시 앞부분으로
    키를 되찾을 수는 없다. 이게 있어야 "옛 키로 암호화된 행이 아직 남았나" 를
    복호화 없이 셀 수 있다.
    """
    return hashlib.sha256(key).hexdigest()[:16]


class MasterKeySet:
    """현재 키 하나 + 은퇴한 키 여러 개.

    은퇴한 키를 **읽기 전용**으로 남긴다. 키를 바꾸는 순간 기존 암호문을 전부
    다시 암호화할 수는 없다(운영 중이고, 실패하면 자격증명을 잃는다). 읽을 수
    있는 상태로 두고 접근할 때마다 새 키로 갈아 쓴다.
    """

    def __init__(self, primary: bytes, retired: tuple[bytes, ...] = ()) -> None:
        self.primary = primary
        self.retired = retired
        # MultiFernet 은 앞에서부터 복호화를 시도하고, 암호화는 항상 첫 키로 한다.
        self._multi = MultiFernet([Fernet(k) for k in (primary, *retired)])

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.primary)

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._multi.encrypt(plaintext)

    def decrypt(self, token: bytes) -> bytes:
        try:
            return self._multi.decrypt(token)
        except InvalidToken as exc:
            # 키가 틀린 것과 변조된 것을 구분해 주지 않는다.
            raise SecretDecryptionError(
                "자격증명을 복호화할 수 없습니다. 마스터 키를 확인하세요.",
            ) from exc


def generate_master_key() -> str:
    """새 마스터 키. 운영자가 env 에 넣을 값이다.

    ```
    python -c "from app.core.secret_store import generate_master_key; print(generate_master_key())"
    ```
    """
    return Fernet.generate_key().decode("ascii")


def _parse_key(raw: str, *, label: str) -> bytes:
    key = raw.strip().encode("ascii")
    try:
        Fernet(key)
    except Exception as exc:  # noqa: BLE001 - 라이브러리가 여러 예외를 낸다
        raise CredentialStoreNotConfiguredError(
            f"{label} 형식이 올바르지 않습니다. "
            "generate_master_key() 로 만든 값이어야 합니다.",
        ) from exc
    return key


def load_master_keys() -> MasterKeySet:
    """설정에서 키를 읽는다. 없으면 **실패한다** — 평문 대체는 없다."""
    from app.config import get_settings

    settings = get_settings()
    primary_raw = getattr(settings, "CREDENTIAL_MASTER_KEY", None)
    if not primary_raw:
        raise CredentialStoreNotConfiguredError(
            "자격증명 마스터 키가 설정되지 않았습니다 "
            "(CREDENTIAL_MASTER_KEY). 비밀을 평문으로 저장하지 않으므로 "
            "키 없이는 자격증명을 다룰 수 없습니다.",
        )

    retired_raw = getattr(settings, "CREDENTIAL_RETIRED_KEYS", "") or ""
    retired = tuple(
        _parse_key(part, label="CREDENTIAL_RETIRED_KEYS")
        for part in retired_raw.split(",") if part.strip()
    )
    return MasterKeySet(_parse_key(primary_raw, label="CREDENTIAL_MASTER_KEY"), retired)


# ──────────────────────────────────────────────────────────────────────────
# 구현 1 — 로컬 암호화 (개발 · 소규모 운영)
# ──────────────────────────────────────────────────────────────────────────

class LocalEncryptedSecretStore(SecretStore):
    """암호문을 우리 DB(`credential_secret`)에 두고 키는 환경에 둔다.

    DB 덤프만으로는 못 푼다
    --------------------
    ```
    DB       암호문
    환경변수  마스터 키
    ```

    이 분리가 이 구현의 전부다. 백업 번들에 DB 와 키를 같이 넣으면 분리가
    무의미해진다 — 운영 문서에 적어 두었다(docs/60).

    한계를 분명히 해 둔다
    ------------------
    앱 프로세스가 털리면 키도 털린다. 그건 이 구현으로 막을 수 없고, 막으려면
    KMS/HSM 처럼 복호화를 밖에서 하는 구조가 필요하다. 그때는 이 클래스를
    그대로 두고 `SecretStore` 구현을 하나 더 만든다 — 호출부는 바뀌지 않는다.
    """

    provider_name = "LOCAL_ENCRYPTED"

    def __init__(self, session_factory: Any = None) -> None:
        # 세션을 인자로 받지 않는 이유: 호출자가 트랜잭션을 신경 쓰지 않아도
        # 되게 하려고. 비밀 저장은 업무 트랜잭션과 수명이 다르다 — 롤백돼도
        # 남아 있는 편이 안전하다(고아 비밀은 지울 수 있지만, 사라진 비밀은
        # 복구할 수 없다).
        self._session_factory = session_factory

    def _session(self):  # noqa: ANN202
        if self._session_factory is not None:
            return self._session_factory()
        from app.db.session import SessionLocal

        return SessionLocal()

    @staticmethod
    def _new_ref() -> str:
        """불투명 참조. 소유자 id 를 담지 않는다 — 참조가 새도 그 자체로는
        아무것도 알려 주지 않아야 한다."""
        return "cs_" + secrets.token_urlsafe(24)

    def put(
        self, *, org_id: UUID, owner_type: str, owner_id: UUID,
        credential_kind: str, payload: dict[str, Any],
    ) -> str:
        cleaned = validate_payload(credential_kind, payload)
        keys = load_master_keys()
        ciphertext = keys.encrypt(
            json.dumps(cleaned, ensure_ascii=False).encode("utf-8")
        )
        ref = self._new_ref()

        from sqlalchemy import text

        db = self._session()
        try:
            # 옛 행을 먼저 물리고 나서 새 행을 넣는다. **한 트랜잭션 안이다.**
            #
            # 처음에는 "새 것을 먼저 넣어야 잃지 않는다" 고 생각해 순서를
            # 반대로 뒀는데, `uq_credential_secret_owner`(한 소유자에 살아 있는
            # 비밀은 하나) 가 그 INSERT 를 막았다. 제약이 옳다 — 둘이 살아 있으면
            # 어느 것으로 접속하는지 아무도 모른다.
            #
            # 순서를 바꿔도 안전한 이유는 트랜잭션이다. INSERT 가 실패하면
            # 앞의 UPDATE 도 함께 롤백돼 기존 비밀이 그대로 남는다.
            db.execute(text("""
                UPDATE credential_secret SET deleted_at = now()
                 WHERE org_id = :org AND owner_type = :ot AND owner_id = :oid
                   AND deleted_at IS NULL
            """), {"org": str(org_id), "ot": owner_type, "oid": str(owner_id)})
            db.execute(text("""
                INSERT INTO credential_secret
                       (org_id, secret_ref, owner_type, owner_id, provider,
                        credential_kind, key_fingerprint, ciphertext)
                VALUES (:org, :ref, :ot, :oid, :prov, :kind, :fp, :ct)
            """), {
                "org": str(org_id), "ref": ref, "ot": owner_type,
                "oid": str(owner_id), "prov": self.provider_name,
                "kind": credential_kind, "fp": keys.fingerprint,
                "ct": ciphertext,
            })
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return ref

    def get(self, *, org_id: UUID, secret_ref: str) -> dict[str, str]:
        from sqlalchemy import text

        keys = load_master_keys()
        db = self._session()
        try:
            row = db.execute(text("""
                SELECT ciphertext FROM credential_secret
                 WHERE org_id = :org AND secret_ref = :ref AND deleted_at IS NULL
            """), {"org": str(org_id), "ref": secret_ref}).first()
        finally:
            db.close()

        if row is None:
            raise SecretNotFoundError(
                "자격증명이 저장소에 없습니다. 다시 설정해야 합니다.",
            )
        loaded = json.loads(keys.decrypt(bytes(row[0])).decode("utf-8"))
        return {str(k): str(v) for k, v in loaded.items()}

    def delete(self, *, org_id: UUID, secret_ref: str) -> None:
        from sqlalchemy import text

        db = self._session()
        try:
            # soft delete 다. 언제 지워졌는지가 사고 조사에 필요하고,
            # 암호문은 키 없이는 쓸모가 없다.
            db.execute(text("""
                UPDATE credential_secret SET deleted_at = now()
                 WHERE org_id = :org AND secret_ref = :ref AND deleted_at IS NULL
            """), {"org": str(org_id), "ref": secret_ref})
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def exists(self, *, org_id: UUID, secret_ref: str) -> bool:
        from sqlalchemy import text

        db = self._session()
        try:
            return db.execute(text("""
                SELECT 1 FROM credential_secret
                 WHERE org_id = :org AND secret_ref = :ref AND deleted_at IS NULL
            """), {"org": str(org_id), "ref": secret_ref}).first() is not None
        finally:
            db.close()


# ──────────────────────────────────────────────────────────────────────────
# 구현 2 — 메모리 (테스트 전용)
# ──────────────────────────────────────────────────────────────────────────

class InMemorySecretStore(SecretStore):
    """테스트 전용. **운영에 등록할 수 없다.**

    운영에서 이게 켜지면 프로세스를 재시작하는 순간 모든 자격증명이 사라지고,
    화면은 "설정됨" 이라고 계속 말한다. 커넥터 registry 의 운영 차단과 같은
    이유다(`services/connector.register`).
    """

    provider_name = "IN_MEMORY_TEST"

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], dict[str, str]] = {}
        self._owners: dict[tuple[str, str, str], str] = {}

    def put(
        self, *, org_id: UUID, owner_type: str, owner_id: UUID,
        credential_kind: str, payload: dict[str, Any],
    ) -> str:
        cleaned = validate_payload(credential_kind, payload)
        owner = (str(org_id), owner_type, str(owner_id))
        previous = self._owners.get(owner)
        ref = "mem_" + secrets.token_urlsafe(12)
        self._data[(str(org_id), ref)] = cleaned
        self._owners[owner] = ref
        if previous:
            self._data.pop((str(org_id), previous), None)
        return ref

    def get(self, *, org_id: UUID, secret_ref: str) -> dict[str, str]:
        found = self._data.get((str(org_id), secret_ref))
        if found is None:
            raise SecretNotFoundError("자격증명이 저장소에 없습니다.")
        return dict(found)

    def delete(self, *, org_id: UUID, secret_ref: str) -> None:
        self._data.pop((str(org_id), secret_ref), None)
        for owner, ref in list(self._owners.items()):
            if ref == secret_ref:
                self._owners.pop(owner, None)

    def exists(self, *, org_id: UUID, secret_ref: str) -> bool:
        return (str(org_id), secret_ref) in self._data


# ──────────────────────────────────────────────────────────────────────────
# 선택
# ──────────────────────────────────────────────────────────────────────────

_store: SecretStore | None = None


def _is_production() -> bool:
    from app.config import get_settings

    # `services/connector._is_production` 과 같은 규칙 — 표기가 달라도 막힌다.
    return str(getattr(get_settings(), "APP_ENV", "")).lower() in ("prod", "production")


def get_secret_store() -> SecretStore:
    """프로세스 단일 인스턴스.

    지금은 구현이 하나뿐이라 분기가 없다. 관리형 저장소를 붙일 때
    `CREDENTIAL_STORE_BACKEND` 를 보고 여기서 갈라진다 — 호출부는 그대로다.
    """
    global _store
    if _store is None:
        from app.config import get_settings

        backend = str(getattr(get_settings(), "CREDENTIAL_STORE_BACKEND", "local"))
        if backend.lower() not in ("local", "local_encrypted"):
            raise CredentialStoreNotConfiguredError(
                f"알 수 없는 자격증명 저장소 설정입니다: {backend}",
            )
        _store = LocalEncryptedSecretStore()
    return _store


def set_secret_store(store: SecretStore | None) -> None:
    """테스트가 갈아끼울 때 쓴다.

    운영에서 테스트 구현을 넣으려 하면 막는다. 실수로 들어가면 아무도
    모른 채 자격증명이 휘발된다.
    """
    global _store
    if isinstance(store, InMemorySecretStore) and _is_production():
        raise RuntimeError(
            "테스트 전용 자격증명 저장소를 운영에 등록할 수 없습니다.",
        )
    _store = store


def store_is_configured() -> bool:
    """마스터 키가 있는가. 화면이 "왜 설정 버튼이 안 눌리는지" 를 말할 수 있게."""
    try:
        load_master_keys()
    except CredentialStoreNotConfiguredError:
        return False
    return True


__all__ = [
    "CREDENTIAL_KINDS",
    "REQUIRED_FIELDS",
    "CredentialStoreNotConfiguredError",
    "InMemorySecretStore",
    "LocalEncryptedSecretStore",
    "MasterKeySet",
    "SecretDecryptionError",
    "SecretNotFoundError",
    "SecretStore",
    "generate_master_key",
    "get_secret_store",
    "load_master_keys",
    "set_secret_store",
    "store_is_configured",
    "validate_payload",
]
