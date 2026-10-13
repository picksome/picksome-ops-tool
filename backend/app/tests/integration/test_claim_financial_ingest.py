"""클레임 재무 적재 (Phase 3).

이 파일이 고정하는 것
------------------
1. 원 주문을 **덮어쓰지 않는다** — 조정 행을 쌓는다
2. 모르는 금액을 0 으로 만들지 않는다
3. 반품이라고 재고·원가를 건드리지 않는다
4. 잠긴 달을 다시 계산하지 않고 **열린 달에 조정**을 단다
5. 철회는 지우지 않고 상쇄한다
6. Excel 로 들어온 과거 주문에도 클레임이 붙는다
7. 같은 클레임을 몇 번 읽어도 매출은 한 번만 깎인다
8. 개인정보를 저장하지 않는다

2 가 이 파일의 핵심이다
--------------------
실측에서 `취소완료` 인데 `cancel_price` 가 0 으로 왔다. 그 0 을 믿으면
3만원짜리 취소가 "0원 취소" 가 되어 매출이 그대로 남는다.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.connectors.next_engine.claim_normalize import normalize_claim
from app.services import claim_ingest as ci
from app.services import next_engine_order_sync as nes
from app.tests.fixtures.factories import (
    make_app_user,
    make_integration_account,
    make_organization,
    make_product,
)

NOW = datetime(2026, 8, 20, 9, 0, 0)

ROW = {
    "ne_claim_no": "C-1", "ne_order_no": "O-1", "mall_order_no": "M-1",
    "mall_cd": "mall02400", "mall_name": "쿠팡",
    "ne_product_cd": "P-1", "ne_partner_product_cd": "SKU-1",
    "product_name": "테스트상품", "option_name": "기본",
    "order_price": 30000, "order_cnt": 1, "sell_price": 30000,
    "delivery_price": 0,
    "order_date": "2026-08-01 10:00:00", "pay_date": "2026-08-01 10:00:00",
    "claim_status_txt": "취소완료",
}


def _claim(divi="08", **over):
    return normalize_claim(dict(ROW, **over), claim_divi=divi)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


@pytest.fixture
def ne_account(db: Session, org, actor) -> uuid.UUID:
    sys_id = db.execute(text("""
        INSERT INTO external_system (code, name, system_type, supports_api_key)
        VALUES ('NEXT_ENGINE', 'NEXT ENGINE', 'SELLER_TOOL', TRUE)
        ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id
    """)).scalar()
    return uuid.UUID(str(db.execute(text("""
        INSERT INTO external_account (org_id, external_system_id, account_code,
               display_name, created_by, updated_by)
        VALUES (:o, :s, 'DEFAULT', 'NE', :a, :a) RETURNING id
    """), {"o": str(org), "s": str(sys_id), "a": str(actor)}).scalar()))


@pytest.fixture
def market(db: Session, org, ne_account) -> uuid.UUID:
    account = make_integration_account(db, org_id=org, provider="COUPANG",
                                       account_label="쿠팡 본점")
    db.execute(text("""
        INSERT INTO external_entity_mapping
               (org_id, external_account_id, external_entity_type, external_entity_id,
                internal_entity_type, internal_entity_id, mapping_status, first_seen_at)
        VALUES (:o, :a, :et, 'mall02400', :et, :iid, 'ACTIVE', now())
    """), {"o": str(org), "a": str(ne_account),
           "et": nes.ENTITY_MARKETPLACE_ACCOUNT, "iid": str(account)})
    db.commit()
    return account


def _api_order(db, org, *, business_key="NE:O-1", sale_price=30000, quantity=1,
               sale_date=date(2026, 8, 1), order_no="M-1", line_no=1):
    """API 로 들어온 주문 한 줄."""
    product = make_product(db, org_id=org, business_key=f"SKU-{uuid.uuid4().hex[:6]}")
    oid = db.execute(text("""
        INSERT INTO sales_order (org_id, business_key, sale_date, marketplace,
               marketplace_order_no, product_id, product_snapshot_name, quantity,
               sale_price, ad_cost, shipping_fee, vat_inclusion_type,
               import_source, line_no, profit_status)
        VALUES (:o, :bk, :sd, 'COUPANG', :mo, :p, '테스트상품', :q, :sp,
                0, 0, 'INCLUDED', 'NEXTENGINE_API', :ln, 'EXPECTED')
        RETURNING id
    """), {"o": str(org), "bk": business_key, "sd": sale_date, "mo": order_no,
           "p": str(product), "q": quantity, "sp": sale_price, "ln": line_no}).scalar()
    db.commit()
    return uuid.UUID(str(oid))


def _ingest(db, org, actor, ne_account, claims, now=NOW):
    result = ci.ingest_claims(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        claims=claims, now=now)
    db.commit()
    return result


def _adjustments(db, org):
    return db.execute(text("""
        SELECT adjustment_type, amount_delta, original_period, adjustment_period,
               amount_source, quantity_delta
          FROM sales_adjustment WHERE org_id = :o AND deleted_at IS NULL
         ORDER BY created_at
    """), {"o": str(org)}).mappings().all()


def _claims(db, org):
    return db.execute(text("""
        SELECT external_claim_id, claim_type, claim_status, financial_effect,
               claim_amount, claim_quantity, amount_source, sales_order_id
          FROM sales_claim WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org)}).mappings().all()


