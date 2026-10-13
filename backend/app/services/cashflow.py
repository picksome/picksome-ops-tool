"""현금흐름 (Phase 6).

`docs/54_cashflow_semantics.md` 가 source of truth 다. 여기는 그 결론의 구현이다.

읽기 모델이다 — 원장을 복제하지 않는다
--------------------------------
`payout_payment` 는 이미 "이 날 이 금액을 보냈다" 는 사실을 갖고 있다.
그걸 cashflow row 로 또 만들면 지급을 취소했을 때 두 곳을 고쳐야 하고,
하나만 고쳐지는 순간 이중 계상이 된다.

그래서 현금흐름은 **조회할 때 계산한다.** 새 테이블은 기존 source 로 표현할 수
없는 것뿐이다(`cash_balance_baseline`, `manual_cash_event` — M034).

ACTUAL 의 뜻은 좁다
-----------------
"돈이 실제로 움직였다" 는 기록이 있을 때만 ACTUAL 이다.

```
ACTUAL      payout_payment                     사람이 지급을 기록했다(증빙 첨부 가능)
            manual_cash_event(status=ACTUAL)   사람이 입출금을 확인했다

CONFIRMED   marketplace_settlement_record      판매처가 금액을 확정했다.
                                               **실제 입금이라는 근거는 없다**
            manual_cash_event(CONFIRMED)

EXPECTED    payout_item 미지급 잔액             줘야 하는데 아직 안 줬다
            manual_cash_event(EXPECTED)
```

**아닌 것**: 매출액, 월 snapshot 의 순이익, 비용 발생액.
전부 손익이지 현금이 아니다.

정산을 ACTUAL 로 올리지 않는 이유
------------------------------
`settled_at` 이 실제 은행 입금일이라는 근거가 스키마에도 데이터에도 없다
(docs/54 §3-2). 이름만 보고 올리면 "2천만원 들어왔다" 가 화면에 뜨는데
통장에는 아무것도 없을 수 있다.

ZERO 와 UNKNOWN
--------------
기준 잔액이 없으면 잔액을 **계산하지 않는다**(`None`). 0 으로 두면
"통장에 0원" 으로 읽힌다. 유입·유출 합계는 0원이 맞을 수 있으므로 0 을 준다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db_errors import translate_integrity_error
from app.core.exceptions import ConflictError, NotFoundError, ValidationError

# 현금 상태. 화면은 이 코드를 자기 사전으로 번역한다.
EXPECTED = "EXPECTED"
CONFIRMED = "CONFIRMED"
ACTUAL = "ACTUAL"

INFLOW = "INFLOW"
OUTFLOW = "OUTFLOW"

# 읽기 모델이 **이미 직접 집계하는** source.
#
# 수동 기록이 이 중 하나를 가리키면 그 행은 합계에서 뺀다. 안 그러면 같은 돈이
# 두 번 나간 것으로 잡힌다 — 지급 기록 한 줄 + 그걸 가리키는 수동 기록 한 줄.
# M034 의 UNIQUE 는 "같은 원본을 두 번" 만 막지, 원본과 나란히 서는 것은 못 막는다.
#
# 이 목록에 없는 source_type(예: 나중에 붙을 은행 거래)은 읽기 모델이 따로 세지
# 않으므로 수동 기록이 유일한 근거다 — 그대로 집계한다.
AGGREGATED_SOURCE_TYPES = (
    "payout_payment",
    "payout_item",
    "marketplace_settlement_record",
)

# 기간 프리셋. 사용자 지정은 from/to 를 직접 받는다.
PRESET_DAYS = {"30d": 30, "60d": 60, "90d": 90}

# 현금흐름을 볼 수 있는 역할.
#
# **여기 한 곳에서 정한다.** 대시보드 카드에도 잔액·미지급액이 그대로 들어가므로
# 두 라우터가 다른 목록을 쓰면 한쪽이 뒷문이 된다 — 실제로 그랬다:
# `/cashflow` 는 403 인데 `/reports/dashboard` 로는 같은 숫자가 나갔다.
VIEW_ROLES = ("ADMIN", "ACCOUNTING")


@dataclass(slots=True)
class CashRow:
    """현금흐름 한 줄. **원본으로 되돌아갈 수 있어야 한다.**

    출처 없는 숫자를 보여주지 않는다 — 사용자가 "이 300만원이 뭐지" 를
    물었을 때 답할 수 없으면 그 화면은 신뢰를 잃는다.
    """

    occurred_on: date
    direction: str
    status: str
    amount: int
    label: str
    source_type: str
    source_id: str | None
    # 화면이 원본 화면으로 이동할 때 쓴다. 없으면 이동 불가(그대로 표시).
    drill_href: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "occurred_on": self.occurred_on.isoformat(),
            "direction": self.direction,
            "status": self.status,
            "amount": self.amount,
            "label": self.label,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "drill_href": self.drill_href,
        }


def resolve_range(
    *, preset: str | None, date_from: date | None, date_to: date | None,
    today: date | None = None,
) -> tuple[date, date]:
    """조회 기간. 프리셋은 **오늘부터 앞으로** 본다 — 현금흐름은 예측이다."""
    base = today or date.today()
    if date_from and date_to:
        if date_to < date_from:
            raise ValidationError("종료일이 시작일보다 빠릅니다.")
        return date_from, date_to
    days = PRESET_DAYS.get(preset or "30d", 30)
    return base, base + timedelta(days=days)


# ──────────────────────────────────────────────────────────────────────────
# 기준 잔액
# ──────────────────────────────────────────────────────────────────────────

def get_baseline(
    db: Session, *, org_id: UUID, as_of: date | None = None
) -> dict[str, Any] | None:
    """기준일 이전(포함)의 가장 최근 기준 잔액.

    없으면 `None` — **0 을 돌려주지 않는다.** 0 으로 두면 화면이
    "통장에 0원" 으로 그린다.
    """
    row = db.execute(text("""
        SELECT id, as_of_date, amount, memo, created_at
          FROM cash_balance_baseline
         WHERE org_id = :org AND deleted_at IS NULL
           AND (CAST(:asof AS DATE) IS NULL OR as_of_date <= CAST(:asof AS DATE))
         ORDER BY as_of_date DESC
         LIMIT 1
    """), {"org": str(org_id), "asof": as_of}).mappings().first()
    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "as_of_date": row["as_of_date"].isoformat(),
        "amount": int(row["amount"]),
        "memo": row["memo"],
    }


def set_baseline(
    db: Session, *, org_id: UUID, actor_user_id: UUID, as_of_date: date,
    amount: int, memo: str | None = None,
) -> dict[str, Any]:
    """기준 잔액 입력/수정. 같은 날짜는 덮어쓴다(UNIQUE).

    같은 날에 기준 잔액이 둘이면 어느 쪽이 맞는지 알 수 없다.
    """
    before = db.execute(text("""
        SELECT id, amount FROM cash_balance_baseline
         WHERE org_id = :org AND as_of_date = :d AND deleted_at IS NULL
    """), {"org": str(org_id), "d": as_of_date}).mappings().first()

    row = db.execute(text("""
        INSERT INTO cash_balance_baseline
               (org_id, as_of_date, amount, memo, created_by, updated_by)
        VALUES (:org, :d, :amt, :memo, :actor, :actor)
        ON CONFLICT (org_id, as_of_date) WHERE deleted_at IS NULL
        DO UPDATE SET amount = EXCLUDED.amount, memo = EXCLUDED.memo,
                      updated_at = now(), updated_by = EXCLUDED.updated_by
        RETURNING id, as_of_date, amount, memo
    """), {"org": str(org_id), "d": as_of_date, "amt": amount,
           "memo": memo, "actor": str(actor_user_id)}).mappings().one()

    record_audit(
        db, org_id=org_id, domain_table="cash_balance_baseline",
        domain_row_id=UUID(str(row["id"])),
        action="UPDATE" if before else "CREATE", actor_user_id=actor_user_id,
        before={"amount": int(before["amount"])} if before else None,
        after={"as_of_date": as_of_date.isoformat(), "amount": amount},
        reason="기준 잔액 입력",
    )
    db.commit()
    return {
        "id": str(row["id"]), "as_of_date": row["as_of_date"].isoformat(),
        "amount": int(row["amount"]), "memo": row["memo"],
    }


# ──────────────────────────────────────────────────────────────────────────
# 수동 현금 기록
# ──────────────────────────────────────────────────────────────────────────

def create_manual_event(
    db: Session, *, org_id: UUID, actor_user_id: UUID,
    direction: str, status: str, amount: int,
    occurred_at: date | None = None, expected_at: date | None = None,
    category: str | None = None, memo: str | None = None,
    source_type: str | None = None, source_id: UUID | None = None,
) -> dict[str, Any]:
    """수동 현금 기록 1건.

    **이중 계상은 DB 가 막는다.** 이미 `payout_payment` 로 기록된 지급을
    수동 출금으로 또 넣으면 부분 UNIQUE 에 걸린다(M034).
    애플리케이션 검사만으로는 동시 요청에서 뚫린다.
    """
    if direction not in (INFLOW, OUTFLOW):
        raise ValidationError(f"알 수 없는 방향입니다: {direction}")
    if status not in (EXPECTED, CONFIRMED, ACTUAL):
        raise ValidationError(f"알 수 없는 상태입니다: {status}")
    if amount <= 0:
        # 방향은 `direction` 이 정한다. 음수로 방향을 표현하면 합계에서 뒤집힌다.
        raise ValidationError("금액은 0보다 커야 합니다.")
    if status == ACTUAL and occurred_at is None:
        raise ValidationError(
            "실제 입출금은 발생일이 필요합니다. 언제 움직였는지 모르면 현금흐름에 넣을 수 없습니다."
        )
    if status != ACTUAL and expected_at is None:
        raise ValidationError("예정 항목은 예정일이 필요합니다.")
    if source_type in AGGREGATED_SOURCE_TYPES:
        # 읽기 모델이 이미 세고 있는 원본이다. 넣어도 합계에서 빠지지만,
        # 조용히 무시하면 사용자는 "기록했는데 왜 금액이 안 늘지" 를 묻게 된다.
        raise ValidationError(
            f"{source_type} 은(는) 이미 현금흐름에 자동으로 집계됩니다. "
            "같은 건을 여기에 또 기록하면 금액이 두 배로 잡힙니다.",
            detail={"source_type": source_type},
        )

    try:
        row = db.execute(text("""
            INSERT INTO manual_cash_event
                   (org_id, direction, status, amount, occurred_at, expected_at,
                    category, memo, source_type, source_id, created_by, updated_by)
            VALUES (:org, :dir, :st, :amt, :occ, :exp, :cat, :memo, :stype, :sid,
                    :actor, :actor)
            RETURNING id
        """), {"org": str(org_id), "dir": direction, "st": status, "amt": amount,
               "occ": occurred_at, "exp": expected_at, "cat": category, "memo": memo,
               "stype": source_type, "sid": str(source_id) if source_id else None,
               "actor": str(actor_user_id)}).mappings().one()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        domain = translate_integrity_error(exc, {
            "uq_manual_cash_source": lambda: ConflictError(
                "이 원본은 이미 현금흐름에 기록돼 있습니다. "
                "같은 지급을 두 번 넣으면 출금이 두 배로 잡힙니다.",
                detail={"source_type": source_type, "source_id": str(source_id)},
            ),
        })
        if domain is not None:
            raise domain from exc
        raise

    record_audit(
        db, org_id=org_id, domain_table="manual_cash_event",
        domain_row_id=UUID(str(row["id"])), action="CREATE",
        actor_user_id=actor_user_id,
        after={"direction": direction, "status": status, "amount": amount,
               "occurred_at": occurred_at.isoformat() if occurred_at else None,
               "expected_at": expected_at.isoformat() if expected_at else None},
        reason="수동 현금 기록",
    )
    db.commit()
    return get_manual_event(db, org_id=org_id, event_id=UUID(str(row["id"])))


def get_manual_event(db: Session, *, org_id: UUID, event_id: UUID) -> dict[str, Any]:
    row = db.execute(text("""
        SELECT id, direction, status, amount, currency, occurred_at, expected_at,
               category, memo, source_type, source_id, created_at
          FROM manual_cash_event
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(event_id), "org": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("현금 기록을 찾을 수 없습니다.")
    d = dict(row)
    d["id"] = str(d["id"])
    d["source_id"] = str(d["source_id"]) if d["source_id"] else None
    for k in ("occurred_at", "expected_at"):
        d[k] = d[k].isoformat() if d[k] else None
    return d


