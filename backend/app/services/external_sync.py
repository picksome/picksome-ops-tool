"""외부 시스템 계정 · 매핑 · 동기화 (M038/M039).

이 모듈은 vendor 를 모른다
-----------------------
커넥터가 외부 API 를 DTO 로 바꾸고, 여기서 검증·매핑·적용을 한다.
셀러툴을 하나 더 붙여도 이 파일은 바뀌지 않는다.

세 가지 원칙
----------
1. **덮어쓰지 않는다.** 사용자가 손으로 고친 값은 다음 sync 가 되돌리지 않는다.
   되돌리면 사용자는 같은 수정을 매번 반복하게 되고, 결국 동기화를 꺼 버린다.
2. **같은 것을 두 번 만들지 않는다.** 외부 상품 하나는 내부 상품 하나에만 붙는다.
   매핑이 idempotency 의 핵심이다.
3. **원가는 자동 채택하지 않는다.** 셀러툴이 매입가를 줘도 그것은 관측된 사실일
   뿐이다. `product.cost_amount` 는 `COST_CHANGE` 승인으로만 바뀐다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db_errors import translate_integrity_error
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.services import connector as conn

# 외부가 값을 줄 수 있는 상품 필드. 여기 없는 필드는 sync 가 건드리지 않는다.
#
# **원가는 없다.** 셀러툴이 매입가를 줘도 `product.cost_amount` 로 가지 않는다 —
# 소싱 원가 version 후보가 될 뿐이다(`_apply_source_cost`).
SYNCABLE_PRODUCT_FIELDS = ("name_ko",)


# ──────────────────────────────────────────────────────────────────────────
# 외부 시스템 · 계정
# ──────────────────────────────────────────────────────────────────────────

def list_systems(db: Session) -> list[dict[str, Any]]:
    """카탈로그 + **커넥터 구현 여부**.

    카탈로그에 있다고 연동되는 게 아니다. 화면이 둘을 구분할 수 있어야
    "왜 동기화 버튼이 없지" 를 묻지 않는다.
    """
    rows = db.execute(text("""
        SELECT id, code, name, system_type, supports_oauth, supports_api_key,
               supports_webhook, documentation_url, is_active
          FROM external_system WHERE is_active ORDER BY name
    """)).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["connector_available"] = conn.has_connector(d["code"])
        d["capabilities"] = conn.capabilities_of(d["code"])
        out.append(d)
    return out


def _system(db: Session, system_id: UUID) -> dict[str, Any]:
    row = db.execute(text("""
        SELECT id, code, name FROM external_system WHERE id = :i AND is_active
    """), {"i": str(system_id)}).mappings().first()
    if row is None:
        raise NotFoundError("외부 시스템을 찾을 수 없습니다.")
    return dict(row)


def create_account(
    db: Session, *, org_id: UUID, actor_user_id: UUID, external_system_id: UUID,
    display_name: str, account_code: str, external_account_identifier: str | None = None,
) -> dict[str, Any]:
    """외부 계정 등록.

    **자격증명은 여기서 받지 않는다.** 등록과 인증정보 설정을 나눈다 —
    등록 폼에 비밀번호 칸을 두면 그 값이 요청 로그·브라우저 히스토리에 남는다.
    """
    system = _system(db, external_system_id)
    if not display_name.strip() or not account_code.strip():
        raise ValidationError("계정 이름과 코드가 필요합니다.")

    try:
        row = db.execute(text("""
            INSERT INTO external_account
                   (org_id, external_system_id, account_code, display_name,
                    external_account_identifier, created_by, updated_by)
            VALUES (:org, :sys, :code, :name, :ident, :actor, :actor)
            RETURNING id
        """), {"org": str(org_id), "sys": str(external_system_id),
               "code": account_code.strip(), "name": display_name.strip(),
               "ident": external_account_identifier,
               "actor": str(actor_user_id)}).mappings().one()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        domain = translate_integrity_error(exc, {
            "uq_external_account_code": lambda: ConflictError(
                f"이미 있는 계정 코드입니다: {account_code}",
            ),
        })
        if domain is not None:
            raise domain from exc
        raise

    aid = UUID(str(row["id"]))
    record_audit(
        db, org_id=org_id, domain_table="external_account", domain_row_id=aid,
        action="CREATE", actor_user_id=actor_user_id,
        after={"system": system["code"], "account_code": account_code,
               "display_name": display_name},
    )
    db.commit()
    return get_account(db, org_id=org_id, account_id=aid)


def get_account(db: Session, *, org_id: UUID, account_id: UUID) -> dict[str, Any]:
    row = db.execute(text("""
        SELECT a.id, a.account_code, a.display_name, a.external_account_identifier,
               a.status, a.last_connected_at, a.last_successful_sync_at,
               a.credentials_vault_entry_id, a.credential_secret_ref,
               a.credential_kind,
               a.credential_updated_at, a.credential_updated_by,
               a.last_test_status, a.last_test_at, a.last_test_message,
               a.created_at,
               s.code AS system_code, s.name AS system_name, s.system_type
          FROM external_account a
          JOIN external_system s ON s.id = a.external_system_id
         WHERE a.id = :i AND a.org_id = :org AND a.deleted_at IS NULL
    """), {"i": str(account_id), "org": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("외부 계정을 찾을 수 없습니다.")

    d = dict(row)
    d["id"] = str(d["id"])
    # **비밀도 참조도 나가지 않는다.** 두 가지 사실만 알려준다.
    #
    #   credential_configured   프로그램이 인증할 수 있는가 (무인 동기화 가능)
    #   vault_note_linked       사람이 찾아갈 쪽지가 있는가
    #
    # 둘을 하나로 합치지 않는다. 쪽지만 있는 상태에서 "설정됨" 이라고 말하면
    # 사용자는 새벽 배치가 왜 계속 실패하는지 알 수 없다.
    #
    # 참조 문자열은 응답에서 뺀다 — 참조를 알면 무엇을 노려야 하는지 알게 된다.
    d["credential_configured"] = d.pop("credential_secret_ref") is not None
    d["vault_note_linked"] = d.pop("credentials_vault_entry_id") is not None
    d["credential_updated_by"] = (
        str(d["credential_updated_by"]) if d["credential_updated_by"] else None
    )
    d["connector_available"] = conn.has_connector(d["system_code"])
    d["capabilities"] = conn.capabilities_of(d["system_code"])
    d["mapping_count"] = db.execute(text("""
        SELECT COUNT(*) FROM external_entity_mapping
         WHERE external_account_id = :a AND deleted_at IS NULL
    """), {"a": str(account_id)}).scalar()
    return d


def list_accounts(db: Session, *, org_id: UUID) -> list[dict[str, Any]]:
    ids = db.execute(text("""
        SELECT id FROM external_account
         WHERE org_id = :org AND deleted_at IS NULL ORDER BY created_at
    """), {"org": str(org_id)}).scalars().all()
    return [get_account(db, org_id=org_id, account_id=UUID(str(i))) for i in ids]


def set_account_status(
    db: Session, *, org_id: UUID, actor_user_id: UUID, account_id: UUID, status: str,
) -> dict[str, Any]:
    """비활성화는 삭제가 아니다 — 과거 매핑과 sync 기록이 남아야 한다."""
    if status not in ("DISCONNECTED", "CONNECTED", "ERROR", "DISABLED"):
        raise ValidationError(f"알 수 없는 상태입니다: {status}")
    before = get_account(db, org_id=org_id, account_id=account_id)

    db.execute(text("""
        UPDATE external_account SET status = :s, updated_at = now(), updated_by = :a
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"s": status, "i": str(account_id), "org": str(org_id),
           "a": str(actor_user_id)})
    record_audit(
        db, org_id=org_id, domain_table="external_account", domain_row_id=account_id,
        action="UPDATE", actor_user_id=actor_user_id,
        before={"status": before["status"]}, after={"status": status},
    )
    db.commit()
    return get_account(db, org_id=org_id, account_id=account_id)


