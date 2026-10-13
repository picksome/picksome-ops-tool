"""확정 수익배분 (Phase 31).

시뮬레이션과 확정을 분리한다
-------------------------
```
services/settlement.calculate_monthly_settlement()  ← 시뮬레이션. 언제든 다시 돈다
services/distribution.create_run()                  ← 확정. snapshot 을 근거로 한 번
```

확정 배분은 **월 확정 snapshot 없이는 만들 수 없다.** `distribution_run.close_snapshot_id`
가 NOT NULL 이라 DB 가 그걸 강제한다. 근거 없는 배분을 만들면 나중에
"이 금액이 어느 순이익에서 나왔나" 를 답할 수 없다.

배분 대상은 snapshot 의 `distributable_profit` 이다
------------------------------------------------
live 재집계가 아니다. 확정한 순이익을 나눠야 하고, 확정 후 판매가 하나 더
들어왔다고 배분액이 달라지면 그건 확정이 아니다.

과거 배분은 바뀌지 않는다
----------------------
규칙이 바뀌어도 이미 만들어진 `distribution_result` 를 다시 계산하지 않는다.
월을 재오픈해 재확정하면 **새 run** 이 생기고 이전 run 은 superseded 로 내려간다.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.money import to_krw
from app.services import close_snapshot
from app.services.settlement import month_range


def get_current_run(db: Session, *, org_id: UUID, year_month: str) -> dict[str, Any] | None:
    row = db.execute(text("""
        SELECT * FROM distribution_run
         WHERE org_id = :org AND year_month = :ym
           AND superseded_at IS NULL AND status <> 'CANCELLED'
    """), {"org": str(org_id), "ym": year_month}).mappings().first()
    if row is None:
        return None
    out = dict(row)
    out["results"] = list_results(db, run_id=UUID(str(row["id"])))
    return out


def list_results(db: Session, *, run_id: UUID) -> list[dict[str, Any]]:
    rows = db.execute(text("""
        SELECT id, partner_id, partner_name_snapshot, participant_type,
               distribution_kind, calculation_base, ratio_used, priority_order,
               amount, note
          FROM distribution_result
         WHERE run_id = :run
         ORDER BY CASE distribution_kind WHEN 'PRIORITY' THEN 0 ELSE 1 END,
                  priority_order NULLS LAST, partner_name_snapshot
    """), {"run": str(run_id)}).mappings().all()
    return [dict(r) for r in rows]


def list_runs(db: Session, *, org_id: UUID, year_month: str) -> list[dict[str, Any]]:
    """이력. 재오픈으로 배분이 어떻게 달라졌는지 비교할 수 있어야 한다."""
    rows = db.execute(text("""
        SELECT id, year_month, snapshot_generation, distributable_base,
               total_distributed, status, created_at, approved_at,
               superseded_at, applied_rule_versions_json
          FROM distribution_run
         WHERE org_id = :org AND year_month = :ym
         ORDER BY snapshot_generation DESC, created_at DESC
    """), {"org": str(org_id), "ym": year_month}).mappings().all()
    return [dict(r) for r in rows]


def preview(db: Session, *, org_id: UUID, year_month: str) -> dict[str, Any]:
    """확정하면 누가 얼마를 받는지 미리 본다. **저장하지 않는다.**

    확정 snapshot 이 있으면 그 금액을, 없으면 현재 집계 금액을 기준으로 계산한다.
    후자는 잠정치이므로 `is_provisional` 로 표시한다 — 화면이 "확정 배분액" 이라고
    부르면 안 된다.
    """
    snap = close_snapshot.get_current(db, org_id=org_id, year_month=year_month)
    if snap is not None:
        base = int(snap["distributable_profit"])
        provisional = False
    else:
        figures = close_snapshot.compute_figures(db, org_id=org_id, year_month=year_month)
        base = int(figures["distributable_profit"])
        provisional = True

    rows, rule_versions, loss_policy = _compute(db, org_id=org_id, year_month=year_month,
                                                distributable=base)
    return {
        "year_month": year_month,
        "is_provisional": provisional,
        "distributable_base": base,
        "total_distributed": sum(r["amount"] for r in rows),
        "applied_rule_versions": rule_versions,
        "loss_policy": loss_policy,
        "results": rows,
    }


def _compute(
    db: Session, *, org_id: UUID, year_month: str, distributable: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
    """배분 계산.

    `services/settlement.calculate_monthly_settlement` 과 **같은 규칙**이다
    (우선 분배 → 잔액 비율 분배 → 1원 단위 잔수는 마지막 파트너에게).
    거기서 계산 로직을 재사용하지 않는 이유: 그 함수는 매출·비용 집계까지
    같이 하고 결과를 `MonthlySettlementResult` 로 돌려준다. 확정 배분은
    **이미 확정된 금액**을 입력으로 받아야 하므로 집계 부분이 필요 없다.
    """
    _, period_end = month_range(year_month)
    p = {"org": str(org_id), "asof": period_end, "ym": year_month}

    policy = db.execute(text("""
        SELECT id, version, priority_distribution_json, loss_policy
          FROM settlement_policy
         WHERE org_id = :org AND is_active = TRUE AND deleted_at IS NULL
           AND effective_from <= :asof AND (effective_to IS NULL OR effective_to >= :asof)
         ORDER BY effective_from DESC LIMIT 1
    """), p).mappings().first()

    rule_versions: dict[str, Any] = {}
    if policy:
        rule_versions["settlement_policy"] = policy["version"]

    results: list[dict[str, Any]] = []
    priority_total = 0

    entries = list(policy["priority_distribution_json"] or []) if policy else []
    for order, entry in enumerate(entries, start=1):
        amount = min(int(entry.get("fixed_amount", 0)),
                     max(distributable - priority_total, 0))
        if amount <= 0:
            continue
        pid = UUID(str(entry["partner_id"]))
        results.append({
            "partner_id": pid,
            "partner_name_snapshot": _partner_name(db, org_id, pid),
            "participant_type": "PARTNER",
            "distribution_kind": "PRIORITY",
            "calculation_base": distributable,
            "ratio_used": None,
            "priority_order": order,
            "amount": amount,
            "note": "우선 분배 (정액)",
        })
        priority_total += amount

    remaining = distributable - priority_total

    share_rows = db.execute(text("""
        SELECT partner_id, share_ratio, version FROM partner_share_policy
         WHERE org_id = :org AND is_active = TRUE AND deleted_at IS NULL
           AND effective_from <= :asof AND (effective_to IS NULL OR effective_to >= :asof)
           AND (applies_year_month IS NULL OR applies_year_month = :ym)
         ORDER BY (applies_year_month IS NOT NULL) DESC, effective_from DESC
    """), p).mappings().all()

    # 월 override 가 default 보다 먼저 온다 → 같은 partner 의 첫 hit 만 채택.
    seen: dict[UUID, Decimal] = {}
    versions: list[int] = []
    for r in share_rows:
        pid = UUID(str(r["partner_id"]))
        if pid not in seen:
            seen[pid] = Decimal(str(r["share_ratio"]))
            if r["version"] is not None:
                versions.append(int(r["version"]))
    if versions:
        rule_versions["partner_share_policy"] = sorted(set(versions))

    total_ratio = sum(seen.values(), Decimal(0))
    if seen and (total_ratio - Decimal(1)).copy_abs() > Decimal("0.0005"):
        raise ConflictError(
            f"수익배분 비율 합이 {total_ratio} 입니다(1.0 이어야 합니다).",
            detail={"sum": str(total_ratio)},
        )

    items = list(seen.items())
    accumulated = 0
    for idx, (pid, ratio) in enumerate(items):
        # 1원 단위 잔수는 마지막 파트너에게 귀속 — Σ amount = remaining 보장.
        amount = (remaining - accumulated) if idx == len(items) - 1 else to_krw(
            Decimal(remaining) * ratio
        )
        if idx != len(items) - 1:
            accumulated += amount
        results.append({
            "partner_id": pid,
            "partner_name_snapshot": _partner_name(db, org_id, pid),
            "participant_type": "PARTNER",
            "distribution_kind": "RATIO",
            "calculation_base": remaining,
            "ratio_used": ratio,
            "priority_order": None,
            "amount": max(amount, 0),
            "note": None,
        })

    return results, rule_versions, (policy["loss_policy"] if policy else None)


def _partner_name(db: Session, org_id: UUID, partner_id: UUID) -> str:
    name = db.execute(text("""
        SELECT name FROM partner WHERE id = :id AND org_id = :org
    """), {"id": str(partner_id), "org": str(org_id)}).scalar()
    if name is None:
        # 정책이 삭제된 파트너를 가리키고 있다. 조용히 넘기면 배분이 이름 없이 남는다.
        raise ValidationError(
            "배분 정책이 존재하지 않는 파트너를 가리킵니다.",
            detail={"partner_id": str(partner_id)},
        )
    return str(name)


def create_run(
    db: Session, *, org_id: UUID, year_month: str, actor_user_id: UUID,
    request_id: str | None = None, _commit: bool = True,
) -> dict[str, Any]:
    """확정 배분 생성. **월 확정 snapshot 이 있어야 한다.**

    확정 시점의 순이익을 근거로 만들어지고, 이후 정책이 바뀌어도 다시 계산되지 않는다.
    """
    snap = close_snapshot.get_current(db, org_id=org_id, year_month=year_month)
    if snap is None:
        raise ConflictError(
            f"{year_month} 이 아직 확정되지 않았습니다. "
            "월을 확정하면 그 순이익을 기준으로 배분을 만들 수 있습니다.",
        )

    existing = db.execute(text("""
        SELECT id FROM distribution_run
         WHERE org_id = :org AND year_month = :ym
           AND superseded_at IS NULL AND status <> 'CANCELLED'
    """), {"org": str(org_id), "ym": year_month}).first()
    if existing:
        raise ConflictError(
            f"{year_month} 의 확정 배분이 이미 있습니다.",
            detail={"distribution_run_id": str(existing.id)},
        )

    base = int(snap["distributable_profit"])
    results, rule_versions, loss_policy = _compute(
        db, org_id=org_id, year_month=year_month, distributable=base
    )
    total = sum(r["amount"] for r in results)

    run_id = db.execute(text("""
        INSERT INTO distribution_run (
          org_id, year_month, monthly_settlement_id, close_snapshot_id,
          snapshot_generation, distributable_base, carryover_applied,
          total_distributed, status, applied_rule_versions_json, loss_policy, created_by
        ) VALUES (
          :org, :ym, :msid, :snap, :gen, :base, :carry,
          :total, 'CALCULATED', CAST(:rules AS JSONB), :loss, :actor
        )
        RETURNING id
    """), {
        "org": str(org_id), "ym": year_month,
        "msid": str(snap["monthly_settlement_id"]), "snap": str(snap["id"]),
        "gen": snap["generation"], "base": base,
        "carry": int(snap["carryover_offset"] or 0), "total": total,
        "rules": json.dumps(rule_versions, ensure_ascii=False, default=str),
        "loss": loss_policy, "actor": str(actor_user_id),
    }).scalar_one()

    for r in results:
        db.execute(text("""
            INSERT INTO distribution_result (
              run_id, org_id, partner_id, partner_name_snapshot, participant_type,
              distribution_kind, calculation_base, ratio_used, priority_order, amount, note
            ) VALUES (
              :run, :org, :pid, :pname, :ptype,
              :kind, :cbase, :ratio, :porder, :amt, :note
            )
        """), {
            "run": str(run_id), "org": str(org_id), "pid": str(r["partner_id"]),
            "pname": r["partner_name_snapshot"], "ptype": r["participant_type"],
            "kind": r["distribution_kind"], "cbase": r["calculation_base"],
            "ratio": str(r["ratio_used"]) if r["ratio_used"] is not None else None,
            "porder": r["priority_order"], "amt": r["amount"], "note": r["note"],
        })

    record_audit(
        db, org_id=org_id, domain_table="distribution_run", domain_row_id=run_id,
        action="CREATE", actor_user_id=actor_user_id,
        after={"year_month": year_month, "distributable_base": base,
               "total_distributed": total, "snapshot_generation": snap["generation"]},
        reason="월 확정 순이익 기준 수익배분 확정", request_id=request_id,
    )
    if _commit:
        db.commit()
    return get_current_run(db, org_id=org_id, year_month=year_month)


def supersede_run(
    db: Session, *, org_id: UUID, year_month: str, actor_user_id: UUID | None = None,
) -> UUID | None:
    """현재 확정 배분을 historical 로 내린다. **삭제하지 않는다.**

    월 재오픈 시 호출된다. 과거 배분 결과는 그대로 남아야 —
    파트너에게 이미 지급했을 수 있고, 그 근거가 사라지면 안 된다.
    """
    row = db.execute(text("""
        SELECT id FROM distribution_run
         WHERE org_id = :org AND year_month = :ym
           AND superseded_at IS NULL AND status <> 'CANCELLED'
         FOR UPDATE
    """), {"org": str(org_id), "ym": year_month}).first()
    if row is None:
        return None
    db.execute(text("""
        UPDATE distribution_run
           SET superseded_at = now(), superseded_by_id = :self, status = 'SUPERSEDED'
         WHERE id = :id
    """), {"id": str(row.id), "self": str(row.id)})
    if actor_user_id:
        record_audit(
            db, org_id=org_id, domain_table="distribution_run",
            domain_row_id=UUID(str(row.id)), action="STATUS_CHANGE",
            actor_user_id=actor_user_id, after={"status": "SUPERSEDED"},
            reason=f"{year_month} 재오픈으로 확정 배분 무효화",
        )
    return UUID(str(row.id))


def approve_run(
    db: Session, *, org_id: UUID, run_id: UUID, actor_user_id: UUID,
    request_id: str | None = None,
) -> dict[str, Any]:
    """배분 확정 승인. 이후 지급 배치를 만들 수 있다."""
    row = db.execute(text("""
        SELECT id, status, year_month FROM distribution_run
         WHERE id = :id AND org_id = :org FOR UPDATE
    """), {"id": str(run_id), "org": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("배분 결과를 찾을 수 없습니다.")
    if row["status"] != "CALCULATED":
        raise ConflictError(
            f"승인할 수 없는 상태입니다(현재 {row['status']}).",
            detail={"status": row["status"]},
        )

    db.execute(text("""
        UPDATE distribution_run
           SET status = 'APPROVED', approved_at = now(), approved_by = :actor
         WHERE id = :id AND status = 'CALCULATED'
    """), {"id": str(run_id), "actor": str(actor_user_id)})
    record_audit(
        db, org_id=org_id, domain_table="distribution_run", domain_row_id=run_id,
        action="STATUS_CHANGE", actor_user_id=actor_user_id,
        before={"status": "CALCULATED"}, after={"status": "APPROVED"},
        reason="수익배분 확정 승인", request_id=request_id,
    )
    db.commit()
    return get_current_run(db, org_id=org_id, year_month=row["year_month"])
