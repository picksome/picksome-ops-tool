"""세무사 리포트 Excel 생성.

각 ReportKind 별 SQL → openpyxl Workbook → bytes.
파일명 규칙 (CLAUDE.md): `YYYY-MM_<리포트명>.xlsx`, 컬럼명 한국어.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from openpyxl import Workbook
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.services.reports import ReportFile, TaxReportRequest

REPORT_LABELS = {
    "monthly_sales": "월별매출리포트",
    "monthly_expense": "월별비용리포트",
    "monthly_settlement": "월별정산리포트",
    "evidence_missing": "증빙누락리포트",
    "vat_quarterly": "부가세참고리포트",
    "personal_income_summary": "종합소득세참고리포트",
    "business_income": "사업소득리포트",
    "by_marketplace": "판매처별매출리포트",
    "by_product_pl": "상품별손익리포트",
    "coupang_reconcile": "쿠팡정산대사리포트",
    "partner_distribution": "파트너수익배분리포트",
    "monthly_purchase": "월별매입리포트",
    "payout_ledger": "지급장부리포트",
    "cashflow": "현금흐름리포트",
    "operation_issues": "운영이슈리포트",
}


#: SQL 이 아직 없는 종류. **목록에서 지우지 않고 여기 적는다** —
#: 지우면 "왜 이 리포트가 없지" 를 아무도 답하지 못한다.
#:
#: 요청이 오면 **거절한다.** 예전에는 빈 시트에 `TODO` 헤더 한 줄을 넣어
#: 돌려줬는데, 그건 세무 리포트로서 위험하다. 사용자는 파일을 받았으니
#: 만들어진 줄 알고 세무사에게 그대로 넘길 수 있다.
NOT_IMPLEMENTED_KINDS: frozenset[str] = frozenset({
    "personal_income_summary",
    "business_income",
})


def build_report(req: TaxReportRequest, db: Session) -> ReportFile:
    if req.kind in NOT_IMPLEMENTED_KINDS:
        raise ValidationError(
            f"{REPORT_LABELS.get(req.kind, req.kind)} 는 아직 제공하지 않습니다. "
            "빈 파일을 내려받아 신고 자료로 쓰는 일이 없도록 막습니다.",
        )
    rows, headers = _fetch(req, db)
    wb = Workbook()
    ws = wb.active
    ws.title = REPORT_LABELS.get(req.kind, req.kind)
    ws.append(headers)
    for r in rows:
        ws.append(list(r))
    buf = BytesIO()
    wb.save(buf)
    label = REPORT_LABELS.get(req.kind, req.kind)
    period = req.period_start.strftime("%Y-%m")
    name = f"{period}_{label}.xlsx"
    return ReportFile(file_name=name, content=buf.getvalue(), row_count=len(rows))


def _fetch(req: TaxReportRequest, db: Session) -> tuple[list[tuple[Any, ...]], list[str]]:
    org = str(req.org_id)
    s, e = req.period_start, req.period_end

    if req.kind == "monthly_sales":
        rs = db.execute(
            text(
                """
                -- sale_price 는 라인 합계(판매가+배송비)라 그 자체가 매출액이다.
                -- quantity 를 곱하면 이중 계산된다.
                SELECT sale_date, marketplace, marketplace_order_no,
                       quantity, sale_price AS revenue
                  FROM sales_order
                 WHERE org_id = :o AND deleted_at IS NULL
                   AND sale_date BETWEEN :s AND :e
                 ORDER BY sale_date
                """
            ),
            {"o": org, "s": s, "e": e},
        ).all()
        return [tuple(r) for r in rs], ["판매일", "마켓", "주문번호", "수량", "매출액(판매가+배송비)"]

    if req.kind == "monthly_expense":
        rs = db.execute(
            text(
                """
                SELECT expense_date, description, gross_amount, supply_amount, vat_amount,
                       payment_method, evidence_type, evidence_status
                  FROM expenses
                 WHERE org_id = :o AND deleted_at IS NULL
                   AND expense_date BETWEEN :s AND :e
                 ORDER BY expense_date
                """
            ),
            {"o": org, "s": s, "e": e},
        ).all()
        return [tuple(r) for r in rs], [
            "지출일", "내용", "총액", "공급가액", "부가세", "결제수단", "증빙유형", "증빙상태"
        ]

    if req.kind == "evidence_missing":
        rs = db.execute(
            text(
                """
                SELECT expense_date, description, gross_amount, evidence_status
                  FROM expenses
                 WHERE org_id = :o AND deleted_at IS NULL
                   AND expense_date BETWEEN :s AND :e
                   AND evidence_status IN ('NONE','NEEDS_CHECK')
                 ORDER BY expense_date
                """
            ),
            {"o": org, "s": s, "e": e},
        ).all()
        return [tuple(r) for r in rs], ["지출일", "내용", "금액", "증빙상태"]

    if req.kind == "vat_quarterly":
        rs = db.execute(
            text(
                """
                SELECT 'EXPENSE' AS kind, expense_date AS d,
                       supply_amount, vat_amount, vat_inclusion_type, tax_type
                  FROM expenses
                 WHERE org_id = :o AND deleted_at IS NULL
                   AND expense_date BETWEEN :s AND :e
                 ORDER BY expense_date
                """
            ),
            {"o": org, "s": s, "e": e},
        ).all()
        return [tuple(r) for r in rs], ["구분", "일자", "공급가액", "부가세", "포함여부", "과세유형"]

    if req.kind == "monthly_settlement":
        rs = db.execute(
            text(
                """
                SELECT year_month, total_revenue, total_direct_cost, total_common_expense,
                       net_profit, distributable_amount, status
                  FROM monthly_settlement
                 WHERE org_id = :o AND deleted_at IS NULL
                   AND year_month BETWEEN to_char(:s, 'YYYY-MM') AND to_char(:e, 'YYYY-MM')
                 ORDER BY year_month
                """
            ),
            {"o": org, "s": s, "e": e},
        ).all()
        return [tuple(r) for r in rs], [
            "년월", "매출", "직접비", "공통비", "순이익", "분배대상", "상태"
        ]

    if req.kind == "monthly_purchase":
        rs = db.execute(
            text(
                """
                -- 매입금액은 sales_order.purchase_cost(그 주문에서 실제 지출한
                -- 라인 합계) 가 최우선이다. 없을 때만 상품 마스터의 단가×수량으로
                -- 추정한다 (M013).
                SELECT so.sale_date,
                       COALESCE(p.name_ko, so.product_snapshot_name, '상품 미매칭'),
                       so.quantity,
                       so.shipping_fee,
                       -- NULL(미상)일 때만 상품 마스터로 대체. 0 은 실제 0원 (M014)
                       --
                       -- 어디에도 원가가 없으면 **빈 칸**으로 둔다. 예전에는
                       -- 0 을 넣었는데, 매입 리포트에서 0원은 "이 건은 공짜로
                       -- 샀다" 로 읽히고 그대로 부가세 매입세액을 과소 계상한다.
                       -- 빈 칸이면 세무사가 되묻는다 — 그게 맞는 동작이다.
                       CASE WHEN so.purchase_cost IS NOT NULL THEN so.purchase_cost
                            WHEN sv.cost_amount IS NOT NULL THEN sv.cost_amount * so.quantity
                            WHEN p.cost_amount IS NOT NULL THEN p.cost_amount * so.quantity
                            ELSE NULL END AS total_cost
                  FROM sales_order so
                  LEFT JOIN product p ON p.id = so.product_id
                  LEFT JOIN product_variant sv ON sv.id = so.product_variant_id
                 WHERE so.org_id = :o AND so.deleted_at IS NULL
                   AND so.sale_date BETWEEN :s AND :e
                 ORDER BY so.sale_date
                """
            ),
            {"o": org, "s": s, "e": e},
        ).all()
        return [tuple(r) for r in rs], [
            "매입일(판매일 기준)", "상품명", "수량", "매입 배송비", "매입금액"
        ]

    if req.kind == "by_marketplace":
        rs = db.execute(
            text(
                """
                -- sale_price 는 라인 합계 — quantity 를 곱하지 않는다
                SELECT marketplace::text, COUNT(*), SUM(quantity), SUM(sale_price)
                  FROM sales_order
                 WHERE org_id = :o AND deleted_at IS NULL
                   AND sale_date BETWEEN :s AND :e
                 GROUP BY marketplace
                 ORDER BY SUM(sale_price) DESC
                """
            ),
            {"o": org, "s": s, "e": e},
        ).all()
        return [tuple(r) for r in rs], ["마켓", "주문건수", "수량", "매출액"]

    if req.kind == "by_product_pl":
        from app.services.margin_analytics import aggregate_actual_margin

        items = aggregate_actual_margin(db, org_id=req.org_id, start=s, end=e)
        rows = [
            (
                it["product_name"],
                it["sku"],
                it["gross_revenue"],
                it["total_cost"],
                it["actual_margin_amount"],
                it["actual_margin_rate"],
            )
            for it in items
        ]
        return rows, ["상품명", "SKU", "매출", "원가", "마진액", "마진율"]

    if req.kind == "coupang_reconcile":
        rs = db.execute(
            text(
                """
                SELECT msr.settlement_period_start, msr.settlement_period_end,
                       msr.marketplace_order_no, msr.gross_revenue, msr.marketplace_fee,
                       msr.ad_cost, msr.return_cost, msr.fulfillment_fee,
                       msr.other_deductions, msr.net_settlement,
                       so.sale_price AS matched_sales_amount  -- 라인 합계, qty 곱하지 않음
                  FROM marketplace_settlement_record msr
                  LEFT JOIN sales_order so ON so.id = msr.matched_sales_order_id
                 WHERE msr.org_id = :o AND msr.deleted_at IS NULL
                   AND msr.marketplace = 'COUPANG'
                   AND msr.settlement_period_start >= :s
                   AND msr.settlement_period_end <= :e
                 ORDER BY msr.settlement_period_start
                """
            ),
            {"o": org, "s": s, "e": e},
        ).all()
        return [tuple(r) for r in rs], [
            "정산시작", "정산종료", "주문번호", "정산매출", "마켓수수료",
            "광고비", "반품비", "풀필먼트비", "기타공제", "정산액", "매칭판매금액",
        ]

    if req.kind == "partner_distribution":
        rs = db.execute(
            text(
                """
                SELECT ms.year_month, pt.name, ps.distribution_kind::text,
                       ps.amount, ps.ratio_used, ms.status::text
                  FROM partner_settlement ps
                  JOIN monthly_settlement ms ON ms.id = ps.monthly_settlement_id
                  JOIN partner pt ON pt.id = ps.partner_id
                 WHERE ms.org_id = :o AND ms.deleted_at IS NULL
                   AND ms.year_month BETWEEN to_char(:s, 'YYYY-MM') AND to_char(:e, 'YYYY-MM')
                 ORDER BY ms.year_month, pt.name
                """
            ),
            {"o": org, "s": s, "e": e},
        ).all()
        return [tuple(r) for r in rs], [
            "년월", "파트너명", "배분방식", "배분금액", "적용비율", "정산상태"
        ]

    if req.kind == "payout_ledger":
        # 지급 **의무**와 실제 지급을 한 장에 놓는다. 둘을 따로 보면
        # "승인은 했는데 아직 안 줬다" 를 알아채기 어렵다.
        rs = db.execute(
            text(
                """
                SELECT pb.period_year_month, pb.title, pi.payee_name_snapshot,
                       pi.item_type::text, pi.accounting_effect::text,
                       pi.due_amount, pi.paid_amount,
                       pi.due_amount - pi.paid_amount AS outstanding,
                       pi.status::text, pi.due_date
                  FROM payout_item pi
                  JOIN payout_batch pb ON pb.id = pi.batch_id
                 WHERE pi.org_id = :o AND pi.deleted_at IS NULL
                   AND (pi.due_date BETWEEN :s AND :e
                        OR pb.period_year_month
                            BETWEEN to_char(CAST(:s AS DATE), 'YYYY-MM')
                                AND to_char(CAST(:e AS DATE), 'YYYY-MM'))
                 ORDER BY pb.period_year_month, pi.payee_name_snapshot
                """
            ),
            {"o": org, "s": s, "e": e},
        ).all()
        return [tuple(r) for r in rs], [
            "정산월", "배치명", "받는사람", "지급유형", "회계효과",
            "지급예정액", "지급완료액", "미지급잔액", "상태", "지급예정일",
        ]

    if req.kind == "cashflow":
        # 읽기 모델을 그대로 내보낸다. export 전용 SQL 을 따로 쓰면 화면과 파일이
        # 다른 숫자를 말하게 되고, 그 순간 둘 다 신뢰를 잃는다.
        from app.services import cashflow as cashflow_svc

        rows = cashflow_svc.collect_rows(
            db, org_id=req.org_id, date_from=s, date_to=e
        )
        return (
            [
                (
                    r.occurred_on,
                    "유입" if r.direction == cashflow_svc.INFLOW else "유출",
                    {"EXPECTED": "예상", "CONFIRMED": "확정", "ACTUAL": "실제"}[r.status],
                    r.amount,
                    r.label,
                    r.source_type,
                )
                for r in rows
            ],
            # 출처 열을 뺀 현금흐름 파일은 검토가 불가능하다 — 세무사가
            # "이 300만원이 뭐냐" 고 물었을 때 답할 근거가 파일 안에 있어야 한다.
            ["일자", "구분", "근거", "금액", "내용", "출처"],
        )

    if req.kind == "operation_issues":
        rs = db.execute(
            text(
                """
                SELECT created_at::date, issue_type, severity::text, status::text,
                       title, year_month, affected_count, due_date, resolution_note
                  FROM operation_issue
                 WHERE org_id = :o AND deleted_at IS NULL
                   AND created_at::date BETWEEN :s AND :e
                 ORDER BY created_at
                """
            ),
            {"o": org, "s": s, "e": e},
        ).all()
        return [tuple(r) for r in rs], [
            "발생일", "이슈유형", "심각도", "상태", "제목",
            "대상월", "건수", "기한", "처리메모",
        ]

    # 여기 오면 안 된다 — `build_report` 가 미구현 종류를 먼저 거절한다.
    # 방어적으로 남긴다: 새 종류를 추가하고 SQL 을 빠뜨리면 조용히 빈 파일이
    # 나가는 대신 여기서 터진다.
    raise ValidationError(f"리포트 SQL 이 구현되지 않았습니다: {req.kind}")
