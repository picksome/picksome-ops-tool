"""판매처 listing / 가격 API (M022).

경로 설계
--------
listing 은 상품에 속하므로 생성·목록은 `/products/{id}/channel-listings` 아래 둔다.
개별 listing 과 그 가격은 상품 id 없이 접근 가능해야 해서(승인 화면은 listing id 만
알고 있다) `/channel-listings/{id}` 로 따로 뺐다.

중복 엔드포인트를 만들지 않았다 — 기존 product router 는 상품 본체와 옵션만 다루고,
여기에 listing 을 끼워 넣으면 파일이 두 관심사를 갖게 된다.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.domain.channel_price import (
    ChannelListingCreate,
    ChannelListingUpdate,
    ChannelPriceIn,
)
from app.services import channel_connector
from app.services import channel_price as svc

# 가격은 원가와 함께 마진을 결정한다. 조회는 넓게, 쓰기는 좁게.
_read = require_roles("ADMIN", "OPERATOR", "ACCOUNTING", "SOURCING", "READONLY")
_write = require_roles("ADMIN", "OPERATOR", "SOURCING")

product_router = APIRouter(prefix="/products", tags=["channel-listings"])
listing_router = APIRouter(prefix="/channel-listings", tags=["channel-listings"])


@product_router.get("/{product_id}/channel-listings")
def list_for_product(
    product_id: UUID,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    items, total = svc.list_listings(
        db, org_id=user.org_id, product_id=product_id, status=status,
        limit=page_size, offset=(page - 1) * page_size,
    )
    return envelope(
        {"items": [i.model_dump(mode="json") for i in items], "total": total},
        meta={"page": page, "page_size": page_size},
    )


@product_router.post("/{product_id}/channel-listings", status_code=201)
def create_for_product(
    product_id: UUID,
    body: ChannelListingCreate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = svc.create_listing(
        db, org_id=user.org_id, actor=user.user_id, product_id=product_id, body=body,
    )
    return envelope(out.model_dump(mode="json"))


@listing_router.get("/connector-support")
def connector_support(user: CurrentUser = Depends(_read)) -> dict:
    """판매처별 가격 전송 지원 여부.

    화면이 판매처 이름으로 추측하지 않게 서버가 알려준다. 지금은 전부 미지원이고,
    connector 가 생기면 이 응답만 바뀌어도 화면 버튼이 살아난다.
    """
    _ = user
    return envelope(channel_connector.all_support())


@listing_router.get("")
def search_listings(
    integration_account_id: UUID | None = None,
    external_product_id: str | None = None,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    items, total = svc.list_listings(
        db, org_id=user.org_id, integration_account_id=integration_account_id,
        external_product_id=external_product_id, status=status,
        limit=page_size, offset=(page - 1) * page_size,
    )
    return envelope(
        {"items": [i.model_dump(mode="json") for i in items], "total": total},
        meta={"page": page, "page_size": page_size},
    )


@listing_router.get("/{listing_id}")
def get_listing(
    listing_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    out = svc.get_listing(db, org_id=user.org_id, listing_id=listing_id)
    payload = out.model_dump(mode="json")
    payload["version_token"] = out.version_token
    return envelope(payload)


@listing_router.patch("/{listing_id}")
def update_listing(
    listing_id: UUID,
    body: ChannelListingUpdate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = svc.update_listing(
        db, org_id=user.org_id, actor=user.user_id, listing_id=listing_id, body=body,
    )
    payload = out.model_dump(mode="json")
    payload["version_token"] = out.version_token
    return envelope(payload)


@listing_router.post("/{listing_id}/deactivate")
def deactivate_listing(
    listing_id: UUID,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    """비활성화. **삭제가 아니다** — 과거 가격과 주문이 이 리스팅을 가리킨다."""
    out = svc.deactivate_listing(
        db, org_id=user.org_id, actor=user.user_id, listing_id=listing_id,
    )
    return envelope(out.model_dump(mode="json"))


@listing_router.get("/{listing_id}/prices")
def list_prices(
    listing_id: UUID,
    variant_id: UUID | None = None,
    status: str | None = None,
    at: date | None = None,
    approval_request_id: UUID | None = None,
    include_default: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """가격 이력. 현재·미래·과거를 모두 돌려주고 구분은 화면이 한다.

    `effective_from DESC` 정렬이라 최신 예정 가격이 위에 온다.
    """
    items, total = svc.list_prices(
        db, org_id=user.org_id, listing_id=listing_id, variant_id=variant_id,
        status=status, at=at, approval_request_id=approval_request_id,
        include_default=include_default,
        limit=page_size, offset=(page - 1) * page_size,
    )
    return envelope(
        {"items": [i.model_dump(mode="json") for i in items], "total": total},
        meta={"page": page, "page_size": page_size, "today_kst": svc.today_kst().isoformat()},
    )


@listing_router.post("/{listing_id}/price-preview")
def price_preview(
    listing_id: UUID,
    body: ChannelPriceIn,
    at: date | None = None,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """가격 변경 전후 마진 영향.

    **기존 마진 계산 엔진을 그대로 쓴다.** 미리보기 전용 계산을 만들면 화면이 보여준
    마진과 실제 저장되는 마진이 언젠가 갈라진다.

    결과는 **판단 보조값**이지 실제 가격 데이터의 source of truth 가 아니다.
    권한이 없으면 원가와 역산 가능한 값을 서버에서 지운다.
    """
    from app.services.channel_price_preview import preview_price_change

    return envelope(preview_price_change(
        db, org_id=user.org_id, listing_id=listing_id, payload=body,
        role=user.role, at=at,
    ))


@listing_router.post("/prices/{price_id}/publish")
def request_publish(
    price_id: UUID,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    """판매처 반영 요청.

    **외부 API 를 여기서 부르지 않는다.** outbox 에 작업을 넣고 즉시 돌려준다.
    connector 가 없는 판매처는 큐에 넣지도 않고 `CONNECTOR_UNAVAILABLE`(403) 로 거절한다 —
    넣어봐야 실패로 끝날 작업이고, 큐에 쌓이면 "처리 중" 처럼 보인다.
    """
    from app.services.channel_publish import request_publish as do

    job_id = do(db, org_id=user.org_id, price_id=price_id, actor=user.user_id)
    return envelope({"job_id": str(job_id), "status": "QUEUED"})


@listing_router.post("/publish/run-due", tags=["channel-listings"])
def run_publish(
    batch_size: int = Query(default=20, ge=1, le=200),
    user: CurrentUser = Depends(require_roles("ADMIN")),
    db: Session = Depends(get_db),
) -> dict:
    """대기 중인 반영 작업 처리 (보호된 admin batch endpoint).

    connector 가 없으므로 현재는 모든 작업이 `CONNECTOR_UNAVAILABLE` 로 끝난다.
    """
    from app.services.channel_publish import run_publish_batch

    return envelope(run_publish_batch(db, org_id=user.org_id, batch_size=batch_size).as_dict())


@listing_router.get("/{listing_id}/resolved-price")
def resolved_price(
    listing_id: UUID,
    variant_id: UUID | None = None,
    at: date | None = None,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """특정 시점·variant 에 실제로 적용되는 가격.

    **화면이 이 계산을 다시 하지 않는다.** fallback 규칙(variant → 기본가)과
    할인 기간 판정이 두 군데 있으면 언젠가 갈린다.
    """
    out = svc.resolve_price(
        db, org_id=user.org_id, listing_id=listing_id, variant_id=variant_id, at=at,
    )
    return envelope(out.as_dict())
