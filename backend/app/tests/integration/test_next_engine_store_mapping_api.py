"""스토어 → 판매처 계정 매핑 API (M052).

이 파일이 고정하는 것
------------------
1. 매핑은 **HTTP 경로로** 만들 수 있다 — 사용자가 SQL 을 칠 필요가 없다
2. 이름이 아니라 **계정 id** 를 받는다
3. 이미 다른 계정에 붙은 스토어는 조용히 갈아끼우지 않는다 (409)
4. 남의 조직 계정에는 붙지 않는다
5. 조회는 넓게, 변경은 ADMIN 만
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
from app.services import external_sync as ex
from app.tests.fixtures.factories import (
    make_app_user,
    make_integration_account,
    make_organization,
)


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
def admin_token(db: Session, org: uuid.UUID) -> str:
    uid = make_app_user(db, org_id=org, role="ADMIN")
    db.commit()
    return create_access_token(user_id=uid, org_id=org, role="ADMIN")


@pytest.fixture
def operator_token(db: Session, org: uuid.UUID) -> str:
    uid = make_app_user(db, org_id=org, role="OPERATOR")
    db.commit()
    return create_access_token(user_id=uid, org_id=org, role="OPERATOR")


def auth(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def ne_account(db: Session, org: uuid.UUID) -> str:
    actor = make_app_user(db, org_id=org, role="ADMIN")
    sysid = db.execute(text("""
        INSERT INTO external_system (code, name, system_type, supports_api_key)
        VALUES ('NE_MAP_TEST', 'NE 매핑 테스트', 'SELLER_TOOL', TRUE)
        ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id
    """)).scalar()
    db.commit()
    acc = ex.create_account(
        db, org_id=org, actor_user_id=actor,
        external_system_id=uuid.UUID(str(sysid)),
        display_name="NE", account_code="NE-MAP-1")
    return acc["id"]


@pytest.fixture
def coupang(db: Session, org: uuid.UUID) -> uuid.UUID:
    aid = make_integration_account(db, org_id=org, provider="COUPANG",
                                   account_label="쿠팡 본점")
    db.commit()
    return aid


def test_store_can_be_mapped_over_http(client, admin_token, ne_account, coupang):
    """사용자가 SQL 을 칠 필요가 없다."""
    res = client.put(
        f"/api/v1/external/accounts/{ne_account}/store-mappings",
        headers=auth(admin_token),
        json={"mall_cd": "mall02400", "integration_account_id": str(coupang)})

    assert res.status_code == 200, res.text
    assert res.json()["data"]["integration_account_id"] == str(coupang)

    listed = client.get(f"/api/v1/external/accounts/{ne_account}/store-mappings",
                        headers=auth(admin_token))
    rows = listed.json()["data"]
    assert [r["mall_cd"] for r in rows] == ["mall02400"]
    assert rows[0]["account_label"] == "쿠팡 본점"


def test_mapping_takes_an_account_id_not_a_name(client, admin_token, ne_account, coupang):
    """`mall_name` 을 받지 않는다 — 받으면 언젠가 이름으로 계정을 찾게 된다."""
    res = client.put(
        f"/api/v1/external/accounts/{ne_account}/store-mappings",
        headers=auth(admin_token),
        json={"mall_cd": "mall02400", "mall_name": "쿠팡"})

    assert res.status_code == 422


def test_remapping_to_another_account_is_a_conflict(
        client, admin_token, ne_account, coupang, db, org):
    """조용히 갈아끼우면 그 스토어로 이미 들어온 매출의 귀속이 통째로 바뀐다."""
    second = make_integration_account(db, org_id=org, provider="COUPANG",
                                      account_label="쿠팡 2호점")
    db.commit()
    client.put(f"/api/v1/external/accounts/{ne_account}/store-mappings",
               headers=auth(admin_token),
               json={"mall_cd": "mall02400", "integration_account_id": str(coupang)})

    res = client.put(
        f"/api/v1/external/accounts/{ne_account}/store-mappings",
        headers=auth(admin_token),
        json={"mall_cd": "mall02400", "integration_account_id": str(second)})

    assert res.status_code == 409


def test_remapping_to_the_same_account_is_idempotent(
        client, admin_token, ne_account, coupang):
    body = {"mall_cd": "mall02400", "integration_account_id": str(coupang)}
    url = f"/api/v1/external/accounts/{ne_account}/store-mappings"
    assert client.put(url, headers=auth(admin_token), json=body).status_code == 200
    assert client.put(url, headers=auth(admin_token), json=body).status_code == 200


def test_cannot_map_to_another_orgs_account(client, admin_token, ne_account, db):
    """남의 조직 계정에 붙으면 매출이 남의 장부로 간다."""
    other_org = make_organization(db)
    foreign = make_integration_account(db, org_id=other_org, provider="COUPANG",
                                       account_label="남의 계정")
    db.commit()

    res = client.put(
        f"/api/v1/external/accounts/{ne_account}/store-mappings",
        headers=auth(admin_token),
        json={"mall_cd": "mall02400", "integration_account_id": str(foreign)})

    assert res.status_code == 404


def test_unmapping_is_soft_and_allows_remap(client, admin_token, ne_account, coupang,
                                            db, org):
    """연결을 끊은 뒤에는 다른 계정에 다시 붙일 수 있다."""
    url = f"/api/v1/external/accounts/{ne_account}/store-mappings"
    client.put(url, headers=auth(admin_token),
               json={"mall_cd": "mall02400", "integration_account_id": str(coupang)})

    assert client.delete(f"{url}/mall02400", headers=auth(admin_token)).status_code == 204

    second = make_integration_account(db, org_id=org, provider="COUPANG",
                                      account_label="쿠팡 2호점")
    db.commit()
    res = client.put(url, headers=auth(admin_token),
                     json={"mall_cd": "mall02400",
                           "integration_account_id": str(second)})
    assert res.status_code == 200

    # 끊긴 기록은 남는다 — 과거에 무엇에 붙어 있었는지가 사고 조사에 필요하다.
    assert db.execute(text("""
        SELECT COUNT(*) FROM external_entity_mapping
         WHERE external_entity_type = 'MARKETPLACE_ACCOUNT' AND deleted_at IS NOT NULL
    """)).scalar() >= 1


def test_only_admin_can_change_mappings(client, operator_token, ne_account, coupang):
    """매핑이 바뀌면 매출 귀속이 바뀐다. 조회보다 좁은 권한이다."""
    url = f"/api/v1/external/accounts/{ne_account}/store-mappings"
    assert client.get(url, headers=auth(operator_token)).status_code == 200

    res = client.put(url, headers=auth(operator_token),
                     json={"mall_cd": "mall02400",
                           "integration_account_id": str(coupang)})
    assert res.status_code == 403


# ── 상품 매핑 (M052 PRODUCT 유형) ────────────────────────────────────────

@pytest.fixture
def product(db: Session, org: uuid.UUID) -> uuid.UUID:
    from app.tests.fixtures.factories import make_product

    pid = make_product(db, org_id=org, business_key="MAP-P-1")
    db.commit()
    return pid


def test_product_can_be_mapped_and_unmapped(client, admin_token, ne_account, product):
    """연결이 틀렸다는 것은 나중에야 드러난다. 되돌릴 길이 있어야 한다."""
    url = f"/api/v1/external/accounts/{ne_account}/product-mappings"
    res = client.put(url, headers=auth(admin_token),
                     json={"ne_product_cd": "NE-9", "product_id": str(product)})
    assert res.status_code == 200, res.text
    assert res.json()["data"]["product_id"] == str(product)

    assert client.delete(f"{url}/NE-9",
                         headers=auth(admin_token)).status_code == 204

    # 끊은 뒤에는 다른 상품에 다시 붙일 수 있다.
    assert client.put(url, headers=auth(admin_token),
                      json={"ne_product_cd": "NE-9",
                            "product_id": str(product)}).status_code == 200


def test_product_remap_to_another_product_is_a_conflict(
        client, admin_token, ne_account, product, db, org):
    from app.tests.fixtures.factories import make_product

    other = make_product(db, org_id=org, business_key="MAP-P-2")
    db.commit()
    url = f"/api/v1/external/accounts/{ne_account}/product-mappings"
    client.put(url, headers=auth(admin_token),
               json={"ne_product_cd": "NE-9", "product_id": str(product)})

    res = client.put(url, headers=auth(admin_token),
                     json={"ne_product_cd": "NE-9", "product_id": str(other)})
    assert res.status_code == 409


def test_cannot_map_another_orgs_product(client, admin_token, ne_account, db):
    from app.tests.fixtures.factories import make_product

    other_org = make_organization(db)
    foreign = make_product(db, org_id=other_org, business_key="FOREIGN-P")
    db.commit()

    res = client.put(f"/api/v1/external/accounts/{ne_account}/product-mappings",
                     headers=auth(admin_token),
                     json={"ne_product_cd": "NE-9", "product_id": str(foreign)})
    assert res.status_code == 404


def test_product_mapping_takes_no_name(client, admin_token, ne_account, product):
    """상품명을 받으면 언젠가 이름으로 찾는 코드가 붙는다."""
    res = client.put(f"/api/v1/external/accounts/{ne_account}/product-mappings",
                     headers=auth(admin_token),
                     json={"ne_product_cd": "NE-9", "product_name": "테스트상품"})
    assert res.status_code == 422


def test_only_admin_can_change_product_mappings(client, operator_token, ne_account,
                                                product):
    res = client.put(f"/api/v1/external/accounts/{ne_account}/product-mappings",
                     headers=auth(operator_token),
                     json={"ne_product_cd": "NE-9", "product_id": str(product)})
    assert res.status_code == 403
