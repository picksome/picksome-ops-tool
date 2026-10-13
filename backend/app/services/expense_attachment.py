"""증빙 첨부 — 업로드 · 다운로드 · 삭제 (M027).

이 파일이 지키는 것
------------------
1. **storage 와 DB 가 어긋나지 않는다.**
   · storage 쓰기가 실패하면 DB 행을 만들지 않는다
   · DB INSERT 가 실패하면 방금 쓴 파일을 지운다(orphan 정리)
   · 삭제는 **DB 상태 전이가 먼저**고 실제 파일 삭제는 나중이다
2. **사용자 입력이 경로가 되지 않는다.** 원본 파일명은 표시용 컬럼일 뿐이다.
3. **하지 않은 일을 했다고 말하지 않는다.** 악성코드 스캐너가 없으므로
   모든 파일은 `SCAN_UNAVAILABLE` 이다. `CLEAN` 을 붙이지 않는다.

월 잠금은 추가와 삭제를 다르게 본다
---------------------------------
**추가는 잠긴 달에도 허용한다.** 세무사가 마감 후에 "이 비용 증빙 주세요" 라고
요구하는 일이 흔하고, 증빙을 붙이는 것은 **금액을 바꾸지 않는다** —
그 달의 순이익도 수익배분도 그대로다. 막으면 사용자는 시스템 밖에서 파일을
주고받게 되고, 그러면 증빙 관리라는 목적 자체가 사라진다.

**삭제는 막는다.** 확정된 달의 증빙이 사라지면 그 달의 근거가 사라진다.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.attachment_storage import (
    ExpenseAttachmentStorage,
    StorageError,
    build_key,
    get_storage,
)
from app.core.audit import record_audit
from app.core.exceptions import (
    ApprovalRequiredError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.month_lock import assert_period_writable

# ── 업로드 제한 ────────────────────────────────────────────────
#
# 확장자와 MIME 을 **둘 다** 본다. 확장자만 보면 `.pdf` 로 이름 붙인 실행파일이
# 통과하고, MIME 만 보면 클라이언트가 헤더를 조작한다. 둘이 서로를 검증한다.
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS_PER_EXPENSE = 5

ALLOWED: dict[str, frozenset[str]] = {
    "jpg": frozenset({"image/jpeg"}),
    "jpeg": frozenset({"image/jpeg"}),
    "png": frozenset({"image/png"}),
    "heic": frozenset({"image/heic", "image/heif"}),
    "pdf": frozenset({"application/pdf"}),
}

# 파일 시작 바이트(magic). 확장자·MIME 을 둘 다 속여도 내용은 못 속인다.
_MAGIC: dict[str, tuple[bytes, ...]] = {
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "pdf": (b"%PDF-",),
    # HEIC 은 `....ftyp` 형태라 4바이트 오프셋에서 본다(아래 별도 처리).
    "heic": (),
}

_FILENAME_MAX = 255
# 표시용 파일명에서 걷어낼 것: 경로 구분자, 제어문자, 널바이트.
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f/\\]")

# 삭제 재시도 — M024 outbox 와 같은 백오프 곡선을 쓴다.
MAX_DELETE_ATTEMPTS = 5
_DELETE_BACKOFF_MINUTES = (1, 5, 15, 60, 240)


def _now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True)
class UploadResult:
    attachment_id: UUID
    filename: str
    size_bytes: int
    duplicate_of: UUID | None = None


# ──────────────────────────────────────────────────────────────
# 검증
# ──────────────────────────────────────────────────────────────

def sanitize_filename(raw: str | None) -> str:
    """표시용 파일명. **경로로 쓰지 않는다** — 그래서 여기서 하는 일은
    "안전한 경로 만들기" 가 아니라 "화면에 그대로 넣어도 되는 문자열 만들기" 다.

    `../../etc/passwd` 같은 입력도 저장은 되지만, 저장 키는 UUID 로 따로 만들고
    이 값은 컬럼에만 들어간다. 그래도 구분자와 제어문자는 걷어낸다 —
    나중에 누군가 이 값을 경로로 쓰는 실수를 하더라도 한 겹이 남도록.
    """
    name = (raw or "").strip()
    name = name.replace("\\", "/").rsplit("/", 1)[-1]      # 경로 성분 제거
    name = _UNSAFE_NAME.sub("_", name)
    name = name.lstrip(".") or "attachment"                 # `.`·`..` 방지
    return name[:_FILENAME_MAX]


def _ext_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _looks_like(ext: str, data: bytes) -> bool:
    """내용이 확장자와 맞는가."""
    if ext == "heic":
        # ISO-BMFF: 4바이트 크기 + 'ftyp'
        return len(data) >= 12 and data[4:8] == b"ftyp"
    magics = _MAGIC.get(ext, ())
    return any(data.startswith(m) for m in magics)


def validate_upload(*, filename: str, content_type: str | None, data: bytes) -> tuple[str, str]:
    """(안전한 표시용 파일명, 확장자) 반환. 실패하면 `ValidationError`.

    오류 메시지는 **무엇을 고쳐야 하는지** 말한다. "업로드 실패" 로 끝내면
    사용자는 같은 파일을 다시 올린다.
    """
    safe_name = sanitize_filename(filename)
    ext = _ext_of(safe_name)

    if ext not in ALLOWED:
        raise ValidationError(
            f"{safe_name} — jpg·jpeg·png·heic·pdf 만 첨부할 수 있습니다.",
            detail={"filename": safe_name, "reason": "EXTENSION_NOT_ALLOWED"},
        )
    if content_type and content_type.split(";")[0].strip().lower() not in ALLOWED[ext]:
        # 확장자와 MIME 이 어긋난다 — 이름만 바꿔 올린 파일이다.
        raise ValidationError(
            f"{safe_name} — 파일 형식이 확장자와 일치하지 않습니다.",
            detail={"filename": safe_name, "reason": "MIME_MISMATCH",
                    "content_type": content_type},
        )
    if len(data) == 0:
        raise ValidationError(
            f"{safe_name} — 빈 파일입니다.",
            detail={"filename": safe_name, "reason": "EMPTY_FILE"},
        )
    if len(data) > MAX_FILE_BYTES:
        raise ValidationError(
            f"{safe_name} — 10MB를 넘습니다.",
            detail={"filename": safe_name, "reason": "TOO_LARGE",
                    "size_bytes": len(data), "max_bytes": MAX_FILE_BYTES},
        )
    if not _looks_like(ext, data):
        # 확장자·MIME 을 둘 다 맞춰 놨어도 내용이 다르면 거부한다.
        raise ValidationError(
            f"{safe_name} — 파일 내용이 {ext} 형식이 아닙니다.",
            detail={"filename": safe_name, "reason": "CONTENT_MISMATCH"},
        )
    return safe_name, ext


# ──────────────────────────────────────────────────────────────
# 조회
# ──────────────────────────────────────────────────────────────

_SELECT = """
    SELECT a.id, a.expense_id, a.org_id, a.original_filename, a.mime_type,
           a.size_bytes, a.file_hash, a.status, a.scan_status,
           a.uploaded_by, a.uploaded_at,
           a.delete_requested_at, a.delete_requested_by,
           a.delete_approval_request_id, a.delete_attempt_count, a.delete_last_error,
           u.display_name AS uploaded_by_name
      FROM expense_attachment a
      LEFT JOIN app_user u ON u.id = a.uploaded_by
