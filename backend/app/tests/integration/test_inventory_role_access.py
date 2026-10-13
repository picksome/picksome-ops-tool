"""재고·발주 역할 통제와 **원가 파생 노출** 검사 (실제 HTTP).

왜 따로 두나
----------
`test_role_access.py` 는 정산·현금흐름처럼 예전부터 있던 금액 경로를 본다.
재고·발주는 축이 하나 더 있다.

```
재고 수량 조회      "몇 개 남았나"     ← 운영 정보
재고 취득원가 조회   "얼마에 샀나"      ← 사업의 핵심 정보
```

예전에는 원가를 숨기려다 **수량까지 같이 막아** READONLY 가 재고를 아예 못
봤다. 둘을 나눈 뒤, 나눈 것이 실제로 지켜지는지 여기서 확인한다.

파생 노출을 잡는 것이 핵심이다
--------------------------
단가를 지웠는데 합계와 수량이 남으면 나눗셈 한 번으로 복원된다. 그건 가린
것이 아니다. 그래서 **응답 어디에도 금액 냄새가 없는지**를 본다.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.deps import get_db
from app.main import app
from app.services import inventory, purchasing
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
)

ALL_ROLES = (
    "ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY",
    "EXTERNAL_ACCOUNTANT",
)

#: 취득원가를 볼 수 있는 역할. `PERMISSION_MATRIX` 와 같아야 한다.
COST_ROLES = {"ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING"}

UNIT_COST = 4_321          # 응답에서 찾기 쉬운 값
ORDER_QTY = 7
LINE_TOTAL = UNIT_COST * ORDER_QTY


@pytest.fixture
def client(db: Session):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def tokens(db: Session, org: uuid.UUID) -> dict[str, str]:
    out: dict[str, str] = {}
    for role in ALL_ROLES:
        uid = make_app_user(db, org_id=org, role=role)
        out[role] = create_access_token(user_id=uid, org_id=org, role=role)
    db.commit()
    return out


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def data(db: Session, org: uuid.UUID) -> dict[str, str]:
    """재고 원장 + 발주 한 건. 취득원가가 들어 있다."""
    actor = make_app_user(db, org_id=org, role="ADMIN",
                          email="setup@example.com")
    product = make_product(db, org_id=org, cost_amount=9_999)
    site = uuid.uuid4()
    db.execute(text("""
        INSERT INTO supplier_site (id, org_id, supplier_code, name, country_code)
        VALUES (:i, :o, :c, '권한공급처', 'KR')
    """), {"i": str(site), "o": str(org), "c": f"S-{site.hex[:8]}"})
    inventory.record_transaction(
        db, org_id=org, product_id=product, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=12, actor_user_id=actor,
        unit_cost=UNIT_COST)
    db.commit()
    order = purchasing.create_order(
        db, org_id=org, actor_user_id=actor, supplier_site_id=site,
        lines=[{"product_id": str(product), "quantity": ORDER_QTY,
                "unit_price": UNIT_COST}])
    return {"product_id": str(product), "order_id": order["id"]}


# ── 1. 수량과 원가는 다른 축이다 ───────────────────────────────


@pytest.mark.parametrize("role", ALL_ROLES)
def test_inventory_quantity_visibility(client, tokens, data, role: str):
    """재고 **수량**은 조회 전용까지 본다. 외부 세무대리인은 못 본다."""
    res = client.get("/api/v1/inventory", headers=auth(tokens[role]))
    if role == "EXTERNAL_ACCOUNTANT":
        assert res.status_code == 403
        return
    assert res.status_code == 200, res.text
    items = res.json()["data"]["items"]
    assert items and items[0]["on_hand"] == 12


@pytest.mark.parametrize("role", ALL_ROLES)
def test_acquisition_cost_is_redacted_for_readonly(
    client, tokens, data, role: str,
):
    """원장의 취득 단가는 원가 권한자에게만 나간다."""
    res = client.get(
        f"/api/v1/inventory/{data['product_id']}/transactions",
        headers=auth(tokens[role]))
    if role == "EXTERNAL_ACCOUNTANT":
        assert res.status_code == 403
        return
    assert res.status_code == 200
    row = res.json()["data"][0]
    if role in COST_ROLES:
        assert row["unit_cost"] == UNIT_COST
    else:
        # **키 자체가 없다.** null 로 보내면 "원가가 없는 재고" 로 읽힌다.
        assert "unit_cost" not in row


# ── 2. 파생 노출 ───────────────────────────────────────────────


@pytest.mark.parametrize("role", sorted(set(ALL_ROLES) - {"EXTERNAL_ACCOUNTANT"}))
def test_no_cost_leaks_anywhere_in_inventory_response(
    client, tokens, data, role: str,
):
    """응답 **어디에도** 금액이 남지 않는지 통째로 훑는다.

    단가만 지우고 합계를 남기면 수량으로 나눠 복원된다. 필드 이름을 하나씩
    확인하는 대신 값 자체가 문자열 어디에도 없는지 본다.
    """
    body = client.get("/api/v1/inventory", headers=auth(tokens[role])).text
    if role in COST_ROLES:
        return
    assert str(UNIT_COST) not in body
    assert str(LINE_TOTAL) not in body


@pytest.mark.parametrize("role", ["ADMIN", "ACCOUNTING", "SOURCING"])
def test_purchase_totals_visible_to_cost_roles(client, tokens, data, role: str):
    res = client.get(f"/api/v1/purchase-orders/{data['order_id']}",
                     headers=auth(tokens[role]))
    assert res.status_code == 200, res.text
    order = res.json()["data"]
    assert order["total_amount"] == LINE_TOTAL
    assert order["lines"][0]["unit_price"] == UNIT_COST


def test_purchase_is_closed_to_readonly(client, tokens, data):
    """발주는 금액이 본문이다. 수량만 가린 발주 화면은 쓸모가 없다."""
    for path in ("/api/v1/purchase-orders",
                 f"/api/v1/purchase-orders/{data['order_id']}"):
        res = client.get(path, headers=auth(tokens["READONLY"]))
        assert res.status_code == 403, f"{path} → {res.status_code}"


# ── 3. 쓰기 경로 ───────────────────────────────────────────────


WRITE_CASES: list[tuple[str, str, str, dict, set[str]]] = [
    ("재고 조정", "POST", "/api/v1/inventory/adjust",
     {"quantity_delta": -1, "reason": "역할 테스트"},
     {"ADMIN", "OPERATOR"}),
    ("발주 초안 생성", "POST", "/api/v1/purchase-orders",
     {"supplier_site_id": None, "lines": []},
     {"ADMIN", "OPERATOR", "SOURCING"}),
]


@pytest.mark.parametrize("role", ALL_ROLES)
@pytest.mark.parametrize(("label", "method", "path", "body", "allowed"),
                         WRITE_CASES)
def test_write_endpoints_enforce_roles(
    client, tokens, data, role: str, label: str, method: str, path: str,
    body: dict, allowed: set[str],
):
    """버튼을 숨기는 것으로는 못 막는다. **직접 호출**해서 확인한다."""
    payload = dict(body)
    if "quantity_delta" in payload:
        payload["product_id"] = data["product_id"]

    res = client.request(method, path, json=payload, headers=auth(tokens[role]))
    if role in allowed:
        # 허용된 역할은 권한 때문에 막히지 않는다. 본문이 부실해 422/400 은
        # 날 수 있다 — 그건 권한 문제가 아니다.
        assert res.status_code != 403, f"{label}/{role} 가 막혔다"
    else:
        assert res.status_code == 403, f"{label}/{role} 가 통과했다"


@pytest.mark.parametrize("role", ALL_ROLES)
def test_inventory_import_apply_is_restricted(client, tokens, role: str):
    """1,000행을 한 번에 바꾸는 경로다. 조정과 같은 권한으로 좁힌다."""
    res = client.post(
        "/api/v1/inventory/import",
        files={"file": ("t.csv", b"\xed\x92\x88\xeb\xb2\x88,\xec\x88\x98\xeb\x9f\x89\n", "text/csv")},
        data={"mode": "DRY_RUN"},
        headers=auth(tokens[role]))
    if role in {"ADMIN", "OPERATOR"}:
        assert res.status_code != 403
    else:
        assert res.status_code == 403


@pytest.mark.parametrize("role", ALL_ROLES)
def test_sourcing_import_is_restricted(client, tokens, role: str):
    """소싱 연결은 소싱 담당의 업무다. 운영 담당과 다르다."""
    res = client.post(
        "/api/v1/sourcing/import",
        files={"file": ("t.csv", b"sku\n", "text/csv")},
        data={"mode": "DRY_RUN"},
        headers=auth(tokens[role]))
    if role in {"ADMIN", "SOURCING"}:
        assert res.status_code != 403
    else:
        assert res.status_code == 403


def test_no_token_is_401_not_403(client):
    """토큰이 없으면 401 이다. 버튼 숨김으로는 아무것도 못 막는다."""
    assert client.get("/api/v1/inventory").status_code == 401
    assert client.post("/api/v1/inventory/adjust", json={}).status_code == 401


# ── 4. 조직 격리 ───────────────────────────────────────────────


def test_cross_org_inventory_is_invisible(client, db: Session, tokens, data):
    """다른 조직의 재고가 목록에 섞이지 않는다."""
    other_org = make_organization(db)
    other_actor = make_app_user(db, org_id=other_org, role="ADMIN",
                                email="other@example.com")
    other_product = make_product(db, org_id=other_org, name_ko="남의상품")
    inventory.record_transaction(
        db, org_id=other_org, product_id=other_product, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=99, actor_user_id=other_actor,
        unit_cost=1)
    db.commit()

    body = client.get("/api/v1/inventory", headers=auth(tokens["ADMIN"])).json()
    names = [i["product_name"] for i in body["data"]["items"]]
    assert "남의상품" not in names


def test_cross_org_purchase_order_is_404(client, db: Session, tokens):
    """남의 발주 id 를 알아도 열리지 않는다."""
    other_org = make_organization(db)
    other_actor = make_app_user(db, org_id=other_org, role="ADMIN",
                                email="other2@example.com")
    other_product = make_product(db, org_id=other_org)
    site = uuid.uuid4()
    db.execute(text("""
        INSERT INTO supplier_site (id, org_id, supplier_code, name, country_code)
        VALUES (:i, :o, :c, '남의공급처', 'KR')
    """), {"i": str(site), "o": str(other_org), "c": f"S-{site.hex[:8]}"})
    db.commit()
    other_order = purchasing.create_order(
        db, org_id=other_org, actor_user_id=other_actor, supplier_site_id=site,
        lines=[{"product_id": str(other_product), "quantity": 1,
                "unit_price": 100}])

    res = client.get(f"/api/v1/purchase-orders/{other_order['id']}",
                     headers=auth(tokens["ADMIN"]))
    assert res.status_code == 404


def test_cross_org_adjustment_is_rejected(client, db: Session, tokens):
    """남의 상품 재고를 조정할 수 없다."""
    other_org = make_organization(db)
    other_product = make_product(db, org_id=other_org)
    db.commit()

    res = client.post("/api/v1/inventory/adjust", json={
        "product_id": str(other_product), "quantity_delta": 5,
        "reason": "남의 상품",
    }, headers=auth(tokens["ADMIN"]))
    # 만들어지면 안 된다. 400/404 중 무엇이든 성공이 아니어야 한다.
    assert res.status_code >= 400, res.text
    assert db.execute(text("""
        SELECT COUNT(*) FROM inventory_transaction WHERE product_id = :p
    """), {"p": str(other_product)}).scalar_one() == 0
