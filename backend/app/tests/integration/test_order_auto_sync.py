"""주문 자동 동기화 (Phase 2).

이 파일이 고정하는 것
------------------
1. 겹쳐 읽기 — 수정일이 없으므로 매번 최근 구간을 통째로 다시 읽는다
2. cutover 아래로 내려가지 않는다
3. 같은 계정에 대해 **동시에 하나만** 돈다 (수동·자동이 같은 문을 쓴다)
4. 실패·부분 실행은 "마지막 성공" 을 앞으로 옮기지 않는다
5. 미매핑 1건이 나머지 주문의 적재를 막지 않는다
6. 매핑이 생기면 **창 밖으로 밀려난 주문까지** 되살아난다
7. 되살리기가 실패하면 이슈를 닫지 않는다
8. 꺼져 있으면 외부를 부르지 않는다
9. 조직을 넘지 않는다

6 이 이 파일의 핵심이다
--------------------
자동 동기화는 최근 며칠만 본다. 8/1 주문이 미매핑으로 보류됐는데 8/20 에
상품을 연결하면, 그 주문은 이미 일반 창 밖이다. 되살리기가 없으면 그 매출은
영원히 장부에 없고, 아무도 그 사실을 모른다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.connectors.next_engine.normalize import normalize_order_line
from app.services import automation as auto
from app.services import next_engine_order_sync as nes
from app.services import order_sync as osync
from app.tests.fixtures.factories import (
    make_app_user,
    make_integration_account,
    make_organization,
    make_product,
)

NOW = datetime(2026, 8, 20, 9, 0, 0)
CUTOVER = "2026-07-26"

ROW = {
    "ne_order_no": "NE-1", "mall_order_no": "MALL-1",
    "mall_cd": "mall02400", "mall_name": "쿠팡", "mall_id": "shop-1",
    "ne_product_cd": "NE-P-1", "ne_partner_product_cd": "SKU-1",
    "ne_stock_cd": "", "product_name": "테스트상품",
    "order_date": "2026-08-18 10:00:00", "pay_date": "2026-08-18 10:05:00",
    "order_status_divi": "04", "order_price": 14000, "order_cnt": 1,
    "sell_price": 12000, "delivery_price": 0,
}


def _line(**over):
    return normalize_order_line(dict(ROW, **over))


class FakeClient:
    """결정적 벤더. **실제 API 를 부르지 않는다.**

    창별로 무엇을 돌려줄지 정한다 — 되살리기가 "일반 창 밖" 을 실제로 다시
    읽는지 확인하려면 창을 구분할 수 있어야 한다.
    """

    max_window_days = 10

    def __init__(self, lines=None, *, fail_with=None, fail_after=0):
        self._lines = list(lines or [])
        self._fail_with = fail_with
        self._fail_after = fail_after
        self.calls: list[tuple[datetime, datetime]] = []

    def fetch_orders(self, *, api_key, start, end, **kwargs):
        self.calls.append((start, end))
        if self._fail_with is not None and len(self.calls) > self._fail_after:
            raise self._fail_with
        # 요청한 창에 걸리는 것만 준다. 벤더가 그렇게 동작한다.
        return [ln for ln in self._lines
                if ln.sale_date and start.date() <= ln.sale_date <= end.date()]


# ── 픽스처 ──────────────────────────────────────────────────────────────

@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


@pytest.fixture
def ne_account(db: Session, org, actor, secret_store) -> uuid.UUID:
    sys_id = db.execute(text("""
        INSERT INTO external_system (code, name, system_type, supports_api_key)
        VALUES ('NEXT_ENGINE', 'NEXT ENGINE', 'SELLER_TOOL', TRUE)
        ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id
    """)).scalar()
    account_id = uuid.UUID(str(db.execute(text("""
        INSERT INTO external_account (org_id, external_system_id, account_code,
               display_name, credential_kind, created_by, updated_by)
        VALUES (:o, :s, 'DEFAULT', 'NE', 'API_KEY', :a, :a) RETURNING id
    """), {"o": str(org), "s": str(sys_id), "a": str(actor)}).scalar()))

    ref = secret_store.put(
        org_id=org, owner_type="EXTERNAL_ACCOUNT", owner_id=account_id,
        credential_kind="API_KEY", payload={"api_key": "test-key"})
    db.execute(text("""
        UPDATE external_account SET credential_secret_ref = :r WHERE id = :i
    """), {"r": ref, "i": str(account_id)})
    db.commit()
    return account_id


@pytest.fixture
def market_account(db: Session, org) -> uuid.UUID:
    return make_integration_account(db, org_id=org, provider="COUPANG",
                                    account_label="쿠팡 본점")


@pytest.fixture
def mapped_store(db: Session, org, ne_account, market_account):
    db.execute(text("""
        INSERT INTO external_entity_mapping
               (org_id, external_account_id, external_entity_type, external_entity_id,
                internal_entity_type, internal_entity_id, mapping_status, first_seen_at)
        VALUES (:o, :a, :et, 'mall02400', :et, :iid, 'ACTIVE', now())
    """), {"o": str(org), "a": str(ne_account),
           "et": nes.ENTITY_MARKETPLACE_ACCOUNT, "iid": str(market_account)})
    db.commit()


@pytest.fixture
def registered():
    from app.connectors.bootstrap import register_builtin

    register_builtin()


@pytest.fixture
def config(ne_account) -> osync.AccountConfig:
    from datetime import date

    return osync.AccountConfig(
        external_account_id=ne_account,
        initial_sync_from=date.fromisoformat(CUTOVER),
        overlap_days=3,
    )


#: 보류가 생긴 날. 그 시점에는 주문이 겹쳐 읽기 창 안에 있었다.
DAY_1 = datetime(2026, 8, 2, 9, 0, 0)


def _sync(db, org, actor, ne_account, config, client, *, now=NOW, **kw):
    return osync.sync_account(
        db, org_id=org, external_account_id=ne_account, config=config,
        actor_user_id=actor, now=now, client=client, **kw)


def _orders(db, org) -> list[str]:
    return db.execute(text("""
        SELECT business_key FROM sales_order
         WHERE org_id = :o AND deleted_at IS NULL ORDER BY business_key
    """), {"o": str(org)}).scalars().all()


def _open_issues(db, org):
    return db.execute(text("""
        SELECT id, status, year_month FROM operation_issue
         WHERE org_id = :o AND issue_type = :t AND deleted_at IS NULL
           AND status = ANY(ARRAY['OPEN','IN_PROGRESS','WAITING'])
    """), {"o": str(org), "t": nes.ISSUE_UNMAPPED_PRODUCT}).mappings().all()


# ── 1·2) 창 계산 ────────────────────────────────────────────────────────

def test_overlap_window_is_reread_every_time(config):
    """지난번 성공 이후만 읽지 않는다.

    NEXT ENGINE 에는 수정일이 없다. "새로 생긴 것" 만 읽으면 이미 받은 주문이
    나중에 바뀌어도 영원히 모른다.
    """
    start, end = osync.compute_window(now=NOW, config=config, max_window_days=10)
    assert end == NOW
    assert start == NOW - timedelta(days=3)


def test_window_never_goes_below_cutover(ne_account):
    """겹쳐 읽기 폭이 넓어도 cutover 아래로는 안 내려간다.

    내려가면 자동 실행이 매번 과거를 다시 읽고, 엑셀로 들어온 초기 장부와
    같은 주문을 두고 경쟁한다.
    """
    from datetime import date

    wide = osync.AccountConfig(
        external_account_id=ne_account,
        initial_sync_from=date(2026, 8, 18), overlap_days=10)
    start, _ = osync.compute_window(now=NOW, config=wide, max_window_days=10)
    assert start.date() == date(2026, 8, 18)


# ── 3) single-flight ────────────────────────────────────────────────────

def test_second_run_on_the_same_account_is_refused(db, ne_account):
    """수동과 자동이 **같은 문**을 지난다. 자동만 잠그면 겹친다."""
    with osync.single_flight(db, ne_account):
        with pytest.raises(osync.OrderSyncAlreadyRunningError):
            with osync.single_flight(db, ne_account):
                pass


def test_a_different_account_is_not_blocked(db, ne_account):
    """한 계정이 느리다고 다른 계정까지 밀리면 안 된다."""
    other = uuid.uuid4()
    with osync.single_flight(db, ne_account):
        with osync.single_flight(db, other):
            pass


# ── 4·5) 실행 ───────────────────────────────────────────────────────────

def test_mapped_orders_are_ingested_and_run_is_recorded(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    make_product(db, org_id=org, business_key="SKU-1")
    client = FakeClient([_line()])

    out = _sync(db, org, actor, ne_account, config, client)

    assert out.status == osync.SUCCESS
    assert out.created == 1
    assert _orders(db, org) == ["NE:NE-1"]

    run = db.execute(text("""
        SELECT status, sync_type, trigger, read_count, created_count
          FROM external_sync_run WHERE org_id = :o
    """), {"o": str(org)}).mappings().one()
    assert run["status"] == "SUCCESS"
    assert run["sync_type"] == "ORDERS"
    assert run["created_count"] == 1


def test_second_run_creates_nothing(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    make_product(db, org_id=org, business_key="SKU-1")
    client = FakeClient([_line()])

    _sync(db, org, actor, ne_account, config, client)
    second = _sync(db, org, actor, ne_account, config, client)

    assert second.created == 0
    assert second.unchanged == 1
    assert len(_orders(db, org)) == 1


def test_unmapped_product_does_not_stop_the_others(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    make_product(db, org_id=org, business_key="SKU-1")
    client = FakeClient([
        _line(),
        _line(ne_order_no="NE-2", mall_order_no="MALL-2",
              ne_product_cd="NE-P-9", ne_partner_product_cd="없는SKU"),
    ])

    out = _sync(db, org, actor, ne_account, config, client)

    assert out.created == 1
    assert _orders(db, org) == ["NE:NE-1"]
    assert len(_open_issues(db, org)) == 1


# ── 6·7) no-loss 되살리기 ───────────────────────────────────────────────

def test_mapping_recovers_an_order_that_fell_out_of_the_window(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    """이 파일의 핵심.

    6/3 주문이 미매핑으로 보류된다. 8/20 에 상품을 연결하면 그 주문은 이미
    겹쳐 읽기 창(3일) 밖이다. 되살리기가 없으면 영원히 장부에 없다.
    """
    old = _line(ne_order_no="NE-OLD", mall_order_no="MALL-OLD",
                ne_product_cd="NE-P-9", ne_partner_product_cd="없는SKU",
                order_date="2026-08-01 10:00:00", pay_date="2026-08-01 10:00:00")
    client = FakeClient([old])

    # 1) 8/2 에는 창 안이라 읽히고, 미매핑이라 보류된다.
    _sync(db, org, actor, ne_account, config, client, now=DAY_1)
    assert _orders(db, org) == []
    issue = _open_issues(db, org)[0]
    assert issue["year_month"] == "2026-08"

    # 2) 일반 창(8/17~8/20)은 이 주문을 덮지 못한다.
    start, _ = osync.compute_window(now=NOW, config=config, max_window_days=10)
    assert old.sale_date < start.date()

    # 3) 사용자가 상품을 연결한다.
    pid = make_product(db, org_id=org, business_key="아무거나")
    nes.map_product_to_internal(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        ne_product_cd="NE-P-9", product_id=pid)
    assert len(_open_issues(db, org)) == 1  # 저장만으로 닫히지 않는다

    # 4) 다음 동기화가 그 구간을 겨냥해 다시 읽는다.
    out = _sync(db, org, actor, ne_account, config, client)

    assert out.replayed == 1
    assert _orders(db, org) == ["NE:NE-OLD"]
    assert _open_issues(db, org) == []


def test_replay_is_idempotent(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    old = _line(ne_order_no="NE-OLD", mall_order_no="MALL-OLD",
                ne_product_cd="NE-P-9", ne_partner_product_cd="없는SKU",
                order_date="2026-08-01 10:00:00", pay_date="2026-08-01 10:00:00")
    client = FakeClient([old])
    _sync(db, org, actor, ne_account, config, client, now=DAY_1)
    pid = make_product(db, org_id=org, business_key="아무거나")
    nes.map_product_to_internal(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        ne_product_cd="NE-P-9", product_id=pid)

    _sync(db, org, actor, ne_account, config, client)
    third = _sync(db, org, actor, ne_account, config, client)

    assert third.created == 0
    assert len(_orders(db, org)) == 1
    dup = db.execute(text("""
        SELECT count(*) FROM (
          SELECT org_id, marketplace, marketplace_order_no, line_no
            FROM sales_order WHERE org_id = :o AND deleted_at IS NULL
           GROUP BY 1,2,3,4 HAVING count(*) > 1) d
    """), {"o": str(org)}).scalar()
    assert dup == 0


def test_replay_failure_keeps_the_mapping_and_the_issue(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    """되살리기가 실패해도 매핑은 되돌리지 않는다 — 매핑이 틀린 게 아니다.

    중간에 성공한 동기화를 한 번 끼우는 이유
    -----------------------------------
    창이 **마지막 성공 시각**을 기준으로 넓어지므로(기동 따라잡기), 보류가
    생긴 직후에 바로 다시 돌리면 그 주문이 일반 창 안에 들어온다. 그러면
    되살리기 경로를 아예 지나지 않아 이 테스트가 검사하려는 실패가 재현되지
    않는다 — 실제로 그렇게 됐다.

    그건 회귀가 아니라 개선이다. 마지막 성공 이후의 보류 건은 따라잡기가
    알아서 덮는다. **되살리기가 필요한 것은 그 뒤로 성공한 동기화가 쌓여
    창이 앞으로 밀려난 경우**이고, 아래가 바로 그 상황이다.
    """
    from app.connectors.next_engine.client import NextEngineReadError

    old = _line(ne_order_no="NE-OLD", mall_order_no="MALL-OLD",
                ne_product_cd="NE-P-9", ne_partner_product_cd="없는SKU",
                order_date="2026-08-01 10:00:00", pay_date="2026-08-01 10:00:00")
    _sync(db, org, actor, ne_account, config, FakeClient([old]), now=DAY_1)

    # 8/15 에 정상적으로 한 번 더 돈다. 마지막 성공이 여기로 옮겨가면서
    # 8/1 주문은 이제 어떤 일반 창에도 들어오지 않는다.
    _sync(db, org, actor, ne_account, config, FakeClient([]),
          now=datetime(2026, 8, 15, 9, 0, 0))
    start, _ = osync.compute_window(
        now=NOW, config=config, max_window_days=10,
        last_success_at=datetime(2026, 8, 15, 9, 0, 0))
    assert old.sale_date < start.date()

    pid = make_product(db, org_id=org, business_key="아무거나")
    nes.map_product_to_internal(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        ne_product_cd="NE-P-9", product_id=pid)

    # 일반 창은 성공하고 되살리기 창에서 죽는다.
    broken = FakeClient(
        [old],
        fail_with=NextEngineReadError("실패", detail={"http_status": 500}),
        fail_after=1)
    out = _sync(db, org, actor, ne_account, config, broken)

    assert out.replayed == 0
    assert len(_open_issues(db, org)) == 1      # 닫지 않았다
    assert db.execute(text("""
        SELECT count(*) FROM external_entity_mapping
         WHERE external_account_id = :a AND external_entity_type = 'PRODUCT'
           AND deleted_at IS NULL
    """), {"a": str(ne_account)}).scalar() == 1  # 매핑은 그대로


def test_replay_reaches_data_a_human_had_ignored(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    """경고를 그만 보는 것과 매출을 버리는 것은 다른 결정이다."""
    old = _line(ne_order_no="NE-OLD", mall_order_no="MALL-OLD",
                ne_product_cd="NE-P-9", ne_partner_product_cd="없는SKU",
                order_date="2026-08-01 10:00:00", pay_date="2026-08-01 10:00:00")
    client = FakeClient([old])
    _sync(db, org, actor, ne_account, config, client, now=DAY_1)
    db.execute(text("""
        UPDATE operation_issue SET status='IGNORED', resolved_at=now(),
               resolution_note='당분간 보지 않는다'
         WHERE org_id = :o AND issue_type = :t
    """), {"o": str(org), "t": nes.ISSUE_UNMAPPED_PRODUCT})
    pid = make_product(db, org_id=org, business_key="아무거나")
    nes.map_product_to_internal(
        db, org_id=org, actor_user_id=actor, external_account_id=ne_account,
        ne_product_cd="NE-P-9", product_id=pid)

    out = _sync(db, org, actor, ne_account, config, client)

    assert out.replayed == 1
    assert _orders(db, org) == ["NE:NE-OLD"]
    # 경고를 다시 띄우지 않는다 — 열린 이슈는 없다.
    assert _open_issues(db, org) == []


# ── 4) 실패는 마지막 성공을 옮기지 않는다 ────────────────────────────────

@pytest.mark.parametrize(("detail", "expected"), [
    ({"http_status": 401}, osync.AUTH_FAILED),
    ({"http_status": 403}, osync.AUTH_FAILED),
    ({"http_status": 429}, osync.RATE_LIMITED),
    ({"http_status": 500}, osync.SERVER_ERROR),
    ({"http_status": 422}, osync.SCHEMA_CHANGED),
    ({"cause": "ReadTimeout"}, osync.TIMEOUT),
])
def test_failures_are_classified_not_lumped(detail, expected):
    """전부 "동기화 실패" 로 뭉치면 화면을 봐도 할 일을 모른다."""
    from app.connectors.next_engine.client import NextEngineReadError

    assert osync.classify_error(
        NextEngineReadError("실패", detail=detail)) == expected


def test_pagination_stall_is_its_own_category():
    from app.connectors.next_engine.client import NextEnginePaginationError

    assert osync.classify_error(
        NextEnginePaginationError("멈춤")) == osync.PAGINATION_STALLED


def test_a_failed_run_does_not_advance_last_success(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    """실패를 마지막 성공으로 적으면, 몇 시간째 못 읽고 있는 상태가 숨는다."""
    from app.connectors.next_engine.client import NextEngineReadError

    client = FakeClient(
        [], fail_with=NextEngineReadError("거부", detail={"http_status": 401}))
    out = _sync(db, org, actor, ne_account, config, client)

    assert out.status in (osync.PARTIAL, osync.FAILED)
    assert out.reason == osync.AUTH_FAILED
    assert db.execute(text("""
        SELECT last_successful_sync_at FROM external_account WHERE id = :i
    """), {"i": str(ne_account)}).scalar() is None


def test_auth_failure_stops_calling_the_vendor(
    db, org, actor, ne_account, market_account, mapped_store,
):
    """자격증명이 거부됐는데 창마다 다시 부르면 잠금 정책에 걸린다."""
    from datetime import date

    from app.connectors.next_engine.client import NextEngineReadError

    wide = osync.AccountConfig(
        external_account_id=ne_account,
        initial_sync_from=date(2026, 6, 1), overlap_days=10)
    client = FakeClient(
        [], fail_with=NextEngineReadError("거부", detail={"http_status": 403}))
    _sync(db, org, actor, ne_account, wide, client)

    assert len(client.calls) == 1


def test_a_successful_run_records_the_evidence(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    make_product(db, org_id=org, business_key="SKU-1")
    _sync(db, org, actor, ne_account, config, FakeClient([_line()]))

    assert db.execute(text("""
        SELECT last_successful_sync_at FROM external_account WHERE id = :i
    """), {"i": str(ne_account)}).scalar() is not None


# ── 8) 꺼져 있으면 부르지 않는다 ─────────────────────────────────────────

def test_disabled_policy_makes_no_external_call(
    db, org, actor, ne_account, market_account, mapped_store, registered,
):
    from app.services import automation_runners as runners

    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="ORDER_SYNC",
        enabled=False, mode="AUTO_EXECUTE", guardrails={"overlap_days": 3},
        scope={"accounts": [{"external_account_id": str(ne_account),
                             "initial_sync_from": CUTOVER}]})
    runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)

    assert db.execute(text("""
        SELECT count(*) FROM external_sync_run WHERE org_id = :o
    """), {"o": str(org)}).scalar() == 0


# ── 9) 조직 격리 ────────────────────────────────────────────────────────

def test_the_worker_never_reaches_another_orgs_account(db, org, actor, ne_account):
    """자동 실행에는 요청 맥락이 없다. 질의 자체가 조직으로 좁혀져야 한다."""
    other_org = make_organization(db)
    visible = {str(a["id"]) for a in osync.ready_accounts(db, org_id=other_org)}

    assert str(ne_account) not in visible


def test_config_pointing_at_a_foreign_account_is_skipped(
    db, org, actor, ne_account, market_account, mapped_store, registered,
):
    other_org = make_organization(db)
    policy = {
        "id": str(uuid.uuid4()), "automation_type": "ORDER_SYNC",
        "enabled": True, "mode": "AUTO_EXECUTE",
        "guardrail_json": {"overlap_days": 3},
        "scope_json": {"accounts": [{"external_account_id": str(ne_account),
                                     "initial_sync_from": CUTOVER}]},
    }
    outcomes = osync.sync_org(
        db, org_id=other_org, policy=policy, actor_user_id=actor, now=NOW,
        client=FakeClient([_line()]))

    assert [o.status for o in outcomes] == [osync.SKIPPED]
    assert outcomes[0].reason == osync.NOT_CONFIGURED


# ── 설정 ────────────────────────────────────────────────────────────────

def test_missing_overlap_means_the_automation_does_not_run():
    """기본값으로 채우면 사용자가 정하지 않은 범위로 외부를 두들긴다."""
    assert osync.read_config({"guardrail_json": {}, "scope_json": {}}) == {}


def test_half_filled_account_config_is_rejected_not_ignored():
    """조용히 무시하면 사용자는 켰다고 믿는다."""
    from app.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        osync.read_config({
            "guardrail_json": {"overlap_days": 3},
            "scope_json": {"accounts": [{"external_account_id": str(uuid.uuid4())}]},
        })


def test_the_counts_reconcile_and_do_not_double_count(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    """읽은 수 = 관문에서 멈춘 수 + 처리한 수.

    `gate_total` 에는 `eligible` 이 들어 있다. 그대로 "보호됨" 으로 세면
    정상 적재된 주문까지 두 번 세어 화면 숫자가 부풀어 오른다.
    """
    make_product(db, org_id=org, business_key="SKU-1")
    client = FakeClient([
        _line(),
        _line(ne_order_no="NE-2", mall_order_no="MALL-2",
              ne_product_cd="NE-P-9", ne_partner_product_cd="없는SKU"),
    ])

    out = _sync(db, org, actor, ne_account, config, client)

    handled = out.created + out.updated + out.unchanged + out.conflict + out.rejected
    assert out.fetched == out.protected + handled
    assert out.fetched == 2
    assert out.protected == 1        # 미매핑 1건만
    assert handled == 1


def test_a_manual_run_by_a_service_account_does_not_break_the_run_record(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    """CLI 수동 실행은 사람이 아니라 서비스 계정으로 돈다.

    `external_sync_run.created_by` 는 `app_user` 를 가리킨다. 서비스 계정
    id 를 그대로 넣으면 외래키에서 터진다 — 실행 자체가 못 시작한다.
    """
    from app.services.automation_runners import automation_actor

    service_actor = automation_actor(db, org_id=org)
    make_product(db, org_id=org, business_key="SKU-1")

    out = osync.sync_account(
        db, org_id=org, external_account_id=ne_account, config=config,
        actor_user_id=service_actor, trigger="MANUAL", now=NOW,
        client=FakeClient([_line()]))

    assert out.status == osync.SUCCESS
    row = db.execute(text("""
        SELECT trigger, created_by FROM external_sync_run WHERE org_id = :o
    """), {"o": str(org)}).mappings().one()
    assert row["trigger"] == "MANUAL"
    assert row["created_by"] is None    # 사람이 아니므로 비운다


def test_a_human_manual_run_is_attributed_to_that_person(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    make_product(db, org_id=org, business_key="SKU-1")

    osync.sync_account(
        db, org_id=org, external_account_id=ne_account, config=config,
        actor_user_id=actor, trigger="MANUAL", now=NOW,
        client=FakeClient([_line()]))

    assert db.execute(text("""
        SELECT created_by FROM external_sync_run WHERE org_id = :o
    """), {"o": str(org)}).scalar() == actor


# ── 예외 센터 노출 ──────────────────────────────────────────────────────

def test_auth_failure_is_blocking_in_the_exception_center(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    """자격증명이 거부된 상태를 두면 주문이 계속 안 들어온다."""
    from app.connectors.next_engine.client import NextEngineReadError
    from app.services import exception_center

    _sync(db, org, actor, ne_account, config, FakeClient(
        [], fail_with=NextEngineReadError("거부", detail={"http_status": 401})))

    view = exception_center.build(db, org_id=org)
    item = next(i for i in view["items"]
                if i["kind"] == "ORDER_SYNC_AUTH_FAILED")
    assert item["severity"] == "BLOCKING"
    assert item["fix_link"]


def test_rate_limit_is_a_warning_not_a_blocker(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    """다음 주기에 저절로 다시 시도한다. 사람이 할 일이 없다."""
    from app.connectors.next_engine.client import NextEngineReadError
    from app.services import exception_center

    _sync(db, org, actor, ne_account, config, FakeClient(
        [], fail_with=NextEngineReadError("한도", detail={"http_status": 429})))

    view = exception_center.build(db, org_id=org)
    item = next(i for i in view["items"]
                if i["kind"] == "ORDER_SYNC_RATE_LIMITED")
    assert item["severity"] == "WARNING"


def test_a_later_success_clears_the_earlier_failure(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    """어제 실패하고 오늘 성공했으면 지금은 문제가 없다.

    지난 실패를 계속 띄우면 아무도 목록을 보지 않게 된다.
    """
    from app.connectors.next_engine.client import NextEngineReadError
    from app.services import exception_center

    make_product(db, org_id=org, business_key="SKU-1")
    _sync(db, org, actor, ne_account, config, FakeClient(
        [], fail_with=NextEngineReadError("오류", detail={"http_status": 500})))
    _sync(db, org, actor, ne_account, config, FakeClient([_line()]))

    kinds = [i["kind"] for i in exception_center.build(db, org_id=org)["items"]]
    assert not any(k.startswith("ORDER_SYNC_") for k in kinds)


def test_zero_orders_is_not_reported_as_a_failure(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    """주문이 없는 것과 못 읽은 것은 다르다."""
    from app.services import exception_center

    out = _sync(db, org, actor, ne_account, config, FakeClient([]))

    assert out.status == osync.SUCCESS
    assert out.fetched == 0
    kinds = [i["kind"] for i in exception_center.build(db, org_id=org)["items"]]
    assert not any(k.startswith("ORDER_SYNC_") for k in kinds)
