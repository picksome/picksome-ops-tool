"""NEXT ENGINE 주문 해석 · Shadow 대사 (Phase 1B).

이 파일이 고정하는 것
------------------
1. 판매처 계정은 **명시적 매핑**으로만 붙는다 — 이름이 같다고 붙지 않는다
2. 상품은 정확 일치로만 붙고, 후보가 둘이면 **고르지 않는다**
3. 옵션 코드가 없는데 우리 상품이 다옵션이면 **거부한다**
4. 조직을 넘지 않는다
5. Shadow 는 아무것도 쓰지 않는다

3 이 이 파일의 핵심이다
--------------------
다옵션 상품에서 옵션을 모른 채 하나를 고르면 재고와 손익이 동시에 틀어지고,
어느 옵션이 틀렸는지도 남지 않는다.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.connectors.next_engine.normalize import normalize_order_line
from app.services import next_engine_order_sync as nes
from app.tests.fixtures.factories import (
    make_app_user,
    make_integration_account,
    make_organization,
    make_product,
)

ROW = {
    "ne_order_no": "NE-1", "mall_order_no": "MALL-1",
    "mall_cd": "mall02400", "mall_name": "쿠팡", "mall_id": "shop-1",
    "ne_product_cd": "NE-P-1", "ne_partner_product_cd": "SKU-1",
    "ne_stock_cd": "", "product_name": "테스트상품",
    "order_date": "2026-08-01 10:00:00", "pay_date": "2026-08-01 10:05:00",
    "order_status_divi": "04", "order_price": 14000, "order_cnt": 1,
    "sell_price": 12000, "delivery_price": 0,
}


def _line(**over):
    return normalize_order_line(dict(ROW, **over))


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


@pytest.fixture
def ne_account(db: Session, org, actor) -> uuid.UUID:
    """NEXT ENGINE 외부 계정(셀러툴)."""
    sys_id = db.execute(text("""
        INSERT INTO external_system (code, name, system_type, supports_api_key)
        VALUES ('NEXT_ENGINE_T', '테스트 NE', 'SELLER_TOOL', TRUE)
        ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id
    """)).scalar()
    return uuid.UUID(str(db.execute(text("""
        INSERT INTO external_account (org_id, external_system_id, account_code,
               display_name, created_by, updated_by)
        VALUES (:o, :s, 'DEFAULT', 'NE', :a, :a) RETURNING id
    """), {"o": str(org), "s": str(sys_id), "a": str(actor)}).scalar()))


@pytest.fixture
def market_account(db: Session, org, actor) -> uuid.UUID:
    """쿠팡 판매자 계정(`integration_account`)."""
    return make_integration_account(db, org_id=org, provider="COUPANG",
                                    account_label="쿠팡 본점")


def _map_account(db, org, ne_account, market_account, mall_cd="mall02400"):
    """`mall_cd` → 판매처 계정 매핑.

    M052 로 `MARKETPLACE_ACCOUNT` 유형이 열렸다. 더 이상 제약을 풀 필요가 없고,
    external/internal 양쪽에 **같은 토큰**을 쓴다(`ck_eem_type_symmetry`).
    """
    db.execute(text("""
        INSERT INTO external_entity_mapping
               (org_id, external_account_id, external_entity_type, external_entity_id,
                internal_entity_type, internal_entity_id, mapping_status, first_seen_at)
        VALUES (:o, :a, :et, :eid, :et, :iid, 'ACTIVE', now())
    """), {"o": str(org), "a": str(ne_account), "et": nes.ENTITY_MARKETPLACE_ACCOUNT,
           "eid": mall_cd, "iid": str(market_account)})


# ── 판매처 계정 ──────────────────────────────────────────────────────────

def test_unmapped_store_is_rejected_not_guessed(db, org, ne_account, market_account):
    """쿠팡 계정이 **있어도** 매핑이 없으면 붙이지 않는다.

    "계정이 하나뿐이니 그거겠지" 가 가장 위험하다 — 2호점이 생기는 날 과거
    주문까지 전부 잘못된 계정에 귀속된다.
    """
    resolved, summary = nes.resolve_lines(
        db, org_id=org, external_account_id=ne_account, lines=[_line()])

    assert resolved[0].status == nes.UNMAPPED_MARKETPLACE_ACCOUNT
    assert resolved[0].integration_account_id is None
    assert summary.unmapped_malls == {"mall02400": "쿠팡"}


def test_mapped_store_resolves_to_the_exact_account(
        db, org, actor, ne_account, market_account):
    _map_account(db, org, ne_account, market_account)
    make_product(db, org_id=org, business_key="SKU-1")

    resolved, _ = nes.resolve_lines(
        db, org_id=org, external_account_id=ne_account, lines=[_line()],
        actor_user_id=actor)

    assert resolved[0].integration_account_id == market_account


def test_second_store_of_same_marketplace_is_distinguished(
        db, org, actor, ne_account, market_account):
    """같은 마켓의 2호점. `mall_cd` 가 다르면 다른 계정이어야 한다."""
    second = make_integration_account(db, org_id=org, provider="COUPANG",
                                      account_label="쿠팡 2호점")
    _map_account(db, org, ne_account, market_account, "mall02400")
    db.execute(text("""
        INSERT INTO external_entity_mapping
               (org_id, external_account_id, external_entity_type, external_entity_id,
                internal_entity_type, internal_entity_id, mapping_status, first_seen_at)
        VALUES (:o, :a, :et, 'mall09999', :et, :iid, 'ACTIVE', now())
    """), {"o": str(org), "a": str(ne_account), "et": nes.ENTITY_MARKETPLACE_ACCOUNT,
           "iid": str(second)})
    make_product(db, org_id=org, business_key="SKU-1")

    lines = [_line(), _line(ne_order_no="NE-2", mall_cd="mall09999")]
    resolved, _ = nes.resolve_lines(
        db, org_id=org, external_account_id=ne_account, lines=lines,
        actor_user_id=actor)

    assert resolved[0].integration_account_id == market_account
    assert resolved[1].integration_account_id == second
    assert resolved[0].integration_account_id != resolved[1].integration_account_id


def test_account_mapping_cannot_cross_org(db, org, actor, ne_account, market_account):
    """다른 조직의 판매처 계정을 가리키는 매핑은 무시한다."""
    other_org = make_organization(db)
    foreign = make_integration_account(db, org_id=other_org, provider="COUPANG",
                                       account_label="남의 계정")
    _map_account(db, org, ne_account, foreign)

    resolved, _ = nes.resolve_lines(
        db, org_id=org, external_account_id=ne_account, lines=[_line()])

    assert resolved[0].status == nes.UNMAPPED_MARKETPLACE_ACCOUNT


# ── 상품 ────────────────────────────────────────────────────────────────

def test_bootstrap_maps_on_exact_sku_and_remembers_it(
        db, org, actor, ne_account, market_account):
    """정확히 1건일 때만 잇고, 이은 뒤에는 기록이 진실이 된다."""
    _map_account(db, org, ne_account, market_account)
    pid = make_product(db, org_id=org, business_key="SKU-1")

    resolved, summary = nes.resolve_lines(
        db, org_id=org, external_account_id=ne_account, lines=[_line()],
        actor_user_id=actor)

    assert resolved[0].status == nes.RESOLVED
    assert resolved[0].product_id == pid
    assert summary.product_mappings_created == 1

    stored = db.execute(text("""
        SELECT internal_entity_id FROM external_entity_mapping
         WHERE org_id = :o AND external_entity_type = 'PRODUCT'
           AND external_entity_id = 'NE-P-1' AND deleted_at IS NULL
    """), {"o": str(org)}).scalar()
    assert uuid.UUID(str(stored)) == pid


def test_ambiguous_sku_is_never_auto_chosen(db, org, actor, ne_account, market_account):
    """후보가 둘이면 고르지 않는다. 고르면 절반이 틀린다.

    실제로 일어나는 모양은 이렇다 — 같은 코드가 한 상품의 `business_key` 이면서
    다른 상품의 `sku` 다. `business_key` 는 조직 안에서 유일하지만 `sku` 는
    아니라, 두 축을 함께 보는 순간 겹칠 수 있다.
    """
    _map_account(db, org, ne_account, market_account)
    make_product(db, org_id=org, business_key="SKU-1")
    other = make_product(db, org_id=org, business_key="OTHER-KEY")
    db.execute(text("UPDATE product SET sku = 'SKU-1' WHERE id = :i"), {"i": str(other)})

    resolved, summary = nes.resolve_lines(
        db, org_id=org, external_account_id=ne_account, lines=[_line()],
        actor_user_id=actor)

    assert resolved[0].status == nes.AMBIGUOUS_PRODUCT
    assert resolved[0].product_id is None
    assert summary.product_mappings_created == 0


def test_unknown_sku_is_unmapped(db, org, actor, ne_account, market_account):
    _map_account(db, org, ne_account, market_account)
    make_product(db, org_id=org, business_key="DIFFERENT")

    resolved, summary = nes.resolve_lines(
        db, org_id=org, external_account_id=ne_account, lines=[_line()],
        actor_user_id=actor)

    assert resolved[0].status == nes.UNMAPPED_PRODUCT
    assert "NE-P-1" in summary.unmapped_products


def test_product_name_never_matches(db, org, actor, ne_account, market_account):
    """상품명이 같아도 코드가 다르면 잇지 않는다."""
    _map_account(db, org, ne_account, market_account)
    make_product(db, org_id=org, business_key="OTHER-SKU", name_ko="테스트상품")

    resolved, _ = nes.resolve_lines(
        db, org_id=org, external_account_id=ne_account, lines=[_line()],
        actor_user_id=actor)

    assert resolved[0].status == nes.UNMAPPED_PRODUCT


def test_product_bootstrap_cannot_cross_org(db, org, actor, ne_account, market_account):
    _map_account(db, org, ne_account, market_account)
    other = make_organization(db)
    make_product(db, org_id=other, business_key="SKU-1")

    resolved, _ = nes.resolve_lines(
        db, org_id=org, external_account_id=ne_account, lines=[_line()],
        actor_user_id=actor)

    assert resolved[0].status == nes.UNMAPPED_PRODUCT


# ── 옵션 안전규칙 ────────────────────────────────────────────────────────

def _add_variant(db, org, product_id, key, external=None):
    return uuid.UUID(str(db.execute(text("""
        INSERT INTO product_variant (org_id, product_id, variant_key,
               option_values_json, sku_external, is_active)
        VALUES (:o, :p, :k, '{}'::jsonb, :e, TRUE) RETURNING id
    """), {"o": str(org), "p": str(product_id), "k": key, "e": external}).scalar()))


def test_case_b_blank_option_with_single_product_is_safe(
        db, org, actor, ne_account, market_account):
    """옵션이 없으면 고를 것이 없다 — 상품 단위로 안전하다."""
    _map_account(db, org, ne_account, market_account)
    make_product(db, org_id=org, business_key="SKU-1")

    resolved, _ = nes.resolve_lines(
        db, org_id=org, external_account_id=ne_account, lines=[_line()],
        actor_user_id=actor)

    assert resolved[0].status == nes.RESOLVED
    assert resolved[0].product_variant_id is None


def test_case_c_blank_option_with_multi_option_product_is_rejected(
        db, org, actor, ne_account, market_account):
    """**이 파일의 핵심.** 모르면 고르지 않는다."""
    _map_account(db, org, ne_account, market_account)
    pid = make_product(db, org_id=org, business_key="SKU-1")
    _add_variant(db, org, pid, "RED")
    _add_variant(db, org, pid, "BLUE")

    resolved, summary = nes.resolve_lines(
        db, org_id=org, external_account_id=ne_account, lines=[_line()],
        actor_user_id=actor)

    assert resolved[0].status == nes.UNMAPPED_OPTION
    assert resolved[0].product_variant_id is None
    assert summary.counts[nes.UNMAPPED_OPTION] == 1


def test_case_a_option_code_maps_exactly(db, org, actor, ne_account, market_account):
    """옵션 코드가 오면 그것으로 정확히 잇는다."""
    _map_account(db, org, ne_account, market_account)
    pid = make_product(db, org_id=org, business_key="SKU-1")
    red = _add_variant(db, org, pid, "RED", external="NE-STOCK-RED")
    _add_variant(db, org, pid, "BLUE", external="NE-STOCK-BLUE")

    resolved, _ = nes.resolve_lines(
        db, org_id=org, external_account_id=ne_account,
        lines=[_line(ne_stock_cd="NE-STOCK-RED")], actor_user_id=actor)

    assert resolved[0].status == nes.RESOLVED
    assert resolved[0].product_variant_id == red


def test_unknown_option_code_is_rejected(db, org, actor, ne_account, market_account):
    _map_account(db, org, ne_account, market_account)
    pid = make_product(db, org_id=org, business_key="SKU-1")
    _add_variant(db, org, pid, "RED", external="NE-STOCK-RED")

    resolved, _ = nes.resolve_lines(
        db, org_id=org, external_account_id=ne_account,
        lines=[_line(ne_stock_cd="NE-STOCK-GREEN")], actor_user_id=actor)

    assert resolved[0].status == nes.UNMAPPED_OPTION


def test_inactive_variant_still_counts_as_multi_option(
        db, org, actor, ne_account, market_account):
    """비활성 옵션을 빼고 세면 다옵션 상품이 단일로 보인다."""
    _map_account(db, org, ne_account, market_account)
    pid = make_product(db, org_id=org, business_key="SKU-1")
    _add_variant(db, org, pid, "RED")
    vid = _add_variant(db, org, pid, "BLUE")
    db.execute(text("UPDATE product_variant SET is_active = FALSE WHERE id = :i"),
               {"i": str(vid)})

    resolved, _ = nes.resolve_lines(
        db, org_id=org, external_account_id=ne_account, lines=[_line()],
        actor_user_id=actor)

    assert resolved[0].status == nes.UNMAPPED_OPTION


# ── Shadow ──────────────────────────────────────────────────────────────

def test_shadow_writes_nothing(db, org):
    """대사는 읽기다."""
    before = db.execute(text("SELECT COUNT(*) FROM sales_order WHERE org_id = :o"),
                        {"o": str(org)}).scalar()
    nes.shadow_compare(db, org_id=org, lines=[_line()])
    after = db.execute(text("SELECT COUNT(*) FROM sales_order WHERE org_id = :o"),
                       {"o": str(org)}).scalar()
    assert before == after


def test_shadow_counts_new_candidates(db, org):
    report = nes.shadow_compare(db, org_id=org, lines=[_line(), _line(ne_order_no="NE-2",
                                                                     mall_order_no="MALL-2")])
    assert report.fetched == 2
    assert report.new_candidate == 2
    assert report.exact_existing_match == 0


def test_marketplace_is_not_compared_by_name(db, org, actor):
    """우리는 `COUPANG`, 상대는 `쿠팡` 이다. 문자열로 맞대면 전부 불일치가 된다.

    처음에 그렇게 만들어 실제 대사에서 14건 전부를 거짓 불일치로 셌다.
    계정 매핑이 없으면 **비교 불가**로 세는 것이 정직하다.
    """
    db.execute(text("""
        INSERT INTO sales_order (org_id, business_key, sale_date, marketplace,
               marketplace_order_no, quantity, sale_price, ad_cost, shipping_fee,
               vat_inclusion_type, import_source, line_no, profit_status,
               created_by, updated_by)
        VALUES (:o, 'BK-M', '2026-08-01', 'COUPANG', 'MALL-1', 1, 14000, 0, 0,
                'INCLUDED', 'EXCEL_IMPORT', 1, 'EXPECTED', :a, :a)
    """), {"o": str(org), "a": str(actor)})

    report = nes.shadow_compare(db, org_id=org, lines=[_line()])
    assert report.exact_existing_match == 1
    assert report.marketplace_mismatch == 0
    assert report.marketplace_not_comparable == 1


def test_marketplace_mismatch_needs_an_account_mapping(db, org, actor, market_account):
    """매핑이 있으면 그때는 진짜로 비교한다."""
    other = make_integration_account(db, org_id=org, provider="COUPANG",
                                     account_label="다른 계정")
    db.execute(text("""
        INSERT INTO sales_order (org_id, business_key, sale_date, marketplace,
               marketplace_order_no, quantity, sale_price, ad_cost, shipping_fee,
               vat_inclusion_type, import_source, line_no, profit_status,
               integration_account_id, created_by, updated_by)
        VALUES (:o, 'BK-M2', '2026-08-01', 'COUPANG', 'MALL-1', 1, 14000, 0, 0,
                'INCLUDED', 'EXCEL_IMPORT', 1, 'EXPECTED', :ia, :a, :a)
    """), {"o": str(org), "a": str(actor), "ia": str(other)})

    report = nes.shadow_compare(db, org_id=org, lines=[_line()],
                                account_by_mall={"mall02400": market_account})
    assert report.marketplace_mismatch == 1
    assert report.marketplace_not_comparable == 0


def test_shadow_matches_only_on_exact_order_number(db, org, actor, market_account):
    """금액·상품명·날짜가 비슷하다고 같은 주문이라고 하지 않는다."""
    db.execute(text("""
        INSERT INTO sales_order (org_id, business_key, sale_date, marketplace,
               marketplace_order_no, quantity, sale_price, ad_cost, shipping_fee,
               vat_inclusion_type, import_source, line_no, profit_status,
               created_by, updated_by)
        VALUES (:o, 'BK-1', '2026-08-01', '쿠팡', 'MALL-1', 1, 14000, 0, 0,
                'INCLUDED', 'EXCEL_IMPORT', 1, 'EXPECTED', :a, :a)
    """), {"o": str(org), "a": str(actor)})

    report = nes.shadow_compare(db, org_id=org, lines=[_line()])
    assert report.exact_existing_match == 1
    assert report.new_candidate == 0
    assert report.quantity_mismatch == 0
    assert report.amount_mismatch == 0


def test_shadow_flags_amount_mismatch(db, org, actor):
    db.execute(text("""
        INSERT INTO sales_order (org_id, business_key, sale_date, marketplace,
               marketplace_order_no, quantity, sale_price, ad_cost, shipping_fee,
               vat_inclusion_type, import_source, line_no, profit_status,
               created_by, updated_by)
        VALUES (:o, 'BK-2', '2026-08-01', '쿠팡', 'MALL-1', 5, 99999, 0, 0,
                'INCLUDED', 'EXCEL_IMPORT', 1, 'EXPECTED', :a, :a)
    """), {"o": str(org), "a": str(actor)})

    report = nes.shadow_compare(db, org_id=org, lines=[_line()])
    assert report.exact_existing_match == 1
    assert report.quantity_mismatch == 1
    assert report.amount_mismatch == 1


def test_shadow_report_has_no_order_numbers(db, org):
    """보고서는 화면·로그로 나간다."""
    report = nes.shadow_compare(db, org_id=org, lines=[_line()])
    import json as _json

    dumped = _json.dumps(report.as_dict(), ensure_ascii=False)
    assert "MALL-1" not in dumped
    assert "NE-1" not in dumped


def test_cutover_candidate_is_low_confidence_without_overlap(db, org):
    report = nes.shadow_compare(db, org_id=org, lines=[_line()])
    candidate = nes.cutover_candidate(report)
    assert candidate["confidence"] == "LOW"


def test_resolution_summary_has_no_pii(db, org, ne_account):
    import json as _json

    _, summary = nes.resolve_lines(
        db, org_id=org, external_account_id=ne_account, lines=[_line()])
    dumped = _json.dumps(summary.as_dict(), ensure_ascii=False)

    assert "PII" not in dumped
    # 스토어 코드/이름은 사용자가 연결해야 할 대상이라 남는다.
    assert "mall02400" in dumped


def test_m052_vocabulary_is_semantic_not_table_names(db):
    """어휘에 물리 테이블 이름이 들어가지 않는다.

    `integration_account` 를 값으로 쓰면 그 테이블 이름을 바꾸는 날 **데이터에
    적힌 문자열**까지 따라 고쳐야 한다.
    """
    defs = dict(db.execute(text("""
        SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
         WHERE conrelid = 'external_entity_mapping'::regclass AND contype = 'c'
    """)).all())

    assert {"ck_eem_external_type", "ck_eem_internal_type",
            "ck_eem_type_symmetry"} <= set(defs)
    for name in ("ck_eem_external_type", "ck_eem_internal_type"):
        assert "MARKETPLACE_ACCOUNT" in defs[name]
        assert "PRODUCT" in defs[name]
        assert "integration_account" not in defs[name]


def test_type_symmetry_is_enforced(db, org, ne_account, market_account):
    """상품을 판매계정에 매핑하는 것은 어떤 경우에도 말이 되지 않는다."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        db.execute(text("""
            INSERT INTO external_entity_mapping
                   (org_id, external_account_id, external_entity_type, external_entity_id,
                    internal_entity_type, internal_entity_id, mapping_status, first_seen_at)
            VALUES (:o, :a, 'PRODUCT', 'x', 'MARKETPLACE_ACCOUNT', :iid, 'ACTIVE', now())
        """), {"o": str(org), "a": str(ne_account), "iid": str(market_account)})
    db.rollback()


# ── 라인 번호 · 멱등 적재 ────────────────────────────────────────────────

def test_line_numbers_are_stable_and_unique_within_a_mall_order():
    """같은 쇼핑몰 주문의 같은 상품 두 라인이 같은 번호를 받으면 안 된다.

    실제로 그런 주문이 있었다 — 한 쇼핑몰 주문이 같은 상품 두 라인으로 왔고
    각각 다른 `ne_order_no` 를 갖고 있었다. 상품코드로 번호를 매기면 둘 다
    1번이 되어 `uq_sales_order_mkt_orderno` 를 위반한다.
    """
    a = _line(ne_order_no="NE-A")
    b = _line(ne_order_no="NE-B")          # 같은 mall_order_no, 같은 상품
    numbering = nes._line_numbers([a, b])

    assert numbering["NE-A"] != numbering["NE-B"]
    assert sorted(numbering.values()) == [1, 2]
    # 순서를 바꿔도 같은 번호가 나온다.
    assert nes._line_numbers([b, a]) == numbering


def _ready(db, org, actor, ne_account, market_account):
    """계정·상품이 모두 매핑된 상태."""
    _map_account(db, org, ne_account, market_account)
    return make_product(db, org_id=org, business_key="SKU-1")


def test_ingest_creates_then_is_idempotent(db, org, actor, ne_account, market_account):
    """같은 범위를 다시 넣어도 하나다."""
    _ready(db, org, actor, ne_account, market_account)
    lines = [_line()]

    first = nes.ingest_orders(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        lines=lines)
    assert first.created == 1
    assert first.reconciles

    second = nes.ingest_orders(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        lines=lines)
    assert second.created == 0
    assert second.unchanged == 1
    assert second.reconciles

    assert db.execute(text("""
        SELECT COUNT(*) FROM sales_order WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org)}).scalar() == 1


