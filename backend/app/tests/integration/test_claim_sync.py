"""클레임 자동 동기화 — 창 전략 · 실패 · 동시성.

이 파일이 고정하는 것
------------------
1. 클레임 창은 **주문 창과 다르다** — 최근 며칠이 아니라 "클레임이 생길 수
   있는 주문 기간" 이다
2. 설정이 없으면 외부를 부르지 않는다
3. 실패·부분 실행은 성공이 아니다
4. 주문 동기화와 **동시에 돌 수 있다** — 서로 막지 않는다
5. 같은 클레임 동기화는 겹치지 않는다
6. 벤더 응답·비밀이 오류에 실리지 않는다

1 이 이 파일의 핵심이다
--------------------
클레임 조회의 날짜는 **주문 날짜**다(실측). 최근 3일만 보면 3일 전 주문의
클레임만 보이고, 오늘 발생한 6월 주문의 반품은 영원히 못 본다.
"""

from __future__ import annotations

import threading
import uuid
from datetime import date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.connectors.next_engine.claim_client import NextEngineClaimClient
from app.connectors.next_engine.client import (
    NextEnginePaginationError,
    NextEngineReadError,
)
from app.services import automation as auto
from app.services import claim_sync as cs
from app.services import order_sync as osync
from app.tests.fixtures.factories import (
    make_app_user,
    make_integration_account,
    make_organization,
)

NOW = datetime(2026, 8, 20, 9, 0, 0)
START = datetime(2026, 8, 10)
END = datetime(2026, 8, 20)


def _client(handler) -> NextEngineClaimClient:
    return NextEngineClaimClient(transport=httpx.MockTransport(handler))


def _row(no: str) -> dict:
    """벤더 응답 한 줄. 개인정보는 넣지 않는다."""
    return {
        "ne_claim_no": no, "ne_order_no": f"O{no}", "mall_order_no": f"M{no}",
        "mall_cd": "mall02400", "mall_name": "쿠팡",
        "order_price": 10000, "order_cnt": 1, "cancel_cnt": "1",
        "cancel_price": "10000", "pay_date": "2026-08-15 10:00:00",
        "claim_status_txt": "취소완료",
    }


# ── 1) 창 전략 ──────────────────────────────────────────────────────────

def test_the_claim_window_looks_back_far_further_than_orders():
    """클레임 조회 날짜는 주문 날짜다. 최근 며칠만 보면 옛 주문의 반품을 못 본다."""
    config = cs.ClaimAccountConfig(
        external_account_id=uuid.uuid4(),
        initial_sync_from=date(2026, 1, 1), lookback_days=90)

    start, end = cs.compute_window(now=NOW, config=config)

    assert end == NOW
    assert start == NOW - timedelta(days=90)


def test_the_claim_window_still_stops_at_the_cutover():
    """되돌아보되 cutover 아래로는 안 내려간다 — 초기 장부와 겹치지 않는다."""
    config = cs.ClaimAccountConfig(
        external_account_id=uuid.uuid4(),
        initial_sync_from=date(2026, 8, 1), lookback_days=90)

    start, _ = cs.compute_window(now=NOW, config=config)

    assert start.date() == date(2026, 8, 1)


def test_a_wide_window_is_split_to_what_the_vendor_accepts():
    """벤더는 한 요청에 10일만 받는다(실측). 90일을 그냥 던지면 400 이다."""
    from app.connectors.next_engine.claim_client import split_claim_windows

    windows = split_claim_windows(NOW - timedelta(days=90), NOW)

    assert len(windows) == 9
    assert all((e - s).days <= 10 for s, e in windows)


def test_missing_lookback_means_the_automation_does_not_run():
    """기본값을 코드에 두지 않는다. 반품 가능 기간은 사업이 정한다."""
    assert cs.read_config({"guardrail_json": {}, "scope_json": {}}) == {}


def test_half_filled_config_is_rejected_not_ignored():
    from app.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        cs.read_config({
            "guardrail_json": {"claim_lookback_days": 30},
            "scope_json": {"accounts": [{"external_account_id": str(uuid.uuid4())}]},
        })


