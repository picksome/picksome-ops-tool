"""미매핑 상품은 **결함이 아니라 운영 예외**다.

이 파일이 고정하는 것
------------------
1. 매핑이 없는 상품 때문에 다른 주문의 적재가 통째로 롤백되지 않는다
2. 막힌 코드가 `operation_issue` 에 남아 **사람이 보는 곳**에 뜬다
3. 예외 센터의 연동 구획이 그걸 실제로 보여 준다
4. 상품을 연결하면 그 이슈가 닫힌다
5. 사람이 닫은 이슈를 다음 동기화가 다시 열지 않는다
6. 이슈에 개인정보·주문번호·상품명을 싣지 않는다

2 가 없으면 매출이 조용히 빠진 채로 달이 마감된다. 적재 결과 dict 는 그
실행에서만 살아 있고 화면은 그걸 못 본다.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.connectors.next_engine.normalize import normalize_order_line
from app.services import exception_center
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
    return make_integration_account(db, org_id=org, provider="COUPANG",
                                    account_label="쿠팡 본점")


@pytest.fixture
def mapped_store(db: Session, org, ne_account, market_account):
    db.execute(text("""
        INSERT INTO external_entity_mapping
               (org_id, external_account_id, external_entity_type, external_entity_id,
                internal_entity_type, internal_entity_id, mapping_status, first_seen_at)
        VALUES (:o, :a, :et, :eid, :et, :iid, 'ACTIVE', now())
    """), {"o": str(org), "a": str(ne_account),
           "et": nes.ENTITY_MARKETPLACE_ACCOUNT, "eid": "mall02400",
           "iid": str(market_account)})


def _open_issues(db, org):
    return db.execute(text("""
        SELECT id, title, description, dedupe_key, severity, status,
               year_month, auto_resolve, created_by_scanner
          FROM operation_issue
         WHERE org_id = :o AND issue_type = :t AND deleted_at IS NULL
           AND status = ANY(ARRAY['OPEN','IN_PROGRESS','WAITING'])
    """), {"o": str(org), "t": nes.ISSUE_UNMAPPED_PRODUCT}).mappings().all()


def _ingest(db, org, actor, ne_account, lines):
    return nes.ingest_orders(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        lines=lines)


# ── 1) 한 건이 막혀도 나머지는 들어온다 ──────────────────────────────────

def test_unmapped_product_does_not_roll_back_the_other_orders(
    db, org, actor, ne_account, market_account, mapped_store,
):
    """미매핑 1건 때문에 성공한 주문까지 되돌리지 않는다.

    전부-아니면-전무로 만들면, 카탈로그가 하나 어긋난 날은 그날 매출이 통째로
    안 들어온다. 그건 안전이 아니라 정지다.
    """
    make_product(db, org_id=org, business_key="SKU-1")

    result = _ingest(db, org, actor, ne_account, [
        _line(),                                          # 매핑됨
        _line(ne_order_no="NE-2", mall_order_no="MALL-2",
              ne_product_cd="NE-P-2",
              ne_partner_product_cd="SKU-없음"),           # 미매핑
    ])

    assert result.created == 1
    assert result.product_unmapped == 1
    assert result.reconciles

    rows = db.execute(text("""
        SELECT business_key FROM sales_order
         WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org)}).scalars().all()
    assert rows == ["NE:NE-1"]


# ── 2·3) 사람이 보는 곳에 뜬다 ───────────────────────────────────────────

def test_unmapped_product_becomes_an_operation_issue(
    db, org, actor, ne_account, market_account, mapped_store,
):
    _ingest(db, org, actor, ne_account,
            [_line(ne_product_cd="NE-P-9", ne_partner_product_cd="SKU-없음")])

    issues = _open_issues(db, org)
    assert len(issues) == 1
    assert "NE-P-9" in issues[0]["title"]
    assert issues[0]["severity"] == "WARNING"
    # 스캐너가 만든 것이 아니므로 스캐너가 자동으로 닫아서도 안 된다.
    assert issues[0]["created_by_scanner"] is False
    assert issues[0]["auto_resolve"] is False


def test_the_exception_center_shows_it(
    db, org, actor, ne_account, market_account, mapped_store,
):
    _ingest(db, org, actor, ne_account,
            [_line(ne_product_cd="NE-P-9", ne_partner_product_cd="SKU-없음")])

    view = exception_center.build(db, org_id=org)
    kinds = [i["kind"] for i in view["items"]]
    assert "UNMAPPED_EXTERNAL_PRODUCT" in kinds

    item = next(i for i in view["items"] if i["kind"] == "UNMAPPED_EXTERNAL_PRODUCT")
    assert item["category"] == "INTEGRATION"
    assert item["severity"] == "WARNING"
    # 목록만 보고 막히지 않게, 무엇을 하면 사라지는지가 붙어 있어야 한다.
    assert item["fix_link"]