def test_ingest_updates_instead_of_duplicating(db, org, actor, ne_account, market_account):
    _ready(db, org, actor, ne_account, market_account)
    nes.ingest_orders(db, org_id=org, actor_user_id=actor,
                      external_account_id=ne_account, lines=[_line()])

    changed = nes.ingest_orders(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        lines=[_line(order_cnt=3)])

    assert changed.updated == 1
    assert changed.created == 0
    assert db.execute(text("""
        SELECT quantity FROM sales_order WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org)}).scalar() == 3


def test_two_lines_of_one_mall_order_both_land(db, org, actor, ne_account, market_account):
    """라인 번호 버그가 있으면 여기서 유일 인덱스 위반이 난다."""
    _ready(db, org, actor, ne_account, market_account)
    lines = [_line(ne_order_no="NE-A"), _line(ne_order_no="NE-B")]

    result = nes.ingest_orders(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        lines=lines)

    assert result.created == 2
    assert result.reconciles
    assert sorted(db.execute(text("""
        SELECT line_no FROM sales_order WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org)}).scalars().all()) == [1, 2]


def test_unknown_amount_is_rejected_not_zeroed(db, org, actor, ne_account, market_account):
    """모르는 금액을 0 으로 넣으면 순이익이 조용히 부풀려진다."""
    _ready(db, org, actor, ne_account, market_account)

    result = nes.ingest_orders(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        lines=[_line(order_price=None)])

    assert result.rejected == 1
    assert result.created == 0
    assert result.reasons[nes.REJECT_UNKNOWN_AMOUNT] == 1
    assert result.reconciles


