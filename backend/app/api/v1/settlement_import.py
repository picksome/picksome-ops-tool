"""정산서 가져오기 (M056 · docs/82).

한 엔드포인트에 단계를 넘긴다
--------------------------
`inventory/import` 와 같은 모양이다. `/dry-run` 과 `/commit` 을 나누지 않고
`mode` 를 받는다 — 두 단계가 **같은 파일에 대해** 돌아야 하고, 경로를 나누면
미리보기는 A 파일로 하고 반영은 B 파일로 하는 일이 생긴다.

기본값은 `DRY_RUN` 이다. `COMMIT` 은 명시적으로 보내야 한다.

판매계정을 화면이 고른다
--------------------
파일 내용만 보고 쿠팡 계정을 자동 선택하지 않는다. 조직에 쿠팡 계정이
하나뿐이어도 마찬가지다 — 두 번째 스토어가 생기는 날 조용히 틀린 계정에
붙는다. 어느 스토어의 정산서인지는 사람이 안다.

업로드를 저장하지 않는다
--------------------
정산서에는 구매자명이 들어 있다. 메모리에서 읽고 버린다 — 해시와 행 지문만
남는다. 저장하면 지워야 할 것이 하나 더 생긴다.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.services import settlement_import as si

router = APIRouter(prefix="/settlements/import", tags=["marketplace-settlements"])

#: 정산 자료는 손익 귀속의 입력이다. 비용·정산과 같은 권한으로 묶는다.
_manage = require_roles("ADMIN", "OPERATOR", "ACCOUNTING")
_view = require_roles("ADMIN", "OPERATOR", "ACCOUNTING", "READONLY")

#: 한 달치 정산서는 수십~수백 KB 다. 10MB 를 넘으면 잘못된 파일이다.
_MAX_BYTES = 10 * 1024 * 1024


@router.post("")
async def run_import(
    file: UploadFile = File(...),
    integration_account_id: UUID = Form(...),
    # 기본이 DRY_RUN 이다. 실수로 반영되는 경로를 만들지 않는다.
    mode: str = Form(si.DRY_RUN),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_manage),
) -> dict:
    """쿠팡 정산서 미리보기·반영.

    ```
    DRY_RUN   무엇이 들어갈지 계산한다 (쓰지 않는다)
    COMMIT    정산 행을 적재하고 주문에 붙인다
    ```

    같은 파일을 다시 보내도 행이 두 번 생기지 않는다 — 멱등성은 파일 해시가
    아니라 행 식별자가 지킨다.
    """
    raw = await file.read()
    if len(raw) > _MAX_BYTES:
        raise ValidationError("파일이 너무 큽니다 (최대 10MB).")

    summary = si.import_bytes(
        db, org_id=user.org_id, integration_account_id=integration_account_id,
        source_name=file.filename or "upload", raw=raw, mode=mode,
        actor_user_id=user.user_id,
    )
    if mode == si.COMMIT:
        db.commit()
    else:
        # 미리보기가 세션에 남긴 것이 없도록 되돌린다. 서비스가 쓰지
        # 않지만, 여기서 명시적으로 끊어야 다음 요청에 새지 않는다.
        db.rollback()
    return envelope(summary.as_dict())


@router.get("/runs")
def list_runs(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_view),
) -> dict:
    """지난 가져오기 이력. 무엇을 언제 넣었는지 되짚는 화면이다."""
    return envelope(si.list_runs(db, org_id=user.org_id, limit=limit))
