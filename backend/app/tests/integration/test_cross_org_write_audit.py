"""조직 경계 red team — **요청 본문이 남의 id 를 실어 보낼 때.**

왜 이 파일이 있나
--------------
스키마에는 `org_id` 를 가진 테이블이 75개 있고, 그중 157개 FK 가 **부모의
org 가 같은지 DB 차원에서 강제하지 않는다.** 단순 FK 만으로는

```
org A 의 행  →  org B 의 product_id
```

가 막히지 않는다.

대부분은 서버가 세션에서 채우는 값(`created_by` 등)이라 공격자가 못 건드린다.
**실제 공격면은 요청 본문이 id 를 실어 보내는 곳**이고, 여기서 그 경로들을
직접 찌른다.

재고 조정에서 실제로 이 결함이 있었다(남의 상품 id 로 200). 같은 모양이 다른
곳에도 있는지 확인한다.

무엇이 통과인가
------------
남의 id 를 넣은 요청은 **거절**되고, 그 결과로 **어떤 행도 만들어지지 않아야**
한다. 상태 코드가 400 인지 404 인지는 정책 문제라 여기서 따지지 않는다 —
`>= 400` 이면 되고, 대신 **DB 에 흔적이 없는지**를 본다.
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
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
)


@pytest.fixture
def client(db: Session):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def mine(db: Session) -> dict:
    """공격자의 조직. 정상적인 자기 자원도 함께 만든다."""
    org = make_organization(db)
    user = make_app_user(db, org_id=org, role="ADMIN", email="mine@example.com")
    product = make_product(db, org_id=org, name_ko="내상품")
    site = uuid.uuid4()
    db.execute(text("""
        INSERT INTO supplier_site (id, org_id, supplier_code, name, country_code)
        VALUES (:i, :o, :c, '내공급처', 'KR')
    """), {"i": str(site), "o": str(org), "c": f"MINE-{site.hex[:6]}"})
    db.commit()
    return {
        "org": org, "token": create_access_token(
            user_id=user, org_id=org, role="ADMIN"),
        "product": product, "site": site,
    }


@pytest.fixture
def theirs(db: Session) -> dict:
    """피해자의 조직. 이 id 들이 새어 나가면 안 된다."""
    org = make_organization(db)
    user = make_app_user(db, org_id=org, role="ADMIN",
                         email="theirs@example.com")
    product = make_product(db, org_id=org, name_ko="남의상품")
    site = uuid.uuid4()
    db.execute(text("""
        INSERT INTO supplier_site (id, org_id, supplier_code, name, country_code)
        VALUES (:i, :o, :c, '남의공급처', 'KR')
    """), {"i": str(site), "o": str(org), "c": f"THEIRS-{site.hex[:6]}"})
    source = uuid.uuid4()
    db.execute(text("""
        INSERT INTO product_source (id, org_id, product_id, supplier_site_id,
                                    status, currency)
        VALUES (:i, :o, :p, :s, 'ACTIVE', 'KRW')
    """), {"i": str(source), "o": str(org), "p": str(product), "s": str(site)})
    db.commit()
    return {"org": org, "user": user, "product": product, "site": site,
            "source": source}


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _count(db: Session, table: str, org) -> int:
    return db.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE org_id = :o"),
        {"o": str(org)}).scalar_one()


# ── 재고 ───────────────────────────────────────────────────────


def test_adjust_with_foreign_product_is_rejected(client, db, mine, theirs):
    """이미 고친 결함. 회귀로 남긴다."""
    res = client.post("/api/v1/inventory/adjust", json={
        "product_id": str(theirs["product"]), "quantity_delta": 5,
        "reason": "남의 상품",
    }, headers=auth(mine["token"]))
    assert res.status_code >= 400, res.text
    assert _count(db, "inventory_transaction", mine["org"]) == 0


def test_adjust_with_foreign_variant_is_rejected(client, db, mine, theirs):
    """상품은 내 것인데 **옵션만** 남의 것인 경우."""
    variant = uuid.uuid4()
    db.execute(text("""
        INSERT INTO product_variant (id, org_id, product_id, variant_key,
                                     option_values_json)
        VALUES (:v, :o, :p, 'X', '{}'::jsonb)
    """), {"v": str(variant), "o": str(theirs["org"]),
           "p": str(theirs["product"])})
    db.commit()

    res = client.post("/api/v1/inventory/adjust", json={
        "product_id": str(mine["product"]),
        "product_variant_id": str(variant),
        "quantity_delta": 3, "reason": "남의 옵션",
    }, headers=auth(mine["token"]))
    assert res.status_code >= 400, res.text
    assert _count(db, "inventory_transaction", mine["org"]) == 0


# ── 발주 ───────────────────────────────────────────────────────


def test_purchase_with_foreign_supplier_is_rejected(client, db, mine, theirs):
    res = client.post("/api/v1/purchase-orders", json={
        "supplier_site_id": str(theirs["site"]),
        "lines": [{"product_id": str(mine["product"]), "quantity": 1,
                   "unit_price": 1000}],
    }, headers=auth(mine["token"]))
    assert res.status_code >= 400, res.text
    assert _count(db, "purchase_order", mine["org"]) == 0


def test_purchase_with_foreign_product_line_is_rejected(
    client, db, mine, theirs,
):
    """공급처는 내 것인데 **품목이 남의 상품**인 경우.

    막히지 않으면 남의 상품이 우리 발주에 들어가고, 입고하면 우리 org 에
    남의 상품 재고가 생긴다.
    """
    res = client.post("/api/v1/purchase-orders", json={
        "supplier_site_id": str(mine["site"]),
        "lines": [{"product_id": str(theirs["product"]), "quantity": 1,
                   "unit_price": 1000}],
    }, headers=auth(mine["token"]))
    assert res.status_code >= 400, res.text
    assert _count(db, "purchase_order", mine["org"]) == 0
    assert _count(db, "purchase_order_line", mine["org"]) == 0


def test_purchase_detail_of_other_org_is_hidden(client, db, mine, theirs):
    from app.services import purchasing

    actor = make_app_user(db, org_id=theirs["org"], role="ADMIN",
                          email="t2@example.com")
    order = purchasing.create_order(
        db, org_id=theirs["org"], actor_user_id=actor,
        supplier_site_id=theirs["site"],
        lines=[{"product_id": str(theirs["product"]), "quantity": 1,
                "unit_price": 1000}])
    res = client.get(f"/api/v1/purchase-orders/{order['id']}",
                     headers=auth(mine["token"]))
    assert res.status_code == 404


# ── 소싱 연결 ──────────────────────────────────────────────────


def test_source_import_does_not_cross_orgs(client, db, mine, theirs):
    """가져오기는 열쇠로 찾는다 — 남의 조직 상품·공급처는 **안 보여야** 한다.

    열쇠가 우연히 같아도 다른 조직 것을 집으면 안 된다.
    """
    # 두 조직에 같은 SKU 를 만든다.
    db.execute(text("UPDATE product SET sku = 'SAME-SKU' WHERE id = :i"),
               {"i": str(theirs["product"])})
    db.commit()

    csv = "SKU,공급처코드\nSAME-SKU,THEIRS-x\n".encode()
    res = client.post(
        "/api/v1/sourcing/import",
        files={"file": ("s.csv", csv, "text/csv")},
        data={"mode": "DRY_RUN"}, headers=auth(mine["token"]))
    assert res.status_code == 200, res.text
    counts = res.json()["data"]["counts"]
    # 내 조직에는 그 SKU 가 없으므로 못 찾아야 한다.
    assert counts["UNMATCHED_PRODUCT"] == 1
    assert counts["NEW_MAPPING"] == 0


def test_inventory_import_does_not_cross_orgs(client, db, mine, theirs):
    db.execute(text("UPDATE product SET sku = 'DUP-SKU' WHERE id = :i"),
               {"i": str(theirs["product"])})
    db.commit()

    csv = "SKU,수량\nDUP-SKU,10\n".encode()
    res = client.post(
        "/api/v1/inventory/import",
        files={"file": ("i.csv", csv, "text/csv")},
        data={"mode": "APPLY"}, headers=auth(mine["token"]))
    assert res.status_code == 200, res.text
    assert res.json()["data"]["counts"]["UNMATCHED"] == 1
    # 어느 조직에도 원장이 생기지 않았다.
    assert _count(db, "inventory_transaction", mine["org"]) == 0
    assert _count(db, "inventory_transaction", theirs["org"]) == 0


# ── 집계 ───────────────────────────────────────────────────────


def test_aggregates_do_not_mix_orgs(client, db, mine, theirs):
    """상세뿐 아니라 **집계**에도 남의 데이터가 섞이면 안 된다."""
    from app.services import inventory

    other_actor = make_app_user(db, org_id=theirs["org"], role="ADMIN",
                                email="t3@example.com")
    inventory.record_transaction(
        db, org_id=theirs["org"], product_id=theirs["product"],
        product_variant_id=None, transaction_type="OPENING",
        quantity_delta=999, actor_user_id=other_actor, unit_cost=1)
    db.commit()

    body = client.get("/api/v1/inventory",
                      headers=auth(mine["token"])).json()["data"]
    assert body["items"] == []
    assert "남의상품" not in str(body)

    center = client.get("/api/v1/operations/exceptions",
                        headers=auth(mine["token"])).json()["data"]
    assert "남의상품" not in str(center)
