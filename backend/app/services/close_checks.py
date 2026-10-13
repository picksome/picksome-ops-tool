"""월 마감 검증 카탈로그 (Phase 26).

M017 의 검증은 5개였고 결과를 한 칼럼에 덮어썼다. 여기서 두 가지를 바꾼다.

  1. 판매·비용·정산·승인·정책 다섯 영역을 전부 본다
  2. 검증 1회 = `monthly_close_check_run` 1행. 덮어쓰지 않는다

등급
----
```
BLOCKING   숫자가 틀리게 되는 것. 확정을 막는다.
WARNING    사람이 보고 넘길 수 있는 것. 넘기려면 명시적 확인이 필요하다.
INFO       참고. 확정을 막지도, 확인을 요구하지도 않는다.
```

**임계값이 확정되지 않은 항목을 BLOCKING 으로 만들지 않는다.** 예를 들어
"목표 마진 미달" 은 몇 %부터 문제인지 업무 기준이 없다. 그런 항목을 차단으로
만들면 사용자는 검증 자체를 신뢰하지 않게 되고, 결국 전부 무시한다.

정산 미연결을 정상으로 보지 않는다
------------------------------
`marketplace_settlement_record` 가 0건인 것은 "정산에 문제가 없다" 가 아니라
**"정산을 아직 한 번도 연결하지 않았다"** 이다. 이 둘을 같게 취급하면
정산 없이 확정한 달이 완전한 달처럼 보인다. 그래서 명시적 WARNING 을 낸다.

기존 결함 수정 (D3)
-----------------
`UNMATCHED_SETTLEMENT` 가 BLOCKING 이었다. `docs/47_settlement_reconciliation_design.md` §4
에서 확정한 결론과 어긋난다 — 판매처가 정산서를 늦게 주는 일이 흔하고, 그걸 차단으로
만들면 **마감이 판매처 일정에 인질로 잡힌다.** WARNING 으로 내린다.
확인 없이 넘어갈 수는 없다(확정 시 acknowledged_warnings 필요).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.settlement import month_range

# ── 등급 ──
BLOCKING = "BLOCKING"
WARNING = "WARNING"
INFO = "INFO"

# 기존 응답 계약(severity = INFO/WARNING/CRITICAL)을 깨지 않기 위한 사상.
# 프론트가 CRITICAL 로 색을 칠하고 있어서 값을 바꾸면 배지가 사라진다.
_LEGACY_SEVERITY = {BLOCKING: "CRITICAL", WARNING: "WARNING", INFO: "INFO"}


@dataclass(slots=True)
class ValidationItem:
    """검증 항목 1건.

    `severity` 는 **기존 계약(INFO/WARNING/CRITICAL)** 을 유지하고,
    새 3등급은 `grade` 로 따로 노출한다. 두 어휘를 한 필드에 섞으면
    프론트가 조용히 잘못 칠한다.
    """

    code: str
    severity: str
    blocking: bool
    count: int
    message: str
    hint: str | None = None
    grade: str = WARNING
    entity_type: str | None = None
    domain: str = "OTHER"
    entity_id: UUID | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _item(
    code: str, grade: str, count: int, message: str, hint: str | None = None,
    *, domain: str = "OTHER", entity_type: str | None = None,
) -> ValidationItem:
    return ValidationItem(
        code=code,
        severity=_LEGACY_SEVERITY[grade],
        blocking=(grade == BLOCKING),
        count=count,
        message=message,
        hint=hint,
        grade=grade,
        domain=domain,
        entity_type=entity_type,
    )


def _has_table(db: Session, table: str) -> bool:
    return db.execute(text("""
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = 'public' AND table_name = :t
    """), {"t": table}).first() is not None


def _has_column(db: Session, table: str, column: str) -> bool:
    return db.execute(text("""
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = :t AND column_name = :c
    """), {"t": table, "c": column}).first() is not None


# ──────────────────────────────────────────────────────────────────────────
# 카탈로그
# ──────────────────────────────────────────────────────────────────────────

def run_checks(db: Session, *, org_id: UUID, year_month: str) -> list[ValidationItem]:
    """전체 검증. 저장하지 않는다(저장은 `persist_run`)."""
    start, end = month_range(year_month)
    p = {"org": str(org_id), "s": start, "e": end, "ym": year_month}
    items: list[ValidationItem] = []

    def n_of(sql: str, extra: dict[str, Any] | None = None) -> int:
        row = db.execute(text(sql), {**p, **(extra or {})}).first()
        return int(row[0]) if row and row[0] is not None else 0

    items += _sales_checks(db, p, n_of)
    items += _claim_checks(db, p, n_of)
    items += _expense_checks(db, p, n_of)
    items += _settlement_checks(db, p, n_of)
    items += _cashflow_checks(db, p, n_of)
    items += _sourcing_checks(db, p, n_of)
    items += _external_checks(db, p, n_of)
    items += _approval_checks(db, p, n_of)
    items += _policy_checks(db, p, n_of)
    items += _issue_checks(db, p, n_of)

    return items


# ── 클레임 ──────────────────────────────────────────────────────────────

def _claim_checks(db: Session, p: dict, n_of) -> list[ValidationItem]:
    """취소·반품이 매출에 제대로 반영됐는가.

    막는 기준은 하나다 — **순매출이 부풀어 있는가.**

    ```
    UNRESOLVED        확정됐는데 금액을 못 정했다   부풀어 있다   BLOCKING
    NO_LEDGER_IMPACT  원 주문이 장부에 없다        정확하다      WARNING
    PENDING           아직 확정 전이다            정확하다      WARNING
    ```

    가운데 줄이 실측에서 문제가 됐다. `NO_LEDGER_IMPACT` 를 만들기 전에는
    출고 전 취소 14건이 `UNRESOLVED` 로 들어와 5·6·7월 마감이 전부 막혔다.
    되돌릴 매출이 애초에 없는데도. 되돌릴 게 없는 것과 얼마인지 모르는 것은
    정반대의 사실이다(M054).

    아직 확정 전인 클레임(반품 요청 등)도 막지 않는다. 철회될 수 있고, 확정
    전까지는 매출이 실제로 줄지 않았다 — 다만 사람이 알고는 있어야 한다.
    """
    if not _has_table(db, "sales_claim"):
        return []

    out: list[ValidationItem] = []

    n = n_of("""
        SELECT COUNT(*) FROM sales_claim
         WHERE org_id = :org AND deleted_at IS NULL
           AND financial_effect = 'UNRESOLVED'
           AND (original_sale_date IS NULL
                OR original_sale_date BETWEEN :s AND :e)
    """)
    if n:
        out.append(_item(
            "CLAIM_FINANCIAL_UNRESOLVED", BLOCKING, n,
            f"매출 반영을 확정하지 못한 취소·반품 {n}건이 있습니다.",
            "그만큼 순매출이 실제보다 크게 잡혀 있습니다. 판매처에서 금액을 "
            "확인하거나 원 주문을 연결해 주세요.",
            domain="SALES", entity_type="sales_claim",
        ))

    n = n_of("""
        SELECT COUNT(*) FROM sales_claim
         WHERE org_id = :org AND deleted_at IS NULL
           AND sales_order_id IS NULL
           AND (original_sale_date IS NULL
                OR original_sale_date BETWEEN :s AND :e)
    """)
    if n:
        out.append(_item(
            "CLAIM_WITHOUT_ORDER", WARNING, n,
            f"원 주문을 찾지 못한 취소·반품 {n}건이 있습니다.",
            "되돌릴 매출이 장부에 없으므로 이 달의 순매출은 정확합니다 — "
            "출고 전에 취소돼 매출로 잡힌 적이 없는 건이면 정상입니다. "
            "주문이 뒤늦게 들어오면 다음 동기화가 자동으로 이어 붙입니다.",
            domain="SALES", entity_type="sales_claim",
        ))

    n = n_of("""
        SELECT COUNT(*) FROM sales_claim
         WHERE org_id = :org AND deleted_at IS NULL
           AND financial_effect = 'PENDING'
           AND (original_sale_date IS NULL
                OR original_sale_date BETWEEN :s AND :e)
    """)
    if n:
        out.append(_item(
            "CLAIM_PENDING", WARNING, n,
            f"아직 확정되지 않은 취소·반품 {n}건이 있습니다.",
            "확정 전까지는 매출이 줄지 않습니다. 확정되면 다음 동기화가 "
            "반영합니다 — 마감을 막지는 않습니다.",
            domain="SALES", entity_type="sales_claim",
        ))

    # 확정 스냅샷의 `gross_sales` 는 `sales_order` 만 더한다. 클레임 조정은
    # **거기 들어 있지 않다** — `expense_adjustment` 도 같은 방식이라 조정을
    # 순이익에 접는 것은 따로 정해야 할 재무 결정이다(docs/80 §12-1).
    #
    # 그 결정을 여기서 대신 내리지 않는다. 대신 **잠그기 직전에 금액을
    # 보여 준다** — 모르고 잠그는 것과 알고 잠그는 것은 다르다.
    delta = n_of("""
        SELECT COALESCE(SUM(amount_delta), 0) FROM sales_adjustment
         WHERE org_id = :org AND deleted_at IS NULL AND adjustment_period = :ym
    """)
    if delta:
        out.append(_item(
            "CLAIM_ADJUSTMENT_NOT_IN_SNAPSHOT", WARNING, 1,
            f"이 달 확정 매출에 반영되지 않은 취소·반품 조정 {delta:,}원이 있습니다.",
            "확정 스냅샷의 총매출은 주문만 더한 값입니다. 사업비용 정정"
            "(expense_adjustment)과 같은 방식이며, 조정을 순이익·배분에 "
            "접을지는 아직 정해지지 않았습니다. 금액을 확인하고 잠가 주세요.",
            domain="SALES", entity_type="sales_adjustment",
        ))

    return out


# ── 판매 ────────────────────────────────────────────────────────────────

def _sales_checks(db: Session, p: dict, n_of) -> list[ValidationItem]:
    out: list[ValidationItem] = []

    n = n_of("""
        SELECT COUNT(*) FROM sales_order
         WHERE org_id = :org AND deleted_at IS NULL
           AND sale_date BETWEEN :s AND :e AND product_id IS NULL
    """)
    if n:
        out.append(_item(
            "SALE_WITHOUT_PRODUCT", BLOCKING, n,
            f"상품과 연결되지 않은 판매 {n}건이 있습니다.",
            "원가를 알 수 없어 마진이 매출 전액으로 잡힙니다. 판매 장부에서 상품을 연결하세요.",
            domain="SALES", entity_type="sales_order",
        ))

    # 원가 0원 — 매출도 0이면 취소/증정 건이라 정상이다(M014 판단과 동일)
    n = n_of("""
        SELECT COUNT(*) FROM sales_order
         WHERE org_id = :org AND deleted_at IS NULL
           AND sale_date BETWEEN :s AND :e
           AND sale_price > 0
           AND COALESCE(purchase_cost, 0) = 0
           AND product_id IS NOT NULL
           AND NOT EXISTS (
             SELECT 1 FROM product pr
              WHERE pr.id = sales_order.product_id AND COALESCE(pr.cost_amount, 0) > 0
           )
    """)
    if n:
        out.append(_item(
            "ZERO_COST_SALE", BLOCKING, n,
            f"원가가 0원인 판매 {n}건이 있습니다.",
            "매출 전액이 이익으로 잡혀 순이익이 과대계상됩니다.",
            domain="SALES", entity_type="sales_order",
        ))

    # ── 판매자 계정 귀속 (M032) ──────────────────────────────────
    #
    # `integration_account_id` 가 NULL 인 것 자체는 **오류가 아니다.**
    # 엑셀·수동으로 넣은 과거 주문은 그 정보가 애초에 없었다.
    #
    # 그러나 **자동 수집 경로로 들어온 주문**이 계정을 모르는 것은 다르다.
    # 그 경로는 계정을 알고 있어야 하고(키로 인증한다), 모른다면 수집 코드에
    # 문제가 있다는 뜻이다. 그래서 출처에 따라 등급을 나눈다.
    n = n_of("""
        SELECT COUNT(*) FROM sales_order
         WHERE org_id = :org AND deleted_at IS NULL
           AND sale_date BETWEEN :s AND :e
           AND integration_account_id IS NULL
           AND import_source IN ('BOT_AUTO', 'API_AUTO')
    """)
    if n:
        out.append(_item(
            "SALE_ACCOUNT_MISSING", WARNING, n,
            f"자동 수집된 판매 {n}건에 판매자 계정이 없습니다.",
            "자동 수집은 실행 계정을 알고 있어야 합니다. 수집 설정을 확인하세요. "
            "계정이 없으면 스토어별 매출·정산 대사를 할 수 없습니다.",
            domain="SALES", entity_type="sales_order",
        ))

    # 수동·엑셀 주문의 계정 미상은 **참고 정보**다. 차단도 경고도 아니다 —
    # 업무 기준이 확정되지 않은 것을 임의로 경고로 올리면 사용자가
    # 마감 검증 전체를 무시하게 된다.
    n = n_of("""
        SELECT COUNT(*) FROM sales_order
         WHERE org_id = :org AND deleted_at IS NULL
           AND sale_date BETWEEN :s AND :e
           AND integration_account_id IS NULL
           AND COALESCE(import_source, 'MANUAL') NOT IN ('BOT_AUTO', 'API_AUTO')
    """)
    if n:
        out.append(_item(
            "SALE_ACCOUNT_UNASSIGNED", INFO, n,
            f"판매자 계정이 지정되지 않은 판매 {n}건이 있습니다.",
            "수동·엑셀로 등록된 주문입니다. 스토어별로 나눠 보려면 계정을 지정하세요. "
            "지정하지 않아도 손익 계산에는 영향이 없습니다.",
            domain="SALES", entity_type="sales_order",
        ))

    # 조직 불일치 — 서비스가 막고 있지만 직접 INSERT 로 들어올 수 있다.
    # 이건 명백한 오류다: 남의 조직 계정 매출로 잡힌다.
    n = n_of("""
        SELECT COUNT(*) FROM sales_order so
          JOIN integration_account ia ON ia.id = so.integration_account_id
         WHERE so.org_id = :org AND so.deleted_at IS NULL
           AND so.sale_date BETWEEN :s AND :e
           AND ia.org_id <> so.org_id
    """)
    if n:
        out.append(_item(
            "SALE_ACCOUNT_ORG_MISMATCH", BLOCKING, n,
            f"다른 조직의 연동 계정에 붙은 판매 {n}건이 있습니다.",
            "조직 격리가 깨진 상태입니다. 이 달의 손익을 신뢰할 수 없습니다.",
            domain="SALES", entity_type="sales_order",
        ))

    # 판매처 불일치 (D12). 계정이 채널을 선언한 경우에만 비교한다 —
    # `sale_channel_id` 가 NULL 인 계정은 비교할 대상이 없다.
    n = n_of("""
        SELECT COUNT(*) FROM sales_order so
          JOIN integration_account ia ON ia.id = so.integration_account_id
          JOIN sale_channel sc ON sc.id = ia.sale_channel_id
         WHERE so.org_id = :org AND so.deleted_at IS NULL
           AND so.sale_date BETWEEN :s AND :e
           AND UPPER(sc.code) <> UPPER(so.marketplace)
    """)
    if n:
        out.append(_item(
            "SALE_ACCOUNT_MARKETPLACE_MISMATCH", BLOCKING, n,
            f"판매처와 연동 계정이 맞지 않는 판매 {n}건이 있습니다.",
            "예: 쿠팡 주문이 네이버 계정에 붙어 있습니다. "
            "정산 대사와 스토어별 손익이 틀어집니다.",
            domain="SALES", entity_type="sales_order",
        ))

    n = n_of("""
        SELECT COUNT(*) FROM sales_order so
         WHERE so.org_id = :org AND so.deleted_at IS NULL
           AND so.sale_date BETWEEN :s AND :e
           AND NOT EXISTS (
             SELECT 1 FROM product_margin_actual pma
              WHERE pma.sales_order_id = so.id AND pma.deleted_at IS NULL
           )
    """)
    if n:
        out.append(_item(
            "MARGIN_NOT_CALCULATED", WARNING, n,
            f"실 마진이 계산되지 않은 판매 {n}건이 있습니다.",
            "직접비가 누락되어 순이익이 실제보다 높게 나옵니다. 실 마진 화면에서 재계산하세요.",
            domain="SALES", entity_type="sales_order",
        ))

    # 적자 판매 — 차단하지 않는다. 미끼상품·재고 정리는 정상적인 업무 판단이다.
    n = n_of("""
        SELECT COUNT(*) FROM product_margin_actual pma
          JOIN sales_order so ON so.id = pma.sales_order_id
         WHERE so.org_id = :org AND so.deleted_at IS NULL AND pma.deleted_at IS NULL
           AND so.sale_date BETWEEN :s AND :e
           AND pma.actual_margin_amount < 0
    """)
    if n:
        out.append(_item(
            "LOSS_SALE", WARNING, n,
            f"적자로 계산된 판매 {n}건이 있습니다.",
            "의도한 손실인지 원가·수수료 입력 오류인지 확인하세요.",
            domain="SALES", entity_type="sales_order",
        ))

    return out


# ── 사업비용 ─────────────────────────────────────────────────────────────

def _expense_checks(db: Session, p: dict, n_of) -> list[ValidationItem]:
    out: list[ValidationItem] = []

    n = n_of("""
        SELECT COUNT(*) FROM expenses
         WHERE org_id = :org AND deleted_at IS NULL
           AND expense_date BETWEEN :s AND :e
           AND evidence_status IN ('NONE','NEEDS_CHECK')
    """)
    if n:
        out.append(_item(
            "MISSING_EVIDENCE", WARNING, n,
            f"증빙이 없는 비용 {n}건이 있습니다.",
            "세무 신고 시 손금 인정이 어려울 수 있습니다.",
            domain="EXPENSES", entity_type="expenses",
        ))

    # 삭제 처리 중인 증빙 — 파일이 아직 남아 있고 worker 가 처리 대기 중이다.
    if _has_table(db, "expense_attachment"):
        n = n_of("""
            SELECT COUNT(*) FROM expense_attachment a
              JOIN expenses e ON e.id = a.expense_id
             WHERE a.org_id = :org AND a.deleted_at IS NULL
               AND e.expense_date BETWEEN :s AND :e
               AND a.status = 'PENDING_DELETE'
        """)
        if n:
            out.append(_item(
                "ATTACHMENT_DELETE_PENDING", WARNING, n,
                f"삭제 처리 중인 증빙 {n}건이 있습니다.",
                "삭제 worker 가 아직 처리하지 않았습니다. 증빙 수가 확정되지 않은 상태입니다.",
                domain="EXPENSES", entity_type="expense_attachment",
            ))

    # 재계산 필요 표시 — 비용이 바뀌었는데 집계를 다시 돌리지 않았다는 뜻이다.
    # 이 상태로 확정하면 **낡은 숫자를 박제**한다. 차단이 맞다.
    if _has_table(db, "period_recalc_flag"):
        n = n_of("""
            SELECT COUNT(*) FROM period_recalc_flag
             WHERE org_id = :org AND year_month = :ym AND resolved_at IS NULL
        """)
        if n:
            out.append(_item(
                "PERIOD_RECALC_PENDING", BLOCKING, n,
                "이 달의 집계를 다시 계산해야 합니다.",
                "비용 또는 조정이 변경된 뒤 재계산이 실행되지 않았습니다. "
                "월별 정산에서 다시 계산한 뒤 확정하세요.",
                domain="EXPENSES", entity_type="period_recalc_flag",
            ))

    return out


# ── 정산 ─────────────────────────────────────────────────────────────────

def _settlement_checks(db: Session, p: dict, n_of) -> list[ValidationItem]:
    """정산 대사 검증.

    **전부 WARNING 이하다** (docs/47 §4). 판매처가 정산서를 늦게 주는 일이
    흔하고, 그걸 차단으로 만들면 마감이 판매처 일정에 인질로 잡힌다.
    조직 격리가 깨진 두 건만 예외다 — 그건 정산서 지연이 아니라 오류다.

    `AMBIGUOUS` 를 `UNMATCHED` 와 나눈다 (M055)
    ------------------------------------------
    ```
    AMBIGUOUS   후보는 있는데 라인이 여럿이라 고를 수 없다   사람이 고르면 끝난다
    UNMATCHED   후보가 없다                                 정산서·주문이 안 왔다
    ```

    안내해야 하는 작업이 다르다. 한 문구로 뭉치면 사용자는 둘 다 방치한다.
    """
    out: list[ValidationItem] = []
    # M055 이전 DB 에서도 검증이 돌아야 한다. 컬럼이 없으면 상태별 검사를
    # 건너뛰고 기존 NULL 기준으로만 본다.
    has_status = _has_column(db, "marketplace_settlement_record", "matching_status")

    total = n_of("""
        SELECT COUNT(*) FROM marketplace_settlement_record
         WHERE org_id = :org AND deleted_at IS NULL
           AND settlement_period_start BETWEEN :s AND :e
    """)

    if total == 0:
        out.append(_item(
            "SETTLEMENT_NOT_CONNECTED", WARNING, 0,
            "이 달의 쇼핑몰 정산 자료가 연결되지 않았습니다.",
            "판매·비용만으로 계산한 잠정 순이익입니다. "
            "정산서를 등록하면 실제 입금 기준으로 대사할 수 있습니다.",
            domain="SETTLEMENT", entity_type="marketplace_settlement_record",
        ))
        return out

    # 상태 컬럼이 있으면 `EXCLUDED`(주문과 무관한 정산행)를 뺀다. 광고비
    # 정산행을 "연결 안 됨" 으로 세면 영원히 줄지 않는 경고가 매달 쌓이고,
    # 그 순간부터 사람은 이 항목을 안 본다.
    unmatched = n_of("""
        SELECT COUNT(*) FROM marketplace_settlement_record
         WHERE org_id = :org AND deleted_at IS NULL
           AND settlement_period_start BETWEEN :s AND :e
           AND matched_sales_order_id IS NULL
    """ + ("AND matching_status = 'UNMATCHED'" if has_status else ""))
    # ── 정산서의 판매자 계정 (M033) ──────────────────────────────
    #
    # 주문(M032)과 **같은 규칙**이다. 정산서도 계정 단위로 오기 때문에,
    # 한쪽만 계정을 알면 본점 주문에 2호점 정산금이 붙어도 알아챌 수 없다.
    #
    # 계정 미상 자체는 오류가 아니다 — 실제 정산 파일이 아직 없어서
    # 모든 레코드가 그 상태다. 등급을 올리면 의미 없는 경고만 쌓인다.
    n = n_of("""
        SELECT COUNT(*) FROM marketplace_settlement_record msr
          JOIN integration_account ia ON ia.id = msr.integration_account_id
         WHERE msr.org_id = :org AND msr.deleted_at IS NULL
           AND msr.settlement_period_start BETWEEN :s AND :e
           AND ia.org_id <> msr.org_id
    """)
    if n:
        out.append(_item(
            "SETTLEMENT_ACCOUNT_ORG_MISMATCH", BLOCKING, n,
            f"다른 조직의 연동 계정에 붙은 정산 레코드 {n}건이 있습니다.",
            "조직 격리가 깨진 상태입니다. 이 달의 정산 대사를 신뢰할 수 없습니다.",
            domain="SETTLEMENT", entity_type="marketplace_settlement_record",
        ))

    # 계정이 채널을 선언한 경우에만 비교한다 — `provider` 는 판매처가 아니다(D12).
    n = n_of("""
        SELECT COUNT(*) FROM marketplace_settlement_record msr
          JOIN integration_account ia ON ia.id = msr.integration_account_id
          JOIN sale_channel sc ON sc.id = ia.sale_channel_id
         WHERE msr.org_id = :org AND msr.deleted_at IS NULL
           AND msr.settlement_period_start BETWEEN :s AND :e
           AND UPPER(sc.code) <> UPPER(msr.marketplace)
    """)
    if n:
        out.append(_item(
            "SETTLEMENT_ACCOUNT_MARKETPLACE_MISMATCH", BLOCKING, n,
            f"판매처와 연동 계정이 맞지 않는 정산 레코드 {n}건이 있습니다.",
            "예: 쿠팡 정산서가 네이버 계정에 붙어 있습니다. 대사 결과가 틀어집니다.",
            domain="SETTLEMENT", entity_type="marketplace_settlement_record",
        ))

    if unmatched:
        # D3 수정: BLOCKING → WARNING. 판매처 정산서 지연이 마감을 막으면 안 된다.
        out.append(_item(
            "UNMATCHED_SETTLEMENT", WARNING, unmatched,
            f"주문과 연결되지 않은 정산 {unmatched}건이 있습니다.",
            "그 주문번호로 장부에 잡힌 판매가 없습니다. 주문이 아직 안 들어왔거나, "
            "주문과 무관한 정산행(광고비·보증금)일 수 있습니다 — 후자면 대사 "
            "대상에서 제외하세요. 추측 매칭은 하지 않습니다.",
            domain="SETTLEMENT", entity_type="marketplace_settlement_record",
        ))

    if not has_status:
        return out

    # 후보는 있는데 고를 수 없는 것 (M055). 위와 **안내가 다르다** —
    # 이건 사람이 고르면 끝나는 일이다.
    n = n_of("""
        SELECT COUNT(*) FROM marketplace_settlement_record
         WHERE org_id = :org AND deleted_at IS NULL
           AND settlement_period_start BETWEEN :s AND :e
           AND matching_status = 'AMBIGUOUS'
    """)
    if n:
        out.append(_item(
            "AMBIGUOUS_SETTLEMENT", WARNING, n,
            f"연결할 주문을 고르지 못한 정산 {n}건이 있습니다.",
            "한 주문번호에 상품 라인이 여러 개라 어느 라인의 정산금인지 규칙으로는 "
            "알 수 없습니다. 정산 화면에서 후보 중 하나를 고르면 됩니다 "
            "(승인이 필요합니다).",
            domain="SETTLEMENT", entity_type="marketplace_settlement_record",
        ))

    # 연결은 끝났는데 확정하지 않은 정산 건. 확정은 월 마감과 다른 축이라
    # (docs/47 §4) **막지 않는다** — 부분 확정이 정상이다.
    n = n_of("""
        SELECT COUNT(*) FROM marketplace_settlement_record
         WHERE org_id = :org AND deleted_at IS NULL
           AND settlement_period_start BETWEEN :s AND :e
           AND confirmed_at IS NULL
           AND matching_status IN ('MATCHED_EXACT', 'MATCHED_MANUAL')
    """)
    if n:
        out.append(_item(
            "SETTLEMENT_NOT_CONFIRMED", INFO, n,
            f"연결됐지만 아직 확정하지 않은 정산 {n}건이 있습니다.",
            "정산 건 확정은 월 마감과 별개입니다. 확정하지 않아도 이 달을 "
            "마감할 수 있습니다.",
            domain="SETTLEMENT", entity_type="marketplace_settlement_record",
        ))

    # 정산서 매출과 장부 판매금액이 다른 주문 (docs/83 D4).
    #
    # **장부를 고치지 않는다.** 정산서가 더 정확할 가능성이 높지만, 판매
    # 원본을 정산서로 덮으면 "그때 무엇을 팔았나" 의 근거가 사라진다.
    # 실측에서 20건 중 2건이 걸렸다 — 수량 누락 1건(+25,500), 배송비매출
    # 누락 1건(+3,000). 둘 다 장부 쪽 결함이었다.
    if _has_column(db, "marketplace_settlement_record", "row_type"):
        n = n_of("""
            SELECT COUNT(*) FROM (
              SELECT m.matched_sales_order_id AS so_id,
                     SUM(m.gross_revenue) AS settled_gross
                FROM marketplace_settlement_record m
               WHERE m.org_id = :org AND m.deleted_at IS NULL
                 AND m.matching_status IN ('MATCHED_EXACT', 'MATCHED_MANUAL')
               GROUP BY 1
            ) g
            JOIN sales_order so ON so.id = g.so_id AND so.deleted_at IS NULL
           WHERE so.sale_date BETWEEN :s AND :e
             AND g.settled_gross <> so.sale_price
        """)
        if n:
            out.append(_item(
                "SETTLEMENT_REVENUE_MISMATCH", WARNING, n,
                f"정산서 매출과 장부 판매금액이 다른 주문이 {n}건 있습니다.",
                "판매 원본은 수정하지 않았습니다. 대개 장부의 수량이나 배송비"
                "매출이 빠진 경우입니다 — 판매 장부에서 확인하세요. "
                "실 마진은 정산서 금액으로 계산됩니다.",
                domain="SETTLEMENT", entity_type="sales_order",
            ))

    # 정산 조정이 확정 스냅샷에 들어 있지 않다. `sales_adjustment`·
    # `expense_adjustment` 와 **같은 상태**다(docs/80 §12-1) — 셋 중 하나만
    # 순이익에 접으면 규칙이 갈라지고 파트너 배분 금액이 바뀐다.
    #
    # 그 결정을 여기서 내리지 않는다. 잠그기 직전에 금액을 보여 준다.
    if _has_table(db, "settlement_adjustment"):
        delta = n_of("""
            SELECT COALESCE(SUM(net_settlement_delta), 0)
              FROM settlement_adjustment
             WHERE org_id = :org AND deleted_at IS NULL
               AND adjustment_period = :ym
        """)
        if delta:
            out.append(_item(
                "SETTLEMENT_ADJUSTMENT_NOT_IN_SNAPSHOT", WARNING, 1,
                f"이 달 확정 숫자에 반영되지 않은 정산 정정 {delta:,}원이 있습니다.",
                "확정 스냅샷은 판매·비용만 더한 값입니다. 취소·반품 조정"
                "(sales_adjustment)·사업비용 정정과 같은 상태이며, 조정을 "
                "순이익·배분에 접을지는 아직 정해지지 않았습니다. 금액을 "
                "확인하고 잠가 주세요.",
                domain="SETTLEMENT", entity_type="settlement_adjustment",
            ))

    return out


# ── 현금흐름 ─────────────────────────────────────────────────────────────

def _cashflow_checks(db: Session, p: dict, n_of) -> list[ValidationItem]:
    """현금 기록 검증 (Phase 8, M034).

    **여기서 검사하는 것은 현금 기록의 신뢰성뿐이다.** 현금흐름은 그 달의
    손익 숫자에 들어가지 않으므로(docs/54 §1) 확정을 막지 않는다 —
    등급을 올리면 마감이 통장 정리에 인질로 잡힌다.

    DB 가 이미 막는 것은 검사하지 않는다
    ---------------------------------
    같은 원본을 두 번 넣는 것은 부분 UNIQUE 가 막고(M034), 원본과 나란히 선
    행은 읽기 모델이 집계에서 뺀다. 이미 불가능한 상태를 검사하면 영원히 0건인
    항목이 목록만 채운다.

    **막지 못하는 것만 검사한다**: 원본을 가리키지 않은 채 같은 날 같은 금액으로
    들어온 수동 출금. DB 는 이것이 지급 기록과 같은 건인지 알 방법이 없다.
    """
    out: list[ValidationItem] = []
    if not _has_table(db, "manual_cash_event"):
        return out

    # ── 이중 계상 의심 ────────────────────────────────────────────
    #
    # 원본 연결이 없으면 DB 도 읽기 모델도 같은 건인지 알 수 없다.
    # 확신할 수 없으므로 지우거나 합치지 않는다 — 사람에게 보여주고 판단을 맡긴다.
    n = n_of("""
        SELECT COUNT(*)
          FROM manual_cash_event m
         WHERE m.org_id = :org AND m.deleted_at IS NULL
           AND m.direction = 'OUTFLOW' AND m.status = 'ACTUAL'
           AND m.source_type IS NULL
           AND m.occurred_at BETWEEN :s AND :e
           AND EXISTS (
                 SELECT 1 FROM payout_payment pp
                  WHERE pp.org_id = m.org_id AND pp.deleted_at IS NULL
                    AND pp.payment_date = m.occurred_at
                    AND pp.amount = m.amount
               )
    """)
    if n:
        out.append(_item(
            "CASH_POSSIBLE_DOUBLE_COUNT", WARNING, n,
            f"지급 기록과 날짜·금액이 같은 수동 출금 {n}건이 있습니다.",
            "같은 지급을 두 번 기록했다면 출금이 두 배로 잡힙니다. "
            "지급 장부에서 확인하고, 중복이면 수동 기록을 삭제하세요.",
            domain="CASHFLOW", entity_type="manual_cash_event",
        ))

    # ── 지나간 예정 ───────────────────────────────────────────────
    #
    # 예정일이 지났는데 그대로면 둘 중 하나다: 실제로 움직였는데 기록을 안 했거나,
    # 예정이 틀렸거나. 어느 쪽이든 예상 잔액이 틀린다.
    n = n_of("""
        SELECT COUNT(*) FROM manual_cash_event
         WHERE org_id = :org AND deleted_at IS NULL
           AND status <> 'ACTUAL'
           AND expected_at IS NOT NULL AND expected_at < :e
    """)
    if n:
        out.append(_item(
            "STALE_EXPECTED_CASHFLOW", WARNING, n,
            f"예정일이 지났는데 실제 여부가 기록되지 않은 입출금 {n}건이 있습니다.",
            "실제로 움직였으면 '실제' 로 바꾸고, 아니면 예정일을 고치세요. "
            "그대로 두면 예상 잔액이 계속 틀립니다.",
            domain="CASHFLOW", entity_type="manual_cash_event",
        ))

    # ── 설명 없는 실제 기록 ───────────────────────────────────────
    #
    # 나중에 "이 300만원이 뭐지" 에 답할 수 없는 줄이다. 금액이 틀린 건 아니므로
    # INFO 다 — 확정을 막지도, 확인을 요구하지도 않는다.
    n = n_of("""
        SELECT COUNT(*) FROM manual_cash_event
         WHERE org_id = :org AND deleted_at IS NULL
           AND status = 'ACTUAL' AND source_type IS NULL
           AND occurred_at BETWEEN :s AND :e
           AND COALESCE(NULLIF(TRIM(category), ''), NULLIF(TRIM(memo), '')) IS NULL
    """)
    if n:
        out.append(_item(
            "CASH_ACTUAL_UNEXPLAINED", INFO, n,
            f"분류·메모가 없는 수동 입출금 {n}건이 있습니다.",
            "나중에 이 금액이 무엇이었는지 확인할 수 없습니다.",
            domain="CASHFLOW", entity_type="manual_cash_event",
        ))

    return out


# ── 소싱 ─────────────────────────────────────────────────────────────────

def _sourcing_checks(db: Session, p: dict, n_of) -> list[ValidationItem]:
    """소싱 데이터 품질 (M037).

    **전부 WARNING 이하다.** 소싱 정보가 없어도 그 달의 손익은 계산된다 —
    판매·비용이 이미 있기 때문이다. 확정을 막으면 마감이 소싱 정리에 인질로 잡힌다.

    기존 상품에 소싱처가 없는 것은 **정상이다**
    ----------------------------------------
    M037 이전 상품은 전부 소싱처가 없다. 자동 backfill 을 하지 않았기 때문이다
    (URL 만 보고 공급처를 만들면 근거 없는 법인이 생긴다). 그래서 INFO 다 —
    WARNING 으로 올리면 첫 스캔에 수백 건이 뜨고 사용자는 검증 전체를 무시한다.
    """
    out: list[ValidationItem] = []
    if not _has_table(db, "product_source"):
        return out

    # 판매 중인데 소싱처를 모르는 상품. 재주문할 때 어디서 사야 할지 알 수 없다.
    n = n_of("""
        SELECT COUNT(*) FROM product pr
         WHERE pr.org_id = :org AND pr.deleted_at IS NULL
           AND pr.status = 'ACTIVE'
           AND NOT EXISTS (
                 SELECT 1 FROM product_source ps
                  WHERE ps.product_id = pr.id AND ps.deleted_at IS NULL
               )
    """)
    if n:
        out.append(_item(
            "PRODUCT_WITHOUT_SOURCE", INFO, n,
            f"소싱처가 등록되지 않은 판매 중 상품 {n}건이 있습니다.",
            "재주문할 때 어디서 사야 하는지 알 수 없습니다. "
            "기존 상품은 자동 연결하지 않았으므로 직접 등록해야 합니다.",
            domain="SOURCING", entity_type="product",
        ))

    # 주 소싱처는 있는데 원가가 없다 — 예상 마진을 계산할 수 없다.
    n = n_of("""
        SELECT COUNT(*) FROM product_source ps
         WHERE ps.org_id = :org AND ps.deleted_at IS NULL
           AND ps.is_primary AND ps.status = 'ACTIVE'
           AND NOT EXISTS (
                 SELECT 1 FROM product_source_cost_version cv
                  WHERE cv.product_source_id = ps.id AND cv.deleted_at IS NULL
               )
    """)
    if n:
        out.append(_item(
            "PRIMARY_SOURCE_MISSING_COST", WARNING, n,
            f"매입가가 등록되지 않은 주 소싱처 {n}건이 있습니다.",
            "예상 원가와 권장 판매가를 계산할 수 없습니다.",
            domain="SOURCING", entity_type="product_source",
        ))

    # 외화 소싱인데 환율이 없다 — landed cost 가 계산 불가로 남는다.
    n = n_of("""
        SELECT COUNT(DISTINCT cv.currency)
          FROM product_source_cost_version cv
          JOIN product_source ps ON ps.id = cv.product_source_id
         WHERE cv.org_id = :org AND cv.deleted_at IS NULL AND ps.deleted_at IS NULL
           AND cv.currency <> 'KRW'
           AND NOT EXISTS (
                 SELECT 1 FROM fx_rate_snapshot fx
                  WHERE fx.org_id = cv.org_id AND fx.deleted_at IS NULL
                    AND fx.base_currency = cv.currency AND fx.quote_currency = 'KRW'
               )
    """)
    if n:
        out.append(_item(
            "FX_REQUIRED_BUT_MISSING", WARNING, n,
            f"환율이 등록되지 않은 외화 {n}종으로 매입가가 잡혀 있습니다.",
            "환율이 없으면 원화 예상 원가를 계산할 수 없습니다. "
            "임의 환율로 계산하지 않습니다.",
            domain="SOURCING", entity_type="fx_rate_snapshot",
        ))

    # 저장된 링크 중 http/https 가 아닌 것. 화면이 링크로 렌더하면 위험하다.
    n = n_of("""
        SELECT COUNT(*) FROM product_source
         WHERE org_id = :org AND deleted_at IS NULL
           AND source_url IS NOT NULL AND source_url <> ''
           AND source_url !~* '^https?://'
    """)
    if n:
        out.append(_item(
            "SOURCE_URL_INVALID", WARNING, n,
            f"형식이 올바르지 않은 소싱 링크 {n}건이 있습니다.",
            "http 또는 https 로 시작하지 않는 주소입니다.",
            domain="SOURCING", entity_type="product_source",
        ))

    # ⚠️ SOURCE_COST_STALE 은 만들지 않았다.
    #    "며칠이 지나면 오래된 원가인가" 는 업무 기준이고 우리가 정할 수 없다.
    #    임의 기준(예: 90일)을 넣으면 근거 없는 경고가 매달 쌓인다.
    #    기준이 정해지면 여기에 추가한다.

    return out


# ── 외부 연동 ────────────────────────────────────────────────────────────

def _broken_secret_reference_count(db: Session, p: dict) -> int:
    """참조는 있는데 저장소에 없는 계정 수.

    저장소 구현을 통해 묻는다 — 관리형 저장소로 바뀌면 `credential_secret`
    테이블을 직접 세는 방식은 틀린 답을 준다.
    """
    from app.core.secret_store import get_secret_store

    refs = db.execute(text("""
        SELECT credential_secret_ref FROM external_account
         WHERE org_id = :org AND deleted_at IS NULL
           AND credential_secret_ref IS NOT NULL
    """), {"org": p["org"]}).scalars().all()
    if not refs:
        return 0

    try:
        store = get_secret_store()
    except Exception:  # noqa: BLE001
        # 저장소 설정 자체가 안 된 환경. 그건 다른 문제이고, 여기서
        # "전부 끊어졌다" 고 보고하면 마감 화면이 거짓 경고로 덮인다.
        return 0
    return sum(
        1 for ref in refs
        if not store.exists(org_id=UUID(p["org"]), secret_ref=ref)
    )


def _external_checks(db: Session, p: dict, n_of) -> list[ValidationItem]:
    """외부 연동 상태 (M038~M040).

    **전부 WARNING 이하다.** 외부 동기화가 멈춰도 그 달의 손익은 계산된다 —
    판매·비용이 이미 있기 때문이다. 확정을 막으면 마감이 셀러툴 장애에 인질로 잡힌다.

    기간과 무관한 검사다
    -----------------
    다른 검사는 그 달의 데이터를 본다. 여기는 "지금 연동이 정상인가" 를 본다 —
    지난달을 마감하면서도 오늘 끊긴 연동을 알아야 한다.

    `SYNC_STALE` 은 만들지 않았다
    ---------------------------
    "몇 시간이면 오래된 동기화인가" 는 업무 기준이고 우리가 정할 수 없다.
    일 1회 도는 조직과 시간당 도는 조직의 기준이 다르다. 설정이 생기면 추가한다.
    """
    out: list[ValidationItem] = []
    if not _has_table(db, "external_account"):
        return out

    # 등록해 놓고 인증정보가 없는 계정. 사용자는 "연결됨" 으로 오해하기 쉽다.
    #
    # vault 쪽지가 아니라 **실제 인증정보**를 본다(M042). 쪽지에는 값이 없어서
    # 무인 배치가 열 수 없다 — 쪽지만 보고 "설정됨" 으로 세면 왜 매일 밤
    # 실패하는지 아무도 모른다.
    n = n_of("""
        SELECT COUNT(*) FROM external_account a
          JOIN external_system s ON s.id = a.external_system_id
         WHERE a.org_id = :org AND a.deleted_at IS NULL
           AND a.status <> 'DISABLED'
           AND a.credential_secret_ref IS NULL
    """)
    if n:
        out.append(_item(
            "CREDENTIAL_NOT_CONFIGURED", INFO, n,
            f"자격증명이 설정되지 않은 외부 계정 {n}건이 있습니다.",
            "동기화를 실행할 수 없습니다. 등록만 해 둔 상태라면 정상입니다.",
            domain="EXTERNAL", entity_type="external_account",
        ))

    # 계정은 "설정됨" 이라는데 저장소에 비밀이 없는 상태.
    #
    # 키 교체 실수, DB 만 복원한 백업 복구, 저장소 장애에서 생긴다. 화면은
    # 계속 "설정됨" 이라고 말하므로 사람이 먼저 알아차릴 방법이 없다 —
    # 그래서 마감 점검이 짚는다.
    #
    # **복호화하지 않는다.** 존재만 확인한다 — 있는지 묻는 데 평문이 필요하지 않고,
    # 마스터 키가 없는 환경에서도 이 검사는 돌아야 한다.
    broken = _broken_secret_reference_count(db, p)
    if broken:
        out.append(_item(
            "CREDENTIAL_REFERENCE_BROKEN", WARNING, broken,
            f"자격증명 참조가 끊어진 외부 계정 {broken}건이 있습니다.",
            "설정됨으로 보이지만 실제 인증정보가 없습니다. 다시 설정해야 합니다 — "
            "마스터 키 교체나 DB 복원 과정을 함께 확인하세요.",
            domain="EXTERNAL", entity_type="external_account",
        ))

    # 가장 최근 실행이 실패한 계정. **매 실패마다 쌓지 않는다** —
    # 계정당 한 건으로 묶어 세지 않으면 이슈 목록이 실패 로그가 된다.
    n = n_of("""
        SELECT COUNT(*) FROM (
          SELECT DISTINCT ON (external_account_id) status
            FROM external_sync_run
           WHERE org_id = :org
           ORDER BY external_account_id, started_at DESC
        ) latest
         WHERE latest.status = 'FAILED'
    """)
    if n:
        out.append(_item(
            "SYNC_FAILED", WARNING, n,
            f"마지막 동기화가 실패한 외부 계정 {n}건이 있습니다.",
            "외부 데이터가 최신이 아닙니다. 연결 시험으로 원인을 확인하세요.",
            domain="EXTERNAL", entity_type="external_sync_run",
        ))

    # 일부만 반영된 실행. 전체 성공과 구분해야 한다 — "동기화 완료" 로 보이면
    # 빠진 건이 있다는 사실을 아무도 모른다.
    n = n_of("""
        SELECT COUNT(*) FROM (
          SELECT DISTINCT ON (external_account_id) status
            FROM external_sync_run
           WHERE org_id = :org
           ORDER BY external_account_id, started_at DESC
        ) latest
         WHERE latest.status = 'PARTIAL'
    """)
    if n:
        out.append(_item(
            "SYNC_PARTIAL_FAILURE", WARNING, n,
            f"일부만 반영된 동기화가 {n}건 있습니다.",
            "전체가 성공한 것이 아닙니다. 실패한 건을 확인하세요.",
            domain="EXTERNAL", entity_type="external_sync_run",
        ))

    # 사람이 고친 값을 외부가 덮으려다 멈춘 자리. 방치하면 그 필드는
    # 영원히 외부와 다른 상태로 남는다.
    n = n_of("""
        SELECT COUNT(*) FROM external_sync_conflict
         WHERE org_id = :org AND resolution = 'PENDING'
    """)
    if n:
        out.append(_item(
            "SYNC_CONFLICT", WARNING, n,
            f"확인이 필요한 동기화 충돌 {n}건이 있습니다.",
            "외부 값과 직접 수정한 값이 다릅니다. 내부 값을 유지했습니다.",
            domain="EXTERNAL", entity_type="external_sync_conflict",
        ))

    # 사용 중지한 계정에 살아 있는 연결. 삭제가 아니라 중지이므로 정상일 수
    # 있다 — 그래서 INFO 다. 다시 켤 때 무엇이 딸려 오는지 알려 주는 용도다.
    n = n_of("""
        SELECT COUNT(*) FROM external_entity_mapping m
          JOIN external_account a ON a.id = m.external_account_id
         WHERE m.org_id = :org AND m.deleted_at IS NULL
           AND a.status = 'DISABLED' AND a.deleted_at IS NULL
    """)
    if n:
        out.append(_item(
            "EXTERNAL_ACCOUNT_DISABLED_WITH_ACTIVE_MAPPING", INFO, n,
            f"사용 중지한 계정에 연결이 {n}건 남아 있습니다.",
            "연결은 그대로 보존됩니다. 계정을 다시 켜면 함께 동작합니다.",
            domain="EXTERNAL", entity_type="external_entity_mapping",
        ))

    # 가리키는 내부 상품이 사라진 연결. FK 가 없는 컬럼이라 DB 가 막지 못한다.
    n = n_of("""
        SELECT COUNT(*) FROM external_entity_mapping m
         WHERE m.org_id = :org AND m.deleted_at IS NULL
           AND m.internal_entity_type = 'PRODUCT'
           AND NOT EXISTS (
                 SELECT 1 FROM product pr
                  WHERE pr.id = m.internal_entity_id AND pr.deleted_at IS NULL
               )
    """)
    if n:
        out.append(_item(
            "EXTERNAL_MAPPING_ORPHAN", WARNING, n,
            f"대상 상품이 없는 외부 연결 {n}건이 있습니다.",
            "상품이 삭제됐거나 다른 조직 것을 가리키고 있습니다. "
            "동기화가 이 항목을 건너뜁니다.",
            domain="EXTERNAL", entity_type="external_entity_mapping",
        ))

    return out


# ── 승인 ─────────────────────────────────────────────────────────────────

def _approval_checks(db: Session, p: dict, n_of) -> list[ValidationItem]:
    """승인 상태 검증.

    귀속월은 `effective_from` 으로 본다. 요청 시각이 아니다 — 8월에 낸 요청이
    7월 비용을 바꾸는 것이면 그건 7월 마감의 문제다.
    """
    out: list[ValidationItem] = []
    where_month = """
        AND COALESCE(effective_from, requested_at::date) BETWEEN :s AND :e
    """

    n = n_of(f"""
        SELECT COUNT(*) FROM approval_request
         WHERE org_id = :org AND deleted_at IS NULL
           AND status = 'PENDING' {where_month}
    """)
    if n:
        out.append(_item(
            "APPROVAL_PENDING", WARNING, n,
            f"결재를 기다리는 승인 요청 {n}건이 있습니다.",
            "승인되면 이 달의 숫자가 바뀝니다. 확정 전에 처리하는 것을 권합니다.",
            domain="APPROVALS", entity_type="approval_request",
        ))

    # 승인됐는데 아직 반영되지 않은 요청 — 확정하면 **승인 내용이 빠진 숫자**가 박제된다.
    n = n_of(f"""
        SELECT COUNT(*) FROM approval_request
         WHERE org_id = :org AND deleted_at IS NULL
           AND status = 'APPROVED'
           AND COALESCE(auto_apply_status, 'PENDING') <> 'DONE' {where_month}
    """)
    if n:
        out.append(_item(
            "APPROVAL_APPROVED_NOT_APPLIED", BLOCKING, n,
            f"승인됐지만 아직 반영되지 않은 변경 {n}건이 있습니다.",
            "승인 화면에서 적용하거나 적용 실패 원인을 해결하세요. "
            "지금 확정하면 승인 내용이 빠진 숫자가 확정됩니다.",
            domain="APPROVALS", entity_type="approval_request",
        ))

    n = n_of(f"""
        SELECT COUNT(*) FROM approval_request
         WHERE org_id = :org AND deleted_at IS NULL
           AND auto_apply_status = 'PERMANENT_FAILURE' {where_month}
    """)
    if n:
        out.append(_item(
            "APPROVAL_APPLY_FAILED", BLOCKING, n,
            f"적용에 영구 실패한 승인 {n}건이 있습니다.",
            "원인을 해결하고 재시도하거나 요청을 취소하세요.",
            domain="APPROVALS", entity_type="approval_request",
        ))

    n = n_of(f"""
        SELECT COUNT(*) FROM approval_request
         WHERE org_id = :org AND deleted_at IS NULL
           AND auto_apply_status = 'HELD_BY_LOCK' {where_month}
    """)
    if n:
        # 잠긴 달 때문에 대기 중이다. 그 달을 지금 확정하는 흐름과 직접 충돌하지는
        # 않지만, 재오픈 없이는 영원히 반영되지 않는다는 사실을 알려야 한다.
        out.append(_item(
            "APPROVAL_HELD_BY_LOCK", WARNING, n,
            f"월 잠금 때문에 반영되지 못한 승인 {n}건이 있습니다.",
            "해당 월을 재오픈해야 반영됩니다. 그대로 두면 계속 대기 상태로 남습니다.",
            domain="APPROVALS", entity_type="approval_request",
        ))

    n = n_of(f"""
        SELECT COUNT(*) FROM approval_request
         WHERE org_id = :org AND deleted_at IS NULL
           AND auto_apply_status = 'RETRYING' {where_month}
    """)
    if n:
        out.append(_item(
            "APPROVAL_RETRY_PENDING", WARNING, n,
            f"재시도 대기 중인 승인 적용 {n}건이 있습니다.",
            "일시 오류로 재시도 중입니다. 잠시 후 다시 검증하세요.",
            domain="APPROVALS", entity_type="approval_request",
        ))

    return out


# ── 정책 ─────────────────────────────────────────────────────────────────

def _policy_checks(db: Session, p: dict, n_of) -> list[ValidationItem]:
    out: list[ValidationItem] = []

    # 배분 비율 합 — 100% 가 아니면 잔액이 붕 뜨거나 초과 배분된다
    row = db.execute(text("""
        SELECT COALESCE(SUM(share_ratio), 0) AS s
          FROM partner_share_policy
         WHERE org_id = :org AND deleted_at IS NULL AND is_active = TRUE
           AND (applies_year_month IS NULL OR applies_year_month = :ym)
    """), {"org": p["org"], "ym": p["ym"]}).first()
    ratio_sum = float(row.s) if row else 0.0
    if ratio_sum > 0 and abs(ratio_sum - 1.0) > 0.0001:
        out.append(_item(
            "DISTRIBUTION_RATIO_INVALID", BLOCKING, 1,
            f"수익배분 비율 합이 {ratio_sum * 100:.1f}% 입니다.",
            "100% 가 되어야 잔액이 정확히 나뉩니다. 설정 > 파트너 배분에서 수정하세요.",
            domain="POLICY", entity_type="partner_share_policy",
        ))

    if _has_column(db, "tax_policy", "verified_at"):
        n = n_of("""
            SELECT COUNT(*) FROM tax_policy
             WHERE org_id = :org AND deleted_at IS NULL AND is_active = TRUE
               AND verified_at IS NULL
        """)
        if n:
            out.append(_item(
                "UNVERIFIED_TAX_POLICY", WARNING, n,
                f"세무 확인이 되지 않은 세금 정책 {n}건이 적용 중입니다.",
                "설정 > 사업자·세금 설정에서 확인 후 표시하세요.",
                domain="POLICY", entity_type="tax_policy",
            ))

    # 기간이 끝난 수수료 정책이 여전히 활성 — 이 달 계산에 낡은 요율이 쓰였을 수 있다
    n = n_of("""
        SELECT COUNT(*) FROM marketplace_fee_policy
         WHERE org_id = :org AND deleted_at IS NULL AND is_active = TRUE
           AND effective_to IS NOT NULL AND effective_to < :s
    """)
    if n:
        out.append(_item(
            "EXPIRED_FEE_POLICY", WARNING, n,
            f"유효기간이 지난 마켓 수수료 정책 {n}건이 활성 상태입니다.",
            "정책 > 수수료에서 기간을 연장하거나 새 버전을 등록하세요.",
            domain="POLICY", entity_type="marketplace_fee_policy",
        ))

    # 신뢰도가 검증되지 않은 계산 정책 — 자동 수집·검토 필요 상태
    n = n_of("""
        SELECT COUNT(*) FROM marketplace_fee_policy
         WHERE org_id = :org AND deleted_at IS NULL AND is_active = TRUE
           AND reliability_status IN ('NEEDS_REVIEW','AUTO_DETECTED','EXPIRED')
           AND effective_from <= :e
    """)
    if n:
        out.append(_item(
            "UNVERIFIED_CALC_POLICY", WARNING, n,
            f"검증되지 않은 계산 정책 {n}건이 이 달 계산에 쓰였습니다.",
            "자동 수집되었거나 검토 대기 상태입니다. 정책 화면에서 확인하세요.",
            domain="POLICY", entity_type="marketplace_fee_policy",
        ))

    return out


# ── 운영 이슈 ────────────────────────────────────────────────────────────

def _issue_checks(db: Session, p: dict, n_of) -> list[ValidationItem]:
    if not _has_table(db, "operation_issue"):
        return []
    n = n_of("""
        SELECT COUNT(*) FROM operation_issue
         WHERE org_id = :org AND deleted_at IS NULL
           AND blocks_month_close = TRUE
           AND status IN ('OPEN','IN_PROGRESS','WAITING')
    """)
    if not n:
        return []
    return [_item(
        "OPEN_BLOCKING_ISSUE", BLOCKING, n,
        f"처리되지 않은 마감 차단 이슈 {n}건이 있습니다.",
        "운영 이슈 화면에서 처리하거나 사유와 함께 무시 처리하세요.",
        domain="ISSUES", entity_type="operation_issue",
    )]


# ──────────────────────────────────────────────────────────────────────────
# run 영속화
# ──────────────────────────────────────────────────────────────────────────

def persist_run(
    db: Session, *, org_id: UUID, year_month: str, close_state: str,
    trigger: str, actor_user_id: UUID | None,
) -> tuple[UUID, list[ValidationItem]]:
    """검증을 실행하고 run + item 을 기록한다. **커밋하지 않는다.**

    커밋하지 않는 이유: 확정 트랜잭션이 이 run 을 참조한다. 여기서 커밋하면
    뒤에서 확정이 롤백돼도 run 만 남아 "검증은 통과했는데 확정은 없는" 상태가 된다.
    호출자가 커밋 시점을 정한다.

    검증 자체가 터져도 run 은 FAILED 로 남긴다 — 실패한 검증을 "이상 없음" 으로
    읽는 것이 가장 위험하다.
    """
    started = time.monotonic()
    run_id = db.execute(text("""
        INSERT INTO monthly_close_check_run (
          org_id, year_month, trigger, close_state_at_run, status, actor_user_id, created_by
        ) VALUES (:org, :ym, :trg, :state, 'RUNNING', :actor, :actor)
        RETURNING id
    """), {
        "org": str(org_id), "ym": year_month, "trg": trigger,
        "state": close_state, "actor": str(actor_user_id) if actor_user_id else None,
    }).scalar_one()

    try:
        items = run_checks(db, org_id=org_id, year_month=year_month)
    except Exception as exc:
        db.execute(text("""
            UPDATE monthly_close_check_run
               SET status = 'FAILED', finished_at = now(), duration_ms = :ms,
                   error_text = :err
             WHERE id = :id
        """), {
            "id": str(run_id), "ms": int((time.monotonic() - started) * 1000),
            "err": f"{type(exc).__name__}: {exc}"[:2000],
        })
        raise

    for it in items:
        db.execute(text("""
            INSERT INTO monthly_close_check_item (
              run_id, org_id, year_month, code, severity, entity_type, entity_id,
              affected_count, message, hint, dedupe_key
            ) VALUES (
              :run, :org, :ym, :code, :sev, :etype, :eid,
              :cnt, :msg, :hint, :dedupe
            )
        """), {
            "run": str(run_id), "org": str(org_id), "ym": year_month,
            "code": it.code, "sev": it.grade, "etype": it.entity_type,
            "eid": str(it.entity_id) if it.entity_id else None,
            "cnt": it.count, "msg": it.message, "hint": it.hint,
            "dedupe": dedupe_key_for(year_month, it.code),
        })

    db.execute(text("""
        UPDATE monthly_close_check_run
           SET status = 'COMPLETED', finished_at = now(), duration_ms = :ms,
               blocking_count = :b, warning_count = :w, info_count = :i
         WHERE id = :id
    """), {
        "id": str(run_id), "ms": int((time.monotonic() - started) * 1000),
        "b": sum(1 for i in items if i.grade == BLOCKING),
        "w": sum(1 for i in items if i.grade == WARNING),
        "i": sum(1 for i in items if i.grade == INFO),
    })

    # 기존 계약 유지 — 화면이 monthly_settlement 에서 최근 결과를 읽는다.
    db.execute(text("""
        UPDATE monthly_settlement
           SET validation_result_json = CAST(:payload AS JSONB),
               validated_at = now(),
               last_check_run_id = :run,
               updated_by = COALESCE(:actor, updated_by)
         WHERE org_id = :org AND year_month = :ym AND deleted_at IS NULL
    """), {
        "payload": json.dumps([_as_json(i) for i in items], ensure_ascii=False),
        "run": str(run_id), "org": str(org_id), "ym": year_month,
        "actor": str(actor_user_id) if actor_user_id else None,
    })

    return UUID(str(run_id)), items


def dedupe_key_for(year_month: str, code: str) -> str:
    """이슈 센터와 공유하는 키. 같은 달 같은 코드는 같은 일이다."""
    return f"MONTH_CLOSE:{year_month}:{code}"


def _as_json(item: ValidationItem) -> dict[str, Any]:
    return {
        "code": item.code,
        "severity": item.severity,
        "grade": item.grade,
        "blocking": item.blocking,
        "count": item.count,
        "message": item.message,
        "hint": item.hint,
        "domain": item.domain,
        "entity_type": item.entity_type,
    }


def as_json(item: ValidationItem) -> dict[str, Any]:
    """라우터 직렬화용 공개 헬퍼."""
    return _as_json(item)


def get_run(db: Session, *, org_id: UUID, run_id: UUID) -> dict[str, Any] | None:
    row = db.execute(text("""
        SELECT id, year_month, trigger, close_state_at_run, status, error_text,
               blocking_count, warning_count, info_count,
               started_at, finished_at, duration_ms, actor_user_id
          FROM monthly_close_check_run
         WHERE id = :id AND org_id = :org
    """), {"id": str(run_id), "org": str(org_id)}).mappings().first()
    if row is None:
        return None
    d = dict(row)
    d["items"] = [dict(r) for r in db.execute(text("""
        SELECT code, severity, entity_type, entity_id, affected_count,
               message, hint, dedupe_key, detected_at
          FROM monthly_close_check_item
         WHERE run_id = :id
         ORDER BY CASE severity WHEN 'BLOCKING' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END, code
    """), {"id": str(run_id)}).mappings().all()]
    return d


def list_runs(
    db: Session, *, org_id: UUID, year_month: str, limit: int = 20
) -> list[dict[str, Any]]:
    rows = db.execute(text("""
        SELECT id, trigger, close_state_at_run, status,
               blocking_count, warning_count, info_count,
               started_at, finished_at, duration_ms, actor_user_id
          FROM monthly_close_check_run
         WHERE org_id = :org AND year_month = :ym
         ORDER BY started_at DESC
         LIMIT :lim
    """), {"org": str(org_id), "ym": year_month, "lim": limit}).mappings().all()
    return [dict(r) for r in rows]