# ── 3) 실패 분류 ────────────────────────────────────────────────────────

@pytest.mark.parametrize(("status", "expected"), [
    (401, "AUTH_FAILED"),
    (403, "AUTH_FAILED"),
    (429, "RATE_LIMITED"),
    (500, "SERVER_ERROR"),
])
def test_claim_http_failures_are_classified(status, expected):
    """전부 "동기화 실패" 로 뭉치면 무엇을 해야 할지 모른다."""
    api = _client(lambda r: httpx.Response(status, json={"code": "E"}))

    with pytest.raises(NextEngineReadError) as err:
        api.fetch_claims(api_key="k", start=START, end=END, divis=("08",))

    assert osync.classify_error(err.value) == expected


def test_a_claim_timeout_is_not_classified_as_a_read_failure():
    """시간 초과는 **기다리면 되는 것**이다. 자격증명 문제와 같은 칸에 두면
    운영자가 키를 다시 발급하러 간다."""
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    api = _client(handler)
    with pytest.raises(NextEngineReadError) as err:
        api.fetch_claims(api_key="k", start=START, end=END, divis=("08",))

    assert osync.classify_error(err.value) == "TIMEOUT"


def test_a_connect_timeout_is_also_a_timeout():
    def handler(request):
        raise httpx.ConnectTimeout("no route", request=request)

    api = _client(handler)
    with pytest.raises(NextEngineReadError) as err:
        api.fetch_claims(api_key="k", start=START, end=END, divis=("08",))

    assert osync.classify_error(err.value) == "TIMEOUT"


def test_a_broken_claim_response_is_schema_changed_not_zero_claims():
    """형식이 깨졌는데 0건이라 하면 "취소 없음" 이 되어 매출이 부풀어 있다."""
    api = _client(lambda r: httpx.Response(200, json={"data": "목록 아님"}))

    with pytest.raises(NextEngineReadError) as err:
        api.fetch_claims(api_key="k", start=START, end=END, divis=("08",))

    assert osync.classify_error(err.value) == "SCHEMA_CHANGED"


def test_a_claim_page_that_never_advances_stops():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"data": [_row("A"), _row("B")]})

    api = _client(handler)
    with pytest.raises(NextEnginePaginationError):
        api.fetch_claims(api_key="k", start=START, end=END, divis=("08",), size=2)

    assert calls["n"] < 10


def test_a_short_claim_page_ends_normally():
    """실측한 종료 규칙 — 받은 수가 요청한 수보다 적으면 마지막."""
    pages = [[_row("A"), _row("B")], [_row("C")]]

    def handler(request):
        return httpx.Response(200, json={"data": pages.pop(0) if pages else []})

    api = _client(handler)
    claims = api.fetch_claims(api_key="k", start=START, end=END,
                              divis=("08",), size=2)
    assert len(claims) == 3


# ── 6) 비밀 · 개인정보 ──────────────────────────────────────────────────

def test_the_api_key_never_appears_in_a_claim_failure():
    api = _client(lambda r: httpx.Response(500, json={"code": "E"}))

    with pytest.raises(NextEngineReadError) as err:
        api.fetch_claims(api_key="super-secret", start=START, end=END,
                         divis=("08",))

    blob = f"{err.value} {getattr(err.value, 'detail', {})}"
    assert "super-secret" not in blob


def test_the_vendor_body_is_not_carried_into_the_claim_error():
    """클레임 응답에는 수취인·주소가 있다. 오류 본문에 섞여 오는 일이 있다."""
    api = _client(lambda r: httpx.Response(
        500, json={"code": "E", "message": "홍길동 서울시 강남구 010-1111-2222"}))

    with pytest.raises(NextEngineReadError) as err:
        api.fetch_claims(api_key="k", start=START, end=END, divis=("08",))

    blob = f"{err.value} {getattr(err.value, 'detail', {})}"
    for leaked in ("홍길동", "강남구", "010-1111-2222"):
        assert leaked not in blob


