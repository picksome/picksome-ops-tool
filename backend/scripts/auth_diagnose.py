"""판매처 API 인증 진단 — **읽기 전용, 수동 실행 전용**.

무엇을 하나
---------
자격증명으로 **가장 작은 인증 호출 하나**만 해 보고 결과를 분류한다.
데이터를 가져와 DB 에 넣지 않는다 — 인증과 수집을 섞으면 실패했을 때 어느
쪽이 문제인지 알 수 없다.

```
credential → authentication → 결과 분류
```

⚠️ 값은 출력하지 않는다
--------------------
자격증명도, 발급된 토큰도, 응답 본문의 개인정보도 찍지 않는다. 나오는 것은
**분류 결과와 진단 힌트**뿐이다.

⚠️ 전체 테스트에 넣지 않는다
------------------------
실제 외부 API 를 부른다. pytest/Playwright 에 넣으면 네트워크나 상대 서버
사정으로 빨간 불이 뜨고, 그 순간부터 아무도 빨간 불을 믿지 않는다.

401 을 전부 "키가 틀렸다" 로 처리하지 않는다
--------------------------------------
같은 401 이라도 서명 생성 오류, 권한 미부여, IP 미등록, 시계 어긋남이 전부
다른 조치를 요구한다. 구분해서 보고한다.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

# ── 결과 분류 ────────────────────────────────────────────────────
AUTH_SUCCESS = "AUTH_SUCCESS"
INVALID_CREDENTIAL = "INVALID_CREDENTIAL"
SIGNATURE_ERROR = "SIGNATURE_ERROR"
SIGNATURE_EXPIRED = "SIGNATURE_EXPIRED"
INVALID_SIGNATURE = "INVALID_SIGNATURE"
CREDENTIAL_FORMAT_MISMATCH = "CREDENTIAL_FORMAT_MISMATCH"
TOKEN_ERROR = "TOKEN_ERROR"
PERMISSION_DENIED = "PERMISSION_DENIED"
IP_NOT_ALLOWED = "IP_NOT_ALLOWED"
ACCOUNT_NOT_LINKED = "ACCOUNT_NOT_LINKED"
RATE_LIMITED = "RATE_LIMITED"
API_DOCUMENTATION_MISSING = "API_DOCUMENTATION_MISSING"
NETWORK_ERROR = "NETWORK_ERROR"
UPSTREAM_ERROR = "UPSTREAM_ERROR"
UNKNOWN_AUTH_ERROR = "UNKNOWN_AUTH_ERROR"


@dataclass
class Diagnosis:
    service: str
    result: str
    http_status: int | None = None
    #: 외부가 준 오류 **코드**. 메시지 원문은 담지 않는다 — 토큰이 섞여 오는
    #: 응답이 있다.
    upstream_code: str | None = None
    hints: list[str] = field(default_factory=list)
    user_action: str | None = None
    #: 응답이 문서에 적힌 모양이었는가. 인증(통과/거부)과 다른 축이다 —
    #: 인증은 됐는데 형식이 다르면 커넥터를 만들 수 없다.
    schema_valid: bool | None = None
    #: 상대가 알려 준 총 건수. **값이 아니라 개수다.**
    total_count: int | None = None
    #: 그 건수를 어디서 읽었는지(응답 필드명 또는 `item_count`).
    count_source: str | None = None
    #: 응답에서 실제로 확인된 **필드 이름**. 그 필드의 값은 담지 않는다.
    observed_fields: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": self.service, "result": self.result,
            "http_status": self.http_status, "upstream_code": self.upstream_code,
            "hints": self.hints, "user_action": self.user_action,
            "schema_valid": self.schema_valid, "total_count": self.total_count,
            "count_source": self.count_source,
            "observed_fields": self.observed_fields,
        }


def _safe_code(body: str) -> str | None:
    """응답에서 **코드만** 뽑는다. 메시지 원문은 쓰지 않는다."""
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return None
    for key in ("code", "errorCode", "error", "error_code", "resultCode"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, str) and value:
            return value[:80]
    return None


# ──────────────────────────────────────────────────────────────────────────
# NAVER 커머스 API
# ──────────────────────────────────────────────────────────────────────────

NAVER_TOKEN_URL = "https://api.commerce.naver.com/external/v1/oauth2/token"


def naver_signature(client_id: str, client_secret: str, timestamp_ms: int) -> str:
    """공식 전자서명.

    ```
    password = client_id + "_" + timestamp(ms)
    salt     = client_secret          ← 그대로 bcrypt salt 로 쓴다
    sign     = base64(bcrypt(password, salt))
    ```

    `client_secret` 이 **bcrypt salt 형식**(`$2a$04$...`)이어야 성립한다.
    커머스 API 가 그 형식으로 발급하기 때문이다. 다른 형식이면 여기서 실패하고,
    그것은 "키가 틀렸다" 가 아니라 **다른 종류의 자격증명**이라는 뜻이다.
    """
    import bcrypt

    password = f"{client_id}_{timestamp_ms}".encode()
    hashed = bcrypt.hashpw(password, client_secret.encode())
    import base64

    return base64.b64encode(hashed).decode()


def diagnose_naver(fields: dict[str, str], *, timeout: float) -> Diagnosis:
    client_id = fields["client_id"]
    client_secret = fields["client_secret"]
    d = Diagnosis("NAVER", UNKNOWN_AUTH_ERROR)

    timestamp = int(time.time() * 1000)
    try:
        sign = naver_signature(client_id, client_secret, timestamp)
    except ValueError as exc:
        # bcrypt 가 salt 를 거부했다. 네트워크를 타기도 전이다.
        d.result = CREDENTIAL_FORMAT_MISMATCH
        d.upstream_code = type(exc).__name__
        d.hints = [
            "client_secret 이 bcrypt salt 형식($2a$04$…)이 아닙니다.",
            "네이버 커머스API 의 client_secret 은 그 형식으로 발급됩니다.",
            "네이버 개발자센터(developers.naver.com)의 검색/지도용 "
            "client_secret 은 형식이 달라 커머스API 에 쓸 수 없습니다.",
        ]
        d.user_action = (
            "커머스API 센터(apicenter.commerce.naver.com)에서 발급한 "
            "애플리케이션의 client_id/client_secret 인지 확인해 주세요. "
            "값을 복사할 때 잘리지 않았는지도 함께 봐 주세요."
        )
        return d
    except Exception as exc:  # noqa: BLE001
        d.result = SIGNATURE_ERROR
        d.upstream_code = type(exc).__name__
        return d

    # 공식 규격: form-urlencoded, type=SELF (커머스API 센터에서 만든
    # 판매자 애플리케이션은 SELF 만 발급 가능).
    form = {
        "client_id": client_id,
        "timestamp": str(timestamp),
        "client_secret_sign": sign,
        "grant_type": "client_credentials",
        "type": "SELF",
    }

    try:
        res = httpx.post(
            NAVER_TOKEN_URL, data=form, timeout=timeout,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as exc:
        d.result = NETWORK_ERROR
        d.upstream_code = type(exc).__name__
        return d

    d.http_status = res.status_code
    d.upstream_code = _safe_code(res.text)

    if res.status_code == 200:
        # 토큰을 저장하지도 출력하지도 않는다. 발급됐다는 사실만 본다.
        try:
            issued = bool(json.loads(res.text).get("access_token"))
        except Exception:  # noqa: BLE001
            issued = False
        d.result = AUTH_SUCCESS if issued else TOKEN_ERROR
        if not issued:
            d.hints.append("200 인데 access_token 이 없습니다.")
        return d

    if res.status_code == 400:
        d.result = SIGNATURE_ERROR
        d.hints = [
            "요청 형식 또는 서명 문제입니다. 키가 틀렸다고 단정할 수 없습니다.",
            "확인: grant_type=client_credentials / type=SELF / "
            "timestamp(ms) / form-urlencoded / 서명 생성 절차",
            "로컬 시계가 어긋나면 timestamp 가 유효 범위를 벗어납니다.",
        ]
        d.user_action = "없음 — 코드/시계 확인이 먼저입니다."
    elif res.status_code == 403:
        d.result = PERMISSION_DENIED
        d.hints = [
            "인증 자체보다 권한·연결 문제일 수 있습니다.",
            "확인: 애플리케이션에 부여된 API 권한, 판매자 계정 연결, type(SELF/SELLER)",
        ]
        d.user_action = (
            "커머스API 센터에서 애플리케이션의 API 사용 권한과 "
            "판매자 계정 연결 상태를 확인해 주세요."
        )
    elif res.status_code == 401:
        d.result = INVALID_CREDENTIAL
        d.hints = ["client_id 또는 서명이 거부됐습니다."]
    elif res.status_code == 429:
        d.result = RATE_LIMITED
        d.user_action = "잠시 후 다시 시도해 주세요."
    elif res.status_code >= 500:
        d.result = UPSTREAM_ERROR
        d.user_action = "네이버 쪽 오류입니다. 잠시 후 다시 확인합니다."
    return d



# ──────────────────────────────────────────────────────────────────────────
# COUPANG OPEN API
# ──────────────────────────────────────────────────────────────────────────

COUPANG_BASE = "https://api-gateway.coupang.com"


def coupang_authorization(
    method: str, path: str, query: str, access_key: str, secret_key: str,
) -> str:
    """공식 CEA HMAC 서명.

    ```
    message   = datetime + method + path + query
    datetime  = yyMMdd'T'HHmmss'Z'   (UTC)
    signature = HMAC-SHA256(secret_key, message) → hex
    header    = CEA algorithm=HmacSHA256, access-key=…, signed-date=…, signature=…
    ```

    **매 요청마다 새로 만든다.** 서명에 시각이 들어 있어 오래 쓰면 만료된다.
    """
    import hashlib
    import hmac
    from datetime import datetime

    signed_date = datetime.now(UTC).strftime("%y%m%dT%H%M%SZ")
    message = f"{signed_date}{method}{path}{query}"
    signature = hmac.new(
        secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    return (f"CEA algorithm=HmacSHA256, access-key={access_key}, "
            f"signed-date={signed_date}, signature={signature}")


def diagnose_coupang(fields: dict[str, str], *, timeout: float) -> Diagnosis:
    """가장 작은 읽기 호출로 인증만 확인한다.

    발주서 목록을 **좁은 기간**으로 조회한다. 데이터를 가져오는 것이 목적이
    아니라 서명이 통과하는지 보는 것이라, 결과가 0건이어도 인증 성공이다.

    쓰기 API 는 부르지 않는다 — 운영 주문 상태를 건드릴 수 있다.
    """
    from datetime import datetime, timedelta

    d = Diagnosis("COUPANG", UNKNOWN_AUTH_ERROR)
    vendor_id = fields["vendor_id"]
    path = f"/v2/providers/openapi/apis/api/v5/vendors/{vendor_id}/ordersheets"

    # 좁은 범위. 개인정보 노출을 최소화한다.
    #
    # ⚠️ **서명한 문자열과 실제로 보내는 문자열이 같아야 한다.**
    # 공식 예시가 `createdAtFrom=2025-07-15+09:00` 처럼 공백을 `+` 로 쓰는 이유다.
    # 공백을 그대로 두면 HTTP 클라이언트가 `%20` 으로 바꿔 보내고, 그러면
    # 서명 대상과 전송 내용이 달라져 401 이 난다. 처음에 그렇게 만들어 실패했다.
    now = datetime.now()
    query = (f"createdAtFrom={(now - timedelta(hours=6)).strftime('%Y-%m-%d+%H:%M')}"
             f"&createdAtTo={now.strftime('%Y-%m-%d+%H:%M')}"
             f"&maxPerPage=1&status=ACCEPT")

    try:
        auth = coupang_authorization(
            "GET", path, query, fields["access_key"], fields["secret_key"],
        )
    except Exception as exc:  # noqa: BLE001
        d.result = SIGNATURE_ERROR
        d.upstream_code = type(exc).__name__
        return d

    try:
        # 미리 만든 URL 문자열을 그대로 쓴다. 다시 인코딩되면 서명이 깨진다.
        res = httpx.get(
            httpx.URL(f"{COUPANG_BASE}{path}?{query}"), timeout=timeout,
            headers={"Authorization": auth, "X-EXTENDED-TIMEOUT": "90000"},
        )
    except httpx.HTTPError as exc:
        d.result = NETWORK_ERROR
        d.upstream_code = type(exc).__name__
        return d

    d.http_status = res.status_code
    d.upstream_code = _safe_code(res.text)
    body = res.text.lower()

    if res.status_code == 200:
        d.result = AUTH_SUCCESS
        d.hints.append("인증 성공. 조회 결과 건수와 무관하게 서명이 통과했습니다.")
        return d

    # 같은 401/403 이라도 원인이 다르다. 문구로 구분한다.
    if "not allowed ip" in body or "not allowed" in body and "ip" in body:
        d.result = IP_NOT_ALLOWED
        d.hints = [
            "서명은 통과했습니다(401 이 아니라 403 이고, 응답에 signature/"
            "expired 문구가 없습니다). 즉 vendor_id·access_key·secret_key 와 "
            "HMAC 구현은 맞습니다.",
            "**키가 잘못된 것이 아닙니다.**",
            "응답이 접근 불가와 IP 를 언급합니다. 이 키가 연동업체(넥스트엔진 "
            "등)용으로 발급된 것이면 자체 서버에서 호출할 수 없습니다.",
            "vendor 문구도 함께 있어, 키와 vendor_id 의 소속이 다를 가능성도 "
            "완전히 배제하지는 못했습니다.",
        ]
        d.user_action = (
            "쿠팡 WING > 판매자정보 > API 연동 설정에서 연동 방식(자체개발/"
            "연동업체)과 등록된 IP 를 확인해 주세요. 기존 연동업체 키를 "
            "재발급하면 그 연동이 끊어지므로, 자체개발용으로 **별도 발급**하고 "
            "이 서버의 outbound IP 를 등록하는 것이 안전합니다."
        )
    elif "expired" in body:
        d.result = SIGNATURE_EXPIRED
        d.hints = [
            "서명에 들어간 시각이 만료됐습니다. 키 문제가 아닙니다.",
            "서버 시계가 UTC 기준으로 어긋났는지 확인하세요.",
        ]
    elif "invalid signature" in body or "signature" in body:
        d.result = INVALID_SIGNATURE
        d.hints = [
            "서명이 거부됐습니다. 확인 순서: vendor_id → access_key → "
            "secret_key → HMAC 구현 → method → path → query → 시각 → 타임존",
        ]
    elif res.status_code == 401:
        d.result = INVALID_CREDENTIAL
    elif res.status_code == 403:
        d.result = PERMISSION_DENIED
        d.hints = ["인증은 됐으나 이 API 사용 권한이 없을 수 있습니다."]
        d.user_action = "WING 에서 해당 API 사용 권한을 확인해 주세요."
    elif res.status_code == 429:
        d.result = RATE_LIMITED
    elif res.status_code >= 500:
        d.result = UPSTREAM_ERROR
    return d


# ──────────────────────────────────────────────────────────────────────────
# NEXT ENGINE
# ──────────────────────────────────────────────────────────────────────────

NEXT_ENGINE_BASE = "https://neapi.next-engine.co.kr"

#: 상품 **목록 조회**. POST 지만 의미는 READ 다.
#:
#: 이 파일에 다른 NEXT ENGINE endpoint 를 넣지 않는다. 상품 등록/수정/삭제는
#: 경로가 한 글자 차이(`/products` vs `/products/search`)라, 상수로 옆에 두면
#: 언젠가 잘못 고른다. 진단은 읽기만 한다.
NEXT_ENGINE_PRODUCT_SEARCH_PATH = "/api/v1/ne/products/search"

#: 상품 **단건 조회**. `{product_cd}` 자리만 채운다.
#:
#: 같은 경로에 PATCH/PUT/DELETE 가 걸려 있다(공식 문서). 그래서 이 상수를
#: 쓰는 곳은 **GET 하나뿐**이어야 하고, method 를 인자로 받는 helper 를
#: 만들지 않는다 — 인자로 받는 순간 잘못된 method 가 들어올 자리가 생긴다.
NEXT_ENGINE_PRODUCT_DETAIL_PATH = "/api/v1/ne/products/{product_cd}"

#: 응답에 있어야 한다고 문서가 말하는 상품 필드. 실제로 왔는지 대조한다.
NEXT_ENGINE_PRODUCT_FIELDS = (
    "product_cd", "product_name", "sell_price",
    "original_price", "custom_price", "partner_product_cd",
)

#: 단건 조회에서 추가로 확인할 필드. 목록에는 없을 수 있는 것들이다.
NEXT_ENGINE_DETAIL_FIELDS = (
    "option_type", "single_option", "mono_option_list", "mix_option_list",
)

#: 재고·창고 관련 필드를 **이름으로** 찾는다.
#:
#: 정확한 필드명을 모르는 채로 `stock_qty` 같은 이름을 지어내 대조하면
#: "없다" 는 잘못된 결론이 난다. 부분 일치로 **실제로 있는 이름을 수집**하고,
#: 그 이름 목록을 그대로 보고한다 — 추측이 아니라 관찰이다.
NEXT_ENGINE_STOCK_KEY_HINTS = ("stock", "zaiko", "inventory", "quantity", "qty")
NEXT_ENGINE_WAREHOUSE_KEY_HINTS = ("warehouse",)
NEXT_ENGINE_STOREHOUSE_KEY_HINTS = ("storehouse",)


def next_engine_headers(api_key: str) -> dict[str, str]:
    """공식 인증 헤더.

    ```
    Api-Auth-Key: <발급받은 키>
    ```

    `Authorization` 이 아니다. 서명도 타임스탬프도 없다 — 키를 그대로 싣는다.
    그래서 **이 헤더 dict 는 로깅 대상이 아니다.** 함수로 떼어 둔 이유가
    그것이다: 테스트가 헤더 구성만 검증하고, 호출부는 만들어 바로 넘긴다.
    """
    if not api_key:
        raise ValueError("api_key 가 비어 있습니다.")
    return {
        "Api-Auth-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


@dataclass
class ProductShape:
    """응답의 **모양**. 값은 하나도 담지 않는다."""

    valid: bool = False
    total: int | None = None
    #: 건수를 어디서 읽었는가. `"count"` 같은 응답 필드명이거나,
    #: 그런 필드가 없어 배열 길이를 센 경우 `"item_count"` 다.
    #:
    #: 이걸 구분하지 않으면 `total=9` 를 보고 "전체 9건" 인지 "이번 페이지에
    #: 9건이 왔다(=size 가 안 먹었다)" 인지 알 수 없다. 페이지네이션이 어떻게
    #: 도는지는 커넥터를 만들 때 반드시 알아야 한다.
    count_source: str | None = None
    fields: list[str] = field(default_factory=list)


def _next_engine_shape(body: str) -> ProductShape:
    """응답 **모양만** 본다. 값은 돌려주지 않는다.

    상품명도 가격도 나가지 않는다 — 이 함수가 값을 만지지 않으면 호출부가
    실수할 여지가 없다.
    """
    shape = ProductShape()
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return shape
    if not isinstance(data, dict):
        return shape

    # 건수 필드는 구현마다 표기가 다르다. 있는 것만 읽고 없으면 None 이다 —
    # 0 으로 대신하면 "없음" 과 "0건" 이 구분되지 않는다.
    for k in ("count", "total_count", "totalCount", "total"):
        v = data.get(k)
        if isinstance(v, int):
            shape.total = v
            shape.count_source = k
            break

    items: list[Any] = []
    for k in ("data", "items", "products", "result", "list"):
        v = data.get(k)
        if isinstance(v, list):
            items = v
            # 최상위가 dict 이고 목록 키를 찾았으면 모양은 맞다. 0건이어도
            # 유효하다 — 인증 확인이 목적이라 건수는 판정 기준이 아니다.
            shape.valid = True
            break

    if items and isinstance(items[0], dict):
        shape.fields = [f for f in NEXT_ENGINE_PRODUCT_FIELDS if f in items[0]]
    if shape.total is None and shape.valid:
        shape.total = len(items)
        shape.count_source = "item_count"
    return shape


def _first_product_cd(body: str) -> str | None:
    """목록 응답에서 `product_cd` **하나만** 꺼낸다.

    단건 조회에 쓸 씨앗이다. 상품 코드는 비밀도 개인정보도 아니지만, 그래도
    필요한 한 건만 들고 나온다 — 목록 전체를 메모리에 들고 다닐 이유가 없다.
    """
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    for k in ("data", "items", "products", "result", "list"):
        v = data.get(k)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            cd = v[0].get("product_cd")
            return str(cd) if cd not in (None, "") else None
    return None


def _collect_keys(node: Any, out: set[str], depth: int = 0) -> None:
    """중첩 구조의 **키 이름**을 모은다. 값은 건드리지 않는다.

    옵션·재고가 어느 깊이에 있는지 문서만으로는 알 수 없어서, 최상위만 보면
    "없다" 는 잘못된 결론이 난다. 깊이를 6 으로 제한하는 것은 순환/거대 구조에
    대한 안전장치다.
    """
    if depth > 6:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            out.add(str(k))
            _collect_keys(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node[:3]:      # 배열은 앞 몇 개면 스키마가 드러난다
            _collect_keys(v, out, depth + 1)


@dataclass
class ProductDetailShape:
    """단건 조회 응답의 **모양**. 값은 하나도 담지 않는다."""

    valid: bool = False
    #: 응답 봉투의 API 레벨 코드. 값이 아니라 **상태**다.
    api_code: Any = None
    #: 장애 추적용 식별자가 오는가. 값은 담지 않는다.
    process_id_present: bool = False
    #: `data` 아래의 구획 이름(product_basic, product_option, …).
    sections: list[str] = field(default_factory=list)
    #: 문서에 적힌 상품 필드 중 실제로 온 것.
    fields: list[str] = field(default_factory=list)
    #: 문서에 적혀 있는데 **오지 않은** 것. 없는 것을 아는 게 더 중요하다.
    missing_fields: list[str] = field(default_factory=list)
    #: 옵션 관련 필드 중 실제로 온 것.
    option_fields: list[str] = field(default_factory=list)
    #: 옵션 **한 건**의 키 이름. 옵션 단위 식별자가 있는지가 여기서 갈린다.
    option_item_keys: list[str] = field(default_factory=list)
    #: 이름에 재고/창고가 들어간 **실제 키 이름**. 우리가 지어낸 이름이 아니다.
    stock_keys: list[str] = field(default_factory=list)
    warehouse_keys: list[str] = field(default_factory=list)
    storehouse_keys: list[str] = field(default_factory=list)
    #: 가격 필드의 **파이썬 타입 이름**. 값이 아니라 타입이다 —
    #: `original_price` 가 문자열로 오는지 숫자로 오는지는 커넥터가 알아야 한다.
    price_types: dict[str, str] = field(default_factory=dict)
    #: 응답에 존재하는 전체 키 개수. 스키마 규모 감각용.
    key_count: int = 0


def _find_nested(node: Any, name: str, depth: int = 0) -> Any:
    """중첩 어디에 있든 그 이름의 값을 찾는다. 첫 번째만 돌려준다."""
    if depth > 6:
        return None
    if isinstance(node, dict):
        if name in node:
            return node[name]
        for v in node.values():
            found = _find_nested(v, name, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node[:3]:
            found = _find_nested(v, name, depth + 1)
            if found is not None:
                return found
    return None


def _next_engine_detail_shape(body: str) -> ProductDetailShape:
    """단건 조회 응답을 **이름으로만** 분석한다.

    상품명·가격 **값**은 어디에도 담기지 않는다. 담기는 것은 키 이름과 타입뿐이다.

    `product_cd` 를 유효성 기준으로 쓰지 않는다
    ---------------------------------------
    처음에는 `product_cd` 가 응답에 있는지로 판정했는데, 실제 응답에는
    **없다.** 조회 키가 경로 인자라 본문에 다시 싣지 않는 설계다. 그걸 모르고
    "스키마 이상" 으로 보고할 뻔했다 — 없는 것을 근거로 삼으면 멀쩡한 응답을
    실패로 읽는다.
    """
    shape = ProductDetailShape()
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return shape
    if not isinstance(data, dict):
        return shape

    shape.api_code = data.get("code")
    shape.process_id_present = bool(data.get("process_id"))

    payload = data.get("data")
    if not isinstance(payload, dict) or not payload:
        return shape
    shape.sections = sorted(payload)

    keys: set[str] = set()
    _collect_keys(payload, keys)
    shape.key_count = len(keys)

    shape.fields = [f for f in NEXT_ENGINE_PRODUCT_FIELDS if f in keys]
    shape.missing_fields = [f for f in NEXT_ENGINE_PRODUCT_FIELDS if f not in keys]
    shape.option_fields = [f for f in NEXT_ENGINE_DETAIL_FIELDS if f in keys]

    # 상품 본문이 왔는가로 판정한다. 문서에 적힌 필드가 하나라도 오면 유효다.
    shape.valid = bool(shape.fields)

    for list_name in ("mix_option_list", "mono_option_list"):
        found = _find_nested(payload, list_name)
        if isinstance(found, list) and found and isinstance(found[0], dict):
            shape.option_item_keys = sorted(found[0])
            break

    lower = {k: k.lower() for k in keys}
    shape.stock_keys = sorted(
        k for k, lo in lower.items()
        if any(h in lo for h in NEXT_ENGINE_STOCK_KEY_HINTS))
    shape.warehouse_keys = sorted(
        k for k, lo in lower.items()
        if any(h in lo for h in NEXT_ENGINE_WAREHOUSE_KEY_HINTS))
    shape.storehouse_keys = sorted(
        k for k, lo in lower.items()
        if any(h in lo for h in NEXT_ENGINE_STOREHOUSE_KEY_HINTS))

    for f in ("original_price", "sell_price", "custom_price"):
        found = _find_nested(payload, f)
        if found is not None:
            # 타입 이름만 남긴다. 값은 남기지 않는다.
            shape.price_types[f] = type(found).__name__
    return shape


def next_engine_product_detail(
    api_key: str, product_cd: str, *, timeout: float,
) -> tuple[Diagnosis, ProductDetailShape]:
    """상품 **단건 조회**. GET 하나뿐이다.

    같은 경로에 PATCH/PUT/DELETE 가 있지만 이 함수는 method 를 인자로 받지
    않는다 — 받는 순간 잘못된 method 가 들어올 자리가 생긴다.
    """
    d = Diagnosis("NEXT_ENGINE_DETAIL", UNKNOWN_AUTH_ERROR)
    shape = ProductDetailShape()
    if not product_cd:
        d.result = UNKNOWN_AUTH_ERROR
        d.hints = ["조회할 product_cd 가 없습니다."]
        return d, shape

    url = NEXT_ENGINE_BASE + NEXT_ENGINE_PRODUCT_DETAIL_PATH.format(
        product_cd=quote(str(product_cd), safe=""))
    try:
        res = httpx.get(url, timeout=timeout, headers=next_engine_headers(api_key))
    except httpx.HTTPError as exc:
        d.result = NETWORK_ERROR
        d.upstream_code = type(exc).__name__
        return d, shape

    d.http_status = res.status_code
    d.upstream_code = _safe_code(res.text)

    if res.status_code == 200:
        shape = _next_engine_detail_shape(res.text)
        d.schema_valid = shape.valid
        d.observed_fields = shape.fields
        d.result = AUTH_SUCCESS if shape.valid else UNKNOWN_AUTH_ERROR
        if not shape.valid:
            d.hints.append("200 이지만 product_cd 를 찾지 못했습니다.")
        return d, shape

    if res.status_code == 401:
        d.result = INVALID_CREDENTIAL
    elif res.status_code == 403:
        d.result = PERMISSION_DENIED
        d.hints = ["목록은 되는데 단건이 막혔다면 권한이 endpoint 별로 나뉜 것입니다."]
    elif res.status_code == 404:
        d.result = API_DOCUMENTATION_MISSING
        d.hints = ["경로가 없거나 그 product_cd 가 없습니다."]
    elif res.status_code == 429:
        d.result = RATE_LIMITED
    elif res.status_code >= 500:
        d.result = UPSTREAM_ERROR
    return d, shape


def diagnose_next_engine(fields: dict[str, str], *, timeout: float) -> Diagnosis:
    """상품 **목록 조회** 한 번으로 인증만 확인한다.

    POST 지만 읽기다
    --------------
    `/products/search` 는 조회 조건을 body 로 받는 검색 endpoint 다. 상품을
    만들거나 고치지 않는다. 등록(`POST /products`)·수정·삭제는 **부르지
    않는다** — 운영 상품이 바뀐다.

    가장 작은 요청을 보낸다
    -------------------
    ```
    page = 1
    size = 1
    ```

    데이터를 모으러 온 것이 아니다. 크게 부르면 남의 서버에 부담을 주고,
    응답에 담긴 상품 정보가 우리 쪽 로그·메모리에 불필요하게 오래 남는다.
    """
    d = Diagnosis("NEXT_ENGINE", UNKNOWN_AUTH_ERROR)
    api_key = fields.get("api_key", "")

    try:
        headers = next_engine_headers(api_key)
    except ValueError as exc:
        d.result = CREDENTIAL_FORMAT_MISMATCH
        d.upstream_code = type(exc).__name__
        d.hints = ["api_key 가 비어 있습니다."]
        d.user_action = "자격증명 파일 또는 SecretStore 등록 상태를 확인해 주세요."
        return d

    try:
        res = httpx.post(
            f"{NEXT_ENGINE_BASE}{NEXT_ENGINE_PRODUCT_SEARCH_PATH}",
            timeout=timeout, headers=headers, json={"page": 1, "size": 1},
        )
    except httpx.HTTPError as exc:
        d.result = NETWORK_ERROR
        # 예외 문자열이 아니라 **타입 이름**만 남긴다. httpx 예외 메시지에는
        # 요청 URL 이 들어가고, 구현이 바뀌면 헤더가 딸려올 수도 있다.
        d.upstream_code = type(exc).__name__
        return d

    d.http_status = res.status_code
    d.upstream_code = _safe_code(res.text)

    if res.status_code == 200:
        shape = _next_engine_shape(res.text)
        d.result = AUTH_SUCCESS if shape.valid else UNKNOWN_AUTH_ERROR
        d.schema_valid = shape.valid
        d.total_count = shape.total
        d.count_source = shape.count_source
        d.observed_fields = shape.fields
        if shape.valid:
            d.hints.append(
                "인증 성공. 조회 결과 건수와 무관하게 키가 통과했습니다.")
        else:
            d.hints.append(
                "200 이지만 상품 목록 구조를 찾지 못했습니다. 인증은 통과했을 "
                "수 있으나 응답 형식이 예상과 다릅니다.")
        return d

    if res.status_code == 401:
        d.result = INVALID_CREDENTIAL
        d.hints = [
            "Api-Auth-Key 가 거부됐습니다.",
            "**키를 재발급하지 마세요.** 먼저 이 키가 이 API(상품)용으로 "
            "발급된 것인지, 유효기간이 남았는지 확인합니다.",
        ]
        d.user_action = "NEXT ENGINE 관리자에서 키의 용도와 유효기간을 확인해 주세요."
    elif res.status_code == 403:
        d.result = PERMISSION_DENIED
        d.hints = [
            "키 자체는 인식됐으나 이 API 사용 권한이 없거나 호출 IP 가 "
            "허용 목록에 없습니다.",
            "쿠팡에서 겪은 것과 같은 형태입니다 — **키가 틀린 것이 아닙니다.**",
        ]
        d.user_action = "상품 API 사용 권한과 허용 IP 등록 여부를 확인해 주세요."
    elif res.status_code == 404:
        d.result = API_DOCUMENTATION_MISSING
        d.hints = [
            f"endpoint 가 없습니다: {NEXT_ENGINE_PRODUCT_SEARCH_PATH}",
            "경로·버전이 문서와 다를 수 있습니다. 추측으로 다른 경로를 "
            "시도하지 않습니다.",
        ]
        d.user_action = "상품 API 문서의 정확한 경로를 확인해 주세요."
    elif res.status_code == 429:
        d.result = RATE_LIMITED
        d.hints = ["호출 한도에 걸렸습니다. 재시도하지 않습니다."]
    elif res.status_code >= 500:
        d.result = UPSTREAM_ERROR
        d.hints = ["상대 서버 오류입니다. 자격증명 문제가 아닙니다."]
    return d


def probe_next_engine_detail(
    fields: dict[str, str], *, timeout: float,
) -> tuple[Diagnosis, ProductDetailShape]:
    """단건 조회 진단. 외부 호출 **2회**(검색 1 + 단건 1).

    검색이 필요한 이유는 씨앗 `product_cd` 하나를 얻기 위해서다. 코드를
    지어내면 404 를 받고 "endpoint 가 없다" 는 잘못된 결론에 이른다.
    검색은 `size=1` 이라 목록을 끌어오지 않는다.
    """
    api_key = fields.get("api_key", "")
    d = Diagnosis("NEXT_ENGINE_DETAIL", UNKNOWN_AUTH_ERROR)
    try:
        headers = next_engine_headers(api_key)
    except ValueError:
        d.result = CREDENTIAL_FORMAT_MISMATCH
        d.hints = ["api_key 가 비어 있습니다."]
        return d, ProductDetailShape()

    try:
        seed = httpx.post(
            f"{NEXT_ENGINE_BASE}{NEXT_ENGINE_PRODUCT_SEARCH_PATH}",
            timeout=timeout, headers=headers, json={"page": 1, "size": 1},
        )
    except httpx.HTTPError as exc:
        d.result = NETWORK_ERROR
        d.upstream_code = type(exc).__name__
        return d, ProductDetailShape()

    if seed.status_code != 200:
        d.http_status = seed.status_code
        d.upstream_code = _safe_code(seed.text)
        d.hints = ["씨앗 product_cd 를 얻기 위한 검색이 실패했습니다."]
        return d, ProductDetailShape()

    product_cd = _first_product_cd(seed.text)
    if not product_cd:
        d.hints = ["검색 결과가 비어 있어 단건 조회할 product_cd 가 없습니다."]
        return d, ProductDetailShape()

    return next_engine_product_detail(api_key, product_cd, timeout=timeout)


# ──────────────────────────────────────────────────────────────────────────
# 등록
# ──────────────────────────────────────────────────────────────────────────

DIAGNOSERS = {
    "NAVER": diagnose_naver,
    "COUPANG": diagnose_coupang,
    "NEXT_ENGINE": diagnose_next_engine,
}

#: 아직 공식 기술 문서를 확보하지 못한 서비스.
#:
#: 자격증명이 있다는 이유만으로 endpoint 를 추측하지 않는다. 추측한 URL 로
#: 실패하면 "키가 틀렸다" 는 잘못된 결론에 이른다.
#:
#: NEXT ENGINE 은 여기서 뺐다 — **상품 API 문서만** 확보했고, 그 범위에서만
#: 진단한다. 주문·재고·클레임·정산은 여전히 문서가 없고, 있을 것으로 보고
#: endpoint 를 만들지 않는다(아래 `NEXT_ENGINE_UNDOCUMENTED_DOMAINS`).
NO_DOCUMENTATION = {
    "ELEVENST": (
        "11번가 OpenAPI 의 인증 방식과 endpoint 를 공식 문서로 확인하지 "
        "못했습니다."
    ),
}

#: NEXT ENGINE 에서 **아직 문서가 없는** 업무 영역.
#:
#: 상품 API 가 되니까 주문도 될 것이라고 보지 않는다. 인증이 통과했다는 사실은
#: 키가 유효하다는 뜻이지, 그 키로 어떤 API 를 부를 수 있는지를 말해 주지 않는다.
NEXT_ENGINE_UNDOCUMENTED_DOMAINS = ("ORDER", "INVENTORY", "CLAIM", "SETTLEMENT")


def _creds_from_secret_store(org_id: str, service: str) -> list[Any]:
    """등록된 자격증명으로 진단한다 — **평문 파일을 읽지 않는다.**

    이게 되면 부트스트랩 이후 그 파일이 런타임에 필요 없다는 뜻이다. 파일을
    계속 읽는 진단만 있으면 "지워도 되는가" 에 답할 수 없다.

    지금은 셀러툴(`external_account`)만 본다. 판매처 계정은
    `integration_account` 라 조회 경로가 다르고, 여기서 둘을 합치면 어느
    테이블을 봤는지 결과만 보고는 알 수 없게 된다.
    """
    from sqlalchemy import text

    from app.db.session import SessionLocal
    from app.services.credential_resolver import resolve_for_account
    from scripts.credential_bootstrap import (
        EXTERNAL_SYSTEM_CODE,
        ServiceCredential,
    )

    code = EXTERNAL_SYSTEM_CODE.get(service.upper())
    if code is None:
        raise SystemExit(
            f"SecretStore 조회 경로가 정의되지 않은 서비스입니다: {service}")

    db = SessionLocal()
    try:
        account_id = db.execute(text("""
            SELECT a.id FROM external_account a
              JOIN external_system s ON s.id = a.external_system_id
             WHERE a.org_id = :o AND s.code = :c AND a.deleted_at IS NULL
             ORDER BY a.created_at LIMIT 1
        """), {"o": org_id, "c": code}).scalar()
        if account_id is None:
            raise SystemExit(
                f"{code} 외부 계정이 없습니다. credential_bootstrap 을 먼저 "
                "실행해 주세요.")
        from uuid import UUID as _UUID

        resolved = resolve_for_account(
            db, org_id=_UUID(org_id), account_id=_UUID(str(account_id)))
    finally:
        db.close()

    cred = ServiceCredential(service.upper())
    # 평문은 여기서 dict 로 들어와 진단 함수까지만 간다. 출력되지 않는다.
    cred.fields.update(resolved.as_dict())
    return [cred]


def main() -> int:
    ap = argparse.ArgumentParser(description="판매처 API 인증 진단 (읽기 전용)")
    ap.add_argument("--secrets", default="../secrets",
                    help="자격증명 파일 또는 디렉터리")
    ap.add_argument("--service", default=None, help="한 서비스만 진단")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--source", choices=("file", "secretstore"), default="file",
                    help="자격증명 출처 (secretstore 는 --service·--org 필요)")
    ap.add_argument("--org", default=None, help="조직 UUID (--source secretstore)")
    ap.add_argument("--probe", choices=("auth", "detail"), default="auth",
                    help="auth=상품 목록으로 인증만, detail=상품 단건 조회까지")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.credential_bootstrap import parse_dir, parse_file

    if args.source == "secretstore":
        if not (args.service and args.org):
            print("--source secretstore 에는 --service 와 --org 가 필요합니다.")
            return 2
        creds = _creds_from_secret_store(args.org, args.service)
    else:
        target = Path(args.secrets)
        creds = parse_dir(target) if target.is_dir() else parse_file(target)

    results: list[Diagnosis] = []
    detail_shape: ProductDetailShape | None = None
    for cred in sorted(creds, key=lambda c: c.service):
        if args.service and cred.service != args.service.upper():
            continue
        if args.probe == "detail":
            # 단건 조회는 NEXT ENGINE 만 공식 문서를 확보했다. 다른 서비스에
            # 같은 모양의 endpoint 가 있을 것으로 보고 부르지 않는다.
            if cred.service != "NEXT_ENGINE":
                continue
            d, detail_shape = probe_next_engine_detail(
                cred.fields, timeout=args.timeout)
            results.append(d)
            continue
        if cred.service in NO_DOCUMENTATION:
            results.append(Diagnosis(
                cred.service, API_DOCUMENTATION_MISSING,
                hints=[NO_DOCUMENTATION[cred.service]],
                user_action="공식 기술 문서와 실제 응답 샘플이 필요합니다.",
            ))
            continue
        fn = DIAGNOSERS.get(cred.service)
        if fn is None:
            continue
        if not cred.complete:
            results.append(Diagnosis(
                cred.service, INVALID_CREDENTIAL,
                hints=[f"필수 항목 누락: {cred.missing}"],
                user_action="누락된 항목을 자격증명 파일에 채워 주세요.",
            ))
            continue
        results.append(fn(cred.fields, timeout=args.timeout))

    for d in results:
        print(f"\n[{d.service}]")
        print(f"  result        = {d.result}")
        if d.http_status is not None:
            print(f"  http_status   = {d.http_status}")
        if d.upstream_code:
            print(f"  upstream_code = {d.upstream_code}")
        if d.schema_valid is not None:
            print(f"  schema_valid  = {d.schema_valid}")
        if d.total_count is not None:
            print(f"  total_count   = {d.total_count} (출처: {d.count_source})")
        if d.observed_fields:
            # 필드 **이름**만이다. 값은 어디에도 없다.
            print(f"  fields_seen   = {d.observed_fields}")
        if detail_shape is not None and d.service == "NEXT_ENGINE_DETAIL":
            # 전부 **이름과 타입**이다. 상품명도 가격도 없다.
            s = detail_shape
            print(f"  api_code      = {s.api_code}")
            print(f"  process_id    = {'있음' if s.process_id_present else '없음'}")
            print(f"  sections      = {s.sections}")
            print(f"  key_count     = {s.key_count}")
            print(f"  missing       = {s.missing_fields or '없음'}")
            print(f"  option_fields = {s.option_fields or '없음'}")
            print(f"  option_keys   = {s.option_item_keys or '없음'}")
            print(f"  price_types   = {s.price_types or '없음'}")
            print(f"  stock_keys    = {s.stock_keys or '없음'}")
            print(f"  warehouse_keys= {s.warehouse_keys or '없음'}")
            print(f"  storehouse_ks = {s.storehouse_keys or '없음'}")
        for h in d.hints:
            print(f"  · {h}")
        if d.user_action:
            print(f"  사용자 조치   : {d.user_action}")

    return 0 if any(d.result == AUTH_SUCCESS for d in results) else 1


if __name__ == "__main__":
    sys.exit(main())
