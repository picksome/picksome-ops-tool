"""NEXT ENGINE 주문 정규화 · 조회 창 · 페이지네이션 (Phase 1B).

네트워크를 타지 않는다. 실제 API 검증은 별도 smoke 로만 한다.

이 파일이 고정하는 것
------------------
1. **개인정보는 DTO 에 담길 자리가 없다** — 실수할 여지 자체를 없앤다
2. 모르는 금액은 `None` 이다 — `0` 이 아니다
3. 조회 창이 벤더 제한을 넘으면 **네트워크를 타기 전에** 막는다
4. 페이지네이션은 반드시 끝난다
"""

from __future__ import annotations

import json
from dataclasses import fields as dataclass_fields
from datetime import datetime

import httpx
import pytest

from app.connectors.next_engine import (
    NEXT_ENGINE_MAX_WINDOW_DAYS,
    PII_FIELDS,
    ExternalOrderLine,
    NextEngineOrderClient,
    normalize_order_line,
)
from app.connectors.next_engine.client import (
    NextEngineReadError,
    NextEngineWindowError,
    split_windows,
)

FAKE_KEY = "test-not-a-real-key-0000"

#: 실제 응답 모양을 본뜬 행. **개인정보 자리에는 눈에 띄는 표시값**을 넣어
#: 어디로도 새지 않는지 검사한다.
VENDOR_ROW = {
    "ne_order_no": "7930662596",
    "mall_order_no": "202311011137479431",
    "mall_cd": "mall02400",
    "mall_name": "쿠팡",
    "mall_id": "shop-account-1",
    "delivery_group_no": "37325223",
    "ne_product_cd": "3238751191",
    "ne_partner_product_cd": "6176129056",
    "ne_stock_cd": "",
    "mall_product_cd": "114970007",
    "product_name": "테스트상품",
    "option_name": "색상 : 빨강",
    "order_date": "2023-11-01 11:37:47",
    "pay_date": "2023-11-01 11:40:47",
    "order_reg_date": None,
    "order_status_divi": "04",
    "order_price": 14000,
    "order_cnt": 1,
    "sell_price": 12000,
    "sell_option_price": 2000,
    "delivery_price": 0,
    "delivery_divi": "01",
    "add_option_flg": "N",
    "free_gift_flg": "N",
    # ── 개인정보 11종 ──
    "buyer_name": "PII_BUYER_NAME",
    "buyer_id": "PII_BUYER_ID",
    "buyer_phone": "PII_BUYER_PHONE",
    "receiver_name": "PII_RECEIVER_NAME",
    "receiver_hp_phone": "PII_RECEIVER_HP",
    "receiver_phone": "PII_RECEIVER_PHONE",
    "receiver_email": "PII_RECEIVER_EMAIL",
    "receiver_zip_code": "PII_ZIP",
    "receiver_addr": "PII_ADDR",
    "receiver_msg": "PII_MSG",
    "individual_clearance_no": "PII_CLEARANCE",
}


# ── 개인정보 ──────────────────────────────────────────────────────────────

def test_dto_has_no_place_to_put_pii():
    """저장할 때 빼는 게 아니라 **담을 그릇이 없다.**

    필드가 있으면 언젠가 누가 채운다.
    """
    names = {f.name for f in dataclass_fields(ExternalOrderLine)}
    assert not (names & PII_FIELDS)
    assert len(PII_FIELDS) == 11


def test_normalized_line_carries_no_pii():
    line = normalize_order_line(VENDOR_ROW)
    dumped = json.dumps(
        {f.name: str(getattr(line, f.name)) for f in dataclass_fields(line)},
        ensure_ascii=False)

    for value in VENDOR_ROW.values():
        if isinstance(value, str) and value.startswith("PII_"):
            assert value not in dumped


def test_normalizer_does_not_keep_the_raw_row():
    """원본 dict 를 들고 다니면 개인정보가 따라온다."""
    line = normalize_order_line(VENDOR_ROW)
    assert not hasattr(line, "raw")
    text = repr(line)
    assert "PII_" not in text


# ── 금액 ─────────────────────────────────────────────────────────────────

