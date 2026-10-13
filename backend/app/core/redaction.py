"""비밀 가리기 (Phase A).

왜 필요한가 — 실제로 새고 있었다
---------------------------
`validation_error_handler` 가 `RequestValidationError.errors()` 를 그대로
응답에 넣고 있었다. Pydantic v2 의 오류 항목에는 **제출한 값이 `input` 으로
들어 있다.**

```python
M(api_secret="SECRET_TEST_VALUE_123")   # min_length 위반
# → errors()[0]["input"] == "SECRET_TEST_VALUE_123"
```

즉 자격증명 입력이 검증에 걸리는 순간 그 값이 422 응답 본문으로 돌아왔다.
브라우저 콘솔·프록시 로그·Playwright trace 어디에나 남는다.

무엇을 가리나
-----------
**키 이름으로** 판단한다. 값의 생김새로 추측하면(예: 긴 랜덤 문자열) 상품
설명이나 메모 같은 정상 업무 데이터를 가리게 된다 — 그러면 사용자는 왜 자기
데이터가 `***` 로 보이는지 알 수 없다.

중첩 구조도 따라 들어간다. 자격증명은 보통 `{"credential": {"api_key": ...}}`
처럼 한 겹 안에 있다.
"""

from __future__ import annotations

from typing import Any

#: 이 이름을 **포함**하면 가린다. 부분 일치인 이유는 `client_secret`,
#: `refresh_token`, `x_api_key` 처럼 접두·접미가 붙기 때문이다.
SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "accesskey",
    "private_key",
    "credential",
    "authorization",
    "auth_header",
    "webhook_url",
    "session_id",
)

REDACTED = "***"

#: 이 이름들은 비밀처럼 보이지만 **가리지 않는다.**
#: 가려 버리면 "자격증명이 설정됐는가" 를 화면이 표시할 수 없다.
SAFE_KEYS = frozenset({
    "credential_configured",
    "credential_present",
    "credential_detected",
    "credential_skipped",
    "credential_updated_at",
    "credential_updated_by",
    "credentials_vault_entry_id",  # 값이 아니라 보관 위치 참조 id 다
    "has_credential",
})


def is_secret_key(key: str) -> bool:
    lowered = str(key).lower()
    if lowered in SAFE_KEYS:
        return False
    return any(part in lowered for part in SECRET_KEY_PARTS)


def redact(value: Any, *, _depth: int = 0) -> Any:
    """비밀 키의 값을 `***` 로 바꾼 사본.

    원본을 바꾸지 않는다 — 로깅하려고 호출했다가 업무 데이터가 훼손되면 안 된다.

    깊이 제한을 둔다. 순환 참조나 아주 깊은 구조에서 무한히 내려가면 로깅이
    요청을 멈춘다.
    """
    if _depth > 8:
        return value

    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            out[k] = REDACTED if is_secret_key(k) else redact(v, _depth=_depth + 1)
        return out

    if isinstance(value, (list, tuple)):
        redacted = [redact(v, _depth=_depth + 1) for v in value]
        return type(value)(redacted) if isinstance(value, tuple) else redacted

    return value


def redact_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pydantic 오류 목록에서 **제출값을 통째로 뺀다.**

    처음에는 비밀 키(`api_secret` 등)의 `input` 만 지웠다. 그런데 사용자가
    비밀을 엉뚱한 칸에 붙여넣으면 그 칸은 비밀 키가 아니므로 그대로 새어 나온다
    (실제로 `vault_entry_id` 에 비밀을 넣은 테스트가 이걸 잡았다).

    키 이름으로 판단하는 방식은 **제출값에는 통하지 않는다.** 그래서 아예 뺀다.

    잃는 것이 거의 없다
    -----------------
    `loc`(어느 필드)과 `msg`(무엇이 잘못됐나)는 남는다. 사용자는 자기가 무엇을
    입력했는지 이미 안다 — 값을 되돌려줄 필요가 없다.
    """
    out = []
    for err in errors:
        item = dict(err)
        item.pop("input", None)
        # `ctx` 에도 값이 섞여 오는 경우가 있다(문자열 파싱 오류 등).
        if any(isinstance(p, str) and is_secret_key(p) for p in item.get("loc") or ()):
            item.pop("ctx", None)
        elif "ctx" in item:
            item["ctx"] = redact(item["ctx"])
        out.append(item)
    return out
