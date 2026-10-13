"""검증센터 — 대사·신뢰도·계산 추적.

이 파일이 지키는 것
----------------
```
1. 세 상태를 섞지 않는다          NOT_VERIFIED 를 PASS 로 세지 않는다
2. 독립 산술로 검산한다            production 함수를 불러 expected 를 만들지 않는다
3. 모르는 값을 0 과 구분한다       원가 미확인과 원가 0원은 다른 사실이다
4. 조직을 넘지 않는다
5. 개인정보를 내보내지 않는다
```

2 가 가장 중요하다
---------------
`calculate_actual_margin()` 을 불러 expected 를 만들면, 그 함수가 틀려도
테스트는 통과한다. 여기서는 **원천 숫자를 손으로 더해** 기대값을 만든다.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import verification as ver
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
    make_sales_order,
)

#: 모든 주문을 같은 날짜에 둔다. 기간 필터가 아니라 대사 로직을 보는 파일이라
#: 날짜가 흩어지면 무엇을 세는지만 흐려진다.
SALE_DATE = date(2026, 1, 15)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


@pytest.fixture
def ne_account(db: Session, org, actor) -> uuid.UUID:
    """수집 실행 기록이 가리킬 외부 계정. `external_sync_run` 이 NOT NULL 이다."""
    sys_id = db.execute(text("""
        INSERT INTO external_system (code, name, system_type, supports_api_key)
        VALUES ('NEXT_ENGINE', 'NEXT ENGINE', 'SELLER_TOOL', TRUE)
        ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id
    """)).scalar()
    return uuid.UUID(str(db.execute(text("""
        INSERT INTO external_account (org_id, external_system_id, account_code,
               display_name, credential_kind, created_by, updated_by)
        VALUES (:o, :s, 'DEFAULT', 'NE', 'API_KEY', :a, :a) RETURNING id
    """), {"o": str(org), "s": str(sys_id), "a": str(actor)}).scalar()))


def _settlement_row(
    db: Session, *, org_id: uuid.UUID, sales_order_id: uuid.UUID | None,
    row_type: str, gross: int, fee: int, net: int, deductions: int = 0,
    status: str = "MATCHED_EXACT", order_no: str = "M-1",
    period_start: date = date(2026, 1, 1),
) -> uuid.UUID:
    return uuid.UUID(str(db.execute(text("""
        INSERT INTO marketplace_settlement_record
               (org_id, marketplace, settlement_period_start, settlement_period_end,
                marketplace_order_no, gross_revenue, marketplace_fee, ad_cost,
                return_cost, fulfillment_fee, other_deductions, net_settlement,
                matched_sales_order_id, matching_status, row_type, line_reference,
                import_file_hash, import_idempotency_key, source_row_fingerprint)
        VALUES (:o, 'COUPANG', :ps, :pe, :ono, :g, :f, 0, 0, 0, :d, :n,
                :sid, :st, :rt, :lr, :hash, :key, :fp)
        RETURNING id
    """), {
        "o": str(org_id), "ps": period_start, "pe": period_start,
        "ono": order_no, "g": gross, "f": fee, "d": deductions, "n": net,
        "sid": str(sales_order_id) if sales_order_id else None,
        "st": status, "rt": row_type, "lr": f"{order_no}-{row_type}",
        "hash": "test-file-hash",
        "key": f"{order_no}-{row_type}-{uuid.uuid4()}",
        "fp": str(uuid.uuid4()),
    }).scalar()))


# ── 1) 정산 금액 대사 — 독립 산술 ──────────────────────────────────────

def test_settlement_amounts_are_checked_against_the_vendors_own_numbers(
    db: Session, org, actor,
):
    """§64 — 우리 계산을 부르지 않고 정산서 숫자끼리의 관계를 본다.

    ```
    순매출 = 상품매출 + 배송비매출 - 판매자쿠폰
    순매출 - 수수료 = 벤더 정산금액
    ```

    기대값을 손으로 만든다. `settlement_components` 를 불러 만들면 그 모듈이
    틀렸을 때 함께 틀린다.
    """
    product = make_product(db, org_id=org)
    order = make_sales_order(db, org_id=org, product_id=product, sale_date=SALE_DATE, sale_price=30000)

    # 원천 숫자 — 사람이 정산서에서 읽었다고 가정한 값.
    product_revenue, delivery_revenue, coupon, fee = 30000, 3000, 1000, 3520
    net_revenue = product_revenue + delivery_revenue - coupon      # 32,000
    vendor_net = net_revenue - fee                                  # 28,480

    _settlement_row(db, org_id=org, sales_order_id=order, row_type="PRODUCT_SALE",
                    gross=product_revenue, fee=fee, net=vendor_net, deductions=coupon)
    _settlement_row(db, org_id=org, sales_order_id=order, row_type="SHIPPING_FEE_BASE",
                    gross=delivery_revenue, fee=0, net=0)
    db.commit()

    result = ver.settlement_reconciliation(db, org_id=org)

    assert result["orders_checked"] == 1
    assert result["amount_verified"] == 1
    assert result["amount_failure_count"] == 0
    assert result["status"] == ver.PASS


def test_a_one_won_difference_is_not_hidden(db: Session, org, actor):
    """원 단위로 본다. 허용 오차를 두면 그 안에서 조용히 망가진다."""
    product = make_product(db, org_id=org)
    order = make_sales_order(db, org_id=org, product_id=product, sale_date=SALE_DATE, sale_price=30000)

    # 벤더 정산금액을 1원 어긋나게 둔다.
    _settlement_row(db, org_id=org, sales_order_id=order, row_type="PRODUCT_SALE",
                    gross=30000, fee=3000, net=26999)
    db.commit()

    result = ver.settlement_reconciliation(db, org_id=org)

    assert result["amount_failure_count"] == 1
    assert result["status"] == ver.FAIL
    assert result["amount_failures"][0]["difference"] == 1


def test_refund_rows_are_excluded_not_passed(db: Session, org, actor):
    """§21 — 환불 식은 표본이 없어 **모른다.** PASS 로 세지 않는다."""
    product = make_product(db, org_id=org)
    order = make_sales_order(db, org_id=org, product_id=product, sale_date=SALE_DATE, sale_price=30000)

    # 음수 금액 = 환불로 짐작되는 행.
    _settlement_row(db, org_id=org, sales_order_id=order, row_type="PRODUCT_SALE",
                    gross=-30000, fee=-3000, net=-27000)
    db.commit()

    result = ver.settlement_reconciliation(db, org_id=org)

    assert result["refund_excluded"] == 1
    assert result["amount_verified"] == 0
    assert result["refund_formula"] == ver.NOT_VERIFIED


# ── 2) 수집 대사 ────────────────────────────────────────────────────────

def _sync_run(
    db: Session, *, org_id: uuid.UUID, account_id: uuid.UUID, sync_type: str, read: int, created: int,
    updated: int, skipped: int, conflict: int, rejected: int,
    status: str = "SUCCESS", started: str = "now()",
) -> None:
    db.execute(text(f"""
        INSERT INTO external_sync_run
               (org_id, external_account_id, sync_type, trigger, status, started_at, finished_at,
                read_count, created_count, updated_count, skipped_count,
                conflict_count, error_count)
        VALUES (:o, :acc, :t, 'SCHEDULED', :s, {started}, now(),
                :r, :c, :u, :sk, :cf, :e)
    """), {"o": str(org_id), "acc": str(account_id), "t": sync_type, "s": status, "r": read,
           "c": created, "u": updated, "sk": skipped, "cf": conflict, "e": rejected})


def test_orders_and_claims_use_different_reconciliation_identities(db: Session, org, ne_account):
    """클레임의 `conflict_count` 는 배타적 분류가 아니다.

    주문 등식을 클레임에 대면 항상 어긋난다 — 실측에서 그렇게 보였다.
    각 도메인의 실제 등식을 써야 한다.
    """
    # 주문: read = created + updated + skipped + conflict + rejected
    _sync_run(db, org_id=org, account_id=ne_account, sync_type="ORDERS", read=10, created=3, updated=1,
              skipped=6, conflict=0, rejected=0)
    # 클레임: conflict 는 가로지르는 카운터라 등식에 넣지 않는다.
    _sync_run(db, org_id=org, account_id=ne_account, sync_type="CLAIMS", read=16, created=0, updated=0,
              skipped=16, conflict=14, rejected=0)
    db.commit()

    result = ver.sync_reconciliation(db, org_id=org)

    assert result["reconciliation_failures"] == 0
    assert result["status"] == ver.PASS


def test_a_run_that_loses_a_line_is_reported(db: Session, org, ne_account):
    """읽은 수와 처리한 수가 다르면 라인 하나가 조용히 사라진 것이다."""
    _sync_run(db, org_id=org, account_id=ne_account, sync_type="ORDERS", read=10, created=3, updated=0,
              skipped=6, conflict=0, rejected=0)   # 9 != 10
    db.commit()

    result = ver.sync_reconciliation(db, org_id=org)

    assert result["reconciliation_failures"] == 1
    assert result["status"] == ver.FAIL
    assert result["failures"][0]["difference"] == 1


def test_pre_split_order_runs_are_not_verified_rather_than_failed(db: Session, org, ne_account):
    """M057 이전 기록은 유형이 섞여 있다 — 고칠 수 없는 과거를 FAIL 로 두지 않는다."""
    _sync_run(db, org_id=org, account_id=ne_account, sync_type="ORDERS", read=16, created=0, updated=0,
              skipped=16, conflict=14, rejected=0,
              started="now() - interval '10 days'")
    _sync_run(db, org_id=org, account_id=ne_account, sync_type="CLAIMS", read=1, created=0, updated=0,
              skipped=1, conflict=0, rejected=0)
    db.commit()

    result = ver.sync_reconciliation(db, org_id=org)

    assert result["legacy_unclassified_runs"] == 1
    assert result["reconciliation_failures"] == 0
    assert result["status"] == ver.PASS
    assert result["legacy_reason"]


# ── 3) 신뢰도 · 미확인 ─────────────────────────────────────────────────

def test_buckets_never_double_count_an_order(db: Session, org):
    """한 주문은 정확히 한 칸에만 들어간다."""
    product = make_product(db, org_id=org)
    for _ in range(3):
        make_sales_order(db, org_id=org, product_id=product, sale_date=SALE_DATE, sale_price=10000)
    db.commit()

    result = ver.margin_confidence(db, org_id=org)

    assert result["buckets_sum_matches_total"] is True
    assert (result["no_margin_row"] + result["actual"]
            + result["estimated"] + result["unknown"]) == result["orders_total"]


def test_unknown_cost_is_not_the_same_as_zero_cost(db: Session, org, actor):
    """원가를 모르는 것과 원가가 0원인 것은 다른 사실이다.

    둘을 같게 보면 마진이 원가만큼 부풀고 아무도 눈치채지 못한다.
    """
    from app.domain.margin import ActualMarginInput
    from app.services import margin as margin_svc

    # 원가 근거가 하나도 없는 상품 (마스터 원가 없음, 주문 매입액 없음)
    product = make_product(db, org_id=org, cost_amount=None)
    order = make_sales_order(db, org_id=org, product_id=product, sale_date=SALE_DATE,
                             sale_price=10000, purchase_cost=None)
    db.commit()

    result = margin_svc.calculate_actual_margin(
        ActualMarginInput(org_id=org, sales_order_id=order), db)

    evidence = result.settlement_snapshot["components"]["cost"]
    assert evidence["value"] == 0
    assert evidence["confidence"] == ver.UNKNOWN
    assert evidence["source"] == "UNKNOWN"
    assert any("원가를 확인할 수 없어" in w for w in result.warnings)


def test_a_known_zero_cost_is_actual_not_unknown(db: Session, org, actor):
    """실제로 0원인 건(증정·취소)은 **아는 값**이다."""
    from app.domain.margin import ActualMarginInput
    from app.services import margin as margin_svc

    product = make_product(db, org_id=org, cost_amount=None)
    order = make_sales_order(db, org_id=org, product_id=product, sale_date=SALE_DATE,
                             sale_price=0, purchase_cost=0)
    db.commit()

    result = margin_svc.calculate_actual_margin(
        ActualMarginInput(org_id=org, sales_order_id=order), db)

    cost = result.settlement_snapshot["components"]["cost"]
    assert cost["value"] == 0
    assert cost["confidence"] == ver.ACTUAL
    assert cost["source"] == "ORDER_PURCHASE_COST"


# ── 4) 계산 추적 ────────────────────────────────────────────────────────

def test_the_trace_recomputes_the_margin_from_its_own_components(
    db: Session, org, actor,
):
    """저장된 성분을 산식에 다시 넣어 저장된 마진과 맞는지 본다."""
    from app.domain.margin import ActualMarginInput
    from app.services import margin as margin_svc

    product = make_product(db, org_id=org, cost_amount=4000)
    order = make_sales_order(db, org_id=org, product_id=product, sale_date=SALE_DATE, sale_price=10000,
                             purchase_cost=4000)
    result = margin_svc.calculate_actual_margin(
        ActualMarginInput(org_id=org, sales_order_id=order), db)
    margin_svc.save_actual_margin(
        ActualMarginInput(org_id=org, sales_order_id=order), result, db)
    db.commit()

    trace = ver.order_trace(db, org_id=org, sales_order_id=order)

    assert trace["evidence_kind"] == ver.FROM_SNAPSHOT
    assert trace["margin_difference"] == 0
    assert trace["status"] == ver.PASS
    assert trace["formula"] == margin_svc.MARGIN_FORMULA


def test_a_trace_without_evidence_says_so(db: Session, org, actor):
    """근거가 없으면 되짚되 **그 사실을 숨기지 않는다.**"""
    product = make_product(db, org_id=org, cost_amount=4000)
    order = make_sales_order(db, org_id=org, product_id=product, sale_date=SALE_DATE, sale_price=10000,
                             purchase_cost=4000)
    db.execute(text("""
        INSERT INTO product_margin_actual
               (org_id, product_id, sales_order_id, actual_revenue, actual_cost,
                actual_shipping_cost, actual_marketplace_fee, actual_payment_fee,
                actual_ad_cost, actual_fulfillment_fee, actual_return_cost,
                actual_vat, actual_margin_amount, actual_margin_rate)
        VALUES (:o, :p, :s, 10000, 4000, 0, 0, 0, 0, 0, 0, 0, 6000, 0.6)
    """), {"o": str(org), "p": str(product), "s": str(order)})
    db.commit()

    trace = ver.order_trace(db, org_id=org, sales_order_id=order)

    assert trace["evidence_kind"] == ver.RECONSTRUCTED
    assert any("박제돼 있지 않다" in w for w in trace["warnings"])


def test_the_trace_carries_no_personal_information(db: Session, org, actor):
    """§49 — 검증에 구매자 정보가 필요하지 않다."""
    product = make_product(db, org_id=org, cost_amount=4000)
    order = make_sales_order(db, org_id=org, product_id=product, sale_date=SALE_DATE, sale_price=10000)
    db.commit()

    trace = ver.order_trace(db, org_id=org, sales_order_id=order)

    blob = str(trace).lower()
    for forbidden in ("recipient", "phone", "address", "email",
                      "수취인", "전화", "주소", "이메일"):
        assert forbidden not in blob


# ── 5) 조직 격리 ────────────────────────────────────────────────────────

def test_verification_never_reaches_another_org(db: Session, org, actor):
    """§48 — 질의 자체가 조직으로 좁혀져야 한다."""
    other = make_organization(db)
    product = make_product(db, org_id=org)
    order = make_sales_order(db, org_id=org, product_id=product, sale_date=SALE_DATE, sale_price=10000)
    db.commit()

    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        ver.order_trace(db, org_id=other, sales_order_id=order)

    assert ver.margin_confidence(db, org_id=other)["orders_total"] == 0


def test_integrity_checks_report_zero_on_clean_data(db: Session, org):
    result = ver.integrity(db, org_id=org)

    assert result["checks"]["duplicate_orders"] == 0
    assert result["checks"]["cross_org_settlement_match"] == 0
    assert result["status"] == ver.PASS


# ── 6) evidence gate ───────────────────────────────────────────────────

def test_revenue_split_never_double_counts(db: Session, org, actor):
    """§11 — 실정산 확인분과 추정분을 더하면 장부 매출이 된다.

    겹치면 매출이 부풀어 보이고, 빠지면 어디로 샜는지 알 수 없다.
    """
    product = make_product(db, org_id=org)
    settled = make_sales_order(db, org_id=org, product_id=product,
                               sale_date=SALE_DATE, sale_price=30000)
    make_sales_order(db, org_id=org, product_id=product,
                     sale_date=SALE_DATE, sale_price=20000)
    _settlement_row(db, org_id=org, sales_order_id=settled, row_type="PRODUCT_SALE",
                    gross=30000, fee=3000, net=27000)
    db.commit()

    result = ver.revenue_split(db, org_id=org)

    assert result["ledger_revenue"] == 50000
    assert result["settled_ledger_revenue"] == 30000
    assert result["estimated_revenue"] == 20000
    # 두 몫의 합이 전체다.
    assert result["settled_ledger_revenue"] + result["estimated_revenue"] == 50000
    assert result["settled_orders"] + result["estimated_orders"] == result["orders"]


def test_an_unsettled_order_is_not_reported_as_a_difference(db: Session, org, actor):
    """정산서가 아직 안 온 것은 오류가 아니다 — coverage 로 보여야 한다."""
    product = make_product(db, org_id=org)
    make_sales_order(db, org_id=org, product_id=product,
                     sale_date=SALE_DATE, sale_price=20000)
    db.commit()

    result = ver.revenue_split(db, org_id=org)

    assert result["estimated_orders"] == 1
    # 정산 확인분이 없으니 차이도 0 이다. 20,000 을 "차이" 라고 부르지 않는다.
    assert result["settled_difference"] == 0


def test_inventory_reports_external_reconciliation_as_unavailable(db: Session, org):
    """§35 — 없는 대사를 '차이 0' 으로 적지 않는다."""
    result = ver.inventory_integrity(db, org_id=org)

    assert result["external_reconciliation"] == ver.NOT_AVAILABLE
    # 저장된 잔량이 없으므로 '원장 vs 잔량' 검사 자체를 하지 않는다.
    assert result["balance_is_derived"] is True
    assert set(result["checks"]) == {
        "negative_on_hand", "orphan_product_reference", "duplicate_source_rows"}


def test_gates_without_samples_are_not_verified_not_passed(db: Session, org):
    """§68 — 표본이 없는 영역을 통과로 표시하지 않는다."""
    gates = ver.evidence_gates(db, org_id=org)

    assert gates["refund_financial"]["state"] == ver.NOT_VERIFIED
    assert gates["cash_receipt"]["state"] == ver.NOT_VERIFIED
    assert gates["external_inventory"]["state"] == ver.NOT_AVAILABLE
    assert gates["adjustment_posting"]["state"] == ver.NOT_VERIFIED


def test_daily_report_uses_the_same_verification_service(db: Session, org, actor):
    """§51 — 리포트가 따로 세면 두 화면의 숫자가 갈라진다."""
    from app.services import daily_report, verification

    report = daily_report.build(db, org_id=org)
    summary = verification.summary(db, org_id=org)

    assert report["verification"]["overall"] == summary["overall"]
    assert (report["verification"]["margin_not_calculated"]
            == summary["confidence"]["no_margin_row"])


def test_summary_counts_not_verified_separately_from_pass(db: Session, org):
    """미검증을 통과에 더하지 않는다 — 이 파일의 전제다."""
    result = ver.summary(db, org_id=org)

    assert set(result["counts"]) == {ver.PASS, ver.FAIL, ver.NOT_VERIFIED}
    assert sum(result["counts"].values()) == len(result["verdicts"])
    assert result["overall"] in ("PASS", "ATTENTION", "FAIL")
