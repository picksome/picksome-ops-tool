"""integration_account 발급/검증 — 봇/API 연동 계정 (M009, docs/27 D-289).

API Key 는 발급 시 1회만 평문으로 반환하고 DB 에는 해시만 저장한다
(core.security.hash_password 재사용 — "평문 비밀번호 절대 저장 금지" 원칙을
API Key 에도 그대로 적용, CLAUDE.md Security Rules).
"""
from __future__ import annotations

import secrets
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.core.ids import new_id
from app.core.security import hash_password as _hash_secret
from app.core.security import verify_password as _verify_secret
from app.services.sale_channel import is_valid_channel_code


def create_bot_account(
    db: Session,
    *,
    org_id: UUID,
    actor: UUID,
    provider: str,
    account_label: str,
    sale_channel_code: str | None = None,
) -> tuple[UUID, str]:
    """BOT/API_KEY 연동 계정 발급. 반환: (account_id, raw_api_key).

    raw_api_key 는 이 호출의 응답으로만 노출된다 — 잃어버리면 재발급(회전) 필요,
    DB 에서 복구 불가(해시만 저장).
    """
    sale_channel_id: UUID | None = None
    if sale_channel_code:
        if not is_valid_channel_code(db, org_id=org_id, code=sale_channel_code):
            raise ValidationError(f"등록되지 않은 판매채널입니다: {sale_channel_code}")
        row = db.execute(
            text(
                """
                SELECT id FROM sale_channel
                 WHERE (org_id = :org OR org_id IS NULL) AND code = :code AND deleted_at IS NULL
                 LIMIT 1
                """
            ),
            {"org": str(org_id), "code": sale_channel_code},
        ).first()
        sale_channel_id = UUID(str(row.id)) if row else None

    raw_key = secrets.token_urlsafe(32)
    key_hash = _hash_secret(raw_key)
    new_aid = new_id()
    db.execute(
        text(
            """
            INSERT INTO integration_account (
              id, org_id, provider, account_label, sale_channel_id, auth_kind,
              api_key_hash, status, created_by, updated_by
            ) VALUES (
              :id, :org, :provider, :label, :scid, 'API_KEY', :hash, 'ACTIVE', :actor, :actor
            )
            """
        ),
        {
            "id": str(new_aid),
            "org": str(org_id),
            "provider": provider,
            "label": account_label,
            "scid": str(sale_channel_id) if sale_channel_id else None,
            "hash": key_hash,
            "actor": str(actor),
        },
    )
    return new_aid, raw_key


def list_bot_accounts(db: Session, *, org_id: UUID) -> list[dict]:
    """org 의 연동 계정 목록. api_key_hash 는 절대 포함하지 않는다(Security)."""
    rows = db.execute(
        text(
            """
            SELECT ia.id, ia.provider, ia.account_label, ia.auth_kind, ia.status,
                   ia.last_synced_at, sc.code AS sale_channel_code
              FROM integration_account ia
              LEFT JOIN sale_channel sc ON sc.id = ia.sale_channel_id
             WHERE ia.org_id = :org AND ia.deleted_at IS NULL
             ORDER BY ia.created_at DESC
            """
        ),
        {"org": str(org_id)},
    ).all()
    return [dict(r._mapping) for r in rows]


def list_valid_channel_codes(db: Session, *, org_id: UUID) -> list[dict]:
    """봇 ingest 클라이언트가 유효 마켓코드를 미리 알 수 있게(X-Integration-Key 인증 경로 전용)."""
    rows = db.execute(
        text(
            """
            SELECT code, display_name FROM sale_channel
             WHERE (org_id = :org OR org_id IS NULL) AND is_active = TRUE AND deleted_at IS NULL
             ORDER BY is_system_default DESC, code
            """
        ),
        {"org": str(org_id)},
    ).all()
    return [dict(r._mapping) for r in rows]


