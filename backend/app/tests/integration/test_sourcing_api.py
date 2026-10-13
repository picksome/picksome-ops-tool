"""Sourcing API — 권한 · 민감 원가 가림 · 승인 연결 (Vertical Slice).

이 파일이 고정하는 것
------------------
1. 소싱 **메타데이터**(어디서 사 오나)는 넓게 보고, **매입가**는 좁게 본다
2. 가림은 **API 응답에서** 일어난다 — 프론트에서 숨기는 것으로 끝내지 않는다
3. `product.cost_amount` 는 소싱 원가를 등록해도 **변하지 않는다**
4. 적용은 `COST_CHANGE` 승인을 거친다. 승인 적용 후에만 상품 원가가 바뀐다
5. 승인 적용 후에도 **과거 판매의 실마진은 그대로**다
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
from app.services import sourcing_core as sc
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
    make_product_margin_actual,
    make_sales_order,
)

ALL_ROLES = ("ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY",
             "EXTERNAL_ACCOUNTANT")
DAY = date(2026, 8, 1)


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
    out = {}
    for role in ALL_ROLES:
        uid = make_app_user(db, org_id=org, role=role)
        out[role] = create_access_token(user_id=uid, org_id=org, role=role)
    db.commit()
    return out


def auth(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def seeded(db: Session, org: uuid.UUID):
    """공급처 + 상품 + 소싱처 + 원가 + 환율."""
    actor = make_app_user(db, org_id=org, role="ADMIN")
    product = make_product(db, org_id=org)
    supplier = sc.create_supplier(db, org_id=org, actor_user_id=actor, name="1688",
                                  website_url="https://detail.1688.com/offer/123.html")
    source = sc.add_product_source(
        db, org_id=org, actor_user_id=actor, product_id=product,
        supplier_site_id=uuid.UUID(supplier["id"]), currency="CNY",
        source_url="https://detail.1688.com/offer/123.html",
    )
    sc.record_fx_rate(db, org_id=org, actor_user_id=actor, base_currency="CNY",
                      quote_currency="KRW", rate="205")
    # 마진 엔진은 세금 정책 없이 계산하지 않는다(0% 로 계산하면 거짓이 된다).
    # 조직마다 세율이 다르므로 테스트 조직에도 실제로 하나 넣는다.
    db.execute(text("""
        INSERT INTO tax_policy (org_id, version, effective_from, tax_type,
                                vat_rate, calculation_method, default_vat_inclusion,
                                applies_to, is_active)
        VALUES (:o, 1, '2020-01-01', 'GENERAL', 0.1, 'STANDARD_RATIO',
                'INCLUDED', 'ALL', TRUE)
    """), {"o": str(org)})
    sc.add_cost_version(db, org_id=org, actor_user_id=actor,
                        product_source_id=uuid.UUID(source["id"]),
                        purchase_price=120, currency="CNY", valid_from=date(2026, 1, 1),
                        international_shipping=7_200)
    db.commit()
    return {"actor": actor, "product": product,
            "supplier_id": supplier["id"], "source_id": source["id"]}


# ──────────────────────────────────────────────────────────────────────────
# 권한
# ──────────────────────────────────────────────────────────────────────────

def test_supplier_list_is_visible_to_every_role(client, tokens, seeded):
    """어디서 사 오는지는 운영 업무에 필요하다 — 매입가와 다르다."""
    for role in ALL_ROLES:
        res = client.get("/api/v1/supplier-sites", headers=auth(tokens[role]))
        if role == "EXTERNAL_ACCOUNTANT":
            # 외부 세무대리인은 소싱 자체가 업무 범위 밖이다.
            assert res.status_code == 403, role
        else:
            assert res.status_code == 200, f"{role}: {res.text}"


def test_only_sourcing_and_admin_can_create_suppliers(client, tokens):
    body = {"name": "새 공급처"}
    for role in ("OPERATOR", "ACCOUNTING", "READONLY", "EXTERNAL_ACCOUNTANT"):
        res = client.post("/api/v1/supplier-sites", json=body, headers=auth(tokens[role]))
        assert res.status_code == 403, f"{role} 이 공급처를 만들 수 있다"

    assert client.post("/api/v1/supplier-sites", json=body,
                       headers=auth(tokens["SOURCING"])).status_code == 201


def test_purchase_price_is_hidden_from_readonly(client, tokens, seeded):
    """매입가는 사업의 핵심 정보다. 조회 전용 계정은 볼 이유가 없다.

    **응답에서 지운다** — 프론트에서 숨기면 API 를 직접 부르는 순간 뚫린다.
    """
    path = f"/api/v1/product-sources/{seeded['source_id']}/cost-versions"

    allowed = client.get(path, headers=auth(tokens["SOURCING"]))
    assert allowed.json()["data"][0]["purchase_price"] == 120

    hidden = client.get(path, headers=auth(tokens["READONLY"]))
    assert hidden.status_code == 200
    row = hidden.json()["data"][0]
    assert row["purchase_price"] is None
    assert row["redacted"] is True
    # 원가가 있었다는 사실 자체는 남긴다 — 아예 안 보이면 "원가 미등록" 으로 읽힌다.
    assert row["valid_from"] is not None


def test_landed_cost_is_refused_not_zeroed_for_readonly(client, tokens, seeded):
    """가릴 수 없는 계산 결과는 아예 주지 않는다. 0 으로 주면 거짓이 된다."""
    res = client.get(
        f"/api/v1/product-sources/{seeded['source_id']}/landed-cost",
        headers=auth(tokens["READONLY"]),
    )
    assert res.status_code == 403


def test_fx_can_be_managed_by_accounting(client, tokens):
    """결제·회계 시점 환율은 정산 담당의 업무다."""
    body = {"base_currency": "CNY", "rate": "205.34", "rate_type": "PAYMENT"}
    assert client.post("/api/v1/fx-rates", json=body,
                       headers=auth(tokens["ACCOUNTING"])).status_code == 201
    assert client.post("/api/v1/fx-rates", json=body,
                       headers=auth(tokens["OPERATOR"])).status_code == 403


def test_no_token_is_rejected(client):
    for path in ("/api/v1/supplier-sites", "/api/v1/fx-rates"):
        assert client.get(path).status_code == 401


# ──────────────────────────────────────────────────────────────────────────
# 흐름
# ──────────────────────────────────────────────────────────────────────────

def test_landed_cost_and_recommendation_flow(client, tokens, seeded):
    src = seeded["source_id"]

    lc = client.get(f"/api/v1/product-sources/{src}/landed-cost",
                    headers=auth(tokens["SOURCING"])).json()["data"]
    # 120 CNY × 205 = 24,600 + 국제배송 7,200 = 31,800
    assert lc["landed_cost"]["total_krw"] == 31_800
    assert lc["landed_cost"]["is_complete"] is True

    rec = client.get(f"/api/v1/product-sources/{src}/recommendation?marketplace=COUPANG",
                     headers=auth(tokens["SOURCING"])).json()["data"]
    assert rec["available"] is True
    assert rec["cost_krw"] == 31_800
    keys = {p["key"] for p in rec["prices"]}
    # 정책이 없으면 손익분기만 나온다 — 없는 목표를 지어내지 않는다.
    assert "break_even" in keys
    # 근거가 함께 나와야 나중에 "왜 달라졌나" 에 답할 수 있다.
    assert rec["basis"]["source_cost_version_id"]
    assert rec["basis"]["fx"]["rate"] == "205.000000"


def test_recommendation_is_unavailable_without_a_tax_policy(client, tokens, db, org):
    """세금 정책이 없으면 권장가를 내지 않는다 — 500 이 아니라 "계산 불가" 다.

    0% 세율로 계산하면 "이 값에 팔면 남는다" 가 나오고 사용자는 그 값으로
    가격을 정한다.
    """
    actor = make_app_user(db, org_id=org, role="ADMIN")
    product = make_product(db, org_id=org)
    sup = sc.create_supplier(db, org_id=org, actor_user_id=actor, name="세금정책없음")
    s = sc.add_product_source(db, org_id=org, actor_user_id=actor, product_id=product,
                              supplier_site_id=uuid.UUID(sup["id"]), currency="KRW")
    sc.add_cost_version(db, org_id=org, actor_user_id=actor,
                        product_source_id=uuid.UUID(s["id"]),
                        purchase_price=10_000, currency="KRW", valid_from=date(2026, 1, 1))
    db.commit()

    res = client.get(
        f"/api/v1/product-sources/{s['id']}/recommendation?marketplace=COUPANG",
        headers=auth(tokens["SOURCING"]),
    )
    assert res.status_code == 200, "계산 불가를 500 으로 내보냈다"
    data = res.json()["data"]
    assert data["available"] is False
    assert data["missing"] == ["POLICY:tax_policy"]


def test_recommendation_is_unavailable_without_fx(client, tokens, db, org):
    """환율이 없으면 권장가를 내지 않는다. 0 으로 계산하면 "얼마에 팔아도 남는다"."""
    actor = make_app_user(db, org_id=org, role="ADMIN")
    product = make_product(db, org_id=org)
    sup = sc.create_supplier(db, org_id=org, actor_user_id=actor, name="환율없음")
    s = sc.add_product_source(db, org_id=org, actor_user_id=actor, product_id=product,
                              supplier_site_id=uuid.UUID(sup["id"]), currency="USD")
    sc.add_cost_version(db, org_id=org, actor_user_id=actor,
                        product_source_id=uuid.UUID(s["id"]),
                        purchase_price=18, currency="USD", valid_from=date(2026, 1, 1))
    db.commit()

    res = client.get(
        f"/api/v1/product-sources/{s['id']}/recommendation?marketplace=COUPANG",
        headers=auth(tokens["SOURCING"]),
    ).json()["data"]
    assert res["available"] is False
    assert res["missing"] == ["FX_RATE:USD/KRW"]


def test_sensitivity_does_not_persist_scenarios(client, tokens, seeded, db, org):
    """시나리오는 DB 에 남지 않는다 — 가짜 환율이 실제 관측치처럼 보이면 안 된다."""
    before = len(sc.list_fx_rates(db, org_id=org))
    costs_before = len(sc.cost_history(
        db, org_id=org, product_source_id=uuid.UUID(seeded["source_id"])))

    res = client.get(
        f"/api/v1/product-sources/{seeded['source_id']}/sensitivity"
        "?marketplace=COUPANG&sale_price=60000",
        headers=auth(tokens["SOURCING"]),
    ).json()["data"]

    assert res["fx"]["available"] is True
    assert len(res["fx"]["scenarios"]) == 3
    # 환율이 오르면 마진이 줄어야 한다.
    rates = [float(s["margin_rate"]) for s in res["fx"]["scenarios"]]
    assert rates[0] > rates[-1]

    assert res["cost"]["available"] is True
    assert res["cost"]["scenarios"][0]["cost_krw"] == 31_800

    assert len(sc.list_fx_rates(db, org_id=org)) == before
    assert len(sc.cost_history(
        db, org_id=org, product_source_id=uuid.UUID(seeded["source_id"]))) == costs_before


def test_unsafe_source_url_is_rejected(client, tokens, seeded):
    """`javascript:` 를 저장하면 화면이 링크로 렌더할 때 클릭 한 번에 실행된다."""
    res = client.patch(
        f"/api/v1/product-sources/{seeded['source_id']}",
        json={"source_url": "javascript:alert(1)"},
        headers=auth(tokens["SOURCING"]),
    )
    assert res.status_code == 422


def test_primary_switch_is_one_call(client, tokens, seeded, db, org):
    """두 단계를 클라이언트가 순서대로 보내면 중간에 끊길 때 primary 가 없어진다."""
    actor = seeded["actor"]
    other = sc.create_supplier(db, org_id=org, actor_user_id=actor, name="대체 공급처")
    second = sc.add_product_source(
        db, org_id=org, actor_user_id=actor, product_id=seeded["product"],
        supplier_site_id=uuid.UUID(other["id"]),
    )
    db.commit()

    res = client.post(f"/api/v1/product-sources/{second['id']}/primary",
                      headers=auth(tokens["SOURCING"]))
    assert res.status_code == 200
    assert res.json()["data"]["is_primary"] is True

    rows = client.get(f"/api/v1/products/{seeded['product']}/sources",
                      headers=auth(tokens["SOURCING"])).json()["data"]
    assert sum(1 for r in rows if r["is_primary"]) == 1


# ──────────────────────────────────────────────────────────────────────────
# product.cost_amount 와의 경계
# ──────────────────────────────────────────────────────────────────────────

def test_registering_source_cost_never_changes_product_cost(
    client, tokens, seeded, db, org
):
    """소싱에서 관측한 사실과 계산에 쓰는 값은 다른 것이다."""
    before = db.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                        {"i": str(seeded["product"])}).scalar()

    res = client.post(
        f"/api/v1/product-sources/{seeded['source_id']}/cost-versions",
        json={"purchase_price": 200, "currency": "CNY", "valid_from": "2026-09-01"},
        headers=auth(tokens["SOURCING"]),
    )
    assert res.status_code == 201

    after = db.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                       {"i": str(seeded["product"])}).scalar()
    assert after == before, "소싱 원가 등록이 상품 원가를 덮어썼다"


def test_applying_cost_goes_through_approval_and_preserves_history(
    client, tokens, seeded, db, org
):
    """상품 원가 적용은 기존 COST_CHANGE 승인을 거친다.

    적용 후에도 소싱 원가 이력은 그대로 남는다 —
    `source cost = 관측 사실`, `product cost = 채택한 값`.
    """
    product = seeded["product"]
    approver = make_app_user(db, org_id=org, role="OPERATOR")
    db.commit()
    approver_token = create_access_token(user_id=approver, org_id=org, role="OPERATOR")

    lc = client.get(f"/api/v1/product-sources/{seeded['source_id']}/landed-cost",
                    headers=auth(tokens["SOURCING"])).json()["data"]
    new_cost = lc["landed_cost"]["total_krw"]

    created = client.post("/api/v1/approvals", json={
        "target_type": "product", "target_id": str(product),
        "request_type": "COST_CHANGE",
        "proposed_changes": {"cost_amount": new_cost},
        "reason": f"소싱 원가 반영 ({lc['supplier_name']})",
        "submit": True,
    }, headers=auth(tokens["SOURCING"]))
    assert created.status_code == 200, created.text
    request_id = created.json()["data"]["id"]

    # **요청만으로는 바뀌지 않는다.**
    assert db.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                      {"i": str(product)}).scalar() != new_cost

    approved = client.post(f"/api/v1/approvals/{request_id}/approve", json={},
                           headers=auth(approver_token))
    assert approved.status_code == 200, approved.text
    client.post(f"/api/v1/approvals/{request_id}/apply", json={},
                headers=auth(approver_token))

    db.expire_all()
    assert db.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                      {"i": str(product)}).scalar() == new_cost

    # 소싱 원가 이력은 보존된다.
    history = sc.cost_history(db, org_id=org,
                              product_source_id=uuid.UUID(seeded["source_id"]))
    assert history[-1]["purchase_price"] == 120


def test_cost_apply_does_not_rewrite_past_sales_margin(client, tokens, seeded, db, org):
    """상품 원가를 바꿔도 이미 팔린 건의 실마진은 그대로다."""
    product = seeded["product"]
    so = make_sales_order(db, org_id=org, product_id=product,
                          sale_date=date(2026, 3, 5), sale_price=50_000)
    make_product_margin_actual(db, org_id=org, product_id=product, sales_order_id=so,
                               actual_revenue=50_000, actual_cost=20_000)
    db.commit()
    before = dict(db.execute(text("""
        SELECT actual_cost, actual_margin_amount FROM product_margin_actual
         WHERE sales_order_id = :s
    """), {"s": str(so)}).mappings().one())

    db.execute(text("UPDATE product SET cost_amount = 31800 WHERE id = :i"),
               {"i": str(product)})
    db.commit()

    after = dict(db.execute(text("""
        SELECT actual_cost, actual_margin_amount FROM product_margin_actual
         WHERE sales_order_id = :s
    """), {"s": str(so)}).mappings().one())
    assert after == before


# ──────────────────────────────────────────────────────────────────────────
# 데이터 품질 (소싱)
#
# 전부 WARNING 이하다. 소싱 정보가 없어도 그 달의 손익은 계산된다 —
# 확정을 막으면 마감이 소싱 정리에 인질로 잡힌다.
# ──────────────────────────────────────────────────────────────────────────

def _codes(db: Session, org: uuid.UUID) -> dict[str, str]:
    from app.services import close_checks

    return {i.code: i.grade
            for i in close_checks.run_checks(db, org_id=org, year_month="2026-08")}


def test_sourcing_checks_never_block_the_close(db: Session, org: uuid.UUID):
    from app.services import close_checks

    actor = make_app_user(db, org_id=org, role="ADMIN")
    product = make_product(db, org_id=org)
    db.execute(text("UPDATE product SET status = 'ACTIVE' WHERE id = :i"),
               {"i": str(product)})
    sup = sc.create_supplier(db, org_id=org, actor_user_id=actor, name="공급처")
    s = sc.add_product_source(db, org_id=org, actor_user_id=actor, product_id=product,
                              supplier_site_id=uuid.UUID(sup["id"]), currency="CNY")
    sc.add_cost_version(db, org_id=org, actor_user_id=actor,
                        product_source_id=uuid.UUID(s["id"]),
                        purchase_price=100, currency="CNY", valid_from=date(2026, 1, 1))
    db.commit()

    sourcing = [i for i in close_checks.run_checks(db, org_id=org, year_month="2026-08")
                if i.domain == "SOURCING"]
    assert sourcing, "소싱 검사가 하나도 실행되지 않았다"
    assert not any(i.blocking for i in sourcing)


def test_missing_fx_is_flagged(db: Session, org: uuid.UUID):
    """환율이 없으면 원화 예상 원가를 계산할 수 없다 — 임의 환율로 채우지 않는다."""
    actor = make_app_user(db, org_id=org, role="ADMIN")
    product = make_product(db, org_id=org)
    sup = sc.create_supplier(db, org_id=org, actor_user_id=actor, name="1688")
    s = sc.add_product_source(db, org_id=org, actor_user_id=actor, product_id=product,
                              supplier_site_id=uuid.UUID(sup["id"]), currency="CNY")
    sc.add_cost_version(db, org_id=org, actor_user_id=actor,
                        product_source_id=uuid.UUID(s["id"]),
                        purchase_price=100, currency="CNY", valid_from=date(2026, 1, 1))
    db.commit()

    assert _codes(db, org).get("FX_REQUIRED_BUT_MISSING") == "WARNING"


def test_primary_source_without_cost_is_flagged(db: Session, org: uuid.UUID):
    actor = make_app_user(db, org_id=org, role="ADMIN")
    product = make_product(db, org_id=org)
    sup = sc.create_supplier(db, org_id=org, actor_user_id=actor, name="공급처")
    sc.add_product_source(db, org_id=org, actor_user_id=actor, product_id=product,
                          supplier_site_id=uuid.UUID(sup["id"]))
    db.commit()

    assert _codes(db, org).get("PRIMARY_SOURCE_MISSING_COST") == "WARNING"


def test_legacy_product_without_source_is_only_informational(db: Session, org: uuid.UUID):
    """M037 이전 상품은 전부 소싱처가 없다 — 자동 backfill 을 안 했기 때문이다.

    WARNING 으로 올리면 첫 스캔에 수백 건이 뜨고 사용자는 검증 전체를 무시한다.
    """
    product = make_product(db, org_id=org)
    db.execute(text("UPDATE product SET status = 'ACTIVE' WHERE id = :i"),
               {"i": str(product)})
    db.commit()

    assert _codes(db, org).get("PRODUCT_WITHOUT_SOURCE") == "INFO"