def link_vault_note(
    db: Session, *, org_id: UUID, actor_user_id: UUID, account_id: UUID,
    vault_entry_id: UUID,
) -> dict[str, Any]:
    """사람이 찾아갈 **쪽지**를 연결한다 (`password_vault_entry`).

    이것으로 동기화가 되지는 않는다
    ---------------------------
    쪽지에는 값이 없다. "Bitwarden 의 그 항목" 이라고만 적혀 있고, 새벽 3시에
    도는 배치는 그걸 열 수 없다. 무인 동기화용 인증정보는
    `set_credential_secret` 이 따로 다룬다.

    그래도 쪽지를 남기는 이유: 담당자가 바뀔 때 "이 계정 비밀번호 어디 있죠" 의
    유일한 단서인 경우가 많다.
    """
    get_account(db, org_id=org_id, account_id=account_id)

    ok = db.execute(text("""
        SELECT 1 FROM password_vault_entry
         WHERE id = :v AND org_id = :org AND deleted_at IS NULL
    """), {"v": str(vault_entry_id), "org": str(org_id)}).first()
    if ok is None:
        # 조직 격리 — 남의 보관함을 가리킬 수 없다.
        raise NotFoundError("자격증명 보관 항목을 찾을 수 없습니다.")

    db.execute(text("""
        UPDATE external_account
           SET credentials_vault_entry_id = :v, updated_at = now(), updated_by = :a
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"v": str(vault_entry_id), "a": str(actor_user_id),
           "i": str(account_id), "org": str(org_id)})
    record_audit(
        db, org_id=org_id, domain_table="external_account", domain_row_id=account_id,
        action="UPDATE", actor_user_id=actor_user_id,
        after={"vault_note_linked": True}, reason="CREDENTIAL_VAULT_NOTE_LINKED",
    )
    db.commit()
    return get_account(db, org_id=org_id, account_id=account_id)


def set_credential_secret(
    db: Session, *, org_id: UUID, actor_user_id: UUID, account_id: UUID,
    credential_kind: str, payload: dict[str, Any],
) -> dict[str, Any]:
    """무인 동기화용 인증정보를 저장한다 (M042).

    평문은 이 함수 안에서만 산다
    -------------------------
    ```
    payload  →  SecretStore.put  →  암호문(DB) + 불투명 참조
                                    평문은 어디에도 남지 않는다
    ```

    감사에도 값이 없다. 무엇이 일어났는지(설정/교체)와 형태만 남는다.

    교체 중간에 기존 연결을 잃지 않는다
    -------------------------------
    새 비밀을 **먼저** 저장하고, 성공한 뒤에 계정 참조를 바꾼다. 순서를
    뒤집으면 저장이 실패했을 때 멀쩡하던 자격증명이 사라진다. 저장은 됐는데
    참조 갱신이 실패하면 고아 비밀이 남는데, 그건 지울 수 있다 —
    사라진 비밀은 복구할 수 없다.
    """
    from app.core.secret_store import get_secret_store, validate_payload

    before = get_account(db, org_id=org_id, account_id=account_id)

    try:
        validate_payload(credential_kind, payload)
    except ValueError as exc:
        # 메시지에 값이 들어가지 않는다 — validate_payload 가 칸 이름만 말한다.
        raise ValidationError(str(exc)) from exc

    store = get_secret_store()
    # 키가 없으면 CredentialStoreNotConfiguredError(503) 가 여기서 올라간다.
    # 평문으로 대신 저장하지 않는다.
    ref = store.put(
        org_id=org_id, owner_type="external_account", owner_id=account_id,
        credential_kind=credential_kind, payload=payload,
    )

    old_ref = db.execute(text("""
        SELECT credential_secret_ref FROM external_account
         WHERE id = :i AND org_id = :org
    """), {"i": str(account_id), "org": str(org_id)}).scalar()

    db.execute(text("""
        UPDATE external_account
           SET credential_secret_ref = :ref, credential_kind = :k,
               credential_updated_at = now(), credential_updated_by = :a,
               updated_at = now(), updated_by = :a
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"ref": ref, "k": credential_kind, "a": str(actor_user_id),
           "i": str(account_id), "org": str(org_id)})

    record_audit(
        db, org_id=org_id, domain_table="external_account", domain_row_id=account_id,
        action="UPDATE", actor_user_id=actor_user_id,
        before={"credential_configured": before["credential_configured"]},
        after={"credential_configured": True, "credential_kind": credential_kind},
        reason="CREDENTIAL_ROTATED" if before["credential_configured"]
               else "CREDENTIAL_CONFIGURED",
    )
    db.commit()

    if old_ref and old_ref != ref:
        # 참조가 바뀐 뒤에 옛 비밀을 버린다. 실패해도 새 자격증명은 이미
        # 유효하므로 여기서 예외를 올려 사용자에게 실패로 보이게 하지 않는다.
        try:
            store.delete(org_id=org_id, secret_ref=old_ref)
        except Exception:  # noqa: BLE001
            pass

    return get_account(db, org_id=org_id, account_id=account_id)