def test_personal_fields_are_not_even_read_into_the_dto():
    """담을 그릇이 없으면 저장하는 실수를 할 수 없다."""
    from app.connectors.next_engine.claim_normalize import (
        DISCARDED_FIELDS,
        normalize_claim,
    )

    claim = normalize_claim(
        dict(_row("Z"), buyer_name="홍길동", receiver_addr="서울"),
        claim_divi="08")
    for field_name in DISCARDED_FIELDS:
        assert not hasattr(claim, field_name), f"{field_name} 가 DTO 에 있다"


# ── 4·5) 동시성 ─────────────────────────────────────────────────────────

def test_claim_sync_does_not_block_order_sync(db):
    """둘은 다른 일이다. 하나가 느리다고 다른 하나를 멈추면 주문이 밀린다."""
    account_id = uuid.uuid4()

    with cs.single_flight(db, account_id):
        # 같은 계정이라도 주문 동기화는 자기 이름공간으로 잡는다.
        with osync.single_flight(db, account_id):
            pass


def test_two_claim_syncs_on_the_same_account_cannot_overlap(db):
    account_id = uuid.uuid4()

    with cs.single_flight(db, account_id):
        with pytest.raises(cs.ClaimSyncAlreadyRunningError):
            with cs.single_flight(db, account_id):
                pass


def test_two_real_connections_cannot_hold_the_same_claim_lock(db):
    """코드에 잠금이 있다는 이유로 PASS 시키지 않는다."""
    account_id = uuid.uuid4()
    engine = getattr(db.get_bind(), "engine", db.get_bind())
    acquired: list[bool] = []
    ready, release = threading.Event(), threading.Event()

    def holder():
        with cs.single_flight(db, account_id):
            ready.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=holder)
    thread.start()
    ready.wait(timeout=10)
    try:
        with engine.connect() as other:
            got = bool(other.execute(
                text("SELECT pg_try_advisory_lock(:ns, :k)"),
                {"ns": cs._LOCK_NAMESPACE, "k": cs._lock_key(account_id)},
            ).scalar())
            acquired.append(got)
            if got:
                other.execute(text("SELECT pg_advisory_unlock(:ns, :k)"),
                              {"ns": cs._LOCK_NAMESPACE,
                               "k": cs._lock_key(account_id)})
    finally:
        release.set()
        thread.join(timeout=10)

    assert acquired == [False]


# ── 2) 정책 ────────────────────────────────────────────────────────────

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
    account_id = uuid.UUID(str(db.execute(text("""
        INSERT INTO external_account (org_id, external_system_id, account_code,
               display_name, created_by, updated_by)
        VALUES (:o, :s, 'DEFAULT', 'NE', :a, :a) RETURNING id
    """), {"o": str(org), "s": str(sys_id), "a": str(actor)}).scalar()))
    make_integration_account(db, org_id=org, provider="COUPANG")
    db.commit()
    return account_id


def test_a_disabled_claim_policy_makes_no_external_call(db, org, actor, ne_account):
    from app.services import automation_runners as runners

    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="CLAIM_SYNC",
        enabled=False, mode="AUTO_EXECUTE",
        guardrails={"claim_lookback_days": 30},
        scope={"accounts": [{"external_account_id": str(ne_account),
                             "initial_sync_from": "2026-05-01"}]})
    runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)

    assert db.execute(text("""
        SELECT count(*) FROM external_sync_run WHERE org_id = :o
    """), {"o": str(org)}).scalar() == 0


def test_report_only_claim_policy_never_calls_the_vendor(db, org, actor, ne_account):
    """꺼 두었다고 믿는 사용자의 계정으로 요청이 나가면 안 된다."""
    from app.services import automation_runners as runners

    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="CLAIM_SYNC",
        enabled=True, mode="REPORT_ONLY",
        guardrails={"claim_lookback_days": 30},
        scope={"accounts": [{"external_account_id": str(ne_account),
                             "initial_sync_from": "2026-05-01"}]})
    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)

    assert result.counts() == {"REPORTED": 1}
    assert db.execute(text("""
        SELECT count(*) FROM external_sync_run WHERE org_id = :o
    """), {"o": str(org)}).scalar() == 0


