"""월별 정산 CRUD 래퍼.

상태 전이:
    DRAFT ──confirm──> CONFIRMED ──lock──> LOCKED
                            ▲                 │
                            └──reopen─────────┘ (REOPENED → confirm 으로 복귀)

LOCKED 후:
- expense / sales_order 가 linked_settlement_id 로 묶이면 수정 금지
- loss_carryover_ledger 에 1행 append (net_profit 부호에 따라 손실 누적/차감)

REOPEN (D-285):
- services.settlement.reopen_settlement 재사용
- reopen_count 초과 시 REOPEN_LIMIT_EXCEEDED (409)

⚠️ 상태 전이는 **단일 진실의 출처가 `services/monthly_close.py`** 다 (Phase 26)
---------------------------------------------------------------------
발견한 결함 (D1)
  이 모듈의 `confirm_settlement` / `lock_settlement` 는 `status` 만 바꾸고
  `close_state` 를 건드리지 않았다. 그런데 월 잠금 가드(`core/month_lock.py`)는
  **`close_state` 를 읽는다.**

    → 이 경로로 잠근 달은 `status='LOCKED'` 인데 `close_state='OPEN'` 이라
      판매·비용이 그대로 수정 가능했다. **잠금이 전혀 걸리지 않았다.**

  게다가 마감 검증(blocking 항목)도 거치지 않아 원가 0원 판매가 남은 달을
  그대로 확정할 수 있었다.

  이제 두 함수는 `monthly_close` 로 위임한다. 상태 기계는 하나다.

발견한 결함 (D4)
  `lock_settlement` 이 `loss_carryover_ledger.event_kind` 에 `'ACCRUE'`/`'OFFSET'`
  을 넣었다. 컬럼은 `enum_carryover_event` 이고 유효 라벨은
  `LOSS_ADDED / OFFSET_APPLIED / MANUAL_ADJUST` 뿐이다 —
  **적자월을 잠그면 무조건 500.** 이월 로직은 `services/carryover.py` 로 옮겼다.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import ConflictError, NotFoundError
from app.core.ids import new_id
from app.domain.settlement import MonthlySettlementInput, MonthlySettlementResult
from app.services.settlement import calculate_monthly_settlement


def calculate_preview(
    db: Session, *, org_id: UUID, year_month: str
) -> MonthlySettlementResult:
    return calculate_monthly_settlement(
        MonthlySettlementInput(org_id=org_id, year_month=year_month), db
    )


def create_settlement(
    db: Session,
    *,
    org_id: UUID,
    actor: UUID,
    year_month: str,
    memo: str | None,
) -> UUID:
    exists = db.execute(
        text(
            """
            SELECT id FROM monthly_settlement
             WHERE org_id = :o AND year_month = :ym AND deleted_at IS NULL
            """
        ),
        {"o": str(org_id), "ym": year_month},
    ).first()
    if exists is not None:
        raise ConflictError(
            f"{year_month} 정산이 이미 존재합니다.",
            detail={"settlement_id": str(exists.id)},
        )

    result = calculate_monthly_settlement(
        MonthlySettlementInput(org_id=org_id, year_month=year_month), db
    )

    sid = new_id()
    business_key = f"SETTLE-{year_month}"

    priority_total = sum(d.amount for d in result.distributions if d.distribution_kind == "PRIORITY")
    ratio_total = sum(d.amount for d in result.distributions if d.distribution_kind == "RATIO")

    db.execute(
        text(
            """
            INSERT INTO monthly_settlement (
              id, org_id, business_key, year_month, status,
              total_revenue, total_direct_cost, total_common_expense,
              net_profit, carryover_offset, distributable_amount,
              priority_distribution_total, ratio_distribution_total,
              memo, created_by, updated_by
            ) VALUES (
              :id, :o, :bk, :ym, 'DRAFT',
              :rev, :dc, :ce,
              :np, :co, :da,
              :pt, :rt,
              :memo, :u, :u
            )
            """
        ),
        {
            "id": str(sid),
            "o": str(org_id),
            "bk": business_key,
            "ym": year_month,
            "rev": result.total_revenue,
            "dc": result.total_direct_cost,
            "ce": result.total_common_expense,
            "np": result.net_profit,
            "co": result.carryover_offset_applied,
            "da": result.distributable_amount,
            "pt": priority_total,
            "rt": ratio_total,
            "memo": memo,
            "u": str(actor),
        },
    )

    for d in result.distributions:
        db.execute(
            text(
                """
                INSERT INTO partner_settlement (
                  id, monthly_settlement_id, partner_id, distribution_kind,
                  amount, ratio_used, created_by, updated_by
                ) VALUES (
                  :id, :ms, :p, :k, :a, :r, :u, :u
                )
                """
            ),
            {
                "id": str(new_id()),
                "ms": str(sid),
                "p": str(d.partner_id),
                "k": d.distribution_kind,
                "a": d.amount,
                "r": str(d.ratio_used) if d.ratio_used is not None else None,
                "u": str(actor),
            },
        )

    # 이 행이 그 달의 손익 숫자를 만든다. 누가 언제 만들었는지 없으면
    # 나중에 금액이 왜 그런지 설명할 근거가 사라진다.
    record_audit(
        db, org_id=org_id, domain_table="monthly_settlement", domain_row_id=sid,
        action="CREATE", actor_user_id=actor,
        after={
            "year_month": year_month,
            "net_profit": result.net_profit,
            "distributable_amount": result.distributable_amount,
        },
    )

    return sid


def _year_month_of(db: Session, *, org_id: UUID, settlement_id: UUID) -> str:
    row = db.execute(
        text("""
            SELECT year_month FROM monthly_settlement
             WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        {"i": str(settlement_id), "o": str(org_id)},
    ).first()
    if row is None:
        raise NotFoundError("정산을 찾을 수 없습니다.")
    return row.year_month


