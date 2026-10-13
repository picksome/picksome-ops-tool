"""MCP 재고·발주 도구.

이 파일이 고정하는 것
------------------
1. 발주를 **실행하는** tool 이 없다
2. `purchasing:execute` scope 는 발급 자체가 금지다
3. `propose_purchase_order` 는 DRAFT 까지만
4. 에이전트가 만든 발주는 **사람으로 위장하지 않는다**
5. scope 없이는 못 부른다
6. `cost:read` 없으면 금액이 지워진다
7. 재주문 tool 에 기본값이 없다

1·2·3 이 핵심이다
---------------
AI 에게 돈 쓰는 경로를 열어 주면 승인 체계가 장식이 된다.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.api_token import (
    ALL_SCOPES,
    FORBIDDEN_SCOPES,
    InvalidScopeError,
    validate_scopes,
)
from app.mcp import tools as mcp
from app.services import inventory
from app.services.service_account import TokenPrincipal
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org)


@pytest.fixture
def agent(db: Session, org: uuid.UUID) -> uuid.UUID:
    sid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO service_account (id, org_id, name, status)
        VALUES (:id, :o, '테스트 에이전트', 'ACTIVE')
    """), {"id": str(sid), "o": str(org)})
    db.commit()
    return sid


def _principal(org, agent, scopes: list[str]) -> TokenPrincipal:
    return TokenPrincipal(
        org_id=org, service_account_id=agent, scopes=tuple(scopes),
        token_id=uuid.uuid4(), service_account_name="테스트 에이전트",
    )


# ── 실행 경로가 없다 ───────────────────────────────────────────


def test_no_execute_tool_exists():
    """발주를 실제로 넣는 tool 이 없다."""
    names = {t.name for t in mcp.TOOLS}
    assert "place_purchase_order" not in names
    assert "execute_purchase_order" not in names
    assert "confirm_purchase_order" not in names
    # 모든 tool 은 READ 아니면 PROPOSE 다.
    assert {t.kind for t in mcp.TOOLS} <= {"READ", "PROPOSE"}


def test_purchasing_execute_scope_cannot_be_issued():
    """토큰에 발주 실행 권한을 넣을 수 없다."""
    assert "purchasing:execute" in FORBIDDEN_SCOPES
    assert "purchasing:execute" not in ALL_SCOPES
    with pytest.raises(InvalidScopeError):
        validate_scopes(["purchasing:execute"])


def test_new_read_scopes_are_issuable():
    assert validate_scopes(["inventory:read", "purchasing:read"]) == [
        "inventory:read", "purchasing:read"]


# ── scope 강제 ─────────────────────────────────────────────────


def test_tool_requires_scope(db: Session, org, agent):
    """목록에서 안 보인다고 못 부르는 게 아니다. 부를 때 막아야 한다."""
    p = _principal(org, agent, ["products:read"])
    with pytest.raises(Exception):
        mcp.call(db, principal=p, name="get_inventory_status", arguments={})


def test_visible_tools_follow_scope(db: Session, org, agent):
    p = _principal(org, agent, ["inventory:read"])
    names = {t.name for t in mcp.visible_tools(p)}
    assert "get_inventory_status" in names
    assert "get_purchase_order" not in names  # purchasing:read 없음


# ── 재고 읽기 ──────────────────────────────────────────────────


def test_inventory_status_keeps_unknown_marker(db: Session, org, agent, actor):
    """`reserved_status` 가 그대로 전달된다.

    AI 가 "예약 0개" 로 요약하지 않으려면 모른다는 사실이 데이터에 있어야 한다.
    """
    p = make_product(db, org_id=org)
    inventory.record_transaction(
        db, org_id=org, product_id=p, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=7, actor_user_id=actor,
        unit_cost=1000)
    db.commit()

    principal = _principal(org, agent, ["inventory:read"])
    result = mcp.call(db, principal=principal,
                      name="get_inventory_status", arguments={})
    assert result["reserved_status"] == "NOT_CONNECTED"
    assert result["items"][0]["on_hand"] == 7


