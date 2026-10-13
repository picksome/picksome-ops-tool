"""상품↔공급처 연결 대량 가져오기.

이 파일이 고정하는 것
------------------
1. 이름으로 **연결하지 않는다** — 상품도 공급처도
2. 재적용해도 연결이 두 번 생기지 않는다
3. 동시 실행에서도 DB 제약이 막는다
4. 이미 있는 연결은 **비어 있는 것만** 채운다
5. 리드타임·MOQ 가 없으면 `NULL` 이다 — 임의값을 만들지 않는다
6. 매입가는 새 버전으로 쌓이고 `product.cost_amount` 는 그대로다
7. 주 소싱처를 말없이 바꾸지 않는다
8. ID/비밀번호 열은 읽지 않는다
9. 공급처 사이트 URL 과 상품 URL 을 섞지 않는다

1·6·7 이 핵심이다
---------------
잘못 연결하면 틀린 공급처의 원가로 발주가 나간다. 파일 한 번으로 확정 원가가
바뀌면 과거 마진이 흔들린다. 주 소싱처가 말없이 바뀌면 발주가 다른 곳으로 간다.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import product_source_import as psi
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
)

HEADER = ("SKU", "공급처코드", "공급처URL", "리드타임", "MOQ", "주소싱처", "매입가")


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org)


def _product(db, org, sku: str, name: str = "소싱상품", cost: int = 10_000):
    pid = make_product(db, org_id=org, name_ko=name, cost_amount=cost)
    db.execute(text("UPDATE product SET sku = :s WHERE id = :i"),
               {"s": sku, "i": str(pid)})
    db.commit()
    return pid


def _supplier(db, org, code: str, domain: str | None = None, name: str = "공급처"):
    sid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO supplier_site (id, org_id, supplier_code, name,
                                   country_code, website_domain)
        VALUES (:id, :o, :c, :n, 'KR', :d)
    """), {"id": str(sid), "o": str(org), "c": code, "n": name, "d": domain})
    db.commit()
    return sid


def _plan(db, org, rows, mode="DRY_RUN"):
    return psi.plan(db, org_id=org, rows=rows, mode=mode)


def _apply(db, org, actor, plan):
    return psi.apply_plan(db, org_id=org, actor_user_id=actor,
                          plan_result=plan, source_name="소싱.csv")


def _sources(db, org, pid):
    return db.execute(text("""
        SELECT supplier_site_id, source_url, lead_time_days, moq, is_primary
          FROM product_source
         WHERE org_id = :o AND product_id = :p AND deleted_at IS NULL
    """), {"o": str(org), "p": str(pid)}).mappings().all()


# ── 1. 연결 생성 ───────────────────────────────────────────────


def test_new_mapping(db: Session, org, actor):
    pid = _product(db, org, "SK-1")
    _supplier(db, org, "SUP-1")

    plan = _plan(db, org, [HEADER,
                           ("SK-1", "SUP-1", "https://x.com/p/1", 7, 10, "Y", 3000)])
    assert plan.counts()["NEW_MAPPING"] == 1
    _apply(db, org, actor, plan)

    rows = _sources(db, org, pid)
    assert len(rows) == 1
    assert rows[0]["lead_time_days"] == 7
    assert rows[0]["moq"] == 10
    assert rows[0]["is_primary"] is True
    assert rows[0]["source_url"] == "https://x.com/p/1"


def test_supplier_found_by_domain(db: Session, org, actor):
    """도메인은 정규화 후 **완전일치**다."""
    pid = _product(db, org, "SK-2")
    _supplier(db, org, "SUP-2", domain="domeggook.com")

    plan = _plan(db, org, [("SKU", "공급처도메인", "수량없음"),
                           ("SK-2", "https://www.domeggook.com/item/9", None)])
    assert plan.rows[0].outcome == "NEW_MAPPING"
    _apply(db, org, actor, plan)
    assert len(_sources(db, org, pid)) == 1


def test_similar_domain_is_not_matched(db: Session, org, actor):
    """`domeggook.co.kr` 은 `domeggook.com` 이 아니다."""
    _product(db, org, "SK-3")
    _supplier(db, org, "SUP-3", domain="domeggook.com")

    plan = _plan(db, org, [("SKU", "공급처도메인"), ("SK-3", "domeggook.co.kr")])
    assert plan.rows[0].outcome == "UNMATCHED_SUPPLIER"


def test_mobile_subdomain_is_not_merged(db: Session, org):
    """`m.` 을 벗기지 않는다 — 별도 공급처로 등록된 경우가 실제로 있다."""
    assert psi.normalize_host("m.petbtob.co.kr") == "m.petbtob.co.kr"
    assert psi.normalize_host("https://www.petbtob.co.kr") == "petbtob.co.kr"


