"""상품↔공급처 연결 대량 가져오기 API.

`inventory_import` 와 같은 모양이다 — 한 엔드포인트에 `mode` 를 넘기고,
기본은 `DRY_RUN` 이며, 파일은 저장하지 않는다. 가져오기마다 다른 규칙을
만들면 사용자가 매번 새로 배워야 한다.

권한이 `SOURCING` 을 포함하는 이유: 어느 공급처에서 사는가는 소싱 담당의
업무다. 재고 가져오기(`ADMIN`/`OPERATOR`)와 다른 일이다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

from app.api.v1.inventory_import import _MAX_BYTES, _rows_from_upload
from app.core.exceptions import ValidationError
from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.services import product_source_import as psi

router = APIRouter(prefix="/sourcing/import", tags=["sourcing"])

_manage = require_roles("ADMIN", "SOURCING")
_view = require_roles("ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING")


@router.post("")
async def run_import(
    file: UploadFile = File(...),
    mode: str = Form("DRY_RUN"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_manage),
) -> dict:
    """상품↔공급처 연결 분석·검토·반영."""
    if mode not in psi.MODES:
        raise ValidationError(f"알 수 없는 단계입니다: {mode}")

    raw = await file.read()
    if not raw:
        raise ValidationError("파일이 비어 있습니다.")
    if len(raw) > _MAX_BYTES:
        raise ValidationError("파일이 너무 큽니다 (최대 10MB).")

    rows = _rows_from_upload(file.filename or "upload", raw)
    plan = psi.plan(db, org_id=user.org_id, rows=rows, mode=mode)

    if mode != "APPLY":
        return envelope({**plan.as_dict(), "source_name": file.filename})

    return envelope(psi.apply_plan(
        db, org_id=user.org_id, actor_user_id=user.user_id, plan_result=plan,
        source_name=file.filename or "upload"))


@router.get("/completeness")
def completeness(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_view),
) -> dict:
    """소싱 정보가 얼마나 채워졌나 — 재주문이 왜 안 도는지의 답."""
    return envelope(psi.completeness(db, org_id=user.org_id))


@router.get("/template")
def template(user: CurrentUser = Depends(_view)) -> dict:
    """어떤 열이 필요한지.

    양식 파일을 내려주지 않는다. 사용자는 대개 자기 목록을 갖고 있고, 우리
    양식으로 옮겨 적게 하면 오히려 오류가 는다. 별칭을 넓게 받는 쪽이 낫다.
    """
    _ = user
    return envelope({
        "product_keys": list(psi.PRODUCT_KEYS),
        "supplier_keys": list(psi.SUPPLIER_KEYS),
        "columns": [
            {"field": "sku", "label": "SKU", "required": False,
             "note": "상품 열쇠 셋 중 하나는 필수입니다.",
             "aliases": ["SKU", "상품코드", "품번"]},
            {"field": "business_key", "label": "상품번호", "required": False,
             "note": "PROD-000000 형식", "aliases": ["상품번호"]},
            {"field": "supplier_code", "label": "공급처코드", "required": False,
             "note": "공급처 열쇠 셋 중 하나는 필수입니다.",
             "aliases": ["공급처코드"]},
            {"field": "supplier_domain", "label": "공급처도메인", "required": False,
             "note": "domeggook.com 처럼. 완전일치로만 찾습니다.",
             "aliases": ["공급처도메인", "공급처사이트"]},
            {"field": "source_url", "label": "공급처 상품 URL", "required": False,
             "note": "이 상품의 매입 페이지. 공급처 사이트 주소와 다릅니다.",
             "aliases": ["공급처URL", "매입URL"]},
            {"field": "lead_time_days", "label": "리드타임(일)", "required": False,
             "note": "없으면 비워 두세요. 재주문 계산에 필요합니다.",
             "aliases": ["리드타임"]},
            {"field": "moq", "label": "최소주문수량", "required": False,
             "aliases": ["MOQ", "최소수량"]},
            {"field": "is_primary", "label": "주 소싱처", "required": False,
             "note": "Y 로 표시. 상품당 하나만 가능합니다.",
             "aliases": ["주소싱처", "대표"]},
            {"field": "purchase_price", "label": "매입가", "required": False,
             "note": "새 원가 버전으로 쌓입니다. 상품 원가는 바뀌지 않습니다.",
             "aliases": ["매입가", "공급가"]},
        ],
        "notes": [
            "상품명·공급처명으로는 찾지 않습니다. 잘못 연결하면 틀린 공급처의 "
            "원가로 발주가 나갑니다.",
            "리드타임·MOQ 가 없으면 비워 두세요. 임의값을 넣지 않습니다.",
            "매입가는 소싱 원가 버전으로만 쌓이고, 상품에 적용된 원가는 "
            "승인을 거쳐야 바뀝니다.",
            "이미 있는 연결은 **비어 있는 항목만** 채웁니다. 사람이 고친 값을 "
            "파일이 덮지 않습니다.",
            "ID·비밀번호 열은 읽지 않고 버립니다.",
        ],
    })
