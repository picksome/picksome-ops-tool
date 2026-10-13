"""월 마감 서비스 — 검증 / 검토요청 / 확정 / 잠금 / 잠금해제.

상태: OPEN → REVIEW → CONFIRMED → LOCKED  (M017)

```
판매 → 비용 → 정산 → 조정 → 승인 → 검토 → 순이익 snapshot
     → 수익배분 기준 확정 → 월 확정 → 잠금
```

설계 판단
---------
**모든 경고를 차단으로 만들지 않는다.** 증빙이 하나 빠졌다고 월 마감을 통째로 막으면
사용자는 결국 검증을 끄거나 무시하게 된다. 등급 정의와 개별 항목은
`services/close_checks.py` 로 옮겼다(Phase 26에서 5개 → 16개로 늘었다).

warning 을 넘기려면 `acknowledged_warnings` 에 해당 코드를 명시해야 한다.
"모르고 넘어간 것" 과 "보고 넘긴 것" 을 구분하기 위해서다. 그 기록은 남는다.

CONFIRMED 와 LOCKED 는 다른 뜻이다
--------------------------------
```
CONFIRMED  업무 검토가 끝났다. 숫자를 snapshot 으로 박제한다.
           일반 운영자는 못 바꾸지만 되돌릴 수 있다(CONFIRMED → REVIEW).
LOCKED     최종 봉인. 그 달의 판매·비용·매칭이 전부 읽기 전용이 된다.
           되돌리려면 재오픈 승인이 필요하고 횟수 한도가 있다.
```

확정 트랜잭션
------------
검증 재실행 · blocking 0 확인 · snapshot INSERT · 상태 변경 · audit 이
**하나의 트랜잭션**이다. 하나라도 실패하면 전부 롤백된다 —
snapshot 없이 CONFIRMED 인 달이나, CONFIRMED 아닌데 snapshot 이 있는 달을 만들지 않는다.

검증 run 기록만 예외적으로 먼저 커밋한다. run 은 "무엇이 보였는가" 라는 관찰
기록이고, 확정이 거부돼도 남아야 하기 때문이다.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.services import carryover, close_checks, close_snapshot, notification_events
from app.services.close_checks import BLOCKING, ValidationItem

__all__ = [
    "ValidationItem", "validate_month", "run_validation", "get_month", "list_months",
    "request_review", "confirm", "lock", "unlock", "preview_snapshot", "check_drift",
]

# 상태 전이 허용표. 여기 없는 전이는 전부 거부한다.
_ALLOWED: dict[str, tuple[str, ...]] = {
    "OPEN": ("REVIEW", "CONFIRMED"),      # 작은 조직은 검토 단계를 건너뛰기도 한다
    "REVIEW": ("OPEN", "CONFIRMED"),
    "CONFIRMED": ("REVIEW", "LOCKED"),    # 확정 후에도 되돌릴 수 있다. 잠그기 전까지는.
    "LOCKED": ("OPEN",),                  # 잠금 해제 = 재개. ADMIN + 사유 필수
}


# ──────────────────────────────────────────────────────────────
# 검증
# ──────────────────────────────────────────────────────────────

def validate_month(db: Session, *, org_id: UUID, year_month: str) -> list[ValidationItem]:
    """마감 전 검증. 실행만 하고 저장하지 않는다(저장은 `run_validation`).

    항목 카탈로그는 `services/close_checks.py` 에 있다. 이 함수는 기존 호출부와의
    계약을 유지하기 위한 얇은 위임이다.
    """
    return close_checks.run_checks(db, org_id=org_id, year_month=year_month)


# ──────────────────────────────────────────────────────────────
# 조회 / 전이
# ──────────────────────────────────────────────────────────────

def get_month(db: Session, *, org_id: UUID, year_month: str) -> dict[str, Any]:
    row = db.execute(text("""
        SELECT id, year_month, close_state, status,
               total_revenue, total_direct_cost, total_common_expense, net_profit,
               distributable_amount, reopen_count, reopen_limit_override,
               review_requested_at, validated_at, validation_result_json,
               acknowledged_warnings_json, confirmed_at, locked_at,
               close_snapshot_id, close_generation, last_check_run_id,
               last_reopened_at, last_reopen_reason
          FROM monthly_settlement
         WHERE org_id = :org AND year_month = :ym AND deleted_at IS NULL
         ORDER BY created_at DESC LIMIT 1
    """), {"org": str(org_id), "ym": year_month}).mappings().first()

    if row is None:
        # 아직 정산을 돌리지 않은 달. 존재하지 않는 게 아니라 "열려 있는 달" 이다.
        return {
            "year_month": year_month, "close_state": "OPEN", "exists": False,
            "total_revenue": 0, "total_direct_cost": 0, "total_common_expense": 0,
            "net_profit": 0, "distributable_amount": 0, "reopen_count": 0,
            "close_generation": 0, "validation_result": None,
            "acknowledged_warnings": [], "snapshot": None,
            "profit_label": "잠정 순이익 (미확정)",
        }

    d = dict(row)
    d["exists"] = True
    d["validation_result"] = d.pop("validation_result_json", None)
    d["acknowledged_warnings"] = d.pop("acknowledged_warnings_json", None) or []

    # 확정된 달의 숫자는 snapshot 이 진실이다. live 재집계로 다른 값을 보여주지 않는다.
    snap = close_snapshot.get_current(db, org_id=org_id, year_month=year_month)
    d["snapshot"] = _public_snapshot(snap) if snap else None
    d["profit_label"] = _profit_label(d["close_state"], snap)
    return d


def _public_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    """API 로 내보낼 snapshot 표현. 내부 식별자를 그대로 흘리지 않는다."""
    keep = (
        "generation", "taken_at_state", "taken_at", "reason",
        "order_count", "gross_sales",
        "cogs", "shipping_cost", "marketplace_fees", "payment_fees",
        "advertising_cost", "fulfillment_fees", "returns_cost", "total_direct_cost",
        "business_expenses", "labor_expense", "personal_card_expense",
        "expense_vat", "adjustment_total",
        "settlement_connected", "settlement_record_count",
        "settlement_unmatched_count", "confirmed_settlement_amount",
        "net_profit", "carryover_offset", "distributable_profit",
        "provisional_profit_total", "settled_profit_total", "final_profit_total",
        "profit_basis", "warning_count", "source_cutoff_at",
        "applied_policy_versions_json", "calculation_version", "content_hash",
    )
    out = {k: snap[k] for k in keep if k in snap}
    out["id"] = str(snap["id"])
    return out


def _profit_label(close_state: str, snap: dict[str, Any] | None) -> str:
    """이 달의 순이익을 뭐라고 불러야 하는가.

    정산이 연결되지 않았는데 "확정 순이익" 이라고 부르면 과장이다(Phase 30).
    """
    if snap is None:
        return "잠정 순이익 (미확정)"
    reconciled = snap.get("profit_basis") == "SETTLEMENT_RECONCILED"
    if close_state == "LOCKED":
        return "확정 순이익" if reconciled else "확정 순이익 (정산 미연결 — 판매·비용 기준)"
    return "검토 확정 순이익" if reconciled else "검토 확정 순이익 (정산 미연결)"


def list_months(db: Session, *, org_id: UUID, limit: int = 24) -> list[dict[str, Any]]:
    """월 목록.

    확정된 달은 `snapshot_net_profit` 이 채워진다. 목록 화면이 확정월과 진행중인 달을
    같은 컬럼에 섞어 보여주면 "7월 순이익" 이 매일 달라 보인다 — 두 값을 분리해서 준다.
    """
    rows = db.execute(text("""
        SELECT ms.year_month, ms.close_state, ms.net_profit, ms.distributable_amount,
               ms.reopen_count, ms.confirmed_at, ms.locked_at, ms.close_generation,
               s.net_profit           AS snapshot_net_profit,
               s.distributable_profit AS snapshot_distributable_profit,
               s.profit_basis         AS snapshot_profit_basis
          FROM monthly_settlement ms
          LEFT JOIN monthly_close_snapshot s
                 ON s.org_id = ms.org_id AND s.year_month = ms.year_month
                AND s.superseded_at IS NULL
         WHERE ms.org_id = :org AND ms.deleted_at IS NULL
         ORDER BY ms.year_month DESC, ms.id DESC
         LIMIT :lim
    """), {"org": str(org_id), "lim": limit}).mappings().all()
    return [dict(r) for r in rows]


def _load_row(db: Session, org_id: UUID, year_month: str, *, for_update: bool = False):
    """월 정산 행 조회.

    `for_update=True` 면 행 잠금을 건다. 두 사람이 같은 달을 동시에 확정/잠금할 때
    둘 다 통과하는 것을 막는다. 잠금만으로는 부족해서 UPDATE 에 CAS 조건도 함께 건다
    (`_transition` 참조) — 잠금을 못 잡는 경로가 생겨도 상태가 어긋나지 않게.
    """
    row = db.execute(text(f"""
        SELECT id, close_state, reopen_count, reopen_limit_override
          FROM monthly_settlement
         WHERE org_id = :org AND year_month = :ym AND deleted_at IS NULL
         ORDER BY created_at DESC LIMIT 1
         {"FOR UPDATE" if for_update else ""}
    """), {"org": str(org_id), "ym": year_month}).first()
    if row is None:
        raise NotFoundError(
            f"{year_month} 정산이 아직 생성되지 않았습니다. 월별 정산에서 먼저 계산하세요."
        )
    return row


def _transition(
    db: Session, *, org_id: UUID, year_month: str, to_state: str,
    actor_user_id: UUID, reason: str | None, request_id: str | None,
    extra_sql: str = "", extra_params: dict[str, Any] | None = None,
    commit: bool = True, on_transition=None,
) -> dict[str, Any]:
    """상태 전이.

    `commit=False` 는 호출자가 **같은 트랜잭션에 더 넣을 것이 있다**는 뜻이다
    (확정의 snapshot INSERT 가 그렇다). 이 경우 커밋 책임은 호출자에게 있다.

    `on_transition(settlement_id)` 는 상태를 바꾼 직후, 아직 커밋하기 전에
    같은 트랜잭션 안에서 호출된다 — snapshot 이나 이월 원장처럼 상태 변경과
    원자적이어야 하는 작업을 여기에 건다.
    """
    # ⚠️ SAVEPOINT 안에서 잠그고 바꾼다.
    #
    # 예전 구현은 거부 경로에서 `db.rollback()` 을 불렀다. 서비스가 요청 트랜잭션을
    # **소유하지 않고 공유**하기 때문에(deps.get_db 가 세션만 넘겨준다), 그 rollback 은
    # 같은 요청에서 앞서 수행한 무관한 변경까지 통째로 되돌린다.
    # 지금은 SAVEPOINT 만 되돌린다 — FOR UPDATE 로 잡은 행 잠금도 함께 풀리므로
    # 교착도 생기지 않는다(PostgreSQL 은 savepoint 이후 획득한 잠금을 함께 해제한다).
    savepoint = db.begin_nested()

    row = _load_row(db, org_id, year_month, for_update=True)
    current = row.close_state

    if to_state not in _ALLOWED.get(current, ()):
        savepoint.rollback()
        raise ConflictError(
            f"{year_month} 은 {current} 상태라 {to_state} 로 바꿀 수 없습니다.",
            detail={"from": current, "to": to_state, "allowed": list(_ALLOWED.get(current, ()))},
        )

    # 기존 status 컬럼도 함께 동기화한다 — 기존 정산/리포트 코드가 이걸 읽는다
    legacy = {"OPEN": "DRAFT", "REVIEW": "DRAFT", "CONFIRMED": "CONFIRMED", "LOCKED": "LOCKED"}[to_state]

    # compare-and-swap: 읽은 상태가 그대로일 때만 바꾼다.
    # 잠금을 놓친 경로가 있어도 두 번째 요청은 rowcount 0 을 받아 409 로 떨어진다.
    result = db.execute(
        text(f"""
            UPDATE monthly_settlement
               SET close_state = :to_state,
                   status = :legacy,
                   updated_at = now(),
                   updated_by = :actor
                   {extra_sql}
             WHERE id = :id AND close_state = :expected_from
        """),
        {"to_state": to_state, "legacy": legacy, "actor": str(actor_user_id),
         "id": str(row.id), "expected_from": current, **(extra_params or {})},
    )
    if result.rowcount == 0:
        # 우리가 읽은 사이에 다른 요청이 상태를 바꿨다.
        savepoint.rollback()
        raise ConflictError(
            f"{year_month} 의 마감 상태가 방금 다른 요청에 의해 변경되었습니다. "
            "현재 상태를 다시 확인한 뒤 시도하세요.",
            detail={"expected_from": current, "attempted_to": to_state},
        )

    record_audit(
        db,
        org_id=org_id,
        domain_table="monthly_settlement",
        domain_row_id=row.id,
        action="STATUS_CHANGE",
        actor_user_id=actor_user_id,
        before={"close_state": current},
        after={"close_state": to_state},
        reason=reason,
        request_id=request_id,
    )

    # 상태 변경과 원자적이어야 하는 후속 작업 (snapshot INSERT, 이월 원장 …).
    # 여기서 예외가 나면 savepoint 가 되돌아가고 상태 변경도 함께 사라진다.
    if on_transition is not None:
        try:
            on_transition(UUID(str(row.id)))
        except Exception:
            savepoint.rollback()
            raise

    savepoint.commit()
    if commit:
        db.commit()
    return get_month(db, org_id=org_id, year_month=year_month)


def run_validation(
    db: Session, *, org_id: UUID, year_month: str, actor_user_id: UUID,
    trigger: str = "MANUAL",
) -> list[ValidationItem]:
    """검증 실행 + run 기록 + 커밋.

    run 은 **결과와 무관하게 남는다.** 확정이 거부돼도 "그때 무엇이 보였는가" 는
    남아야 한다. 그래서 여기서 커밋한다.
    """
    close_state = get_close_state_of(db, org_id=org_id, year_month=year_month)
    _run_id, items = close_checks.persist_run(
        db, org_id=org_id, year_month=year_month, close_state=close_state,
        trigger=trigger, actor_user_id=actor_user_id,
    )
    db.commit()

    # 검증 결과를 알림으로 남긴다.
    #
    # **아직 확정되지 않은 달만 알린다.** 이미 확정·잠금된 달을 다시 검증하는 것은
    # drift 확인이지 "마감하세요" 가 아니다.
    #
    # dedupe 키에 차단 항목 조합이 들어가므로, 사유가 바뀌면 새 알림이고
    # 같은 사유가 계속되는 동안에는 하나로 유지된다 — 5분마다 도는 검증이
    # 알림함을 채우지 않는다.
    if close_state in ("OPEN", "REVIEW"):
        blocking = [i.code for i in items if i.grade == close_checks.BLOCKING]
        if blocking:
            notification_events.month_close_blocked(
                db, org_id=org_id, year_month=year_month, blocking_codes=blocking,
            )
        else:
            notification_events.month_close_ready(
                db, org_id=org_id, year_month=year_month,
            )
        db.commit()

    return items


def get_close_state_of(db: Session, *, org_id: UUID, year_month: str) -> str:
    row = db.execute(text("""
        SELECT close_state FROM monthly_settlement
         WHERE org_id = :org AND year_month = :ym AND deleted_at IS NULL
         ORDER BY created_at DESC LIMIT 1
    """), {"org": str(org_id), "ym": year_month}).first()
    return row.close_state if row else "OPEN"


def list_check_runs(db: Session, *, org_id: UUID, year_month: str, limit: int = 20):
    return close_checks.list_runs(db, org_id=org_id, year_month=year_month, limit=limit)


def get_check_run(db: Session, *, org_id: UUID, run_id: UUID):
    run = close_checks.get_run(db, org_id=org_id, run_id=run_id)
    if run is None:
        raise NotFoundError("검증 실행 기록을 찾을 수 없습니다.")
    return run


def preview_snapshot(db: Session, *, org_id: UUID, year_month: str) -> dict[str, Any]:
    """확정하면 어떤 숫자가 박제되는지 미리 본다. **아무것도 바꾸지 않는다.**"""
    return close_snapshot.compute_figures(db, org_id=org_id, year_month=year_month)


def check_drift(db: Session, *, org_id: UUID, year_month: str) -> dict[str, Any] | None:
    """확정 후 원본 데이터가 조용히 바뀌었는지 확인한다."""
    return close_snapshot.detect_drift(db, org_id=org_id, year_month=year_month)


def snapshot_history(db: Session, *, org_id: UUID, year_month: str):
    """세대 이력. 재오픈으로 숫자가 어떻게 달라졌는지 비교할 수 있어야 한다."""
    return close_snapshot.list_generations(db, org_id=org_id, year_month=year_month)


def _assert_can_close(
    items: list[ValidationItem], acknowledged: set[str], *, action: str
) -> None:
    blocking = [i for i in items if i.grade == BLOCKING]
    if blocking:
        raise ValidationError(
            f"{action}할 수 없습니다. 차단 항목 {len(blocking)}건을 먼저 처리하세요.",
            detail={"blocking": [close_checks.as_json(i) for i in blocking]},
        )
    unacked = [i.code for i in items if i.grade != BLOCKING and i.code not in acknowledged]
    if unacked:
        raise ValidationError(
            "확인하지 않은 경고가 있습니다. 내용을 확인한 뒤 다시 시도하세요.",
            detail={
                "unacknowledged": unacked,
                "warnings": [close_checks.as_json(i) for i in items if i.code in unacked],
            },
        )


def request_review(db, *, org_id, year_month, actor_user_id, request_id=None):
    return _transition(
        db, org_id=org_id, year_month=year_month, to_state="REVIEW",
        actor_user_id=actor_user_id, reason="검토 요청", request_id=request_id,
        extra_sql=", review_requested_at = now(), review_requested_by = :actor2",
        extra_params={"actor2": str(actor_user_id)},
    )


def confirm(
    db: Session, *, org_id: UUID, year_month: str, actor_user_id: UUID,
    acknowledged_warnings: list[str] | None = None, request_id: str | None = None,
) -> dict[str, Any]:
    """월 확정.

    한 트랜잭션에서 처리하는 것:
      · 최종 검증 재실행 (blocking 0 확인)
      · snapshot INSERT
      · close_state 변경
      · audit log

    하나라도 실패하면 전부 롤백된다. 검증 run 기록만 앞서 별도로 커밋된다 —
    거부된 확정 시도의 근거도 남아야 하기 때문이다.
    """
    ack = set(acknowledged_warnings or [])

    # 1) 관찰 기록. 결과와 무관하게 남긴다.
    items = run_validation(
        db, org_id=org_id, year_month=year_month,
        actor_user_id=actor_user_id, trigger="CONFIRM",
    )
    _assert_can_close(items, ack, action="확정")

    # 2) 원자 트랜잭션. 전이 직후 같은 savepoint 안에서 검증을 다시 돌리고
    #    snapshot 을 만든다. 1) 이후 다른 요청이 데이터를 바꿨다면 여기서 잡힌다.
    def _on_transition(settlement_id: UUID) -> None:
        final_items = close_checks.run_checks(db, org_id=org_id, year_month=year_month)
        final_blocking = [i for i in final_items if i.grade == BLOCKING]
        if final_blocking:
            raise ConflictError(
                "확정 직전에 차단 항목이 새로 생겼습니다. 다시 검증하세요.",
                detail={"blocking": [close_checks.as_json(i) for i in final_blocking]},
            )
        close_snapshot.create_snapshot(
            db, org_id=org_id, year_month=year_month,
            monthly_settlement_id=settlement_id,
            taken_at_state="CONFIRMED", actor_user_id=actor_user_id,
            reason="월 확정", check_run_id=None,
            warning_count=sum(1 for i in final_items if i.grade != BLOCKING),
            acknowledged_warnings=sorted(ack),
        )

    return _transition(
        db, org_id=org_id, year_month=year_month, to_state="CONFIRMED",
        actor_user_id=actor_user_id, reason="월 확정", request_id=request_id,
        extra_sql=(", confirmed_at = now(), confirmed_by = :actor3,"
                   " acknowledged_warnings_json = CAST(:ack AS JSONB)"),
        extra_params={"actor3": str(actor_user_id),
                      "ack": json.dumps(sorted(ack), ensure_ascii=False)},
        on_transition=_on_transition,
    )


def lock(db, *, org_id, year_month, actor_user_id, request_id=None,
         acknowledged_warnings: list[str] | None = None):
    """월 잠금 — 최종 봉인.

    확정과 잠금 사이에 데이터가 바뀌었으면 잠그지 않는다. snapshot hash 를 다시
    계산해 비교한다 — 바뀐 채로 잠그면 **박제된 숫자와 실제 데이터가 어긋난 채로
    영구히 봉인**된다.
    """
    ack = set(acknowledged_warnings or [])
    items = run_validation(
        db, org_id=org_id, year_month=year_month,
        actor_user_id=actor_user_id, trigger="LOCK",
    )
    blocking = [i for i in items if i.grade == BLOCKING]
    if blocking:
        raise ValidationError(
            f"잠글 수 없습니다. 차단 항목 {len(blocking)}건을 먼저 처리하세요.",
            detail={"blocking": [close_checks.as_json(i) for i in blocking]},
        )

    drift = close_snapshot.detect_drift(db, org_id=org_id, year_month=year_month)
    if drift and drift["drifted"]:
        raise ConflictError(
            "확정 이후 이 달의 데이터가 변경되었습니다. "
            "월을 재오픈해 다시 확정한 뒤 잠그세요.",
            detail={
                "changed_fields": drift["changed_fields"],
                "snapshot_hash": drift["snapshot_hash"],
                "current_hash": drift["current_hash"],
            },
        )

    def _on_transition(settlement_id: UUID) -> None:
        # M028 이전에 확정된 달은 snapshot 이 없다. 잠금 시점에 만들어 준다 —
        # 그래야 잠긴 달의 확정 숫자를 물어볼 수 있다.
        if close_snapshot.get_current(db, org_id=org_id, year_month=year_month) is None:
            close_snapshot.create_snapshot(
                db, org_id=org_id, year_month=year_month,
                monthly_settlement_id=settlement_id,
                taken_at_state="LOCKED", actor_user_id=actor_user_id,
                reason="월 잠금 (확정 시 snapshot 없음 — 소급 생성)",
                check_run_id=None,
                warning_count=sum(1 for i in items if i.grade != BLOCKING),
                acknowledged_warnings=sorted(ack),
            )
        # 적자 이월 원장. 잠금과 같은 트랜잭션이어야 한다 —
        # 잠갔는데 이월이 안 쌓이면 다음 달 분배가 틀린다.
        row = db.execute(text("""
            SELECT net_profit, carryover_offset FROM monthly_settlement WHERE id = :id
        """), {"id": str(settlement_id)}).mappings().one()
        carryover.apply_month_lock(
            db, org_id=org_id, settlement_id=settlement_id, year_month=year_month,
            net_profit=int(row["net_profit"]), carryover_offset=int(row["carryover_offset"]),
            actor_user_id=actor_user_id,
        )

    result = _transition(
        db, org_id=org_id, year_month=year_month, to_state="LOCKED",
        actor_user_id=actor_user_id, reason="월 잠금", request_id=request_id,
        extra_sql=", locked_at = now(), locked_by = :actor4",
        extra_params={"actor4": str(actor_user_id)},
        on_transition=_on_transition,
    )
    # 잠금과 동시에 그 달의 이익을 FINAL 로 확정한다 (Phase 1).
    # 잠금 트랜잭션 밖이다 — 주문이 많으면 오래 걸리고, 실패해도 잠금 자체는
    # 유효하다(FINAL 승격은 재실행할 수 있다).
    from app.services.profit_status import finalize_month
    finalize_month(db, org_id=org_id, year_month=year_month, actor_user_id=actor_user_id)
    return result


def reopen_limit_of(db: Session, *, org_id: UUID, year_month: str) -> dict[str, int]:
    """이 달의 재개 한도와 사용 횟수. 화면이 미리 보여줄 수 있어야 한다."""
    row = _load_row(db, org_id, year_month)
    org_limit = db.execute(text("""
        SELECT settlement_reopen_limit FROM organization WHERE id = :org
    """), {"org": str(org_id)}).scalar()
    limit = (
        row.reopen_limit_override if row.reopen_limit_override is not None
        else (org_limit or 3)
    )
    return {"reopen_count": int(row.reopen_count), "limit": int(limit)}


def request_reopen_approval(
    db: Session, *, org_id: UUID, year_month: str, actor_user_id: UUID,
    reason: str, submit: bool = True, request_id: str | None = None,
) -> dict[str, Any]:
    """월 재오픈 승인 요청을 만든다 (Phase 29).

    직접 `unlock` 을 부르지 않는다. 승인을 거쳐 `MonthReopenAdapter.apply_changes`
    가 부른다 — 그래야 누가 요청하고 누가 승인했는지가 남는다.

    한도는 여기서도 미리 본다. 한도를 넘긴 달의 요청을 만들어 두면 승인자가
    결재한 뒤 적용 단계에서야 실패한다.
    """
    quota = reopen_limit_of(db, org_id=org_id, year_month=year_month)
    if quota["reopen_count"] >= quota["limit"]:
        raise ConflictError(
            f"재개 한도({quota['limit']}회)를 초과했습니다. 요청을 만들 수 없습니다.",
            detail=quota,
        )

    settlement_id = close_snapshot.require_settlement_row(
        db, org_id=org_id, year_month=year_month
    )
    from app.services import approval as approval_svc

    # 귀속월을 명시한다. 비워 두면 `create_request` 가 NULL 로 저장하고,
    # 승인 목록의 기간 필터에서 이 요청이 사라진다 —
    # 8월에 낸 7월 재오픈 요청은 **7월 건**으로 보여야 한다.
    return approval_svc.create_request(
        db, org_id=org_id, actor_user_id=actor_user_id,
        target_type="monthly_close", target_id=settlement_id,
        request_type="MONTH_REOPEN",
        proposed_changes={"reopen_to_state": "OPEN", "reopen_reason": reason},
        reason=reason, submit=submit, request_id=request_id,
        effective_from=date(int(year_month[:4]), int(year_month[5:7]), 1),
    )


def unlock(
    db: Session, *, org_id: UUID, year_month: str, actor_user_id: UUID,
    reason: str, approved_by: UUID | None = None, request_id: str | None = None,
    _approved_via_request: UUID | None = None,
) -> dict[str, Any]:
    """잠금 해제 = 재개. 사유 필수. reopen 한도(D-285)를 확인한다.

    snapshot 을 지우지 않는다
    -----------------------
    기존 확정 숫자는 `superseded` 로 내려가고 그대로 남는다. 그래야
    "재오픈 전에 뭐라고 확정했었나" 를 나중에 답할 수 있다. 재확정하면 세대 2가
    생기고, 두 세대를 나란히 비교할 수 있다.

    `_approved_via_request` 는 승인 어댑터가 넘기는 내부 인자다. 라우터에 노출되지
    않아 클라이언트가 설정할 수 없다 — 승인 없이 재오픈하는 경로를 열지 않는다.
    """
    if not reason or not reason.strip():
        raise ValidationError("잠금 해제에는 사유가 필요합니다.")

    quota = reopen_limit_of(db, org_id=org_id, year_month=year_month)
    if quota["reopen_count"] >= quota["limit"]:
        # 여기서는 잠금을 잡지 않았다(_load_row 기본값). 되돌릴 것이 없으므로
        # rollback 하지 않는다 — 상위 트랜잭션의 다른 변경을 건드리면 안 된다.
        raise ConflictError(
            f"재개 한도({quota['limit']}회)를 초과했습니다. 관리자에게 문의하세요.",
            detail=quota,
        )

    def _on_transition(settlement_id: UUID) -> None:
        superseded_id = close_snapshot.supersede(db, org_id=org_id, year_month=year_month)
        # 확정 배분도 함께 무효화한다. snapshot 만 내리고 배분을 두면
        # **없어진 순이익을 근거로 한 배분**이 유효한 것처럼 남는다.
        # 삭제하지 않는다 — 이미 지급했을 수 있고 그 근거가 사라지면 안 된다.
        from app.services import distribution

        superseded_run_id = distribution.supersede_run(
            db, org_id=org_id, year_month=year_month, actor_user_id=actor_user_id,
        )
        # 현재 snapshot 포인터를 비운다. 재확정 전까지 이 달에는 확정 숫자가 없다.
        db.execute(text("""
            UPDATE monthly_settlement SET close_snapshot_id = NULL WHERE id = :id
        """), {"id": str(settlement_id)})
        db.execute(text("""
            INSERT INTO settlement_reopen_audit (
              monthly_settlement_id, reopened_by, reason, previous_status,
              reopen_count_after, snapshot_before_json
            ) VALUES (
              :sid, :uid, :reason, 'LOCKED', :n, CAST(:snap AS JSONB)
            )
        """), {
            "sid": str(settlement_id), "uid": str(actor_user_id), "reason": reason,
            "n": quota["reopen_count"] + 1,
            "snap": json.dumps({
                "superseded_snapshot_id": str(superseded_id) if superseded_id else None,
                "superseded_distribution_run_id": (
                    str(superseded_run_id) if superseded_run_id else None
                ),
                "generation": quota["reopen_count"] + 1,
                "approval_request_id": (
                    str(_approved_via_request) if _approved_via_request else None
                ),
                "approved_by": str(approved_by) if approved_by else None,
            }, ensure_ascii=False),
        })

    return _transition(
        db, org_id=org_id, year_month=year_month, to_state="OPEN",
        actor_user_id=actor_user_id, reason=reason, request_id=request_id,
        extra_sql=(", reopen_count = reopen_count + 1, last_reopened_at = now(),"
                   " last_reopened_by = :actor5, last_reopen_reason = :reason5,"
                   " locked_at = NULL, locked_by = NULL, confirmed_at = NULL"),
        extra_params={"actor5": str(actor_user_id), "reason5": reason},
        on_transition=_on_transition,
    )
