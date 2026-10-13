"""표준 응답 / 에러 envelope.

docs/22_api_spec.md §1-3 응답 포맷:
- 성공: `{ "data": ..., "meta": {...} }`
- 실패: `{ "error": { "code": "...", "message": "...", "details": [...] } }`

모든 라우터는 응답을 `envelope(...)` 으로 감싸고, 예외는 raise 만 하면
아래 핸들러들이 일괄 변환한다 (라우터에 try/except 금지).
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import DomainError


def envelope(data: Any, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """성공 응답 wrapper. meta=None 이면 키 자체를 생략 (프론트 일관성)."""
    body: dict[str, Any] = {"data": data}
    if meta is not None:
        body["meta"] = meta
    return body


def error_envelope(code: str, message: str, details: Any = None) -> dict[str, Any]:
    """실패 응답 wrapper. details 는 검증 에러 리스트 등 자유 구조."""
    err: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return {"error": err}


async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    """비즈니스 예외 → JSON envelope. http_status 는 예외 클래스가 보유.

    `detail` 도 가린다 — 서비스가 "무엇이 잘못됐는지" 를 알리려고 입력값을
    담는 일이 있고, 그 입력이 자격증명일 수 있다.
    """
    from app.core.redaction import redact

    return JSONResponse(
        status_code=exc.http_status,
        content=error_envelope(exc.code, exc.message, redact(exc.detail) or None),
    )


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Pydantic 검증 실패 → 422 + 필드별 에러 리스트.

    ⚠️ `exc.errors()` 를 그대로 내보내지 않는다.

    Pydantic v2 의 오류 항목에는 **제출한 값이 `input` 으로 들어 있다.**
    자격증명 입력이 길이·형식 검증에 걸리면 그 비밀이 422 응답 본문으로
    돌아온다 — 브라우저 콘솔·프록시 로그·테스트 trace 어디에나 남는다.
    (실제로 그 상태였고, 이 핸들러가 유일한 유출 경로였다.)
    """
    from app.core.redaction import redact_validation_errors

    return JSONResponse(
        status_code=422,
        content=error_envelope(
            "VALIDATION_ERROR", "입력값 검증 실패",
            redact_validation_errors(exc.errors()),
        ),
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """FastAPI/Starlette HTTPException → envelope 으로 정규화."""
    # 401/403/404 만 친화적 코드로, 나머지는 일반 HTTP_ERROR.
    code_map = {401: "AUTH_REQUIRED", 403: "FORBIDDEN", 404: "NOT_FOUND"}
    code = code_map.get(exc.status_code, "HTTP_ERROR")
    return JSONResponse(
        status_code=exc.status_code,
        content=error_envelope(code, str(exc.detail or "")),
    )


async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """알 수 없는 예외 → 500. 메시지에 내부 스택 노출 금지 (정보 누출 방지)."""
    return JSONResponse(
        status_code=500,
        content=error_envelope("INTERNAL_ERROR", "내부 오류"),
    )
