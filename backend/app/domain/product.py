"""상품 / 옵션 / 변형 (D-286) 도메인 schemas.

D-286 옵션 정규화: option_group / option_value / product_variant 3테이블.
- product.has_variants=True 인 상품만 variant 보유.
- variant.option_values_json 은 검색·표시용 캐시 (정합성 확보는 join 결과 기준).
- variant 삭제 전 sales/settlement 참조 확인 → VariantInUseError.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProductCreate(BaseModel):
    business_key: str | None = Field(default=None, max_length=80)  # 미지정 시 서버 자동 발급(PROD-NNNNNN)
    sku: str | None = Field(default=None, max_length=80)
    name_ko: str = Field(min_length=1, max_length=200)
    name_en: str | None = Field(default=None, max_length=200)
    category_path: str | None = Field(default=None, max_length=300)
    margin_type: str = Field(pattern=r"^(PURCHASE_AGENCY|ROCKET_GROWTH|OVERSEAS_DIRECT|GENERAL_RESELL)$")
    status: str = Field(default="CANDIDATE")
    supplier_id: UUID | None = None
    supply_url: str | None = Field(default=None, max_length=1000)
    cost_amount: int | None = Field(default=None, ge=0)
    cost_currency: str | None = Field(default=None, max_length=8)
    cost_fx_rate: Decimal | None = None
    cost_amount_foreign: int | None = Field(default=None, ge=0)  # 현지통화 공급가 — KRW 환산은 서버가 계산
    has_variants: bool = False
    memo: str | None = None


class ProductUpdate(BaseModel):
    sku: str | None = Field(default=None, max_length=80)
    name_ko: str | None = Field(default=None, min_length=1, max_length=200)
    name_en: str | None = Field(default=None, max_length=200)
    category_path: str | None = Field(default=None, max_length=300)
    margin_type: str | None = Field(
        default=None, pattern=r"^(PURCHASE_AGENCY|ROCKET_GROWTH|OVERSEAS_DIRECT|GENERAL_RESELL)$"
    )
    status: str | None = None
    supplier_id: UUID | None = None
    supply_url: str | None = Field(default=None, max_length=1000)
    cost_amount: int | None = Field(default=None, ge=0)
    cost_currency: str | None = Field(default=None, max_length=8)
    cost_fx_rate: Decimal | None = None
    cost_amount_foreign: int | None = Field(default=None, ge=0)
    has_variants: bool | None = None
    memo: str | None = None
    # 낙관적 동시성 토큰. 상세 조회에서 받은 값을 그대로 돌려보낸다.
    # product 에 version 컬럼이 없어 updated_at 을 토큰으로 쓴다 — 마이그레이션 없이
    # 같은 보호를 얻는다. 값을 안 보내면 검사하지 않는다(기존 호출자 호환).
    expected_version: str | None = None


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    business_key: str
    sku: str | None
    name_ko: str
    name_en: str | None
    category_path: str | None
    margin_type: str
    status: str
    supplier_id: UUID | None
    supply_url: str | None
    cost_amount: int | None
    cost_currency: str | None
    cost_fx_rate: Decimal | None
    cost_amount_foreign: int | None
    has_variants: bool
    memo: str | None
    created_at: datetime
    updated_at: datetime | None = None

    @property
    def version_token(self) -> str | None:
        """동시성 토큰. `updated_at` 의 ISO 표현.

        상세 응답에 실어 보내고, 수정 요청이 그대로 되돌려준다.
        그 사이 누가 저장했으면 `updated_at` 이 달라져 409 가 난다.
        """
        return self.updated_at.isoformat() if self.updated_at else None


# ---------------------------------------------------------------------------
# Option group / value
# ---------------------------------------------------------------------------


class OptionGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    sort_order: int = 0


class OptionGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    sort_order: int | None = None


class OptionGroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID
    name: str
    sort_order: int


class OptionValueCreate(BaseModel):
    value: str = Field(min_length=1, max_length=80)
    sort_order: int = 0


class OptionValueUpdate(BaseModel):
    value: str | None = Field(default=None, min_length=1, max_length=80)
    sort_order: int | None = None


class OptionValueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    option_group_id: UUID
    value: str
    sort_order: int


# ---------------------------------------------------------------------------
# Variant
# ---------------------------------------------------------------------------


class VariantCreate(BaseModel):
    variant_key: str = Field(min_length=1, max_length=120)
    option_values_json: dict[str, Any]  # e.g. {"색상": "BLACK", "크기": "L"}
    sku_external: str | None = Field(default=None, max_length=120)
    cost_amount: int | None = Field(default=None, ge=0)
    sale_price: int | None = Field(default=None, ge=0)
    extra_price: int = Field(default=0, ge=0)
    stock_quantity: int | None = Field(default=None, ge=0)
    is_active: bool = True


class VariantUpdate(BaseModel):
    variant_key: str | None = Field(default=None, min_length=1, max_length=120)
    option_values_json: dict[str, Any] | None = None
    sku_external: str | None = Field(default=None, max_length=120)
    cost_amount: int | None = Field(default=None, ge=0)
    sale_price: int | None = Field(default=None, ge=0)
    extra_price: int | None = Field(default=None, ge=0)
    stock_quantity: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class VariantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID
    variant_key: str
    option_values_json: dict[str, Any]
    sku_external: str | None
    cost_amount: int | None
    sale_price: int | None
    extra_price: int
    stock_quantity: int | None
    is_active: bool
