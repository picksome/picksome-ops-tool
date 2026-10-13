"""상품 / 옵션 / 변형 라우터 (D-286, docs/22_api_spec.md §10).

3-테이블 정규화 구조:
  product            — 상품 마스터 (margin_type/marketplace 메타)
   └ option_group    — 옵션 그룹 (예: "색상", "사이즈")
      └ option_value — 옵션 값 (예: "빨강", "파랑")
  product_variant    — SKU 단위 변형 (product × 옵션 조합)

endpoints (product):
  GET    /products                          — 목록 (status/margin_type/q 필터)
  GET    /products/{id}                     — 상세
  POST   /products                          — 등록
  PATCH  /products/{id}                     — 부분 수정
  DELETE /products/{id}                     — soft delete

endpoints (option_group):
  GET    /products/{pid}/option-groups
  POST   /products/{pid}/option-groups
  PATCH  /products/option-groups/{gid}
  DELETE /products/option-groups/{gid}

endpoints (option_value):
  GET    /products/option-groups/{gid}/values
  POST   /products/option-groups/{gid}/values
  PATCH  /products/option-values/{vid}
  DELETE /products/option-values/{vid}      — sales_order 사용 중 시 409 VARIANT_IN_USE

endpoints (variant):
  GET    /products/{pid}/variants
  GET    /products/variants/{vid}
  POST   /products/{pid}/variants
  PATCH  /products/variants/{vid}
  DELETE /products/variants/{vid}           — sales_order 사용 중 시 409 VARIANT_IN_USE

권한:
  - 쓰기(_write): ADMIN/OPERATOR/SOURCING
  - 읽기(_read): + ACCOUNTING/READONLY"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.domain.product import (
    OptionGroupCreate,
    OptionGroupUpdate,
    OptionValueCreate,
    OptionValueUpdate,
    ProductCreate,
    ProductUpdate,
    VariantCreate,
    VariantUpdate,
)
from app.services.approval import list_open_approvals_for_target
from app.services.product import (
    create_option_group,
    create_option_value,
    create_product,
    create_variant,
    delete_option_group,
    delete_option_value,
    delete_product,
    delete_variant,
    get_product,
    get_variant,
    list_option_groups,
    list_option_values,
    list_products,
    list_variants,
    update_option_group,
    update_option_value,
    update_product,
    update_variant,
)

router = APIRouter(prefix="/products", tags=["products"])

_read = require_roles("ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY")
_write = require_roles("ADMIN", "OPERATOR", "SOURCING")


@router.get("")
def list_(
    status: str | None = Query(default=None),
    margin_type: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    items, total = list_products(
        db, org_id=user.org_id, status=status, margin_type=margin_type, q=q,
        limit=limit, offset=offset,
    )
    return envelope(
        [i.model_dump() for i in items],
        meta={"limit": limit, "offset": offset, "total": total},
    )


@router.get("/{product_id}")
def detail(
    product_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    out = get_product(db, org_id=user.org_id, product_id=product_id)
    payload = out.model_dump()
    # 프론트가 저장 시 되돌려줄 동시성 토큰.
    payload["version_token"] = out.version_token
    # 진행 중 승인 요청 요약 — drawer 가 "이미 요청이 있다" 를 즉시 알 수 있어야
    # 사용자가 승인 요청을 눌렀다가 409 를 맞는 일이 없다.
    payload["open_approvals"] = list_open_approvals_for_target(
        db, org_id=user.org_id, target_type="product", target_id=product_id
    )
    return envelope(payload)


@router.post("", status_code=201)
def create(
    body: ProductCreate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(create_product(db, org_id=user.org_id, actor=user.user_id, body=body).model_dump())


@router.patch("/{product_id}")
def update(
    product_id: UUID,
    body: ProductUpdate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = update_product(db, org_id=user.org_id, actor=user.user_id, product_id=product_id, body=body)
    return envelope(out.model_dump())


@router.delete("/{product_id}", status_code=204, response_model=None)
def delete(
    product_id: UUID,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> None:
    delete_product(db, org_id=user.org_id, actor=user.user_id, product_id=product_id)


# ---------------------------------------------------------------------------
# Option groups (per product)
# ---------------------------------------------------------------------------


@router.get("/{product_id}/option-groups")
def og_list(
    product_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    items = list_option_groups(db, org_id=user.org_id, product_id=product_id)
    return envelope([i.model_dump() for i in items])


@router.post("/{product_id}/option-groups", status_code=201)
def og_create(
    product_id: UUID,
    body: OptionGroupCreate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = create_option_group(
        db, org_id=user.org_id, actor=user.user_id, product_id=product_id, body=body,
    )
    return envelope(out.model_dump())


@router.patch("/option-groups/{group_id}")
def og_update(
    group_id: UUID,
    body: OptionGroupUpdate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = update_option_group(db, org_id=user.org_id, actor=user.user_id, group_id=group_id, body=body)
    return envelope(out.model_dump())


@router.delete("/option-groups/{group_id}", status_code=204, response_model=None)
def og_delete(
    group_id: UUID,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> None:
    delete_option_group(db, org_id=user.org_id, actor=user.user_id, group_id=group_id)


# ---------------------------------------------------------------------------
# Option values (per group)
# ---------------------------------------------------------------------------


@router.get("/option-groups/{group_id}/values")
def ov_list(
    group_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    items = list_option_values(db, org_id=user.org_id, group_id=group_id)
    return envelope([i.model_dump() for i in items])


@router.post("/option-groups/{group_id}/values", status_code=201)
def ov_create(
    group_id: UUID,
    body: OptionValueCreate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = create_option_value(
        db, org_id=user.org_id, actor=user.user_id, group_id=group_id, body=body,
    )
    return envelope(out.model_dump())


@router.patch("/option-values/{value_id}")
def ov_update(
    value_id: UUID,
    body: OptionValueUpdate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = update_option_value(db, org_id=user.org_id, actor=user.user_id, value_id=value_id, body=body)
    return envelope(out.model_dump())


@router.delete("/option-values/{value_id}", status_code=204, response_model=None)
def ov_delete(
    value_id: UUID,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> None:
    delete_option_value(db, org_id=user.org_id, actor=user.user_id, value_id=value_id)


# ---------------------------------------------------------------------------
# Variants (per product)
# ---------------------------------------------------------------------------


@router.get("/{product_id}/variants")
def v_list(
    product_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    items = list_variants(db, org_id=user.org_id, product_id=product_id)
    return envelope([i.model_dump() for i in items])


@router.post("/{product_id}/variants", status_code=201)
def v_create(
    product_id: UUID,
    body: VariantCreate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = create_variant(db, org_id=user.org_id, actor=user.user_id, product_id=product_id, body=body)
    return envelope(out.model_dump())


@router.get("/variants/{variant_id}")
def v_detail(
    variant_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(get_variant(db, org_id=user.org_id, variant_id=variant_id).model_dump())


@router.patch("/variants/{variant_id}")
def v_update(
    variant_id: UUID,
    body: VariantUpdate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = update_variant(db, org_id=user.org_id, actor=user.user_id, variant_id=variant_id, body=body)
    return envelope(out.model_dump())


@router.delete("/variants/{variant_id}", status_code=204, response_model=None)
def v_delete(
    variant_id: UUID,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> None:
    delete_variant(db, org_id=user.org_id, actor=user.user_id, variant_id=variant_id)