# ── 1) 원 주문을 덮어쓰지 않는다 ────────────────────────────────────────

def test_the_original_order_is_never_overwritten(db, org, actor, ne_account, market):
    """3만원 주문이 실제로 있었다는 사실은 취소돼도 사라지지 않는다."""
    order_id = _api_order(db, org)

    _ingest(db, org, actor, ne_account, [_claim(cancel_cnt="1", cancel_price="30000")])

    still = db.execute(text("""
        SELECT sale_price, quantity FROM sales_order WHERE id = :i
    """), {"i": str(order_id)}).mappings().one()
    assert still["sale_price"] == 30000
    assert still["quantity"] == 1

    adj = _adjustments(db, org)
    assert len(adj) == 1
    assert adj[0]["amount_delta"] == -30000
    assert adj[0]["amount_source"] == "VENDOR_CLAIM"


# ── 2) 모르는 금액을 0 으로 만들지 않는다 ───────────────────────────────

def test_a_zero_vendor_amount_on_a_full_cancel_is_not_believed(
    db, org, actor, ne_account, market,
):
    """실측 사례. 취소완료인데 cancel_price 가 0 으로 온다.

    그 0 을 믿으면 3만원 취소가 아무 일도 아닌 게 된다. 전량 취소이므로
    그 라인 매출 전부를 되돌린다 — 할인 배분을 풀 필요가 없다.
    """
    _api_order(db, org)

    _ingest(db, org, actor, ne_account,
            [_claim(cancel_cnt="1", cancel_price="0")])

    adj = _adjustments(db, org)
    assert len(adj) == 1
    assert adj[0]["amount_delta"] == -30000
    assert adj[0]["amount_source"] == "DERIVED_FROM_ORIGINAL_ORDER"


def test_a_partial_cancel_without_an_amount_is_unresolved_not_guessed(
    db, org, actor, ne_account, market,
):
    """단가를 안다고 부분 금액을 알 수 있는 게 아니다.

    쿠폰·옵션가·마켓부담 할인이 어디에 붙었는지 모른다. 3개 중 1개 취소를
    "단가 × 1" 로 계산하면 그 차액이 조용히 틀린다.
    """
    _api_order(db, org, sale_price=90000, quantity=3)

    result = _ingest(db, org, actor, ne_account, [
        _claim(order_cnt=3, order_price=90000, cancel_cnt="1", cancel_price="0"),
    ])

    assert result.financial_unresolved == 1
    assert _adjustments(db, org) == []
    claim = _claims(db, org)[0]
    assert claim["financial_effect"] == "UNRESOLVED"
    assert claim["claim_amount"] is None       # 0 이 아니라 없음


def test_a_partial_cancel_with_a_vendor_amount_uses_exactly_that(
    db, org, actor, ne_account, market,
):
    """부분이라도 벤더가 금액을 주면 그 금액만 조정한다. 전체를 되돌리지 않는다."""
    _api_order(db, org, sale_price=90000, quantity=3)

    _ingest(db, org, actor, ne_account, [
        _claim(order_cnt=3, order_price=90000, cancel_cnt="1", cancel_price="30000"),
    ])

    adj = _adjustments(db, org)
    assert len(adj) == 1
    assert adj[0]["amount_delta"] == -30000    # -90000 이 아니다
    assert adj[0]["quantity_delta"] == -1


# ── 3) 재고·원가를 건드리지 않는다 ──────────────────────────────────────