def test_names_are_not_usable_keys(db: Session, org):
    """상품명·공급처명 열만 있는 파일은 거부한다."""
    plan = _plan(db, org, [("상품명", "공급처명"), ("파란 티셔츠", "도매꾹")])
    assert plan.file_problems
    assert any("상품을 찾을 열이 없습니다" in p for p in plan.file_problems)


def test_unmatched_product_creates_nothing(db: Session, org, actor):
    _supplier(db, org, "SUP-4")
    plan = _plan(db, org, [HEADER, ("없는SKU", "SUP-4", None, None, None, None, None)])
    assert plan.rows[0].outcome == "UNMATCHED_PRODUCT"
    _apply(db, org, actor, plan)
    assert db.execute(text(
        "SELECT COUNT(*) FROM product_source WHERE org_id = :o"),
        {"o": str(org)}).scalar_one() == 0


# ── 2. 재적용 ──────────────────────────────────────────────────


def test_reapply_creates_no_duplicate(db: Session, org, actor):
    pid = _product(db, org, "SK-5")
    _supplier(db, org, "SUP-5")
    rows = [HEADER, ("SK-5", "SUP-5", "https://x.com/5", 5, 1, "Y", 1000)]

    first = _apply(db, org, actor, _plan(db, org, rows))
    assert first["created"] == 1

    second_plan = _plan(db, org, rows)
    # 이미 있고 채울 것이 없다. 매입가는 같은 날 같은 값이라 새 버전도 없다.
    assert second_plan.rows[0].outcome == "UPDATE_METADATA"
    second = _apply(db, org, actor, second_plan)

    assert second["created"] == 0
    assert second["cost_versions"] == 0
    assert len(_sources(db, org, pid)) == 1


def test_db_constraint_is_the_final_defence(db: Session, org, actor):
    """계획을 세운 뒤 다른 쪽이 먼저 만들어도 두 줄이 되지 않는다.

    check-then-insert 만 믿으면 동시 실행에서 둘 다 통과한다.
    """
    pid = _product(db, org, "SK-6")
    sid = _supplier(db, org, "SUP-6")
    rows = [HEADER, ("SK-6", "SUP-6", None, None, None, None, None)]

    plan = _plan(db, org, rows)
    assert plan.rows[0].outcome == "NEW_MAPPING"

    # 계획과 반영 **사이에** 다른 경로가 같은 연결을 만든다.
    db.execute(text("""
        INSERT INTO product_source (org_id, product_id, supplier_site_id,
                                    status, currency)
        VALUES (:o, :p, :s, 'ACTIVE', 'KRW')
    """), {"o": str(org), "p": str(pid), "s": str(sid)})
    db.commit()

    result = _apply(db, org, actor, plan)
    assert result["created"] == 0
    assert len(_sources(db, org, pid)) == 1


# ── 3. 기존 연결 갱신 ──────────────────────────────────────────


def test_only_empty_fields_are_filled(db: Session, org, actor):
    """사람이 고친 값을 파일이 덮지 않는다."""
    pid = _product(db, org, "SK-7")
    sid = _supplier(db, org, "SUP-7")
    db.execute(text("""
        INSERT INTO product_source (org_id, product_id, supplier_site_id,
                                    lead_time_days, status, currency)
        VALUES (:o, :p, :s, 3, 'ACTIVE', 'KRW')
    """), {"o": str(org), "p": str(pid), "s": str(sid)})
    db.commit()

    # 파일에는 리드타임 99, MOQ 20 이 있다.
    plan = _plan(db, org, [HEADER,
                           ("SK-7", "SUP-7", None, 99, 20, None, None)])
    assert plan.rows[0].changes == {"moq": 20}   # 리드타임은 안 건드린다
    _apply(db, org, actor, plan)

    row = _sources(db, org, pid)[0]
    assert row["lead_time_days"] == 3   # 99 로 덮이지 않았다
    assert row["moq"] == 20


# ── 4. 없는 값 ─────────────────────────────────────────────────


def test_missing_lead_time_and_moq_stay_null(db: Session, org, actor):
    """없으면 `NULL` 이다. 10일·1개 같은 값을 만들지 않는다."""
    pid = _product(db, org, "SK-8")
    _supplier(db, org, "SUP-8")

    _apply(db, org, actor, _plan(db, org, [HEADER,
                                           ("SK-8", "SUP-8", None, None, None, None, None)]))
    row = _sources(db, org, pid)[0]
    assert row["lead_time_days"] is None
    assert row["moq"] is None


@pytest.mark.parametrize("value", ["abc", "7.5", "-1"])
def test_invalid_lead_time_is_rejected(db: Session, org, value):
    _product(db, org, "SK-9")
    _supplier(db, org, "SUP-9")
    plan = _plan(db, org, [HEADER, ("SK-9", "SUP-9", None, value, None, None, None)])
    assert plan.rows[0].outcome == "INVALID"


# ── 5. 원가 ────────────────────────────────────────────────────


