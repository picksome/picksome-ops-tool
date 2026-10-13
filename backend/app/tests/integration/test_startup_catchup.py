"""기동 따라잡기 (Local Operation Mode).

무엇을 고정하나
--------------
개인 PC 운영에서는 전원을 끄면 자동화가 멈춘다. 그건 정상이다. 문제는
**다시 켰을 때 그 사이가 메워지는가** 이고, 이 파일이 그것만 고정한다.

```
1. 창은 좁아지지 않는다      마지막 성공이 방금이어도 겹쳐읽기 폭은 그대로
2. 창은 넓어진다             18일 꺼져 있었으면 18일 + 겹쳐읽기를 덮는다
3. cutover 아래로 안 내려간다 오래 꺼져 있어도 초기 장부를 다시 안 읽는다
4. 요청은 10일을 안 넘는다    벤더 한도. 창 개수로 확인한다
5. 다시 돌려도 중복 0        멱등. 이게 없으면 겹쳐읽기 자체가 위험하다
6. 부분 실패는 성공이 아니다   못 읽은 구간이 다음 창에 다시 들어와야 한다
7. 클레임도 같다             단, 마지막 성공은 CLAIMS 실행에서만 읽는다
8. 오늘 리포트는 하루 한 번   기동을 두 번 해도 두 개가 안 생긴다
```

2 가 이 파일의 핵심이다
--------------------
예전에는 창이 항상 `now - overlap` 이었다. 겹쳐읽기가 3일인데 PC 를 18일
꺼 두면 그 사이 결제된 주문을 **영원히** 읽지 않는다. 실패로 기록되지도
않는다 — 조용히 없는 매출이 된다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.connectors.next_engine.client import split_windows
from app.connectors.next_engine.normalize import normalize_order_line
from app.services import automation as auto
from app.services import claim_sync as csync
from app.services import next_engine_order_sync as nes
from app.services import order_sync as osync
from app.tests.fixtures.factories import (
    make_app_user,
    make_integration_account,
    make_organization,
    make_product,
)

#: "지금". PC 를 다시 켠 시각이라고 생각하면 된다.
NOW = datetime(2026, 8, 20, 9, 0, 0)
CUTOVER = "2026-06-01"
OVERLAP = 3
#: PC 가 꺼져 있던 기간. 10일(벤더 한도)보다 확실히 길게 잡는다.
OFFLINE_DAYS = 18

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


def _on(day: date, order_no: str):
    """그 날짜에 결제된 주문 한 줄."""
    stamp = f"{day.isoformat()} 10:00:00"
    return _line(ne_order_no=order_no, mall_order_no=f"MALL-{order_no}",
                 order_date=stamp, pay_date=stamp)


class FakeClient:
    """결정적 벤더. 요청한 창에 걸리는 것만 돌려준다."""

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
def config(ne_account) -> osync.AccountConfig:
    return osync.AccountConfig(
        external_account_id=ne_account,
        initial_sync_from=date.fromisoformat(CUTOVER),
        overlap_days=OVERLAP)


def _set_last_success(db, account_id, when: datetime | None) -> None:
    db.execute(text("""
        UPDATE external_account SET last_successful_sync_at = :t WHERE id = :i
    """), {"t": when, "i": str(account_id)})
    db.commit()


def _orders(db, org) -> list[str]:
    return db.execute(text("""
        SELECT business_key FROM sales_order
         WHERE org_id = :o AND deleted_at IS NULL ORDER BY business_key
    """), {"o": str(org)}).scalars().all()


def _sync(db, org, actor, ne_account, config, client, *, now=NOW, **kw):
    return osync.sync_account(
        db, org_id=org, external_account_id=ne_account, config=config,
        actor_user_id=actor, now=now, client=client, **kw)


# ── 1) 창은 좁아지지 않는다 ─────────────────────────────────────────────

def test_a_recent_success_does_not_shrink_the_window(config):
    """마지막 성공이 방금이어도 겹쳐 읽기 폭은 그대로다.

    좁아지면 watermark 가 되고, 수정일이 없는 벤더에서 watermark 는
    "이미 받은 주문의 변경" 을 영원히 못 본다는 뜻이다.
    """
    start, end = osync.compute_window(
        now=NOW, config=config, max_window_days=10,
        last_success_at=NOW - timedelta(minutes=5))

    assert end == NOW
    assert start == NOW - timedelta(minutes=5) - timedelta(days=OVERLAP)
    assert start <= NOW - timedelta(days=OVERLAP)


def test_no_recorded_success_behaves_exactly_as_before(config):
    """처음 도는 계정의 동작을 바꾸지 않는다."""
    with_none = osync.compute_window(
        now=NOW, config=config, max_window_days=10, last_success_at=None)

    assert with_none == (NOW - timedelta(days=OVERLAP), NOW)


def test_a_future_success_timestamp_cannot_move_the_window_forward(config):
    """이상한 데이터가 창을 앞으로 밀지 못한다.

    시계가 틀어졌거나 손으로 고친 행이 미래를 가리키면, 그대로 쓰면 창이
    비어 버리고 아무것도 안 읽는다. `min` 이 그걸 막는다.
    """
    start, _ = osync.compute_window(
        now=NOW, config=config, max_window_days=10,
        last_success_at=NOW + timedelta(days=30))

    assert start == NOW - timedelta(days=OVERLAP)


# ── 2) 창은 넓어진다 ────────────────────────────────────────────────────

def test_an_18_day_outage_is_covered_by_the_window(config):
    """PC 가 18일 꺼져 있었으면 그 18일이 창에 들어와야 한다."""
    last = NOW - timedelta(days=OFFLINE_DAYS)

    start, end = osync.compute_window(
        now=NOW, config=config, max_window_days=10, last_success_at=last)

    assert end == NOW
    assert start == last - timedelta(days=OVERLAP)
    assert (end - start).days == OFFLINE_DAYS + OVERLAP


def test_a_timezone_aware_success_is_compared_against_a_naive_now(config):
    """DB 의 `timestamptz` 와 코드의 naive `now` 를 섞어도 죽지 않는다.

    맞춰 주지 않으면 `TypeError` 로 죽거나 — 더 나쁘게는 — 9시간 어긋난
    창이 조용히 만들어진다.
    """
    aware = (NOW - timedelta(days=OFFLINE_DAYS)).replace(tzinfo=UTC)

    start, _ = osync.compute_window(
        now=NOW, config=config, max_window_days=10, last_success_at=aware)

    assert start.tzinfo is None
    assert start < NOW - timedelta(days=OFFLINE_DAYS - 1)


# ── 3) cutover 는 여전히 바닥이다 ───────────────────────────────────────

def test_a_very_old_success_still_cannot_reach_below_cutover(ne_account):
    """1년을 꺼 뒀어도 초기 장부 구간을 다시 읽지 않는다."""
    late_cutover = osync.AccountConfig(
        external_account_id=ne_account,
        initial_sync_from=date(2026, 8, 1), overlap_days=OVERLAP)

    start, _ = osync.compute_window(
        now=NOW, config=late_cutover, max_window_days=10,
        last_success_at=NOW - timedelta(days=365))

    assert start.date() == date(2026, 8, 1)


# ── 4) 요청은 벤더 한도를 넘지 않는다 ───────────────────────────────────

def test_a_long_outage_is_split_into_windows_of_at_most_ten_days(config):
    """18일 + 겹쳐읽기 3일 = 21일 → 10일 이하 창 세 개."""
    start, end = osync.compute_window(
        now=NOW, config=config, max_window_days=10,
        last_success_at=NOW - timedelta(days=OFFLINE_DAYS))

    windows = split_windows(start, end)

    assert len(windows) == 3
    assert all((w_end - w_start) <= timedelta(days=10) for w_start, w_end in windows)
    # 빈틈이 없어야 한다. 하나라도 벌어지면 그 사이 주문이 사라진다.
    assert windows[0][0] == start
    assert windows[-1][1] == end
    for earlier, later in zip(windows, windows[1:], strict=False):
        assert earlier[1] == later[0]


# ── 5) 실제로 따라잡고, 다시 돌려도 중복이 없다 ─────────────────────────

def test_orders_from_the_offline_period_are_ingested(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    """§50 — PC 가 18일 꺼져 있던 사이의 주문이 들어온다."""
    make_product(db, org_id=org, business_key="SKU-1")
    last = NOW - timedelta(days=OFFLINE_DAYS)
    _set_last_success(db, ne_account, last)

    # 꺼져 있던 기간 한가운데 · 시작 직후 · 최근. 셋 다 들어와야 한다.
    lines = [
        _on((last + timedelta(days=1)).date(), "NE-GAP-1"),
        _on((last + timedelta(days=9)).date(), "NE-GAP-2"),
        _on((NOW - timedelta(days=1)).date(), "NE-RECENT"),
    ]
    client = FakeClient(lines)

    out = _sync(db, org, actor, ne_account, config, client)

    assert out.status == osync.SUCCESS
    assert out.windows == 3
    assert out.created == 3
    assert _orders(db, org) == ["NE:NE-GAP-1", "NE:NE-GAP-2", "NE:NE-RECENT"]


def test_running_the_catchup_twice_creates_no_duplicates(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    """§17 — 겹쳐 읽기가 안전한 이유가 여기 있다. 없으면 못 쓴다."""
    make_product(db, org_id=org, business_key="SKU-1")
    last = NOW - timedelta(days=OFFLINE_DAYS)
    _set_last_success(db, ne_account, last)
    lines = [_on((last + timedelta(days=2)).date(), "NE-GAP-1")]

    first = _sync(db, org, actor, ne_account, config, FakeClient(lines))
    # 두 번째는 첫 번째가 옮겨 놓은 마지막 성공 시각을 본다.
    second = _sync(db, org, actor, ne_account, config, FakeClient(lines))

    assert first.created == 1
    assert second.created == 0
    assert len(_orders(db, org)) == 1
    assert db.execute(text("""
        SELECT count(*) FROM sales_order
         WHERE org_id = :o AND business_key = 'NE:NE-GAP-1' AND deleted_at IS NULL
    """), {"o": str(org)}).scalar() == 1


def test_the_window_narrows_after_a_successful_catchup(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    """따라잡고 나면 다음 실행은 다시 평소 폭이다.

    안 좁아지면 매 실행이 18일을 다시 읽고, 그 비용을 영원히 낸다.
    """
    make_product(db, org_id=org, business_key="SKU-1")
    _set_last_success(db, ne_account, NOW - timedelta(days=OFFLINE_DAYS))

    first = _sync(db, org, actor, ne_account, config, FakeClient([]))
    later = NOW + timedelta(hours=1)
    second = _sync(db, org, actor, ne_account, config, FakeClient([]), now=later)

    assert first.windows == 3
    assert second.windows == 1


# ── 6) 부분 실패는 마지막 성공을 옮기지 않는다 ──────────────────────────

def test_a_partial_catchup_keeps_the_gap_open_for_the_next_run(
    db, org, actor, ne_account, market_account, mapped_store, config,
):
    """§20·§21 — 일부 창이 실패하면 그 구간이 다음 실행에 다시 들어와야 한다.

    옮겨 버리면 못 읽은 며칠이 영영 안 읽히고, 화면은 방금 성공한 것처럼
    보인다. 가장 나쁜 조합이다.
    """
    import httpx

    make_product(db, org_id=org, business_key="SKU-1")
    last = NOW - timedelta(days=OFFLINE_DAYS)
    _set_last_success(db, ne_account, last)

    # 첫 창은 읽히고 두 번째부터 터진다.
    broken = FakeClient([], fail_with=httpx.ReadTimeout("timeout"), fail_after=1)
    out = _sync(db, org, actor, ne_account, config, broken)

    assert out.status == osync.PARTIAL
    assert out.failed_windows >= 1
    still = db.execute(text("""
        SELECT last_successful_sync_at FROM external_account WHERE id = :i
    """), {"i": str(ne_account)}).scalar()
    assert still is not None
    assert osync.align_tz(still, last) == last

    # 다음 실행의 창이 여전히 그 구멍을 덮는다.
    start, _ = osync.compute_window(
        now=NOW + timedelta(hours=1), config=config, max_window_days=10,
        last_success_at=still)
    assert start <= last


# ── 7) 클레임도 같다 ────────────────────────────────────────────────────

def test_claim_window_widens_after_an_outage(ne_account):
    claim_config = csync.ClaimAccountConfig(
        external_account_id=ne_account,
        initial_sync_from=date.fromisoformat(CUTOVER), lookback_days=20)
    last = NOW - timedelta(days=OFFLINE_DAYS)

    start, end = csync.compute_window(
        now=NOW, config=claim_config, last_success_at=last)

    assert end == NOW
    assert start == last - timedelta(days=20)


def test_claim_last_success_ignores_order_sync_runs(
    db, org, actor, ne_account,
):
    """주문이 성공했다고 클레임이 최신인 것은 아니다.

    `external_account.last_successful_sync_at` 은 주문 동기화가 소유한다.
    클레임이 그 값을 같이 보면 주문만 돌아도 클레임이 최신으로 보이고,
    그 거짓이 M057 이 고친 문제다.
    """
    db.execute(text("""
        INSERT INTO external_sync_run
               (org_id, external_account_id, sync_type, trigger, status,
                started_at, finished_at)
        VALUES (:o, :a, 'ORDERS', 'SCHEDULED', 'SUCCESS', now(), now())
    """), {"o": str(org), "a": str(ne_account)})
    db.commit()

    assert csync.last_success_at(
        db, org_id=org, external_account_id=ne_account) is None


def test_claim_last_success_ignores_partial_runs(db, org, actor, ne_account):
    """`PARTIAL` 은 성공이 아니다. 못 읽은 창이 다시 들어와야 한다."""
    db.execute(text("""
        INSERT INTO external_sync_run
               (org_id, external_account_id, sync_type, trigger, status,
                started_at, finished_at)
        VALUES (:o, :a, 'CLAIMS', 'STARTUP', 'PARTIAL', now(), now())
    """), {"o": str(org), "a": str(ne_account)})
    db.commit()

    assert csync.last_success_at(
        db, org_id=org, external_account_id=ne_account) is None


# ── 8) 동기화 도중 전원이 꺼졌을 때 ────────────────────────────────────

def test_a_sync_interrupted_by_shutdown_is_reclaimed_not_left_running(
    db, org, ne_account,
):
    """§48 — 전원이 꺼져 중단된 실행이 영원히 "조회 중" 으로 남지 않는다.

    개인 PC 운영에서는 동기화 도중 전원이 꺼지는 일이 드물지 않다. advisory
    lock 은 연결이 끊기면 풀리므로 다음 실행이 막히지는 않지만, RUNNING 행이
    남으면 화면은 계속 "지금 조회 중" 이라고 말하고 그게 마지막 시도가 된다.
    """
    db.execute(text("""
        INSERT INTO external_sync_run
               (org_id, external_account_id, sync_type, trigger, status, started_at)
        VALUES (:o, :a, 'CLAIMS', 'STARTUP', 'RUNNING',
                now() - make_interval(hours => 5))
    """), {"o": str(org), "a": str(ne_account)})
    db.commit()

    assert osync.reclaim_stale_sync_runs(db) >= 1

    row = db.execute(text("""
        SELECT status, error_code, finished_at, read_count FROM external_sync_run
         WHERE org_id = :o AND sync_type = 'CLAIMS' ORDER BY started_at DESC LIMIT 1
    """), {"o": str(org)}).mappings().one()
    assert row["status"] == "FAILED"
    assert row["error_code"] == osync.STALE_RUN
    assert row["finished_at"] is not None
    # 몇 건을 읽었는지는 알 수 없다. 0 으로 덮으면 "0건 읽고 끝" 처럼 보인다.
    assert row["read_count"] is None


def test_a_sync_that_just_started_is_not_reclaimed(db, org, ne_account):
    """지금 돌고 있는 실행을 죽이지 않는다."""
    db.execute(text("""
        INSERT INTO external_sync_run
               (org_id, external_account_id, sync_type, trigger, status, started_at)
        VALUES (:o, :a, 'ORDERS', 'STARTUP', 'RUNNING', now())
    """), {"o": str(org), "a": str(ne_account)})
    db.commit()

    osync.reclaim_stale_sync_runs(db)

    assert db.execute(text("""
        SELECT status FROM external_sync_run
         WHERE org_id = :o AND sync_type = 'ORDERS' ORDER BY started_at DESC LIMIT 1
    """), {"o": str(org)}).scalar() == "RUNNING"


# ── 9) 기동 실행 기록 · 리포트 중복 ─────────────────────────────────────

def test_startup_runs_are_recorded_as_startup_not_scheduled(
    db, org, actor, ne_account, market_account, mapped_store,
):
    """§M058 — "PC 가 꺼져 있었다" 와 "스케줄러가 멈췄다" 는 다른 사건이다."""
    from app.connectors.bootstrap import register_builtin
    from app.services import automation_runners as runners

    register_builtin()
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="ORDER_SYNC",
        enabled=True, mode="AUTO_EXECUTE", guardrails={"overlap_days": OVERLAP},
        scope={"accounts": [{"external_account_id": str(ne_account),
                             "initial_sync_from": CUTOVER}]})

    runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor,
                       trigger="STARTUP")

    assert db.execute(text("""
        SELECT trigger FROM external_sync_run
         WHERE org_id = :o AND sync_type = 'ORDERS'
         ORDER BY started_at DESC LIMIT 1
    """), {"o": str(org)}).scalar() == "STARTUP"


def test_a_second_startup_on_the_same_day_builds_no_second_report(
    db, org, actor,
):
    """§34 — 같은 KST 날짜에 리포트가 두 개 생기지 않는다."""
    from app.services import automation_runners as runners

    # 리포트는 REPORT_ONLY 만 지원한다 — 만들어 알리는 것 외에 실행할 것이 없다.
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="DAILY_REPORT",
        enabled=True, mode="REPORT_ONLY", scope={})

    runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor,
                       trigger="STARTUP")
    runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor,
                       trigger="STARTUP")

    built = db.execute(text("""
        SELECT count(*)
          FROM automation_run r
          JOIN automation_policy p ON p.id = r.policy_id
          JOIN automation_action a ON a.run_id = r.id
         WHERE r.org_id = :o AND p.automation_type = 'DAILY_REPORT'
           AND a.action_type = 'DAILY_REPORT_BUILT' AND a.outcome = 'REPORTED'
    """), {"o": str(org)}).scalar()

    assert built == 1
