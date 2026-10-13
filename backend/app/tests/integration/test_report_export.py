"""세무사·운영 리포트 export (Phase 15).

왜 이 파일이 있나
--------------
리포트 12종이 있었지만 **아무 테스트도 없었다.** 이 파일들은 세무사에게 그대로
건네지는 산출물이다. 컬럼 하나가 비거나 SQL 이 깨져도 아무도 모르고, 알게 되는
시점은 세무사가 "이거 왜 비었냐" 고 물을 때다.

무엇을 고정하나
------------
1. **선언된 모든 종류가 실제로 만들어진다** — 목록에만 있고 SQL 이 없는 종류가
   조용히 빈 파일을 주지 않는다
2. 컬럼명이 한국어다 (CLAUDE.md: 세무사 제출용)
3. 실제 데이터가 파일에 들어간다
4. 다른 조직 데이터가 섞이지 않는다
5. **반출이 감사에 남는다** — 누가 언제 무엇을 밖으로 가져갔는지
"""

from __future__ import annotations

import io
import uuid
from datetime import date

import pytest
from openpyxl import load_workbook
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.io.excel_export.tax_reports import REPORT_LABELS, _fetch, build_report
from app.services import cashflow as cf
from app.services import payout as payout_svc
from app.services.reports import TaxReportRequest
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_partner,
    make_product,
    make_sales_order,
)

START = date(2026, 5, 1)
END = date(2026, 5, 31)

# 아직 SQL 이 없는 종류. **목록에서 빼지 않고 여기 적는다** — 화면에 노출되는
# 종류와 구현된 종류가 어긋나면 사용자가 빈 파일을 받는다.
NOT_IMPLEMENTED = {"personal_income_summary", "business_income"}


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


def _req(org: uuid.UUID, kind: str) -> TaxReportRequest:
    return TaxReportRequest(
        org_id=org, kind=kind, period_start=START, period_end=END,  # type: ignore[arg-type]
    )


def _sheet(org: uuid.UUID, kind: str, db: Session):
    file = build_report(_req(org, kind), db)
    wb = load_workbook(io.BytesIO(file.content))
    return wb.active, file


# 미구현 종류는 아래 `test_unimplemented_kinds_are_refused_not_emptied` 가
# 따로 본다 — 여기서는 **만들 수 있다고 선언한 것**만 실제로 만들어지는지 본다.
@pytest.mark.parametrize("kind", sorted(set(REPORT_LABELS) - NOT_IMPLEMENTED))
def test_every_declared_report_kind_builds(db: Session, org: uuid.UUID, kind: str):
    """선언된 종류는 전부 만들어져야 한다.

    데이터가 없어도 헤더는 나와야 한다 — 빈 파일과 "만들 줄 모르는 파일" 은 다르다.
    """
    ws, file = _sheet(org, kind, db)
    headers = [c.value for c in ws[1]]

    assert file.file_name.endswith(".xlsx")
    assert REPORT_LABELS[kind] in file.file_name
    assert headers, f"{kind}: 헤더가 비었습니다"
    assert "TODO" not in str(headers[0]), f"{kind}: SQL 이 없습니다"
    # CLAUDE.md — 세무사 제출용이므로 컬럼명은 한국어다.
    assert any(
        any("가" <= ch <= "힣" for ch in str(h)) for h in headers
    ), f"{kind}: 한국어 컬럼명이 없습니다"


@pytest.mark.parametrize("kind", sorted(NOT_IMPLEMENTED))
def test_unimplemented_kinds_are_refused_not_emptied(
    db: Session, org: uuid.UUID, kind: str,
):
    """빈 파일을 주지 않는다.

    예전에는 `TODO — 리포트 SQL 미구현` 이라는 헤더 한 줄짜리 엑셀을 돌려줬다.
    사용자는 파일을 받았으니 만들어진 줄 알고 **세무사에게 그대로 넘길 수
    있다.** 세무 자료에서 그건 빈 표보다 나쁘다.
    """
    with pytest.raises(ValidationError):
        build_report(_req(org, kind), db)


def test_unimplemented_kinds_are_marked_unavailable():
    """목록에서 지우지 않고 `available=false` 로 표시한다.

    지우면 "왜 종합소득세 리포트가 없지" 를 아무도 답하지 못하고 사용자는
    우리가 잊었다고 생각한다.
    """
    from app.io.excel_export.tax_reports import NOT_IMPLEMENTED_KINDS

    # 라벨은 남아 있어야 화면이 "제공 예정" 으로 보여줄 수 있다.
    for kind in NOT_IMPLEMENTED_KINDS:
        assert kind in REPORT_LABELS


def test_sales_report_contains_the_actual_rows(db: Session, org: uuid.UUID):
    product = make_product(db, org_id=org)
    make_sales_order(
        db, org_id=org, product_id=product, sale_date=date(2026, 5, 10),
        sale_price=123_456, marketplace="COUPANG",
    )
    db.commit()

    ws, file = _sheet(org, "monthly_sales", db)
    values = [c.value for c in ws[2]]
    assert file.row_count == 1
    assert 123_456 in values


def test_reports_do_not_leak_across_orgs(db: Session, org: uuid.UUID):
    other = make_organization(db)
    make_sales_order(
        db, org_id=other, product_id=make_product(db, org_id=other),
        sale_date=date(2026, 5, 10), sale_price=9_999_000,
    )
    db.commit()

    _, file = _sheet(org, "monthly_sales", db)
    assert file.row_count == 0


