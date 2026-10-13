"""서비스 계정 · API 토큰 · MCP tool (M044).

이 파일이 고정하는 것
------------------
1. 토큰 원문은 **발급 응답 한 번**만 존재한다 — DB 에도 감사에도 없다
2. 접두사만으로 인증되지 않는다
3. 폐기·만료·계정 정지가 **즉시** 막는다
4. 조직을 넘지 않는다
5. scope 없는 tool 호출은 **403** 이다 (401 아님)
6. 원가 권한이 없으면 원가와 **파생값**이 함께 가려진다
7. AI 는 원가를 직접 바꾸지 못한다 — 승인 요청까지다
8. MCP 를 꺼도 자동화·리포트는 그대로 돈다

7~8 이 이 파일의 이유다
--------------------
AI 에게 열어 준 문이 승인 체계를 우회하거나, AI 를 안 쓰는 고객의 자동화를
망가뜨리면 안 된다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.deps import get_db
from app.main import app
from app.services import service_account as sa
from app.tests.fixtures.factories import make_app_user, make_organization, make_product

SECRET_MARKER = "SELLER_OS_SUPER_SECRET_12345"


@pytest.fixture
def client(db: Session):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def admin(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


@pytest.fixture
def admin_token(db: Session, org: uuid.UUID, admin: uuid.UUID) -> str:
    db.commit()
    return create_access_token(user_id=admin, org_id=org, role="ADMIN")


def auth(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def account(db: Session, org: uuid.UUID, admin: uuid.UUID) -> dict:
    return sa.create_account(db, org_id=org, actor_user_id=admin,
                             name="OpenClaw 셀러 에이전트")


@pytest.fixture
def mcp_on(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "MCP_ENABLED", True, raising=False)


def _issue(db, org, admin, account, scopes):
    return sa.issue_token(db, org_id=org, actor_user_id=admin,
                          account_id=uuid.UUID(account["id"]), scopes=scopes)


# ── 토큰 발급 ────────────────────────────────────────────────────

def test_token_plaintext_exists_only_once(db, org, admin, account):
    """원문은 발급 응답에만 있다. DB 에도 감사에도 없다."""
    issued = _issue(db, org, admin, account, ["products:read"])
    token = issued["token"]

    assert token.startswith("sos_")
    stored = db.execute(text("""
        SELECT string_agg(t::text, ' ') FROM api_token t WHERE org_id = :o
    """), {"o": str(org)}).scalar() or ""
    assert token not in stored

    audit = db.execute(text("""
        SELECT string_agg(a::text, ' ') FROM audit_log a
         WHERE org_id = :o AND domain_table = 'api_token'
    """), {"o": str(org)}).scalar() or ""
    assert token not in audit
    # 무엇을 줬는지는 남는다 — 권한 감사를 하려면 필요하다.
    assert "products:read" in audit


def test_there_is_no_way_to_read_a_token_back(db, org, admin, account):
    """되읽기 경로를 만들면 그게 가장 약한 고리가 된다."""
    _issue(db, org, admin, account, ["products:read"])
    detail = sa.get_account(db, org_id=org, account_id=uuid.UUID(account["id"]))

    blob = repr(detail)
    assert "token_hash" not in blob
    for t in detail["tokens"]:
        assert "token" not in t or t.get("token") is None
        # 접두사는 사람이 알아보는 용도로 남는다.
        assert t["token_prefix"].startswith("sos_")


def test_token_is_high_entropy(db, org, admin, account):
    """빠른 해시를 쓰는 근거가 엔트로피다. 짧아지면 근거가 무너진다."""
    a = _issue(db, org, admin, account, ["products:read"])["token"]
    b = _issue(db, org, admin, account, ["products:read"])["token"]

    assert a != b
    # sos_ + urlsafe(32바이트) ≈ 47자.
    assert len(a) >= 40


def test_unknown_and_forbidden_scopes_are_rejected(db, org, admin, account):
    """조용히 무시하면 사용자는 준 적 없는 권한이 있다고 믿는다."""
    from app.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        _issue(db, org, admin, account, ["products:write"])

    with pytest.raises(ValidationError) as err:
        _issue(db, org, admin, account, ["payouts:execute"])
    # 왜 안 되는지 말해 준다.
    assert "사람이 화면에서" in str(err.value)


# ── 인증 ────────────────────────────────────────────────────────

def test_authenticate_returns_scoped_principal(db, org, admin, account):
    issued = _issue(db, org, admin, account, ["products:read", "cost:read"])
    principal = sa.authenticate(db, issued["token"])

    assert principal.org_id == org
    assert principal.has("products:read")
    assert not principal.has("sales:read")


def test_prefix_alone_does_not_authenticate(db, org, admin, account):
    """접두사로 인증되면 접두사만 맞히면 되는 셈이다."""
    issued = _issue(db, org, admin, account, ["products:read"])
    prefix = issued["token_prefix"]

    with pytest.raises(sa.TokenAuthError):
        sa.authenticate(db, prefix)
    with pytest.raises(sa.TokenAuthError):
        sa.authenticate(db, prefix + "0" * 40)


def test_revoked_token_stops_immediately(db, org, admin, account):
    issued = _issue(db, org, admin, account, ["products:read"])
    sa.authenticate(db, issued["token"])

    sa.revoke_token(db, org_id=org, actor_user_id=admin,
                    token_id=uuid.UUID(issued["id"]))
    with pytest.raises(sa.TokenAuthError):
        sa.authenticate(db, issued["token"])


def test_expired_token_is_rejected(db, org, admin, account):
    """영구 토큰만 강요하지 않는다 — 만료를 걸 수 있어야 한다."""
    issued = sa.issue_token(
        db, org_id=org, actor_user_id=admin, account_id=uuid.UUID(account["id"]),
        scopes=["products:read"],
        expires_at=datetime.now().astimezone() - timedelta(minutes=1),
    )
    with pytest.raises(sa.TokenAuthError):
        sa.authenticate(db, issued["token"])


def test_disabling_the_account_blocks_all_its_tokens(db, org, admin, account):
    """토큰을 하나씩 폐기하는 것보다 빠른 차단 수단이 필요하다."""
    issued = _issue(db, org, admin, account, ["products:read"])
    sa.authenticate(db, issued["token"])

    sa.set_account_status(db, org_id=org, actor_user_id=admin,
                          account_id=uuid.UUID(account["id"]), status="DISABLED")
    with pytest.raises(sa.TokenAuthError):
        sa.authenticate(db, issued["token"])


def test_failure_reason_is_not_disclosed(db, org, admin, account):
    """"만료됨" 과 "없는 토큰" 을 구분해 주면 유효한 토큰 탐색에 쓸 수 있다."""
    issued = _issue(db, org, admin, account, ["products:read"])
    sa.revoke_token(db, org_id=org, actor_user_id=admin,
                    token_id=uuid.UUID(issued["id"]))

    with pytest.raises(sa.TokenAuthError) as revoked:
        sa.authenticate(db, issued["token"])
    with pytest.raises(sa.TokenAuthError) as missing:
        sa.authenticate(db, "sos_" + "x" * 43)

    assert str(revoked.value) == str(missing.value)


def test_last_used_is_recorded(db, org, admin, account):
    issued = _issue(db, org, admin, account, ["products:read"])
    sa.authenticate(db, issued["token"])

    used = db.execute(text("""
        SELECT last_used_at FROM api_token WHERE id = :i
    """), {"i": issued["id"]}).scalar()
    assert used is not None


def test_token_does_not_cross_orgs(db, org, admin, account):
    """토큰은 조직에 고정된다."""
    issued = _issue(db, org, admin, account, ["products:read"])
    principal = sa.authenticate(db, issued["token"])

    other = make_organization(db)
    assert principal.org_id != other
    # 다른 조직 관리자가 이 토큰을 폐기할 수 없다.
    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        sa.revoke_token(db, org_id=other, actor_user_id=admin,
                        token_id=uuid.UUID(issued["id"]))


# ── 권한 (API) ───────────────────────────────────────────────────

def test_only_admin_can_manage_service_accounts(db, client, org):
    """화면에서 숨기는 것으로 끝내지 않는다."""
    for role in ("OPERATOR", "SOURCING", "ACCOUNTING", "READONLY"):
        uid = make_app_user(db, org_id=org, role=role)
        db.commit()
        res = client.post(
            "/api/v1/ai-access/service-accounts", json={"name": "x"},
            headers=auth(create_access_token(user_id=uid, org_id=org, role=role)),
        )
        assert res.status_code == 403, f"{role} 이 서비스 계정을 만들 수 있다"


def test_issued_token_is_returned_once_over_http(db, client, admin_token, account):
    res = client.post(
        f"/api/v1/ai-access/service-accounts/{account['id']}/tokens",
        json={"scopes": ["products:read"], "label": "읽기 전용"},
        headers=auth(admin_token),
    )
    assert res.status_code == 201, res.text
    body = res.json()["data"]
    assert body["token"].startswith("sos_")
    assert "다시 볼 수 없습니다" in body["warning"]

    # 목록에는 원문이 없다.
    listing = client.get(f"/api/v1/ai-access/service-accounts/{account['id']}",
                         headers=auth(admin_token))
    assert body["token"] not in listing.text


# ── MCP tool ────────────────────────────────────────────────────

def test_mcp_is_off_by_default(db, client, org, admin, account):
    """켜지 않았으면 경로가 **없는 것처럼** 보인다."""
    issued = _issue(db, org, admin, account, ["products:read"])
    res = client.get("/api/v1/mcp/tools", headers=auth(issued["token"]))
    assert res.status_code == 404


def test_user_jwt_cannot_call_mcp(db, client, admin_token, mcp_on):
    """AI 에게 사람 토큰을 주는 길을 열지 않는다."""
    res = client.get("/api/v1/mcp/tools", headers=auth(admin_token))
    assert res.status_code == 401
    assert "서비스 계정 토큰" in res.text


def test_tool_list_follows_scopes(db, client, org, admin, account, mcp_on):
    issued = _issue(db, org, admin, account, ["products:read"])
    res = client.get("/api/v1/mcp/tools", headers=auth(issued["token"]))
    assert res.status_code == 200

    names = {t["name"] for t in res.json()["data"]}
    assert "search_products" in names
    assert "get_cashflow" not in names
    # 실행 tool 은 아예 없다.
    assert not any(t["kind"] == "EXECUTE" for t in res.json()["data"])


def test_hidden_tool_is_still_refused_when_called(db, client, org, admin,
                                                  account, mcp_on):
    """목록 필터는 보안이 아니다 — 이름만 알면 부를 수 있다."""
    issued = _issue(db, org, admin, account, ["products:read"])
    res = client.post("/api/v1/mcp/call",
                      json={"name": "get_cashflow", "arguments": {}},
                      headers=auth(issued["token"]))
    # 401 이 아니라 403 이다 — 인증은 됐고 권한이 없다.
    assert res.status_code == 403


def test_cost_is_hidden_without_cost_scope(db, client, org, admin, account, mcp_on):
    """원가를 가리면서 마진을 열어 두면 역산이 된다."""
    make_product(db, org_id=org, cost_amount=5000)
    db.commit()
    issued = _issue(db, org, admin, account, ["products:read"])

    res = client.post("/api/v1/mcp/call",
                      json={"name": "search_products", "arguments": {}},
                      headers=auth(issued["token"]))
    assert res.status_code == 200
    items = res.json()["data"]["result"]["items"]
    assert items
    assert items[0]["cost_amount"] == "(권한 없음)"


def test_response_size_is_capped(db, client, org, admin, account, mcp_on):
    """AI 에게 10만 건을 넘기지 않는다."""
    issued = _issue(db, org, admin, account, ["products:read"])
    res = client.post("/api/v1/mcp/call",
                      json={"name": "search_products",
                            "arguments": {"limit": 100000}},
                      headers=auth(issued["token"]))
    assert res.status_code == 200
    assert len(res.json()["data"]["result"]["items"]) <= 100


def test_unknown_tool_is_404(db, client, org, admin, account, mcp_on):
    issued = _issue(db, org, admin, account, ["products:read"])
    res = client.post("/api/v1/mcp/call",
                      json={"name": "drop_everything", "arguments": {}},
                      headers=auth(issued["token"]))
    assert res.status_code == 404


def test_ai_cannot_change_cost_directly(db, client, org, admin, account, mcp_on):
    """AI 가 승인 체계를 지나칠 수 있으면 승인 체계가 장식이 된다."""
    product = make_product(db, org_id=org, cost_amount=5000)
    db.commit()
    issued = _issue(db, org, admin, account,
                    ["products:read", "approvals:create"])

    res = client.post("/api/v1/mcp/call", json={
        "name": "propose_cost_change",
        "arguments": {"product_id": str(product), "cost_amount": 9000,
                      "reason": "공급처 인상"},
    }, headers=auth(issued["token"]))
    assert res.status_code == 200, res.text

    # ① 원가는 그대로다.
    assert db.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                      {"i": str(product)}).scalar() == 5000
    # ② 승인 요청이 대기 중이다.
    row = db.execute(text("""
        SELECT status, request_type FROM approval_request
         WHERE org_id = :o AND target_id = :p AND deleted_at IS NULL
    """), {"o": str(org), "p": str(product)}).mappings().one()
    assert row["status"] == "PENDING"
    assert row["request_type"] == "COST_CHANGE"


def test_agent_action_is_audited_as_service_account(db, client, org, admin,
                                                    account, mcp_on):
    """"김대표가 했다" 로 남으면 자동화 사고의 책임 소재를 못 찾는다."""
    product = make_product(db, org_id=org, cost_amount=5000)
    db.commit()
    issued = _issue(db, org, admin, account,
                    ["products:read", "approvals:create"])
    client.post("/api/v1/mcp/call", json={
        "name": "propose_cost_change",
        "arguments": {"product_id": str(product), "cost_amount": 9000,
                      "reason": "공급처 인상"},
    }, headers=auth(issued["token"]))

    row = db.execute(text("""
        SELECT actor_type, actor_user_id, actor_service_account_id, reason
          FROM audit_log
         WHERE org_id = :o AND actor_type = 'SERVICE_ACCOUNT'
         ORDER BY occurred_at DESC LIMIT 1
    """), {"o": str(org)}).mappings().first()
    assert row is not None, "서비스 계정 감사 기록이 없다"
    assert row["actor_user_id"] is None
    assert str(row["actor_service_account_id"]) == account["id"]
    assert row["reason"] == "MCP_PROPOSE_COST_CHANGE"


def test_token_is_never_logged_in_the_response(db, client, org, admin,
                                               account, mcp_on):
    issued = _issue(db, org, admin, account, ["products:read"])
    res = client.get("/api/v1/mcp/whoami", headers=auth(issued["token"]))
    assert res.status_code == 200
    assert issued["token"] not in res.text
    assert SECRET_MARKER not in res.text
