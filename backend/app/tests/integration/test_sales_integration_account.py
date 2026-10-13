"""주문의 판매자 계정 귀속 (M032, Phase 5).

무엇을 고정하나
------------
1. 주문이 어느 계정에서 왔는지 저장된다
2. **같은 판매처의 다른 계정 주문이 섞이지 않는다** — 이 컬럼의 존재 이유다
3. 없는 계정·남의 조직 계정·삭제된 계정은 거절한다
4. legacy NULL 주문이 조회에서 사라지지 않는다
5. ingest 는 **실행 계정**을 귀속시킨다(페이로드가 주장하는 계정이 아니라)
6. 계정을 비활성화해도 과거 주문이 살아 있다
7. 마감 snapshot·대시보드 숫자가 이 컬럼 때문에 달라지지 않는다
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.domain.sales import SaleOrderCreate, SaleOrderUpdate
from app.services import close_snapshot
from app.services import dashboard as dash
from app.services.integration_ingest import ingest_orders
from app.services.sales import create_sale, get_sale, list_sales, update_sale
from app.tests.fixtures.factories import (
    make_app_user,
    make_integration_account,
    make_organization,
    make_product,
    make_sales_order,
)

YM = "2026-03"
SALE_DAY = date(2026, 3, 10)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


@pytest.fixture
def coupang_accounts(db: Session, org: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """같은 판매처(쿠팡)의 서로 다른 스토어 두 개."""
    main = make_integration_account(db, org_id=org, provider="COUPANG", account_label="본점")
    second = make_integration_account(db, org_id=org, provider="COUPANG", account_label="2호점")
    return main, second


def _body(order_no: str, *, account_id: uuid.UUID | None = None, price: int = 100_000):
    return SaleOrderCreate(
        sale_date=SALE_DAY.isoformat(),
        marketplace="COUPANG",
        marketplace_order_no=order_no,
        product_snapshot_name="상품",
        quantity=1,
        sale_price=price,
        integration_account_id=account_id,
    )


# ──────────────────────────────────────────────────────────────────────────
# 저장 / 귀속
# ──────────────────────────────────────────────────────────────────────────

def test_create_stores_the_integration_account(
    db: Session, org: uuid.UUID, actor: uuid.UUID, coupang_accounts
):
    main, _ = coupang_accounts
    out = create_sale(db, org_id=org, actor=actor, body=_body("ORD-1", account_id=main))
    assert out.integration_account_id == main

    stored = db.execute(text("""
        SELECT integration_account_id FROM sales_order WHERE id = :i
    """), {"i": str(out.id)}).scalar()
    assert uuid.UUID(str(stored)) == main


def test_orders_from_two_accounts_on_the_same_channel_do_not_mix(
    db: Session, org: uuid.UUID, actor: uuid.UUID, coupang_accounts
):
    """이 컬럼이 존재하는 이유.

    본점과 2호점은 둘 다 `marketplace='COUPANG'` 이다. 채널 문자열만으로는
    "쿠팡 매출 30만" 까지만 말할 수 있고 스토어별로 나눌 수 없다.
    """
    main, second = coupang_accounts
    create_sale(db, org_id=org, actor=actor, body=_body("ORD-A", account_id=main, price=200_000))
    create_sale(db, org_id=org, actor=actor, body=_body("ORD-B", account_id=second, price=100_000))
    db.commit()

    from_main = list_sales(db, org_id=org, integration_account_id=main)
    from_second = list_sales(db, org_id=org, integration_account_id=second)

    assert from_main["total"] == 1
    assert from_second["total"] == 1
    assert from_main["items"][0].marketplace_order_no == "ORD-A"
    assert from_second["items"][0].marketplace_order_no == "ORD-B"

    # 채널 필터로는 둘 다 나온다 — 계정 필터가 있어야 나뉜다.
    assert list_sales(db, org_id=org, marketplace="COUPANG")["total"] == 2


def test_account_can_be_changed_and_cleared(
    db: Session, org: uuid.UUID, actor: uuid.UUID, coupang_accounts
):
    """잘못 붙인 계정을 되돌릴 수 있어야 한다."""
    main, second = coupang_accounts
    out = create_sale(db, org_id=org, actor=actor, body=_body("ORD-C", account_id=main))

    moved = update_sale(
        db, org_id=org, actor=actor, sale_id=out.id,
        body=SaleOrderUpdate(integration_account_id=second),
    )
    assert moved.integration_account_id == second

    cleared = update_sale(
        db, org_id=org, actor=actor, sale_id=out.id,
        body=SaleOrderUpdate(integration_account_id=None),
    )
    assert cleared.integration_account_id is None


# ──────────────────────────────────────────────────────────────────────────
# 거절
# ──────────────────────────────────────────────────────────────────────────

def test_unknown_account_is_rejected(db: Session, org: uuid.UUID, actor: uuid.UUID):
    with pytest.raises(ValidationError):
        create_sale(db, org_id=org, actor=actor, body=_body("ORD-X", account_id=uuid.uuid4()))


def test_other_orgs_account_is_rejected(db: Session, org: uuid.UUID, actor: uuid.UUID):
    """조직 격리. 남의 계정 매출로 잡히면 그 조직 손익이 오염된다."""
    other = make_organization(db)
    foreign = make_integration_account(db, org_id=other, provider="COUPANG")

    with pytest.raises(ValidationError):
        create_sale(db, org_id=org, actor=actor, body=_body("ORD-Y", account_id=foreign))


def test_marketplace_must_match_the_account(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """쿠팡 주문을 네이버 계정에 붙일 수 없다 (D12).

    이게 뚫리면 정산 대사와 스토어별 손익이 통째로 틀어지고, 나중에는
    어느 쪽이 맞는지 알 방법이 없다. FK 만으로는 막지 못한다 —
    계정 id 는 유효하고 조직도 같기 때문이다.
    """
    naver = make_integration_account(
        db, org_id=org, provider="SMARTSTORE", account_label="네이버 스토어",
    )

    with pytest.raises(ValidationError) as err:
        create_sale(db, org_id=org, actor=actor,
                    body=_body("ORD-MISMATCH", account_id=naver))
    assert "맞지 않습니다" in str(err.value)

    assert db.execute(text("""
        SELECT COUNT(*) FROM sales_order
         WHERE org_id = :o AND marketplace_order_no = 'ORD-MISMATCH'
    """), {"o": str(org)}).scalar() == 0


def test_matching_marketplace_is_accepted(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """같은 판매처면 통과한다 — 검사가 과하게 막지 않는지 확인한다."""
    naver = make_integration_account(
        db, org_id=org, provider="SMARTSTORE", account_label="네이버 스토어",
    )
    body = SaleOrderCreate(
        sale_date=SALE_DAY.isoformat(), marketplace="SMARTSTORE",
        marketplace_order_no="ORD-NAVER", product_snapshot_name="상품",
        quantity=1, sale_price=50_000, integration_account_id=naver,
    )
    out = create_sale(db, org_id=org, actor=actor, body=body)
    assert out.integration_account_id == naver


def test_changing_marketplace_alone_cannot_break_the_match(
    db: Session, org: uuid.UUID, actor: uuid.UUID, coupang_accounts
):
    """계정은 그대로 두고 판매처만 바꿔서 불일치를 만들 수 없다.

    검증을 생성 시점에만 두면 이 경로로 빠져나간다.
    """
    main, _ = coupang_accounts
    out = create_sale(db, org_id=org, actor=actor, body=_body("ORD-SWAP", account_id=main))

    with pytest.raises(ValidationError):
        update_sale(db, org_id=org, actor=actor, sale_id=out.id,
                    body=SaleOrderUpdate(marketplace="SMARTSTORE"))


def test_changing_both_marketplace_and_account_together_is_allowed(
    db: Session, org: uuid.UUID, actor: uuid.UUID, coupang_accounts
):
    """판매처와 계정을 함께 옮기는 것은 정상이다 — 바뀐 뒤 값으로 검증해야 한다."""
    main, _ = coupang_accounts
    naver = make_integration_account(
        db, org_id=org, provider="SMARTSTORE", account_label="네이버",
    )
    out = create_sale(db, org_id=org, actor=actor, body=_body("ORD-MOVE", account_id=main))

    moved = update_sale(
        db, org_id=org, actor=actor, sale_id=out.id,
        body=SaleOrderUpdate(marketplace="SMARTSTORE", integration_account_id=naver),
    )
    assert moved.marketplace == "SMARTSTORE"
    assert moved.integration_account_id == naver


def test_ingest_rejects_an_item_whose_marketplace_differs_from_the_key(
    db: Session, org: uuid.UUID, coupang_accounts
):
    """쿠팡 키로 스마트스토어 주문을 밀어 넣을 수 없다.

    항목 단위 실패이므로 배치 전체가 죽지는 않는다 — 그 항목만 실패로 기록된다.
    """
    main, _ = coupang_accounts
    result = ingest_orders(
        db, org_id=org, account_id=main, source_type="BOT_AUTO",
        items=[{
            "sale_date": SALE_DAY.isoformat(),
            "marketplace": "SMARTSTORE",
            "marketplace_order_no": "BOT-WRONG",
            "quantity": 1,
            "sale_price": 10_000,
        }],
    )
    assert result.failed_count == 1
    assert result.success_count == 0
    assert db.execute(text("""
        SELECT COUNT(*) FROM sales_order
         WHERE org_id = :o AND marketplace_order_no = 'BOT-WRONG'
    """), {"o": str(org)}).scalar() == 0


def test_deleted_account_is_rejected(db: Session, org: uuid.UUID, actor: uuid.UUID):
    """지워진 계정에 새 주문을 붙이면 출처를 화면에서 설명할 수 없다."""
    gone = make_integration_account(db, org_id=org, provider="COUPANG", account_label="폐점")
    db.execute(text("UPDATE integration_account SET deleted_at = now() WHERE id = :i"),
               {"i": str(gone)})

    with pytest.raises(ValidationError):
        create_sale(db, org_id=org, actor=actor, body=_body("ORD-Z", account_id=gone))


def test_db_rejects_an_account_that_does_not_exist(db: Session, org: uuid.UUID):
    """서비스를 우회해도 FK 가 막는다."""
    with pytest.raises(IntegrityError):
        make_sales_order(
            db, org_id=org, product_id=make_product(db, org_id=org),
            sale_date=SALE_DAY, sale_price=1000,
            integration_account_id=uuid.uuid4(),
        )
    db.rollback()


# ──────────────────────────────────────────────────────────────────────────
# legacy / 비활성
# ──────────────────────────────────────────────────────────────────────────

def test_legacy_orders_without_an_account_are_still_listed(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """NULL 은 '모른다' 이지 '없는 주문' 이 아니다.

    조회에서 조용히 빠지면 매출이 줄어 보인다. `_SELECT` 가 LEFT JOIN 인 이유다.
    """
    out = create_sale(db, org_id=org, actor=actor, body=_body("ORD-LEGACY"))
    db.commit()

    assert out.integration_account_id is None
    assert list_sales(db, org_id=org)["total"] == 1
    assert get_sale(db, org_id=org, sale_id=out.id).integration_account_label is None


def test_deactivated_account_keeps_its_past_orders_readable(
    db: Session, org: uuid.UUID, actor: uuid.UUID, coupang_accounts
):
    """계정을 멈춰도 지난 매출은 남아야 한다."""
    main, _ = coupang_accounts
    out = create_sale(db, org_id=org, actor=actor, body=_body("ORD-OLD", account_id=main))
    db.commit()

    db.execute(text("UPDATE integration_account SET status = 'INACTIVE' WHERE id = :i"),
               {"i": str(main)})
    db.commit()

    fetched = get_sale(db, org_id=org, sale_id=out.id)
    assert fetched.integration_account_id == main
    assert fetched.integration_account_label == "본점"
    assert list_sales(db, org_id=org, integration_account_id=main)["total"] == 1


def test_account_label_change_does_not_move_the_order(
    db: Session, org: uuid.UUID, actor: uuid.UUID, coupang_accounts
):
    """이름이 바뀌어도 귀속은 id 로 남는다 — 확정된 금액 계산에 영향이 없다."""
    main, _ = coupang_accounts
    out = create_sale(db, org_id=org, actor=actor, body=_body("ORD-REN", account_id=main))
    db.commit()

    db.execute(text("UPDATE integration_account SET account_label = '본점(구)' WHERE id = :i"),
               {"i": str(main)})
    db.commit()

    fetched = get_sale(db, org_id=org, sale_id=out.id)
    assert fetched.integration_account_id == main
    assert fetched.integration_account_label == "본점(구)"


# ──────────────────────────────────────────────────────────────────────────
# ingest — 실행 계정이 귀속 대상이다
# ──────────────────────────────────────────────────────────────────────────

def test_ingest_binds_orders_to_the_executing_account(
    db: Session, org: uuid.UUID, coupang_accounts
):
    main, _ = coupang_accounts
    result = ingest_orders(
        db, org_id=org, account_id=main, source_type="BOT_AUTO",
        items=[{
            "sale_date": SALE_DAY.isoformat(),
            "marketplace": "COUPANG",
            "marketplace_order_no": "BOT-1",
            "quantity": 1,
            "sale_price": 50_000,
            "product_snapshot_name": "봇 상품",
        }],
    )
    assert result.success_count == 1

    stored = db.execute(text("""
        SELECT integration_account_id FROM sales_order
         WHERE org_id = :o AND marketplace_order_no = 'BOT-1'
    """), {"o": str(org)}).scalar()
    assert uuid.UUID(str(stored)) == main


def test_ingest_ignores_an_account_id_claimed_in_the_payload(
    db: Session, org: uuid.UUID, coupang_accounts
):
    """봇이 남의 계정을 주장해도 실행 계정으로 귀속된다.

    페이로드를 믿으면 한 계정의 키를 가진 봇이 다른 계정 매출을 만들 수 있다.
    """
    main, second = coupang_accounts
    ingest_orders(
        db, org_id=org, account_id=main, source_type="BOT_AUTO",
        items=[{
            "sale_date": SALE_DAY.isoformat(),
            "marketplace": "COUPANG",
            "marketplace_order_no": "BOT-2",
            "quantity": 1,
            "sale_price": 50_000,
            # 봇이 2호점이라고 주장한다 — 무시돼야 한다.
            "integration_account_id": str(second),
        }],
    )

    stored = db.execute(text("""
        SELECT integration_account_id FROM sales_order
         WHERE org_id = :o AND marketplace_order_no = 'BOT-2'
    """), {"o": str(org)}).scalar()
    assert uuid.UUID(str(stored)) == main


# ──────────────────────────────────────────────────────────────────────────
# 회귀 — 집계 숫자가 달라지지 않는다
# ──────────────────────────────────────────────────────────────────────────

def _seed_month(db: Session, org: uuid.UUID, *, account_id: uuid.UUID | None) -> None:
    """같은 금액의 판매 2건. 계정 귀속만 다르게 준다."""
    product = make_product(db, org_id=org)
    for i, price in enumerate((300_000, 100_000), start=1):
        so = make_sales_order(
            db, org_id=org, product_id=product, sale_date=SALE_DAY,
            sale_price=price, purchase_cost=price // 2,
            marketplace_order_no=f"AGG-{i}",
            integration_account_id=account_id,
        )
        db.execute(text("""
            INSERT INTO product_margin_actual
              (id, org_id, product_id, sales_order_id, actual_revenue, actual_cost,
               actual_marketplace_fee, actual_payment_fee, actual_ad_cost,
               actual_fulfillment_fee, actual_return_cost, actual_vat,
               actual_shipping_cost, actual_margin_amount, actual_margin_rate)
            VALUES (gen_random_uuid(), :o, :p, :so, :rev, :cost, 0,0,0,0,0,0,0, :m, 0)
        """), {"o": str(org), "p": str(product), "so": str(so),
               "rev": price, "cost": price // 2, "m": price - price // 2})


def test_dashboard_numbers_are_identical_with_and_without_an_account(
    db: Session, coupang_accounts, org: uuid.UUID
):
    """계정 FK 추가로 집계가 달라지면 안 된다.

    특히 **legacy NULL 주문이 몰래 빠지지 않는지**를 본다.
    """
    main, _ = coupang_accounts
    _seed_month(db, org, account_id=main)

    legacy_org = make_organization(db)
    _seed_month(db, legacy_org, account_id=None)
    db.commit()

    with_account = dash.build_dashboard(db, org_id=org, year_month=YM, trend_months=1)
    without = dash.build_dashboard(db, org_id=legacy_org, year_month=YM, trend_months=1)

    def kpi(data, key):
        return next(k for k in data["kpis"] if k["key"] == key)["value"]

    assert kpi(with_account, "gross_sales") == kpi(without, "gross_sales") == 400_000
    assert kpi(with_account, "net_profit") == kpi(without, "net_profit") == 200_000
    # 판매처 분해 합계도 그대로다 — 계정이 없다고 빠지지 않는다.
    assert sum(m["gross_sales"] for m in without["by_marketplace"]) == 400_000


def test_close_snapshot_figures_are_unchanged_by_the_account_column(
    db: Session, org: uuid.UUID, coupang_accounts
):
    """마감 확정 숫자가 계정 귀속과 무관해야 한다."""
    main, _ = coupang_accounts
    _seed_month(db, org, account_id=main)
    db.commit()

    before = close_snapshot.compute_figures(db, org_id=org, year_month=YM)

    # 귀속을 지운다 — 금액은 그대로여야 한다.
    db.execute(text("""
        UPDATE sales_order SET integration_account_id = NULL WHERE org_id = :o
    """), {"o": str(org)})
    db.commit()

    after = close_snapshot.compute_figures(db, org_id=org, year_month=YM)
    assert before["gross_sales"] == after["gross_sales"] == 400_000
    assert before["net_profit"] == after["net_profit"]
    assert before["content_hash"] == after["content_hash"]


# ──────────────────────────────────────────────────────────────────────────
# backfill 정책
# ──────────────────────────────────────────────────────────────────────────

def test_backfill_never_guesses_from_the_channel_string(
    db: Session, org: uuid.UUID, coupang_accounts
):
    """M032 의 backfill 은 채널 문자열로 계정을 고르지 않는다.

    쿠팡 계정이 하나뿐이어도 "쿠팡 주문이니 이 계정" 은 추측이다 —
    엑셀로 넣은 과거 주문일 수 있다. 그 추측이 확정 snapshot 에 박제되면
    나중에 진짜 계정을 알게 돼도 되돌릴 수 없다.
    """
    main, _ = coupang_accounts
    legacy = make_sales_order(
        db, org_id=org, product_id=make_product(db, org_id=org),
        sale_date=SALE_DAY, sale_price=10_000, marketplace="COUPANG",
    )
    db.commit()

    # migration 의 backfill 구문을 그대로 다시 돌린다.
    db.execute(text("""
        UPDATE sales_order so
           SET integration_account_id = ij.account_id
          FROM batch_run br
          JOIN integration_job ij ON ij.id = br.job_id
         WHERE so.import_batch_id = br.id
           AND so.integration_account_id IS NULL
           AND ij.account_id IS NOT NULL
           AND EXISTS (
                 SELECT 1 FROM integration_account ia
                  WHERE ia.id = ij.account_id AND ia.org_id = so.org_id
               )
    """))
    db.commit()

    still_null = db.execute(text("""
        SELECT integration_account_id FROM sales_order WHERE id = :i
    """), {"i": str(legacy)}).scalar()
    assert still_null is None, "채널만 보고 계정을 추측해 채웠다"
    assert main is not None  # 계정이 존재해도 채우지 않는다는 것이 요점이다


# ──────────────────────────────────────────────────────────────────────────
# 데이터 품질 검사 (Phase 8) — 계정 귀속
#
# **legacy NULL 을 무조건 오류로 만들지 않는다.** 출처에 따라 등급이 다르다.
# ──────────────────────────────────────────────────────────────────────────

def _codes(db: Session, org: uuid.UUID) -> dict[str, str]:
    from app.services import close_checks

    items = close_checks.run_checks(db, org_id=org, year_month=YM)
    return {i.code: i.grade for i in items}


def _sale_with_source(
    db: Session, org: uuid.UUID, *, order_no: str, import_source: str,
    account_id: uuid.UUID | None = None, marketplace: str = "COUPANG",
) -> uuid.UUID:
    sid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO sales_order
          (id, org_id, business_key, sale_date, marketplace, marketplace_order_no,
           line_no, product_id, quantity, sale_price, purchase_cost, import_source,
           integration_account_id)
        VALUES (:id, :o, :bk, :d, :mk, :ono, 1, :p, 1, 10000, 5000, :src, :acc)
    """), {"id": str(sid), "o": str(org), "bk": f"SO-{sid.hex[:10]}",
           "d": SALE_DAY, "mk": marketplace, "ono": order_no,
           "p": str(make_product(db, org_id=org)), "src": import_source,
           "acc": str(account_id) if account_id else None})
    return sid