def test_rows_outside_the_period_are_excluded(db: Session, org: uuid.UUID):
    product = make_product(db, org_id=org)
    make_sales_order(
        db, org_id=org, product_id=product, sale_date=date(2026, 4, 30),
        sale_price=500_000,
    )
    db.commit()

    _, file = _sheet(org, "monthly_sales", db)
    assert file.row_count == 0


# ──────────────────────────────────────────────────────────────────────────
# 새 운영 리포트 (Phase 15)
# ──────────────────────────────────────────────────────────────────────────

def test_payout_ledger_shows_obligation_and_payment_together(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """지급 의무와 실제 지급을 한 장에 놓는다.

    따로 보면 "승인은 했는데 아직 안 줬다" 를 알아채기 어렵다.
    """
    batch = payout_svc.create_batch(
        db, org_id=org, actor_user_id=actor, title="5월 배분", period_year_month="2026-05",
    )
    item = payout_svc.add_item(
        db, org_id=org, actor_user_id=actor, batch_id=batch,
        item_type="PARTNER_DISTRIBUTION", payee_name="파트너B",
        payee_partner_id=make_partner(db, org_id=org, name="파트너B"),
        due_amount=4_000_000, due_date=date(2026, 5, 20),
    )
    payout_svc.approve_batch(db, org_id=org, batch_id=batch, actor_user_id=actor)
    payout_svc.record_payment(
        db, org_id=org, actor_user_id=actor, item_id=item,
        payment_date=date(2026, 5, 15), amount=1_000_000,
    )

    ws, file = _sheet(org, "payout_ledger", db)
    assert file.row_count == 1
    row = dict(zip([c.value for c in ws[1]], [c.value for c in ws[2]], strict=True))
    assert row["지급예정액"] == 4_000_000
    assert row["지급완료액"] == 1_000_000
    # 미지급 잔액이 없으면 이 파일로 "얼마가 남았나" 를 답할 수 없다.
    assert row["미지급잔액"] == 3_000_000
    assert row["상태"] == "PARTIALLY_PAID"


def test_cashflow_report_uses_the_same_read_model_as_the_screen(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """export 전용 SQL 을 따로 쓰면 화면과 파일이 다른 숫자를 말한다.

    그 순간 둘 다 신뢰를 잃는다 — 어느 쪽이 맞는지 사용자가 알 방법이 없다.
    """
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="INFLOW", status="ACTUAL",
        amount=3_000_000, occurred_at=date(2026, 5, 8), category="쿠팡 정산 입금",
    )

    ws, file = _sheet(org, "cashflow", db)
    assert file.row_count == 1
    row = dict(zip([c.value for c in ws[1]], [c.value for c in ws[2]], strict=True))
    assert row["금액"] == 3_000_000
    assert row["구분"] == "유입"
    # 근거 없는 숫자를 내보내지 않는다 — 받는 사람이 검토할 수 없다.
    assert row["근거"] == "실제"
    assert row["출처"] == "manual_cash_event"


def test_cashflow_report_never_reports_a_settlement_as_actual(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """파일에서도 `settled_at` 을 실제 입금이라고 쓰지 않는다 (docs/54 §3-2).

    화면에서만 조심하고 파일에서 놓치면, 세무사가 받는 쪽이 틀린 것이 된다.
    """
    rows, _ = _fetch(_req(org, "cashflow"), db)
    assert all(r[2] != "실제" or r[5] != "marketplace_settlement_record" for r in rows)


def test_operation_issue_report_carries_the_resolution_note(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    db.execute(text("""
        INSERT INTO operation_issue
          (org_id, issue_type, severity, status, title, dedupe_key,
           affected_count, created_at)
        VALUES (:o, 'MISSING_EVIDENCE', 'WARNING', 'OPEN', '증빙 누락 3건',
                'T:MISSING', 3, :d)
    """), {"o": str(org), "d": date(2026, 5, 12)})
    db.commit()

    ws, file = _sheet(org, "operation_issues", db)
    assert file.row_count == 1
    row = dict(zip([c.value for c in ws[1]], [c.value for c in ws[2]], strict=True))
    assert row["이슈유형"] == "MISSING_EVIDENCE"
    assert row["심각도"] == "WARNING"
    assert row["건수"] == 3


# ──────────────────────────────────────────────────────────────────────────
# 반출 기록
# ──────────────────────────────────────────────────────────────────────────

def test_export_is_recorded_in_the_audit_log(db: Session, org: uuid.UUID, actor: uuid.UUID):
    """외부로 나가는 자료는 누가 언제 무엇을 가져갔는지 남아야 한다."""
    from fastapi.testclient import TestClient

    from app.core.security import create_access_token
    from app.deps import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            res = client.post(
                "/api/v1/reports/export",
                json={
                    "kind": "cashflow",
                    "period_start": START.isoformat(),
                    "period_end": END.isoformat(),
                    "format": "xlsx",
                },
                headers={
                    "Authorization": "Bearer "
                    + create_access_token(user_id=actor, org_id=org, role="ADMIN")
                },
            )
            assert res.status_code == 200, res.text
    finally:
        app.dependency_overrides.clear()

    rows = db.execute(text("""
        SELECT action, after_json FROM audit_log
         WHERE org_id = :o AND domain_table = 'tax_report_export'
    """), {"o": str(org)}).mappings().all()
    assert len(rows) == 1
    assert rows[0]["action"] == "EXPORT"
    assert rows[0]["after_json"]["kind"] == "cashflow"
