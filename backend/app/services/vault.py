"""password_vault_entry CRUD 서비스.

CLAUDE.md Security Rules:
- 평문 비밀번호 절대 저장 금지.
- DB CHECK 제약 (ck_pve_memo_no_pw1/2) 외에도 service 단에서 1차 차단.
- 위반 시 MaskedSecretDetectedError → MASKED_SECRET_DETECTED (422).

권한 (라우터):
- ADMIN: 전체 CRUD
- OPERATOR: 읽기 전용 (memo 제외 / 또는 제한 필드)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import MaskedSecretDetectedError, NotFoundError
from app.core.ids import new_id
from app.domain.vault import VaultEntryCreate, VaultEntryOut, VaultEntryUpdate

# 메모 본문에서 평문 비밀번호로 의심되는 패턴 차단.
# 보수적으로 매칭 — 오탐 (예: "비밀번호 관리자: 파트너A") 시에는 패턴 조정 필요.
_PW_PATTERNS = [
    re.compile(r"비밀번호[ :=]*[A-Za-z0-9!@#$]+"),
    re.compile(r"(?i)(pw|pass|pwd)[ :=]+[A-Za-z0-9!@#$]+"),
    re.compile(r"(?i)password[ :=]+[A-Za-z0-9!@#$]+"),
]


def _check_plaintext(memo: str | None) -> None:
    """메모에 평문 비밀번호 패턴이 있으면 즉시 거부 (Security Rules)."""
    if not memo:
        return
    for pat in _PW_PATTERNS:
        if pat.search(memo):
            raise MaskedSecretDetectedError(
                "메모에 평문 비밀번호로 의심되는 패턴이 포함되어 있습니다."
            )


def _now() -> datetime:
    return datetime.now(tz=UTC)


_SELECT = """
  SELECT id, service_name, vault_provider, vault_item_name,
         account_masked, memo, created_at, updated_at
    FROM password_vault_entry
"""


def list_vault_entries(
    db: Session, *, org_id: UUID, limit: int = 50, offset: int = 0
) -> tuple[list[VaultEntryOut], int]:
    count_row = db.execute(
        text("""
          SELECT COUNT(*) AS c FROM password_vault_entry
           WHERE org_id = :o AND deleted_at IS NULL
        """),
        {"o": str(org_id)},
    ).first()
    total = int(count_row.c) if count_row else 0
    rows = db.execute(
        text(f"""
          {_SELECT}
           WHERE org_id = :o AND deleted_at IS NULL
           ORDER BY service_name
           LIMIT :limit OFFSET :offset
        """),
        {"o": str(org_id), "limit": limit, "offset": offset},
    ).all()
    return [VaultEntryOut(**r._mapping) for r in rows], total


def get_vault_entry(db: Session, *, org_id: UUID, entry_id: UUID) -> VaultEntryOut:
    row = db.execute(
        text(f"{_SELECT} WHERE id = :i AND org_id = :o AND deleted_at IS NULL"),
        {"i": str(entry_id), "o": str(org_id)},
    ).first()
    if row is None:
        raise NotFoundError("Vault 항목을 찾을 수 없습니다.")
    return VaultEntryOut(**row._mapping)


def create_vault_entry(
    db: Session, *, org_id: UUID, actor: UUID, body: VaultEntryCreate
) -> VaultEntryOut:
    _check_plaintext(body.memo)
    new_entry_id = new_id()
    db.execute(
        text("""
          INSERT INTO password_vault_entry (
            id, org_id, service_name, vault_provider, vault_item_name,
            account_masked, memo, created_by, updated_by
          ) VALUES (
            :id, :o, :sn, :vp, :vi, :am, :memo, :actor, :actor
          )
        """),
        {
            "id": str(new_entry_id),
            "o": str(org_id),
            "sn": body.service_name,
            "vp": body.vault_provider,
            "vi": body.vault_item_name,
            "am": body.account_masked,
            "memo": body.memo,
            "actor": str(actor),
        },
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="password_vault_entry",
        domain_row_id=new_entry_id,
        action="CREATE",
        actor_user_id=actor,
        after={"service_name": body.service_name, "vault_provider": body.vault_provider},
    )
    db.commit()
    return get_vault_entry(db, org_id=org_id, entry_id=new_entry_id)


def update_vault_entry(
    db: Session, *, org_id: UUID, actor: UUID, entry_id: UUID, body: VaultEntryUpdate
) -> VaultEntryOut:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return get_vault_entry(db, org_id=org_id, entry_id=entry_id)
    if "memo" in fields:
        _check_plaintext(fields["memo"])
    before = get_vault_entry(db, org_id=org_id, entry_id=entry_id)
    sets = ", ".join([f"{k} = :{k}" for k in fields])
    db.execute(
        text(f"""
          UPDATE password_vault_entry
             SET {sets}, updated_at = :now, updated_by = :actor
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        {**fields, "i": str(entry_id), "o": str(org_id), "now": _now(), "actor": str(actor)},
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="password_vault_entry",
        domain_row_id=entry_id,
        action="UPDATE",
        actor_user_id=actor,
        before=before.model_dump(),
        after=fields,
    )
    db.commit()
    return get_vault_entry(db, org_id=org_id, entry_id=entry_id)


def delete_vault_entry(db: Session, *, org_id: UUID, actor: UUID, entry_id: UUID) -> None:
    """soft delete. integration_account 가 참조 중이면 ConflictError 로 차단."""
    # 외래키 참조 (integration_account 등) 가 살아있으면 삭제 불가 — Post-MVP 자동 연동 안정성 보호.
    used = db.execute(
        text("""
          SELECT 1 FROM integration_account
           WHERE credentials_vault_entry_id = :i AND deleted_at IS NULL
           LIMIT 1
        """),
        {"i": str(entry_id)},
    ).first()
    if used:
        from app.core.exceptions import ConflictError

        raise ConflictError("이 Vault 항목은 사용 중인 연동 계정에 연결되어 있습니다.")

    db.execute(
        text("""
          UPDATE password_vault_entry
             SET deleted_at = :now, deleted_by = :actor
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        {"now": _now(), "actor": str(actor), "i": str(entry_id), "o": str(org_id)},
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="password_vault_entry",
        domain_row_id=entry_id,
        action="DELETE",
        actor_user_id=actor,
    )
    db.commit()
