"""DB 무결성 오류 → 도메인 오류 변환.

**제약조건을 예외 메시지 문자열로 판별하지 않는다.**

기존 코드는 `if "ux_approval_open_target" in str(exc)` 였다. 이건 두 가지로 깨진다.
  1. PostgreSQL 로케일에 따라 메시지가 바뀐다 (이 환경은 한국어 메시지를 낸다)
  2. 다른 제약 위반이 우연히 같은 문자열을 포함하면 오분류된다

psycopg 는 `exc.diag.constraint_name` 으로 **구조화된 제약조건 이름**을 준다.
SQLAlchemy 를 거치면 `exc.orig.diag.constraint_name` 이다. 그걸 쓴다.

알려지지 않은 무결성 오류는 **삼키지 않는다.** 도메인 오류로 바꾸지 않고 그대로
올려보내 500 이 되게 한다 — 모르는 제약 위반을 409 로 뭉개면 진짜 버그가 숨는다.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.core.exceptions import DomainError


def constraint_name_of(exc: BaseException) -> str | None:
    """예외에서 제약조건 이름을 꺼낸다. 못 꺼내면 None.

    SQLAlchemy IntegrityError → `.orig` 가 psycopg 예외.
    psycopg 예외 → `.diag.constraint_name`.
    """
    candidate: Any = getattr(exc, "orig", exc)
    diag = getattr(candidate, "diag", None)
    name = getattr(diag, "constraint_name", None)
    if name:
        return str(name)
    # 중첩된 경우 한 단계 더 (드물게 wrapper 가 겹친다)
    inner = getattr(candidate, "orig", None)
    if inner is not None:
        diag = getattr(inner, "diag", None)
        return getattr(diag, "constraint_name", None)
    return None


def translate_integrity_error(
    exc: BaseException,
    mapping: dict[str, Callable[[], DomainError]],
) -> DomainError | None:
    """알려진 제약조건이면 도메인 오류를 만들어 돌려준다. 모르면 None.

    호출 측:
        domain = translate_integrity_error(exc, {...})
        if domain is not None:
            raise domain from exc
        raise            # 알 수 없는 무결성 오류는 그대로
    """
    name = constraint_name_of(exc)
    if not name:
        return None
    factory = mapping.get(name)
    return factory() if factory else None