def test_cost_becomes_version_not_product_cost(db: Session, org, actor):
    """`product.cost_amount` 는 **그대로다.** 새 버전만 쌓인다."""
    pid = _product(db, org, "SK-10", cost=10_000)
    _supplier(db, org, "SUP-10")

    result = _apply(db, org, actor, _plan(
        db, org, [HEADER, ("SK-10", "SUP-10", None, None, None, None, 7_500)]))
    assert result["cost_versions"] == 1

    # 확정 원가는 안 바뀌었다.
    assert db.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                      {"i": str(pid)}).scalar_one() == 10_000

    version = db.execute(text("""
        SELECT purchase_price, valid_from FROM product_source_cost_version c
          JOIN product_source s ON s.id = c.product_source_id
         WHERE s.product_id = :p
    """), {"p": str(pid)}).mappings().one()
    assert version["purchase_price"] == 7_500
    assert version["valid_from"] == date.today()


def test_same_price_same_day_creates_no_second_version(db: Session, org, actor):
    _product(db, org, "SK-11")
    _supplier(db, org, "SUP-11")
    rows = [HEADER, ("SK-11", "SUP-11", None, None, None, None, 5_000)]

    assert _apply(db, org, actor, _plan(db, org, rows))["cost_versions"] == 1
    assert _apply(db, org, actor, _plan(db, org, rows))["cost_versions"] == 0


# ── 6. 주 소싱처 ───────────────────────────────────────────────


def test_primary_conflict_is_reported_not_overwritten(db: Session, org, actor):
    """주 소싱처를 말없이 바꾸면 발주가 다른 곳으로 나간다."""
    pid = _product(db, org, "SK-12")
    first = _supplier(db, org, "SUP-12A")
    _supplier(db, org, "SUP-12B")
    db.execute(text("""
        INSERT INTO product_source (org_id, product_id, supplier_site_id,
                                    is_primary, status, currency)
        VALUES (:o, :p, :s, TRUE, 'ACTIVE', 'KRW')
    """), {"o": str(org), "p": str(pid), "s": str(first)})
    db.commit()

    plan = _plan(db, org, [HEADER,
                           ("SK-12", "SUP-12B", None, None, None, "Y", None)])
    assert plan.rows[0].outcome == "CONFLICT_PRIMARY"
    _apply(db, org, actor, plan)

    primaries = [r for r in _sources(db, org, pid) if r["is_primary"]]
    assert len(primaries) == 1
    assert str(primaries[0]["supplier_site_id"]) == str(first)


def test_primary_not_set_when_not_marked(db: Session, org, actor):
    """단일 소싱처라고 근거 없이 주 소싱처로 만들지 않는다.

    파일이 표시하지 않았으면 사람이 정할 일이다.
    """
    pid = _product(db, org, "SK-13")
    _supplier(db, org, "SUP-13")
    _apply(db, org, actor, _plan(
        db, org, [HEADER, ("SK-13", "SUP-13", None, 5, None, None, None)]))
    assert _sources(db, org, pid)[0]["is_primary"] is False


# ── 7. 비밀 ────────────────────────────────────────────────────


def test_credential_columns_are_skipped(db: Session, org):
    """ID/PW 열은 읽지 않고 개수만 센다."""
    _product(db, org, "SK-14")
    _supplier(db, org, "SUP-14")
    plan = _plan(db, org, [("SKU", "공급처코드", "ID", "PW"),
                           ("SK-14", "SUP-14", "picksome", "secret!")])
    assert plan.credential_headers_skipped == 2
    assert plan.as_dict()["credential_detected"] is True
    # 값이 어디에도 남지 않았다.
    assert "secret!" not in str(plan.as_dict())


# ── 8. 완성도 ──────────────────────────────────────────────────


def test_completeness_counts(db: Session, org, actor):
    pid = _product(db, org, "SK-15")
    _product(db, org, "SK-16")          # 연결 없음
    _supplier(db, org, "SUP-15")
    _apply(db, org, actor, _plan(
        db, org, [HEADER, ("SK-15", "SUP-15", None, None, None, None, None)]))

    c = psi.completeness(db, org_id=org)
    assert c["products"] == 2
    assert c["with_source"] == 1
    assert c["without_source"] == 1
    assert c["without_lead_time"] == 1   # 연결은 있지만 리드타임 없음
    assert c["without_primary"] == 1
    assert c["sources_without_cost"] == 1
    _ = pid


def test_analyze_does_not_touch_db(db: Session, org):
    _product(db, org, "SK-17")
    _supplier(db, org, "SUP-17")
    plan = _plan(db, org, [HEADER, ("SK-17", "SUP-17", None, 3, None, None, None)],
                 mode="ANALYZE")
    assert plan.rows[0].product_id is None
    assert db.execute(text(
        "SELECT COUNT(*) FROM product_source WHERE org_id = :o"),
        {"o": str(org)}).scalar_one() == 0