def test_a_completed_return_touches_neither_inventory_nor_cost(
    db, org, actor, ne_account, market,
):
    """반품 완료가 곧 물건이 돌아온 것은 아니다.

    미발송·파손·폐기가 전부 '반품완료' 로 온다. 실물 입고 증거 없이 재고를
    늘리면 팔 수 없는 물건을 재고로 세게 된다.
    """
    _api_order(db, org)
    before_tx = db.execute(text("SELECT count(*) FROM inventory_transaction")).scalar()
    before_cost = db.execute(text("""
        SELECT COALESCE(sum(cost_amount), 0) FROM product WHERE org_id = :o
    """), {"o": str(org)}).scalar()

    _ingest(db, org, actor, ne_account,
            [_claim("19", return_cnt="1", return_price="30000")])

    assert db.execute(text("SELECT count(*) FROM inventory_transaction")).scalar() \
        == before_tx
    assert db.execute(text("""
        SELECT COALESCE(sum(cost_amount), 0) FROM product WHERE org_id = :o
    """), {"o": str(org)}).scalar() == before_cost
    # 매출은 줄었다 — 그것만 확정할 수 있다.
    assert _adjustments(db, org)[0]["amount_delta"] == -30000


def test_a_claim_never_creates_a_cash_movement(db, org, actor, ne_account, market):
    """클레임은 사실이고, 실제 현금은 정산서가 말한다."""
    _api_order(db, org)
    before = db.execute(text("SELECT count(*) FROM manual_cash_event")).scalar()
    before_bal = db.execute(text("SELECT count(*) FROM cash_balance_baseline")).scalar()

    _ingest(db, org, actor, ne_account, [_claim(cancel_price="30000")])

    assert db.execute(text("SELECT count(*) FROM manual_cash_event")).scalar() == before
    assert db.execute(
        text("SELECT count(*) FROM cash_balance_baseline")).scalar() == before_bal


# ── 4) 잠긴 달 ──────────────────────────────────────────────────────────

def test_a_late_claim_is_booked_to_the_open_period(db, org, actor, ne_account, market):
    """7월 주문이 마감된 뒤 8월에 반품이 확정됐다.

    7월을 다시 계산하면 이미 확정된 순이익과 파트너 배분이 소급해서 틀린
    값이 된다. 8월에 조정을 달고 7월 원본은 그대로 둔다.
    """
    _api_order(db, org, sale_date=date(2026, 7, 10))
    db.execute(text("""
        INSERT INTO monthly_settlement (org_id, business_key, year_month, close_state)
        VALUES (:o, :bk, '2026-07', 'LOCKED')
    """), {"o": str(org), "bk": f"MS-{uuid.uuid4().hex[:8]}"})
    db.commit()

    _ingest(db, org, actor, ne_account, [
        _claim("19", pay_date="2026-07-10 10:00:00", return_cnt="1",
               return_price="30000"),
    ])

    adj = _adjustments(db, org)
    assert len(adj) == 1
    assert adj[0]["original_period"] == "2026-07"
    assert adj[0]["adjustment_period"] == "2026-08"     # 열린 달


def test_an_open_month_claim_stays_in_its_own_month(db, org, actor, ne_account, market):
    """열린 달이면 굳이 옮기지 않는다."""
    _api_order(db, org, sale_date=date(2026, 8, 1))

    _ingest(db, org, actor, ne_account, [_claim(cancel_price="30000")])

    adj = _adjustments(db, org)[0]
    assert adj["original_period"] == "2026-08"
    assert adj["adjustment_period"] == "2026-08"


def test_a_claim_never_reopens_a_closed_month(db, org, actor, ne_account, market):
    _api_order(db, org, sale_date=date(2026, 7, 10))
    db.execute(text("""
        INSERT INTO monthly_settlement (org_id, business_key, year_month, close_state)
        VALUES (:o, :bk, '2026-07', 'LOCKED')
    """), {"o": str(org), "bk": f"MS-{uuid.uuid4().hex[:8]}"})
    db.commit()

    _ingest(db, org, actor, ne_account,
            [_claim("19", pay_date="2026-07-10 10:00:00", return_price="30000")])

    assert db.execute(text("""
        SELECT close_state FROM monthly_settlement
         WHERE org_id = :o AND year_month = '2026-07'
    """), {"o": str(org)}).scalar() == "LOCKED"


# ── 5) 철회는 상쇄한다 ──────────────────────────────────────────────────

def test_a_withdrawal_compensates_instead_of_deleting(db, org, actor, ne_account, market):
    """지우면 "취소가 있었다가 철회됐다" 는 사실이 사라진다."""
    _api_order(db, org)
    _ingest(db, org, actor, ne_account, [_claim("08", cancel_price="30000")])

    _ingest(db, org, actor, ne_account, [_claim("09", cancel_price="30000")])

    adj = _adjustments(db, org)
    assert [a["adjustment_type"] for a in adj] == ["CLAIM_REVENUE", "CLAIM_REVERSAL"]
    assert [a["amount_delta"] for a in adj] == [-30000, 30000]
    assert sum(a["amount_delta"] for a in adj) == 0


