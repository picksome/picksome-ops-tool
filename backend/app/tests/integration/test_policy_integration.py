"""services/policy.py 통합 테스트 — 실 DB(test DB)로 resolve/persist 검증.

CLAUDE.md Generalization Principle 박제 원칙의 핵심 경로:
  - effective_from/effective_to 기준 정책 선택이 정확한가
  - tax_policy 누락 시 즉시 실패하는가 (M002 seed 누락 신호)
  - persist_policy_snapshot 이 snapshot/ref/value 3테이블에 정확히 기록되는가
"""

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text

from app.domain.policy import PolicyContext
from app.services.policy import persist_policy_snapshot, resolve_active_policies
from app.tests.fixtures import factories as f


def test_resolve_active_policies_picks_effective_version(db):
    org_id = f.make_organization(db)
    f.make_tax_policy(db, org_id=org_id, vat_rate=Decimal("0.1000"), version=1,
                       effective_from=date(2026, 1, 1), effective_to=date(2026, 5, 31))
    f.make_tax_policy(db, org_id=org_id, vat_rate=Decimal("0.0900"), version=2,
                       effective_from=date(2026, 6, 1), effective_to=None)
    f.make_marketplace_fee_policy(db, org_id=org_id, rate=Decimal("0.1080"))

    ctx_before = PolicyContext(org_id=org_id, at=date(2026, 3, 1), marketplace="COUPANG")
    resolved_before = resolve_active_policies(ctx_before, db)
    assert resolved_before.tax.vat_rate == Decimal("0.1000")

    ctx_after = PolicyContext(org_id=org_id, at=date(2026, 6, 15), marketplace="COUPANG")
    resolved_after = resolve_active_policies(ctx_after, db)
    assert resolved_after.tax.vat_rate == Decimal("0.0900")


def test_resolve_active_policies_category_prefix_match(db):
    org_id = f.make_organization(db)
    f.make_tax_policy(db, org_id=org_id)
    f.make_marketplace_fee_policy(db, org_id=org_id, rate=Decimal("0.1000"), category_path=None)
    f.make_marketplace_fee_policy(db, org_id=org_id, rate=Decimal("0.1500"), category_path="패션/신발")

    ctx = PolicyContext(
        org_id=org_id, at=date(2026, 3, 1), marketplace="COUPANG", category_path="패션/신발/운동화"
    )
    resolved = resolve_active_policies(ctx, db)
    # 카테고리 특정 정책이 전체(NULL) 정책보다 우선.
    assert resolved.marketplace_fee.rate == Decimal("0.1500")


def test_resolve_active_policies_missing_tax_raises(db):
    org_id = f.make_organization(db)
    ctx = PolicyContext(org_id=org_id, at=date(2026, 3, 1))
    with pytest.raises(RuntimeError):
        resolve_active_policies(ctx, db)


def test_persist_policy_snapshot_round_trip(db):
    org_id = f.make_organization(db)
    f.make_tax_policy(db, org_id=org_id)
    f.make_marketplace_fee_policy(db, org_id=org_id)
    ctx = PolicyContext(org_id=org_id, at=date(2026, 3, 1), marketplace="COUPANG")
    resolved = resolve_active_policies(ctx, db)

    owner_id = UUID(int=999)
    snapshot_id = persist_policy_snapshot("MARGIN_EXPECTED", owner_id, resolved, org_id, db)

    snap_row = db.execute(
        text("SELECT owner_kind, owner_id FROM policy_snapshot WHERE id = :id"),
        {"id": str(snapshot_id)},
    ).first()
    assert snap_row.owner_kind == "MARGIN_EXPECTED"
    assert UUID(str(snap_row.owner_id)) == owner_id

    ref_rows = db.execute(
        text("SELECT policy_table FROM policy_snapshot_ref WHERE snapshot_id = :id"),
        {"id": str(snapshot_id)},
    ).all()
    ref_tables = {r.policy_table for r in ref_rows}
    assert "tax_policy" in ref_tables
    assert "marketplace_fee_policy" in ref_tables

    value_row = db.execute(
        text("SELECT values_json FROM policy_snapshot_value WHERE snapshot_id = :id"),
        {"id": str(snapshot_id)},
    ).first()
    assert "tax_policy" in value_row.values_json
