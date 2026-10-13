"""services/integration_account.py + integration_ingest.py 통합 테스트 (M009, docs/27 3-3)."""

import pytest
from sqlalchemy import text

from app.core.exceptions import ValidationError
from app.services.integration_account import create_bot_account, verify_account_key
from app.services.integration_ingest import ingest_orders
from app.tests.fixtures import factories as f


def _make_sku_product(db, org_id):
    product_id = f.make_product(db, org_id=org_id, cost_amount=5_000, business_key="TESTP-1")
    db.execute(text("UPDATE product SET sku = 'SKU-BOT-1' WHERE id = :id"), {"id": str(product_id)})
    return product_id


def test_create_bot_account_and_verify_key(db):
    org_id = f.make_organization(db)
    actor = f.make_app_user(db, org_id=org_id)

    account_id, raw_key = create_bot_account(
        db, org_id=org_id, actor=actor, provider="BOT_GENERIC", account_label="테스트 봇",
        sale_channel_code="COUPANG",
    )
    assert verify_account_key(db, account_id=account_id, raw_key=raw_key) == org_id

    with pytest.raises(ValidationError):
        verify_account_key(db, account_id=account_id, raw_key="wrong-key")


def test_ingest_orders_partial_success(db):
    org_id = f.make_organization(db)
    actor = f.make_app_user(db, org_id=org_id)
    product_id = _make_sku_product(db, org_id)
    account_id, _ = create_bot_account(
        db, org_id=org_id, actor=actor, provider="BOT_GENERIC", account_label="테스트 봇",
    )

    items = [
        {
            "sale_date": "2026-07-29", "marketplace": "COUPANG", "marketplace_order_no": "BOT-ORDER-1",
            "sku": "SKU-BOT-1", "quantity": 1, "sale_price": 15_000,
        },
        {
            "sale_date": "2026-07-29", "marketplace": "NOT_A_CHANNEL", "marketplace_order_no": "BOT-ORDER-2",
            "quantity": 1, "sale_price": 9_000,
        },
    ]
    result = ingest_orders(db, org_id=org_id, account_id=account_id, items=items, source_type="BOT_AUTO")

    assert result.status == "PARTIAL"
    assert result.success_count == 1
    assert result.failed_count == 1

    row = db.execute(
        text(
            "SELECT product_id, import_source, import_batch_id FROM sales_order "
            "WHERE org_id = :o AND marketplace_order_no = 'BOT-ORDER-1'"
        ),
        {"o": str(org_id)},
    ).first()
    assert str(row.product_id) == str(product_id)
    assert row.import_source == "BOT_AUTO"
    assert str(row.import_batch_id) == str(result.batch_run_id)

    batch_row = db.execute(
        text("SELECT status, affected_rows FROM batch_run WHERE id = :id"),
        {"id": str(result.batch_run_id)},
    ).first()
    assert batch_row.status == "PARTIAL"
    assert batch_row.affected_rows == 1

    log_rows = db.execute(
        text("SELECT level FROM batch_run_log WHERE batch_run_id = :id"),
        {"id": str(result.batch_run_id)},
    ).all()
    assert len(log_rows) == 1
    assert log_rows[0].level == "ERROR"


def test_ingest_orders_all_success_updates_last_synced_at(db):
    org_id = f.make_organization(db)
    actor = f.make_app_user(db, org_id=org_id)
    _make_sku_product(db, org_id)
    account_id, _ = create_bot_account(
        db, org_id=org_id, actor=actor, provider="BOT_GENERIC", account_label="테스트 봇",
    )

    items = [
        {
            "sale_date": "2026-07-29", "marketplace": "COUPANG", "marketplace_order_no": "BOT-ORDER-3",
            "sku": "SKU-BOT-1", "quantity": 2, "sale_price": 10_000,
        },
    ]
    result = ingest_orders(db, org_id=org_id, account_id=account_id, items=items, source_type="BOT_AUTO")

    assert result.status == "SUCCESS"
    assert result.failed_count == 0

    acct_row = db.execute(
        text("SELECT last_synced_at FROM integration_account WHERE id = :id"),
        {"id": str(account_id)},
    ).first()
    assert acct_row.last_synced_at is not None