def test_movements_hide_cost_without_scope(db: Session, org, agent, actor):
    """`cost:read` 가 없으면 취득 단가가 안 나간다."""
    p = make_product(db, org_id=org)
    inventory.record_transaction(
        db, org_id=org, product_id=p, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=3, actor_user_id=actor,
        unit_cost=12_345)
    db.commit()

    without = mcp.call(
        db, principal=_principal(org, agent, ["inventory:read"]),
        name="list_inventory_movements", arguments={"product_id": str(p)})
    assert "unit_cost" not in without["items"][0]

    with_cost = mcp.call(
        db, principal=_principal(org, agent, ["inventory:read", "cost:read"]),
        name="list_inventory_movements", arguments={"product_id": str(p)})
    assert with_cost["items"][0]["unit_cost"] == 12_345


def test_reorder_tool_has_no_defaults(db: Session, org, agent):
    """기간을 안 주면 계산하지 않는다. AI 가 30일을 지어내지 못하게."""
    result = mcp.call(
        db, principal=_principal(org, agent, ["inventory:read"]),
        name="list_reorder_candidates", arguments={})
    assert result["status"] == "NOT_CONFIGURED"
    assert result["candidates"] == []
    assert "기본값" in result["reason"]


# ── 제안 ───────────────────────────────────────────────────────


def _supplier(db: Session, org) -> uuid.UUID:
    sid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO supplier_site (id, org_id, supplier_code, name, country_code)
        VALUES (:id, :o, :c, 'MCP공급처', 'KR')
    """), {"id": str(sid), "o": str(org), "c": f"S-{sid.hex[:8]}"})
    db.commit()
    return sid


def test_propose_creates_draft_as_service_account(db: Session, org, agent):
    """에이전트가 만든 초안은 DRAFT 이고, 사람으로 위장하지 않는다."""
    site = _supplier(db, org)
    p = make_product(db, org_id=org)

    result = mcp.call(
        db, principal=_principal(org, agent, ["purchasing:propose"]),
        name="propose_purchase_order",
        arguments={
            "supplier_site_id": str(site),
            "lines": [{"product_id": str(p), "quantity": 5, "unit_price": 1000}],
            "reason": "재고 소진 임박",
        })

    assert result["status"] == "DRAFT"
    row = db.execute(text("""
        SELECT status, created_by, created_by_service_account
          FROM purchase_order WHERE id = :id
    """), {"id": result["purchase_order_id"]}).mappings().one()
    assert row["status"] == "DRAFT"
    # 사람 칸은 비어 있다.
    assert row["created_by"] is None
    assert str(row["created_by_service_account"]) == str(agent)


def test_propose_requires_reason_and_price(db: Session, org, agent):
    """사유 없는 제안, 단가 없는 제안은 거부한다."""
    site = _supplier(db, org)
    p = make_product(db, org_id=org)
    principal = _principal(org, agent, ["purchasing:propose"])

    with pytest.raises(ValueError):
        mcp.call(db, principal=principal, name="propose_purchase_order",
                 arguments={"supplier_site_id": str(site),
                            "lines": [{"product_id": str(p), "quantity": 1,
                                       "unit_price": 100}],
                            "reason": "  "})
    db.rollback()
    with pytest.raises(ValueError):
        mcp.call(db, principal=principal, name="propose_purchase_order",
                 arguments={"supplier_site_id": str(site),
                            "lines": [{"product_id": str(p), "quantity": 1}],
                            "reason": "사유 있음"})


def test_propose_needs_propose_scope(db: Session, org, agent):
    """읽기 권한만으로는 제안할 수 없다."""
    site = _supplier(db, org)
    p = make_product(db, org_id=org)
    with pytest.raises(Exception):
        mcp.call(db, principal=_principal(org, agent, ["purchasing:read"]),
                 name="propose_purchase_order",
                 arguments={"supplier_site_id": str(site),
                            "lines": [{"product_id": str(p), "quantity": 1,
                                       "unit_price": 100}],
                            "reason": "테스트"})
