"""권한 매트릭스가 실제 라우터와 일치하는지 (Phase 62).

왜 이 파일이 있나
--------------
구성원 화면은 "이 역할이 무엇을 할 수 있는가" 를 표로 보여준다.
그 표가 코드와 어긋나면 **권한 화면이 거짓말을 한다** — "이 사람은 못 본다" 는
판단 자체가 무너지고, 그건 권한이 아예 없는 것보다 나쁘다.

예전 화면은 아예 존재하지 않는 역할 체계(OWNER/PRODUCT_MANAGER/...)를 보여줬다.
백엔드 역할은 ADMIN/OPERATOR/SOURCING/ACCOUNTING/READONLY/EXTERNAL_ACCOUNTANT 다.

무엇을 검사하나
------------
라우터 소스에서 `require_roles(...)` 를 읽어, 화면 표에 적힌 역할 집합이
실제로 그 라우터에 존재하는지 확인한다. 라우터가 바뀌면 여기가 먼저 깨진다.

**표가 라우터를 정의하지 않는다.** 라우터가 진실이고 표는 그것을 옮긴 것이다.
"""

from __future__ import annotations

import re
from importlib import import_module
from pathlib import Path

import pytest

from app.core.rbac import ROLES

_API_DIR = Path(__file__).resolve().parents[2] / "api" / "v1"


def _allowed_of(dep: object) -> frozenset[str] | None:
    """`require_roles(...)` 가 돌려준 의존성에서 허용 역할을 꺼낸다.

    닫힘(closure)에 들어 있는 `allowed` 를 읽는다 — 소스 문자열을 읽는 것보다
    정확하다. `require_roles(*cashflow.VIEW_ROLES)` 처럼 **상수를 펼쳐 쓰는 형태**도
    그대로 잡히기 때문이다.

    역할 목록을 상수로 두는 데는 이유가 있다: 현금흐름은 `/cashflow` 와 대시보드
    카드 두 곳에서 쓰이고, 두 곳이 다른 목록을 쓰면 한쪽이 뒷문이 된다.
    실제로 그런 일이 있었다.
    """
    cells = getattr(dep, "__closure__", None)
    if not cells:
        return None
    for cell in cells:
        v = cell.cell_contents
        if isinstance(v, tuple) and v and all(isinstance(x, str) for x in v):
            return frozenset(v)
    return None


def _role_sets(module: str) -> set[frozenset[str]]:
    """모듈이 실제로 쓰는 모든 `require_roles(...)` 조합.

    모듈을 import 해서 **살아 있는 의존성 객체**를 본다. 정규식으로 소스를 읽으면
    상수를 쓴 라우터가 "권한이 하나도 없다" 로 보이고, 그러면 이 테스트가
    조용히 아무것도 검사하지 않게 된다.
    """
    mod = import_module(f"app.api.v1.{module}")
    out: set[frozenset[str]] = set()
    for value in vars(mod).values():
        roles = _allowed_of(value)
        if roles:
            out.add(roles)
    # 라우터 함수의 기본값에 직접 박아 쓴 경우(`Depends(require_roles("ADMIN"))`)도 본다.
    for value in vars(mod).values():
        for default in getattr(value, "__defaults__", None) or ():
            roles = _allowed_of(getattr(default, "dependency", None))
            if roles:
                out.add(roles)
    return out