def test_repeat_sync_does_not_pile_up_duplicates(
    db, org, actor, ne_account, market_account, mapped_store,
):
    """같은 범위를 다시 돌려도 이슈는 하나다."""
    for _ in range(3):
        _ingest(db, org, actor, ne_account,
                [_line(ne_product_cd="NE-P-9", ne_partner_product_cd="SKU-없음")])

    assert len(_open_issues(db, org)) == 1


# ── 4) 연결하면 닫힌다 ───────────────────────────────────────────────────

def test_mapping_alone_does_not_close_the_issue(
    db, org, actor, ne_account, market_account, mapped_store,
):
    """매핑 저장 ≠ 빠졌던 주문 복구 완료.

    저장 시점에 닫으면 목록은 깨끗해지고 매출은 비어 있다. 자동 동기화는
    최근 며칠만 겹쳐 읽으므로, 이미 창 밖으로 밀려난 주문은 아무도 다시
    읽어 주지 않는다. 이슈가 열려 있는 동안이 곧 "아직 복구 안 됨" 이다.
    """
    _ingest(db, org, actor, ne_account,
            [_line(ne_product_cd="NE-P-9", ne_partner_product_cd="SKU-없음")])
    assert len(_open_issues(db, org)) == 1

    pid = make_product(db, org_id=org, business_key="다른키")
    nes.map_product_to_internal(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        ne_product_cd="NE-P-9", product_id=pid)

    # 열려 있어야 한다. 되살리기는 `order_sync` 가 하고, 닫는 것도 거기서 한다.
    assert len(_open_issues(db, org)) == 1


def test_the_issue_records_where_to_replay_from(
    db, org, actor, ne_account, market_account, mapped_store,
):
    """어디까지 거슬러 읽어야 하는지가 이슈에 남아야 한다.

    이 값이 없으면 매핑이 생겨도 복구 범위를 알 수 없고, 그러면 좁은 겹쳐
    읽기 창 밖의 주문은 영원히 장부에 없다.
    """
    _ingest(db, org, actor, ne_account, [
        _line(ne_product_cd="NE-P-9", ne_partner_product_cd="SKU-없음",
              pay_date="2026-06-03 10:00:00"),
        _line(ne_order_no="NE-2", mall_order_no="MALL-2",
              ne_product_cd="NE-P-9", ne_partner_product_cd="SKU-없음",
              pay_date="2026-08-11 10:00:00"),
    ])

    issue = _open_issues(db, org)[0]
    # **가장 이른** 달이어야 한다. 뒤로 밀면 그 사이 주문을 놓친다.
    assert issue["year_month"] == "2026-06"


# ── 5) 사람이 닫은 것을 다시 열지 않는다 ─────────────────────────────────

def test_a_human_closed_issue_is_not_reopened(
    db, org, actor, ne_account, market_account, mapped_store,
):
    """사람이 "안 할 일" 로 판단한 것을 동기화가 매번 되살리면 목록을 아무도
    보지 않게 된다."""
    _ingest(db, org, actor, ne_account,
            [_line(ne_product_cd="NE-P-9", ne_partner_product_cd="SKU-없음")])
    db.execute(text("""
        UPDATE operation_issue
           SET status = 'IGNORED', resolved_at = now(),
               resolution_note = '단종 상품이라 연결하지 않는다'
         WHERE org_id = :o AND issue_type = :t
    """), {"o": str(org), "t": nes.ISSUE_UNMAPPED_PRODUCT})

    _ingest(db, org, actor, ne_account,
            [_line(ne_product_cd="NE-P-9", ne_partner_product_cd="SKU-없음")])

    assert _open_issues(db, org) == []


# ── 6) 이슈에 개인정보를 싣지 않는다 ─────────────────────────────────────

def test_the_issue_carries_no_order_or_buyer_data(
    db, org, actor, ne_account, market_account, mapped_store,
):
    """이슈 목록은 권한이 더 넓은 화면이다. 여기 흘러들면 회수할 수 없다."""
    _ingest(db, org, actor, ne_account, [
        _line(ne_product_cd="NE-P-9", ne_partner_product_cd="SKU-없음",
              product_name="https://domeggook.com/20396068?from=lstGen"),
    ])

    issue = _open_issues(db, org)[0]
    blob = f"{issue['title']} {issue['description']} {issue['dedupe_key']}"
    for leaked in ("MALL-1", "NE-1", "domeggook", "shop-1", "쿠팡"):
        assert leaked not in blob