def test_missing_money_is_none_not_zero():
    """`0원 수수료` 와 `모르는 수수료` 가 같아지면 순이익이 부풀려진다."""
    row = dict(VENDOR_ROW, order_price=None, sell_price="", delivery_price=None)
    line = normalize_order_line(row)

    assert line.order_price is None
    assert line.sell_price is None
    assert line.delivery_price is None
    # 진짜 0 은 0 으로 남는다 — 없는 것과 다르다.
    assert normalize_order_line(dict(VENDOR_ROW, delivery_price=0)).delivery_price == 0


def test_money_never_becomes_a_bool():
    """`True` 가 1 로 새어 들어오면 금액이 조용히 1원이 된다."""
    assert normalize_order_line(dict(VENDOR_ROW, order_price=True)).order_price is None


def test_unparseable_money_is_none():
    assert normalize_order_line(dict(VENDOR_ROW, order_price="약 1만원")).order_price is None


# ── 값 정리 ──────────────────────────────────────────────────────────────

def test_blank_option_code_is_absent_not_empty_string():
    """`""` 를 값으로 취급하면 빈 문자열로 옵션을 찾으러 간다."""
    line = normalize_order_line(VENDOR_ROW)
    assert line.ne_stock_cd is None
    assert line.has_option_code is False


def test_sale_date_prefers_payment_date():
    line = normalize_order_line(VENDOR_ROW)
    assert line.sale_date == datetime(2023, 11, 1, 11, 40, 47).date()
    # 결제일이 없으면 주문일로 물러난다.
    no_pay = normalize_order_line(dict(VENDOR_ROW, pay_date=None))
    assert no_pay.sale_date == datetime(2023, 11, 1, 11, 37, 47).date()


def test_unparseable_timestamp_is_none():
    assert normalize_order_line(dict(VENDOR_ROW, pay_date="주문시간")).pay_date is None


def test_identity_is_the_ne_order_no_alone():
    """`ne_order_no` 가 이미 **라인** 식별자다 — 실측으로 확인했다.

    23행에 `ne_order_no` 23개, `mall_order_no` 22개였다. 쇼핑몰 주문 하나가
    라인마다 다른 NEXT ENGINE 주문번호를 받는다.

    상품코드를 섞어 두면 같은 주문·같은 상품의 두 라인이 하나로 접힌다.
    실제로 그 일이 일어나 대사에서 거짓 불일치가 나왔다.
    """
    a = normalize_order_line(VENDOR_ROW)
    same_order_same_product = normalize_order_line(dict(VENDOR_ROW))
    assert a.identity() == same_order_same_product.identity()

    # 같은 쇼핑몰 주문의 **다른 라인**은 ne_order_no 가 다르다 → 접히지 않는다.
    sibling = normalize_order_line(dict(VENDOR_ROW, ne_order_no="7930662597"))
    assert a.identity() != sibling.identity()
    assert a.mall_order_no == sibling.mall_order_no


# ── 조회 창 ──────────────────────────────────────────────────────────────

def test_window_limit_is_enforced_before_the_network():
    """벤더가 400 을 주기 전에 우리가 막는다.

    실제 400 메시지: `최대 10일까지 조회할수 있습니다`. spec 에는 없다.
    """
    client = NextEngineOrderClient()
    with pytest.raises(NextEngineWindowError):
        client.fetch_order_page(
            api_key=FAKE_KEY, start=datetime(2026, 1, 1), end=datetime(2026, 3, 1),
            status="04")


def test_split_windows_never_exceeds_the_limit():
    windows = split_windows(datetime(2026, 1, 1), datetime(2026, 3, 1))
    assert windows
    for start, end in windows:
        assert (end - start).days <= NEXT_ENGINE_MAX_WINDOW_DAYS
    # 끝을 넘지 않는다 — 넘으면 미래 구간을 조회하게 된다.
    assert windows[-1][1] == datetime(2026, 3, 1)
    assert windows[0][0] == datetime(2026, 1, 1)


def test_split_windows_rejects_reversed_range():
    with pytest.raises(NextEngineWindowError):
        split_windows(datetime(2026, 3, 1), datetime(2026, 1, 1))


# ── 페이지네이션 ──────────────────────────────────────────────────────────