# 화면(`settings/members`)의 권한 매트릭스와 **같은 값**이어야 한다.
# 왼쪽이 화면 라벨, 오른쪽이 (모듈, 역할집합).
_MATRIX: list[tuple[str, str, frozenset[str]]] = [
    ("상품 조회", "products", frozenset({"ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY"})),
    ("상품 등록·수정", "products", frozenset({"ADMIN", "OPERATOR", "SOURCING"})),
    ("판매장부 조회", "sales", frozenset({"ADMIN", "OPERATOR", "ACCOUNTING", "READONLY"})),
    ("판매 등록·수정", "sales", frozenset({"ADMIN", "OPERATOR", "ACCOUNTING"})),
    ("사업비용 조회", "expenses", frozenset({"ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY"})),
    ("사업비용 등록·수정", "expenses", frozenset({"ADMIN", "OPERATOR", "ACCOUNTING"})),
    ("판매가 등록·수정", "channel_prices", frozenset({"ADMIN", "OPERATOR", "SOURCING"})),
    ("월 마감 확정", "monthly_close", frozenset({"ADMIN", "ACCOUNTING"})),
    ("월 잠금·재오픈", "monthly_close", frozenset({"ADMIN"})),
    ("수익배분 조회", "payouts", frozenset({"ADMIN", "ACCOUNTING", "OPERATOR"})),
    ("수익배분 확정·승인", "payouts", frozenset({"ADMIN", "ACCOUNTING"})),
    ("지급 승인", "payouts", frozenset({"ADMIN"})),
    ("현금흐름 조회·기록", "cashflow", frozenset({"ADMIN", "ACCOUNTING"})),
    ("정책 수정", "policies", frozenset({"ADMIN", "OPERATOR"})),
    ("세무 리포트 내보내기", "reports", frozenset({"ADMIN", "ACCOUNTING"})),
    ("운영 이슈 처리", "operations", frozenset({"ADMIN", "OPERATOR", "ACCOUNTING"})),
    ("시스템 상태·worker 실행", "operations", frozenset({"ADMIN"})),
    ("구성원 관리", "admin_users", frozenset({"ADMIN"})),
    ("감사 로그", "admin_audit", frozenset({"ADMIN"})),
]


@pytest.mark.parametrize("label,module,expected", _MATRIX, ids=[m[0] for m in _MATRIX])
def test_matrix_row_matches_router(label: str, module: str, expected: frozenset[str]) -> None:
    actual = _role_sets(module)
    assert expected in actual, (
        f"'{label}' 의 권한이 화면 표와 라우터에서 다릅니다.\n"
        f"  표:      {sorted(expected)}\n"
        f"  라우터:  {[sorted(s) for s in actual]}\n"
        f"라우터가 진실입니다. 라우터를 고쳤다면 화면 표"
        f"(frontend/app/(app)/settings/members/page.tsx 의 CAPABILITIES)도 함께 고치세요."
    )


def test_no_router_uses_an_unknown_role() -> None:
    """오타로 만든 역할은 아무도 통과시키지 못한다 — 조용히 잠기는 기능이 된다."""
    for path in sorted(_API_DIR.glob("*.py")):
        if path.stem == "__init__":
            continue
        for combo in _role_sets(path.stem):
            unknown = set(combo) - ROLES
            assert not unknown, f"{path.name}: 알 수 없는 역할 {unknown}"


def test_payout_ledger_is_hidden_from_operator_and_readonly() -> None:
    """지급 금액은 민감 정보다.

    누가 얼마를 가져가는지는 운영 업무에 필요하지 않다. 이 경계가 무너지면
    조직 안에서 배분액이 공개된다.
    """
    src = (_API_DIR / "payouts.py").read_text(encoding="utf-8")
    m = re.search(r'_pay_read = require_roles\(([^)]*)\)', src)
    assert m, "_pay_read 정의를 찾지 못했습니다"
    roles = set(re.findall(r'"([A-Z_]+)"', m.group(1)))
    assert roles == {"ADMIN", "ACCOUNTING"}, f"지급 조회 권한이 넓어졌습니다: {sorted(roles)}"


def test_admin_only_routes_stay_admin_only() -> None:
    """되돌리기 어려운 조작은 관리자만."""
    for module in ("admin_users", "admin_audit"):
        assert _role_sets(module) == {frozenset({"ADMIN"})}, (
            f"{module} 에 ADMIN 외 역할이 생겼습니다"
        )