def remove_credential(
    db: Session, *, org_id: UUID, actor_user_id: UUID, account_id: UUID,
) -> dict[str, Any]:
    """인증정보를 지운다. 이후 동기화는 `CREDENTIAL_NOT_CONFIGURED` 로 거부된다.

    쪽지(`password_vault_entry`)는 건드리지 않는다 — 우리 것이 아니고,
    사람이 다시 찾아갈 단서로 남아야 한다.
    """
    from app.core.secret_store import get_secret_store

    get_account(db, org_id=org_id, account_id=account_id)
    ref = db.execute(text("""
        SELECT credential_secret_ref FROM external_account
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(account_id), "org": str(org_id)}).scalar()

    db.execute(text("""
        UPDATE external_account
           SET credential_secret_ref = NULL, credential_kind = NULL,
               credential_updated_at = now(), credential_updated_by = :a,
               status = CASE WHEN status = 'CONNECTED' THEN 'DISCONNECTED' ELSE status END
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"a": str(actor_user_id), "i": str(account_id), "org": str(org_id)})

    record_audit(
        db, org_id=org_id, domain_table="external_account", domain_row_id=account_id,
        action="UPDATE", actor_user_id=actor_user_id,
        after={"credential_configured": False}, reason="CREDENTIAL_REMOVED",
    )
    db.commit()

    if ref:
        # 참조를 먼저 끊고 나서 지운다. 반대 순서로 하면 지우기가 실패했을 때
        # 계정은 "설정됨" 인데 비밀은 없는 상태가 된다.
        get_secret_store().delete(org_id=org_id, secret_ref=ref)

    return get_account(db, org_id=org_id, account_id=account_id)