"""

# 화면에 보여줄 상태. DELETED 는 목록에서 빠진다 — 지운 파일을 계속 보여주면
# 사용자가 삭제가 안 된 줄 안다.
_VISIBLE = ("PENDING_SCAN", "AVAILABLE", "PENDING_DELETE", "FAILED")


def _row_out(r: Any) -> dict[str, Any]:
    """API 응답 형태. **storage_key 를 넣지 않는다.**"""
    return {
        "id": str(r["id"]),
        "expense_id": str(r["expense_id"]),
        "filename": r["original_filename"],
        "mime_type": r["mime_type"],
        "size_bytes": r["size_bytes"],
        "file_hash": r["file_hash"],
        "status": r["status"],
        "scan_status": r["scan_status"],
        "uploaded_by": str(r["uploaded_by"]) if r["uploaded_by"] else None,
        "uploaded_by_name": r["uploaded_by_name"],
        "uploaded_at": r["uploaded_at"].isoformat() if r["uploaded_at"] else None,
        "delete_requested_at": (
            r["delete_requested_at"].isoformat() if r["delete_requested_at"] else None
        ),
        "delete_approval_request_id": (
            str(r["delete_approval_request_id"]) if r["delete_approval_request_id"] else None
        ),
        "delete_attempt_count": r["delete_attempt_count"],
        "delete_last_error": r["delete_last_error"],
    }


def list_attachments(db: Session, *, org_id: UUID, expense_id: UUID) -> list[dict[str, Any]]:
    _assert_expense(db, org_id=org_id, expense_id=expense_id)
    rows = db.execute(text(f"""
        {_SELECT}
         WHERE a.expense_id = :e AND a.org_id = :o
           AND a.deleted_at IS NULL AND a.status = ANY(:vis)
         ORDER BY a.uploaded_at NULLS LAST, a.id
    """), {"e": str(expense_id), "o": str(org_id), "vis": list(_VISIBLE)}).mappings().all()
    return [_row_out(r) for r in rows]


def get_attachment(db: Session, *, org_id: UUID, attachment_id: UUID) -> dict[str, Any]:
    """조직 격리 — 다른 조직 첨부는 **404** 다. 403 으로 구분해 주면
    다른 조직의 id 존재 여부가 새어나간다."""
    row = db.execute(text(f"""
        {_SELECT} WHERE a.id = :i AND a.org_id = :o AND a.deleted_at IS NULL
    """), {"i": str(attachment_id), "o": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("증빙 파일을 찾을 수 없습니다.")
    return _row_out(row)


def _assert_expense(db: Session, *, org_id: UUID, expense_id: UUID) -> dict:
    row = db.execute(text("""
        SELECT id, expense_date, evidence_status
          FROM expenses
         WHERE id = :i AND org_id = :o AND deleted_at IS NULL
    """), {"i": str(expense_id), "o": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("사업비용을 찾을 수 없습니다.")
    return dict(row)


# ──────────────────────────────────────────────────────────────
# 업로드
# ──────────────────────────────────────────────────────────────

def upload_attachment(
    db: Session, *, org_id: UUID, actor: UUID, expense_id: UUID,
    filename: str, content_type: str | None, data: bytes,
    storage: ExpenseAttachmentStorage | None = None,
) -> UploadResult:
    """증빙 1건 업로드.

    순서가 중요하다.
      ① 대상·개수 검증 → ② 파일 검증 → ③ storage 쓰기 → ④ DB INSERT →
      ⑤ evidence_status 갱신 → ⑥ 감사 로그

    ③ 이 실패하면 ④ 를 하지 않는다(행 없는 상태 = 업로드 안 된 상태).
    ④ 가 실패하면 ③ 을 되돌린다(orphan 파일 제거).
    """
    store = storage or get_storage()
    # 존재·권한 검증이 목적이다. 반환값은 쓰지 않는다.
    _assert_expense(db, org_id=org_id, expense_id=expense_id)

    # **추가는 잠긴 달에도 허용한다** (모듈 docstring 참고). 삭제만 막는다.

    live = db.execute(text("""
        SELECT count(*) FROM expense_attachment
         WHERE expense_id = :e AND org_id = :o AND deleted_at IS NULL
           AND status IN ('PENDING_SCAN','AVAILABLE','PENDING_DELETE')
    """), {"e": str(expense_id), "o": str(org_id)}).scalar_one()
    if live >= MAX_ATTACHMENTS_PER_EXPENSE:
        raise ValidationError(
            f"증빙은 비용 1건당 최대 {MAX_ATTACHMENTS_PER_EXPENSE}개까지 첨부할 수 있습니다.",
            detail={"reason": "TOO_MANY_ATTACHMENTS", "current": int(live),
                    "max": MAX_ATTACHMENTS_PER_EXPENSE},
        )

    safe_name, ext = validate_upload(
        filename=filename, content_type=content_type, data=data)
    file_hash = hashlib.sha256(data).hexdigest()

    dup = db.execute(text("""
        SELECT id, original_filename FROM expense_attachment
         WHERE org_id = :o AND expense_id = :e AND file_hash = :h
           AND deleted_at IS NULL
           AND status IN ('PENDING_SCAN','AVAILABLE','PENDING_DELETE')
         LIMIT 1
    """), {"o": str(org_id), "e": str(expense_id), "h": file_hash}).mappings().first()
    if dup:
        # 같은 파일을 두 번 올리는 건 대개 실수다(더블클릭·재시도).
        # 오류로 만들지 않고 기존 것을 가리켜 준다 — 저장은 하지 않는다.
        raise ConflictError(
            f"이미 같은 파일이 첨부되어 있습니다: {dup['original_filename']}",
            detail={"reason": "DUPLICATE_FILE",
                    "existing_attachment_id": str(dup["id"]),
                    "filename": safe_name},
        )

    attachment_id = uuid4()
    key = build_key(org_id=org_id, expense_id=expense_id,
                    attachment_id=attachment_id, ext=ext)

    # ③ storage 먼저. 여기서 실패하면 DB 는 건드리지 않는다.
    stored = store.save(key=key, data=data)

    try:
        db.execute(text("""
            INSERT INTO expense_attachment
                   (id, org_id, expense_id, storage_key, storage_path,
                    original_filename, file_hash, mime_type, size_bytes,
                    status, scan_status, uploaded_by, uploaded_at,
                    created_by, updated_by)
            VALUES (:i, :o, :e, :k, :k,
                    :fn, :h, :mt, :sz,
                    'AVAILABLE', 'SCAN_UNAVAILABLE', :actor, now(),
                    :actor, :actor)
        """), {
            "i": str(attachment_id), "o": str(org_id), "e": str(expense_id),
            "k": key, "fn": safe_name, "h": file_hash,
            "mt": (content_type or "").split(";")[0].strip() or None,
            "sz": stored.size_bytes, "actor": str(actor),
        })

        # ⑤ 증빙이 하나라도 붙으면 "없음" 이 아니다. 이미 세무사 제출까지 간 건은
        #    되돌리지 않는다 — 뒤로 가는 상태 변경은 사람이 결정할 일이다.
        db.execute(text("""
            UPDATE expenses SET evidence_status = 'PRESENT', updated_by = :actor
             WHERE id = :e AND org_id = :o
               AND evidence_status IN ('NONE','NEEDS_CHECK')
        """), {"e": str(expense_id), "o": str(org_id), "actor": str(actor)})

        record_audit(
            db, org_id=org_id, domain_table="expense_attachment",
            domain_row_id=attachment_id, action="CREATE", actor_user_id=actor,
            after={"expense_id": str(expense_id), "filename": safe_name,
                   "file_hash": file_hash, "size_bytes": stored.size_bytes,
                   "mime_type": content_type},
            reason="증빙 첨부",
        )
        db.commit()
    except Exception:
        db.rollback()
        # ④ 실패 → ③ 되돌리기. 이걸 안 하면 아무도 참조하지 않는 파일이
        # 스토리지에 남고, 용량만 먹으면서 영원히 발견되지 않는다.
        try:
            store.delete(key=key)
        except StorageError:
            # 정리까지 실패하면 남는다. 삼킨다 — 원래 예외가 더 중요하다.
            pass
        raise

    return UploadResult(attachment_id=attachment_id, filename=safe_name,
                        size_bytes=stored.size_bytes)


# ──────────────────────────────────────────────────────────────
# 다운로드
# ──────────────────────────────────────────────────────────────

def open_for_download(
    db: Session, *, org_id: UUID, attachment_id: UUID,
    storage: ExpenseAttachmentStorage | None = None,
) -> tuple[Any, dict[str, Any]]:
    """(스트림, 메타) 반환. 권한 검사는 **라우터가 이미 통과시킨 뒤**다.

    `PENDING_DELETE` 는 아직 파일이 있으므로 받을 수 있다 — 삭제 승인이
    적용됐어도 worker 가 지우기 전까지는 유효한 증빙이다.
    """
    store = storage or get_storage()
    row = db.execute(text("""
        SELECT id, storage_key, original_filename, mime_type, size_bytes, status
          FROM expense_attachment
         WHERE id = :i AND org_id = :o AND deleted_at IS NULL
    """), {"i": str(attachment_id), "o": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("증빙 파일을 찾을 수 없습니다.")
    if row["status"] == "DELETED":
        raise NotFoundError("삭제된 증빙입니다.")
    if not row["storage_key"]:
        raise NotFoundError("저장된 파일이 없습니다.")

    stream = store.open(key=row["storage_key"])
    return stream, {
        "filename": row["original_filename"] or "attachment",
        "mime_type": row["mime_type"] or "application/octet-stream",
        "size_bytes": row["size_bytes"],
    }


# ──────────────────────────────────────────────────────────────
# 삭제 — 요청 · 적용 · worker
# ──────────────────────────────────────────────────────────────

def requires_delete_approval(db: Session, *, org_id: UUID, expense_id: UUID) -> bool:
    """이 비용의 증빙을 지우려면 승인이 필요한가.

    기준은 **비용이 승인 이력을 가졌는지**다. 승인을 거쳐 확정된 금액의 근거를
    말없이 없앨 수 있으면 승인 자체가 의미를 잃는다.
    """
    n = db.execute(text("""
        SELECT count(*) FROM approval_request
         WHERE org_id = :o AND target_type = 'expense' AND target_id = :e
           AND status = 'APPLIED' AND deleted_at IS NULL
    """), {"o": str(org_id), "e": str(expense_id)}).scalar_one()
    return int(n) > 0


def assert_deletable(db: Session, *, org_id: UUID, attachment_id: UUID) -> dict[str, Any]:
    """직접 삭제가 가능한지. 안 되면 **이유가 담긴** 예외를 던진다."""
    row = db.execute(text("""
        SELECT a.id, a.expense_id, a.status, e.expense_date
          FROM expense_attachment a
          JOIN expenses e ON e.id = a.expense_id AND e.org_id = a.org_id
         WHERE a.id = :i AND a.org_id = :o AND a.deleted_at IS NULL
    """), {"i": str(attachment_id), "o": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("증빙 파일을 찾을 수 없습니다.")
    if row["status"] in ("PENDING_DELETE", "DELETED"):
        raise ConflictError(
            "이미 삭제 처리 중이거나 삭제된 증빙입니다.",
            detail={"status": row["status"]},
        )

    # 잠긴 달은 직접 삭제 금지. 승인으로도 우회되지 않는다 —
    # 적용 경로도 같은 가드를 통과한다.
    assert_period_writable(
        db, org_id=org_id, effective_date=row["expense_date"],
        operation="DELETE", what="증빙 파일",
    )

    if requires_delete_approval(db, org_id=org_id, expense_id=row["expense_id"]):
        raise ApprovalRequiredError(
            "승인된 비용의 증빙은 승인을 거쳐야 삭제할 수 있습니다.",
            detail={"approval_required_fields": ["attachment"],
                    "target_type": "expense_attachment",
                    "target_id": str(attachment_id),
                    "request_type": "EXPENSE_EVIDENCE_CHANGE"},
        )
    return dict(row)


def mark_pending_delete(
    db: Session, *, org_id: UUID, actor: UUID, attachment_id: UUID,
    approval_request_id: UUID | None = None, _commit: bool = True,
) -> dict[str, Any]:
    """실제 파일은 **지금 지우지 않는다.** 상태만 바꾸고 worker 에게 넘긴다.

    승인 적용 트랜잭션에서 파일을 지우면, 그 트랜잭션이 뒤에 롤백됐을 때
    파일만 사라지고 되돌릴 방법이 없다.

    `_commit=False` 는 승인 적용 경로 전용이다 — 커밋 소유권은 `approval.apply()`
    에 있고, 여기서 커밋하면 감사 로그가 실패해도 상태 변경만 남는다.
    """
    row = db.execute(text("""
        UPDATE expense_attachment
           SET status = 'PENDING_DELETE',
               delete_requested_at = now(), delete_requested_by = :actor,
               delete_approval_request_id = :ar,
               delete_next_attempt_at = now(), delete_attempt_count = 0,
               delete_last_error = NULL, updated_by = :actor
         WHERE id = :i AND org_id = :o AND deleted_at IS NULL
           AND status IN ('AVAILABLE','PENDING_SCAN','FAILED')
     RETURNING id, expense_id, original_filename, file_hash
    """), {"i": str(attachment_id), "o": str(org_id), "actor": str(actor),
           "ar": str(approval_request_id) if approval_request_id else None}).mappings().first()
    if row is None:
        raise ConflictError(
            "삭제할 수 없는 상태입니다. 이미 삭제 처리 중일 수 있습니다.",
            detail={"attachment_id": str(attachment_id)},
        )

    record_audit(
        db, org_id=org_id, domain_table="expense_attachment",
        domain_row_id=attachment_id, action="DELETE", actor_user_id=actor,
        before={"status": "AVAILABLE"},
        after={"status": "PENDING_DELETE", "filename": row["original_filename"],
               "file_hash": row["file_hash"],
               "approval_request_id": str(approval_request_id) if approval_request_id else None},
        reason="증빙 삭제 요청",
    )
    if _commit:
        db.commit()
    return {"id": str(row["id"]), "expense_id": str(row["expense_id"]),
            "status": "PENDING_DELETE"}


@dataclass(slots=True)
class DeleteBatchResult:
    scanned: int = 0
    deleted: int = 0
    retry_pending: int = 0
    failed: int = 0
    details: list[dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"scanned": self.scanned, "deleted": self.deleted,
                "retry_pending": self.retry_pending, "failed": self.failed,
                "details": self.details or []}


def run_delete_worker(
    db: Session, *, org_id: UUID | None = None, batch_size: int = 50,
    storage: ExpenseAttachmentStorage | None = None,
) -> DeleteBatchResult:
    """`PENDING_DELETE` 를 실제로 지운다.

    **실패해도 삭제 완료로 표시하지 않는다.** 상태를 `PENDING_DELETE` 로 두고
    백오프 후 재시도한다. 시도 한도를 넘기면 `delete_last_error` 를 남긴 채
    멈춘다 — 사람이 봐야 한다는 뜻이지 "지워졌다" 는 뜻이 아니다.
    """
    store = storage or get_storage()
    result = DeleteBatchResult(details=[])

    where = ["status = 'PENDING_DELETE'",
             "(delete_next_attempt_at IS NULL OR delete_next_attempt_at <= now())",
             "delete_attempt_count < :maxa"]
    params: dict[str, Any] = {"lim": batch_size, "maxa": MAX_DELETE_ATTEMPTS}
    if org_id is not None:
        where.append("org_id = :org")
        params["org"] = str(org_id)

    # SKIP LOCKED — worker 두 개가 같은 행을 집지 않는다.
    rows = db.execute(text(f"""
        SELECT id, org_id, expense_id, storage_key, delete_attempt_count
          FROM expense_attachment
         WHERE {' AND '.join(where)}
         ORDER BY delete_requested_at NULLS FIRST, id
           FOR UPDATE SKIP LOCKED
         LIMIT :lim
    """), params).mappings().all()
    result.scanned = len(rows)

    for r in rows:
        aid = r["id"]
        attempt = int(r["delete_attempt_count"]) + 1
        try:
            if r["storage_key"]:
                store.delete(key=r["storage_key"])
        except StorageError as exc:
            over = attempt >= MAX_DELETE_ATTEMPTS
            backoff = _DELETE_BACKOFF_MINUTES[min(attempt - 1, len(_DELETE_BACKOFF_MINUTES) - 1)]
            db.execute(text("""
                UPDATE expense_attachment
                   SET delete_attempt_count = :n,
                       delete_next_attempt_at = CASE WHEN :over THEN NULL
                                                     ELSE now() + make_interval(mins => :mins) END,
                       delete_last_error = :err
                 WHERE id = :i
            """), {"n": attempt, "over": over, "mins": backoff,
                   "err": str(exc)[:500], "i": str(aid)})
            if over:
                result.failed += 1
            else:
                result.retry_pending += 1
            result.details.append({"attachment_id": str(aid), "result": "RETRY" if not over else "FAILED",
                                   "message": str(exc)[:200]})
            continue

        # 파일이 실제로 사라진 뒤에만 DELETED 로 넘어간다.
        db.execute(text("""
            UPDATE expense_attachment
               SET status = 'DELETED', storage_deleted_at = now(),
                   delete_attempt_count = :n, delete_next_attempt_at = NULL,
                   delete_last_error = NULL,
                   deleted_at = now(), deleted_by = delete_requested_by
             WHERE id = :i
        """), {"n": attempt, "i": str(aid)})

        # 남은 증빙이 없으면 비용의 증빙 상태를 되돌린다 — 안 그러면
        # 파일이 하나도 없는데 "보유" 로 남아 증빙 누락 리포트에서 빠진다.
        db.execute(text("""
            UPDATE expenses e
               SET evidence_status = 'NONE'
             WHERE e.id = :e AND e.org_id = :o
               AND e.evidence_status = 'PRESENT'
               AND NOT EXISTS (
                     SELECT 1 FROM expense_attachment a
                      WHERE a.expense_id = e.id AND a.deleted_at IS NULL
                        AND a.status IN ('PENDING_SCAN','AVAILABLE','PENDING_DELETE'))
        """), {"e": str(r["expense_id"]), "o": str(r["org_id"])})

        result.deleted += 1
        result.details.append({"attachment_id": str(aid), "result": "DELETED"})

    db.commit()
    return result


def retry_delete(db: Session, *, org_id: UUID, actor: UUID, attachment_id: UUID) -> dict[str, Any]:
    """시도 한도를 넘겨 멈춘 삭제를 사람이 다시 밀어 넣는다."""
    row = db.execute(text("""
        UPDATE expense_attachment
           SET delete_attempt_count = 0, delete_next_attempt_at = now(),
               delete_last_error = NULL, updated_by = :actor
         WHERE id = :i AND org_id = :o AND status = 'PENDING_DELETE'
     RETURNING id
    """), {"i": str(attachment_id), "o": str(org_id), "actor": str(actor)}).mappings().first()
    if row is None:
        raise ConflictError("재시도할 수 있는 삭제 작업이 아닙니다.")
    db.commit()
    return {"id": str(row["id"]), "status": "PENDING_DELETE"}
