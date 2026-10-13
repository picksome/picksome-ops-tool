"""NEXT ENGINE 클레임 조회 — **GET 만 있다.**

주문 클라이언트의 규칙을 그대로 따르되, **주문의 값을 물려받지 않는다.**
클레임 조회의 제약은 따로 실측했다(docs/80).

실측으로 확인한 것
---------------
```
조회 기간         한 요청 최대 10일   (11일 → 400 "최대 10일까지 조회할수 있습니다")
claim_divi       필수               "전체 클레임" 을 한 번에 받는 방법이 없다
코드 20          벤더가 거부         문서에는 있는데 400 이다
date_type        무시된다            없는 값을 줘도 200 이고 결과가 같다
조회 날짜        **주문 날짜**        클레임 날짜가 아니다
페이지네이션      page 정상 동작       count < size 가 마지막 (size=2 로 실측)
```

**조회 날짜가 주문 날짜라는 것이 가장 중요하다.** 오늘 발생한 클레임이라도
원 주문이 6월이면 6월 창으로만 찾을 수 있다. 최근 며칠만 겹쳐 읽으면 오래된
주문의 클레임을 영원히 못 본다 — 동기화 전략이 주문과 달라지는 이유다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.connectors.next_engine.claim_normalize import (
    QUERYABLE_DIVI,
    ExternalClaim,
    normalize_claim,
)
from app.connectors.next_engine.client import (
    NEXT_ENGINE_BASE,
    NextEnginePaginationError,
    NextEngineReadError,
    NextEngineSchemaError,
    NextEngineWindowError,
    _fmt,
)

#: 클레임 조회. **이 파일의 유일한 경로다.**
CLAIM_LIST_PATH = "/api/v1/ne/claims"

#: 실측한 제약. 주문과 우연히 같지만 **따로 잰 값**이다.
CLAIM_MAX_WINDOW_DAYS = 10

DEFAULT_PAGE_SIZE = 30
MAX_PAGES = 100


@dataclass(slots=True)
class ClaimPage:
    claims: list[ExternalClaim] = field(default_factory=list)
    http_status: int = 0
    api_code: Any = None
    process_id: str | None = None


def split_claim_windows(
    start: datetime, end: datetime, *, max_days: int = CLAIM_MAX_WINDOW_DAYS,
) -> list[tuple[datetime, datetime]]:
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


class NextEngineClaimClient:
    """클레임 **읽기** 전용. 자격증명을 보관하지 않는다."""

    max_window_days = CLAIM_MAX_WINDOW_DAYS

    def __init__(self, *, base_url: str = NEXT_ENGINE_BASE, timeout: float = 30.0,
                 transport: Any = None) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    def _headers(self, api_key: str) -> dict[str, str]:
        if not api_key:
            raise NextEngineReadError("자격증명이 비어 있습니다.")
        return {"Api-Auth-Key": api_key, "Accept": "application/json"}

    def fetch_claim_page(
        self, *, api_key: str, start: datetime, end: datetime, claim_divi: str,
        page: int = 1, size: int = DEFAULT_PAGE_SIZE,
    ) -> ClaimPage:
        """한 페이지. 창 검증을 **호출 전에** 한다."""
        if (end - start).days > CLAIM_MAX_WINDOW_DAYS:
            raise NextEngineWindowError(
                f"클레임 조회 기간은 최대 {CLAIM_MAX_WINDOW_DAYS}일입니다.",
                detail={"max_days": CLAIM_MAX_WINDOW_DAYS},
            )

        params = {
            "start_date": _fmt(start), "end_date": _fmt(end),
            "claim_divi": claim_divi, "page": page, "size": size,
        }
        kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        try:
            with httpx.Client(**kwargs) as http:
                res = http.get(f"{self._base}{CLAIM_LIST_PATH}",
                               headers=self._headers(api_key), params=params)
        except httpx.HTTPError as exc:
            raise NextEngineReadError(
                "클레임 조회에 실패했습니다.", detail={"cause": type(exc).__name__},
            ) from exc

        out = ClaimPage(http_status=res.status_code)
        try:
            body = res.json()
        except Exception:  # noqa: BLE001
            body = None
        if not isinstance(body, dict):
            raise NextEngineSchemaError(
                "클레임 조회 응답을 해석할 수 없습니다.",
                detail={"http_status": res.status_code},
            )

        out.api_code = body.get("code")
        pid = body.get("process_id")
        out.process_id = str(pid) if pid else None

        if res.status_code != 200:
            # 벤더 메시지를 그대로 싣지 않는다. 클레임 응답에는 개인정보가 있고,
            # 오류 본문에 그 값이 섞여 오는 일이 있다.
            raise NextEngineReadError(
                "클레임 조회가 거부되었습니다.",
                detail={"http_status": res.status_code, "api_code": out.api_code,
                        "process_id": out.process_id, "claim_divi": claim_divi},
            )

        rows = body.get("data")
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise NextEngineSchemaError(
                "클레임 목록 형식이 예상과 다릅니다.",
                detail={"process_id": out.process_id},
            )
        out.claims = [normalize_claim(r, claim_divi=claim_divi)
                      for r in rows if isinstance(r, dict)]
        return out

    def fetch_claims(
        self, *, api_key: str, start: datetime, end: datetime,
        divis: tuple[str, ...] = QUERYABLE_DIVI,
        size: int = DEFAULT_PAGE_SIZE, max_pages: int = MAX_PAGES,
        pause_seconds: float = 0.0,
    ) -> list[ExternalClaim]:
        """창 하나를 상태별로 끝까지 읽는다.

        `claim_divi` 가 필수라 상태마다 나눠 부른다. 같은 클레임이 상태를 옮기면
        두 코드에서 나올 수 있으므로 **마지막에 본 것을 남긴다** — 상태가
        진행된 쪽이 최신이다.
        """
        collected: dict[str, ExternalClaim] = {}
        for divi in divis:
            previous: frozenset[str] | None = None
            for page in range(1, max_pages + 1):
                got = self.fetch_claim_page(
                    api_key=api_key, start=start, end=end, claim_divi=divi,
                    page=page, size=size)

                # 페이지가 안 넘어가면 상한까지 같은 요청을 반복하게 된다.
                # 식별자 집합으로만 비교한다 — 본문 비교는 개인정보를 만진다.
                current = frozenset(
                    c.ne_claim_no for c in got.claims if c.ne_claim_no)
                if current and current == previous:
                    raise NextEnginePaginationError(
                        "클레임 페이지가 넘어가지 않습니다.",
                        detail={"page": page, "claim_divi": divi, "size": size},
                    )
                previous = current

                for claim in got.claims:
                    if claim.ne_claim_no:
                        collected[claim.ne_claim_no] = claim
                if len(got.claims) < size:
                    break
                if pause_seconds:
                    time.sleep(pause_seconds)
            else:
                raise NextEngineReadError(
                    "클레임 페이지 상한에 도달했습니다. 조회 범위를 좁혀 주세요.",
                    detail={"max_pages": max_pages, "claim_divi": divi},
                )
        return list(collected.values())
