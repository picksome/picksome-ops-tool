"""세무사 export 라우터 (D-282, docs/22_api_spec.md §8).

MVP 7종 (CLAUDE.md Tax Report 절):
  monthly_sales / monthly_purchase / monthly_expense / sales_by_channel /
  product_pnl / coupang_settlement_recon / vat_reference

확장 5종 (Post-MVP 후보): partner_distribution / evidence_missing 등.

endpoints:
  GET  /reports/kinds   — 사용 가능한 리포트 종류 + 한글 라벨
  POST /reports/export  — Excel(xlsx) 또는 CSV 다운로드 (UTF-8 BOM)
                          파일명 규칙: 2026-06_월별매출리포트.xlsx

권한: ADMIN, ACCOUNTING. READONLY 는 export 제외 — 외부 반출 추적 명확화.

박제 원칙: 모든 export 는 audit_log 에 kind/period/row_count/exported_at 기록 → 누가 언제 무엇을 반출했는지 추적 가능.
EXTERNAL_ACCOUNTANT 전용 라우트는 Post-MVP (D-282)."""

from __future__ import annotations

import calendar as _calendar
import csv
import zipfile
from datetime import date, datetime
from io import BytesIO, StringIO
from typing import Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text as _text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.io.excel_export.tax_reports import REPORT_LABELS, _fetch
from app.services import dashboard as dashboard_service
from app.services.margin_analytics import aggregate_actual_margin
from app.services.reports import ReportKind, TaxReportRequest, generate_tax_report

router = APIRouter(prefix="/reports", tags=["reports"])


def _sum_known_margin(items: list[dict]) -> tuple[int, int]:
    """마진 합계와 **집계에서 빠진 상품 수.**

    원가를 모르는 상품은 `actual_margin_amount = None` 이다. 그걸 0 으로 더하면
    이익이 부풀고, 그냥 빼면 사용자가 빠진 줄 모른다. 그래서 **합계와 제외
    건수를 함께** 돌려준다 — 화면이 "N개 제외됨" 을 같이 보여줄 수 있게.
    """
    total = sum(
        it["actual_margin_amount"] for it in items
        if it["actual_margin_amount"] is not None)
    excluded = sum(1 for it in items if it["actual_margin_amount"] is None)
    return total, excluded


_dash_read = require_roles("ADMIN", "OPERATOR", "ACCOUNTING", "READONLY")


def _month_range(ym: str) -> tuple[date, date]:
    """'YYYY-MM' → (해당 월 1일, 말일)."""
    y, m = int(ym[:4]), int(ym[5:7])
    last_day = _calendar.monthrange(y, m)[1]
    return date(y, m, 1), date(y, m, last_day)


def _content_disposition(filename: str) -> str:
    """한국어 파일명 → RFC 6266/5987 헤더.

    filename* 는 퍼센트 인코딩된 UTF-8 octet 이어야 하며 (Latin-1 로 그대로
    보내면 UnicodeEncodeError), 구형 클라이언트를 위한 ASCII fallback도 같이 둔다.
    """
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
    return f"attachment; filename=\"download.{ext}\"; filename*=UTF-8''{quote(filename)}"

_export = require_roles("ADMIN", "ACCOUNTING")


class ReportExportRequest(BaseModel):
    kind: ReportKind
    period_start: date
    period_end: date
    format: Literal["xlsx", "csv"] = Field(default="xlsx")


@router.get("/kinds")
def kinds(user: CurrentUser = Depends(_export)) -> dict:
    """리포트 종류. **아직 못 만드는 것도 함께 준다.**

    목록에서 지우면 "왜 종합소득세 리포트가 없지" 를 아무도 답하지 못하고,
    사용자는 우리가 잊었다고 생각한다. 대신 `available=false` 로 표시해서
    화면이 고를 수 없게 만든다.

    누르면 빈 파일이 내려오던 것이 예전 동작이었다 — 세무 자료로 그대로
    넘어갈 수 있어 막았다.
    """
    from app.io.excel_export.tax_reports import NOT_IMPLEMENTED_KINDS

    items = [
        {
            "kind": k, "label": v,
            "available": k not in NOT_IMPLEMENTED_KINDS,
            "unavailable_reason": (
                None if k not in NOT_IMPLEMENTED_KINDS
                else "아직 제공하지 않습니다."
            ),
        }
        for k, v in REPORT_LABELS.items()
    ]
    return envelope(items)


