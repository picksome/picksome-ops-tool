"""월 마감 확정 숫자 snapshot (Phase 27).

왜 필요한가
----------
확정한 달의 숫자를 화면이 매번 raw 테이블에서 다시 집계하면, 확정 이후에
판매 한 건이 조용히 바뀌었을 때 "7월 확정 순이익" 이 어제와 오늘 다르게 나온다.
그건 확정이 아니다.

그래서 확정 시점에 숫자를 **박제**하고, 이후 모든 조회는 이 행을 읽는다.

무엇을 담는가
-----------
실제로 집계 가능한 값만 담았다. 예를 들어 `expected_settlement` 은 스키마에
컬럼 자체가 없어서(`docs/47` §1-1) 담지 않는다. 없는 숫자를 0 으로 채워 두면
화면이 "정산 예정 0원" 이라고 단정한다.

정산 미연결과 0원을 구분한다
-------------------------
`settlement_connected` 가 FALSE 면 정산 금액 컬럼은 "모른다" 는 뜻이다.
그래서 `profit_basis` 도 `SALES_AND_EXPENSE_ONLY` 로 남는다 — 이 달의 순이익을
"정산 대사 완료" 라고 부르지 않는다.

hash
----
주요 숫자 + 정책 버전의 정규화 표현을 SHA-256 한다. 목적은 **감사와 회귀 탐지**다.
확정 후 raw 데이터가 바뀌었는지 다시 집계해서 비교하면 알 수 있다.
서명 키가 없으므로 위조 방지 기능은 없다 — 보안 장치라고 부르지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.services.settlement import month_range

# 집계 산식이 바뀌면 올린다. 과거 snapshot 은 그대로 두고, 값 비교 시
# 버전이 다르면 "산식이 달라졌다" 를 먼저 말할 수 있게 한다.
CALCULATION_VERSION = "2026.08-1"

# hash 대상 필드. 여기 없는 필드(취한 시각, 사유 등)는 숫자가 아니므로 제외한다.
_HASHED_FIELDS = (
    "order_count", "gross_sales",
    "cogs", "shipping_cost", "marketplace_fees", "payment_fees",
    "advertising_cost", "fulfillment_fees", "returns_cost", "total_direct_cost",
    "business_expenses", "labor_expense", "personal_card_expense",
    "expense_vat", "adjustment_total",
    "settlement_record_count", "settlement_unmatched_count",
    "confirmed_settlement_amount",
    "net_profit", "carryover_offset", "distributable_profit",
    "provisional_profit_total", "settled_profit_total", "final_profit_total",
)


@dataclass(slots=True)
class SnapshotFigures:
    """집계 결과. DB 컬럼과 이름을 맞춰 둔다."""

    values: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.values[key]


def compute_figures(db: Session, *, org_id: UUID, year_month: str) -> dict[str, Any]:
    """확정 대상 숫자를 한 번에 집계한다.

    **읽기 전용이다.** 어떤 행도 바꾸지 않는다 — 확정 전에 미리보기로 부를 수 있어야
    하고, 미리보기가 데이터를 건드리면 안 된다.
    """
    start, end = month_range(year_month)
    p = {"org": str(org_id), "s": start, "e": end, "ym": year_month}

    sales = db.execute(text("""
        SELECT COUNT(*) AS order_count,
               COALESCE(SUM(sale_price), 0) AS gross_sales
          FROM sales_order
         WHERE org_id = :org AND deleted_at IS NULL AND sale_date BETWEEN :s AND :e
    """), p).mappings().one()

    direct = db.execute(text("""
        SELECT COALESCE(SUM(pma.actual_cost), 0)             AS cogs,
               COALESCE(SUM(pma.actual_shipping_cost), 0)    AS shipping_cost,
               COALESCE(SUM(pma.actual_marketplace_fee), 0)  AS marketplace_fees,
               COALESCE(SUM(pma.actual_payment_fee), 0)      AS payment_fees,
               COALESCE(SUM(pma.actual_ad_cost), 0)          AS advertising_cost,
               COALESCE(SUM(pma.actual_fulfillment_fee), 0)  AS fulfillment_fees,
               COALESCE(SUM(pma.actual_return_cost), 0)      AS returns_cost,
               COALESCE(SUM(pma.provisional_profit_amount), 0) AS provisional_profit_total,
               COALESCE(SUM(pma.settled_profit_amount), 0)     AS settled_profit_total,
               COALESCE(SUM(pma.final_profit_amount), 0)       AS final_profit_total
          FROM product_margin_actual pma
          JOIN sales_order so ON so.id = pma.sales_order_id
         WHERE so.org_id = :org AND so.deleted_at IS NULL AND pma.deleted_at IS NULL
           AND so.sale_date BETWEEN :s AND :e
    """), p).mappings().one()

    # 급여는 비용이다 — 동업자 배분과 계산 단계가 다르므로 따로 센다.
    # 개인카드 비용은 상환 대상이므로 지급 장부가 이 값을 참조한다.
    expense = db.execute(text("""
        SELECT COALESCE(SUM(e.gross_amount), 0) AS business_expenses,
               COALESCE(SUM(e.vat_amount), 0)   AS expense_vat,
               COALESCE(SUM(e.gross_amount) FILTER (
                 WHERE c.kind = 'LABOR'
               ), 0) AS labor_expense,
               COALESCE(SUM(e.gross_amount) FILTER (
                 WHERE e.is_personal_card = TRUE
               ), 0) AS personal_card_expense
          FROM expenses e
          LEFT JOIN expense_category_policy c ON c.id = e.category_id
         WHERE e.org_id = :org AND e.deleted_at IS NULL
           AND e.expense_date BETWEEN :s AND :e
           AND e.is_settlement_deductible = TRUE AND e.is_business_use = TRUE
    """), p).mappings().one()

    adjustment_total = db.execute(text("""
        SELECT COALESCE(SUM(amount_delta), 0)
          FROM expense_adjustment
         WHERE org_id = :org AND deleted_at IS NULL AND adjustment_period = :ym
    """), p).scalar() or 0

    settlement = db.execute(text("""
        SELECT COUNT(*) AS settlement_record_count,
               COUNT(*) FILTER (WHERE matched_sales_order_id IS NULL)
                 AS settlement_unmatched_count,
               COALESCE(SUM(net_settlement), 0) AS confirmed_settlement_amount
          FROM marketplace_settlement_record
         WHERE org_id = :org AND deleted_at IS NULL
           AND settlement_period_start BETWEEN :s AND :e
    """), p).mappings().one()

    carryover_offset = db.execute(text("""
        SELECT COALESCE(carryover_offset, 0) FROM monthly_settlement
         WHERE org_id = :org AND year_month = :ym AND deleted_at IS NULL
         ORDER BY created_at DESC LIMIT 1
    """), p).scalar() or 0

    total_direct_cost = sum(int(direct[k]) for k in (
        "cogs", "shipping_cost", "marketplace_fees", "payment_fees",
        "advertising_cost", "fulfillment_fees", "returns_cost",
    ))
    gross_sales = int(sales["gross_sales"])
    business_expenses = int(expense["business_expenses"])
    net_profit = gross_sales - total_direct_cost - business_expenses
    distributable = max(net_profit - int(carryover_offset), 0) if net_profit > 0 else 0

    connected = int(settlement["settlement_record_count"]) > 0

    figures: dict[str, Any] = {
        "order_count": int(sales["order_count"]),
        "gross_sales": gross_sales,
        "cogs": int(direct["cogs"]),
        "shipping_cost": int(direct["shipping_cost"]),
        "marketplace_fees": int(direct["marketplace_fees"]),
        "payment_fees": int(direct["payment_fees"]),
        "advertising_cost": int(direct["advertising_cost"]),
        "fulfillment_fees": int(direct["fulfillment_fees"]),
        "returns_cost": int(direct["returns_cost"]),
        "total_direct_cost": total_direct_cost,
        "business_expenses": business_expenses,
        "labor_expense": int(expense["labor_expense"]),
        "personal_card_expense": int(expense["personal_card_expense"]),
        "expense_vat": int(expense["expense_vat"]),
        "adjustment_total": int(adjustment_total),
        "settlement_connected": connected,
        "settlement_record_count": int(settlement["settlement_record_count"]),
        "settlement_unmatched_count": int(settlement["settlement_unmatched_count"]),
        "confirmed_settlement_amount": int(settlement["confirmed_settlement_amount"]),
        "net_profit": net_profit,
        "carryover_offset": int(carryover_offset),
        "distributable_profit": distributable,
        "provisional_profit_total": int(direct["provisional_profit_total"]),
        "settled_profit_total": int(direct["settled_profit_total"]),
        "final_profit_total": int(direct["final_profit_total"]),
        # 정산이 하나도 연결되지 않았으면 "대사 완료" 라고 말할 수 없다.
        # 일부만 매칭된 경우도 마찬가지다 — 전부 매칭돼야 대사 기준이다.
        "profit_basis": (
            "SETTLEMENT_RECONCILED"
            if connected and int(settlement["settlement_unmatched_count"]) == 0
            else "SALES_AND_EXPENSE_ONLY"
        ),
    }
    figures["applied_policy_versions"] = _policy_versions(db, org_id=org_id, year_month=year_month)
    figures["calculation_version"] = CALCULATION_VERSION
    figures["content_hash"] = content_hash(figures)
    return figures


def _policy_versions(db: Session, *, org_id: UUID, year_month: str) -> dict[str, Any]:
    """이 달 계산에 유효했던 정책 버전. 나중에 정책이 바뀌어도 이 값은 그대로다."""
    _, end = month_range(year_month)
    out: dict[str, Any] = {}
    for table in (
        "marketplace_fee_policy", "payment_fee_policy", "fulfillment_fee_policy",
        "return_risk_policy", "tax_policy", "settlement_policy", "target_margin_policy",
    ):
        rows = db.execute(text(f"""
            SELECT version FROM {table}
             WHERE org_id = :org AND deleted_at IS NULL AND is_active = TRUE
               AND effective_from <= :asof
               AND (effective_to IS NULL OR effective_to >= :asof)
        """), {"org": str(org_id), "asof": end}).scalars().all()
        if rows:
            out[table] = sorted({int(v) for v in rows if v is not None})
    return out


def content_hash(figures: dict[str, Any]) -> str:
    """숫자 + 정책 버전의 정규화 표현 → SHA-256.

    키 순서에 흔들리지 않도록 정렬한다. 부동소수는 쓰지 않는다(전부 int).
    """
    payload = {k: figures[k] for k in _HASHED_FIELDS if k in figures}
    payload["profit_basis"] = figures.get("profit_basis")
    payload["settlement_connected"] = figures.get("settlement_connected")
    payload["applied_policy_versions"] = figures.get("applied_policy_versions")
    payload["calculation_version"] = figures.get("calculation_version")
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ──────────────────────────────────────────────────────────────────────────
# 생성 / 조회 / 세대 교체
# ──────────────────────────────────────────────────────────────────────────

def create_snapshot(
    db: Session, *, org_id: UUID, year_month: str, monthly_settlement_id: UUID,
    taken_at_state: str, actor_user_id: UUID | None, reason: str | None,
    check_run_id: UUID | None, warning_count: int,
    acknowledged_warnings: list[str] | None,
) -> dict[str, Any]:
    """snapshot 1행 INSERT. **커밋하지 않는다** — 확정 트랜잭션의 일부다.

    같은 달에 살아 있는 snapshot 이 이미 있으면 거부한다. 세대를 올리려면
    먼저 `supersede` 를 호출해야 한다(= 재오픈을 거쳐야 한다).
    """
    existing = db.execute(text("""
        SELECT id, generation FROM monthly_close_snapshot
         WHERE org_id = :org AND year_month = :ym AND superseded_at IS NULL
    """), {"org": str(org_id), "ym": year_month}).mappings().first()
    if existing:
        raise ConflictError(
            f"{year_month} 의 확정 snapshot 이 이미 있습니다(세대 {existing['generation']}). "
            "숫자를 다시 확정하려면 월을 재오픈해야 합니다.",
            detail={"snapshot_id": str(existing["id"]), "generation": existing["generation"]},
        )

    max_gen = db.execute(text("""
        SELECT COALESCE(MAX(generation), 0) FROM monthly_close_snapshot
         WHERE org_id = :org AND year_month = :ym
    """), {"org": str(org_id), "ym": year_month}).scalar() or 0

    f = compute_figures(db, org_id=org_id, year_month=year_month)

    row = db.execute(text("""
        INSERT INTO monthly_close_snapshot (
          org_id, year_month, monthly_settlement_id, generation, taken_at_state,
          taken_by, reason,
          order_count, gross_sales,
          cogs, shipping_cost, marketplace_fees, payment_fees,
          advertising_cost, fulfillment_fees, returns_cost, total_direct_cost,
          business_expenses, labor_expense, personal_card_expense,
          expense_vat, adjustment_total,
          settlement_connected, settlement_record_count, settlement_unmatched_count,
          confirmed_settlement_amount,
          net_profit, carryover_offset, distributable_profit,
          provisional_profit_total, settled_profit_total, final_profit_total,
          profit_basis,
          check_run_id, blocking_count, warning_count, acknowledged_warnings_json,
          source_cutoff_at, applied_policy_versions_json,
          calculation_version, content_hash, created_by
        ) VALUES (
          :org, :ym, :msid, :gen, :state,
          :actor, :reason,
          :order_count, :gross_sales,
          :cogs, :shipping_cost, :marketplace_fees, :payment_fees,
          :advertising_cost, :fulfillment_fees, :returns_cost, :total_direct_cost,
          :business_expenses, :labor_expense, :personal_card_expense,
          :expense_vat, :adjustment_total,
          :settlement_connected, :settlement_record_count, :settlement_unmatched_count,
          :confirmed_settlement_amount,
          :net_profit, :carryover_offset, :distributable_profit,
          :provisional_profit_total, :settled_profit_total, :final_profit_total,
          :profit_basis,
          :run, 0, :warning_count, CAST(:ack AS JSONB),
          now(), CAST(:pv AS JSONB),
          :calc_version, :hash, :actor
        )
        RETURNING id, generation
    """), {
        "org": str(org_id), "ym": year_month, "msid": str(monthly_settlement_id),
        "gen": int(max_gen) + 1, "state": taken_at_state,
        "actor": str(actor_user_id) if actor_user_id else None, "reason": reason,
        "run": str(check_run_id) if check_run_id else None,
        "warning_count": warning_count,
        "ack": json.dumps(sorted(acknowledged_warnings or []), ensure_ascii=False),
        "pv": json.dumps(f["applied_policy_versions"], ensure_ascii=False),
        "calc_version": f["calculation_version"], "hash": f["content_hash"],
        **{k: f[k] for k in (
            "order_count", "gross_sales", "cogs", "shipping_cost", "marketplace_fees",
            "payment_fees", "advertising_cost", "fulfillment_fees", "returns_cost",
            "total_direct_cost", "business_expenses", "labor_expense",
            "personal_card_expense", "expense_vat", "adjustment_total",
            "settlement_connected", "settlement_record_count",
            "settlement_unmatched_count", "confirmed_settlement_amount",
            "net_profit", "carryover_offset", "distributable_profit",
            "provisional_profit_total", "settled_profit_total", "final_profit_total",
            "profit_basis",
        )},
    }).mappings().one()

    db.execute(text("""
        UPDATE monthly_settlement
           SET close_snapshot_id = :sid, close_generation = :gen
         WHERE id = :msid
    """), {"sid": str(row["id"]), "gen": row["generation"], "msid": str(monthly_settlement_id)})

    return {**f, "id": row["id"], "generation": row["generation"]}


def get_current(db: Session, *, org_id: UUID, year_month: str) -> dict[str, Any] | None:
    """살아 있는(대체되지 않은) snapshot. 확정 숫자를 묻는 모든 화면이 여기를 본다."""
    row = db.execute(text("""
        SELECT * FROM monthly_close_snapshot
         WHERE org_id = :org AND year_month = :ym AND superseded_at IS NULL
    """), {"org": str(org_id), "ym": year_month}).mappings().first()
    return dict(row) if row else None


def list_generations(db: Session, *, org_id: UUID, year_month: str) -> list[dict[str, Any]]:
    """세대 이력. 재오픈으로 숫자가 어떻게 달라졌는지 비교할 수 있어야 한다."""
    rows = db.execute(text("""
        SELECT id, generation, taken_at_state, taken_at, taken_by, reason,
               gross_sales, total_direct_cost, business_expenses, net_profit,
               distributable_profit, profit_basis, content_hash,
               superseded_at, superseded_by_id
          FROM monthly_close_snapshot
         WHERE org_id = :org AND year_month = :ym
         ORDER BY generation DESC
    """), {"org": str(org_id), "ym": year_month}).mappings().all()
    return [dict(r) for r in rows]


def supersede(
    db: Session, *, org_id: UUID, year_month: str, superseded_by_id: UUID | None = None,
) -> UUID | None:
    """현재 snapshot 을 historical 로 내린다. **삭제하지 않는다.**

    재오픈 시점에는 아직 다음 snapshot 이 없으므로 `superseded_by_id` 가 없다.
    DB 제약이 (superseded_at IS NULL) = (superseded_by_id IS NULL) 을 요구하므로
    **자기 자신을 가리키게 한다** — "대체됐지만 후속은 아직 없다" 를 표현하는 방법이다.
    재확정 시 `link_supersede` 가 실제 후속으로 고쳐 쓴다.
    """
    row = db.execute(text("""
        SELECT id FROM monthly_close_snapshot
         WHERE org_id = :org AND year_month = :ym AND superseded_at IS NULL
         FOR UPDATE
    """), {"org": str(org_id), "ym": year_month}).first()
    if row is None:
        return None
    target = superseded_by_id or row.id
    db.execute(text("""
        UPDATE monthly_close_snapshot
           SET superseded_at = now(), superseded_by_id = :by
         WHERE id = :id
    """), {"id": str(row.id), "by": str(target)})
    return UUID(str(row.id))


def detect_drift(
    db: Session, *, org_id: UUID, year_month: str
) -> dict[str, Any] | None:
    """확정 후 raw 데이터가 조용히 바뀌었는지 확인한다.

    snapshot 이 없으면 None. 있으면 지금 다시 집계한 hash 와 비교한다.
    산식 버전이 다르면 hash 가 달라도 데이터 변경이라고 단정할 수 없다 —
    그 사실을 함께 돌려준다.
    """
    snap = get_current(db, org_id=org_id, year_month=year_month)
    if snap is None:
        return None
    now = compute_figures(db, org_id=org_id, year_month=year_month)
    changed = [
        k for k in _HASHED_FIELDS
        if k in snap and k in now and int(snap[k] or 0) != int(now[k] or 0)
    ]
    return {
        "drifted": now["content_hash"] != snap["content_hash"],
        "snapshot_hash": snap["content_hash"],
        "current_hash": now["content_hash"],
        "calculation_version_changed": (
            now["calculation_version"] != snap["calculation_version"]
        ),
        "changed_fields": changed,
        "snapshot_generation": snap["generation"],
    }


def require_settlement_row(db: Session, *, org_id: UUID, year_month: str) -> UUID:
    row = db.execute(text("""
        SELECT id FROM monthly_settlement
         WHERE org_id = :org AND year_month = :ym AND deleted_at IS NULL
         ORDER BY created_at DESC LIMIT 1
    """), {"org": str(org_id), "ym": year_month}).first()
    if row is None:
        raise NotFoundError(
            f"{year_month} 정산이 아직 생성되지 않았습니다. 월별 정산에서 먼저 계산하세요."
        )
    return UUID(str(row.id))