def test_a_withdrawal_of_something_never_applied_does_nothing(
    db, org, actor, ne_account, market,
):
    """반영한 적 없는 것을 되돌릴 수는 없다."""
    _api_order(db, org)

    _ingest(db, org, actor, ne_account, [_claim("09", cancel_price="30000")])

    assert _adjustments(db, org) == []


# ── 6) Excel 주문에도 붙는다 ────────────────────────────────────────────

def test_a_claim_links_to_an_order_that_came_from_excel(
    db, org, actor, ne_account, market,
):
    """초기 구축 데이터라고 클레임을 무시하면 7월 주문의 8월 반품이 사라진다.

    들어온 경로가 다를 뿐 같은 판매다. 마켓 주문번호로 정확히 잇는다.
    """
    product = make_product(db, org_id=org, business_key="EXCEL-1")
    db.execute(text("""
        INSERT INTO sales_order (org_id, business_key, sale_date, marketplace,
               marketplace_order_no, product_id, product_snapshot_name, quantity,
               sale_price, ad_cost, shipping_fee, vat_inclusion_type,
               import_source, line_no, profit_status)
        VALUES (:o, 'XLS-1', DATE '2026-08-01', 'COUPANG', 'M-1', :p, '엑셀상품',
                1, 30000, 0, 0, 'INCLUDED', 'EXCEL_IMPORT', 1, 'EXPECTED')
    """), {"o": str(org), "p": str(product)})
    db.commit()

    # ne_order_no 로는 못 찾는다 — 그 주문은 API 로 들어오지 않았다.
    result = _ingest(db, org, actor, ne_account,
                     [_claim(ne_order_no="없는번호", cancel_price="30000")])

    assert result.linked == 1
    assert result.unmatched_order == 0
    assert _adjustments(db, org)[0]["amount_delta"] == -30000


def test_an_unmatched_claim_is_held_not_attached_to_a_similar_order(
    db, org, actor, ne_account, market,
):
    """비슷한 주문에 붙이지 않는다. 틀린 조정은 아무도 발견하지 못한다."""
    _api_order(db, org, business_key="NE:다른주문", order_no="다른번호")

    result = _ingest(db, org, actor, ne_account,
                     [_claim(ne_order_no="없음", mall_order_no="없음",
                             cancel_price="30000")])

    assert result.unmatched_order == 1
    assert _adjustments(db, org) == []
    assert _claims(db, org)[0]["sales_order_id"] is None


# ── 출고 전 취소 — 되돌릴 매출이 없다 ───────────────────────────────────
#
# 실측 16건 중 14건이 이 경우였고, `UNRESOLVED` 로 두었을 때 5·6·7월 마감이
# 전부 막혔다. 순매출은 한 원도 틀리지 않았는데도(M054).

def test_a_claim_whose_order_is_not_in_the_ledger_has_no_ledger_impact(
    db, org, actor, ne_account, market,
):
    """원 주문이 장부에 없으면 되돌릴 매출이 없다.

    "확정됐는데 금액을 못 정했다"(순매출이 부풀어 있다) 와 정반대의 사실이다.
    한 칸에 두면 출고 전 취소가 마감을 막는다.
    """
    result = _ingest(db, org, actor, ne_account,
                     [_claim(ne_order_no="없음", mall_order_no="없음",
                             cancel_cnt="1", cancel_price="0")])

    assert result.financial_no_ledger_impact == 1
    assert result.financial_unresolved == 0
    claim = _claims(db, org)[0]
    assert claim["financial_effect"] == "NO_LEDGER_IMPACT"
    assert claim["claim_amount"] is None
    assert _adjustments(db, org) == []


def test_a_pre_shipment_cancel_does_not_block_the_month_close(
    db, org, actor, ne_account, market,
):
    """이 테스트가 없어서 실측 14건이 세 달의 마감을 막았다.

    막는 근거는 "그 상태로 잠그면 순매출이 부풀어 있다" 이다. 원 주문이
    없으면 되돌릴 매출도 없으므로 그 근거가 성립하지 않는다.
    """
    from app.services.close_checks import run_checks

    # 그 달에 실제 매출도 있어야 마감 검증이 의미가 있다.
    _api_order(db, org, business_key="NE:정상", order_no="정상번호",
               sale_date=date(2026, 8, 1))
    _ingest(db, org, actor, ne_account,
            [_claim(ne_order_no="없음", mall_order_no="없음",
                    cancel_cnt="1", cancel_price="0")])

    items = run_checks(db, org_id=org, year_month="2026-08")
    codes = {i.code: i for i in items}

    assert "CLAIM_FINANCIAL_UNRESOLVED" not in codes
    # 그렇다고 조용하지도 않다 — 사람이 볼 수 있게 경고로 남는다.
    assert codes["CLAIM_WITHOUT_ORDER"].grade == "WARNING"
    assert codes["CLAIM_WITHOUT_ORDER"].blocking is False