def list_manual_events(
    db: Session, *, org_id: UUID, date_from: date, date_to: date,
) -> list[dict[str, Any]]:
    rows = db.execute(text("""
        SELECT id, direction, status, amount, occurred_at, expected_at,
               category, memo, source_type, source_id
          FROM manual_cash_event
         WHERE org_id = :org AND deleted_at IS NULL
           AND COALESCE(occurred_at, expected_at) BETWEEN :s AND :e
         ORDER BY COALESCE(occurred_at, expected_at), created_at
    """), {"org": str(org_id), "s": date_from, "e": date_to}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["source_id"] = str(d["source_id"]) if d["source_id"] else None
        for k in ("occurred_at", "expected_at"):
            d[k] = d[k].isoformat() if d[k] else None
        out.append(d)
    return out


def delete_manual_event(
    db: Session, *, org_id: UUID, actor_user_id: UUID, event_id: UUID, reason: str,
) -> None:
    """행을 지우지 않고 취소 표시. 무엇을 지웠는지 남아야 한다."""
    if not reason or not reason.strip():
        raise ValidationError("현금 기록 삭제에는 사유가 필요합니다.")

    res = db.execute(text("""
        UPDATE manual_cash_event SET deleted_at = now(), deleted_by = :actor
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(event_id), "org": str(org_id), "actor": str(actor_user_id)})
    if res.rowcount == 0:
        raise NotFoundError("현금 기록을 찾을 수 없습니다.")

    record_audit(
        db, org_id=org_id, domain_table="manual_cash_event", domain_row_id=event_id,
        action="DELETE", actor_user_id=actor_user_id, reason=reason,
    )
    db.commit()


# ──────────────────────────────────────────────────────────────────────────
# 읽기 모델 — 기존 source 를 모아 계산한다
# ──────────────────────────────────────────────────────────────────────────

def _actual_outflows(
    db: Session, *, org_id: UUID, date_from: date, date_to: date
) -> list[CashRow]:
    """실제 지급 — `payout_payment`.

    취소된 지급은 `deleted_at` 이 찍힌다(행을 지우지 않는다). 그래서
    `deleted_at IS NULL` 만 센다 — 취소한 지급이 현금흐름에 남아 있으면 안 된다.
    """
    rows = db.execute(text("""
        SELECT pp.id, pp.payment_date, pp.amount,
               pi.payee_name_snapshot, pi.item_type, pi.id AS item_id
          FROM payout_payment pp
          JOIN payout_item pi ON pi.id = pp.payout_item_id
         WHERE pp.org_id = :org AND pp.deleted_at IS NULL
           AND pp.payment_date BETWEEN :s AND :e
         ORDER BY pp.payment_date
    """), {"org": str(org_id), "s": date_from, "e": date_to}).mappings().all()

    return [
        CashRow(
            occurred_on=r["payment_date"], direction=OUTFLOW, status=ACTUAL,
            amount=int(r["amount"]),
            label=f"{r['payee_name_snapshot']} 지급",
            source_type="payout_payment", source_id=str(r["id"]),
            drill_href="/payouts",
        )
        for r in rows
    ]


def _expected_outflows(
    db: Session, *, org_id: UUID, date_from: date, date_to: date
) -> list[CashRow]:
    """줘야 하는데 아직 안 준 것 — `payout_item` 의 미지급 잔액.

    `due_amount - paid_amount` 다. 이미 일부 지급했으면 남은 만큼만 예상 출금이다.
    승인 전(CALCULATED)은 제외한다 — 아직 줄지 말지 정해지지 않았다.
    """
    rows = db.execute(text("""
        SELECT pi.id, pi.due_date, pi.due_amount - pi.paid_amount AS outstanding,
               pi.payee_name_snapshot, pi.item_type, pb.period_year_month
          FROM payout_item pi
          JOIN payout_batch pb ON pb.id = pi.batch_id
         WHERE pi.org_id = :org AND pi.deleted_at IS NULL
           AND pi.status IN ('APPROVED', 'PARTIALLY_PAID')
           AND pi.due_amount > pi.paid_amount
           AND pi.due_date IS NOT NULL
           AND pi.due_date BETWEEN :s AND :e
         ORDER BY pi.due_date
    """), {"org": str(org_id), "s": date_from, "e": date_to}).mappings().all()

    return [
        CashRow(
            occurred_on=r["due_date"], direction=OUTFLOW, status=EXPECTED,
            amount=int(r["outstanding"]),
            label=f"{r['payee_name_snapshot']} 지급 예정",
            source_type="payout_item", source_id=str(r["id"]),
            drill_href="/payouts",
        )
        for r in rows
    ]


def _confirmed_inflows(
    db: Session, *, org_id: UUID, date_from: date, date_to: date
) -> list[CashRow]:
    """판매처가 확정한 정산액 — **ACTUAL 이 아니다.**

    `settled_at` 이 실제 은행 입금일이라는 근거가 스키마에도 데이터에도 없다
    (docs/54 §3-2). 최대 CONFIRMED 다.
    """
    rows = db.execute(text("""
        SELECT msr.id, msr.settled_at, msr.net_settlement, msr.marketplace,
               ia.account_label
          FROM marketplace_settlement_record msr
          LEFT JOIN integration_account ia ON ia.id = msr.integration_account_id
         WHERE msr.org_id = :org AND msr.deleted_at IS NULL
           -- 날짜를 모르면 어느 칸에도 넣을 수 없다. 오늘로 밀어 넣지 않는다.
           -- (`net_settlement` 는 NOT NULL 이므로 금액은 항상 있다 — M001)
           AND msr.settled_at IS NOT NULL
           AND msr.settled_at BETWEEN :s AND :e
         ORDER BY msr.settled_at
    """), {"org": str(org_id), "s": date_from, "e": date_to}).mappings().all()

    return [
        CashRow(
            occurred_on=r["settled_at"], direction=INFLOW, status=CONFIRMED,
            amount=int(r["net_settlement"]),
            label=(
                f"{r['account_label']} 정산" if r["account_label"]
                else f"{r['marketplace']} 정산"
            ),
            source_type="marketplace_settlement_record", source_id=str(r["id"]),
            drill_href="/settlements/marketplace",
        )
        for r in rows
    ]


def _manual_rows(
    db: Session, *, org_id: UUID, date_from: date, date_to: date
) -> list[CashRow]:
    rows = db.execute(text("""
        SELECT id, direction, status, amount,
               COALESCE(occurred_at, expected_at) AS on_date,
               category, memo
          FROM manual_cash_event
         WHERE org_id = :org AND deleted_at IS NULL
           AND COALESCE(occurred_at, expected_at) BETWEEN :s AND :e
           -- 이미 직접 집계하는 원본을 가리키는 행은 뺀다. 남겨 두면 지급 한 건이
           -- 두 줄(지급 기록 + 수동 기록)이 되어 출금이 두 배가 된다.
           AND (source_type IS NULL OR source_type <> ALL(:counted))
         ORDER BY COALESCE(occurred_at, expected_at)
    """), {"org": str(org_id), "s": date_from, "e": date_to,
           "counted": list(AGGREGATED_SOURCE_TYPES)}).mappings().all()

    return [
        CashRow(
            occurred_on=r["on_date"], direction=r["direction"], status=r["status"],
            amount=int(r["amount"]),
            label=r["category"] or r["memo"] or "수동 기록",
            source_type="manual_cash_event", source_id=str(r["id"]),
            drill_href="/finance/cashflow?tab=manual",
        )
        for r in rows
    ]


def collect_rows(
    db: Session, *, org_id: UUID, date_from: date, date_to: date
) -> list[CashRow]:
    """기간 안의 모든 현금 이동. 각 줄이 자기 출처를 갖는다."""
    rows = (
        _actual_outflows(db, org_id=org_id, date_from=date_from, date_to=date_to)
        + _expected_outflows(db, org_id=org_id, date_from=date_from, date_to=date_to)
        + _confirmed_inflows(db, org_id=org_id, date_from=date_from, date_to=date_to)
        + _manual_rows(db, org_id=org_id, date_from=date_from, date_to=date_to)
    )
    rows.sort(key=lambda r: (r.occurred_on, r.direction, r.source_type))
    return rows


def _sum(rows: list[CashRow], direction: str, status: str) -> int:
    return sum(r.amount for r in rows if r.direction == direction and r.status == status)


def build_summary(
    db: Session, *, org_id: UUID, preset: str | None = None,
    date_from: date | None = None, date_to: date | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """기간 요약 + 타임라인 + 출처별 분해.

    잔액은 기준 잔액이 있을 때만 계산한다 — 없으면 `None` 이다.
    0 으로 두면 화면이 "통장에 0원" 으로 그린다.
    """
    start, end = resolve_range(
        preset=preset, date_from=date_from, date_to=date_to, today=today
    )
    rows = collect_rows(db, org_id=org_id, date_from=start, date_to=end)
    baseline = get_baseline(db, org_id=org_id, as_of=start)

    expected_in = _sum(rows, INFLOW, EXPECTED)
    confirmed_in = _sum(rows, INFLOW, CONFIRMED)
    actual_in = _sum(rows, INFLOW, ACTUAL)
    expected_out = _sum(rows, OUTFLOW, EXPECTED)
    actual_out = _sum(rows, OUTFLOW, ACTUAL)
    confirmed_out = _sum(rows, OUTFLOW, CONFIRMED)

    # 예상 잔액은 **모든 상태를 합친 순변동**이다. 확정·실제도 앞으로의 잔액에
    # 영향을 주기 때문이다. 상태별 합계는 위에서 따로 준다.
    net_change = (expected_in + confirmed_in + actual_in) - (
        expected_out + confirmed_out + actual_out
    )

    timeline, lowest = _build_timeline(rows, baseline, start, end)

    return {
        "range": {"from": start.isoformat(), "to": end.isoformat(),
                  "preset": preset if not (date_from and date_to) else None},
        "baseline": baseline,
        # 기준 잔액이 없으면 잔액 계산 자체가 불가능하다 — 화면이 그 사실을 말한다.
        "balance_available": baseline is not None,
        "inflow": {"expected": expected_in, "confirmed": confirmed_in, "actual": actual_in},
        "outflow": {"expected": expected_out, "confirmed": confirmed_out, "actual": actual_out},
        "projected_ending_balance": (
            baseline["amount"] + net_change if baseline else None
        ),
        "lowest_projected_balance": lowest,
        "unpaid_payout": _unpaid_payout(db, org_id=org_id),
        "overdue_payout": _overdue_payout(db, org_id=org_id, today=today or date.today()),
        "timeline": timeline,
        "rows": [r.as_dict() for r in rows],
        # 입금 쪽 ACTUAL 근거가 없다는 사실을 화면이 표시할 수 있게 알려준다.
        "actual_inflow_source_available": actual_in > 0,
    }


def _build_timeline(
    rows: list[CashRow], baseline: dict[str, Any] | None, start: date, end: date,
) -> tuple[list[dict[str, Any]], int | None]:
    """날짜별 순변동 + 누적 잔액.

    기준 잔액이 없으면 누적 잔액은 `None` 이다 — 0 에서 시작한 것처럼 그리지 않는다.
    """
    by_day: dict[date, dict[str, int]] = {}
    for r in rows:
        bucket = by_day.setdefault(r.occurred_on, {"inflow": 0, "outflow": 0})
        bucket["inflow" if r.direction == INFLOW else "outflow"] += r.amount

    running = baseline["amount"] if baseline else None
    lowest = running
    out: list[dict[str, Any]] = []
    for day in sorted(by_day):
        b = by_day[day]
        net = b["inflow"] - b["outflow"]
        if running is not None:
            running += net
            lowest = min(lowest, running) if lowest is not None else running
        out.append({
            "date": day.isoformat(), "inflow": b["inflow"], "outflow": b["outflow"],
            "net": net, "balance": running,
        })
    _ = (start, end)
    return out, lowest


def _unpaid_payout(db: Session, *, org_id: UUID) -> int:
    """기간과 무관하게 남아 있는 미지급 총액. 기한이 없는 항목도 포함한다."""
    return int(db.execute(text("""
        SELECT COALESCE(SUM(due_amount - paid_amount), 0)
          FROM payout_item
         WHERE org_id = :org AND deleted_at IS NULL
           AND status IN ('APPROVED', 'PARTIALLY_PAID')
           AND due_amount > paid_amount
    """), {"org": str(org_id)}).scalar() or 0)


def _overdue_payout(db: Session, *, org_id: UUID, today: date) -> int:
    return int(db.execute(text("""
        SELECT COALESCE(SUM(due_amount - paid_amount), 0)
          FROM payout_item
         WHERE org_id = :org AND deleted_at IS NULL
           AND status IN ('APPROVED', 'PARTIALLY_PAID')
           AND due_amount > paid_amount
           AND due_date IS NOT NULL AND due_date < :today
    """), {"org": str(org_id), "today": today}).scalar() or 0)
