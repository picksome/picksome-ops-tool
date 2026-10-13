"""대시보드 집계 (Phase 45).

왜 이 모듈이 따로 있는가
--------------------
대시보드 이전 구현은 프론트엔드가 `MOCK_PRODUCTS`/`MOCK_EXPENSES` 에서 숫자를
만들어 냈다. 실제 금액처럼 보이는 sample 이었고, "8월 순이익" 이 DB 와 무관했다.

그렇다고 프론트엔드가 여러 endpoint 를 조합해 순이익을 다시 계산하게 두면
같은 산식이 두 벌이 된다. 마감 화면과 대시보드가 다른 숫자를 말하는 순간
둘 다 신뢰를 잃는다. 그래서 **집계를 서버 한 곳에서** 한다.

진행 중인 달과 확정된 달은 source 가 다르다
--------------------------------------
```
OPEN / REVIEW        → close_snapshot.compute_figures()  (LIVE)
CONFIRMED / LOCKED   → monthly_close_snapshot 행         (MONTH_CLOSE_SNAPSHOT)
```

확정한 달을 raw 테이블에서 다시 집계하지 않는다. 그러면 확정 이후 판매 한 건이
바뀌었을 때 "7월 확정 순이익" 이 어제와 오늘 다르다 — 그건 확정이 아니다.

두 source 는 **같은 함수**(`compute_figures`)가 만든 같은 컬럼 이름을 쓴다.
snapshot 은 그 함수의 결과를 확정 시점에 박제한 것이므로, 열 이름과 산식이
자동으로 일치한다. 산식을 한 벌만 유지하기 위한 설계다.

섞지 않는다
--------
모든 숫자는 자기 source 를 달고 나간다. 진행월(LIVE)과 확정월(SNAPSHOT)을
비교할 때는 `comparable=False` 와 사유를 함께 준다 — 화면이 그 사실을 표시한다.

없는 값과 0원을 구분한다
--------------------
정산이 연결되지 않은 달의 정산액은 0원이 아니라 **모른다** 이다.
그런 KPI 는 `value=None` + `availability="NOT_CONNECTED"` 로 내려간다.
0 으로 채우면 화면이 "정산 예정 0원" 이라고 단정한다.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import cashflow, close_snapshot, operation_issue
from app.services.settlement import month_range

# KPI/트렌드가 붙일 수 있는 source. 화면이 문자열을 그대로 라벨로 쓰지 않고
# 자기 사전으로 번역하므로, 여기 값은 의미가 고정된 코드다.
SOURCE_LIVE = "LIVE"
SOURCE_SNAPSHOT = "MONTH_CLOSE_SNAPSHOT"

# KPI 가용성.
AVAILABLE = "AVAILABLE"
NOT_CONNECTED = "NOT_CONNECTED"

_LIVE_STATES = ("OPEN", "REVIEW")

# 대시보드가 다루는 최대 트렌드 길이. 월마다 집계 쿼리가 돌기 때문에 상한을 둔다.
MAX_TREND_MONTHS = 24


def _shift_month(year_month: str, delta: int) -> str:
    y, m = int(year_month[:4]), int(year_month[5:7])
    total = y * 12 + (m - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def current_year_month(today: date | None = None) -> str:
    d = today or date.today()
    return d.strftime("%Y-%m")


def _close_state(db: Session, *, org_id: UUID, year_month: str) -> tuple[str, bool]:
    """(close_state, exists). 정산 행이 없는 달은 '아직 열려 있는 달' 이다."""
    row = db.execute(text("""
        SELECT close_state FROM monthly_settlement
         WHERE org_id = :org AND year_month = :ym AND deleted_at IS NULL
         ORDER BY created_at DESC LIMIT 1
    """), {"org": str(org_id), "ym": year_month}).first()
    return (row.close_state, True) if row else ("OPEN", False)


def month_figures(db: Session, *, org_id: UUID, year_month: str) -> dict[str, Any]:
    """한 달치 숫자 + 그 숫자의 출처.

    확정 snapshot 이 살아 있으면 그것이 진실이다. 없으면 지금 집계한다.
    두 경로가 같은 키를 돌려주므로 호출자는 분기하지 않아도 된다.
    """
    close_state, exists = _close_state(db, org_id=org_id, year_month=year_month)
    snap = close_snapshot.get_current(db, org_id=org_id, year_month=year_month)

    if snap is not None:
        figures = dict(snap)
        source = SOURCE_SNAPSHOT
        generation = int(snap["generation"])
        taken_at = snap["taken_at"]
    else:
        figures = close_snapshot.compute_figures(
            db, org_id=org_id, year_month=year_month
        )
        source = SOURCE_LIVE
        generation = None
        taken_at = None

    start, end = month_range(year_month)
    return {
        "year_month": year_month,
        "start": start,
        "end": end,
        "close_state": close_state,
        "settlement_row_exists": exists,
        "source": source,
        "snapshot_generation": generation,
        "taken_at": taken_at,
        "figures": figures,
        "profit_basis": figures.get("profit_basis"),
        "settlement_connected": bool(figures.get("settlement_connected")),
        "profit_label": profit_label(close_state, source, figures),
    }


def profit_label(close_state: str, source: str, figures: dict[str, Any]) -> str:
    """이 달의 순이익을 뭐라고 불러야 하는가.

    `monthly_close._profit_label` 과 같은 원칙이다 — 정산이 연결되지 않았는데
    그냥 "확정 순이익" 이라고 부르면 과장이다(Phase 30). 여기서는 진행 중인
    달까지 다루므로 "예상/잠정" 단계를 추가로 구분한다.
    """
    if source == SOURCE_LIVE:
        return "잠정 순이익 (미확정)"
    reconciled = figures.get("profit_basis") == "SETTLEMENT_RECONCILED"
    if close_state == "LOCKED":
        return "확정 순이익" if reconciled else "확정 순이익 (정산 미연결 — 판매·비용 기준)"
    return "검토 확정 순이익" if reconciled else "검토 확정 순이익 (정산 미연결)"


def _rate(numerator: int, denominator: int) -> float | None:
    """비율. 분모가 0이면 0.0 이 아니라 None — '0%' 와 '모름' 은 다르다."""
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


# ──────────────────────────────────────────────────────────────────────────
# KPI
# ──────────────────────────────────────────────────────────────────────────

def _kpi(
    key: str, label: str, value: Any, fmt: str, source: str, *,
    higher_is_better: bool = True, hint: str | None = None,
    availability: str = AVAILABLE,
    previous: Any = None, previous_source: str | None = None,
    comparable: bool = True,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "value": value,
        "format": fmt,
        "source": source,
        "availability": availability,
        "higher_is_better": higher_is_better,
        "hint": hint,
        "previous": previous,
        "previous_source": previous_source,
        # source 가 다른 값끼리의 증감은 의미가 약하다. 화면이 그걸 표시한다.
        "comparable": comparable,
    }


def _active_product_count(db: Session, *, org_id: UUID) -> int:
    """지금 판매 중인 상품 수. 특정 달의 값이 아니라 **현재 상태**다."""
    return int(db.execute(text("""
        SELECT COUNT(*) FROM product
         WHERE org_id = :org AND deleted_at IS NULL AND status = 'ACTIVE'
    """), {"org": str(org_id)}).scalar() or 0)


def build_kpis(
    db: Session, *, org_id: UUID, period: dict[str, Any], prev: dict[str, Any],
) -> list[dict[str, Any]]:
    """KPI 8종. 각 값은 자기 source 를 달고 나간다."""
    f, pf = period["figures"], prev["figures"]
    src, psrc = period["source"], prev["source"]
    confirmed = src == SOURCE_SNAPSHOT
    # source 가 다르면 증감 비교의 의미가 약해진다 — 화면이 그 사실을 밝힌다.
    same_source = src == psrc

    def n(d: dict[str, Any], key: str) -> int:
        return int(d.get(key) or 0)

    contribution = n(f, "gross_sales") - n(f, "total_direct_cost")
    prev_contribution = n(pf, "gross_sales") - n(pf, "total_direct_cost")

    kpis: list[dict[str, Any]] = [
        _kpi("gross_sales", "총매출", n(f, "gross_sales"), "money", src,
             previous=n(pf, "gross_sales"), previous_source=psrc, comparable=same_source),
        _kpi(
            "settlement_amount",
            "확정 정산액" if confirmed else "정산액",
            n(f, "confirmed_settlement_amount") if period["settlement_connected"] else None,
            "money", src,
            availability=AVAILABLE if period["settlement_connected"] else NOT_CONNECTED,
            hint=(
                "판매처 정산서 기준 실입금액"
                if period["settlement_connected"]
                else "이 달에 연결된 판매처 정산서가 없습니다. 0원이 아니라 아직 모르는 값입니다"
            ),
            previous=(
                n(pf, "confirmed_settlement_amount")
                if prev["settlement_connected"] else None
            ),
            previous_source=psrc,
            comparable=same_source and period["settlement_connected"] and prev["settlement_connected"],
        ),
        _kpi("contribution_profit", "공헌이익", contribution, "money", src,
             hint="총매출 − 상품 직접비. 사업 공통비 차감 전",
             previous=prev_contribution, previous_source=psrc, comparable=same_source),
        _kpi(
            "net_profit",
            period["profit_label"],
            n(f, "net_profit"), "money", src,
            hint="총매출 − 상품 직접비 − 사업비용",
            previous=n(pf, "net_profit"), previous_source=psrc, comparable=same_source,
        ),
        _kpi("margin_rate", "평균 마진율",
             _rate(n(f, "net_profit"), n(f, "gross_sales")), "percent", src,
             availability=AVAILABLE if n(f, "gross_sales") else NOT_CONNECTED,
             hint="순이익 ÷ 총매출. 매출이 없는 달은 계산할 수 없습니다",
             previous=_rate(n(pf, "net_profit"), n(pf, "gross_sales")),
             previous_source=psrc, comparable=same_source),
        _kpi("business_expenses", "사업비용", n(f, "business_expenses"), "money", src,
             higher_is_better=False,
             previous=n(pf, "business_expenses"), previous_source=psrc,
             comparable=same_source),
        # 상품 수는 기간 집계가 아니라 현재 상태다 — 확정 snapshot 에 담기지 않는다.
        _kpi("active_products", "활성 판매 상품 수", _active_product_count(db, org_id=org_id),
             "count", SOURCE_LIVE, hint="현재 판매 중(ACTIVE) 상품 수. 기간 집계가 아닙니다",
             comparable=False),
    ]

    # 정산 상태는 금액이 아니라 상태다. 미연결과 불일치를 한 칸에 섞지 않는다.
    if not period["settlement_connected"]:
        kpis.append(_kpi(
            "settlement_status", "정산 연결 상태", None, "status", src,
            availability=NOT_CONNECTED, higher_is_better=False,
            hint="판매처 정산서가 연결되지 않아 이 달의 손익은 판매·비용 기준입니다",
            comparable=False,
        ))
    else:
        kpis.append(_kpi(
            "settlement_status", "미매칭 정산 건수",
            n(f, "settlement_unmatched_count"), "count", src,
            higher_is_better=False,
            hint=f"정산서 {n(f, 'settlement_record_count')}건 중 주문에 매칭되지 않은 건",
            previous=n(pf, "settlement_unmatched_count") if prev["settlement_connected"] else None,
            previous_source=psrc,
            comparable=same_source and prev["settlement_connected"],
        ))

    return kpis


# ──────────────────────────────────────────────────────────────────────────
# 워터폴 / 트렌드
# ──────────────────────────────────────────────────────────────────────────

# (컬럼, 라벨). 여기 없는 항목은 대시보드에 나오지 않는다 —
# 예전 mock 은 "예상 세금" 을 매출의 3% 로 지어냈다. 그런 컬럼은 없다.
_WATERFALL_COSTS = (
    ("cogs", "상품 원가"),
    ("marketplace_fees", "판매수수료"),
    ("payment_fees", "결제수수료"),
    ("shipping_cost", "배송비"),
    ("advertising_cost", "광고비"),
    ("fulfillment_fees", "풀필먼트비"),
    ("returns_cost", "반품비용"),
    ("business_expenses", "사업비용"),
)


def build_waterfall(period: dict[str, Any]) -> list[dict[str, Any]]:
    """총매출에서 순이익까지. 전부 실제 집계 컬럼이다."""
    f = period["figures"]
    steps: list[dict[str, Any]] = [
        {"key": "gross_sales", "label": "총매출",
         "amount": int(f.get("gross_sales") or 0), "kind": "start"},
    ]
    for key, label in _WATERFALL_COSTS:
        steps.append({
            "key": key, "label": label,
            "amount": int(f.get(key) or 0), "kind": "deduction",
        })
    steps.append({
        "key": "net_profit", "label": period["profit_label"],
        "amount": int(f.get("net_profit") or 0), "kind": "total",
    })
    return steps


def build_trend(
    db: Session, *, org_id: UUID, end_year_month: str, months: int,
) -> list[dict[str, Any]]:
    """최근 N개월. **각 점이 자기 source 를 갖는다.**

    확정월은 snapshot, 진행월은 live 다. 하나의 선에 두 source 가 섞이지만
    그건 사실이다 — 화면이 확정 구간과 진행 구간을 구분해 그린다.
    """
    out: list[dict[str, Any]] = []
    for i in range(months - 1, -1, -1):
        ym = _shift_month(end_year_month, -i)
        m = month_figures(db, org_id=org_id, year_month=ym)
        f = m["figures"]
        gross = int(f.get("gross_sales") or 0)
        net = int(f.get("net_profit") or 0)
        out.append({
            "year_month": ym,
            "close_state": m["close_state"],
            "source": m["source"],
            "gross_sales": gross,
            "net_profit": net,
            "business_expenses": int(f.get("business_expenses") or 0),
            "margin_rate": _rate(net, gross),
            "profit_basis": f.get("profit_basis"),
        })
    return out


# ──────────────────────────────────────────────────────────────────────────
# 분해 (판매처 / 비용 / 상품)
# ──────────────────────────────────────────────────────────────────────────

def marketplace_breakdown(
    db: Session, *, org_id: UUID, start: date, end: date,
) -> list[dict[str, Any]]:
    """판매처별 매출·공헌이익.

    **순이익이 아니라 공헌이익이다.** 사업 공통비(임대료·급여 등)는 특정
    판매처에 귀속되지 않는다. 그걸 임의로 배분해 "쿠팡 순이익" 이라고 부르면
    배분 규칙이 곧 결론이 된다.
    """
    rows = db.execute(text("""
        SELECT so.marketplace,
               COUNT(*)                                  AS order_count,
               COALESCE(SUM(so.sale_price), 0)           AS gross_sales,
               COALESCE(SUM(
                 COALESCE(pma.actual_cost, 0) + COALESCE(pma.actual_shipping_cost, 0)
               + COALESCE(pma.actual_marketplace_fee, 0) + COALESCE(pma.actual_payment_fee, 0)
               + COALESCE(pma.actual_ad_cost, 0) + COALESCE(pma.actual_fulfillment_fee, 0)
               + COALESCE(pma.actual_return_cost, 0)
               ), 0)                                     AS direct_cost,
               COUNT(*) FILTER (WHERE pma.id IS NULL)    AS unpriced_order_count
          FROM sales_order so
          LEFT JOIN product_margin_actual pma
                 ON pma.sales_order_id = so.id AND pma.deleted_at IS NULL
         WHERE so.org_id = :org AND so.deleted_at IS NULL
           AND so.sale_date BETWEEN :s AND :e
         GROUP BY so.marketplace
         ORDER BY gross_sales DESC
    """), {"org": str(org_id), "s": start, "e": end}).mappings().all()

    out = []
    for r in rows:
        gross, direct = int(r["gross_sales"]), int(r["direct_cost"])
        out.append({
            "marketplace": r["marketplace"],
            "order_count": int(r["order_count"]),
            "gross_sales": gross,
            "direct_cost": direct,
            "contribution_profit": gross - direct,
            "contribution_rate": _rate(gross - direct, gross),
            # 마진 계산이 아직 안 된 주문 수. 공헌이익이 과대평가된 정도를 알려준다.
            "unpriced_order_count": int(r["unpriced_order_count"]),
        })
    return out


def expense_breakdown(
    db: Session, *, org_id: UUID, start: date, end: date,
) -> list[dict[str, Any]]:
    """비용 카테고리 구성. `compute_figures` 의 사업비용과 같은 필터를 쓴다."""
    rows = db.execute(text("""
        SELECT COALESCE(c.name, '미분류')       AS category_name,
               COALESCE(SUM(e.gross_amount), 0) AS amount,
               COUNT(*)                         AS entry_count
          FROM expenses e
          LEFT JOIN expense_category_policy c ON c.id = e.category_id
         WHERE e.org_id = :org AND e.deleted_at IS NULL
           AND e.expense_date BETWEEN :s AND :e
           AND e.is_settlement_deductible = TRUE AND e.is_business_use = TRUE
         GROUP BY COALESCE(c.name, '미분류')
         ORDER BY amount DESC
    """), {"org": str(org_id), "s": start, "e": end}).mappings().all()

    total = sum(int(r["amount"]) for r in rows)
    return [{
        "category_name": r["category_name"],
        "amount": int(r["amount"]),
        "entry_count": int(r["entry_count"]),
        "ratio": _rate(int(r["amount"]), total),
    } for r in rows]


def margin_distribution(
    db: Session, *, org_id: UUID, start: date, end: date, limit: int = 200,
) -> list[dict[str, Any]]:
    """상품별 매출 × 공헌이익률 분포.

    `compute_figures` 와 **같은 테이블**(`product_margin_actual`)에서 만든다.
    다른 근사식으로 계산하면 대시보드 총합과 점들의 합이 어긋난다.
    """
    rows = db.execute(text("""
        SELECT COALESCE(p.id::text, 'UNLINKED:' || COALESCE(so.product_snapshot_name, '?'))
                 AS product_key,
               COALESCE(MAX(p.name_ko), MAX(so.product_snapshot_name), '상품 미연결')
                 AS product_name,
               MAX(p.id::text)                            AS product_id,
               COALESCE(SUM(so.sale_price), 0)            AS gross_sales,
               COALESCE(SUM(
                 COALESCE(pma.actual_cost, 0) + COALESCE(pma.actual_shipping_cost, 0)
               + COALESCE(pma.actual_marketplace_fee, 0) + COALESCE(pma.actual_payment_fee, 0)
               + COALESCE(pma.actual_ad_cost, 0) + COALESCE(pma.actual_fulfillment_fee, 0)
               + COALESCE(pma.actual_return_cost, 0)
               ), 0)                                      AS direct_cost,
               COUNT(*) FILTER (WHERE pma.id IS NULL)     AS unpriced_order_count
          FROM sales_order so
          LEFT JOIN product p ON p.id = so.product_id AND p.deleted_at IS NULL
          LEFT JOIN product_margin_actual pma
                 ON pma.sales_order_id = so.id AND pma.deleted_at IS NULL
         WHERE so.org_id = :org AND so.deleted_at IS NULL
           AND so.sale_date BETWEEN :s AND :e
         GROUP BY product_key
         ORDER BY gross_sales DESC
         LIMIT :lim
    """), {"org": str(org_id), "s": start, "e": end, "lim": limit}).mappings().all()

    out = []
    for r in rows:
        gross, direct = int(r["gross_sales"]), int(r["direct_cost"])
        out.append({
            "product_id": r["product_id"],
            "product_name": r["product_name"],
            "gross_sales": gross,
            "contribution_profit": gross - direct,
            "contribution_rate": _rate(gross - direct, gross),
            "unpriced_order_count": int(r["unpriced_order_count"]),
        })
    return out


# ──────────────────────────────────────────────────────────────────────────
# 경고 — 재계산하지 않고 운영 이슈를 그대로 쓴다
# ──────────────────────────────────────────────────────────────────────────

def build_warnings(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """대시보드 경고.

    **여기서 조건을 다시 계산하지 않는다.** `operation_issue` 가 이미 스캔한
    결과를 요약해서 보여주고, 클릭하면 그 목록으로 간다. 대시보드 전용 규칙을
    따로 두면 이슈 센터에서 처리·무시한 건이 대시보드에는 계속 떠 있게 된다.
    """
    s = operation_issue.summary(db, org_id=org_id)
    by_type = [{
        "issue_type": r["issue_type"],
        "severity": r["severity"],
        "count": int(r["n"]),
    } for r in s.get("by_type", [])]

    return {
        "open_total": int(s.get("open_total") or 0),
        "blocking": int(s.get("blocking") or 0),
        "warning": int(s.get("warning") or 0),
        "info": int(s.get("info") or 0),
        "overdue": int(s.get("overdue") or 0),
        "unassigned": int(s.get("unassigned") or 0),
        "by_type": by_type,
        # 스캔이 한 번도 안 돌았으면 "이슈 0건" 은 "문제 없음" 이 아니라
        # "확인한 적 없음" 이다. 화면이 그 둘을 구분할 수 있어야 한다.
        "last_scan": s.get("last_scan"),
        "never_scanned": s.get("last_scan") is None,
    }


# ──────────────────────────────────────────────────────────────────────────
# 확정 월 이력
# ──────────────────────────────────────────────────────────────────────────

def confirmed_snapshots(
    db: Session, *, org_id: UUID, limit: int = 12,
) -> list[dict[str, Any]]:
    """최근 확정된 달. 진행월 카드와 나란히 두되 섞지 않는다."""
    rows = db.execute(text("""
        SELECT s.year_month, s.generation, s.taken_at, s.taken_at_state,
               s.gross_sales, s.net_profit, s.distributable_profit,
               s.profit_basis, s.settlement_connected,
               ms.close_state
          FROM monthly_close_snapshot s
          JOIN monthly_settlement ms
            ON ms.org_id = s.org_id AND ms.year_month = s.year_month
           AND ms.deleted_at IS NULL
         WHERE s.org_id = :org AND s.superseded_at IS NULL
         ORDER BY s.year_month DESC
         LIMIT :lim
    """), {"org": str(org_id), "lim": limit}).mappings().all()

    return [{
        "year_month": r["year_month"],
        "close_state": r["close_state"],
        "generation": int(r["generation"]),
        "taken_at": r["taken_at"],
        "gross_sales": int(r["gross_sales"]),
        "net_profit": int(r["net_profit"]),
        "distributable_profit": int(r["distributable_profit"]),
        "profit_basis": r["profit_basis"],
        "settlement_connected": bool(r["settlement_connected"]),
        "source": SOURCE_SNAPSHOT,
        "profit_label": profit_label(r["close_state"], SOURCE_SNAPSHOT, dict(r)),
    } for r in rows]


# ──────────────────────────────────────────────────────────────────────────
# 조립
# ──────────────────────────────────────────────────────────────────────────

def build_dashboard(
    db: Session, *, org_id: UUID, year_month: str, trend_months: int = 6,
    viewer_role: str | None = None,
) -> dict[str, Any]:
    """대시보드 한 판. 프론트엔드가 여러 endpoint 를 조합하지 않게 한 번에 준다.

    `viewer_role` 이 현금흐름을 볼 수 없는 역할이면 `cashflow` 를 아예 빼고 준다.
    대시보드는 OPERATOR·READONLY 도 읽지만 현금흐름은 그렇지 않다 — 카드를 그냥
    넣으면 `/cashflow` 가 403 인 사람에게 같은 숫자가 나가는 뒷문이 된다.
    `None` 은 "역할을 모른다" 이므로 **넣지 않는다** — 모르면 안 주는 쪽이 안전하다.
    """
    trend_months = max(1, min(trend_months, MAX_TREND_MONTHS))

    period = month_figures(db, org_id=org_id, year_month=year_month)
    prev_ym = _shift_month(year_month, -1)
    prev = month_figures(db, org_id=org_id, year_month=prev_ym)

    start, end = period["start"], period["end"]
    comparable = period["source"] == prev["source"]

    board: dict[str, Any] = {
        "period": {
            "year_month": year_month,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "close_state": period["close_state"],
            "source": period["source"],
            "profit_basis": period["profit_basis"],
            "profit_label": period["profit_label"],
            "settlement_connected": period["settlement_connected"],
            "snapshot_generation": period["snapshot_generation"],
            "snapshot_taken_at": period["taken_at"],
            "is_confirmed": period["source"] == SOURCE_SNAPSHOT,
        },
        "comparison": {
            "year_month": prev_ym,
            "close_state": prev["close_state"],
            "source": prev["source"],
            "comparable": comparable,
            "note": None if comparable else (
                "기준이 다릅니다 — "
                f"{year_month}는 {'확정 snapshot' if period['source'] == SOURCE_SNAPSHOT else '진행 중 집계'}, "
                f"{prev_ym}는 {'확정 snapshot' if prev['source'] == SOURCE_SNAPSHOT else '진행 중 집계'} 기준입니다"
            ),
        },
        "kpis": build_kpis(db, org_id=org_id, period=period, prev=prev),
        "waterfall": build_waterfall(period),
        "trend": build_trend(
            db, org_id=org_id, end_year_month=year_month, months=trend_months
        ),
        "by_marketplace": marketplace_breakdown(db, org_id=org_id, start=start, end=end),
        "expense_breakdown": expense_breakdown(db, org_id=org_id, start=start, end=end),
        "margin_distribution": margin_distribution(db, org_id=org_id, start=start, end=end),
        "warnings": build_warnings(db, org_id=org_id),
        "confirmed_snapshots": confirmed_snapshots(db, org_id=org_id),
    }
    if viewer_role in cashflow.VIEW_ROLES:
        board["cashflow"] = cashflow_card(db, org_id=org_id)
    return board


def cashflow_card(db: Session, *, org_id: UUID, days: int = 30) -> dict[str, Any]:
    """대시보드의 현금흐름 카드.

    **선택한 달을 따라가지 않는다.** 대시보드의 나머지는 "그 달에 얼마를 벌었나"
    (손익)를 묻지만, 현금흐름은 "앞으로 통장이 버티나" 를 묻는다. 과거 달을 보는
    중에도 이 카드는 오늘부터 앞을 본다 — 그래서 `basis` 로 기준을 명시한다.

    합치면 docs/54 §1 이 경고하는 바로 그 혼동이 생긴다: 매출 3천만이 현금
    3천만으로 읽히는 것.
    """
    summary = cashflow.build_summary(db, org_id=org_id, preset=f"{days}d")
    return {
        # 화면이 "8월 현금흐름" 이라고 쓰지 못하게 기준을 함께 준다.
        "basis": f"오늘부터 {days}일",
        "range": summary["range"],
        "balance_available": summary["balance_available"],
        "baseline": summary["baseline"],
        "projected_ending_balance": summary["projected_ending_balance"],
        "lowest_projected_balance": summary["lowest_projected_balance"],
        "inflow": summary["inflow"],
        "outflow": summary["outflow"],
        "unpaid_payout": summary["unpaid_payout"],
        "overdue_payout": summary["overdue_payout"],
        # 실제 입금을 확인할 수단이 없다는 사실을 화면이 말할 수 있어야 한다 —
        # 설명 없는 "실제 유입 0원" 은 장부가 틀린 것처럼 보인다.
        "actual_inflow_source_available": summary["actual_inflow_source_available"],
    }


def available_months(db: Session, *, org_id: UUID, limit: int = 36) -> list[str]:
    """대시보드 월 선택기에 넣을 달.

    판매·비용·정산 행이 실제로 있는 달 + 이번 달. 데이터가 없는 달을
    목록에 넣으면 사용자는 "왜 전부 0이지" 를 먼저 묻게 된다.
    """
    rows = db.execute(text("""
        SELECT DISTINCT ym FROM (
          SELECT to_char(sale_date, 'YYYY-MM') AS ym FROM sales_order
           WHERE org_id = :org AND deleted_at IS NULL
          UNION
          SELECT to_char(expense_date, 'YYYY-MM') FROM expenses
           WHERE org_id = :org AND deleted_at IS NULL
          UNION
          SELECT year_month FROM monthly_settlement
           WHERE org_id = :org AND deleted_at IS NULL
        ) t
        WHERE ym IS NOT NULL
        ORDER BY ym DESC
        LIMIT :lim
    """), {"org": str(org_id), "lim": limit}).scalars().all()

    months = sorted({*rows, current_year_month()}, reverse=True)
    return months[:limit]


def month_end_day(year_month: str) -> int:
    y, m = int(year_month[:4]), int(year_month[5:7])
    return calendar.monthrange(y, m)[1]
