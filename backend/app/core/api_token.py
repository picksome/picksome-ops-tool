"""API 토큰 — 생성 · 해시 · scope (M044).

무엇을 지키나
-----------
1. 원문은 **발급 응답 한 번**만 존재한다. DB 에도 로그에도 없다
2. 접두사는 사람이 알아보는 용도다. **인증에 쓰지 않는다**
3. 비교는 상수 시간으로 한다
4. scope 는 토큰에 붙는다. 계정이 아니라 — 같은 에이전트라도 읽기용과 제안용을
   나눌 수 있어야 한다

해시가 SHA-256 인 근거
-------------------
비밀번호에는 argon2 를 쓴다(`core/security.py`). 여기는 다르다.

```
사람 비밀번호   30~40비트   사전·규칙 공격이 통한다 → 추측 1회 비용을 올린다
기계 토큰       256비트     사전이 없다             → 2^256 앞에서 속도는 무의미
```

토큰은 우리가 만든 32바이트 난수다. 사용자가 문자열을 정할 수 있게 하면 이
근거가 무너지므로 **그런 API 를 만들지 않았다.**

argon2 를 쓰면 모든 API·MCP 호출마다 100~300ms 가 붙는다. 얻는 것 없이 치르는
비용이다(M044 docstring, docs/61).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

#: 사람이 보는 접두사. 어떤 종류의 토큰인지 알 수 있을 정도만.
TOKEN_PREFIX = "sos"

#: 32바이트 = 256비트. 이 값이 줄면 위 해시 근거가 무너진다.
TOKEN_ENTROPY_BYTES = 32

#: 표시용 접두사 길이. 이걸로 인증하지 않으므로 짧아도 된다.
PREFIX_DISPLAY_LEN = 12


#: 발급 가능한 권한.
#:
#: 읽기와 제안만 있다. **실행 scope 를 지금 만들지 않는다** — 실행이 필요한
#: 자동화는 `automation_policy` 가 한도와 함께 정하고, 그 경로로만 간다.
#: 토큰에 실행 권한을 주면 정책 밖에서 실행되는 길이 생긴다.
READ_SCOPES = (
    "products:read",
    "sourcing:read",
    "cost:read",
    "sales:read",
    "orders:read",
    "settlements:read",
    "expenses:read",
    "cashflow:read",
    "issues:read",
    "reports:read",
    # 재고는 원가와 다른 축이다. 수량을 보는 것과 매입가를 보는 것은 별개라
    # scope 를 나눈다 — 물류 담당 에이전트에게 원가까지 줄 이유가 없다.
    "inventory:read",
    "purchasing:read",
)

#: 제안 — 사람의 승인을 거치는 것만.
PROPOSE_SCOPES = (
    "approvals:create",
    # 발주 **초안**까지만. 초안은 아무 데도 안 나가고 사람이 확정해야 한다.
    # `purchasing:execute` 는 만들지 않는다 — FORBIDDEN 참고.
    "purchasing:propose",
)

ALL_SCOPES = READ_SCOPES + PROPOSE_SCOPES

#: 발급 자체를 막는다. 만들어 두고 "기본 비활성" 으로 두면 언젠가 켜진다.
#:
#: 돈이 나가거나(지급·이체), 확정된 과거를 되돌리거나(월 재오픈), 우리 인증을
#: 바꾸는(자격증명) 일은 사람이 화면에서 한다. 자동화가 필요해지면
#: `automation_policy` 에 한도와 함께 정의하고 거기서 실행한다.
FORBIDDEN_SCOPES = (
    # 발주 확정은 공급처에 돈을 쓰겠다는 약속이고, 그 판단 근거인 판매속도는
    # 추정치다. 추정치로 돈을 쓰는 경로를 토큰에 만들지 않는다.
    "purchasing:execute",
    "payouts:execute",
    "month:reopen",
    "credentials:manage",
    "bank:transfer",
)

#: `cost:read` 가 없으면 가려야 하는 것. 원가를 가리면서 마진을 열어 두면
#: 역산이 되므로 함께 묶는다(`core/sensitive.py` 와 같은 판단).
COST_DERIVED_SCOPE = "cost:read"


class InvalidScopeError(ValueError):
    """알 수 없거나 발급이 금지된 scope."""


def validate_scopes(scopes: list[str]) -> list[str]:
    """중복을 없애고 순서를 고정한다. 모르는 값은 거부한다.

    조용히 무시하면 사용자는 준 적 없는 권한이 있다고 믿거나, 준 권한이
    없다고 오해한다.
    """
    if not scopes:
        raise InvalidScopeError("권한을 하나 이상 지정해야 합니다.")

    cleaned: list[str] = []
    for raw in scopes:
        scope = str(raw).strip()
        if scope in FORBIDDEN_SCOPES:
            raise InvalidScopeError(
                f"이 권한은 토큰으로 발급할 수 없습니다: {scope}. "
                "돈이 나가거나 확정된 기록을 되돌리는 일은 사람이 화면에서 합니다.",
            )
        if scope not in ALL_SCOPES:
            raise InvalidScopeError(f"알 수 없는 권한입니다: {scope}")
        if scope not in cleaned:
            cleaned.append(scope)
    return sorted(cleaned)


def generate_token() -> tuple[str, str, str]:
    """새 토큰 → `(원문, 접두사, 해시)`.

    원문은 호출자가 사용자에게 **한 번** 보여주고 버린다.
    """
    body = secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)
    token = f"{TOKEN_PREFIX}_{body}"
    return token, token[:PREFIX_DISPLAY_LEN], hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str, stored_hash: str) -> bool:
    """상수 시간 비교.

    `==` 로 비교하면 앞에서부터 몇 글자가 맞았는지가 시간으로 새어 나간다.
    실제로 뚫기는 어렵지만, 막는 비용이 한 줄이라 막지 않을 이유가 없다.
    """
    return hmac.compare_digest(hash_token(token), stored_hash)


def looks_like_token(value: str) -> bool:
    """Bearer 값이 우리 토큰 모양인가. JWT 와 구분하는 데 쓴다.

    모양만 본다 — 이걸로 인증하지 않는다.
    """
    return value.startswith(f"{TOKEN_PREFIX}_")


def masked(prefix: str) -> str:
    """화면 표시용. 원문을 복원할 수 없다."""
    return f"{prefix}…"