def confirm_settlement(
    db: Session, *, org_id: UUID, actor: UUID, settlement_id: UUID, note: str | None,
    acknowledged_warnings: list[str] | None = None,
) -> None:
    """확정 — `monthly_close.confirm` 에 위임한다 (D1 수정).

    직접 UPDATE 하지 않는다. 마감 검증·snapshot·close_state 동기화가 전부
    거기 있고, 여기서 따로 쓰면 두 경로가 서로 다른 상태를 만든다.

    메모만 이 경로의 고유 기능이라 별도로 반영한다.
    """
    from app.services import monthly_close

    year_month = _year_month_of(db, org_id=org_id, settlement_id=settlement_id)
    monthly_close.confirm(
        db, org_id=org_id, year_month=year_month, actor_user_id=actor,
        acknowledged_warnings=acknowledged_warnings or [],
    )
    if note:
        db.execute(
            text("""
                UPDATE monthly_settlement SET memo = :note, updated_by = :u
                 WHERE id = :i
            """),
            {"i": str(settlement_id), "u": str(actor), "note": note},
        )


def lock_settlement(
    db: Session, *, org_id: UUID, actor: UUID, settlement_id: UUID
) -> None:
    """잠금 — `monthly_close.lock` 에 위임한다 (D1·D4 수정).

    이월 원장 append 는 `services/carryover.py` 가 하고, 잠금 트랜잭션 안에서
    실행된다. 예전 구현은 여기서 직접 INSERT 했고 enum 라벨이 틀려 있었다.
    """
    from app.services import monthly_close

    year_month = _year_month_of(db, org_id=org_id, settlement_id=settlement_id)
    monthly_close.lock(
        db, org_id=org_id, year_month=year_month, actor_user_id=actor,
    )


_SELECT_MS = """
  SELECT id, business_key, year_month, status,
         total_revenue, total_direct_cost, total_common_expense,
         net_profit, carryover_offset, distributable_amount,
         priority_distribution_total, ratio_distribution_total,
         reopen_count, reopen_limit_override,
         confirmed_at, confirmed_by, locked_at, locked_by,
         snapshot_id, memo, created_at
"""


def list_settlements(
    db: Session,
    *,
    org_id: UUID,
    year_month: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict], int]:
    where = ["org_id = :o", "deleted_at IS NULL"]
    params: dict = {"o": str(org_id)}
    if year_month:
        where.append("year_month = :ym")
        params["ym"] = year_month
    if status:
        where.append("status = :st")
        params["st"] = status
    where_sql = " AND ".join(where)

    count_row = db.execute(
        text(f"SELECT COUNT(*) AS c FROM monthly_settlement WHERE {where_sql}"),
        params,
    ).first()
    total = int(count_row.c) if count_row else 0

    rows = db.execute(
        text(
            f"{_SELECT_MS} FROM monthly_settlement WHERE {where_sql} "
            "ORDER BY year_month DESC, created_at DESC LIMIT :lim OFFSET :off"
        ),
        {**params, "lim": limit, "off": offset},
    ).all()
    return [dict(r._mapping) for r in rows], total


def get_settlement(db: Session, *, org_id: UUID, settlement_id: UUID) -> dict:
    row = db.execute(
        text(
            f"{_SELECT_MS} FROM monthly_settlement "
            "WHERE id = :i AND org_id = :o AND deleted_at IS NULL"
        ),
        {"i": str(settlement_id), "o": str(org_id)},
    ).first()
    if row is None:
        raise NotFoundError("정산을 찾을 수 없습니다.")
    return dict(row._mapping)


def list_distributions(db: Session, *, settlement_id: UUID) -> list[dict]:
    rows = db.execute(
        text(
            """
            SELECT partner_id, distribution_kind, amount, ratio_used, note
              FROM partner_settlement
             WHERE monthly_settlement_id = :i AND deleted_at IS NULL
             ORDER BY distribution_kind, partner_id
            """
        ),
        {"i": str(settlement_id)},
    ).all()
    return [dict(r._mapping) for r in rows]
