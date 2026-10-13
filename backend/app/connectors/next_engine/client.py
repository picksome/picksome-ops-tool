"""NEXT ENGINE 주문 조회 클라이언트 — **GET 만 있다.**

쓰기 endpoint 를 이 파일에 두지 않는다
----------------------------------
공식 문서에는 `PUT` 이 네 개 있다(주문확인·몰주문확인·몰배송중·상태변경).
전부 실제 쇼핑몰에 나가는 동작이다.

```
PUT /api/v1/ne/orders/internal_confirmation
PUT /api/v1/ne/orders/order_confirmation
PUT /api/v1/ne/orders/order_shipping
PUT /api/v1/ne/orders/update
```

이 클래스에는 `httpx.get` 하나뿐이고 method 를 인자로 받는 helper 도 없다.
경로 문자열조차 두지 않는다 — 없는 것을 잘못 부를 수는 없다.

실제 API 를 재 보고 알아낸 것
--------------------------
spec 에 없고 응답으로만 알 수 있었던 제약이다.

```
조회 기간          한 요청 최대 10일   (넘기면 400)
order_status_divi  필수               "전체 주문" 을 한 번에 받는 방법이 없다
총건수             응답에 없다         짧은 페이지로 끝을 판단한다
과거 구간          10일 창이면 조회된다  (40일 전 창에서 7건 확인)
```

마지막 줄이 중요하다. **10일 제한은 요청당 폭이지 조회 가능 범위가 아니다.**
백필은 10일씩 잘라서 과거로 갈 수 있다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.connectors.next_engine.normalize import ExternalOrderLine, normalize_order_line
from app.core.exceptions import DomainError

NEXT_ENGINE_BASE = "https://neapi.next-engine.co.kr"

#: 주문 조회. **이 파일의 유일한 주문 경로다.**
ORDER_LIST_PATH = "/api/v1/ne/orders"

#: 실제 400 응답으로 확인한 제약: `최대 10일까지 조회할수 있습니다`.
#: 문서에는 없다. 넘겨서 400 을 받고 나서 알기보다 미리 막는다.
NEXT_ENGINE_MAX_WINDOW_DAYS = 10

#: 공식 코드표(문서 그대로). 이름으로 의미를 추측해 넣지 않았다.
ALL_ORDER_STATUSES = ("01", "02", "03", "04", "05", "25", "29")

#: 한 번에 받아 올 행 수. 문서 예시가 30 이다.
DEFAULT_PAGE_SIZE = 30

#: 페이지를 도는 최대 횟수. **무한 페이지네이션을 구조적으로 막는다.**
#:
#: 총건수가 없어서 종료 조건이 "짧은 페이지" 하나뿐이다. 벤더가 항상 가득 찬
#: 페이지를 돌려주는 버그를 내면 영원히 돈다 — 그 사고를 상수 하나로 막는다.
MAX_PAGES = 100


class NextEngineWindowError(DomainError):
    """조회 창이 벤더 제한을 넘었다. **네트워크를 타기 전에** 막는다."""

    code = "NEXT_ENGINE_WINDOW_TOO_WIDE"
    http_status = 422


class NextEngineReadError(DomainError):
    """주문 조회 실패. **응답 본문을 메시지에 넣지 않는다** — 개인정보가 있다."""

    code = "NEXT_ENGINE_READ_FAILED"
    http_status = 502


class NextEnginePaginationError(NextEngineReadError):
    """페이지가 넘어가지 않는다. 같은 요청을 상한까지 반복하지 않고 멈춘다."""

    code = "NEXT_ENGINE_PAGINATION_STALLED"


class NextEngineSchemaError(NextEngineReadError):
    """응답이 우리가 아는 모양이 아니다.

    네트워크 실패와 **다르게** 다뤄야 한다. 재시도해도 결과가 같고, 사람이
    스키마 변경을 확인해야 한다. 여기서 0건으로 넘기면 "주문이 없었다" 가
    되어 매출이 조용히 빈다.
    """

    code = "NEXT_ENGINE_SCHEMA_CHANGED"


@dataclass(slots=True)
class OrderPage:
    """한 페이지 결과. 원본 dict 는 들고 나오지 않는다."""

    lines: list[ExternalOrderLine] = field(default_factory=list)
    http_status: int = 0
    api_code: Any = None
    #: 벤더 추적 식별자. 장애 문의에 쓴다 — 개인정보가 아니다.
    process_id: str | None = None


def _fmt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def split_windows(
    start: datetime, end: datetime, *, max_days: int = NEXT_ENGINE_MAX_WINDOW_DAYS,
) -> list[tuple[datetime, datetime]]:
    """긴 기간을 벤더가 받아 주는 폭으로 자른다.

    백필이 여기에 기댄다. 창을 겹치지 않게 자르되 **끝을 넘지 않는다** —
    넘으면 미래 구간을 조회하게 되고, 그건 결과가 없어서 조용히 지나간다.
    """
    if start > end:
        raise NextEngineWindowError("조회 시작이 종료보다 뒤입니다.")
    out: list[tuple[datetime, datetime]] = []
    cursor = start
    span = timedelta(days=max_days)
    while cursor < end:
        stop = min(cursor + span, end)
        out.append((cursor, stop))
        cursor = stop
    return out or [(start, end)]


class NextEngineOrderClient:
    """주문 **읽기** 전용 클라이언트.

    자격증명을 보관하지 않는다 — 호출할 때마다 받는다. 인스턴스가 키를 들고
    있으면 그 인스턴스가 로깅되는 순간 키가 샌다.
    """

    def __init__(self, *, base_url: str = NEXT_ENGINE_BASE, timeout: float = 30.0,
                 transport: Any = None) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        # 테스트가 가짜 transport 를 넣는다. 실제 API 를 부르지 않기 위해서다.
        self._transport = transport

    def _headers(self, api_key: str) -> dict[str, str]:
        if not api_key:
            raise NextEngineReadError("자격증명이 비어 있습니다.")
        return {
            "Api-Auth-Key": api_key,
            "Accept": "application/json",
        }

    def fetch_order_page(
        self, *, api_key: str, start: datetime, end: datetime, status: str,
        page: int = 1, size: int = DEFAULT_PAGE_SIZE, date_type: str = "pay_date",
    ) -> OrderPage:
        """한 페이지. 창 검증을 **호출 전에** 한다."""
        if (end - start).days > NEXT_ENGINE_MAX_WINDOW_DAYS:
            raise NextEngineWindowError(
                f"조회 기간은 최대 {NEXT_ENGINE_MAX_WINDOW_DAYS}일입니다.",
                detail={"max_days": NEXT_ENGINE_MAX_WINDOW_DAYS},
            )

        params = {
            "start_date": _fmt(start), "end_date": _fmt(end),
            "order_status_divi": status, "date_type": date_type,
            "page": page, "size": size,
        }
        client_kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        try:
            with httpx.Client(**client_kwargs) as http:
                res = http.get(f"{self._base}{ORDER_LIST_PATH}",
                               headers=self._headers(api_key), params=params)
        except httpx.HTTPError as exc:
            # 예외 문자열이 아니라 타입 이름만. httpx 메시지에는 URL 이 들어가고
            # 쿼리에는 조회 조건이 붙는다.
            raise NextEngineReadError(
                "주문 조회에 실패했습니다.", detail={"cause": type(exc).__name__},
            ) from exc

        out = OrderPage(http_status=res.status_code)
        try:
            body = res.json()
        except Exception:  # noqa: BLE001
            body = None
        if not isinstance(body, dict):
            raise NextEngineSchemaError(
                "주문 조회 응답을 해석할 수 없습니다.",
                detail={"http_status": res.status_code},
            )

        out.api_code = body.get("code")
        pid = body.get("process_id")
        out.process_id = str(pid) if pid else None

        if res.status_code != 200:
            # 벤더 메시지를 그대로 싣지 않는다. 상태와 추적 id 만 넘긴다.
            raise NextEngineReadError(
                "주문 조회가 거부되었습니다.",
                detail={"http_status": res.status_code, "api_code": out.api_code,
                        "process_id": out.process_id},
            )

        rows = body.get("data")
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise NextEngineSchemaError(
                "주문 목록 형식이 예상과 다릅니다.",
                detail={"process_id": out.process_id},
            )
        out.lines = [normalize_order_line(r) for r in rows if isinstance(r, dict)]
        return out

    def fetch_orders(
        self, *, api_key: str, start: datetime, end: datetime,
        statuses: tuple[str, ...] = ALL_ORDER_STATUSES,
        size: int = DEFAULT_PAGE_SIZE, date_type: str = "pay_date",
        max_pages: int = MAX_PAGES, pause_seconds: float = 0.0,
    ) -> list[ExternalOrderLine]:
        """창 하나를 상태별로 끝까지 읽는다.

        종료 조건
        --------
        총건수가 없으므로 **받은 행 수가 요청한 size 보다 적으면 마지막**이다.
        빈 페이지도 마지막이다. 실제 응답으로 확인했다(3건짜리 창에 size=2 →
        2건, 1건, 0건).

        `order_status_divi` 가 필수라 상태별로 나눠 부른다. 같은 주문이 두 상태에
        동시에 있을 수는 없지만, 상태가 도중에 바뀌면 두 번 받을 수 있다 —
        그래서 호출부가 `identity()` 로 접는다.
        """
        collected: dict[tuple[str, str], ExternalOrderLine] = {}
        for status in statuses:
            previous_page: frozenset[tuple[str, str]] | None = None
            for page in range(1, max_pages + 1):
                got = self.fetch_order_page(
                    api_key=api_key, start=start, end=end, status=status,
                    page=page, size=size, date_type=date_type,
                )
                # 벤더가 `page` 를 무시하고 같은 쪽을 계속 돌려주는 경우가 있다.
                # 그러면 종료조건(`count < size`)이 영원히 참이 되지 않는다 —
                # 상한에 걸릴 때까지 같은 요청을 100번 반복하고, 그 사이 결과는
                # 매번 같아서 아무 일도 일어나지 않는다.
                #
                # 비교는 **식별자 집합**으로만 한다. 응답 본문을 통째로 비교하면
                # 그 값이 로그·예외에 실려 나갈 위험이 생긴다(주문에는 개인정보가
                # 들어 있다).
                current_page = frozenset(line.identity() for line in got.lines)
                if current_page and current_page == previous_page:
                    raise NextEnginePaginationError(
                        "주문 페이지가 넘어가지 않습니다.",
                        detail={"page": page, "status": status, "size": size},
                    )
                previous_page = current_page

                for line in got.lines:
                    # 마지막에 본 것을 남긴다 — 상태가 진행된 쪽이 최신이다.
                    collected[line.identity()] = line
                if len(got.lines) < size:
                    break
                if pause_seconds:
                    time.sleep(pause_seconds)
            else:
                # for-else: break 없이 끝났다 = 페이지 상한에 걸렸다.
                raise NextEngineReadError(
                    "주문 페이지 상한에 도달했습니다. 조회 범위를 좁혀 주세요.",
                    detail={"max_pages": max_pages, "status": status},
                )
        return list(collected.values())
