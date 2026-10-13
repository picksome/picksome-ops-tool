"""기간 손익 재계산 표시 (M026).

왜 표시만 남기나
--------------
비용이 바뀌면 그 달의 잠정 손익이 낡는다. 그렇다고 승인 트랜잭션 안에서 전체를
다시 집계하면 안 된다.

  · 느리다. 판매·비용이 수천 건인 달을 다시 도는 동안 승인 트랜잭션이 잠금을 쥔다
  · 실패하면 **승인이 통째로 롤백된다.** 집계 버그 때문에 정당한 승인이 막힌다
  · 커밋 후에 돌리면 커밋과 재계산 사이에 프로세스가 죽었을 때 표시가 유실된다

그래서 **같은 트랜잭션에서 표시만** 남긴다. 비용 변경과 표시가 원자적이라
"비용은 바뀌었는데 낡았다는 표시가 없다" 가 생기지 않는다.

과거 확정 데이터는 어떻게 되나
---------------------------
**건드리지 않는다.** 잠긴 달은 애초에 비용이 바뀌지 않고(월 잠금 가드),
조정은 지금 열린 달에 생긴다. 그래서 이 표시는 **열린 달에만** 붙는다.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.month_lock import get_close_state, year_month_of

_log = logging.getLogger(__name__)


def mark_period_dirty(
    db: Session, *, org_id: UUID, at: date | str, reason: str, actor: UUID | None = None
) -> str | None:
    """해당 월을 "재계산 필요" 로 표시한다. **커밋하지 않는다.**

    호출자가 이미 트랜잭션을 열고 있다(승인 적용). 여기서 커밋하면
    승인 상태 전이와 원자성이 깨진다.

    잠긴 달은 표시하지 않는다 — 재계산할 대상이 아니다. 확정된 숫자를
    다시 계산하겠다고 표시해 두면 언젠가 배치가 그걸 실행한다.
    """
    ym = year_month_of(at)
    state = get_close_state(db, org_id=org_id, year_month=ym)
    if state in ("CONFIRMED", "LOCKED"):
        return None

    db.execute(text("""
        INSERT INTO period_recalc_flag (org_id, year_month, reason, marked_by)
        VALUES (:o, :ym, :reason, :actor)
        ON CONFLICT (org_id, year_month) DO UPDATE
           SET reason = EXCLUDED.reason, marked_at = now(),
               marked_by = EXCLUDED.marked_by, resolved_at = NULL
    """), {"o": str(org_id), "ym": ym, "reason": reason[:200],
           "actor": str(actor) if actor else None})
    return ym


def list_dirty_periods(db: Session, *, org_id: UUID) -> list[dict]:
    rows = db.execute(text("""
        SELECT year_month, reason, marked_at, marked_by
          FROM period_recalc_flag
         WHERE org_id = :o AND resolved_at IS NULL
         ORDER BY year_month DESC
    """), {"o": str(org_id)}).mappings().all()
    return [dict(r) for r in rows]


def is_dirty(db: Session, *, org_id: UUID, year_month: str) -> bool:
    return bool(db.execute(text("""
        SELECT 1 FROM period_recalc_flag
         WHERE org_id = :o AND year_month = :ym AND resolved_at IS NULL
    """), {"o": str(org_id), "ym": year_month}).scalar())


def resolve_period(db: Session, *, org_id: UUID, year_month: str) -> None:
    """재계산이 끝났다고 표시한다. 집계 서비스가 부른다."""
    db.execute(text("""
        UPDATE period_recalc_flag SET resolved_at = now()
         WHERE org_id = :o AND year_month = :ym AND resolved_at IS NULL
    """), {"o": str(org_id), "ym": year_month})


def process_dirty_periods(
    db: Session, *, org_id: UUID | None = None, limit: int = 20,
):
    """더러워진 기간을 실제로 다시 집계한다 (Phase 37).

    M026 은 **표시만** 남기고 처리기를 두지 않았다. 그래서 표시가 계속 쌓였고,
    마감 검증의 `PERIOD_RECALC_PENDING` 이 영원히 차단으로 남을 수 있었다.

    무엇을 다시 계산하는가
    -------------------
    `monthly_settlement` 의 집계 컬럼(매출·직접비·공통비·순이익·배분 기준),
    그리고 **정산이 붙은 주문의 실 마진**이다(docs/83).

    원래는 월 집계만 했다. 비용이 바뀌었다고 판매 마진이 달라지지는 않기
    때문이다. 정산서는 다르다 — 매출과 마켓 수수료가 실측으로 바뀌므로
    주문별 마진 자체가 달라진다. 자세한 이유는 `_recalculate_month` 참조.

    잠긴·확정된 달은 건너뛴다
    ----------------------
    확정한 숫자를 배치가 조용히 바꾸면 확정이 의미를 잃는다.
    `mark_period_dirty` 가 애초에 그런 달을 표시하지 않지만, 표시된 뒤에
    확정됐을 수 있으므로 여기서 다시 확인한다.
    """
    from app.services.worker_runtime import WorkerOutcome

    where = ["resolved_at IS NULL"]
    p: dict = {"lim": limit}
    if org_id is not None:
        where.append("org_id = :o")
        p["o"] = str(org_id)

    rows = db.execute(text(f"""
        SELECT org_id, year_month FROM period_recalc_flag
         WHERE {" AND ".join(where)}
         ORDER BY marked_at
         LIMIT :lim
    """), p).mappings().all()

    recalculated = 0
    skipped_closed = 0
    failed = 0
    details: list[dict] = []

    for row in rows:
        oid = UUID(str(row["org_id"]))
        ym = row["year_month"]
        state = get_close_state(db, org_id=oid, year_month=ym)
        if state in ("CONFIRMED", "LOCKED"):
            # 확정된 달이다. 표시만 지운다 — 재계산 대상이 아니라는 사실은
            # 남겨 봐야 매번 다시 집히기만 한다.
            resolve_period(db, org_id=oid, year_month=ym)
            skipped_closed += 1
            details.append({"org_id": str(oid), "year_month": ym, "result": "SKIPPED_CLOSED"})
            continue

        try:
            _recalculate_month(db, org_id=oid, year_month=ym)
            resolve_period(db, org_id=oid, year_month=ym)
            db.commit()
            recalculated += 1
            details.append({"org_id": str(oid), "year_month": ym, "result": "RECALCULATED"})
        except Exception as exc:  # noqa: BLE001 — 한 기간 실패가 나머지를 막지 않는다
            db.rollback()
            failed += 1
            details.append({
                "org_id": str(oid), "year_month": ym, "result": "FAILED",
                "error": f"{type(exc).__name__}: {exc}"[:200],
            })

    return WorkerOutcome(
        processed=len(rows), success=recalculated, failure=failed,
        skipped=skipped_closed, summary={"details": details},
    )


def _recalculate_month(db: Session, *, org_id: UUID, year_month: str) -> None:
    """월 집계 컬럼 갱신. **커밋하지 않는다.**

    `close_snapshot.compute_figures` 를 재사용한다 — 확정 시 박제하는 값과
    같은 산식이어야 한다. 두 벌로 계산하면 확정 직전 화면과 확정 결과가 달라진다.

    정산이 붙은 주문은 **개별 마진도 다시 계산한다** (docs/83)
    ------------------------------------------------------
    비용이 바뀌었다고 판매 마진이 달라지지는 않는다 — 그래서 원래 이 함수는
    월 집계만 손댔다. 그런데 **정산서는 다르다.** 매출과 마켓 수수료가 실측
    값으로 바뀌므로(D1~D3) 주문별 마진 자체가 달라진다.

    월 집계만 다시 하면 `PERIOD_RECALC_PENDING` 은 꺼지는데 실 마진은 옛
    추정값 그대로 남는다 — 표시만 하고 반영은 안 되는 막다른 길이 된다.
    게다가 월 집계(`total_direct_cost`)는 `product_margin_actual` 을 읽으므로
    순서도 중요하다: **마진을 먼저 고치고 그 위에서 월을 집계한다.**

    정산이 안 붙은 주문은 건드리지 않는다 — 바뀐 입력이 없다.
    """
    from app.services import close_snapshot

    _refresh_settled_margins(db, org_id=org_id, year_month=year_month)

    f = close_snapshot.compute_figures(db, org_id=org_id, year_month=year_month)
    db.execute(text("""
        UPDATE monthly_settlement
           SET total_revenue = :rev,
               total_direct_cost = :direct,
               total_common_expense = :common,
               net_profit = :net,
               distributable_amount = :dist,
               updated_at = now()
         WHERE org_id = :o AND year_month = :ym AND deleted_at IS NULL
           AND close_state NOT IN ('CONFIRMED','LOCKED')
    """), {
        "o": str(org_id), "ym": year_month,
        "rev": f["gross_sales"], "direct": f["total_direct_cost"],
        "common": f["business_expenses"], "net": f["net_profit"],
        "dist": f["distributable_profit"],
    })


def _refresh_settled_margins(db: Session, *, org_id: UUID, year_month: str) -> int:
    """그 달의 **정산이 붙은 주문**만 실 마진을 다시 계산한다.

    승인된 D1~D3 이 실제로 숫자에 닿는 지점이다. 정산서가 들어와도 이걸 안
    돌리면 실 마진은 계속 정책 추정값이다.

    범위를 좁게 잡는 이유
    -----------------
    정산이 안 붙은 주문은 입력이 하나도 안 바뀌었다. 전부 다시 계산하면
    수백 건의 `updated_at` 이 흔들리고, 그 흔들림이 "뭔가 바뀌었다" 로 읽힌다.

    한 건이 실패해도 나머지를 막지 않는다 — 상품 연결이 없거나 원가가 이상한
    주문 하나 때문에 그 달 전체가 옛 숫자로 남으면 안 된다.
    """
    from app.domain.margin import ActualMarginInput
    from app.services.margin import calculate_actual_margin, save_actual_margin

    start, end = _month_bounds(year_month)
    order_ids = db.execute(text("""
        SELECT DISTINCT so.id
          FROM sales_order so
          JOIN marketplace_settlement_record m
            ON m.matched_sales_order_id = so.id
         WHERE so.org_id = :o AND so.deleted_at IS NULL
           AND so.sale_date BETWEEN :s AND :e
           AND m.deleted_at IS NULL
           AND m.matching_status IN ('MATCHED_EXACT', 'MATCHED_MANUAL')
    """), {"o": str(org_id), "s": start, "e": end}).scalars().all()

    done = 0
    for so_id in order_ids:
        inp = ActualMarginInput(org_id=org_id, sales_order_id=UUID(str(so_id)))
        try:
            save_actual_margin(inp, calculate_actual_margin(inp, db), db)
            done += 1
        except Exception:  # noqa: BLE001 — 한 건이 나머지를 막지 않는다
            _log.exception("실 마진 재계산 실패: sales_order=%s", so_id)
    return done


def _month_bounds(year_month: str) -> tuple[date, date]:
    y, m = int(year_month[:4]), int(year_month[5:7])
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    return start, end
