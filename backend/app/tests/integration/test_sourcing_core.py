"""Sourcing Core (M037).

이 파일이 고정하는 것
------------------
1. 공급처 코드는 조직 안에서 유일하고, 이름은 중복 가능하다
2. 한 상품의 **ACTIVE primary 소싱처는 최대 하나** — DB 가 막는다
3. 원가는 덮어쓰지 않는다. version 이 쌓이고 과거 값이 남는다
4. 같은 기간에 원가가 둘일 수 없다 — DB 가 막는다
5. **환율이 없으면 계산 불가**다. 0 이나 임의값으로 채우지 않는다
6. 새 원가를 넣어도 **과거 판매의 실마진은 변하지 않는다**
7. 비밀번호는 어떤 경로로도 나가지 않는다

6번이 이 모듈 전체의 경계다
------------------------
SOURCE COST 는 "지금 다시 사면 얼마인가" 이고, 과거 판매에 귀속된 원가와 다르다.
섞이면 작년 순이익이 오늘 달라진다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.services import sourcing_core as sc
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
    make_product_margin_actual,
    make_sales_order,
)

DAY = date(2026, 8, 1)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


@pytest.fixture
def supplier(db: Session, org: uuid.UUID, actor: uuid.UUID) -> dict:
    return sc.create_supplier(
        db, org_id=org, actor_user_id=actor, name="도매꾹",
        website_url="https://www.domeggook.com/8618305?from=lstBiz",
    )


@pytest.fixture
def product(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_product(db, org_id=org)


# ──────────────────────────────────────────────────────────────────────────
# 공급처
# ──────────────────────────────────────────────────────────────────────────

def test_supplier_code_is_generated_and_stable(db: Session, org: uuid.UUID, actor):
    """사람이 이름을 바꿔도 관계가 깨지지 않아야 한다."""
    a = sc.create_supplier(db, org_id=org, actor_user_id=actor, name="도매꾹")
    b = sc.create_supplier(db, org_id=org, actor_user_id=actor, name="오너클랜")

    assert a["supplier_code"] == "SUP-000001"
    assert b["supplier_code"] == "SUP-000002"


def test_website_url_is_normalised_to_a_domain(db: Session, org: uuid.UUID, actor):
    """실제 Excel 의 "구매처" 는 URL 이다 — 같은 사이트가 둘로 갈라지면 안 된다."""
    a = sc.create_supplier(
        db, org_id=org, actor_user_id=actor, name="도매꾹 A",
        website_url="https://www.domeggook.com/8618305?from=lstBiz",
    )
    b = sc.create_supplier(
        db, org_id=org, actor_user_id=actor, name="도매꾹 B",
        website_url="domeggook.com/main/myBuy/order/my_orderList.php",
    )
    assert a["website_domain"] == "domeggook.com"
    assert b["website_domain"] == "domeggook.com"


def test_duplicate_names_are_allowed(db: Session, org: uuid.UUID, actor):
    """같은 이름의 다른 계정이 실제로 있다 — 이름으로 관계를 유지하지 않는다."""
    sc.create_supplier(db, org_id=org, actor_user_id=actor, name="도매꾹")
    second = sc.create_supplier(db, org_id=org, actor_user_id=actor, name="도매꾹")
    assert second["supplier_code"] != "SUP-000001"


def test_duplicate_supplier_code_is_rejected(db: Session, org: uuid.UUID, actor):
    sc.create_supplier(db, org_id=org, actor_user_id=actor,
                       name="A", supplier_code="SUP-000001")
    with pytest.raises(ConflictError):
        sc.create_supplier(db, org_id=org, actor_user_id=actor,
                           name="B", supplier_code="SUP-000001")


def test_supplier_never_exposes_a_password(db: Session, org: uuid.UUID, actor):
    """비밀번호 컬럼 자체가 없다. 계정이 있다는 사실만 남긴다."""
    s = sc.create_supplier(db, org_id=org, actor_user_id=actor,
                           name="도매꾹", credential_present=True)
    assert s["credential_present"] is True
    assert not any("password" in k or "login" in k for k in s)

    cols = [r[0] for r in db.execute(text("""
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'supplier_site'
    """))]
    assert not any("password" in c for c in cols), f"평문 비밀번호 컬럼이 있다: {cols}"


def test_suppliers_do_not_leak_across_orgs(db: Session, org: uuid.UUID, actor):
    other = make_organization(db)
    other_actor = make_app_user(db, org_id=other, role="ADMIN")
    foreign = sc.create_supplier(db, org_id=other, actor_user_id=other_actor, name="남의 공급처")

    assert sc.list_suppliers(db, org_id=org)["total"] == 0
    with pytest.raises(NotFoundError):
        sc.get_supplier(db, org_id=org, supplier_id=uuid.UUID(foreign["id"]))


def test_archived_supplier_keeps_history(db: Session, org: uuid.UUID, actor, supplier):
    """과거 소싱 기록이 어느 공급처였는지 남아야 한다."""
    sid = uuid.UUID(supplier["id"])
    sc.archive_supplier(db, org_id=org, actor_user_id=actor,
                        supplier_id=sid, reason="거래 종료")

    assert sc.list_suppliers(db, org_id=org)["total"] == 0
    row = db.execute(text("SELECT deleted_at FROM supplier_site WHERE id = :i"),
                     {"i": str(sid)}).scalar()
    assert row is not None


# ──────────────────────────────────────────────────────────────────────────
# 상품 소싱처
# ──────────────────────────────────────────────────────────────────────────

def test_first_source_becomes_primary(db: Session, org, actor, supplier, product):
    """하나뿐인데 주 소싱처가 없으면 예상 원가를 어디서 가져올지 정할 수 없다."""
    s = sc.add_product_source(
        db, org_id=org, actor_user_id=actor, product_id=product,
        supplier_site_id=uuid.UUID(supplier["id"]),
    )
    assert s["is_primary"] is True


def test_only_one_active_primary_source_per_product(
    db: Session, org, actor, supplier, product
):
    """DB 가 막는다 — 애플리케이션 검사만으로는 동시 요청에서 뚫린다."""
    other = sc.create_supplier(db, org_id=org, actor_user_id=actor, name="오너클랜")
    first = sc.add_product_source(
        db, org_id=org, actor_user_id=actor, product_id=product,
        supplier_site_id=uuid.UUID(supplier["id"]),
    )
    second = sc.add_product_source(
        db, org_id=org, actor_user_id=actor, product_id=product,
        supplier_site_id=uuid.UUID(other["id"]),
    )
    assert second["is_primary"] is False

    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE product_source SET is_primary = TRUE WHERE id = :i"),
                   {"i": second["id"]})
        db.flush()
    db.rollback()
    _ = first


def test_switching_primary_moves_it(db: Session, org, actor, supplier, product):
    other = sc.create_supplier(db, org_id=org, actor_user_id=actor, name="천유")
    first = sc.add_product_source(
        db, org_id=org, actor_user_id=actor, product_id=product,
        supplier_site_id=uuid.UUID(supplier["id"]),
    )
    second = sc.add_product_source(
        db, org_id=org, actor_user_id=actor, product_id=product,
        supplier_site_id=uuid.UUID(other["id"]),
    )

    sc.set_primary_source(db, org_id=org, actor_user_id=actor,
                          source_id=uuid.UUID(second["id"]))

    sources = {s["id"]: s for s in sc.list_product_sources(db, org_id=org, product_id=product)}
    assert sources[second["id"]]["is_primary"] is True
    assert sources[first["id"]]["is_primary"] is False


def test_same_supplier_cannot_be_linked_twice(db: Session, org, actor, supplier, product):
    sc.add_product_source(db, org_id=org, actor_user_id=actor, product_id=product,
                          supplier_site_id=uuid.UUID(supplier["id"]))
    with pytest.raises(ConflictError):
        sc.add_product_source(db, org_id=org, actor_user_id=actor, product_id=product,
                              supplier_site_id=uuid.UUID(supplier["id"]))


def test_linking_another_orgs_supplier_is_rejected(db: Session, org, actor, product):
    other = make_organization(db)
    other_actor = make_app_user(db, org_id=other, role="ADMIN")
    foreign = sc.create_supplier(db, org_id=other, actor_user_id=other_actor, name="남")

    with pytest.raises(NotFoundError):
        sc.add_product_source(db, org_id=org, actor_user_id=actor, product_id=product,
                              supplier_site_id=uuid.UUID(foreign["id"]))


# ──────────────────────────────────────────────────────────────────────────
# 원가 version
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture
def source(db: Session, org, actor, supplier, product) -> uuid.UUID:
    s = sc.add_product_source(
        db, org_id=org, actor_user_id=actor, product_id=product,
        supplier_site_id=uuid.UUID(supplier["id"]), currency="CNY",
    )
    return uuid.UUID(s["id"])


def test_new_cost_closes_the_previous_version(db: Session, org, actor, source):
    """원가를 덮어쓰지 않는다. 과거에 얼마였는지가 그때 판단의 근거다."""
    sc.add_cost_version(db, org_id=org, actor_user_id=actor, product_source_id=source,
                        purchase_price=120, currency="CNY", valid_from=date(2026, 1, 1))
    sc.add_cost_version(db, org_id=org, actor_user_id=actor, product_source_id=source,
                        purchase_price=130, currency="CNY", valid_from=date(2026, 8, 10))

    history = sc.cost_history(db, org_id=org, product_source_id=source)
    assert [h["purchase_price"] for h in history] == [130, 120]
    assert history[1]["valid_to"] == date(2026, 8, 10)   # 반열린 구간
    assert history[0]["valid_to"] is None


def test_cost_at_a_past_date_returns_the_old_value(db: Session, org, actor, source):
    sc.add_cost_version(db, org_id=org, actor_user_id=actor, product_source_id=source,
                        purchase_price=120, currency="CNY", valid_from=date(2026, 1, 1))
    sc.add_cost_version(db, org_id=org, actor_user_id=actor, product_source_id=source,
                        purchase_price=130, currency="CNY", valid_from=date(2026, 8, 10))

    before = sc.cost_version_at(db, org_id=org, product_source_id=source,
                                at=date(2026, 6, 1))
    after = sc.cost_version_at(db, org_id=org, product_source_id=source,
                               at=date(2026, 8, 10))
    assert before["purchase_price"] == 120
    assert after["purchase_price"] == 130


def test_overlapping_cost_periods_are_blocked_by_the_database(
    db: Session, org, actor, source
):
    """같은 기간에 원가가 둘이면 "그날 얼마였나" 에 답할 수 없다."""
    sc.add_cost_version(db, org_id=org, actor_user_id=actor, product_source_id=source,
                        purchase_price=120, currency="CNY", valid_from=date(2026, 1, 1))
    db.commit()

    with pytest.raises(IntegrityError):
        db.execute(text("""
            INSERT INTO product_source_cost_version
                   (org_id, product_source_id, purchase_price, currency, valid_from)
            VALUES (:o, :ps, 999, 'CNY', '2026-03-01')
        """), {"o": str(org), "ps": str(source)})
        db.flush()
    db.rollback()


def test_unknown_cost_component_is_rejected(db: Session, org, actor, source):
    with pytest.raises(ValidationError):
        sc.add_cost_version(db, org_id=org, actor_user_id=actor,
                            product_source_id=source, purchase_price=1,
                            currency="CNY", valid_from=DAY, made_up_fee=100)


# ──────────────────────────────────────────────────────────────────────────
# 환율
# ──────────────────────────────────────────────────────────────────────────

def test_latest_rate_respects_the_as_of_time(db: Session, org, actor):
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 8, 1, tzinfo=UTC)
    sc.record_fx_rate(db, org_id=org, actor_user_id=actor, base_currency="CNY",
                      quote_currency="KRW", rate="190", observed_at=t1)
    sc.record_fx_rate(db, org_id=org, actor_user_id=actor, base_currency="CNY",
                      quote_currency="KRW", rate="205", observed_at=t2)

    old = sc.latest_fx_rate(db, org_id=org, base_currency="CNY", quote_currency="KRW",
                            at=datetime(2026, 5, 1, tzinfo=UTC))
    now = sc.latest_fx_rate(db, org_id=org, base_currency="CNY", quote_currency="KRW")
    assert Decimal(str(old["rate"])) == Decimal("190")
    assert Decimal(str(now["rate"])) == Decimal("205")


def test_rate_types_do_not_mix(db: Session, org, actor):
    """소싱 검토 환율과 실제 결제 환율은 다르다. 섞으면 어느 시점인지 말할 수 없다."""
    sc.record_fx_rate(db, org_id=org, actor_user_id=actor, base_currency="CNY",
                      quote_currency="KRW", rate="205", rate_type="SOURCING")
    assert sc.latest_fx_rate(db, org_id=org, base_currency="CNY",
                             quote_currency="KRW", rate_type="PAYMENT") is None


def test_missing_rate_returns_none_not_a_guess(db: Session, org):
    """환율을 지어내면 마진이 통째로 틀리고 사용자는 그 숫자로 가격을 정한다."""
    assert sc.latest_fx_rate(db, org_id=org, base_currency="CNY",
                             quote_currency="KRW") is None


def test_same_currency_is_identity(db: Session, org):
    assert sc.latest_fx_rate(db, org_id=org, base_currency="KRW",
                             quote_currency="KRW")["rate"] == Decimal("1")


def test_non_positive_rate_is_rejected(db: Session, org, actor):
    with pytest.raises(ValidationError):
        sc.record_fx_rate(db, org_id=org, actor_user_id=actor, base_currency="CNY",
                          quote_currency="KRW", rate="0")


# ──────────────────────────────────────────────────────────────────────────
# 예상 landed cost
# ──────────────────────────────────────────────────────────────────────────

def test_landed_cost_converts_foreign_components_only(db: Session, org, actor, source):
    """매입가·현지배송비는 공급처 통화, 국제배송비·관세는 원화다."""
    sc.record_fx_rate(db, org_id=org, actor_user_id=actor, base_currency="CNY",
                      quote_currency="KRW", rate="205")
    sc.add_cost_version(
        db, org_id=org, actor_user_id=actor, product_source_id=source,
        purchase_price=120, currency="CNY", valid_from=date(2026, 1, 1),
        domestic_shipping=12, international_shipping=7_200,
        customs_duty=0, import_vat=3_000,
    )

    lc = sc.compute_landed_cost(db, org_id=org, product_source_id=source, at=DAY)

    assert lc.is_complete
    # (120 + 12) * 205 = 27,060  +  7,200 + 0 + 3,000 = 37,260
    assert lc.total_krw == 37_260
    by_key = {b["key"]: b for b in lc.breakdown}
    assert by_key["purchase_price"]["amount_krw"] == 24_600
    assert by_key["international_shipping"]["amount_krw"] == 7_200


def test_landed_cost_is_unavailable_without_a_rate(db: Session, org, actor, source):
    """0 으로 채우면 "원가 0원, 마진 100%" 가 나오고 화면에서는 그럴듯해 보인다."""
    sc.add_cost_version(db, org_id=org, actor_user_id=actor, product_source_id=source,
                        purchase_price=120, currency="CNY", valid_from=date(2026, 1, 1))

    lc = sc.compute_landed_cost(db, org_id=org, product_source_id=source, at=DAY)
    assert lc.total_krw is None
    assert lc.missing == ["FX_RATE:CNY/KRW"]
    assert lc.is_complete is False


def test_landed_cost_is_unavailable_without_a_cost_version(db: Session, org, source):
    lc = sc.compute_landed_cost(db, org_id=org, product_source_id=source, at=DAY)
    assert lc.missing == ["SOURCE_COST"]


def test_unprovided_components_are_zero_but_marked(db: Session, org, actor, supplier, product):
    """국내 소싱에 "국제배송비 없음" 으로 계산 불가를 띄우면 원가를 영영 못 본다.

    대신 입력하지 않은 항목을 breakdown 에 표시해 무엇이 0인지 보이게 한다.
    """
    s = sc.add_product_source(db, org_id=org, actor_user_id=actor, product_id=product,
                              supplier_site_id=uuid.UUID(supplier["id"]), currency="KRW")
    sc.add_cost_version(db, org_id=org, actor_user_id=actor,
                        product_source_id=uuid.UUID(s["id"]),
                        purchase_price=29_000, currency="KRW", valid_from=date(2026, 1, 1))

    lc = sc.compute_landed_cost(db, org_id=org, product_source_id=uuid.UUID(s["id"]), at=DAY)
    assert lc.total_krw == 29_000
    by_key = {b["key"]: b for b in lc.breakdown}
    assert by_key["purchase_price"]["provided"] is True
    assert by_key["international_shipping"]["provided"] is False
    assert by_key["international_shipping"]["amount_krw"] == 0


def test_fx_override_supports_sensitivity_scenarios(db: Session, org, actor, source):
    """환율이 오르면 마진이 얼마나 줄어드는가 — 같은 엔진으로 본다."""
    sc.record_fx_rate(db, org_id=org, actor_user_id=actor, base_currency="CNY",
                      quote_currency="KRW", rate="205")
    sc.add_cost_version(db, org_id=org, actor_user_id=actor, product_source_id=source,
                        purchase_price=100, currency="CNY", valid_from=date(2026, 1, 1))

    base = sc.compute_landed_cost(db, org_id=org, product_source_id=source, at=DAY)
    up = sc.compute_landed_cost(db, org_id=org, product_source_id=source, at=DAY,
                                fx_rate_override=Decimal("215"))

    assert base.total_krw == 20_500
    assert up.total_krw == 21_500
    assert up.fx["source_type"] == "OVERRIDE"


# ──────────────────────────────────────────────────────────────────────────
# 과거 보호 — 이 모듈 전체의 경계
# ──────────────────────────────────────────────────────────────────────────

def test_new_source_cost_does_not_touch_past_sales_margin(
    db: Session, org, actor, supplier, product, source
):
    """SOURCE COST 를 바꿔도 이미 팔린 건의 실마진은 그대로다.

    섞이면 작년 순이익이 오늘 달라진다.
    """
    so = make_sales_order(db, org_id=org, product_id=product,
                          sale_date=date(2026, 2, 10), sale_price=50_000)
    make_product_margin_actual(db, org_id=org, product_id=product, sales_order_id=so,
                               actual_revenue=50_000, actual_cost=20_000)
    db.commit()

    before = db.execute(text("""
        SELECT actual_cost, actual_margin_amount FROM product_margin_actual
         WHERE sales_order_id = :s
    """), {"s": str(so)}).mappings().one()

    sc.add_cost_version(db, org_id=org, actor_user_id=actor, product_source_id=source,
                        purchase_price=999_999, currency="KRW", valid_from=date(2026, 8, 1))

    after = db.execute(text("""
        SELECT actual_cost, actual_margin_amount FROM product_margin_actual
         WHERE sales_order_id = :s
    """), {"s": str(so)}).mappings().one()

    assert dict(after) == dict(before), "과거 판매의 실마진이 바뀌었다"


def test_source_cost_is_not_written_into_the_product_row(
    db: Session, org, actor, product, source
):
    """`product.cost_amount` 를 조용히 덮어쓰지 않는다.

    두 값의 책임이 아직 정리되지 않았다(docs/56 §6). 정리 전에 한쪽이 다른 쪽을
    덮어쓰기 시작하면 어느 것이 진실인지 알 수 없게 된다.
    """
    before = db.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                        {"i": str(product)}).scalar()

    sc.add_cost_version(db, org_id=org, actor_user_id=actor, product_source_id=source,
                        purchase_price=77_777, currency="KRW", valid_from=DAY)

    after = db.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                       {"i": str(product)}).scalar()
    assert after == before


def test_every_sourcing_write_is_audited(db: Session, org, actor, supplier, source):
    sc.record_fx_rate(db, org_id=org, actor_user_id=actor, base_currency="CNY",
                      quote_currency="KRW", rate="205")
    sc.add_cost_version(db, org_id=org, actor_user_id=actor, product_source_id=source,
                        purchase_price=120, currency="CNY", valid_from=DAY)

    tables = {r[0] for r in db.execute(text("""
        SELECT DISTINCT domain_table FROM audit_log WHERE org_id = :o
    """), {"o": str(org)})}
    assert {"supplier_site", "product_source",
            "product_source_cost_version", "fx_rate_snapshot"} <= tables


def test_cost_history_does_not_leak_across_orgs(db: Session, org, actor, source):
    other = make_organization(db)
    sc.add_cost_version(db, org_id=org, actor_user_id=actor, product_source_id=source,
                        purchase_price=120, currency="CNY", valid_from=DAY)

    assert sc.cost_history(db, org_id=other, product_source_id=source) == []
    assert sc.cost_version_at(db, org_id=other, product_source_id=source, at=DAY) is None
    _ = timedelta


# ──────────────────────────────────────────────────────────────────────────
# 대안 소싱처 비교
# ──────────────────────────────────────────────────────────────────────────

def test_comparison_never_ranks_or_recommends(db: Session, org, actor, supplier, product):
    """최저가를 추천으로 만들지 않는다.

    MOQ 를 감당할 현금, 리드타임, 품질 이력은 숫자에 안 담긴다. 하나로 줄을
    세우는 순간 그것들이 사라진다.
    """
    from app.services import sourcing_recommendation as rec

    second = sc.create_supplier(db, org_id=org, actor_user_id=actor, name="천유")
    a = sc.add_product_source(db, org_id=org, actor_user_id=actor, product_id=product,
                              supplier_site_id=uuid.UUID(supplier["id"]), currency="KRW",
                              moq=100, lead_time_days=7)
    b = sc.add_product_source(db, org_id=org, actor_user_id=actor, product_id=product,
                              supplier_site_id=uuid.UUID(second["id"]), currency="KRW",
                              moq=10, lead_time_days=2)
    sc.add_cost_version(db, org_id=org, actor_user_id=actor,
                        product_source_id=uuid.UUID(a["id"]),
                        purchase_price=3_000, currency="KRW", valid_from=DAY)
    sc.add_cost_version(db, org_id=org, actor_user_id=actor,
                        product_source_id=uuid.UUID(b["id"]),
                        purchase_price=4_500, currency="KRW", valid_from=DAY)

    out = rec.compare_sources(db, org_id=org, product_id=product, at=DAY)

    assert len(out["sources"]) == 2
    # 점수도 추천도 없다.
    body = repr(out)
    assert "recommended" not in body and "score" not in body and "rank" not in body
    # MOQ·리드타임처럼 판단에 필요한 것이 함께 온다.
    by_name = {s["supplier_name"]: s for s in out["sources"]}
    assert by_name["천유"]["moq"] == 10
    assert by_name["천유"]["lead_time_days"] == 2


def test_comparison_leaves_incalculable_rows_empty(db: Session, org, actor, supplier,
                                                   product):
    """환율이 없으면 빈칸이다. 0 으로 채우면 **가장 싼 곳**처럼 보인다."""
    from app.services import sourcing_recommendation as rec

    other = sc.create_supplier(db, org_id=org, actor_user_id=actor, name="1688")
    priced = sc.add_product_source(db, org_id=org, actor_user_id=actor,
                                   product_id=product,
                                   supplier_site_id=uuid.UUID(supplier["id"]),
                                   currency="KRW")
    unknown = sc.add_product_source(db, org_id=org, actor_user_id=actor,
                                    product_id=product,
                                    supplier_site_id=uuid.UUID(other["id"]),
                                    currency="CNY")
    sc.add_cost_version(db, org_id=org, actor_user_id=actor,
                        product_source_id=uuid.UUID(priced["id"]),
                        purchase_price=3_000, currency="KRW", valid_from=DAY)
    sc.add_cost_version(db, org_id=org, actor_user_id=actor,
                        product_source_id=uuid.UUID(unknown["id"]),
                        purchase_price=120, currency="CNY", valid_from=DAY)

    out = rec.compare_sources(db, org_id=org, product_id=product, at=DAY)
    rows = {s["supplier_name"]: s for s in out["sources"]}

    assert rows["1688"]["landed_cost"]["total_krw"] is None
    assert rows["1688"]["landed_cost"]["missing"] == ["FX_RATE:CNY/KRW"]
    assert rows["1688"]["difference"] is None
    assert out["comparable_count"] == 1


def test_comparison_shows_applied_cost_separately(db: Session, org, actor, supplier,
                                                  product):
    """소싱 원가와 **지금 계산에 쓰이는 원가**는 다른 것이다."""
    from app.services import sourcing_recommendation as rec

    src = sc.add_product_source(db, org_id=org, actor_user_id=actor, product_id=product,
                                supplier_site_id=uuid.UUID(supplier["id"]),
                                currency="KRW")
    sc.add_cost_version(db, org_id=org, actor_user_id=actor,
                        product_source_id=uuid.UUID(src["id"]),
                        purchase_price=3_000, currency="KRW", valid_from=DAY)
    db.execute(text("UPDATE product SET cost_amount = 2500 WHERE id = :i"),
               {"i": str(product)})

    out = rec.compare_sources(db, org_id=org, product_id=product, at=DAY)

    assert out["current_cost_amount"] == 2_500
    assert out["sources"][0]["difference"] == 500
    assert out["sources"][0]["difference_rate"] == pytest.approx(0.2)
    # 비교를 봤다고 상품 원가가 바뀌지 않는다 — COST_CHANGE 승인이 필요하다.
    assert db.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                      {"i": str(product)}).scalar() == 2_500