def test_an_unconfigured_claim_policy_is_blocked_not_silently_skipped(
    db, org, actor, ne_account,
):
    from app.services import automation_runners as runners

    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="CLAIM_SYNC",
        enabled=True, mode="AUTO_EXECUTE")
    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)

    assert result.counts() == {"BLOCKED": 1}
    assert result.actions[0].blocked_by == cs.NOT_CONFIGURED


def test_a_claim_policy_that_already_ran_is_scheduled_again(db, org, actor):
    """두 번째 주기부터 스케줄이 죽지 않는지 — 주문 쪽에서 겪은 결함이다."""
    from app.services.automation_runners import due_policies

    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="CLAIM_SYNC",
        enabled=True, interval_minutes=60)
    db.execute(text("""
        UPDATE automation_policy SET last_run_at = now() - INTERVAL '2 hours'
         WHERE id = :i
    """), {"i": policy["id"]})
    db.commit()

    assert "CLAIM_SYNC" in [p["automation_type"] for p in due_policies(db, org_id=org)]


# ── 3) 실행 단위의 실패 — 부분 실패는 성공이 아니다 ───────────────────

class _FakeClaimApi:
    """창마다 미리 정한 대로 답하는 가짜 벤더.

    실제 클라이언트를 쓰면 401 하나 재현하는 데 HTTP 스텁이 필요하고, 여기서
    보려는 것은 **실행 단위의 판정**이지 HTTP 해석이 아니다.
    """

    max_window_days = 10

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls = 0

    def fetch_claims(self, *, api_key, start, end, **_):
        self.calls += 1
        item = self.script.pop(0) if self.script else []
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def credential(monkeypatch):
    """자격증명 해석만 통과시킨다. 비밀 저장소 자체는 다른 파일이 본다."""
    from app.services import credential_resolver

    class _Resolved:
        def as_dict(self):
            return {"api_key": "test-not-a-real-key"}

    monkeypatch.setattr(credential_resolver, "resolve_for_account",
                        lambda *a, **k: _Resolved())
    return True


def _config(account_id, *, initial_from=date(2026, 8, 15), lookback=30):
    return cs.ClaimAccountConfig(
        external_account_id=account_id,
        initial_sync_from=initial_from, lookback_days=lookback)


def _run_status(db, org):
    return db.execute(text("""
        SELECT status, error_code, read_count, conflict_count
          FROM external_sync_run WHERE org_id = :o
         ORDER BY started_at DESC LIMIT 1
    """), {"o": str(org)}).mappings().first()


def test_one_failed_window_makes_the_whole_run_partial_not_success(
    db, org, actor, ne_account, credential,
):
    """창 하나가 죽었는데 SUCCESS 로 기록하면, 그 구간의 반품은 영원히 안 온다.

    다음 실행은 "지난번 성공" 을 믿고 같은 창을 다시 읽지 않는다.
    """
    api = _FakeClaimApi([
        NextEngineReadError("서버 오류", detail={"http_status": 500}),
        [],
    ])

    out = cs.sync_account(
        db, org_id=org, external_account_id=ne_account,
        config=_config(ne_account, initial_from=date(2026, 8, 1)),
        actor_user_id=actor, now=NOW, client=api)

    assert api.calls == 2                    # 남은 창을 포기하지 않았다
    assert out.status == cs.PARTIAL
    assert out.failed_windows == 1
    assert out.reason == "SERVER_ERROR"
    assert _run_status(db, org)["status"] == "PARTIAL"