def test_the_close_screen_shows_an_adjustment_the_snapshot_does_not_include(
    db, org, actor, ne_account, market,
):
    """확정 스냅샷의 총매출은 주문만 더한다 — 클레임 조정이 안 들어 있다.

    조정을 순이익·배분에 접을지는 아직 정해지지 않은 재무 결정이다
    (`expense_adjustment` 도 같은 상태다). 그 결정을 검증기가 대신 내리지
    않되, **잠그기 직전에 금액은 보여 준다.**
    """
    from app.services.close_checks import run_checks

    _api_order(db, org, sale_date=date(2026, 8, 1))
    _ingest(db, org, actor, ne_account, [_claim(cancel_price="30000")])

    codes = {i.code: i for i in run_checks(db, org_id=org, year_month="2026-08")}
    row = codes["CLAIM_ADJUSTMENT_NOT_IN_SNAPSHOT"]

    assert "-30,000" in row.message
    # 막지는 않는다 — 막으면 비용 정정과 규칙이 어긋나고 모든 달이 잠긴다.
    assert row.blocking is False


def test_a_month_without_claim_adjustments_stays_quiet(
    db, org, actor, ne_account, market,
):
    """조정이 없으면 경고도 없다. 늘 떠 있는 경고는 아무도 안 읽는다."""
    from app.services.close_checks import run_checks

    _api_order(db, org, sale_date=date(2026, 8, 1))

    codes = {i.code for i in run_checks(db, org_id=org, year_month="2026-08")}
    assert "CLAIM_ADJUSTMENT_NOT_IN_SNAPSHOT" not in codes


def test_an_amount_we_cannot_determine_still_blocks_the_month_close(
    db, org, actor, ne_account, market,
):
    """반대쪽도 고정한다. 원 주문이 **있는데** 금액을 못 정하면 막아야 한다.

    그 상태로 잠그면 순매출이 그만큼 부풀어 있고, 순이익과 파트너 배분이
    전부 그 위에서 계산된다.
    """
    from app.services.close_checks import run_checks

    _api_order(db, org, sale_price=90000, quantity=3, sale_date=date(2026, 8, 1))
    _ingest(db, org, actor, ne_account, [
        _claim(order_cnt=3, order_price=90000, cancel_cnt="1", cancel_price="0"),
    ])

    codes = {i.code: i for i in run_checks(db, org_id=org, year_month="2026-08")}
    assert codes["CLAIM_FINANCIAL_UNRESOLVED"].blocking is True


def test_the_same_pre_shipment_cancel_is_not_counted_twice(
    db, org, actor, ne_account, market,
):
    """한 클레임이 `unmatched_order` 와 `unresolved` 양쪽에 잡히면 안 된다.

    실측에서 14건이 conflict 28 로 기록됐다. 숫자가 두 배면 운영자는
    무엇이 몇 건인지 알 수 없다.
    """
    result = _ingest(db, org, actor, ne_account,
                     [_claim(ne_order_no="없음", mall_order_no="없음",
                             cancel_cnt="1", cancel_price="0")])

    assert result.unmatched_order == 1
    assert result.financial_unresolved == 0
    assert result.reconciles


def test_no_ledger_impact_does_not_pollute_the_effective_sales_warning(
    db, org, actor, ne_account, market,
):
    """유효 매출은 정확하다. `unresolved_claims` 에 섞으면 못 믿는 신호가 된다."""
    _api_order(db, org, sale_price=30000, sale_date=date(2026, 8, 1))
    _ingest(db, org, actor, ne_account,
            [_claim(ne_order_no="없음", mall_order_no="없음", cancel_price="0")])

    view = ci.effective_sales(db, org_id=org, year_month="2026-08")

    assert view["gross_sales"] == 30000
    assert view["claim_adjustment"] == 0
    assert view["effective_sales"] == 30000
    assert view["unresolved_claims"] == 0
    assert view["no_ledger_impact_claims"] == 1