def test_connection(
    db: Session, *, org_id: UUID, actor_user_id: UUID, account_id: UUID,
) -> dict[str, Any]:
    """연결 시험.

    실패 사유를 하나로 뭉치지 않는다
    ---------------------------
    ```
    CONNECTOR_UNAVAILABLE       우리가 아직 구현 안 함
    CREDENTIAL_NOT_CONFIGURED   설정이 안 됨
    AUTHENTICATION_FAILED       값이 틀림
    CONNECTION_FAILED           네트워크·서버
    RATE_LIMITED                잠시 후 다시
    ```
    "연결 실패" 하나로 보여주면 사용자가 무엇을 고쳐야 할지 알 수 없다.
    """
    account = get_account(db, org_id=org_id, account_id=account_id)

    if not conn.has_connector(account["system_code"]):
        status, message = "CONNECTOR_UNAVAILABLE", "이 시스템 연동은 아직 구현되지 않았습니다."
    elif not account["credential_configured"]:
        status, message = "CREDENTIAL_NOT_CONFIGURED", "자격증명이 설정되지 않았습니다."
    else:
        # 실제 호출은 커넥터가 한다. 평문은 여기서 만들어져 커넥터 호출이
        # 끝나면 사라진다 — 저장하지도 로그하지도 않는다.
        #
        # 응답 본문을 그대로 저장하지 않는다: 외부 오류 메시지에 토큰이 섞여
        # 오는 일이 있다. 예외도 타입 이름만 남긴다 — 메시지에 URL 과 쿼리
        # 문자열이 통째로 들어 있는 라이브러리가 흔하다.
        from app.core.secret_store import (
            CredentialStoreNotConfiguredError,
            SecretDecryptionError,
            SecretNotFoundError,
        )
        from app.services.credential_resolver import resolve_for_account

        try:
            credential = resolve_for_account(
                db, org_id=org_id, account_id=account_id,
            )
            result = conn.get_connector(account["system_code"]).test_connection(
                credentials=credential.as_dict(),
            )
            status = "OK" if result.ok else "AUTHENTICATION_FAILED"
            message = result.message[:200]
        except SecretNotFoundError:
            # 계정은 설정됐다는데 저장소에 없다. 미설정으로 뭉개지 않는다 —
            # 사용자가 다시 설정해도 원인(키 교체 실수 등)은 남는다.
            status = "CREDENTIAL_BROKEN"
            message = "자격증명 참조는 있는데 저장소에 값이 없습니다."
        except (SecretDecryptionError, CredentialStoreNotConfiguredError):
            status = "CREDENTIAL_BROKEN"
            message = "자격증명을 복호화할 수 없습니다. 마스터 키를 확인하세요."
        except Exception as exc:  # noqa: BLE001
            status, message = "CONNECTION_FAILED", type(exc).__name__

    db.execute(text("""
        UPDATE external_account
           SET last_test_status = :s, last_test_at = now(), last_test_message = :m,
               last_connected_at = CASE WHEN :s = 'OK' THEN now() ELSE last_connected_at END,
               status = CASE WHEN :s = 'OK' THEN 'CONNECTED' ELSE status END
         WHERE id = :i AND org_id = :org
    """), {"s": status, "m": message, "i": str(account_id), "org": str(org_id)})
    db.commit()

    _ = actor_user_id
    return {"status": status, "message": message,
            "account": get_account(db, org_id=org_id, account_id=account_id)}


# ──────────────────────────────────────────────────────────────────────────
# 매핑
# ──────────────────────────────────────────────────────────────────────────

