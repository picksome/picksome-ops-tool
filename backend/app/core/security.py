"""인증·비밀번호·JWT 헬퍼.

CLAUDE.md Security Rules:
- 평문 비밀번호 절대 저장 금지 — argon2 해시만 저장.
- vault 는 실제 비밀번호가 아닌 **보관 위치**만 (예: "Bitwarden - 쿠팡 Wing").
- 민감정보 로그 출력 금지.

JWT:
- HS256, JWT_SECRET 은 env 에서 주입 (config.JWT_SECRET).
- access_token 만료: JWT_EXPIRE_MINUTES.
- refresh_token: 30일 고정 (MVP).

초대 토큰:
- secrets.token_urlsafe(32) — URL-safe 256bit 엔트로피.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from jose import JWTError, jwt

from app.config import get_settings

_settings = get_settings()
# argon2 기본 파라미터 (memory_cost=64MB, time_cost=3) — 운영 환경에서 100~300ms 소요.
_hasher = PasswordHasher()

# 마이그레이션 호환용 prefix. 과거에 평문이 들어왔는지 식별해 verify=False 처리.
_PLAIN_PREFIX = "plain:"


def hash_password(password: str) -> str:
    """평문 비밀번호 → argon2 해시.

    최소 길이 정책 = 8자 (D-280 정책 테이블화 이전 임시 하드코딩).
    """
    if not password or len(password) < 8:
        raise ValueError("비밀번호는 8자 이상이어야 합니다.")
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """해시 일치 여부. 평문 prefix 가 검출되면 무조건 False (안전 fallback)."""
    if password_hash.startswith(_PLAIN_PREFIX):
        return False
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        # 손상된 해시 포맷 등 — 인증 실패로 간주, 예외 노출하지 않음.
        return False


def needs_rehash(password_hash: str) -> bool:
    """argon2 파라미터 변경 후 재해시 필요 여부. 로그인 성공 시 호출 권장."""
    return _hasher.check_needs_rehash(password_hash)


def create_access_token(
    *, user_id: UUID, org_id: UUID, role: str, expires_minutes: int | None = None
) -> str:
    """JWT access token 발급. payload = {sub, org_id, role, iat, exp, type='access'}."""
    now = datetime.now(tz=UTC)
    exp = now + timedelta(minutes=expires_minutes or _settings.JWT_EXPIRE_MINUTES)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org_id": str(org_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "type": "access",
    }
    return jwt.encode(payload, _settings.JWT_SECRET, algorithm=_settings.JWT_ALGORITHM)


def create_refresh_token(*, user_id: UUID, org_id: UUID) -> str:
    """refresh token. role 미포함 (재발급 시 DB 에서 최신 role 조회)."""
    now = datetime.now(tz=UTC)
    exp = now + timedelta(days=30)
    payload = {
        "sub": str(user_id),
        "org_id": str(org_id),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "type": "refresh",
    }
    return jwt.encode(payload, _settings.JWT_SECRET, algorithm=_settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """JWT decode + 서명 검증. 실패 시 ValueError('INVALID_TOKEN: ...')."""
    try:
        return jwt.decode(token, _settings.JWT_SECRET, algorithms=[_settings.JWT_ALGORITHM])
    except JWTError as e:
        raise ValueError(f"INVALID_TOKEN: {e}") from e


def new_invitation_token() -> str:
    """사용자 초대 링크용 일회성 토큰 (URL-safe 256bit)."""
    return secrets.token_urlsafe(32)