def test_the_claim_still_becomes_an_adjustment_once_its_order_arrives(
    db, org, actor, ne_account, market,
):
    """`NO_LEDGER_IMPACT` 는 "버렸다" 가 아니다. 주문이 오면 매출이 줄어야 한다."""
    claim = _claim(ne_order_no="늦은주문", mall_order_no="늦은번호",
                   cancel_cnt="1", cancel_price="0")
    first = _ingest(db, org, actor, ne_account, [claim])
    assert first.financial_no_ledger_impact == 1

    _api_order(db, org, business_key="NE:늦은주문", order_no="늦은번호")
    second = _ingest(db, org, actor, ne_account, [claim])

    assert second.linked == 1
    assert second.financial_effective == 1
    assert _adjustments(db, org)[0]["amount_delta"] == -30000
    assert _claims(db, org)[0]["financial_effect"] == "EFFECTIVE"


def test_an_unknown_code_without_an_order_is_still_unresolved_not_waved_through(
    db, org, actor, ne_account, market,
):
    """모르는 코드는 취소인지조차 알 수 없다.

    원 주문이 없다고 `NO_LEDGER_IMPACT` 로 넘기면, `ne_order_no` 를 잘못
    읽고 있는 경우까지 조용히 통과한다.
    """
    result = _ingest(db, org, actor, ne_account,
                     [_claim("99", ne_order_no="없음", mall_order_no="없음")])

    assert result.financial_unresolved == 1
    assert result.financial_no_ledger_impact == 0
    # 주문만 찾다가 끝나지 않도록 코드 자체도 보여 준다.
    kinds = {i["issue_type"] for i in _open_issues(db, org)}
    assert ci.ISSUE_STATUS_UNKNOWN in kinds
    assert ci.ISSUE_UNMATCHED_ORDER in kinds


def test_an_ambiguous_line_still_blocks_because_the_revenue_is_in_the_ledger(
    db, org, actor, ne_account, market,
):
    """라인이 애매한 것은 원 주문이 **장부에 있다.** 그 매출은 부풀어 있다."""
    _api_order(db, org, business_key="NE:A", order_no="같은번호", line_no=1)
    _api_order(db, org, business_key="NE:B", order_no="같은번호", line_no=2)

    result = _ingest(db, org, actor, ne_account, [
        _claim(ne_order_no="없음", mall_order_no="같은번호", cancel_price="30000"),
    ])

    assert result.financial_unresolved == 1
    assert result.financial_no_ledger_impact == 0
    assert _claims(db, org)[0]["financial_effect"] == "UNRESOLVED"


def test_a_multi_line_order_number_is_not_split_arbitrarily(
    db, org, actor, ne_account, market,
):
    """한 주문번호에 라인이 둘이면 어느 쪽 클레임인지 모른다."""
    _api_order(db, org, business_key="NE:A", order_no="같은번호", line_no=1)
    _api_order(db, org, business_key="NE:B", order_no="같은번호", line_no=2)

    result = _ingest(db, org, actor, ne_account, [
        _claim(ne_order_no="없음", mall_order_no="같은번호", cancel_price="30000"),
    ])

    assert result.unmatched_line == 1
    assert _adjustments(db, org) == []


# ── 7) 멱등성 ───────────────────────────────────────────────────────────

def test_reading_the_same_claim_many_times_deducts_revenue_once(
    db, org, actor, ne_account, market,
):
    _api_order(db, org)

    for _ in range(5):
        _ingest(db, org, actor, ne_account, [_claim(cancel_price="30000")])

    assert len(_claims(db, org)) == 1
    adj = _adjustments(db, org)
    assert len(adj) == 1
    assert adj[0]["amount_delta"] == -30000


def test_a_status_transition_updates_the_same_claim_row(
    db, org, actor, ne_account, market,
):
    """요청 → 완료가 클레임 2건으로 보이면 취소 건수가 부풀어 오른다."""
    _api_order(db, org)

    first = _ingest(db, org, actor, ne_account, [_claim("06")])
    assert first.financial_pending == 1
    assert _adjustments(db, org) == []

    second = _ingest(db, org, actor, ne_account, [_claim("08", cancel_price="30000")])

    assert second.updated == 1
    assert len(_claims(db, org)) == 1
    assert _claims(db, org)[0]["claim_status"] == "COMPLETED"
    assert len(_adjustments(db, org)) == 1


def test_counts_reconcile(db, org, actor, ne_account, market):
    _api_order(db, org)
    result = _ingest(db, org, actor, ne_account, [
        _claim(cancel_price="30000"),
        _claim(ne_claim_no="C-2", ne_order_no="없음", mall_order_no="없음"),
        _claim(ne_claim_no=None),
    ])
    assert result.fetched == 3
    assert result.invalid == 1
    assert result.reconciles


# ── 교환 · 모르는 코드 ──────────────────────────────────────────────────