def _assert_internal_exists(
    db: Session, org_id: UUID, entity_type: str, entity_id: UUID
) -> None:
    table = {"PRODUCT": "product"}.get(entity_type)
    if table is None:
        raise ValidationError(f"아직 지원하지 않는 대상 유형입니다: {entity_type}")
    ok = db.execute(text(f"""
        SELECT 1 FROM {table}
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(entity_id), "org": str(org_id)}).first()
    if ok is None:
        raise NotFoundError("연결할 내부 대상을 찾을 수 없습니다.")


def upsert_mapping(
    db: Session, *, org_id: UUID, actor_user_id: UUID | None,
    external_account_id: UUID, external_entity_type: str, external_entity_id: str,
    internal_entity_type: str, internal_entity_id: UUID, _commit: bool = True,
) -> dict[str, Any]:
    """매핑 생성 또는 확인.

    **이미 다른 내부 대상에 붙어 있으면 조용히 옮기지 않는다.** 옮기는 것은
    재무적으로 의미가 큰 행위라 명시적인 `remap_mapping` 을 거친다.
    """
    account = get_account(db, org_id=org_id, account_id=external_account_id)
    _assert_internal_exists(db, org_id, internal_entity_type, internal_entity_id)

    existing = db.execute(text("""
        SELECT id, internal_entity_id, internal_entity_type
          FROM external_entity_mapping
         WHERE external_account_id = :a AND external_entity_type = :et
           AND external_entity_id = :eid AND deleted_at IS NULL
    """), {"a": str(external_account_id), "et": external_entity_type,
           "eid": external_entity_id}).mappings().first()

    if existing is not None:
        if str(existing["internal_entity_id"]) != str(internal_entity_id):
            raise ConflictError(
                "이 외부 항목은 이미 다른 내부 항목에 연결돼 있습니다. "
                "연결을 옮기려면 재연결을 사용하세요.",
                detail={"mapping_id": str(existing["id"]),
                        "current_internal_entity_id": str(existing["internal_entity_id"])},
            )
        db.execute(text("""
            UPDATE external_entity_mapping SET last_seen_at = now()
             WHERE id = :i
        """), {"i": str(existing["id"])})
        if _commit:
            db.commit()
        return {"id": str(existing["id"]), "created": False}

    row = db.execute(text("""
        INSERT INTO external_entity_mapping
               (org_id, external_account_id, external_entity_type, external_entity_id,
                internal_entity_type, internal_entity_id, last_seen_at,
                created_by, updated_by)
        VALUES (:org, :a, :et, :eid, :it, :iid, now(), :actor, :actor)
        RETURNING id
    """), {"org": str(org_id), "a": str(external_account_id),
           "et": external_entity_type, "eid": external_entity_id,
           "it": internal_entity_type, "iid": str(internal_entity_id),
           "actor": str(actor_user_id) if actor_user_id else None}).mappings().one()

    mid = UUID(str(row["id"]))
    record_audit(
        db, org_id=org_id, domain_table="external_entity_mapping", domain_row_id=mid,
        action="CREATE", actor_user_id=actor_user_id,
        after={"system": account["system_code"], "external_id": external_entity_id,
               "internal_id": str(internal_entity_id)},
    )
    if _commit:
        db.commit()
    return {"id": str(mid), "created": True}


def remap_mapping(
    db: Session, *, org_id: UUID, actor_user_id: UUID, mapping_id: UUID,
    internal_entity_id: UUID, reason: str,
) -> dict[str, Any]:
    """연결 대상 변경. **사유가 필수다.**

    외부 상품이 가리키는 내부 상품이 바뀌면 그 뒤의 재고·원가가 전부 다른
    상품으로 들어간다. 나중에 "왜 이 상품에 저 재고가" 를 설명할 수 있어야 한다.
    """
    if not reason.strip():
        raise ValidationError("재연결에는 사유가 필요합니다.")

    row = db.execute(text("""
        SELECT internal_entity_type, internal_entity_id FROM external_entity_mapping
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(mapping_id), "org": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("연결을 찾을 수 없습니다.")
    _assert_internal_exists(db, org_id, row["internal_entity_type"], internal_entity_id)

    db.execute(text("""
        UPDATE external_entity_mapping
           SET internal_entity_id = :new, updated_at = now(), updated_by = :a
         WHERE id = :i AND org_id = :org
    """), {"new": str(internal_entity_id), "i": str(mapping_id),
           "org": str(org_id), "a": str(actor_user_id)})

    record_audit(
        db, org_id=org_id, domain_table="external_entity_mapping",
        domain_row_id=mapping_id, action="UPDATE", actor_user_id=actor_user_id,
        before={"internal_entity_id": str(row["internal_entity_id"])},
        after={"internal_entity_id": str(internal_entity_id)}, reason=reason,
    )
    db.commit()
    return {"id": str(mapping_id), "internal_entity_id": str(internal_entity_id)}


def list_mappings(
    db: Session, *, org_id: UUID, external_account_id: UUID | None = None,
    internal_entity_id: UUID | None = None,
) -> list[dict[str, Any]]:
    where = ["m.org_id = :org", "m.deleted_at IS NULL"]
    params: dict[str, Any] = {"org": str(org_id)}
    if external_account_id:
        where.append("m.external_account_id = :a")
        params["a"] = str(external_account_id)
    if internal_entity_id:
        where.append("m.internal_entity_id = :iid")
        params["iid"] = str(internal_entity_id)

    rows = db.execute(text(f"""
        SELECT m.id, m.external_entity_type, m.external_entity_id,
               m.internal_entity_type, m.internal_entity_id, m.mapping_status,
               m.first_seen_at, m.last_seen_at,
               a.display_name AS account_name, s.code AS system_code
          FROM external_entity_mapping m
          JOIN external_account a ON a.id = m.external_account_id
          JOIN external_system s ON s.id = a.external_system_id
         WHERE {" AND ".join(where)}
         ORDER BY m.first_seen_at DESC
    """), params).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["internal_entity_id"] = str(d["internal_entity_id"])
        out.append(d)
    return out


# ──────────────────────────────────────────────────────────────────────────
# 권위 (authority)
# ──────────────────────────────────────────────────────────────────────────

def mark_manual_override(
    db: Session, *, org_id: UUID, actor_user_id: UUID,
    internal_entity_type: str, internal_entity_id: UUID, field_name: str,
) -> None:
    """이 필드는 사람이 정했다 — 외부 sync 가 덮지 않는다.

    사용자가 고친 값을 다음 sync 가 되돌리면 같은 수정을 매번 반복하게 되고,
    결국 동기화를 꺼 버린다.
    """
    db.execute(text("""
        INSERT INTO external_field_authority
               (org_id, internal_entity_type, internal_entity_id, field_name,
                authority, set_by)
        VALUES (:org, :it, :iid, :f, 'MANUAL_OVERRIDE', :a)
        ON CONFLICT (org_id, internal_entity_type, internal_entity_id, field_name)
        DO UPDATE SET authority = 'MANUAL_OVERRIDE', set_at = now(),
                      set_by = EXCLUDED.set_by
    """), {"org": str(org_id), "it": internal_entity_type,
           "iid": str(internal_entity_id), "f": field_name, "a": str(actor_user_id)})


def authority_of(
    db: Session, *, org_id: UUID, internal_entity_type: str,
    internal_entity_id: UUID,
) -> dict[str, str]:
    rows = db.execute(text("""
        SELECT field_name, authority FROM external_field_authority
         WHERE org_id = :org AND internal_entity_type = :it AND internal_entity_id = :iid
    """), {"org": str(org_id), "it": internal_entity_type,
           "iid": str(internal_entity_id)}).all()
    return {r[0]: r[1] for r in rows}


# ──────────────────────────────────────────────────────────────────────────
# 동기화
# ──────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class SyncResult:
    run_id: str
    status: str
    read: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    conflicts: int = 0
    errors: int = 0
    messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "status": self.status, "read_count": self.read,
            "created_count": self.created, "updated_count": self.updated,
            "skipped_count": self.skipped, "conflict_count": self.conflicts,
            "error_count": self.errors, "messages": self.messages,
        }


def sync_products(
    db: Session, *, org_id: UUID, actor_user_id: UUID, account_id: UUID,
    since: date | None = None, credentials: dict[str, Any] | None = None,
) -> SyncResult:
    """외부 상품 동기화.

    한 건 실패가 전체를 막지 않는다
    ---------------------------
    상품 1,000건 중 1건이 잘못됐다고 999건을 되돌리면 운영이 안 된다.
    **행 단위로 커밋**하고 실패는 세어 둔다. 대신 실행 기록에 정확한 건수가
    남아서 무엇이 반영됐는지 알 수 있다.

    **내부 상품을 새로 만들지 않는다.** 외부에 있는데 우리에게 없는 상품은
    `skipped` 로 센다 — 셀러툴을 master 로 만들지 않는다는 뜻이다.
    사람이 먼저 상품을 만들고 연결한다.
    """
    account = get_account(db, org_id=org_id, account_id=account_id)
    run_id = _start_run(db, org_id=org_id, account_id=account_id,
                        sync_type="PRODUCTS", actor_user_id=actor_user_id)

    # 커넥터가 없으면 실패가 아니라 **미지원**이다. 성공으로 만들지 않는다.
    if not conn.has_connector(account["system_code"]):
        _finish_run(db, run_id=run_id, status="UNSUPPORTED",
                    error_code="CONNECTOR_UNAVAILABLE",
                    error_message=f"{account['system_code']} 연동이 구현되지 않았습니다.")
        raise conn.ConnectorUnavailableError(
            f"{account['system_code']} 연동은 아직 구현되지 않았습니다.",
        )
    conn.assert_supports(account["system_code"], "PRODUCTS")

    # 자격증명이 없으면 **거부한다.** 빈 dict 로 호출해서 커넥터가 401 을
    # 받게 두면 실패 사유가 "외부 서버 오류" 로 기록되고, 정작 고쳐야 할 것은
    # 우리 설정이다.
    #
    # `credentials` 인자는 테스트가 주입할 때만 쓴다. 운영 경로에서는 항상
    # resolver 를 거친다 — 커넥터는 저장소도 세션도 모른다.
    if credentials is None:
        from app.services.credential_resolver import (
            CredentialNotConfiguredError,
            resolve_for_account,
        )

        try:
            credentials = resolve_for_account(
                db, org_id=org_id, account_id=account_id,
            ).as_dict()
        except CredentialNotConfiguredError:
            _finish_run(db, run_id=run_id, status="FAILED",
                        error_code="CREDENTIAL_NOT_CONFIGURED",
                        error_message="자격증명이 설정되지 않았습니다.")
            raise
        except Exception as exc:  # noqa: BLE001
            # 복호화 실패·저장소 미설정. 예외 메시지를 그대로 남기지 않는다.
            _finish_run(db, run_id=run_id, status="FAILED",
                        error_code="CREDENTIAL_UNAVAILABLE",
                        error_message=type(exc).__name__)
            raise

    connector = conn.get_connector(account["system_code"])
    result = SyncResult(run_id=str(run_id), status="RUNNING")

    try:
        items = connector.fetch_products(credentials=credentials, since=since)
    except Exception as exc:  # noqa: BLE001
        # 외부 예외 메시지에 인증 헤더나 쿼리 문자열이 섞여 오는 일이 있다.
        # 타입 이름만 남긴다 — 원문을 남기면 sync_run 이 새는 통로가 된다.
        _finish_run(db, run_id=run_id, status="FAILED",
                    error_code="FETCH_FAILED", error_message=type(exc).__name__)
        raise

    result.read = len(items)

    for item in items:
        try:
            outcome = _apply_product(
                db, org_id=org_id, actor_user_id=actor_user_id, account=account,
                run_id=run_id, item=item,
            )
            setattr(result, outcome, getattr(result, outcome) + 1)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            # 한 건 실패가 나머지를 막지 않는다. 무엇이 실패했는지는 남긴다.
            db.rollback()
            result.errors += 1
            result.messages.append(f"{item.external_id}: {exc}")

    result.status = "PARTIAL" if result.errors else "SUCCESS"
    _finish_run(db, run_id=run_id, status=result.status, result=result)
    if result.errors == 0:
        db.execute(text("""
            UPDATE external_account SET last_successful_sync_at = now()
             WHERE id = :i
        """), {"i": str(account_id)})
        db.commit()
    return result


def _apply_product(
    db: Session, *, org_id: UUID, actor_user_id: UUID, account: dict[str, Any],
    run_id: UUID, item: conn.ExternalProduct,
) -> str:
    """외부 상품 한 건. 반환값은 `SyncResult` 의 카운터 이름이다."""
    mapping = db.execute(text("""
        SELECT id, internal_entity_id FROM external_entity_mapping
         WHERE external_account_id = :a AND external_entity_type = 'PRODUCT'
           AND external_entity_id = :eid AND deleted_at IS NULL
    """), {"a": account["id"], "eid": item.external_id}).mappings().first()

    if mapping is None:
        # 우리에게 없는 상품을 만들지 않는다 — 셀러툴이 master 가 아니다.
        return "skipped"

    product_id = UUID(str(mapping["internal_entity_id"]))
    db.execute(text("""
        UPDATE external_entity_mapping SET last_seen_at = now() WHERE id = :i
    """), {"i": str(mapping["id"])})

    authority = authority_of(db, org_id=org_id, internal_entity_type="PRODUCT",
                             internal_entity_id=product_id)
    changed = False

    for fname in SYNCABLE_PRODUCT_FIELDS:
        incoming = getattr(item, {"name_ko": "name"}[fname], None)
        if incoming is None:
            continue
        current = db.execute(text(f"""
            SELECT {fname} FROM product WHERE id = :i
        """), {"i": str(product_id)}).scalar()
        if str(current) == str(incoming):
            continue

        if authority.get(fname) == "MANUAL_OVERRIDE":
            # 사람이 정한 값이다. 덮지 않고 충돌로 남긴다.
            _record_conflict(
                db, org_id=org_id, run_id=run_id, mapping_id=UUID(str(mapping["id"])),
                field_name=fname, external_value=str(incoming),
                internal_value=str(current),
            )
            return "conflicts"

        db.execute(text(f"""
            UPDATE product SET {fname} = :v, updated_at = now() WHERE id = :i
        """), {"v": incoming, "i": str(product_id)})
        changed = True

    # 소싱 원가는 **상품 원가로 가지 않는다.** 관측 사실로만 쌓인다.
    if item.source_cost is not None:
        _apply_source_cost(db, org_id=org_id, actor_user_id=actor_user_id,
                           product_id=product_id, item=item)
        changed = True

    return "updated" if changed else "skipped"


def _apply_source_cost(
    db: Session, *, org_id: UUID, actor_user_id: UUID, product_id: UUID,
    item: conn.ExternalProduct,
) -> None:
    """외부가 준 매입가를 **소싱 원가 version** 으로 쌓는다.

    `product.cost_amount` 는 건드리지 않는다. 그 값은 `COST_CHANGE` 승인으로만
    바뀐다 — 셀러툴이 값을 준다고 우리 계산 기준이 자동으로 바뀌면 안 된다.

    주 소싱처가 없으면 아무것도 하지 않는다. 공급처를 추측해서 만들지 않는다.
    """
    from app.services import sourcing_core as sc

    primary = sc.primary_source_of(db, org_id=org_id, product_id=product_id)
    if primary is None:
        return

    at = item.observed_at or date.today()
    existing = sc.cost_version_at(db, org_id=org_id,
                                  product_source_id=UUID(primary["id"]), at=at)
    if existing and int(existing["purchase_price"]) == int(item.source_cost):
        return

    sc.add_cost_version(
        db, org_id=org_id, actor_user_id=actor_user_id,
        product_source_id=UUID(primary["id"]),
        purchase_price=int(item.source_cost),
        currency=item.source_cost_currency or primary["currency"],
        valid_from=at, source_type="SELLER_TOOL",
        memo=f"외부 동기화 ({item.external_id})",
    )


def _record_conflict(
    db: Session, *, org_id: UUID, run_id: UUID, mapping_id: UUID,
    field_name: str, external_value: str, internal_value: str,
) -> None:
    db.execute(text("""
        INSERT INTO external_sync_conflict
               (org_id, sync_run_id, mapping_id, field_name,
                external_value, internal_value)
        VALUES (:org, :r, :m, :f, :ev, :iv)
    """), {"org": str(org_id), "r": str(run_id), "m": str(mapping_id),
           "f": field_name, "ev": external_value[:500], "iv": internal_value[:500]})


def _start_run(
    db: Session, *, org_id: UUID, account_id: UUID, sync_type: str,
    actor_user_id: UUID, trigger: str = "MANUAL",
) -> UUID:
    row = db.execute(text("""
        INSERT INTO external_sync_run
               (org_id, external_account_id, sync_type, trigger, created_by)
        VALUES (:org, :a, :t, :tr, :actor)
        RETURNING id
    """), {"org": str(org_id), "a": str(account_id), "t": sync_type,
           "tr": trigger, "actor": str(actor_user_id)}).mappings().one()
    db.commit()
    return UUID(str(row["id"]))


def _finish_run(
    db: Session, *, run_id: UUID, status: str, result: SyncResult | None = None,
    error_code: str | None = None, error_message: str | None = None,
) -> None:
    db.execute(text("""
        UPDATE external_sync_run
           SET status = :s, finished_at = now(),
               read_count = :read, created_count = :created,
               updated_count = :updated, skipped_count = :skipped,
               conflict_count = :conflicts, error_count = :errors,
               error_code = :ec, error_message = :em
         WHERE id = :i
    """), {
        "s": status, "i": str(run_id),
        "read": result.read if result else None,
        "created": result.created if result else None,
        "updated": result.updated if result else None,
        "skipped": result.skipped if result else None,
        "conflicts": result.conflicts if result else None,
        "errors": result.errors if result else None,
        "ec": error_code, "em": error_message,
    })
    db.commit()


def list_runs(
    db: Session, *, org_id: UUID, account_id: UUID | None = None, limit: int = 20,
) -> list[dict[str, Any]]:
    where = ["org_id = :org"]
    params: dict[str, Any] = {"org": str(org_id), "lim": limit}
    if account_id:
        where.append("external_account_id = :a")
        params["a"] = str(account_id)

    rows = db.execute(text(f"""
        SELECT id, external_account_id, sync_type, trigger, status,
               started_at, finished_at, read_count, created_count, updated_count,
               skipped_count, conflict_count, error_count, error_code, error_message
          FROM external_sync_run
         WHERE {" AND ".join(where)}
         ORDER BY started_at DESC LIMIT :lim
    """), params).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["external_account_id"] = str(d["external_account_id"])
        out.append(d)
    return out


#: 충돌 목록에 값을 그대로 실으면 안 되는 필드.
#
# 충돌 행은 "외부는 A 라 하고 우리는 B 다" 라서 **두 값이 다 들어 있다.** 원가
# 관련 필드면 이 화면이 원가 조회 권한을 우회하는 통로가 된다. 프론트에서
# 숨기는 것으로는 부족하다 — API 를 직접 부르면 뚫린다.
_COST_CONFLICT_FIELDS = frozenset({
    "cost_amount", "cost_amount_foreign", "cost_fx_rate", "purchase_price",
    "unit_cost", "supply_price", "total_cost",
})


def list_conflicts(
    db: Session, *, org_id: UUID, pending_only: bool = True,
    viewer_role: str | None = None,
) -> list[dict[str, Any]]:
    """미해결 충돌 목록.

    `viewer_role` 이 원가를 볼 수 없는 역할이면 원가 필드의 두 값을 **응답에서**
    지운다. 무엇이 충돌했는지(필드명)는 남긴다 — 가려졌다는 사실까지 숨기면
    사용자는 목록이 비어 보이는 이유를 알 수 없다.
    """
    from app.core.sensitive import can_view_source_cost

    mask_cost = viewer_role is not None and not can_view_source_cost(viewer_role)
    where = ["c.org_id = :org"]
    if pending_only:
        where.append("c.resolution = 'PENDING'")
    # 어떤 상품에서 난 충돌인지 없으면 목록만 보고는 아무것도 할 수 없다.
    # 상품명은 원가가 아니므로 가리지 않는다.
    rows = db.execute(text(f"""
        SELECT c.id, c.sync_run_id, c.mapping_id, c.field_name, c.external_value,
               c.internal_value, c.resolution, c.created_at,
               m.external_entity_id, m.internal_entity_type,
               p.name_ko AS internal_entity_name,
               a.display_name AS account_name
          FROM external_sync_conflict c
          LEFT JOIN external_entity_mapping m ON m.id = c.mapping_id
          LEFT JOIN product p
                 ON m.internal_entity_type = 'PRODUCT'
                AND p.id = m.internal_entity_id
          LEFT JOIN external_account a ON a.id = m.external_account_id
         WHERE {" AND ".join(where)}
         ORDER BY c.created_at DESC LIMIT 200
    """), {"org": str(org_id)}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("id", "sync_run_id", "mapping_id"):
            d[k] = str(d[k]) if d[k] else None
        d["values_masked"] = False
        if mask_cost and d["field_name"] in _COST_CONFLICT_FIELDS:
            d["external_value"] = None
            d["internal_value"] = None
            d["values_masked"] = True
        out.append(d)
    return out