@pytest.mark.parametrize(("status", "expected"), [
    (401, "AUTH_FAILED"),
    (429, "RATE_LIMITED"),
])
def test_a_hopeless_failure_stops_calling_the_remaining_windows(
    db, org, actor, ne_account, credential, status, expected,
):
    """자격증명이 거부됐거나 한도에 걸렸다. 남은 8창을 더 두들기면 상황만 나빠진다."""
    api = _FakeClaimApi([
        NextEngineReadError("거부", detail={"http_status": status}), [], [],
    ])

    out = cs.sync_account(
        db, org_id=org, external_account_id=ne_account,
        config=_config(ne_account, initial_from=date(2026, 5, 1), lookback=180),
        actor_user_id=actor, now=NOW, client=api)

    assert api.calls == 1                    # 첫 실패에서 멈췄다
    assert out.reason == expected
    assert out.status == cs.PARTIAL


def test_a_timeout_does_keep_trying_the_next_window(
    db, org, actor, ne_account, credential,
):
    """시간 초과는 다시 시도하면 될 수 있다. 자격증명 실패와 같이 취급하지 않는다."""
    api = _FakeClaimApi([
        NextEngineReadError("느림", detail={"cause": "ReadTimeout"}), [],
    ])

    out = cs.sync_account(
        db, org_id=org, external_account_id=ne_account,
        config=_config(ne_account, initial_from=date(2026, 8, 1)),
        actor_user_id=actor, now=NOW, client=api)

    assert api.calls == 2
    assert out.reason == "TIMEOUT"
    assert out.status == cs.PARTIAL


def test_a_missing_credential_fails_the_run_without_calling_the_vendor(
    db, org, actor, ne_account, monkeypatch,
):
    from app.services import credential_resolver

    def _boom(*a, **k):
        raise RuntimeError("자격증명 없음")

    monkeypatch.setattr(credential_resolver, "resolve_for_account", _boom)
    api = _FakeClaimApi([[_claim_dto("A")]])

    out = cs.sync_account(
        db, org_id=org, external_account_id=ne_account, config=_config(ne_account),
        actor_user_id=actor, now=NOW, client=api)

    assert api.calls == 0
    assert out.status == cs.FAILED
    assert out.reason == cs.CREDENTIAL_NOT_CONFIGURED
    assert _run_status(db, org)["status"] == "FAILED"


def test_counts_that_do_not_reconcile_are_never_recorded_as_success(
    db, org, actor, ne_account, credential, monkeypatch,
):
    """읽은 수 ≠ 버린 수 + 남긴 수 이면 무언가를 조용히 잃은 것이다.

    그걸 SUCCESS 로 적으면 다음 실행이 그 창을 다시 읽지 않는다.
    """
    from app.services import claim_ingest as ci

    def _lying_result(*a, **k):
        bad = ci.ClaimIngestResult(fetched=5)      # 5건 읽었다는데 남긴 건 0
        return bad

    monkeypatch.setattr(ci, "ingest_claims", _lying_result)
    api = _FakeClaimApi([[]])

    out = cs.sync_account(
        db, org_id=org, external_account_id=ne_account, config=_config(ne_account),
        actor_user_id=actor, now=NOW, client=api)

    assert out.status == cs.FAILED
    assert out.reason == cs.RECONCILIATION_FAILED


def test_a_failed_run_carries_no_vendor_body_or_secret(
    db, org, actor, ne_account, credential,
):
    """실행 기록은 운영자가 그대로 보는 화면이다."""
    api = _FakeClaimApi([
        NextEngineReadError(
            "거부", detail={"http_status": 500, "body": "홍길동 010-1111-2222"}),
    ])

    cs.sync_account(
        db, org_id=org, external_account_id=ne_account, config=_config(ne_account),
        actor_user_id=actor, now=NOW, client=api)

    row = db.execute(text("""
        SELECT t::text FROM external_sync_run t WHERE org_id = :o
    """), {"o": str(org)}).scalar() or ""
    for leaked in ("홍길동", "010-1111-2222", "test-not-a-real-key"):
        assert leaked not in row


# ── 5) 수동 vs 워커 ─────────────────────────────────────────────────────