def test_an_exchange_does_not_move_revenue(db, org, actor, ne_account, market):
    """교환은 같은 값으로 물건이 바뀌는 것이다. 환불 사실을 API 가 주지 않는다."""
    _api_order(db, org)

    result = _ingest(db, org, actor, ne_account,
                     [_claim("14", change_cnt="1", change_price="30000")])

    assert result.financial_none == 1
    assert _adjustments(db, org) == []


def test_an_unknown_vendor_code_does_not_move_revenue(
    db, org, actor, ne_account, market,
):
    """벤더가 새 코드를 냈다. 잘못 해석해 매출을 건드리는 것보다 멈춘다."""
    _api_order(db, org)

    result = _ingest(db, org, actor, ne_account, [_claim("99", cancel_price="30000")])

    assert result.financial_unresolved == 1
    assert ci.STATUS_UNKNOWN in result.reasons
    assert _adjustments(db, org) == []


# ── 8) 개인정보 ─────────────────────────────────────────────────────────

def test_personal_data_never_reaches_the_dto_or_the_database(
    db, org, actor, ne_account, market,
):
    """클레임 응답에는 구매자·수취인 정보가 전부 채워져서 온다(실측 10/10)."""
    _api_order(db, org)
    sentinels = {
        "buyer_name": "홍길동", "buyer_phone": "010-1234-5678",
        "receiver_name": "김수취", "receiver_addr": "서울시 강남구 어딘가",
        "receiver_hp_phone": "010-9999-8888", "receiver_msg": "부재시 경비실",
        "receiver_zip_code": "06222", "receiver_email": "a@b.com",
        "individual_clearance_no": "P123456789",
        "return_req_msg": "집앞에 두세요", "change_cllct_req_msg": "연락주세요",
    }
    claim = _claim(cancel_price="30000", **sentinels)

    # DTO 에 담을 그릇 자체가 없다.
    for field_name in sentinels:
        assert not hasattr(claim, field_name), f"{field_name} 가 DTO 에 있다"

    _ingest(db, org, actor, ne_account, [claim])

    dumped = " ".join(
        str(v) for row in db.execute(text("""
            SELECT * FROM sales_claim WHERE org_id = :o
        """), {"o": str(org)}).mappings() for v in row.values())
    for value in sentinels.values():
        assert value not in dumped


def test_no_raw_vendor_payload_column_exists():
    """편하다고 통째로 넣으면 그 순간부터 회수할 수 없다."""
    import re
    from pathlib import Path

    sql = Path("app/db/migrations/versions/M053_sales_claim_and_adjustment.py") \
        .read_text(encoding="utf-8")
    body = re.sub(r'"""[\s\S]*?"""', "", sql, count=1)   # 문서 주석 제외
    for banned in ("raw_json", "raw_payload", "response_body", "JSONB"):
        assert banned not in body, f"{banned} 컬럼이 있다"


# ── no-loss 되살리기 ────────────────────────────────────────────────────

def _open_issues(db, org, kind=None):
    sql = """
        SELECT issue_type, status, year_month, title FROM operation_issue
         WHERE org_id = :o AND deleted_at IS NULL
           AND status = ANY(ARRAY['OPEN','IN_PROGRESS','WAITING'])
    """
    params = {"o": str(org)}
    if kind:
        sql += " AND issue_type = :k"
        params["k"] = kind
    return db.execute(text(sql), params).mappings().all()


def test_a_claim_without_its_order_is_held_and_shown(
    db, org, actor, ne_account, market,
):
    """원 주문이 아직 없다. 조용히 버리면 그 반품은 영원히 장부에 없다."""
    _ingest(db, org, actor, ne_account,
            [_claim(ne_order_no="아직없음", mall_order_no="아직없음",
                    cancel_price="30000")])

    issues = _open_issues(db, org, ci.ISSUE_UNMATCHED_ORDER)
    assert len(issues) == 1
    # 되살릴 좌표가 남아야 한다 — 어느 구간을 다시 읽어야 하는지.
    assert issues[0]["year_month"] == "2026-08"
    assert _adjustments(db, org) == []


def test_the_claim_is_recovered_once_its_order_arrives(
    db, org, actor, ne_account, market,
):
    """주문이 늦게 들어와도 클레임이 되살아난다.

    이게 없으면 주문 동기화가 잠깐 밀린 사이 도착한 클레임의 매출 조정이
    영영 안 만들어진다.
    """
    claim = _claim(ne_order_no="늦은주문", mall_order_no="늦은번호",
                   cancel_price="30000")
    _ingest(db, org, actor, ne_account, [claim])
    assert len(_open_issues(db, org, ci.ISSUE_UNMATCHED_ORDER)) == 1

    # 주문이 뒤늦게 들어왔다.
    _api_order(db, org, business_key="NE:늦은주문", order_no="늦은번호")

    result = _ingest(db, org, actor, ne_account, [claim])

    assert result.linked == 1
    assert result.recovered == 1
    assert _open_issues(db, org, ci.ISSUE_UNMATCHED_ORDER) == []
    assert _adjustments(db, org)[0]["amount_delta"] == -30000


