"""RBAC (역할 기반 접근제어).

docs/22_api_spec.md §2-2 권한 매트릭스 구현.
각 라우터는 `Depends(require_roles("ADMIN", "OPERATOR"))` 형태로 사용.

CLAUDE.md RBAC: MVP 화면이 없어도 **role 기반 접근제어 가능한 구조** 유지.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import Depends

if TYPE_CHECKING:
    # 런타임 import 는 순환이 된다(deps → core.exceptions). 타입 검사용으로만 가져온다 —
    # 문자열 주석만 두면 이름이 어디에도 정의되지 않아 린터가 잡지 못한다.
    from app.deps import CurrentUser

from app.core.exceptions import ForbiddenError

# 6개 표준 role. EXTERNAL_ACCOUNTANT 는 D-282 Post-MVP feature flag 대상.
ROLES = {
    "ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY", "EXTERNAL_ACCOUNTANT",
}


def require_roles(*allowed: str) -> Callable[..., CurrentUser]:
    """FastAPI dependency: `allowed` 중 하나의 role 을 가진 사용자만 통과.

    의사코드:
        @router.post("/x", dependencies=[Depends(require_roles("ADMIN"))])

    잘못된 role 명을 인자로 주면 import 시점에 즉시 ValueError 발생 (오타 방지).
    """
    # 순환 import 회피 — deps 가 core.exceptions 를 import 하므로 late import.
    from app.deps import get_current_user  # late import to avoid cycle

    invalid = set(allowed) - ROLES
    if invalid:
        raise ValueError(f"unknown roles: {invalid}")

    def _dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in allowed:
            # 에러 메시지에 user.role 노출 — 디버깅 편의. 보안상 민감하지 않음 (본인 role).
            raise ForbiddenError(f"권한 없음: {user.role} → {allowed}")
        return user

    return _dep
