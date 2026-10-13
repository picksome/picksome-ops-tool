"""세무사 export 리포트 (D-282 MVP 7종).

CLAUDE.md Tax Report: 프로그램은 세무신고를 대체하지 않음 — 세무사 검토용 자료 제공.
모든 리포트 컬럼명은 한국어. 파일명: `YYYY-MM_<리포트명>.<ext>`.

MVP 필수 종류:
  monthly_sales / monthly_expense / monthly_settlement / evidence_missing
  / vat_quarterly / personal_income_summary / business_income

확장 (ReportKind Literal):
  by_marketplace / by_product_pl / coupang_reconcile / partner_distribution / monthly_purchase

실제 SQL/엑셀 빌더는 io/excel_export/tax_reports.build_report 에서 구현.
본 모듈은 시그니처 + dispatch 만 (라우터 결합도 분리).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

ReportKind = Literal[
    "monthly_sales",
    "monthly_expense",
    "monthly_settlement",
    "evidence_missing",
    "vat_quarterly",
    "personal_income_summary",
    "business_income",
    "by_marketplace",
    "by_product_pl",
    "coupang_reconcile",
    "partner_distribution",
    "monthly_purchase",
    # 운영 리포트 (Phase 15). 세무 제출용은 아니지만 같은 export 경로를 쓴다 —
    # 반출 기록(audit)과 파일명 규칙을 두 벌 만들 이유가 없다.
    "payout_ledger",
    "cashflow",
    "operation_issues",
]


@dataclass(slots=True)
class TaxReportRequest:
    org_id: UUID
    kind: ReportKind
    period_start: date
    period_end: date
    format: Literal["xlsx", "csv"] = "xlsx"


@dataclass(slots=True)
class ReportFile:
    file_name: str
    content: bytes
    row_count: int
    generated_at: datetime = field(default_factory=datetime.utcnow)


def generate_tax_report(req: TaxReportRequest, db: Session) -> ReportFile:
    """MVP placeholder — io/excel_export 모듈이 실제 SQL+Excel 생성 담당.

    호출자 (API 라우터) 는 본 함수 반환의 `content` (bytes) 를 다운로드로 전달.
    """
    from app.io.excel_export.tax_reports import build_report  # local import: avoid cycle

    return build_report(req, db)