def _transport(pages: list[list[dict]], *, status_code: int = 200):
    """요청 순서대로 페이지를 돌려주는 가짜 전송계층."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        idx = len(calls) - 1
        data = pages[idx] if idx < len(pages) else []
        return httpx.Response(
            status_code,
            json={"code": status_code, "message": "호출 성공",
                  "process_id": "pid-1", "data": data},
        )

    return httpx.MockTransport(handler), calls


def test_pagination_stops_on_a_short_page():
    """총건수가 없어서 종료 조건이 이것뿐이다.

    실제 확인: 3건짜리 창에 size=2 → 2건, 1건. 두 번째에서 멈춰야 한다.
    """
    full = [dict(VENDOR_ROW, ne_order_no=f"A{i}") for i in range(2)]
    short = [dict(VENDOR_ROW, ne_order_no="A9")]
    transport, calls = _transport([full, short])
    client = NextEngineOrderClient(transport=transport)

    lines = client.fetch_orders(
        api_key=FAKE_KEY, start=datetime(2026, 1, 1), end=datetime(2026, 1, 5),
        statuses=("04",), size=2)

    assert len(calls) == 2          # 세 번째 페이지를 부르지 않는다
    assert len(lines) == 3


def test_pagination_stops_on_an_empty_page():
    transport, calls = _transport([[]])
    client = NextEngineOrderClient(transport=transport)

    lines = client.fetch_orders(
        api_key=FAKE_KEY, start=datetime(2026, 1, 1), end=datetime(2026, 1, 5),
        statuses=("04",), size=30)

    assert len(calls) == 1
    assert lines == []


def test_pagination_cannot_run_forever():
    """벤더가 항상 가득 찬 페이지를 주는 버그를 내도 멈춘다."""
    always_full = [dict(VENDOR_ROW, ne_order_no=f"X{i}") for i in range(2)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 200, "data": always_full})

    client = NextEngineOrderClient(transport=httpx.MockTransport(handler))
    with pytest.raises(NextEngineReadError):
        client.fetch_orders(
            api_key=FAKE_KEY, start=datetime(2026, 1, 1), end=datetime(2026, 1, 5),
            statuses=("04",), size=2, max_pages=3)


def test_same_line_seen_in_two_statuses_collapses():
    """상태가 도중에 바뀌면 두 상태에서 같은 라인을 받을 수 있다."""
    row = [dict(VENDOR_ROW)]
    transport, _ = _transport([row, row])
    client = NextEngineOrderClient(transport=transport)

    lines = client.fetch_orders(
        api_key=FAKE_KEY, start=datetime(2026, 1, 1), end=datetime(2026, 1, 5),
        statuses=("03", "04"), size=30)

    assert len(lines) == 1


# ── 오류 ─────────────────────────────────────────────────────────────────

def test_vendor_error_body_is_not_put_into_the_message():
    """오류 응답에도 개인정보가 들어올 수 있다."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "code": 400, "message": "PII_LEAK_MESSAGE", "process_id": "pid-9"})

    client = NextEngineOrderClient(transport=httpx.MockTransport(handler))
    with pytest.raises(NextEngineReadError) as exc:
        client.fetch_order_page(
            api_key=FAKE_KEY, start=datetime(2026, 1, 1), end=datetime(2026, 1, 5),
            status="04")

    rendered = str(exc.value) + json.dumps(
        getattr(exc.value, "detail", {}) or {}, ensure_ascii=False)
    assert "PII_LEAK_MESSAGE" not in rendered
    assert "pid-9" in rendered      # 추적 id 는 남긴다


def test_empty_api_key_is_rejected_before_the_network():
    client = NextEngineOrderClient()
    with pytest.raises(NextEngineReadError):
        client.fetch_order_page(
            api_key="", start=datetime(2026, 1, 1), end=datetime(2026, 1, 5),
            status="04")


def test_client_module_has_no_write_endpoint():
    """공식 문서의 PUT 4개가 이 파일에 문자열로도 없어야 한다."""
    from pathlib import Path

    source = Path(
        __import__("app.connectors.next_engine.client", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")

    for path in ("orders/internal_confirmation", "orders/order_confirmation",
                 "orders/order_shipping", "orders/update"):
        assert f'"/api/v1/ne/{path}"' not in source
    for verb in ("http.put", "http.post", "http.patch", "http.delete"):
        assert verb not in source
