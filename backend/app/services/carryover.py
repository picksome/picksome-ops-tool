"""적자 이월 원장 append.

두 개의 잠금 경로(`settlement_crud.lock_settlement`, `monthly_close.lock`)가
각자 원장을 쓰고 있었다. 한쪽만 쓰면 이월이 새고, 둘 다 쓰면 두 배가 된다.
그래서 append 를 여기 한 곳으로 모은다.

발견한 결함 (D4)
---------------
`settlement_crud.lock_settlement` 이 `event_kind` 에 `'ACCRUE'` / `'OFFSET'` 을 넣고
있었다. 컬럼 타입은 `enum_carryover_event` 이고 유효 라벨은
`LOSS_ADDED / OFFSET_APPLIED / MANUAL_ADJUST` 뿐이다.

    ERROR: invalid input value for enum enum_carryover_event: "ACCRUE"

즉 **적자월이나 이월 차감이 있는 달을 잠그면 무조건 500** 이었다.
흑자이면서 이월 잔액이 없는 달만 우연히 통과했다.

멱등성
------
같은 정산 행에 같은 종류의 이벤트를 두 번 쌓지 않는다. 잠금 → 재오픈 → 재잠금
흐름에서 이월이 중복 누적되면 다음 달 분배가 줄어든다.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

LOSS_ADDED = "LOSS_ADDED"
OFFSET_APPLIED = "OFFSET_APPLIED"


def current_balance(db: Session, *, org_id: UUID) -> int:
    row = db.execute(text("""
        SELECT COALESCE((
          SELECT balance_after FROM loss_carryover_ledger
           WHERE org_id = :o AND deleted_at IS NULL
           ORDER BY event_at DESC, created_at DESC, id DESC LIMIT 1
        ), 0) AS bal
    """), {"o": str(org_id)}).first()
    return int(row.bal) if row else 0


def apply_month_lock(
    db: Session, *, org_id: UUID, settlement_id: UUID, year_month: str,
    net_profit: int, carryover_offset: int, actor_user_id: UUID | None,
) -> dict[str, int | str | None]:
    """월 잠금 시 이월 원장 반영. **커밋하지 않는다.**

    적자월 → 손실 누적. 이월을 차감한 달 → 잔액 감소.
    둘 다 아니면 아무것도 하지 않는다.
    """
    kind: str | None = None
    amount = 0
    if net_profit < 0:
        kind, amount = LOSS_ADDED, abs(net_profit)
    elif carryover_offset > 0:
        kind, amount = OFFSET_APPLIED, carryover_offset

    if kind is None:
        return {"event_kind": None, "amount": 0, "balance_after": current_balance(db, org_id=org_id)}

    already = db.execute(text("""
        SELECT 1 FROM loss_carryover_ledger
         WHERE org_id = :o AND source_settlement_id = :sid
           AND event_kind = CAST(:k AS enum_carryover_event) AND deleted_at IS NULL
    """), {"o": str(org_id), "sid": str(settlement_id), "k": kind}).first()
    if already:
        # 재잠금이다. 이월을 다시 쌓으면 다음 달 분배가 줄어든다.
        return {
            "event_kind": kind, "amount": 0,
            "balance_after": current_balance(db, org_id=org_id),
        }

    balance = current_balance(db, org_id=org_id)
    new_balance = balance + amount if kind == LOSS_ADDED else max(balance - amount, 0)

    db.execute(text("""
        INSERT INTO loss_carryover_ledger (
          org_id, event_at, event_kind, source_settlement_id,
          amount, balance_after, reason, created_by, updated_by
        ) VALUES (
          :o, (:ym || '-01')::date, CAST(:k AS enum_carryover_event), :sid,
          :amt, :bal, :reason, :u, :u
        )
    """), {
        "o": str(org_id), "ym": year_month, "k": kind, "sid": str(settlement_id),
        "amt": amount, "bal": new_balance,
        "reason": (f"{year_month} 적자 누적" if kind == LOSS_ADDED
                   else f"{year_month} 흑자에서 이월 차감"),
        "u": str(actor_user_id) if actor_user_id else None,
    })
    return {"event_kind": kind, "amount": amount, "balance_after": new_balance}