@router.post("/export")
def export(
    body: ReportExportRequest,
    user: CurrentUser = Depends(_export),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    req = TaxReportRequest(
        org_id=user.org_id,
        kind=body.kind,
        period_start=body.period_start,
        period_end=body.period_end,
        format=body.format,
    )

    label = REPORT_LABELS.get(body.kind, body.kind)
    period = body.period_start.strftime("%Y-%m")

    if body.format == "xlsx":
        file = generate_tax_report(req, db)
        content = file.content
        filename = file.file_name
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        row_count = file.row_count
    else:
        rows, headers = _fetch(req, db)
        sio = StringIO()
        # 엑셀 호환 위해 UTF-8 BOM 추가
        sio.write("\ufeff")
        writer = csv.writer(sio)
        writer.writerow(headers)
        for r in rows:
            writer.writerow(list(r))
        content = sio.getvalue().encode("utf-8")
        filename = f"{period}_{label}.csv"
        media = "text/csv; charset=utf-8"
        row_count = len(rows)

    record_audit(
        db,
        org_id=user.org_id,
        domain_table="tax_report_export",
        # audit_log.domain_row_id 는 NOT NULL 이나 이 export 는 참조할 실제 행이
        # 없어 이 export 이벤트 자체를 가리키는 UUID 를 새로 발급한다.
        domain_row_id=uuid4(),
        action="EXPORT",
        actor_user_id=user.user_id,
        after={
            "kind": body.kind,
            "period": f"{body.period_start}~{body.period_end}",
            "format": body.format,
            "row_count": row_count,
            "exported_at": datetime.utcnow().isoformat(),
        },
    )
    db.commit()

    return StreamingResponse(
        BytesIO(content),
        media_type=media,
        headers={
            "Content-Disposition": _content_disposition(filename),
            "X-Report-Row-Count": str(row_count),
        },
    )


# 프론트 ZIP 일괄 다운로드에 포함되는 리포트 종류 (frontend/lib/api/reports.ts 의 9종과 동일)
_ZIP_KINDS: list[ReportKind] = [
    "monthly_sales",
    "monthly_purchase",
    "monthly_expense",
    "by_marketplace",
    "by_product_pl",
    "coupang_reconcile",
    "vat_quarterly",
    "partner_distribution",
    "evidence_missing",
]


class ExportZipRequest(BaseModel):
    period_start: date
    period_end: date
    format: Literal["xlsx", "csv"] = Field(default="xlsx")


@router.post("/export-zip")
def export_zip(
    body: ExportZipRequest,
    user: CurrentUser = Depends(_export),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for kind in _ZIP_KINDS:
            req = TaxReportRequest(
                org_id=user.org_id,
                kind=kind,
                period_start=body.period_start,
                period_end=body.period_end,
                format=body.format,
            )
            label = REPORT_LABELS.get(kind, kind)
            period = body.period_start.strftime("%Y-%m")
            if body.format == "xlsx":
                file = generate_tax_report(req, db)
                zf.writestr(file.file_name, file.content)
                row_count = file.row_count
            else:
                rows, headers = _fetch(req, db)
                sio = StringIO()
                sio.write("﻿")
                writer = csv.writer(sio)
                writer.writerow(headers)
                for r in rows:
                    writer.writerow(list(r))
                zf.writestr(f"{period}_{label}.csv", sio.getvalue().encode("utf-8"))
                row_count = len(rows)

            record_audit(
                db,
                org_id=user.org_id,
                domain_table="tax_report_export",
                domain_row_id=uuid4(),
                action="EXPORT",
                actor_user_id=user.user_id,
                after={
                    "kind": kind,
                    "period": f"{body.period_start}~{body.period_end}",
                    "format": body.format,
                    "row_count": row_count,
                    "exported_at": datetime.utcnow().isoformat(),
                },
            )
    db.commit()

    zip_name = f"{body.period_start.strftime('%Y-%m')}_전체리포트.zip"
    return StreamingResponse(
        BytesIO(buf.getvalue()),
        media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(zip_name)},
    )


@router.get("/actual-margin")
def actual_margin_list(
    year_month: str = Query(...),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: CurrentUser = Depends(_dash_read),
    db: Session = Depends(get_db),
) -> dict:
    start, end = _month_range(year_month)
    items = aggregate_actual_margin(db, org_id=user.org_id, start=start, end=end)
    items.sort(key=lambda it: it["gross_revenue"], reverse=True)

    total = len(items)
    offset = (page - 1) * page_size
    page_items = items[offset : offset + page_size]
    for it in page_items:
        it.pop("category_path", None)

    return envelope(
        {
            "items": page_items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "year_month": year_month,
        }
    )


# ---------------------------------------------------------------------------
# 대시보드 집계 엔드포인트
# ---------------------------------------------------------------------------


def _target_margin_rate(db: Session, org_id) -> float:
    """현재 유효한 목표 마진율 정책. 없으면 0.25 (정책 미설정 조직의 기본값 — 정책 자체가 없을 때만 쓰는 안전 기본값)."""
    row = db.execute(_text("""
        SELECT target_rate FROM target_margin_policy
        WHERE org_id = :o AND deleted_at IS NULL
          AND effective_from <= CURRENT_DATE
          AND (effective_to IS NULL OR effective_to >= CURRENT_DATE)
        ORDER BY effective_from DESC LIMIT 1
    """), {"o": str(org_id)}).first()
    return float(row.target_rate) if row and row.target_rate else 0.25


@router.get("/dashboard")
def dashboard(
    year_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    trend_months: int = Query(default=6, ge=1, le=dashboard_service.MAX_TREND_MONTHS),
    user: CurrentUser = Depends(_dash_read),
    db: Session = Depends(get_db),
) -> dict:
    """대시보드 한 판 (Phase 45).

    **월 단위다.** 임의 날짜 구간이 아니다 — 마감 상태(OPEN/REVIEW/CONFIRMED/LOCKED)와
    확정 snapshot 이 월 단위로 존재하기 때문이다. 6월 15일~7월 15일 같은 구간은
    확정된 달의 박제 숫자와 진행 중인 달의 집계를 한 값에 섞게 되고, 그러면
    그 값의 source 를 말할 수 없다.

    진행월은 live 집계, 확정월은 `monthly_close_snapshot` 을 읽는다.
    모든 숫자에 source 가 붙어 나간다.
    """
    ym = year_month or dashboard_service.current_year_month()
    data = dashboard_service.build_dashboard(
        db, org_id=user.org_id, year_month=ym, trend_months=trend_months,
        # 대시보드는 OPERATOR·READONLY 도 읽지만 현금흐름은 그렇지 않다.
        # 역할을 넘기지 않으면 `/cashflow` 가 403 인 사람에게 여기로 같은
        # 숫자가 나가는 뒷문이 된다.
        viewer_role=user.role,
    )
    # 단건(객체) 응답은 프론트 api wrapper 가 meta 를 버린다(lib/api.ts).
    # 월 선택기가 이 목록 없이는 동작하지 않으므로 data 안에 넣는다.
    data["available_months"] = dashboard_service.available_months(db, org_id=user.org_id)
    return envelope(data)


@router.get("/dashboard-summary")
def dashboard_summary(
    user: CurrentUser = Depends(_dash_read),
    db: Session = Depends(get_db),
) -> dict:
    from datetime import timedelta

    today = date.today()
    ym = today.strftime("%Y-%m")
    ym_prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    month_start, month_end = _month_range(ym)
    prev_start, prev_end = _month_range(ym_prev)

    # 이번달 매출 합계 (sale_price 는 라인 합계 — quantity 를 곱하지 않는다)
    rev = db.execute(_text("""
        SELECT COALESCE(SUM(sale_price), 0) FROM sales_order
        WHERE org_id = :o AND deleted_at IS NULL
          AND to_char(sale_date, 'YYYY-MM') = :ym
    """), {"o": str(user.org_id), "ym": ym}).scalar_one()

    # 지난달 매출 합계
    rev_prev = db.execute(_text("""
        SELECT COALESCE(SUM(sale_price), 0) FROM sales_order
        WHERE org_id = :o AND deleted_at IS NULL
          AND to_char(sale_date, 'YYYY-MM') = :ym
    """), {"o": str(user.org_id), "ym": ym_prev}).scalar_one()

    # 이번달 비용 합계 (settlement model: 정산 대상 공통비만 — services/settlement.py 와 동일 필터)
    exp = db.execute(_text("""
        SELECT COALESCE(SUM(gross_amount), 0) FROM expenses
        WHERE org_id = :o AND deleted_at IS NULL
          AND to_char(expense_date, 'YYYY-MM') = :ym
          AND is_settlement_deductible = TRUE AND is_business_use = TRUE
    """), {"o": str(user.org_id), "ym": ym}).scalar_one()

    # 이번달 증빙 누락 건수
    ev_missing = db.execute(_text("""
        SELECT COUNT(*) FROM expenses
        WHERE org_id = :o AND deleted_at IS NULL
          AND to_char(expense_date, 'YYYY-MM') = :ym
          AND evidence_status = 'NONE'
    """), {"o": str(user.org_id), "ym": ym}).scalar_one()

    # 가장 최근 월별 정산 상태
    settle_row = db.execute(_text("""
        SELECT status FROM monthly_settlement
        WHERE org_id = :o AND deleted_at IS NULL
        ORDER BY year_month DESC LIMIT 1
    """), {"o": str(user.org_id)}).first()
    settle_status = settle_row.status if settle_row else "NONE"

    target_rate = _target_margin_rate(db, user.org_id)

    # 실 마진 = 상품별 실 마진 집계 합 (calculate_actual_margin 과 동일한 MVP 근사 규칙)
    items = aggregate_actual_margin(db, org_id=user.org_id, start=month_start, end=month_end)
    items_prev = aggregate_actual_margin(db, org_id=user.org_id, start=prev_start, end=prev_end)
    actual_margin, margin_excluded = _sum_known_margin(items)
    actual_margin_prev, _ = _sum_known_margin(items_prev)
    avg_margin_rate = actual_margin / int(rev) if int(rev) > 0 else 0.0

    # 분배대상 이익 = 실 마진 - 사업 공통비 (settlement model: 순이익 기준 분배)
    distributable = actual_margin - int(exp)

    # 이번달 마지막 날 (정산 마감일 안내용)
    last_day = month_end.day

    return envelope({
        "current_month": ym,
        "revenue": int(rev),
        "revenue_prev": int(rev_prev),
        "actual_margin": actual_margin,
        "actual_margin_prev": actual_margin_prev,
        # 원가를 몰라 집계에서 빠진 상품 수. 0 이 아니면 위 마진은 **하한**이다 —
        # 화면이 그 사실을 같이 보여줘야 사용자가 숫자를 과신하지 않는다.
        "margin_excluded_products": margin_excluded,
        "avg_margin_rate": avg_margin_rate,
        "target_margin_rate": target_rate,
        "total_expenses": int(exp),
        "distributable_profit": max(0, distributable),
        "evidence_missing_count": int(ev_missing),
        "settlement_status": settle_status,
        "carryover_amount": 0,
        "next_close_day": last_day,
    })


@router.get("/monthly-trend")
def monthly_trend(
    months: int = 6,
    user: CurrentUser = Depends(_dash_read),
    db: Session = Depends(get_db),
) -> dict:
    """최근 N개월 매출 + 실 마진 트렌드."""
    today = date.today()
    result = []
    y, m = today.year, today.month
    for _ in range(months):
        ym = f"{y:04d}-{m:02d}"
        start, end = _month_range(ym)
        rev = db.execute(_text("""
            SELECT COALESCE(SUM(sale_price), 0) FROM sales_order
            WHERE org_id = :o AND deleted_at IS NULL AND sale_date BETWEEN :s AND :e
        """), {"o": str(user.org_id), "s": start, "e": end}).scalar_one()
        items = aggregate_actual_margin(db, org_id=user.org_id, start=start, end=end)
        margin, excluded = _sum_known_margin(items)
        result.append({"year_month": ym, "revenue": int(rev),
                       "actual_margin": margin,
                       "margin_excluded_products": excluded})
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    result.reverse()
    return envelope({"months": result})


@router.get("/category-margin")
def category_margin(
    user: CurrentUser = Depends(_dash_read),
    db: Session = Depends(get_db),
) -> dict:
    """이번달 상품 카테고리별 매출/마진율."""
    today = date.today()
    start, end = _month_range(today.strftime("%Y-%m"))
    items = aggregate_actual_margin(db, org_id=user.org_id, start=start, end=end)

    by_cat: dict[str, dict[str, int]] = {}
    for it in items:
        cat = it["category_path"] or "미분류"
        agg = by_cat.setdefault(cat, {"revenue": 0, "margin": 0, "excluded": 0})
        agg["revenue"] += it["gross_revenue"]
        if it["actual_margin_amount"] is None:
            # 원가 미상은 이 분류의 마진에 넣지 않는다. 대신 몇 개가 빠졌는지 센다.
            agg["excluded"] += 1
        else:
            agg["margin"] += it["actual_margin_amount"]

    categories = [
        {
            "category_path": cat,
            "revenue": v["revenue"],
            "actual_margin": v["margin"],
            "margin_rate": (v["margin"] / v["revenue"]) if v["revenue"] > 0 else 0.0,
        }
        for cat, v in sorted(by_cat.items(), key=lambda kv: kv[1]["revenue"], reverse=True)
    ]
    return envelope({"categories": categories})


@router.get("/margin-gap-top")
def margin_gap_top(
    limit: int = 10,
    user: CurrentUser = Depends(_dash_read),
    db: Session = Depends(get_db),
) -> dict:
    """이번달 목표 마진 대비 실제 마진 gap 상위 N개 상품 (예상 마진 데이터가 있는 상품만)."""
    today = date.today()
    start, end = _month_range(today.strftime("%Y-%m"))
    items = aggregate_actual_margin(db, org_id=user.org_id, start=start, end=end)
    with_expected = [it for it in items if it["expected_margin_amount"] is not None]
    with_expected.sort(key=lambda it: abs(it["delta_amount"]), reverse=True)
    top = with_expected[:limit]
    for it in top:
        it.pop("category_path", None)
    return envelope({"items": top})


@router.get("/margin-drop-alerts")
def margin_drop_alerts(
    user: CurrentUser = Depends(_dash_read),
    db: Session = Depends(get_db),
) -> dict:
    """목표 마진율(target_margin_policy) 미달 상품 알림 목록 (이번달 기준)."""
    today = date.today()
    start, end = _month_range(today.strftime("%Y-%m"))
    target_rate = _target_margin_rate(db, user.org_id)
    items = aggregate_actual_margin(db, org_id=user.org_id, start=start, end=end)
    alerts = [
        {**{k: v for k, v in it.items() if k != "category_path"}, "target_margin_rate": target_rate}
        for it in items
        # 마진을 모르는 상품은 "기준 미달" 이라고 말할 수 없다. 경고에서 뺀다 —
        # 대신 아래 `cost_unknown` 으로 따로 알린다.
        if it["gross_revenue"] > 0
        and it["actual_margin_rate"] is not None
        and it["actual_margin_rate"] < target_rate
    ]
    alerts.sort(key=lambda it: it["actual_margin_rate"])
    # 원가를 몰라 판정하지 못한 상품. 조용히 빠지면 "문제 없음" 으로 읽힌다.
    unknown = [
        {"product_id": it["product_id"], "product_name": it["product_name"],
         "unknown_cost_lines": it["unknown_cost_lines"]}
        for it in items if it["actual_margin_rate"] is None
    ]
    return envelope({"alerts": alerts, "cost_unknown": unknown})
