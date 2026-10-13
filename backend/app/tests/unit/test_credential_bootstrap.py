"""자격증명 부트스트랩 파서 · NEXT ENGINE 요청 구성 (M052).

이 파일은 **네트워크를 타지 않는다.** 실제 API 호출은
`scripts/auth_diagnose.py` 를 손으로 돌리는 것으로만 한다 — 일반 테스트가
외부 서버에 의존하면 상대 사정으로 빨간 불이 뜨고, 그 순간부터 아무도 빨간
불을 믿지 않는다.

여기서 지키려는 것은 두 가지다.

```
1. 사람이 손으로 적은 파일을 우리가 **어떻게 읽는가**   (조용히 틀리면 안 된다)
2. 비밀이 프로세스 밖으로 **나가지 않는가**             (한 번 새면 끝이다)
```
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.auth_diagnose import (  # noqa: E402
    NEXT_ENGINE_BASE,
    NEXT_ENGINE_PRODUCT_DETAIL_PATH,
    NEXT_ENGINE_PRODUCT_SEARCH_PATH,
    NEXT_ENGINE_UNDOCUMENTED_DOMAINS,
    Diagnosis,
    _first_product_cd,
    _next_engine_detail_shape,
    _next_engine_shape,
    next_engine_headers,
)
from scripts.credential_bootstrap import (  # noqa: E402
    EXTERNAL_SYSTEM_CODE,
    KIND,
    MARKETPLACE_PROVIDER,
    REQUIRED,
    ServiceCredential,
    parse_file,
    split_service_field,
)

#: 테스트용 가짜 키. 실제 키와 **길이도 모양도 다르게** 둔다 — 진짜와 닮은
#: 값을 fixture 에 두면 언젠가 진짜가 섞여 들어와도 아무도 눈치채지 못한다.
FAKE_KEY = "test-not-a-real-key-0000"


# ── 파서 ──────────────────────────────────────────────────────────────────

def test_next_engine_api_key_name_is_split_into_service_and_field():
    """`NEXT_ENGINE_API_KEY` 는 서비스와 필드가 한 이름에 붙어 있다."""
    assert split_service_field("NEXT_ENGINE_API_KEY") == ("NEXT_ENGINE", "api_key")
    assert split_service_field("NextEngine_VENDOR_ID") == ("NEXT_ENGINE", "vendor_id")


def test_next_engine_is_complete_with_a_single_api_key():
    """실제 발급된 것은 단일 API 키다. client_id/secret 을 요구하지 않는다."""
    assert REQUIRED["NEXT_ENGINE"] == ("api_key",)
    assert KIND["NEXT_ENGINE"] == "API_KEY"

    cred = ServiceCredential("NEXT_ENGINE", fields={"api_key": FAKE_KEY})
    assert cred.complete is True
    assert cred.missing == []


def test_next_engine_uses_the_seller_tool_path():
    """판매처(integration_account)가 아니라 외부 시스템(external_account)이다.

    두 경로에 동시에 들어가면 어느 테이블의 자격증명이 쓰이는지 알 수 없다.
    """
    assert EXTERNAL_SYSTEM_CODE["NEXT_ENGINE"] == "NEXT_ENGINE"
    assert "NEXT_ENGINE" not in MARKETPLACE_PROVIDER
    assert not set(EXTERNAL_SYSTEM_CODE) & set(MARKETPLACE_PROVIDER)


def test_duplicate_name_keeps_the_first_value(tmp_path: Path):
    """실제 파일이 그랬다.

    사람이 키 아래에 한글 메모를 같은 이름으로 한 줄 더 적어 두었다. 뒤엣것을
    덮어쓰면 메모가 API 키가 되어 인증이 실패하고, 원인은 파일을 눈으로 훑어도
    보이지 않는다.
    """
    f = tmp_path / "api key.txt"
    f.write_text(
        f"NEXT_ENGINE_API_KEY = {FAKE_KEY}\n"
        "NEXT_ENGINE_API_KEY = 발급대기중\n",
        encoding="utf-8",
    )
    cred = next(c for c in parse_file(f) if c.service == "NEXT_ENGINE")

    assert cred.fields["api_key"] == FAKE_KEY
    # 조용히 넘어가지 않는다 — 무시했다는 사실이 보고에 남는다.
    assert any("앞의 값과 다릅니다" in p for p in cred.problems)


def test_empty_value_is_not_taken_as_a_field(tmp_path: Path):
    f = tmp_path / "nextengine.txt"
    f.write_text("NextEngine_VENDOR_ID = \n", encoding="utf-8")
    cred = next(c for c in parse_file(f) if c.service == "NEXT_ENGINE")

    assert "vendor_id" not in cred.fields
    assert cred.complete is False


def test_report_never_contains_the_value():
    """`report()` 는 화면과 로그로 나간다. 여기에 값이 들어가면 끝이다."""
    cred = ServiceCredential("NEXT_ENGINE", fields={"api_key": FAKE_KEY})
    dumped = json.dumps(cred.report(), ensure_ascii=False)

    assert FAKE_KEY not in dumped
    assert "api_key" in dumped  # 칸 이름은 알려 준다


def test_repr_never_contains_the_value():
    """`print(cred)` 한 줄이면 CI 로그에 남는다."""
    cred = ServiceCredential("NEXT_ENGINE", fields={"api_key": FAKE_KEY})

    assert FAKE_KEY not in repr(cred)
    assert FAKE_KEY not in str(cred)
    assert FAKE_KEY not in f"{cred}"


# ── 요청 구성 ─────────────────────────────────────────────────────────────

def test_auth_header_is_api_auth_key_only():
    """공식 규격이 `Api-Auth-Key` 다. `Authorization` 이 아니다."""
    headers = next_engine_headers(FAKE_KEY)

    assert headers["Api-Auth-Key"] == FAKE_KEY
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"


def test_empty_key_is_rejected_before_the_network():
    """네트워크를 타기 전에 막는다. 안 그러면 401 을 받고 "키가 틀렸다" 고
    잘못 결론 낸다 — 실제로는 키가 없었던 것이다."""
    with pytest.raises(ValueError):
        next_engine_headers("")


def test_product_search_path_is_read_only():
    """등록 경로(`/products`)와 한 글자 차이다. 진단이 쓰기로 새면 운영 상품이
    바뀐다."""
    assert NEXT_ENGINE_BASE == "https://neapi.next-engine.co.kr"
    assert NEXT_ENGINE_PRODUCT_SEARCH_PATH == "/api/v1/ne/products/search"
    assert NEXT_ENGINE_PRODUCT_SEARCH_PATH.endswith("/search")


def test_diagnose_module_has_no_write_endpoint():
    """상수로 옆에 두면 언젠가 잘못 고른다. 아예 파일에 없어야 한다."""
    source = (Path(__file__).resolve().parents[3]
              / "scripts" / "auth_diagnose.py").read_text(encoding="utf-8")

    # 문자열 리터럴로 등록/수정/삭제 경로가 있으면 안 된다.
    assert '"/api/v1/ne/products"' not in source
    assert "'/api/v1/ne/products'" not in source
    assert "httpx.delete" not in source
    assert "httpx.put" not in source
    assert "httpx.patch" not in source


# ── 응답 해석 ─────────────────────────────────────────────────────────────

def test_count_source_is_distinguished():
    """`total=9` 만으로는 "전체 9건" 인지 "이번 페이지에 9건" 인지 모른다.

    size=1 을 보냈는데 9건이 오면 페이지네이션이 우리 생각과 다르다는 뜻이고,
    그건 커넥터를 만들기 전에 알아야 한다.
    """
    from_field = _next_engine_shape(json.dumps({"count": 9, "data": [{}]}))
    assert from_field.total == 9
    assert from_field.count_source == "count"

    from_items = _next_engine_shape(json.dumps({"data": [{}, {}, {}]}))
    assert from_items.total == 3
    assert from_items.count_source == "item_count"


def test_zero_results_is_still_a_valid_response():
    """0건과 "가져올 수 없다" 는 다르다. 인증 확인에 건수는 판정 기준이 아니다."""
    shape = _next_engine_shape(json.dumps({"count": 0, "data": []}))

    assert shape.valid is True
    assert shape.total == 0
    assert shape.fields == []


def test_unexpected_shape_is_not_valid():
    for body in ('{"unexpected": 1}', "[]", "not json", '"string"'):
        assert _next_engine_shape(body).valid is False


def test_shape_result_never_contains_values():
    """응답 본문에는 상품명·가격이 들어 있다. 우리가 들고 나오는 것은
    **필드 이름과 개수**뿐이어야 한다."""
    body = json.dumps({
        "count": 1,
        "data": [{
            "product_cd": "SECRET-CD-1",
            "product_name": "대외비 상품명",
            "sell_price": 12345,
            "original_price": 6789,
        }],
    })
    shape = _next_engine_shape(body)
    dumped = json.dumps(shape.__dict__, ensure_ascii=False)

    assert "SECRET-CD-1" not in dumped
    assert "대외비 상품명" not in dumped
    assert "12345" not in dumped
    assert shape.fields == [
        "product_cd", "product_name", "sell_price", "original_price",
    ]


def test_diagnosis_result_never_contains_values():
    """`Diagnosis.as_dict()` 는 그대로 출력된다."""
    d = Diagnosis(
        "NEXT_ENGINE", "AUTH_SUCCESS", http_status=200,
        schema_valid=True, total_count=9, count_source="count",
        observed_fields=["product_cd", "original_price"],
    )
    dumped = json.dumps(d.as_dict(), ensure_ascii=False)

    assert FAKE_KEY not in dumped
    assert "Api-Auth-Key" not in dumped
    assert "original_price" in dumped  # 필드 이름은 남는다


# ── 문서 없는 영역 ────────────────────────────────────────────────────────

def test_undocumented_domains_are_not_claimed_as_supported():
    """상품 API 가 되니까 주문도 될 것이라고 보지 않는다.

    인증 성공은 키가 유효하다는 뜻이지, 그 키로 어떤 API 를 부를 수 있는지를
    말해 주지 않는다.
    """
    assert set(NEXT_ENGINE_UNDOCUMENTED_DOMAINS) == {
        "ORDER", "INVENTORY", "CLAIM", "SETTLEMENT",
    }

    source = (Path(__file__).resolve().parents[3]
              / "scripts" / "auth_diagnose.py").read_text(encoding="utf-8")
    for domain in ("orders", "inventory", "claims", "settlements"):
        assert f"/api/v1/ne/{domain}" not in source


# ── 주석 처리된 할당 ──────────────────────────────────────────────────────

def test_commented_assignment_is_ignored(tmp_path: Path):
    """사람은 값을 지우는 대신 `#` 를 붙인다.

    실제 파일이 그랬다 — 한글 메모가 `NEXT_ENGINE_API_KEY` 이름으로 한 줄 더
    있어서 주석 처리했다. 살려 읽으면 지운 값이 되살아난다.
    """
    f = tmp_path / "api key.txt"
    f.write_text(
        f"NEXT_ENGINE_API_KEY = {FAKE_KEY}\n"
        "# NEXT_ENGINE_API_KEY = 발급대기중\n",
        encoding="utf-8",
    )
    cred = next(c for c in parse_file(f) if c.service == "NEXT_ENGINE")

    assert cred.fields["api_key"] == FAKE_KEY
    assert cred.complete is True
    # 무시했다는 경고조차 남지 않는다 — 주석은 처음부터 없는 줄이다.
    assert not any("api_key" in p for p in cred.problems)


def test_section_header_comment_still_works(tmp_path: Path):
    """`# NAVER` 는 값이 아니라 구분선이다. 주석이라고 버리면 안 된다."""
    f = tmp_path / "creds.txt"
    f.write_text(
        "# NAVER\n"
        f"client_id: {FAKE_KEY}\n"
        f"client_secret: {FAKE_KEY}2\n",
        encoding="utf-8",
    )
    services = {c.service for c in parse_file(f)}
    assert "NAVER" in services


# ── Product Detail ────────────────────────────────────────────────────────

def test_detail_path_is_a_template_not_a_write_route():
    """같은 경로에 PATCH/PUT/DELETE 가 걸려 있다. GET 전용으로만 쓴다."""
    assert NEXT_ENGINE_PRODUCT_DETAIL_PATH == "/api/v1/ne/products/{product_cd}"
    assert "{product_cd}" in NEXT_ENGINE_PRODUCT_DETAIL_PATH

    source = (Path(__file__).resolve().parents[3]
              / "scripts" / "auth_diagnose.py").read_text(encoding="utf-8")
    # 단건 경로를 쓰는 HTTP 호출은 httpx.get 뿐이어야 한다.
    assert "httpx.put" not in source
    assert "httpx.patch" not in source
    assert "httpx.delete" not in source


def test_seed_product_cd_takes_only_one():
    """단건 조회에 쓸 씨앗은 하나면 된다. 목록을 들고 다니지 않는다."""
    body = json.dumps({"data": [{"product_cd": "A1"}, {"product_cd": "A2"}]})
    assert _first_product_cd(body) == "A1"

    assert _first_product_cd(json.dumps({"data": []})) is None
    assert _first_product_cd("not json") is None


def test_missing_product_cd_is_still_a_valid_detail_response():
    """조회 키는 경로 인자라 본문에 다시 오지 않는다.

    처음에 `product_cd` 존재로 유효성을 판정했다가 멀쩡한 응답을 실패로 읽었다.
    **없는 것을 근거로 삼으면 안 된다.**
    """
    body = json.dumps({
        "code": 200, "message": "OK", "process_id": "p-1",
        "data": {"product_basic": {
            "product_name": "x", "partner_product_cd": "y",
            "original_price": 1, "sell_price": 2, "custom_price": 3,
        }},
    })
    shape = _next_engine_detail_shape(body)

    assert shape.valid is True
    assert "product_cd" in shape.missing_fields
    assert shape.api_code == 200
    assert shape.process_id_present is True
    assert shape.sections == ["product_basic"]


def test_detail_reports_price_types_not_values():
    """`original_price` 가 int 인지 str 인지는 커넥터가 알아야 한다.

    타입은 남기고 값은 남기지 않는다.
    """
    body = json.dumps({
        "code": 200,
        "data": {"product_basic": {
            "product_name": "대외비", "original_price": 7777,
            "sell_price": 8888, "custom_price": "9999",
        }},
    })
    shape = _next_engine_detail_shape(body)
    dumped = json.dumps(shape.__dict__, ensure_ascii=False)

    assert shape.price_types == {
        "original_price": "int", "sell_price": "int", "custom_price": "str",
    }
    assert "7777" not in dumped
    assert "8888" not in dumped
    assert "대외비" not in dumped


def test_option_item_keys_are_collected():
    """옵션 단위 식별자가 있는지가 매핑 가능성을 가른다."""
    body = json.dumps({
        "code": 200,
        "data": {
            "product_basic": {"product_name": "x"},
            "product_option": {
                "option_type": "MIX",
                "mix_option_list": [{
                    "mix_option_product_cd": "OPT-1",
                    "mix_option_name1": "색상",
                    "mix_stock_info": 5,
                }],
            },
        },
    })
    shape = _next_engine_detail_shape(body)

    assert "mix_option_product_cd" in shape.option_item_keys
    assert "mix_option_list" in shape.option_fields
    # 옵션 코드 **값**은 담기지 않는다.
    assert "OPT-1" not in json.dumps(shape.__dict__, ensure_ascii=False)


def test_stock_keys_are_observed_not_invented():
    """`stock_qty` 같은 이름을 지어내 대조하면 "없다" 는 잘못된 결론이 난다.

    실제로 있는 이름을 수집한다.
    """
    body = json.dumps({
        "code": 200,
        "data": {"product_basic": {"product_name": "x"},
                 "product_option": {"mix_option_list": [
                     {"mix_stock_info": 1, "mix_stock_mng_flg": "Y"}]}},
    })
    shape = _next_engine_detail_shape(body)

    assert shape.stock_keys == ["mix_stock_info", "mix_stock_mng_flg"]
    # 창고 개념이 없으면 없다고 말한다. 지어내지 않는다.
    assert shape.warehouse_keys == []
    assert shape.storehouse_keys == []


def test_detail_shape_survives_garbage():
    """방어적 파싱. 상대 응답이 바뀌어도 터지지 않는다."""
    for body in ("", "not json", "[]", '"str"', "{}", '{"data": null}',
                 '{"data": {}}', '{"data": []}'):
        shape = _next_engine_detail_shape(body)
        assert shape.valid is False


# ── Seller OS 를 건드리지 않는다 ──────────────────────────────────────────

def test_diagnose_module_never_writes_to_seller_os():
    """진단은 읽기만 한다.

    `original_price` 가 원가를 바꾸거나, 외부 재고가 재고원장을 바꾸는 경로가
    **애초에 없어야** 한다. 코드로 막는 것보다 없는 것이 확실하다.
    """
    source = (Path(__file__).resolve().parents[3]
              / "scripts" / "auth_diagnose.py").read_text(encoding="utf-8")

    for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM",
                      "cost_amount", "inventory_ledger", "sales_order",
                      "db.commit()"):
        assert forbidden not in source, f"진단 모듈에 {forbidden} 이 있으면 안 된다"