def test_the_issue_is_not_closed_until_the_money_is_actually_booked(
    db, org, actor, ne_account, market,
):
    """연결만 되고 금액을 못 정했으면 아직 해결이 아니다.

    닫아 버리면 목록은 깨끗해지고 매출은 비어 있다.
    """
    claim = _claim(ne_order_no="부분", mall_order_no="부분번호",
                   order_cnt=3, order_price=90000,
                   cancel_cnt="1", cancel_price="0")
    _ingest(db, org, actor, ne_account, [claim])
    _api_order(db, org, business_key="NE:부분", order_no="부분번호",
               sale_price=90000, quantity=3)

    result = _ingest(db, org, actor, ne_account, [claim])

    assert result.linked == 1
    assert result.recovered == 0                  # 아직 닫지 않았다
    assert len(_open_issues(db, org, ci.ISSUE_FINANCIAL_UNRESOLVED)) == 1
    assert _adjustments(db, org) == []


def test_repeated_holds_do_not_pile_up_issues(db, org, actor, ne_account, market):
    """같은 클레임을 열 번 읽어도 이슈는 하나다."""
    claim = _claim(ne_order_no="없음", mall_order_no="없음", cancel_price="30000")
    for _ in range(10):
        _ingest(db, org, actor, ne_account, [claim])

    assert len(_open_issues(db, org, ci.ISSUE_UNMATCHED_ORDER)) == 1


def test_a_human_ignored_issue_is_not_reopened_but_data_still_recovers(
    db, org, actor, ne_account, market,
):
    """경고를 그만 보는 것과 매출을 버리는 것은 다른 결정이다."""
    claim = _claim(ne_order_no="무시", mall_order_no="무시번호",
                   cancel_price="30000")
    _ingest(db, org, actor, ne_account, [claim])
    db.execute(text("""
        UPDATE operation_issue SET status='IGNORED', resolved_at=now(),
               resolution_note='당분간 보지 않는다'
         WHERE org_id = :o AND issue_type = :k
    """), {"o": str(org), "k": ci.ISSUE_UNMATCHED_ORDER})
    db.commit()

    _api_order(db, org, business_key="NE:무시", order_no="무시번호")
    _ingest(db, org, actor, ne_account, [claim])

    # 경고를 다시 띄우지 않는다.
    assert _open_issues(db, org, ci.ISSUE_UNMATCHED_ORDER) == []
    # 그래도 매출은 되살아났다.
    assert _adjustments(db, org)[0]["amount_delta"] == -30000


def test_the_issue_carries_no_personal_data(db, org, actor, ne_account, market):
    _ingest(db, org, actor, ne_account, [
        _claim(ne_order_no="없음", mall_order_no="없음", cancel_price="30000",
               buyer_name="홍길동", receiver_addr="서울시 강남구",
               receiver_hp_phone="010-1234-5678"),
    ])

    blob = " ".join(
        f"{i['title']} {i['issue_type']}" for i in _open_issues(db, org))
    for leaked in ("홍길동", "강남구", "010-1234-5678"):
        assert leaked not in blob


# ── 유효 매출 읽기 모델 ─────────────────────────────────────────────────

def test_effective_sales_separates_gross_from_adjustment(
    db, org, actor, ne_account, market,
):
    """총매출과 조정을 한 칸에 뭉치면 무엇이 왜 줄었는지 설명하지 못한다."""
    _api_order(db, org, sale_price=30000)
    _ingest(db, org, actor, ne_account, [_claim(cancel_price="30000")])

    view = ci.effective_sales(db, org_id=org, year_month="2026-08")

    assert view["gross_sales"] == 30000
    assert view["claim_adjustment"] == -30000
    assert view["effective_sales"] == 0
    # 매출은 확정할 수 있지만 순이익은 아직 모른다.
    assert view["profit_complete"] is False


def test_effective_sales_never_claims_a_complete_profit(
    db, org, actor, ne_account, market,
):
    """반품 원가·수수료 환급을 모르는 채로 "마진 100%" 를 만들지 않는다."""
    _api_order(db, org)
    _ingest(db, org, actor, ne_account, [_claim("19", return_price="30000")])

    view = ci.effective_sales(db, org_id=org, year_month="2026-08")
    assert view["profit_complete"] is False
    assert view["profit_incomplete_reason"]
