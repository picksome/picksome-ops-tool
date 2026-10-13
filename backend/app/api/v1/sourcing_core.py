"""Sourcing Core 라우터 — 공급처 · 소싱처 · 원가 · 환율 · 권장가.

권한
----
```
조회        ADMIN / SOURCING / OPERATOR / ACCOUNTING / READONLY
공급처 관리  ADMIN / SOURCING
소싱처 관리  ADMIN / SOURCING
원가 등록    ADMIN / SOURCING
환율 등록    ADMIN / SOURCING / ACCOUNTING
```

**원가 조회는 왜 넓은가**
소싱 원가는 마진의 근거라 운영·정산도 봐야 한다. 다만 `READONLY` 와
`EXTERNAL_ACCOUNTANT` 에게는 금액을 가린다(`sensitive` 참고) — 외부 세무 대리인이
공급처 매입가를 알아야 할 이유가 없다.

**원가를 상품에 적용하는 것은 여기서 하지 않는다.**
`product.cost_amount` 변경은 기존 `COST_CHANGE` 승인을 거친다. 이 라우터에
적용 endpoint 를 두면 승인 우회 경로가 하나 더 생긴다.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.services import sourcing_core as sc
from app.services import sourcing_recommendation as rec

# **`/sourcing` 을 쓰지 않는다.** 기존 소싱 후보 라우터가 `/sourcing/{item_id}` 를
# 갖고 있어서 `/sourcing/suppliers` 가 그쪽에 먼저 잡힌다(FastAPI 는 먼저 등록된
# 경로가 이긴다). item_id="suppliers" 로 UUID 파싱에 실패해 422 가 난다.
router = APIRouter(tags=["sourcing"])
product_router = APIRouter(prefix="/products", tags=["sourcing"])

# 소싱 메타데이터는 넓게 본다 — 어디서 사 오는지는 운영 업무에 필요하다.
_view = require_roles("ADMIN", "SOURCING", "OPERATOR", "ACCOUNTING", "READONLY")
# 공급처·소싱처를 만들고 고치는 것은 소싱 담당의 일이다.
_manage = require_roles("ADMIN", "SOURCING")
# 환율은 정산 담당도 다룬다 — 결제·회계 시점 환율이 그쪽 업무다.
_manage_fx = require_roles("ADMIN", "SOURCING", "ACCOUNTING")


def _hide_cost(role: str) -> bool:
    """공급처 매입가를 가릴 역할인가.

    프론트에서 가리는 것으로 끝내지 않는다 — API 를 직접 부르면 뚫린다.
    """
    from app.core.sensitive import can_view_source_cost

    return not can_view_source_cost(role)


# ──────────────────────────────────────────────────────────────
# 공급처
# ──────────────────────────────────────────────────────────────

class SupplierIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    supplier_type: str = "WHOLESALE"
    country_code: str | None = Field(None, max_length=2)
    website_url: str | None = Field(None, max_length=1000)
    default_currency: str = Field("KRW", max_length=3)
    rating: int | None = Field(None, ge=1, le=5)
    memo: str | None = Field(None, max_length=1000)
    external_partner_id: UUID | None = None
    credential_present: bool = False


class SupplierPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    supplier_type: str | None = None
    country_code: str | None = Field(None, max_length=2)
    website_url: str | None = Field(None, max_length=1000)
    default_currency: str | None = Field(None, max_length=3)
    status: str | None = None
    rating: int | None = Field(None, ge=1, le=5)
    memo: str | None = Field(None, max_length=1000)
    credential_present: bool | None = None


class ArchiveIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=300)


@router.get("/supplier-sites")
def list_suppliers(
    q: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    res = sc.list_suppliers(
        db, org_id=user.org_id, q=q, status=status, limit=limit, offset=offset,
    )
    return envelope(res["items"], {"total": res["total"], "limit": limit, "offset": offset})


@router.post("/supplier-sites", status_code=201)
def create_supplier(
    payload: SupplierIn,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(sc.create_supplier(
        db, org_id=user.org_id, actor_user_id=user.user_id, **payload.model_dump(),
    ))


@router.get("/supplier-sites/{supplier_id}")
def get_supplier(
    supplier_id: UUID,
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(sc.get_supplier(db, org_id=user.org_id, supplier_id=supplier_id))


@router.patch("/supplier-sites/{supplier_id}")
def update_supplier(
    supplier_id: UUID,
    payload: SupplierPatch,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(sc.update_supplier(
        db, org_id=user.org_id, actor_user_id=user.user_id, supplier_id=supplier_id,
        **payload.model_dump(exclude_unset=True),
    ))


@router.delete("/supplier-sites/{supplier_id}")
def archive_supplier(
    supplier_id: UUID,
    payload: ArchiveIn,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> dict:
    """행을 지우지 않고 정리 표시. 과거 소싱 기록이 어느 공급처였는지 남아야 한다."""
    sc.archive_supplier(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        supplier_id=supplier_id, reason=payload.reason,
    )
    return envelope({"archived": True})


# ──────────────────────────────────────────────────────────────
# 상품 소싱처
# ──────────────────────────────────────────────────────────────

class SourceIn(BaseModel):
    supplier_site_id: UUID
    source_url: str | None = Field(None, max_length=1000)
    external_source_product_id: str | None = Field(None, max_length=200)
    currency: str = Field("KRW", max_length=3)
    moq: int | None = Field(None, gt=0)
    lead_time_days: int | None = Field(None, ge=0)
    weight_g: int | None = Field(None, ge=0)
    is_primary: bool = False
    memo: str | None = Field(None, max_length=1000)


class SourcePatch(BaseModel):
    source_url: str | None = Field(None, max_length=1000)
    external_source_product_id: str | None = Field(None, max_length=200)
    currency: str | None = Field(None, max_length=3)
    moq: int | None = Field(None, gt=0)
    lead_time_days: int | None = Field(None, ge=0)
    weight_g: int | None = Field(None, ge=0)
    status: str | None = None
    memo: str | None = Field(None, max_length=1000)


@product_router.get("/{product_id}/sources")
def list_sources(
    product_id: UUID,
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    rows = sc.list_product_sources(db, org_id=user.org_id, product_id=product_id)
    return envelope(rows, {"total": len(rows)})


@product_router.get("/{product_id}/source-comparison")
def source_comparison(
    product_id: UUID,
    at: date | None = Query(None),
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    """대안 소싱처 비교표.

    **순위를 매기지 않는다.** 최저가를 추천으로 만들면 MOQ·리드타임·품질처럼
    숫자에 안 담기는 것들이 사라진다. 값만 나란히 준다.
    """
    if _hide_cost(user.role):
        from app.core.exceptions import ForbiddenError

        raise ForbiddenError("소싱 원가를 볼 권한이 없습니다.")
    return envelope(rec.compare_sources(
        db, org_id=user.org_id, product_id=product_id, at=at,
    ))


@product_router.post("/{product_id}/sources", status_code=201)
def add_source(
    product_id: UUID,
    payload: SourceIn,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(sc.add_product_source(
        db, org_id=user.org_id, actor_user_id=user.user_id, product_id=product_id,
        **payload.model_dump(),
    ))


@router.patch("/product-sources/{source_id}")
def update_source(
    source_id: UUID,
    payload: SourcePatch,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(sc.update_product_source(
        db, org_id=user.org_id, actor_user_id=user.user_id, source_id=source_id,
        **payload.model_dump(exclude_unset=True),
    ))


@router.post("/product-sources/{source_id}/primary")
def set_primary(
    source_id: UUID,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> dict:
    """주 소싱처 지정.

    별도 endpoint 인 이유: PATCH 로 `is_primary=true` 를 받으면 "내리고 올리는"
    두 단계를 클라이언트가 순서대로 보내야 하고, 중간에 끊기면 primary 가 없는
    상태로 남는다. 한 번의 호출이 한 트랜잭션이어야 한다.
    """
    return envelope(sc.set_primary_source(
        db, org_id=user.org_id, actor_user_id=user.user_id, source_id=source_id,
    ))


@router.delete("/product-sources/{source_id}")
def archive_source(
    source_id: UUID,
    payload: ArchiveIn,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> dict:
    sc.archive_product_source(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        source_id=source_id, reason=payload.reason,
    )
    return envelope({"archived": True})


# ──────────────────────────────────────────────────────────────
# 원가 version
# ──────────────────────────────────────────────────────────────

class CostVersionIn(BaseModel):
    purchase_price: int = Field(..., ge=0)
    currency: str = Field(..., max_length=3)
    valid_from: date
    domestic_shipping: int | None = Field(None, ge=0)
    international_shipping: int | None = Field(None, ge=0)
    customs_duty: int | None = Field(None, ge=0)
    import_vat: int | None = Field(None, ge=0)
    packaging_cost: int | None = Field(None, ge=0)
    other_cost: int | None = Field(None, ge=0)
    memo: str | None = Field(None, max_length=500)


@router.get("/product-sources/{source_id}/cost-versions")
def cost_versions(
    source_id: UUID,
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    """원가 이력. **수정 endpoint 는 없다** — 이력은 append 만 한다."""
    rows = sc.cost_history(db, org_id=user.org_id, product_source_id=source_id)
    if _hide_cost(user.role):
        rows = [_redact_cost(r) for r in rows]
    return envelope(rows, {"total": len(rows), "redacted": _hide_cost(user.role)})


@router.post("/product-sources/{source_id}/cost-versions", status_code=201)
def add_cost_version(
    source_id: UUID,
    payload: CostVersionIn,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> dict:
    """새 원가. 기존 version 을 덮어쓰지 않고 앞 기간을 닫는다.

    **`product.cost_amount` 는 바뀌지 않는다.** 상품에 적용하려면 `COST_CHANGE`
    승인을 거친다 — 소싱에서 관측한 사실과 계산에 쓰는 값은 다른 것이다.
    """
    body = payload.model_dump()
    return envelope(sc.add_cost_version(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        product_source_id=source_id,
        purchase_price=body.pop("purchase_price"), currency=body.pop("currency"),
        valid_from=body.pop("valid_from"), memo=body.pop("memo"),
        **body,
    ))


def _redact_cost(row: dict) -> dict:
    """금액을 지운다. 키를 남겨 화면이 "가려짐" 을 표시할 수 있게 한다."""
    hidden = {"purchase_price", "domestic_shipping", "international_shipping",
              "customs_duty", "import_vat", "packaging_cost", "other_cost"}
    return {k: (None if k in hidden else v) for k, v in row.items()} | {"redacted": True}


# ──────────────────────────────────────────────────────────────
# 환율
# ──────────────────────────────────────────────────────────────

class FxIn(BaseModel):
    base_currency: str = Field(..., min_length=3, max_length=3)
    quote_currency: str = Field("KRW", min_length=3, max_length=3)
    rate: Decimal = Field(..., gt=0)
    rate_type: str = "SOURCING"
    observed_at: datetime | None = None
    source_name: str | None = Field(None, max_length=200)


@router.get("/fx-rates")
def list_fx(
    base_currency: str | None = Query(None),
    quote_currency: str = Query("KRW"),
    rate_type: str = Query("SOURCING"),
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    rows = sc.list_fx_rates(
        db, org_id=user.org_id, base_currency=base_currency,
        quote_currency=quote_currency, rate_type=rate_type,
    )
    return envelope(rows, {"total": len(rows)})


@router.post("/fx-rates", status_code=201)
def create_fx(
    payload: FxIn,
    user: CurrentUser = Depends(_manage_fx),
    db: Session = Depends(get_db),
) -> dict:
    """수동 환율. 외부 환율 API 가 없어도 소싱 계산이 돌아가야 한다.

    `source_type` 은 `MANUAL` 로 고정한다 — 사람이 넣은 값을 API 수신값처럼
    보이게 하지 않는다.
    """
    out = sc.record_fx_rate(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        source_type="MANUAL", **payload.model_dump(),
    )
    out["rate"] = str(out["rate"])
    return envelope(out)


# ──────────────────────────────────────────────────────────────
# 예상 원가 · 권장가 · 민감도
# ──────────────────────────────────────────────────────────────

@router.get("/product-sources/{source_id}/landed-cost")
def landed_cost(
    source_id: UUID,
    at: date | None = Query(None),
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    """예상 landed cost + 현재 상품 원가와의 차이.

    차이만 사실대로 준다. "몇 % 이상이면 위험" 같은 임계값은 만들지 않는다 —
    업무 판단이고 근거가 없다.
    """
    if _hide_cost(user.role):
        from app.core.exceptions import ForbiddenError

        raise ForbiddenError("소싱 원가를 볼 권한이 없습니다.")
    return envelope(rec.compare_to_product_cost(
        db, org_id=user.org_id, product_source_id=source_id, at=at,
    ))


@router.get("/product-sources/{source_id}/recommendation")
def recommendation(
    source_id: UUID,
    marketplace: str = Query(...),
    at: date | None = Query(None),
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    """권장 판매가. 기존 마진 엔진이 계산한다 — 새 산식을 만들지 않았다."""
    if _hide_cost(user.role):
        from app.core.exceptions import ForbiddenError

        raise ForbiddenError("소싱 원가를 볼 권한이 없습니다.")
    return envelope(rec.recommend_prices(
        db, org_id=user.org_id, product_source_id=source_id,
        marketplace=marketplace, at=at,
    ))


@router.get("/product-sources/{source_id}/sensitivity")
def sensitivity(
    source_id: UUID,
    marketplace: str = Query(...),
    sale_price: int = Query(..., ge=0),
    at: date | None = Query(None),
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    """환율·원가가 오르면 이 판매가에서 마진이 얼마나 줄어드나.

    **시나리오다.** 환율 snapshot 도 원가 version 도 만들지 않는다.
    """
    if _hide_cost(user.role):
        from app.core.exceptions import ForbiddenError

        raise ForbiddenError("소싱 원가를 볼 권한이 없습니다.")
    return envelope({
        "fx": rec.fx_sensitivity(
            db, org_id=user.org_id, product_source_id=source_id,
            marketplace=marketplace, sale_price=sale_price, at=at,
        ),
        "cost": rec.cost_sensitivity(
            db, org_id=user.org_id, product_source_id=source_id,
            marketplace=marketplace, sale_price=sale_price, at=at,
        ),
    })