def test_purchase_cost_stays_null(db, org, actor, ne_account, market_account):
    """원가 미상을 0 으로 만들지 않는다."""
    _ready(db, org, actor, ne_account, market_account)
    nes.ingest_orders(db, org_id=org, actor_user_id=actor,
                      external_account_id=ne_account, lines=[_line()])

    assert db.execute(text("""
        SELECT purchase_cost FROM sales_order WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org)}).scalar() is None


def test_orders_before_cutover_are_rejected(db, org, actor, ne_account, market_account):
    """과거는 건드리지 않는다."""
    from datetime import date

    _ready(db, org, actor, ne_account, market_account)
    result = nes.ingest_orders(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        lines=[_line()], cutover_after=date(2026, 12, 31))

    assert result.before_cutover == 1
    assert result.reasons[nes.REJECT_BEFORE_CUTOVER] == 1
    assert result.created == 0
    assert result.after_cutover == 0
    assert result.reconciles


def test_closed_month_is_never_written(db, org, actor, ne_account, market_account):
    """마감된 달에 매출이 새로 생기면 확정 숫자가 흔들린다."""
    _ready(db, org, actor, ne_account, market_account)
    # 마감 상태는 `close_state` 가 진실이다(`get_close_state_of`).
    db.execute(text("""
        INSERT INTO monthly_settlement
               (org_id, business_key, year_month, close_state, created_by, updated_by)
        VALUES (:o, 'MS-2026-08', '2026-08', 'LOCKED', :a, :a)
    """), {"o": str(org), "a": str(actor)})

    result = nes.ingest_orders(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        lines=[_line()])

    assert result.closed_month_protected == 1
    assert result.reasons[nes.REJECT_PERIOD_CLOSED] == 1
    assert result.eligible == 0
    assert result.reconciles
    assert db.execute(text("""
        SELECT COUNT(*) FROM sales_order WHERE org_id = :o
    """), {"o": str(org)}).scalar() == 0


def test_existing_excel_order_is_never_overwritten(db, org, actor, ne_account, market_account):
    """사람이 확인해 고쳐 둔 값을 API 가 덮으면 그 사실이 어디에도 안 남는다."""
    _ready(db, org, actor, ne_account, market_account)
    db.execute(text("""
        INSERT INTO sales_order (org_id, business_key, sale_date, marketplace,
               marketplace_order_no, quantity, sale_price, ad_cost, shipping_fee,
               vat_inclusion_type, import_source, line_no, profit_status,
               created_by, updated_by)
        VALUES (:o, 'EXCEL-1', '2026-08-01', 'COUPANG', 'MALL-1', 9, 111, 0, 0,
                'INCLUDED', 'EXCEL_IMPORT', 1, 'EXPECTED', :a, :a)
    """), {"o": str(org), "a": str(actor)})

    result = nes.ingest_orders(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        lines=[_line()])

    assert result.existing_protected == 1
    assert result.reasons[nes.CONFLICT_OTHER_SOURCE] == 1
    assert result.eligible == 0
    assert result.reconciles
    assert db.execute(text("""
        SELECT quantity FROM sales_order WHERE business_key = 'EXCEL-1'
    """)).scalar() == 9


def test_unmapped_lines_are_counted_not_dropped(db, org, actor, ne_account):
    """매핑이 없다고 라인을 버리면 fetched 와 결과 합이 어긋난다."""
    result = nes.ingest_orders(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        lines=[_line(), _line(ne_order_no="NE-2")])

    assert result.fetched == 2
    assert result.account_unmapped == 2
    assert result.account_mapped == 0
    assert result.reconciles
    assert result.reasons[nes.UNMAPPED_MARKETPLACE_ACCOUNT] == 2


def test_ingest_stores_no_pii(db, org, actor, ne_account, market_account):
    """적재된 행에 개인정보가 없어야 한다."""
    _ready(db, org, actor, ne_account, market_account)
    nes.ingest_orders(db, org_id=org, actor_user_id=actor,
                      external_account_id=ne_account, lines=[_line()])

    dumped = db.execute(text("""
        SELECT string_agg(t::text, ' ') FROM sales_order t WHERE org_id = :o
    """), {"o": str(org)}).scalar() or ""
    for marker in ("PII_", "buyer", "receiver", "clearance"):
        assert marker not in dumped


# ── M052 유형 매트릭스 · cross-org (§22-23) ──────────────────────────────

def _insert_mapping(db, org, ne_account, ext_type, int_type, internal_id,
                    external_id="X-1"):
    db.execute(text("""
        INSERT INTO external_entity_mapping
               (org_id, external_account_id, external_entity_type, external_entity_id,
                internal_entity_type, internal_entity_id, mapping_status, first_seen_at)
        VALUES (:o, :a, :et, :eid, :it, :iid, 'ACTIVE', now())
    """), {"o": str(org), "a": str(ne_account), "et": ext_type, "eid": external_id,
           "it": int_type, "iid": str(internal_id)})


def test_m052_product_to_product_is_allowed(db, org, actor, ne_account):
    pid = make_product(db, org_id=org, business_key="M052-P")
    _insert_mapping(db, org, ne_account, "PRODUCT", "PRODUCT", pid)
    assert db.execute(text("""
        SELECT COUNT(*) FROM external_entity_mapping
         WHERE external_entity_type = 'PRODUCT' AND external_entity_id = 'X-1'
    """)).scalar() == 1


def test_m052_account_to_account_is_allowed(db, org, ne_account, market_account):
    _insert_mapping(db, org, ne_account, "MARKETPLACE_ACCOUNT",
                    "MARKETPLACE_ACCOUNT", market_account)
    assert db.execute(text("""
        SELECT COUNT(*) FROM external_entity_mapping
         WHERE external_entity_type = 'MARKETPLACE_ACCOUNT'
    """)).scalar() == 1


def test_m052_product_to_account_is_rejected(db, org, ne_account, market_account):
    """상품을 판매계정에 매핑하는 것은 어떤 경우에도 말이 되지 않는다."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        _insert_mapping(db, org, ne_account, "PRODUCT", "MARKETPLACE_ACCOUNT",
                        market_account)
    db.rollback()


def test_m052_account_to_product_is_rejected(db, org, actor, ne_account):
    from sqlalchemy.exc import IntegrityError

    pid = make_product(db, org_id=org, business_key="M052-Q")
    with pytest.raises(IntegrityError):
        _insert_mapping(db, org, ne_account, "MARKETPLACE_ACCOUNT", "PRODUCT", pid)
    db.rollback()


def test_m052_unknown_entity_type_is_rejected(db, org, ne_account, market_account):
    """지원하지 않는 유형을 미리 열어 두지 않는다 — M039 가 정한 원칙이다."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        _insert_mapping(db, org, ne_account, "WAREHOUSE", "WAREHOUSE", market_account)
    db.rollback()


def test_same_mall_cd_cannot_map_to_two_accounts(db, org, actor, ne_account,
                                                 market_account):
    """DB 가 막는다 — 애플리케이션 검사만으로는 동시 실행 두 개를 못 막는다."""
    from sqlalchemy.exc import IntegrityError

    second = make_integration_account(db, org_id=org, provider="COUPANG",
                                      account_label="쿠팡 2호점")
    _map_account(db, org, ne_account, market_account, "mall02400")
    with pytest.raises(IntegrityError):
        _map_account(db, org, ne_account, second, "mall02400")
    db.rollback()


def test_cross_org_product_mapping_is_ignored_at_read(db, org, actor, ne_account):
    """다른 조직 상품을 가리키는 PRODUCT 매핑은 읽기에서 걸러진다."""
    other = make_organization(db)
    foreign_product = make_product(db, org_id=other, business_key="FOREIGN")
    _insert_mapping(db, org, ne_account, "PRODUCT", "PRODUCT", foreign_product,
                    external_id="NE-P-1")

    found = nes._lookup_product_mapping(
        db, org_id=org, external_account_id=ne_account, ne_product_cd="NE-P-1")
    assert found is None


def test_cross_org_marketplace_mapping_is_ignored_at_read(db, org, ne_account):
    """다른 조직 판매계정을 가리키는 매핑도 마찬가지다."""
    other = make_organization(db)
    foreign = make_integration_account(db, org_id=other, provider="COUPANG",
                                       account_label="남의 계정")
    _insert_mapping(db, org, ne_account, "MARKETPLACE_ACCOUNT",
                    "MARKETPLACE_ACCOUNT", foreign, external_id="mall02400")

    found = nes.resolve_marketplace_account(
        db, org_id=org, external_account_id=ne_account, mall_cd="mall02400")
    assert found is None


# ── 깔때기 대사 (§3) ────────────────────────────────────────────────────

def test_funnel_places_each_line_in_exactly_one_gate(db, org, actor, ne_account,
                                                     market_account):
    """모든 라인은 정확히 하나의 관문에서 떨어진다.

    합이 안 맞으면 어떤 라인이 조용히 사라진 것이고, 그 상태를 성공으로
    기록하면 화면과 장부가 달라진다.
    """
    from datetime import date

    _map_account(db, org, ne_account, market_account)
    make_product(db, org_id=org, business_key="SKU-1")

    lines = [
        _line(ne_order_no="OK-1"),                      # 통과
        _line(ne_order_no="OLD-1", pay_date="2026-01-01 00:00:00"),  # cutover 이전
        _line(ne_order_no="NOACC-1", mall_cd="mall99999"),           # 계정 미매핑
        _line(ne_order_no="NOPROD-1", ne_product_cd="UNKNOWN",
              ne_partner_product_cd="NOPE"),                          # 상품 미매핑
        _line(ne_order_no="BAD-1", pay_date=None, order_date=None),  # 시간축 없음
    ]
    result = nes.ingest_orders(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        lines=lines, cutover_after=date(2026, 6, 30))

    assert result.fetched == 5
    assert result.invalid_line == 1
    assert result.before_cutover == 1
    assert result.account_unmapped == 1
    assert result.product_unmapped == 1
    assert result.eligible == 1
    assert result.created == 1
    assert result.reconciles
    assert result.gate_total == 5
    assert result.root_cause() == "INGESTED"


def test_root_cause_names_the_blocking_gate(db, org, actor, ne_account, market_account):
    """0건일 때 "데이터 문제" 로 끝내지 않는다."""
    _map_account(db, org, ne_account, market_account)
    # 상품을 만들지 않는다 → 상품 관문에서 전부 막힌다.
    result = nes.ingest_orders(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        lines=[_line(), _line(ne_order_no="NE-2")])

    assert result.created == 0
    assert result.product_unmapped == 2
    assert result.root_cause() == "PRODUCT_MAPPING_MISSING"
    assert result.reconciles


def test_cutover_protection_is_not_reported_as_the_root_cause(
        db, org, actor, ne_account, market_account):
    """과거 주문이 많다고 "cutover 때문" 이라고 답하면 안 된다.

    그건 의도한 보호다. 답해야 할 질문은 **cutover 이후 후보가 어디서
    멈췄나** 이고, 여기서는 상품 매핑이다.
    """
    from datetime import date

    _map_account(db, org, ne_account, market_account)
    lines = [_line(ne_order_no=f"OLD-{i}", pay_date="2026-01-01 00:00:00")
             for i in range(5)]
    lines.append(_line(ne_order_no="NEW-1"))

    result = nes.ingest_orders(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        lines=lines, cutover_after=date(2026, 6, 30))

    assert result.before_cutover == 5
    assert result.after_cutover == 1
    assert result.product_unmapped == 1
    assert result.root_cause() == "PRODUCT_MAPPING_MISSING"
    assert result.reconciles


def test_no_candidates_after_cutover_says_so(db, org, actor, ne_account,
                                             market_account):
    from datetime import date

    _map_account(db, org, ne_account, market_account)
    result = nes.ingest_orders(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        lines=[_line(pay_date="2026-01-01 00:00:00")],
        cutover_after=date(2026, 6, 30))

    assert result.after_cutover == 0
    assert result.root_cause() == "NO_ORDERS_AFTER_CUTOVER"
