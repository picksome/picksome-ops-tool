"""판매처별 listing / 가격 도메인 모델 (M022).

가격 상태를 두 축으로 나눠 쓴다 — **합치지 않는다.**

    내부 적용 상태 : 우리 DB 에서 이 가격이 유효한가        (ACTIVE / SUPERSEDED / CANCELLED)
    외부 반영 상태 : 판매처에 실제로 전송됐는가             (publish_status)

하나로 합치면 "승인은 났는데 판매처 반영이 실패한" 상태를 표현할 수 없다.
그 상태는 실제로 자주 발생하고, 사용자가 가장 알아야 하는 상태다.

승인 상태는 여기에 **없다.** `approval_request` 가 승인 상태의 단일 진실이고,
가격 레코드는 "적용된 결과" 만 담는다. 두 곳에 두면 언젠가 어긋난다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ── 상태값. DB CHECK 와 **반드시** 일치해야 한다 (M022) ──
LISTING_STATUSES = ("DRAFT", "ACTIVE", "PAUSED", "ENDED")
PRICE_STATUSES = ("ACTIVE", "SUPERSEDED", "CANCELLED")
PUBLISH_STATUSES = (
    "NOT_REQUESTED", "QUEUED", "PUBLISHING",
    "PUBLISHED", "FAILED", "VERIFICATION_FAILED",
)


class ChannelListingCreate(BaseModel):
    integration_account_id: UUID
    external_product_id: str | None = Field(default=None, max_length=200)
    external_product_url: str | None = Field(default=None, max_length=1000)
    status: str = "DRAFT"
    auto_publish_price: bool = False


class ChannelListingUpdate(BaseModel):
    external_product_id: str | None = Field(default=None, max_length=200)
    external_product_url: str | None = Field(default=None, max_length=1000)
    status: str | None = None
    auto_publish_price: bool | None = None
    # 상품과 같은 방식의 낙관적 동시성 토큰.
    expected_version: str | None = None


class ChannelListingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    integration_account_id: UUID
    product_id: UUID
    external_product_id: str | None
    external_product_url: str | None
    status: str
    auto_publish_price: bool
    created_at: datetime
    updated_at: datetime | None = None

    # 조인해서 채우는 표시용 값 — 화면이 계정 UUID 를 보여줄 수는 없다.
    account_label: str | None = None
    marketplace: str | None = None
    account_status: str | None = None

    # connector 지원 여부. **화면이 판매처 이름으로 추측하지 않게** 서버가 준다.
    price_publish_supported: bool = False
    price_publish_unsupported_reason: str | None = None

    @property
    def version_token(self) -> str | None:
        return self.updated_at.isoformat() if self.updated_at else None


class ChannelPriceIn(BaseModel):
    """가격 한 건의 입력. 승인 요청 payload 로도 그대로 쓴다.

    **JSONB 를 임의 저장소로 쓰지 않기 위해** 이 모델로 검증한 뒤에만 저장한다.
    """

    product_variant_id: UUID | None = None
    currency: str = Field(default="KRW", pattern=r"^[A-Z]{3}$")

    # 미입력은 0 이 아니라 None. 0 원은 "공짜로 판다" 는 뜻이다.
    regular_price: int | None = Field(default=None, gt=0)
    sale_price: int | None = Field(default=None, gt=0)
    sale_start_at: datetime | None = None
    sale_end_at: datetime | None = None

    effective_from: date
    effective_to: date | None = None

    @model_validator(mode="after")
    def _check(self) -> ChannelPriceIn:
        # DB CHECK 와 같은 규칙을 여기서도 본다. DB 가 최종 방어선이지만,
        # 서비스에서 걸러야 사용자가 제약 이름이 아니라 문장을 본다.
        if self.sale_price is not None and self.regular_price is not None:
            if self.sale_price > self.regular_price:
                raise ValueError("할인가는 정상가보다 클 수 없습니다.")
        if self.sale_price is None and (self.sale_start_at or self.sale_end_at):
            raise ValueError("할인가 없이 할인 기간만 지정할 수 없습니다.")
        if self.sale_price is not None and self.sale_start_at is None:
            raise ValueError("할인가를 지정하면 할인 시작일이 필요합니다.")
        if self.sale_start_at and self.sale_end_at and self.sale_end_at <= self.sale_start_at:
            raise ValueError("할인 종료일은 시작일보다 뒤여야 합니다.")
        if self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError("가격 적용 종료일은 시작일보다 뒤여야 합니다.")

        # 할인 기간은 가격 유효기간을 벗어날 수 없다.
        # 벗어나면 "그 가격 버전이 끝난 뒤에도 할인이 살아 있는" 상태가 되어
        # 어느 버전의 할인인지 알 수 없다.
        if self.sale_start_at is not None:
            if self.sale_start_at.date() < self.effective_from:
                raise ValueError("할인 시작일이 가격 적용 시작일보다 앞설 수 없습니다.")
            if self.effective_to and self.sale_start_at.date() >= self.effective_to:
                raise ValueError("할인 시작일이 가격 적용 기간을 벗어납니다.")
        if self.sale_end_at is not None and self.effective_to:
            if self.sale_end_at.date() > self.effective_to:
                raise ValueError("할인 종료일이 가격 적용 기간을 벗어납니다.")
        return self


class ChannelPriceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    listing_id: UUID
    product_variant_id: UUID | None
    currency: str
    regular_price: int | None
    sale_price: int | None
    sale_start_at: datetime | None
    sale_end_at: datetime | None
    effective_from: date
    effective_to: date | None
    status: str
    approval_request_id: UUID | None
    publish_status: str
    publish_requested_at: datetime | None
    published_at: datetime | None
    publish_error: str | None
    publish_attempt_count: int
    created_at: datetime
    created_by: UUID | None = None
    updated_at: datetime | None = None


class ResolvedPrice(BaseModel):
    """특정 시점·variant 에 **실제로 적용되는** 가격.

    화면이 이 계산을 다시 하지 않는다. fallback 규칙이 두 군데 있으면 언젠가 갈린다.
    """

    listing_id: UUID
    product_variant_id: UUID | None
    at: date

    # 실제 적용가. 할인 기간이면 sale_price, 아니면 regular_price. 없으면 None(미입력).
    resolved_price: int | None
    regular_price: int | None
    sale_price: int | None
    sale_start_at: datetime | None
    sale_end_at: datetime | None
    on_sale: bool

    currency: str | None
    # 어느 가격 레코드에서 나왔는가. 감사·디버깅에 필요하다.
    source_price_id: UUID | None
    # variant 가격이 없어 상품 기본 가격을 썼는가
    variant_fallback: bool
    effective_from: date | None
    effective_to: date | None

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