def _hold_claim_lock(db, account_id):
    """다른 **실제 연결**로 잠금을 잡는다. 같은 세션이면 검증이 되지 않는다."""
    engine = getattr(db.get_bind(), "engine", db.get_bind())
    conn = engine.connect()
    got = bool(conn.execute(
        text("SELECT pg_try_advisory_lock(:ns, :k)"),
        {"ns": cs._LOCK_NAMESPACE, "k": cs._lock_key(account_id)}).scalar())
    assert got
    return conn


def test_a_manual_run_does_not_double_up_on_a_worker_already_running(
    db, org, actor, ne_account, credential,
):
    """화면의 "지금 실행" 과 스케줄러가 겹치면 같은 창을 두 번 읽는다.

    적재는 멱등하지만 요청은 두 배로 나가고, `claim_divi` 가 필수라 그
    두 배는 창당 17배다.
    """
    worker = _hold_claim_lock(db, ne_account)
    try:
        with pytest.raises(cs.ClaimSyncAlreadyRunningError):
            cs.sync_account(
                db, org_id=org, external_account_id=ne_account,
                config=_config(ne_account), actor_user_id=actor, now=NOW,
                client=_FakeClaimApi([[]]))
    finally:
        worker.execute(text("SELECT pg_advisory_unlock(:ns, :k)"),
                       {"ns": cs._LOCK_NAMESPACE, "k": cs._lock_key(ne_account)})
        worker.close()

    # 시작하지도 않았으므로 실행 기록을 남기지 않는다 — 실패로 적으면
    # 화면이 빨개지고 실제로는 아무 문제가 없다.
    assert db.execute(text("""
        SELECT count(*) FROM external_sync_run WHERE org_id = :o
    """), {"o": str(org)}).scalar() == 0


def test_the_orchestrator_turns_an_overlap_into_skipped_not_failed(
    db, org, actor, ne_account, credential, monkeypatch,
):
    """겹침은 실패가 아니다. FAILED 로 적으면 알림이 매 주기 울린다."""
    def _busy(*a, **k):
        raise cs.ClaimSyncAlreadyRunningError(str(ne_account))

    monkeypatch.setattr(cs, "sync_account", _busy)
    monkeypatch.setattr(cs, "read_config", lambda p: {ne_account: _config(ne_account)})
    monkeypatch.setattr(
        "app.services.order_sync.ready_accounts",
        lambda db, **k: [{"id": ne_account, "display_name": "NE",
                          "status": "ACTIVE", "system_code": "NEXT_ENGINE",
                          "has_credential": True}])
    monkeypatch.setattr("app.services.order_sync._account_blocker",
                        lambda db, **k: None)

    outcomes = cs.sync_org(db, org_id=org, policy={}, actor_user_id=actor)

    assert [o.status for o in outcomes] == [cs.SKIPPED]
    assert outcomes[0].reason == cs.ALREADY_RUNNING


def test_one_bad_account_does_not_stop_the_next_one(
    db, org, actor, ne_account, credential, monkeypatch,
):
    """계정 하나가 죽었다고 나머지 계정의 반품을 안 읽으면 안 된다."""
    other = uuid.uuid4()
    calls: list = []

    def _sync(db_, *, external_account_id, **k):
        calls.append(external_account_id)
        if external_account_id == ne_account:
            raise RuntimeError("이 계정만 깨졌다")
        return cs.ClaimSyncOutcome(external_account_id=external_account_id,
                                   status=cs.SUCCESS)

    monkeypatch.setattr(cs, "sync_account", _sync)
    monkeypatch.setattr(cs, "read_config", lambda p: {
        ne_account: _config(ne_account), other: _config(other)})
    monkeypatch.setattr(
        "app.services.order_sync.ready_accounts",
        lambda db, **k: [{"id": a, "display_name": "NE", "status": "ACTIVE",
                          "system_code": "NEXT_ENGINE", "has_credential": True}
                         for a in (ne_account, other)])
    monkeypatch.setattr("app.services.order_sync._account_blocker",
                        lambda db, **k: None)

    outcomes = cs.sync_org(db, org_id=org, policy={}, actor_user_id=actor)

    assert calls == [ne_account, other]
    assert [o.status for o in outcomes] == [cs.FAILED, cs.SUCCESS]


