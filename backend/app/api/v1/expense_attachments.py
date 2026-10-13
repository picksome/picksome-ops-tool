"""증빙 파일 라우터 (M027).

endpoints:
  GET    /expenses/{id}/attachments            — 목록 (storage_key 미노출)
  POST   /expenses/{id}/attachments            — 업로드 (multipart, 파일 1건)
  GET    /expense-attachments/{aid}/download   — 다운로드 (별도 권한)
  DELETE /expense-attachments/{aid}            — 직접 삭제 (미승인 비용 · 열린 달만)
  POST   /expense-attachments/{aid}/retry-delete — 멈춘 삭제 재시도 (ADMIN)
  POST   /expense-attachments/run-delete       — 삭제 worker 1회 실행 (ADMIN)

**경로가 두 개로 나뉜 이유**: 승인 화면과 삭제 worker 는 비용 id 를 모르고
첨부 id 만 안다. `/expenses/{expense_id}/attachments/{aid}` 로만 두면 화면이
쓰지도 않을 상위 id 를 억지로 들고 다녀야 한다. (판매처 listing 과 같은 이유)

업로드는 **파일 1건씩** 받는다. 여러 개를 한 요청에 묶으면 3번째가 실패했을 때
앞의 2건을 어떻게 할지가 애매해진다 — 부분 성공을 HTTP 상태 하나로 표현할 수 없다.
화면이 병렬로 여러 번 부르고 **파일별로** 성공·실패를 보여준다.
"""

from __future__ import annotations

from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, ValidationError
from app.core.rbac import require_roles
from app.core.response import envelope
from app.core.sensitive import can_download_expense_attachment
from app.deps import CurrentUser, get_db
from app.services import expense_attachment as svc

# 비용 하위 경로 — 목록·업로드.
expense_scoped_router = APIRouter(prefix="/expenses", tags=["expenses"])
# 첨부 단독 경로 — 다운로드·삭제·worker.
attachment_router = APIRouter(prefix="/expense-attachments", tags=["expenses"])

_read = require_roles("ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY")
_write = require_roles("ADMIN", "OPERATOR", "ACCOUNTING")
_admin = require_roles("ADMIN")
# 다운로드는 role 로 한 번, permission 으로 한 번 더 거른다.
# EXTERNAL_ACCOUNTANT 는 신고 자료를 만들 때 원본이 필요하다.
_download_roles = require_roles(
    "ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY", "EXTERNAL_ACCOUNTANT")

# 업로드 본문 상한. 서비스가 다시 검사하지만, 여기서 먼저 끊어야
# 100MB 짜리를 통째로 메모리에 올린 뒤에 거절하는 일이 없다.
_HARD_LIMIT = svc.MAX_FILE_BYTES + 1


@expense_scoped_router.get("/{expense_id}/attachments")
def list_attachments(
    expense_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    items = svc.list_attachments(db, org_id=user.org_id, expense_id=expense_id)
    return envelope(items, meta={
        "total": len(items),
        "max_attachments": svc.MAX_ATTACHMENTS_PER_EXPENSE,
        "max_file_bytes": svc.MAX_FILE_BYTES,
        # 화면이 "삭제" 와 "삭제 승인 요청" 중 무엇을 보여줄지 미리 안다.
        # 눌러 보고 403 을 맞은 뒤에 알면 늦다.
        "delete_requires_approval": svc.requires_delete_approval(
            db, org_id=user.org_id, expense_id=expense_id),
        # 스캐너가 없다. 화면이 "검사됨" 이라고 말하지 않도록 서버가 알려준다.
        "scanner_available": False,
    })


@expense_scoped_router.post("/{expense_id}/attachments", status_code=201)
async def upload_attachment(
    expense_id: UUID,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    # 스트리밍으로 읽으면서 상한을 넘기면 즉시 끊는다.
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 256):
        total += len(chunk)
        if total > _HARD_LIMIT:
            raise ValidationError(
                f"{file.filename} — 10MB를 넘습니다.",
                detail={"filename": file.filename, "reason": "TOO_LARGE",
                        "max_bytes": svc.MAX_FILE_BYTES},
            )
        chunks.append(chunk)

    out = svc.upload_attachment(
        db, org_id=user.org_id, actor=user.user_id, expense_id=expense_id,
        filename=file.filename or "attachment",
        content_type=file.content_type, data=b"".join(chunks),
    )
    return envelope(svc.get_attachment(
        db, org_id=user.org_id, attachment_id=out.attachment_id))


@attachment_router.post("/run-delete")
def run_delete(
    batch_size: int = Query(default=50, ge=1, le=500),
    all_orgs: bool = Query(default=False, description="전 조직 대상(운영 배치)"),
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    """`PENDING_DELETE` 를 실제로 지운다.

    cron 이 없어도 수동으로 돌릴 수 있어야 한다 — 승인은 됐는데 파일이 남아 있는
    상태를 사람이 해소할 방법이 있어야 하기 때문이다.
    """
    result = svc.run_delete_worker(
        db, org_id=None if all_orgs else user.org_id, batch_size=batch_size)
    return envelope(result.as_dict())


@attachment_router.get("/{attachment_id}/download")
def download(
    attachment_id: UUID,
    user: CurrentUser = Depends(_download_roles),
    db: Session = Depends(get_db),
) -> Response:
    """증빙 원본 내려받기.

    **영구 public URL 을 만들지 않는다.** 매 요청이 인증과 권한을 통과해야 하고,
    조직 경계는 조회 단계에서 강제된다(다른 조직 첨부는 404).
    """
    if not can_download_expense_attachment(user.role):
        raise ForbiddenError(
            "증빙 원본을 열 권한이 없습니다.",
            detail={"required_permission": "expense.view_attachment"},
        )

    stream, meta = svc.open_for_download(
        db, org_id=user.org_id, attachment_id=attachment_id)
    with stream:
        payload = stream.read()

    # Content-Disposition — 파일명에 따옴표·개행·비ASCII 가 들어와도 헤더가 깨지지
    # 않아야 한다. RFC 5987 `filename*` 로 UTF-8 을 싣고, 구형 클라이언트용
    # `filename` 은 ASCII 로만 남긴다.
    name = meta["filename"]
    ascii_fallback = name.encode("ascii", "replace").decode("ascii").replace('"', "_")
    disposition = (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(name, safe='')}"
    )
    return Response(
        content=payload,
        media_type=meta["mime_type"],
        headers={
            "Content-Disposition": disposition,
            # 증빙은 브라우저가 렌더링할 이유가 없다. 스니핑을 막으면
            # 이미지로 위장한 스크립트가 실행될 여지가 사라진다.
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@attachment_router.delete("/{attachment_id}", status_code=200)
def delete_attachment(
    attachment_id: UUID,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    """직접 삭제.

    승인 이력이 있는 비용이면 403(`APPROVAL_REQUIRED`) 이고, 잠긴 달이면
    409 다. **여기서도 파일을 즉시 지우지 않는다** — 상태만 `PENDING_DELETE` 로
    바꾸고 worker 가 지운다. 승인 경로와 삭제 절차가 갈라지면 한쪽만 고쳐지는 날이 온다.
    """
    svc.assert_deletable(db, org_id=user.org_id, attachment_id=attachment_id)
    return envelope(svc.mark_pending_delete(
        db, org_id=user.org_id, actor=user.user_id, attachment_id=attachment_id))


@attachment_router.post("/{attachment_id}/retry-delete")
def retry_delete(
    attachment_id: UUID,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(svc.retry_delete(
        db, org_id=user.org_id, actor=user.user_id, attachment_id=attachment_id))
