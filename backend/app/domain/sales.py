"""판매 주문 도메인 schemas (docs/22_api_spec.md §5).

⚠️ `sale_price` 는 **라인 합계**다 (엑셀 `판매가+배송비`). 단가가 아니다.
   매출 = SUM(sale_price) 이며 quantity 를 곱하지 않는다. 반대로 원가
   (`product.cost_amount`)는 단가이므로 quantity 를 곱한다. 상세 근거는
   마이그레이션 M011 docstring 참조.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SaleOrderCreate(BaseModel):
    sale_date: str  # "YYYY-MM-DD"
    marketplace: str  # sale_channel.code 참조 (M007) — GET /sale-channels 로 유효 코드 목록 조회
    marketplace_order_no: str = Field(min_length=1, max_length=200)
    product_id: UUID | None = None
    product_snapshot_name: str | None = None
    quantity: int = Field(ge=1)
    sale_price: int = Field(ge=0)
    ad_cost: int = Field(default=0, ge=0)
    shipping_fee: int = Field(default=0, ge=0)
    vat_inclusion_type: str = Field(default="INCLUDED")
    memo: str | None = None
    # 어느 판매자 계정에서 온 주문인가 (M032).
    #
    # **`marketplace` 와 같은 것이 아니다.** 쿠팡 본점과 쿠팡 2호점은 같은
    # `marketplace='COUPANG'` 이지만 다른 계정이다. 계정을 아는 쪽(ingest 어댑터
    # 또는 등록하는 사람)이 명시적으로 넘긴다 — 서버가 채널 문자열로 추론하지 않는다.
    #
    # 선택값이다: 아직 연동 계정을 만들지 않은 조직도 수동으로 주문을 넣는다.
    integration_account_id: UUID | None = None


class SaleOrderUpdate(BaseModel):
    sale_date: str | None = None
    marketplace: str | None = None
    marketplace_order_no: str | None = None
    product_id: UUID | None = None
    product_snapshot_name: str | None = None
    quantity: int | None = Field(default=None, ge=1)
    sale_price: int | None = Field(default=None, ge=0)
    ad_cost: int | None = Field(default=None, ge=0)
    shipping_fee: int | None = Field(default=None, ge=0)
    vat_inclusion_type: str | None = None
    memo: str | None = None
    integration_account_id: UUID | None = None


class SaleOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    business_key: str
    sale_date: str
    marketplace: str
    marketplace_order_no: str
    # 같은 주문번호에 여러 라인이 있을 때의 라인 번호 (M012). 단일 상품 주문이면 1.
    # 서버가 자동 채번하므로 Create/Update 입력에는 없다.
    line_no: int
    product_id: UUID | None
    product_snapshot_name: str | None
    quantity: int
    sale_price: int
    ad_cost: int
    shipping_fee: int
    vat_inclusion_type: str
    memo: str | None
    import_source: str
    created_at: datetime
    # 유입 계정. **NULL 은 "모른다"** 이지 "계정 없음" 이 아니다 —
    # legacy 주문(M032 이전)과 계정을 지정하지 않은 수동 등록이 여기 해당한다.
    integration_account_id: UUID | None = None
    # 표시용 이름. `expenses` 의 `category_name`·`supplier_name` 과 같은 평면 관례다.
    # 계정이 없으면 NULL — 화면이 채널 이름으로 대신 채우지 않는다.
    integration_account_label: str | None = None
