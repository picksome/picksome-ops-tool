"""연동(integration_account) + 봇 ingest API schemas (M009, docs/27 3-3)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class IntegrationAccountCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=100)  # 예: 'BOT_GENERIC', 'COUPANG_WING_API'
    account_label: str = Field(min_length=1, max_length=200)
    sale_channel_code: str | None = None  # sale_channel.code — 이 연동이 어느 판매채널 것인지


class IntegrationAccountOut(BaseModel):
    id: UUID
    provider: str
    account_label: str
    api_key: str  # 발급 시 1회만 노출 — DB 에는 해시만 저장, 분실 시 재발급 필요


class IntegrationAccountListItem(BaseModel):
    id: UUID
    provider: str
    account_label: str
    auth_kind: str
    status: str
    last_synced_at: datetime | None
    sale_channel_code: str | None


class SaleChannelCodeOut(BaseModel):
    code: str
    display_name: str


class IngestOrderItem(BaseModel):
    sale_date: str  # "YYYY-MM-DD"
    marketplace: str  # sale_channel.code
    marketplace_order_no: str
    sku: str | None = None  # product.sku 매칭 우선
    product_business_key: str | None = None  # sku 매칭 실패 시 대안
    product_snapshot_name: str | None = None
    quantity: int = Field(ge=1)
    sale_price: int = Field(ge=0)
    ad_cost: int = Field(default=0, ge=0)
    shipping_fee: int = Field(default=0, ge=0)
    memo: str | None = None


class IngestOrdersRequest(BaseModel):
    items: list[IngestOrderItem] = Field(min_length=1, max_length=500)


class IngestResultOut(BaseModel):
    batch_run_id: UUID
    status: str
    success_count: int
    failed_count: int
