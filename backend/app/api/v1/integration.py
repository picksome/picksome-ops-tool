"""연동(integration_account) + 봇 ingest 라우터 (M009, docs/27 3-3 투트랙).

endpoints:
  GET  /integrations                          — 봇 계정 목록 (JWT, api_key_hash 는 응답에 없음)
  POST /integrations                          — 봇/API 연동 계정 발급 (JWT 인증, ADMIN/OPERATOR)
                                                  응답의 api_key 는 이 1회만 노출.
  GET  /integrations/{account_id}/sale-channels — 봇이 유효 마켓코드 미리 조회.
                                                  인증: X-Integration-Key 헤더(봇 전용, JWT 아님).
  POST /integrations/{account_id}/ingest/orders — 봇이 판매데이터 자동입력.
                                                  인증: X-Integration-Key 헤더(JWT 아님, D-289).

X-Integration-Key 엔드포인트는 사람이 아니라 봇/외부 배치가 호출하므로 CurrentUser(JWT) 를
쓰지 않는다 — account_id + 헤더의 키로 org_id 를 그 자리에서 확정한다.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.domain.integration_api import (
    IngestOrdersRequest,
    IngestResultOut,
    IntegrationAccountCreate,
    IntegrationAccountListItem,
    IntegrationAccountOut,
    SaleChannelCodeOut,
)
from app.services.integration_account import (
    create_bot_account,
    list_bot_accounts,
    list_valid_channel_codes,
    verify_account_key,
)
from app.services.integration_ingest import ingest_orders

router = APIRouter(prefix="/integrations", tags=["integrations"])

_read = require_roles("ADMIN", "OPERATOR", "ACCOUNTING", "READONLY")
_write = require_roles("ADMIN", "OPERATOR")


@router.get("")
def list_accounts(
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    rows = list_bot_accounts(db, org_id=user.org_id)
    out = [IntegrationAccountListItem(**r) for r in rows]
    return envelope([o.model_dump() for o in out])


@router.post("", status_code=201)
def create_account(
    body: IntegrationAccountCreate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    new_id, raw_key = create_bot_account(
        db,
        org_id=user.org_id,
        actor=user.user_id,
        provider=body.provider,
        account_label=body.account_label,
        sale_channel_code=body.sale_channel_code,
    )
    record_audit(
        db,
        org_id=user.org_id,
        domain_table="integration_account",
        domain_row_id=new_id,
        action="CREATE",
        actor_user_id=user.user_id,
        after={"provider": body.provider, "account_label": body.account_label},
    )
    db.commit()
    out = IntegrationAccountOut(
        id=new_id, provider=body.provider, account_label=body.account_label, api_key=raw_key
    )
    return envelope(out.model_dump())


def _verify_ingest_key(
    account_id: UUID,
    x_integration_key: str = Header(...),
    db: Session = Depends(get_db),
) -> UUID:
    """account_id 는 라우트 path param 과 이름이 같아 FastAPI 가 그대로 재사용한다."""
    return verify_account_key(db, account_id=account_id, raw_key=x_integration_key)


@router.get("/{account_id}/sale-channels")
def bot_list_sale_channels(
    account_id: UUID,
    org_id: UUID = Depends(_verify_ingest_key),
    db: Session = Depends(get_db),
) -> dict:
    rows = list_valid_channel_codes(db, org_id=org_id)
    out = [SaleChannelCodeOut(**r) for r in rows]
    return envelope([o.model_dump() for o in out])


@router.post("/{account_id}/ingest/orders")
def ingest_orders_endpoint(
    account_id: UUID,
    body: IngestOrdersRequest,
    org_id: UUID = Depends(_verify_ingest_key),
    db: Session = Depends(get_db),
) -> dict:
    result = ingest_orders(
        db,
        org_id=org_id,
        account_id=account_id,
        items=[item.model_dump() for item in body.items],
        source_type="BOT_AUTO",
    )
    out = IngestResultOut(
        batch_run_id=result.batch_run_id,
        status=result.status,
        success_count=result.success_count,
        failed_count=result.failed_count,
    )
    return envelope(out.model_dump())