# ── 2주기 — 한 번 도는 것은 검증이 아니다 ──────────────────────────────

def _claim_dto(no: str):
    from app.connectors.next_engine.claim_normalize import normalize_claim

    return normalize_claim(_row(no), claim_divi="08")


def test_two_cycles_read_the_same_claims_and_change_nothing_the_second_time(
    db, org, actor, ne_account, credential,
):
    """1회 성공은 검증이 아니다. 두 번째 주기에 매출이 또 깎이면 되돌리기 어렵다."""
    rows = [_claim_dto("A"), _claim_dto("B")]
    api = _FakeClaimApi([list(rows), list(rows)])

    first = cs.sync_account(
        db, org_id=org, external_account_id=ne_account, config=_config(ne_account),
        actor_user_id=actor, now=NOW, client=api)
    second = cs.sync_account(
        db, org_id=org, external_account_id=ne_account, config=_config(ne_account),
        actor_user_id=actor, now=NOW, client=api)

    assert first.status == second.status == cs.SUCCESS
    assert first.ingest["created"] == 2
    assert second.ingest["created"] == 0
    assert second.ingest["unchanged"] == 2
    assert db.execute(text("""
        SELECT count(*) FROM sales_claim WHERE org_id = :o
    """), {"o": str(org)}).scalar() == 2
    # 원 주문이 없는 취소다 — 조정이 생기면 안 된다.
    assert db.execute(text("""
        SELECT count(*) FROM sales_adjustment WHERE org_id = :o
    """), {"o": str(org)}).scalar() == 0


def test_a_second_cycle_does_not_pile_up_run_records_or_issues(
    db, org, actor, ne_account, credential,
):
    api = _FakeClaimApi([[_claim_dto("A")], [_claim_dto("A")]])
    for _ in range(2):
        cs.sync_account(
            db, org_id=org, external_account_id=ne_account,
            config=_config(ne_account), actor_user_id=actor, now=NOW, client=api)

    assert db.execute(text("""
        SELECT count(*) FROM operation_issue
         WHERE org_id = :o AND issue_type = 'UNMATCHED_CLAIM_ORDER'
    """), {"o": str(org)}).scalar() == 1
    # 실행 기록은 매번 남는다 — 언제 돌았는지가 사라지면 안 된다.
    assert db.execute(text("""
        SELECT count(*) FROM external_sync_run WHERE org_id = :o
    """), {"o": str(org)}).scalar() == 2


# ── 상태 코드 해석 ──────────────────────────────────────────────────────

def test_the_vendor_group_labels_are_not_trusted():
    """공식 설명의 그룹 라벨이 코드 범위와 어긋나 있다.

    `반품전체: 10,11,...` 인데 10~15 는 개별 설명이 전부 교환이다. 라벨을
    믿으면 반품을 교환으로 읽고, 교환은 매출을 안 건드리므로 반품 매출이
    조용히 남는다.
    """
    from app.connectors.next_engine.claim_normalize import CLAIM_DIVI

    assert CLAIM_DIVI["10"][0] == "EXCHANGE"
    assert CLAIM_DIVI["16"][0] == "RETURN"
    assert CLAIM_DIVI["19"] == ("RETURN", "COMPLETED", "반품완료")


def test_the_code_the_vendor_rejects_is_not_queried():
    """문서에는 있지만 조회하면 400 이다(실측). 매번 실패를 만들지 않는다."""
    from app.connectors.next_engine.claim_normalize import (
        QUERYABLE_DIVI,
        REJECTED_BY_VENDOR,
    )

    assert "20" in REJECTED_BY_VENDOR
    assert "20" not in QUERYABLE_DIVI


def test_an_undocumented_code_is_unknown_not_guessed():
    from app.connectors.next_engine.claim_normalize import CLAIM_DIVI

    assert CLAIM_DIVI["22"][0] == "UNKNOWN"
