"""봇/API 판매 데이터 자동입력 (M009, docs/27 3-3 투트랙).

트랙 A(API pull, integration_job 스케줄)와 트랙 B(봇 push, /ingest/orders 호출)는
둘 다 이 모듈로 수렴한다: batch_run 1건 + marketplace_order_import(raw 박제) 를
만들고, 각 항목을 services.sales.create_sale() 로 정규화한다.

항목 단위 실패는 다른 항목 처리를 막지 않는다(partial success) — 실패 원인은
batch_run_log 에 남고, batch_run.status 로 SUCCESS/PARTIAL/FAILED 노출.
DB 레벨 에러(UNIQUE 위반 등) 발생 시 그 항목만 rollback 하고 다음 항목으로 진행
— 세션이 깨진 트랜잭션 상태로 남지 않게 매 항목마다 격리한다.

알려진 한계: 실패한 항목은 rollback 되면서 marketplace_order_import 의 raw_json 도
함께 사라진다(성공한 항목만 raw 가 남음) — 실패 원인은 batch_run_log.message 에
order_no + 에러 메시지로만 남는다. 실패 건의 raw payload 전체를 보존하려면 그 INSERT 를
create_sale() 시도 전에 별도 커밋해야 하는데, 그러면 성공/실패와 무관하게 매 항목마다
커밋이 늘어나 트레이드오프가 있다 — 지금은 "실패 원인 텍스트로 충분" 쪽으로 단순화.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.domain.sales import SaleOrderCreate
from app.services.sales import create_sale


@dataclass(slots=True)
class IngestResult:
    batch_run_id: UUID
    status: str  # SUCCESS / PARTIAL / FAILED
    success_count: int
    failed_count: int


def _resolve_product_id(
    db: Session, *, org_id: UUID, sku: str | None, product_business_key: str | None
) -> UUID | None:
    if sku:
        row = db.execute(
            text("SELECT id FROM product WHERE org_id = :o AND sku = :sku AND deleted_at IS NULL LIMIT 1"),
            {"o": str(org_id), "sku": sku},
        ).first()
        if row:
            return UUID(str(row.id))
    if product_business_key:
        row = db.execute(
            text(
                "SELECT id FROM product WHERE org_id = :o AND business_key = :bk AND deleted_at IS NULL LIMIT 1"
            ),
            {"o": str(org_id), "bk": product_business_key},
        ).first()
        if row:
            return UUID(str(row.id))
    return None


def _log(db: Session, *, batch_run_id: UUID, level: str, message: str) -> None:
    db.execute(
        text(
            "INSERT INTO batch_run_log (id, batch_run_id, level, message) VALUES (:id, :b, :lvl, :msg)"
        ),
        {"id": str(new_id()), "b": str(batch_run_id), "lvl": level, "msg": message[:2000]},
    )
    db.commit()


def ingest_orders(
    db: Session, *, org_id: UUID, account_id: UUID, items: list[dict[str, Any]], source_type: str
) -> IngestResult:
    """호출자(라우터)는 이미 verify_account_key 로 org_id 를 확정한 뒤 호출."""
    batch_id = new_id()
    db.execute(
        text(
            "INSERT INTO batch_run (id, job_id, started_at, status, affected_rows) "
            "VALUES (:id, NULL, now(), 'RUNNING', 0)"
        ),
        {"id": str(batch_id)},
    )
    db.commit()

    success = 0
    failed = 0
    for item in items:
        order_no = str(item.get("marketplace_order_no", "?"))
        try:
            db.execute(
                text(
                    """
                    INSERT INTO marketplace_order_import (id, org_id, batch_run_id, raw_json, source_type)
                    VALUES (:id, :org, :batch, CAST(:raw AS JSONB), :src)
                    """
                ),
                {
                    "id": str(new_id()),
                    "org": str(org_id),
                    "batch": str(batch_id),
                    "raw": json.dumps(item, ensure_ascii=False, default=str),
                    "src": source_type,
                },
            )
            product_id = _resolve_product_id(
                db, org_id=org_id, sku=item.get("sku"), product_business_key=item.get("product_business_key")
            )
            create_sale(
                db,
                org_id=org_id,
                actor=None,
                body=SaleOrderCreate(
                    sale_date=item["sale_date"],
                    marketplace=item["marketplace"],
                    marketplace_order_no=item["marketplace_order_no"],
                    product_id=product_id,
                    product_snapshot_name=item.get("product_snapshot_name"),
                    quantity=item["quantity"],
                    sale_price=item["sale_price"],
                    ad_cost=item.get("ad_cost", 0),
                    shipping_fee=item.get("shipping_fee", 0),
                    memo=item.get("memo"),
                    # 이 배치를 **실행한 계정**이다 (M032).
                    #
                    # 이 값은 이미 확정돼 있다 — 라우터가 `verify_account_key` 로
                    # 키를 검증하면서 계정을 특정했다. 그런데 예전에는 여기서
                    # 그냥 버렸고, 주문에는 `marketplace` 문자열만 남았다.
                    # 그래서 쿠팡 본점과 2호점의 주문을 구분할 수 없었다.
                    #
                    # 페이로드의 값을 쓰지 않는다 — 봇이 남의 계정 id 를 보내면
                    # 그 계정 매출로 잡힌다. **실행 주체가 곧 귀속 대상이다.**
                    integration_account_id=account_id,
                ),
                import_source=source_type,
                import_batch_id=batch_id,
            )
            success += 1
        except Exception as e:  # noqa: BLE001 — 항목 단위 격리, 실패 원인 다양(검증/DB제약)
            db.rollback()
            failed += 1
            _log(db, batch_run_id=batch_id, level="ERROR", message=f"{order_no}: {e}")

    status_val = "SUCCESS" if failed == 0 else ("FAILED" if success == 0 else "PARTIAL")
    db.execute(
        text(
            "UPDATE batch_run SET status = :status, finished_at = now(), affected_rows = :n WHERE id = :id"
        ),
        {"status": status_val, "n": success, "id": str(batch_id)},
    )
    db.execute(
        text("UPDATE integration_account SET last_synced_at = now() WHERE id = :id"),
        {"id": str(account_id)},
    )
    db.commit()

    return IngestResult(batch_run_id=batch_id, status=status_val, success_count=success, failed_count=failed)
