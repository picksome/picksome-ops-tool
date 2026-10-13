"""돈을 바꾸는 모든 경로는 흔적을 남긴다 (Phase 9).

왜 이 파일이 따로 있는가
--------------------
`record_audit` 호출은 각 서비스 안에 흩어져 있고, **빠뜨려도 아무것도 실패하지
않는다.** 기능은 정상 동작하고 테스트도 통과한다. 그래서 새 write 경로가 생길
때마다 조용히 구멍이 난다 — 실제로 `payout.add_item` 이 그랬다(지급 의무를
만드는데 누가 만들었는지 audit 에 없었다).

여기서 각 금전 write 를 **실행하고** `audit_log` 행이 생기는지 확인한다.
코드를 문자열로 검사하지 않는다 — `record_audit` 를 호출하지만 커밋 전에
rollback 되는 경로가 있을 수 있고, 그건 흔적이 없는 것과 같다.

무엇을 요구하는가
--------------
```
domain_table    무엇이 바뀌었나
domain_row_id   어느 행인가
action          만들었나 / 고쳤나 / 지웠나
actor_user_id   누가 했나        ← 이게 없으면 audit 이 아니다
```

없는 것을 요구하지 않는다
--------------------
worker·스캐너가 만드는 파생 행(마진 재계산, 이슈 스캔)은 사용자 행위가 아니다.
사람이 한 일이 아닌 것에 actor 를 요구하면 가짜 actor 를 넣게 된다.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.expense import ExpenseCreate
from app.domain.sales import SaleOrderCreate, SaleOrderUpdate
from app.services import cashflow as cf
from app.services import distribution as dist
from app.services import expense as expense_svc
from app.services import monthly_close as mc
from app.services import payout as payout_svc
from app.services import sales as sales_svc
from app.tests.fixtures.factories import (
    make_app_user,
    make_expense_category_policy,
    make_organization,
    make_partner,
    make_partner_share_policy,
    make_product,
    make_product_margin_actual,
    make_sales_order,
)

YM = "2026-05"
DAY = date(2026, 5, 10)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


def audit_rows(
    db: Session, org: uuid.UUID, table: str
) -> list[dict]:
    return [
        dict(r)
        for r in db.execute(text("""
            SELECT domain_table, domain_row_id, action, actor_user_id, reason
              FROM audit_log
             WHERE org_id = :o AND domain_table = :t
             ORDER BY occurred_at
        """), {"o": str(org), "t": table}).mappings().all()
    ]


def assert_audited(
    db: Session, org: uuid.UUID, table: str, *, action: str, actor: uuid.UUID,
    row_id: uuid.UUID | None = None,
) -> None:
    rows = audit_rows(db, org, table)
    assert rows, f"{table} 에 대한 audit 기록이 없습니다 — 누가 바꿨는지 알 수 없습니다."

    matched = [r for r in rows if r["action"] == action]
    assert matched, (
        f"{table} 의 {action} 이 기록되지 않았습니다. 남은 것: "
        f"{sorted({r['action'] for r in rows})}"
    )
    for r in matched:
        # actor 없는 audit 은 "무엇이" 바뀌었는지만 알려주고 "누가" 를 못 알려준다.
        assert r["actor_user_id"] is not None, f"{table}.{action} 에 actor 가 없습니다."
        assert r["domain_row_id"] is not None, f"{table}.{action} 에 대상 행이 없습니다."
    if row_id is not None:
        assert any(str(r["domain_row_id"]) == str(row_id) for r in matched), (
            f"{table}.{action} 이 다른 행을 가리킵니다."
        )


# ──────────────────────────────────────────────────────────────────────────
# 판매 · 비용 — 손익의 입력
# ──────────────────────────────────────────────────────────────────────────

def test_sales_order_writes_are_audited(db: Session, org: uuid.UUID, actor: uuid.UUID):
    product = make_product(db, org_id=org)
    sale = sales_svc.create_sale(
        db, org_id=org, actor=actor,
        body=SaleOrderCreate(
            sale_date=DAY.isoformat(), marketplace="COUPANG",
            marketplace_order_no="AUD-1", product_id=product,
            quantity=1, sale_price=100_000,
        ),
    )
    assert_audited(db, org, "sales_order", action="CREATE", actor=actor, row_id=sale.id)

    sales_svc.update_sale(
        db, org_id=org, actor=actor, sale_id=sale.id,
        body=SaleOrderUpdate(sale_price=120_000),
    )
    assert_audited(db, org, "sales_order", action="UPDATE", actor=actor, row_id=sale.id)

    sales_svc.delete_sale(db, org_id=org, actor=actor, sale_id=sale.id)
    assert_audited(db, org, "sales_order", action="DELETE", actor=actor, row_id=sale.id)


def test_expense_writes_are_audited(db: Session, org: uuid.UUID, actor: uuid.UUID):
    category = make_expense_category_policy(db, org_id=org, name="감사테스트")
    created = expense_svc.create_expense(
        db, org_id=org, actor=actor,
        body=ExpenseCreate(
            expense_date=DAY, category_id=category, description="사무용품",
            gross_amount=110_000, payment_method="BUSINESS_CARD",
        ),
    )
    assert_audited(db, org, "expenses", action="CREATE", actor=actor, row_id=created.id)

    expense_svc.delete_expense(db, org_id=org, actor=actor, expense_id=created.id)
    assert_audited(db, org, "expenses", action="DELETE", actor=actor, row_id=created.id)


# ──────────────────────────────────────────────────────────────────────────
# 지급 — 누가 얼마를 받는가
# ──────────────────────────────────────────────────────────────────────────

def test_every_payout_step_is_audited(db: Session, org: uuid.UUID, actor: uuid.UUID):
    """지급은 실제로 돈이 나가는 경로다. 어느 단계도 익명일 수 없다.

    특히 **항목 추가**는 "이 사람에게 500만원을 준다" 를 만드는 행위다.
    배치 생성만 기록하고 항목 추가를 빠뜨리면, 금액이 언제 누구에 의해
    들어왔는지 설명할 수 없다.
    """
    batch = payout_svc.create_batch(
        db, org_id=org, actor_user_id=actor, title="감사 배치", period_year_month=YM,
    )
    assert_audited(db, org, "payout_batch", action="CREATE", actor=actor, row_id=batch)

    item = payout_svc.add_item(
        db, org_id=org, actor_user_id=actor, batch_id=batch,
        item_type="PARTNER_DISTRIBUTION", payee_name="파트너B",
        payee_partner_id=make_partner(db, org_id=org, name="파트너B"),
        due_amount=5_000_000, due_date=DAY,
    )
    assert_audited(db, org, "payout_item", action="CREATE", actor=actor, row_id=item)

    payout_svc.approve_batch(db, org_id=org, batch_id=batch, actor_user_id=actor)
    approvals = [
        r for r in audit_rows(db, org, "payout_batch") if r["action"] != "CREATE"
    ]
    assert approvals, "지급 승인이 기록되지 않았습니다 — 누가 승인했는지 알 수 없습니다."
    assert all(r["actor_user_id"] for r in approvals)

    paid = payout_svc.record_payment(
        db, org_id=org, actor_user_id=actor, item_id=item,
        payment_date=DAY, amount=1_000_000,
    )
    assert_audited(db, org, "payout_payment", action="CREATE", actor=actor)

    payout_svc.cancel_payment(
        db, org_id=org, actor_user_id=actor,
        payment_id=uuid.UUID(str(paid["payment_id"])), reason="계좌 오류",
    )
    assert_audited(db, org, "payout_payment", action="DELETE", actor=actor)
    # 취소는 사유가 남아야 한다 — 금액이 왜 줄었는지 설명할 수 있어야 한다.
    assert any(
        r["action"] == "DELETE" and r["reason"]
        for r in audit_rows(db, org, "payout_payment")
    )


# ──────────────────────────────────────────────────────────────────────────
# 현금 — 사람이 직접 넣는 숫자
# ──────────────────────────────────────────────────────────────────────────

def test_cash_records_are_audited(db: Session, org: uuid.UUID, actor: uuid.UUID):
    """수동 현금 기록은 근거가 사람뿐이다. 누가 넣었는지가 유일한 검증 수단이다."""
    cf.set_baseline(
        db, org_id=org, actor_user_id=actor, as_of_date=DAY, amount=10_000_000,
    )
    assert_audited(db, org, "cash_balance_baseline", action="CREATE", actor=actor)

    # 같은 날짜를 다시 넣으면 덮어쓴다 — 그것도 기록에 남아야 한다.
    cf.set_baseline(
        db, org_id=org, actor_user_id=actor, as_of_date=DAY, amount=12_000_000,
    )
    assert_audited(db, org, "cash_balance_baseline", action="UPDATE", actor=actor)

    ev = cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="INFLOW", status="ACTUAL",
        amount=3_000_000, occurred_at=DAY,
    )
    assert_audited(db, org, "manual_cash_event", action="CREATE", actor=actor)

    cf.delete_manual_event(
        db, org_id=org, actor_user_id=actor,
        event_id=uuid.UUID(ev["id"]), reason="중복 입력",
    )
    assert_audited(db, org, "manual_cash_event", action="DELETE", actor=actor)


# ──────────────────────────────────────────────────────────────────────────
# 월 마감 · 배분 — 숫자를 박제하는 순간
# ──────────────────────────────────────────────────────────────────────────

def _month_with_profit(db: Session, org: uuid.UUID) -> None:
    product = make_product(db, org_id=org)
    so = make_sales_order(
        db, org_id=org, product_id=product, sale_date=DAY, sale_price=10_000_000,
    )
    make_product_margin_actual(
        db, org_id=org, product_id=product, sales_order_id=so,
        actual_revenue=10_000_000, actual_cost=4_000_000,
    )
    ms_id = uuid.uuid4()
    db.execute(text("""
        INSERT INTO monthly_settlement
          (id, org_id, business_key, year_month, status, close_state,
           total_revenue, total_direct_cost, total_common_expense, net_profit)
        VALUES (:id, :o, :bk, :ym, 'DRAFT', 'OPEN', 0, 0, 0, 0)
    """), {"id": str(ms_id), "o": str(org), "bk": f"MS-{ms_id.hex[:10]}", "ym": YM})
    db.commit()


def test_month_close_transitions_are_audited(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """확정·잠금은 숫자를 되돌릴 수 없게 만드는 행위다."""
    _month_with_profit(db, org)

    mc.confirm(db, org_id=org, year_month=YM, actor_user_id=actor,
               acknowledged_warnings=[c.code for c in mc.validate_month(
                   db, org_id=org, year_month=YM) if not c.blocking])
    assert_audited(db, org, "monthly_settlement", action="STATUS_CHANGE", actor=actor)

    mc.lock(db, org_id=org, year_month=YM, actor_user_id=actor)
    # 확정과 잠금이 둘 다 남아야 한다 — 잠금은 되돌릴 수 없는 전이다.
    states = [
        r for r in audit_rows(db, org, "monthly_settlement")
        if r["action"] == "STATUS_CHANGE"
    ]
    assert len(states) >= 2


def test_distribution_run_is_audited(db: Session, org: uuid.UUID, actor: uuid.UUID):
    """누가 얼마를 받는지 계산한 시점과 사람이 남아야 한다."""
    _month_with_profit(db, org)
    partner = make_partner(db, org_id=org, name="파트너B")
    make_partner_share_policy(
        db, org_id=org, partner_id=partner, share_ratio=Decimal("1.0"),
    )
    mc.confirm(db, org_id=org, year_month=YM, actor_user_id=actor,
               acknowledged_warnings=[c.code for c in mc.validate_month(
                   db, org_id=org, year_month=YM) if not c.blocking])

    dist.create_run(db, org_id=org, year_month=YM, actor_user_id=actor)
    assert_audited(db, org, "distribution_run", action="CREATE", actor=actor)


# ──────────────────────────────────────────────────────────────────────────
# 격리
# ──────────────────────────────────────────────────────────────────────────

def test_audit_rows_carry_the_acting_org(db: Session, org: uuid.UUID, actor: uuid.UUID):
    """감사 로그가 조직을 안 달면 다른 조직의 활동이 섞여 보인다."""
    other = make_organization(db)
    cf.set_baseline(
        db, org_id=org, actor_user_id=actor, as_of_date=DAY, amount=1_000_000,
    )

    assert audit_rows(db, other, "cash_balance_baseline") == []