def test_manual_order_without_an_account_is_only_info(db: Session, org: uuid.UUID):
    """엑셀·수동 주문의 계정 미상은 참고 정보다.

    이걸 경고로 올리면 legacy 주문 수백 건 때문에 사용자가 마감 검증 전체를
    무시하게 된다. 손익 계산에는 영향이 없다.
    """
    _sale_with_source(db, org, order_no="MANUAL-1", import_source="EXCEL_IMPORT")
    codes = _codes(db, org)

    assert codes.get("SALE_ACCOUNT_UNASSIGNED") == "INFO"
    assert "SALE_ACCOUNT_MISSING" not in codes


def test_auto_collected_order_without_an_account_is_a_warning(
    db: Session, org: uuid.UUID
):
    """자동 수집은 실행 계정을 알고 있어야 한다 — 모르면 수집 코드 문제다."""
    _sale_with_source(db, org, order_no="BOT-1", import_source="BOT_AUTO")
    codes = _codes(db, org)

    assert codes.get("SALE_ACCOUNT_MISSING") == "WARNING"
    assert "SALE_ACCOUNT_UNASSIGNED" not in codes


def test_marketplace_mismatch_is_blocking_in_the_scanner(
    db: Session, org: uuid.UUID
):
    """서비스를 우회해 들어온 불일치는 마감을 막는다.

    이 상태의 달을 확정하면 틀린 귀속이 snapshot 에 박제된다.
    """
    naver = make_integration_account(
        db, org_id=org, provider="SMARTSTORE", account_label="네이버",
    )
    # 서비스 검증을 우회해 직접 넣는다 — 과거에 들어온 데이터를 재현한다.
    _sale_with_source(db, org, order_no="BAD-1", import_source="EXCEL_IMPORT",
                      account_id=naver, marketplace="COUPANG")

    assert _codes(db, org).get("SALE_ACCOUNT_MARKETPLACE_MISMATCH") == "BLOCKING"


def test_cross_org_account_is_blocking_in_the_scanner(db: Session, org: uuid.UUID):
    other = make_organization(db)
    foreign = make_integration_account(db, org_id=other, provider="COUPANG")
    _sale_with_source(db, org, order_no="XORG-1", import_source="EXCEL_IMPORT",
                      account_id=foreign)

    assert _codes(db, org).get("SALE_ACCOUNT_ORG_MISMATCH") == "BLOCKING"


def test_correctly_bound_order_raises_no_account_issue(
    db: Session, org: uuid.UUID, coupang_accounts
):
    """정상 귀속은 아무 이슈도 만들지 않는다 — 검사가 과하게 울리지 않는지 본다."""
    main, _ = coupang_accounts
    _sale_with_source(db, org, order_no="OK-1", import_source="BOT_AUTO",
                      account_id=main)
    codes = _codes(db, org)

    for code in ("SALE_ACCOUNT_MISSING", "SALE_ACCOUNT_UNASSIGNED",
                 "SALE_ACCOUNT_ORG_MISMATCH", "SALE_ACCOUNT_MARKETPLACE_MISMATCH"):
        assert code not in codes, f"{code} 가 잘못 울렸다"
