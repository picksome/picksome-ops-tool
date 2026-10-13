"""실행 시점 자격증명 해석 (M042).

왜 커넥터에 SecretStore 를 넘기지 않나
----------------------------------
```
Sync Service  →  Credential Resolver  →  SecretStore  →  Credential DTO  →  Connector
```

커넥터에 DB 세션이나 저장소 구현을 넘기면 vendor 코드가 우리 저장 구조를 알게
된다. 그러면 셀러툴을 하나 붙일 때마다 "이 커넥터가 비밀을 어디까지 볼 수
있나" 를 매번 검토해야 한다. 커넥터는 **자기가 쓸 값이 담긴 dict 하나**만 받고,
그게 어디서 왔는지 모른다.

평문의 수명
---------
복호화된 값은 이 함수가 돌려주는 객체 안에만 있다. 다음 어디에도 다시 쓰지
않는다.

```
DB          ✗
캐시         ✗   (다음 호출에 또 복호화한다. 느린 것보다 새는 게 나쁘다)
sync_run    ✗
audit_log   ✗
raw 응답     ✗
예외 메시지   ✗
```

`__repr__` 를 막아 둔 이유도 같다. 로깅 한 줄, `print(dto)` 한 번이면 CI 로그에
남는다.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import DomainError


class CredentialNotConfiguredError(DomainError):
    """이 계정에 인증정보가 없다.

    **422 다.** 우리가 못 하는 것(501 CONNECTOR_UNAVAILABLE)과 다르다 —
    사용자가 설정하면 해결된다.
    """

    code = "CREDENTIAL_NOT_CONFIGURED"
    http_status = 422


class ResolvedCredential:
    """커넥터에 넘길 평문 자격증명.

    dataclass 로 만들지 않는다 — 자동 생성된 `__repr__` 이 값을 그대로 찍는다.
    """

    __slots__ = ("credential_kind", "_values")

    def __init__(self, credential_kind: str, values: dict[str, str]) -> None:
        self.credential_kind = credential_kind
        self._values = values

    def as_dict(self) -> dict[str, str]:
        """커넥터에 넘기는 순간에만 부른다. 사본을 준다."""
        return dict(self._values)

    def __repr__(self) -> str:  # pragma: no cover - 값이 없다는 것이 요점
        keys = ",".join(sorted(self._values))
        return f"<ResolvedCredential {self.credential_kind} fields=[{keys}]>"

    __str__ = __repr__


def resolve_for_account(
    db: Session, *, org_id: UUID, account_id: UUID,
) -> ResolvedCredential:
    """외부 계정의 인증정보를 복호화한다.

    실패를 구분한다
    ------------
    ```
    참조 없음        CREDENTIAL_NOT_CONFIGURED   사용자가 설정하면 된다
    참조는 있는데 없음 SECRET_NOT_FOUND            운영 사고 — 조용히 넘기지 않는다
    복호화 실패       SECRET_DECRYPTION_FAILED    키가 다르거나 변조됐다
    ```

    두 번째를 "미설정" 으로 뭉개면 사용자는 다시 설정하고, 원인(키 교체 실수나
    DB 복원 사고)은 그대로 남는다.
    """
    from app.core.secret_store import get_secret_store

    row = db.execute(text("""
        SELECT credential_secret_ref, credential_kind
          FROM external_account
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(account_id), "org": str(org_id)}).mappings().first()
    if row is None:
        from app.core.exceptions import NotFoundError

        raise NotFoundError("외부 계정을 찾을 수 없습니다.")

    ref = row["credential_secret_ref"]
    if not ref:
        raise CredentialNotConfiguredError(
            "이 계정에 자격증명이 설정되지 않았습니다.",
        )

    values = get_secret_store().get(org_id=org_id, secret_ref=ref)
    return ResolvedCredential(row["credential_kind"] or "CUSTOM", values)


def secret_reference_is_intact(
    db: Session, *, org_id: UUID, account_id: UUID,
) -> bool | None:
    """참조가 가리키는 비밀이 실제로 있는가. **복호화하지 않는다.**

    `None` = 참조 자체가 없다(미설정). `False` = 계정은 설정됐다고 말하는데
    저장소에 없다 — 데이터 품질 점검이 이걸 잡는다.
    """
    from app.core.secret_store import get_secret_store

    ref = db.execute(text("""
        SELECT credential_secret_ref FROM external_account
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(account_id), "org": str(org_id)}).scalar()
    if not ref:
        return None
    return get_secret_store().exists(org_id=org_id, secret_ref=ref)


def redact_for_log(payload: dict[str, Any]) -> dict[str, str]:
    """디버깅용으로 남길 수 있는 것 — **칸 이름과 길이뿐**이다.

    값을 마스킹해서 남기는 방식(`abc***xyz`)을 쓰지 않는다. 앞뒤 몇 글자만으로도
    짧은 키는 좁혀지고, 무엇보다 "마스킹했으니 안전하다" 는 습관이 생긴다.
    """
    return {k: f"<{len(str(v))}자>" for k, v in payload.items()}
