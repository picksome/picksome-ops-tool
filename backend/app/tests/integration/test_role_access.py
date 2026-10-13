"""역할별 접근 통제를 **실제 HTTP 로** 확인한다 (Phase 10).

왜 이 파일이 따로 있는가
--------------------
`test_permission_matrix.py` 는 라우터 소스에서 `require_roles(...)` 문자열을
읽어 화면 표와 대조한다. 그건 **표가 코드와 어긋나지 않는지**를 볼 뿐,
요청이 실제로 막히는지는 보지 않는다.

권한은 화면 버튼이 아니라 라우터가 지킨다. 서비스 계층 테스트로는 `require_roles`
가 아예 실행되지 않으므로, 의존성을 라우터에 붙이는 것을 빠뜨려도 전부 통과한다.
그래서 여기서는 **토큰을 들고 요청을 보낸다.**

무엇을 고정하나
------------
```
403   역할이 없는 사용자가 직접 API 를 부를 때
401   토큰이 없을 때 (버튼을 숨기는 것으로는 못 막는다)
200   허용된 역할
```

특히 **금액이 드러나는 경로**를 본다: 지급 장부, 현금흐름, 수익배분.
누가 얼마를 가져가는지는 운영 업무에 필요하지 않다.

READONLY 가 200 을 받는 것도 검사한다
--------------------------------
막는 것만 검사하면 권한을 과하게 조인 실수를 못 잡는다. 읽기 전용 사용자가
읽지 못하면 그 계정은 쓸모가 없고, 사용자는 결국 전원에게 ADMIN 을 준다.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.deps import get_db
from app.main import app
from app.tests.fixtures.factories import make_app_user, make_organization

ALL_ROLES = (
    "ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY", "EXTERNAL_ACCOUNTANT",
)


@pytest.fixture
def client(db: Session):
    """앱의 DB 의존성을 테스트 세션으로 바꿔 끼운다.

    테스트가 만든 사용자를 라우터가 못 보면 인증 자체가 실패한다 —
    같은 트랜잭션을 써야 한다.
    """
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def tokens(db: Session, org: uuid.UUID) -> dict[str, str]:
    """역할별 사용자 + 토큰. 6개 역할 전부 만든다."""
    out: dict[str, str] = {}
    for role in ALL_ROLES:
        uid = make_app_user(db, org_id=org, role=role)
        out[role] = create_access_token(user_id=uid, org_id=org, role=role)
    db.commit()
    return out


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# 각 항목: (설명, method, path, 허용 역할)
#
# **금액이 드러나는 경로를 중심으로 고른다.** 전 경로를 나열하면 유지가 안 되고,
# 유지 안 되는 목록은 곧 거짓이 된다.
READ_CASES: list[tuple[str, str, str, set[str]]] = [
    (
        "현금흐름 — 통장 잔액과 파트너 지급액이 그대로 드러난다",
        "GET", "/api/v1/cashflow?preset=30d",
        {"ADMIN", "ACCOUNTING"},
    ),
    (
        "기준 잔액",
        "GET", "/api/v1/cashflow/baseline",
        {"ADMIN", "ACCOUNTING"},
    ),
    (
        "지급 장부 — 누가 얼마를 가져가는지",
        "GET", "/api/v1/payouts",
        {"ADMIN", "ACCOUNTING"},
    ),
    (
        "판매장부 — 운영 업무에 필요하다",
        "GET", "/api/v1/sales",
        {"ADMIN", "OPERATOR", "ACCOUNTING", "READONLY"},
    ),
    (
        "사업비용",
        "GET", "/api/v1/expenses",
        {"ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY"},
    ),
    (
        # 조회는 READONLY 도 된다. 처리(무시·해결)만 _work 로 좁혀져 있다 —
        # "뭐가 잘못돼 있나" 는 보고서를 읽는 사람도 알아야 한다.
        "운영 이슈 조회",
        "GET", "/api/v1/operations/issues",
        {"ADMIN", "OPERATOR", "ACCOUNTING", "READONLY"},
    ),
]


@pytest.mark.parametrize(
    "label,method,path,allowed",
    READ_CASES,
    ids=[c[2] for c in READ_CASES],
)
def test_read_access_matches_the_declared_roles(
    client, tokens, label: str, method: str, path: str, allowed: set[str]
):
    """허용된 역할은 통과하고 나머지는 403 이다.

    양쪽을 다 본다 — 막는 것만 검사하면 권한을 과하게 조인 실수를 못 잡는다.
    """
    for role in ALL_ROLES:
        res = client.request(method, path, headers=auth(tokens[role]))
        if role in allowed:
            assert res.status_code == 200, (
                f"{label}: {role} 이 막혔습니다 ({res.status_code}) — {path}"
            )
        else:
            assert res.status_code == 403, (
                f"{label}: {role} 이 통과했습니다 ({res.status_code}) — {path}"
            )


def test_no_token_is_rejected_before_any_role_check(client):
    """화면에서 버튼을 숨기는 것으로는 못 막는다. 토큰 없이 부르면 401 이다."""
    for _, method, path, _ in READ_CASES:
        res = client.request(method, path)
        assert res.status_code == 401, f"{path} 가 인증 없이 열려 있습니다."


# ──────────────────────────────────────────────────────────────────────────
# 쓰기 — 돈을 바꾸는 경로
# ──────────────────────────────────────────────────────────────────────────

def test_only_finance_roles_can_record_cash(client, tokens):
    """수동 현금 기록은 예상 잔액을 바꾼다. 운영 담당자가 넣을 수 있으면 안 된다."""
    body = {
        "direction": "INFLOW", "status": "ACTUAL", "amount": 1_000_000,
        "occurred_at": date(2026, 5, 10).isoformat(),
    }
    for role in ("OPERATOR", "SOURCING", "READONLY", "EXTERNAL_ACCOUNTANT"):
        res = client.post("/api/v1/cashflow/manual-events", json=body, headers=auth(tokens[role]))
        assert res.status_code == 403, f"{role} 이 현금 기록을 만들 수 있습니다."

    res = client.post("/api/v1/cashflow/manual-events", json=body, headers=auth(tokens["ACCOUNTING"]))
    assert res.status_code == 201


def test_only_finance_roles_can_set_the_baseline(client, tokens):
    """기준 잔액은 화면 전체의 잔액 계산 기준이다."""
    body = {"as_of_date": "2026-05-01", "amount": 10_000_000}
    res = client.put("/api/v1/cashflow/baseline", json=body, headers=auth(tokens["OPERATOR"]))
    assert res.status_code == 403

    res = client.put("/api/v1/cashflow/baseline", json=body, headers=auth(tokens["ADMIN"]))
    assert res.status_code == 200


def test_month_lock_is_admin_only(client, tokens):
    """잠금은 되돌릴 수 없는 전이다. 실수로 눌릴 수 있는 자리에 두지 않는다."""
    for role in ("OPERATOR", "SOURCING", "ACCOUNTING", "READONLY"):
        res = client.post("/api/v1/monthly-close/2026-05/lock", headers=auth(tokens[role]))
        assert res.status_code == 403, f"{role} 이 월을 잠글 수 있습니다."


def test_readonly_cannot_write_anywhere_it_can_read(client, tokens):
    """읽기 전용이 이름값을 해야 한다 — 읽을 수 있는 곳에서 쓸 수 있으면 안 된다."""
    ro = auth(tokens["READONLY"])

    assert client.post("/api/v1/sales", json={
        "sale_date": "2026-05-10", "marketplace": "COUPANG",
        "marketplace_order_no": "RO-1", "quantity": 1, "sale_price": 1000,
    }, headers=ro).status_code == 403

    assert client.post("/api/v1/expenses", json={
        "expense_date": "2026-05-10", "category_id": str(uuid.uuid4()),
        "description": "x", "gross_amount": 1000, "payment_method": "BUSINESS_CARD",
    }, headers=ro).status_code == 403


# ──────────────────────────────────────────────────────────────────────────
# 조직 격리 — 역할이 맞아도 남의 조직은 못 본다
# ──────────────────────────────────────────────────────────────────────────

def test_a_valid_role_in_another_org_sees_nothing(client, db: Session, org: uuid.UUID):
    """역할 검사만으로는 부족하다. ADMIN 이라도 자기 조직 것만 봐야 한다."""
    other = make_organization(db)
    other_admin = make_app_user(db, org_id=other, role="ADMIN")
    token = create_access_token(user_id=other_admin, org_id=other, role="ADMIN")

    db.execute(text("""
        INSERT INTO manual_cash_event
               (org_id, direction, status, amount, occurred_at, category)
        VALUES (:o, 'INFLOW', 'ACTUAL', 99000000, :d, '남의 조직 입금')
    """), {"o": str(org), "d": date(2026, 5, 10)})
    db.commit()

    res = client.get(
        "/api/v1/cashflow?date_from=2026-05-01&date_to=2026-05-31",
        headers=auth(token),
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["inflow"]["actual"] == 0
    assert data["rows"] == []


def test_error_bodies_do_not_leak_internals(client, tokens):
    """거절 메시지에 SQL·스택트레이스·비밀이 들어가면 안 된다."""
    res = client.get("/api/v1/cashflow", headers=auth(tokens["READONLY"]))
    assert res.status_code == 403

    body = res.text
    for leak in ("Traceback", "SELECT ", "psycopg", "password", "postgresql://"):
        assert leak not in body, f"오류 응답에 {leak} 가 노출됩니다."