def resolve_account(
    db: Session, *, org_id: UUID, account_id: UUID, marketplace: str | None = None
) -> UUID:
    """이 조직이 쓸 수 있는 계정이면 그대로 반환, 아니면 ValidationError.

    `resolve_sale_channel` 과 같은 자리다 — 값을 저장하기 전에 서비스 진입점에서
    한 번 검증한다.

    무엇을 막나
    ----------
    **① 조직 불일치.** 단순 UUID FK 는 이걸 막지 못한다. 남의 조직 계정에
    주문을 붙이면 그 조직 매출이 오염되고, 조직 격리가 뚫린다.

    **② 판매처 불일치.** `marketplace` 를 넘기면 계정이 실제로 그 판매처의
    것인지 확인한다. 쿠팡 주문이 네이버 계정에 붙는 것을 막는다 —
    그 상태로 쌓이면 정산 대사와 스토어별 손익이 통째로 틀어지고,
    나중에는 어느 쪽이 맞는지 알 방법이 없다.

    계정의 판매처는 **`sale_channel_id` 로만** 판단한다. `provider` 로 대신하지
    않는다 — `provider` 는 판매처가 아니라 **도구/공급자 이름**이다. 실제로
    `BOT_GENERIC` 같은 값이 쓰이고 있어서, 그걸 채널 코드로 취급하면
    정상적인 봇 주문이 전부 거절된다.

    `sale_channel_id` 가 NULL 인 계정은 **판매처를 선언한 적이 없다.** 비교할
    대상이 없으므로 통과시킨다 — 없는 근거로 거절하는 것은 검증이 아니라 추측이다.
    (그런 계정 자체가 데이터 품질 문제이며, 운영 이슈 스캐너가 드러낼 대상이다.)

    무엇을 막지 않나
    --------------
    **비활성 계정은 통과시킨다.** 계정을 잠시 멈춰 둔 동안에도 그 계정으로 들어온
    주문은 기록돼야 하고, 지난달 주문을 뒤늦게 수정할 때 계정이 비활성이라고
    거절하면 과거 데이터를 고칠 수 없게 된다. 활성 여부가 필요한 것은
    **키로 인증하는 경로**(`verify_account_key`)이지 귀속 기록이 아니다.

    삭제된 계정은 거절한다 — 지워진 계정에 새 주문을 붙이면 그 주문의 출처를
    화면에서 설명할 수 없다. 이미 붙어 있던 과거 주문은 FK 가 지켜 준다
    (`ON DELETE NO ACTION` + soft delete).

    다른 조직의 계정은 존재 여부조차 알려주지 않는다 — 같은 메시지로 거절한다.
    """
    row = db.execute(
        text(
            """
            SELECT ia.id, sc.code AS declared_channel
              FROM integration_account ia
              LEFT JOIN sale_channel sc ON sc.id = ia.sale_channel_id
             WHERE ia.id = :id AND ia.org_id = :org AND ia.deleted_at IS NULL
            """
        ),
        {"id": str(account_id), "org": str(org_id)},
    ).first()
    if row is None:
        raise ValidationError(f"등록되지 않은 연동 계정입니다: {account_id}")

    if marketplace is not None:
        declared = (row.declared_channel or "").strip().upper()
        if declared and declared != marketplace.strip().upper():
            raise ValidationError(
                f"판매처와 연동 계정이 맞지 않습니다: 주문은 {marketplace}, "
                f"계정은 {declared} 입니다.",
                detail={"marketplace": marketplace, "account_channel": declared},
            )
    return UUID(str(row.id))


def verify_account_key(db: Session, *, account_id: UUID, raw_key: str) -> UUID:
    """유효하면 org_id 반환, 아니면 ValidationError. 라우터에서 401 로 매핑하지 않고
    도메인 예외 그대로 노출 — 봇 클라이언트가 재시도 로직에 쓸 수 있게 일관된 에러 포맷 유지."""
    row = db.execute(
        text(
            """
            SELECT org_id, api_key_hash, status FROM integration_account
             WHERE id = :id AND deleted_at IS NULL
            """
        ),
        {"id": str(account_id)},
    ).first()
    if row is None or row.api_key_hash is None or row.status != "ACTIVE":
        raise ValidationError("유효하지 않은 연동 계정입니다.")
    if not _verify_secret(raw_key, row.api_key_hash):
        raise ValidationError("API Key 가 일치하지 않습니다.")
    return UUID(str(row.org_id))
