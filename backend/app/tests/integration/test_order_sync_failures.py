"""주문 동기화 — 실제 HTTP 실패와 진짜 동시성.

왜 이 파일이 따로 있나
------------------
`test_order_auto_sync.py` 는 가짜 클라이언트로 **오케스트레이션**을 고정한다.
여기서는 실제 `httpx` 계층을 태워 **응답이 이렇게 오면 이렇게 판단한다** 를
고정한다. 둘을 한 곳에서 하면, 가짜가 실제와 달라진 날 아무도 모른다.

외부 실제 API 를 고의로 망가뜨리지 않는다 — `MockTransport` 로 응답을 만든다.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime

import httpx
import pytest
from sqlalchemy import text

from app.connectors.next_engine.client import (
    NextEngineOrderClient,
    NextEnginePaginationError,
    NextEngineReadError,
)
from app.services import order_sync as osync

START = datetime(2026, 8, 17, 0, 0, 0)
END = datetime(2026, 8, 20, 0, 0, 0)


def _client(handler) -> NextEngineOrderClient:
    return NextEngineOrderClient(transport=httpx.MockTransport(handler))


def _order_row(no: str) -> dict:
    """벤더 응답 한 줄. 개인정보 필드는 넣지 않는다."""
    return {
        "ne_order_no": no, "mall_order_no": f"M{no}",
        "mall_cd": "mall02400", "mall_name": "쿠팡",
        "ne_product_cd": "P1", "ne_partner_product_cd": "SKU-1",
        "order_status_divi": "04", "order_price": 1000, "order_cnt": 1,
        "delivery_price": 0, "pay_date": "2026-08-18 10:00:00",
        "order_date": "2026-08-18 10:00:00",
    }


# ── HTTP 상태별 분류 ────────────────────────────────────────────────────

@pytest.mark.parametrize(("status", "expected"), [
    (401, osync.AUTH_FAILED),
    (403, osync.AUTH_FAILED),
    (429, osync.RATE_LIMITED),
    (500, osync.SERVER_ERROR),
    (503, osync.SERVER_ERROR),
])
def test_http_status_maps_to_an_actionable_category(status, expected):
    """자격증명을 고쳐야 하는 것과 기다리면 되는 것은 다른 일이다."""
    api = _client(lambda req: httpx.Response(status, json={"code": "E"}))

    with pytest.raises(NextEngineReadError) as err:
        api.fetch_orders(api_key="k", start=START, end=END)

    assert osync.classify_error(err.value) == expected


def test_timeout_is_not_reported_as_a_vendor_rejection():
    """우리 쪽에서 못 기다린 것과 벤더가 거부한 것은 대응이 다르다."""
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    api = _client(handler)
    with pytest.raises(NextEngineReadError) as err:
        api.fetch_orders(api_key="k", start=START, end=END)

    assert osync.classify_error(err.value) == osync.TIMEOUT


def test_a_broken_response_shape_is_schema_changed_not_zero_orders():
    """형식이 깨졌는데 0건이라고 하면 확인해 봤다는 뜻으로 읽힌다."""
    api = _client(lambda req: httpx.Response(200, json={"data": "목록이 아님"}))

    with pytest.raises(NextEngineReadError) as err:
        api.fetch_orders(api_key="k", start=START, end=END)

    assert osync.classify_error(err.value) == osync.SCHEMA_CHANGED


# ── 페이지네이션 ────────────────────────────────────────────────────────

def test_a_page_that_never_advances_stops_instead_of_looping():
    """벤더가 `page` 를 무시하면 종료조건이 영원히 참이 되지 않는다.

    상한(100)까지 같은 요청을 반복하는 동안 결과는 매번 같고, 아무 일도
    일어나지 않는다. 그 사이 벤더 한도만 태운다.
    """
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        # size 만큼 가득 채워 계속 돌려준다. 항상 같은 주문이다.
        return httpx.Response(200, json={"data": [_order_row("A"), _order_row("B")]})

    api = _client(handler)
    with pytest.raises(NextEnginePaginationError):
        api.fetch_orders(api_key="k", start=START, end=END, size=2,
                         statuses=("04",))

    # 상한(100)까지 가지 않았다.
    assert calls["n"] < 10


def test_a_short_page_ends_normally():
    """정상 종료조건은 그대로다 — 받은 수가 요청한 수보다 적으면 마지막."""
    pages = [
        [_order_row("A"), _order_row("B")],
        [_order_row("C")],
    ]

    def handler(request):
        return httpx.Response(200, json={"data": pages.pop(0) if pages else []})

    api = _client(handler)
    lines = api.fetch_orders(api_key="k", start=START, end=END, size=2,
                             statuses=("04",))
    assert len(lines) == 3


def test_an_empty_page_ends_without_error():
    api = _client(lambda req: httpx.Response(200, json={"data": []}))
    assert api.fetch_orders(api_key="k", start=START, end=END,
                            statuses=("04",)) == []


# ── 비밀·개인정보 ───────────────────────────────────────────────────────

def test_the_api_key_never_appears_in_the_failure():
    """마스킹했으니 괜찮다는 습관이 생기면 언젠가 마스킹을 잊는다."""
    api = _client(lambda req: httpx.Response(500, json={"code": "E"}))

    with pytest.raises(NextEngineReadError) as err:
        api.fetch_orders(api_key="super-secret-key", start=START, end=END)

    blob = f"{err.value} {getattr(err.value, 'detail', {})}"
    assert "super-secret-key" not in blob


def test_the_vendor_body_is_not_carried_into_the_error():
    """주문 응답에는 수취인과 주소가 들어 있다. 실패 로그는 권한이 넓다."""
    api = _client(lambda req: httpx.Response(
        500, json={"code": "E", "message": "홍길동 서울시 강남구 010-1234-5678"}))

    with pytest.raises(NextEngineReadError) as err:
        api.fetch_orders(api_key="k", start=START, end=END)

    blob = f"{err.value} {getattr(err.value, 'detail', {})}"
    for leaked in ("홍길동", "강남구", "010-1234-5678"):
        assert leaked not in blob


# ── 진짜 동시성 ─────────────────────────────────────────────────────────

def test_two_real_connections_cannot_hold_the_same_account(db):
    """코드에 잠금이 있다는 이유로 PASS 시키지 않는다.

    실제 별도 연결 두 개로 같은 계정을 동시에 잡아 본다. advisory lock 은
    연결 단위라, 같은 연결에서 두 번 잡으면 항상 성공해 아무것도 증명하지
    못한다.
    """
    account_id = uuid.uuid4()
    engine = getattr(db.get_bind(), "engine", db.get_bind())
    second_acquired: list[bool] = []
    ready = threading.Event()
    release = threading.Event()

    def holder():
        with osync.single_flight(db, account_id):
            ready.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=holder)
    thread.start()
    ready.wait(timeout=10)

    try:
        with engine.connect() as other:
            got = bool(other.execute(
                text("SELECT pg_try_advisory_lock(:ns, :k)"),
                {"ns": osync._LOCK_NAMESPACE, "k": osync._lock_key(account_id)},
            ).scalar())
            second_acquired.append(got)
            if got:
                other.execute(text("SELECT pg_advisory_unlock(:ns, :k)"),
                              {"ns": osync._LOCK_NAMESPACE,
                               "k": osync._lock_key(account_id)})
    finally:
        release.set()
        thread.join(timeout=10)

    assert second_acquired == [False]


def test_the_lock_is_released_when_the_run_ends(db):
    """잠금이 새면 다음 주기가 영원히 ALREADY_RUNNING 이 된다."""
    account_id = uuid.uuid4()
    with osync.single_flight(db, account_id):
        pass

    engine = getattr(db.get_bind(), "engine", db.get_bind())
    with engine.connect() as other:
        got = bool(other.execute(
            text("SELECT pg_try_advisory_lock(:ns, :k)"),
            {"ns": osync._LOCK_NAMESPACE, "k": osync._lock_key(account_id)},
        ).scalar())
        other.execute(text("SELECT pg_advisory_unlock(:ns, :k)"),
                      {"ns": osync._LOCK_NAMESPACE,
                       "k": osync._lock_key(account_id)})

    assert got is True


def test_different_accounts_fold_to_different_lock_keys():
    """계정 단위 격리가 키 접기 때문에 무너지지 않는지."""
    keys = {osync._lock_key(uuid.uuid4()) for _ in range(200)}
    assert len(keys) > 190          # 충돌이 사실상 없다
    assert 0 not in keys            # 0 은 예약값이다
