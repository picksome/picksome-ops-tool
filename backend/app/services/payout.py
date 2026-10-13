"""지급 장부 (Phase 32~34).

계산된 금액과 실제로 보낸 돈은 다르다
--------------------------------
```
distribution_result   "3월 순이익 중 파트너B 몫은 4,200,000 원이다"   ← 계산
payout_item           "그 4,200,000 원을 지급해야 한다"             ← 채무
payout_payment        "6/15 에 2,000,000 원 보냈다"                 ← 현금
```

지급 장부는 **손익을 만들지 않는다.** 회계 효과가 항목마다 다르기 때문에
`accounting_effect` 를 행에 박아 두고 DB CHECK 로 유형과 짝지어 고정했다.

| 유형 | 손익 | 지급 시 |
|---|---|---|
| `EMPLOYEE_SALARY` | 이미 비용 (LABOR) | 현금 사실만 기록 |
| `PARTNER_DISTRIBUTION` | **비용 아님** — 순이익 후 이익 처분 | 비용으로 만들지 않는다 |
| `EXPENSE_REIMBURSEMENT` | 이미 비용 (원래 그 비용) | **새 비용을 만들지 않는다** |

PAID 를 만드는 방법은 하나뿐이다
-----------------------------
`paid_amount` 는 DB 트리거가 `payout_payment` 합계에서 계산한다. 이 모듈은
그 컬럼에 쓰지 않는다. `status='PAID'` 는 `paid_amount = due_amount` 일 때만
CHECK 를 통과하므로 **지급 기록 없이 PAID 를 만드는 경로가 없다.**

개인카드 상환
-----------
비용 자체의 상태를 바꾸지 않는다. 상환 상태는 지급 기록에서 **계산**한다 —
`expenses` 에 `reimbursement_status` 컬럼을 만들면 지급 기록과 두 곳에 상태가
생기고 반드시 어긋난다.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import ConflictError, NotFoundError, ValidationError

# 유형 → 회계 효과. DB CHECK 와 같은 내용을 코드에도 둔다 —
# 서비스가 잘못된 조합을 만들면 DB 에 닿기 전에 걸린다.
ACCOUNTING_EFFECT: dict[str, str] = {
    "PARTNER_DISTRIBUTION": "PROFIT_APPROPRIATION",
    "INVESTOR_DISTRIBUTION": "PROFIT_APPROPRIATION",
    "EMPLOYEE_SALARY": "ALREADY_EXPENSED",
    "EXPENSE_REIMBURSEMENT": "ALREADY_EXPENSED",
    "OTHER": "NONE",
}

_OPEN_STATUSES = ("CALCULATED", "APPROVED", "PARTIALLY_PAID")


def _next_business_key(db: Session, org_id: UUID) -> str:
    n = db.execute(text("""
        SELECT COUNT(*) + 1 FROM payout_batch WHERE org_id = :org
    """), {"org": str(org_id)}).scalar() or 1
    return f"PAY-{int(n):06d}"


# ──────────────────────────────────────────────────────────────────────────
# 조회
# ──────────────────────────────────────────────────────────────────────────

def get_batch(db: Session, *, org_id: UUID, batch_id: UUID) -> dict[str, Any]:
    row = db.execute(text("""
        SELECT id, business_key, title, period_year_month, status, memo,
               source_distribution_run_id, created_at, created_by,
               approved_at, approved_by, cancelled_at
          FROM payout_batch
         WHERE id = :id AND org_id = :org AND deleted_at IS NULL
    """), {"id": str(batch_id), "org": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("지급 배치를 찾을 수 없습니다.")

    out = dict(row)
    out["items"] = list_items(db, org_id=org_id, batch_id=batch_id)
    out["due_total"] = sum(i["due_amount"] for i in out["items"])
    out["paid_total"] = sum(i["paid_amount"] for i in out["items"])
    out["outstanding_total"] = out["due_total"] - out["paid_total"]
    return out


def list_batches(
    db: Session, *, org_id: UUID, status: str | None = None,
    year_month: str | None = None, limit: int = 50, offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where = ["b.org_id = :org", "b.deleted_at IS NULL"]
    p: dict[str, Any] = {"org": str(org_id), "lim": limit, "off": offset}
    if status:
        where.append("b.status = :st")
        p["st"] = status
    if year_month:
        where.append("b.period_year_month = :ym")
        p["ym"] = year_month
    clause = " AND ".join(where)

    total = db.execute(
        text(f"SELECT COUNT(*) FROM payout_batch b WHERE {clause}"), p
    ).scalar() or 0
    rows = db.execute(text(f"""
        SELECT b.id, b.business_key, b.title, b.period_year_month, b.status,
               b.source_distribution_run_id, b.created_at, b.approved_at,
               COALESCE(SUM(i.due_amount)  FILTER (WHERE i.deleted_at IS NULL), 0) AS due_total,
               COALESCE(SUM(i.paid_amount) FILTER (WHERE i.deleted_at IS NULL), 0) AS paid_total,
               COUNT(i.id) FILTER (WHERE i.deleted_at IS NULL) AS item_count
          FROM payout_batch b
          LEFT JOIN payout_item i ON i.batch_id = b.id
         WHERE {clause}
         GROUP BY b.id
         ORDER BY b.created_at DESC
         LIMIT :lim OFFSET :off
    """), p).mappings().all()
    return [dict(r) for r in rows], int(total)


def list_items(db: Session, *, org_id: UUID, batch_id: UUID) -> list[dict[str, Any]]:
    rows = db.execute(text("""
        SELECT id, item_type, accounting_effect, payee_partner_id, payee_name_snapshot,
               source_distribution_result_id, source_expense_id,
               due_amount, paid_amount, status, due_date, memo
          FROM payout_item
         WHERE batch_id = :b AND org_id = :org AND deleted_at IS NULL
         ORDER BY item_type, payee_name_snapshot
    """), {"b": str(batch_id), "org": str(org_id)}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["outstanding_amount"] = int(d["due_amount"]) - int(d["paid_amount"])
        out.append(d)
    return out


def list_payments(db: Session, *, org_id: UUID, item_id: UUID) -> list[dict[str, Any]]:
    rows = db.execute(text("""
        SELECT id, payment_date, amount, payment_reference, memo,
               recorded_by, recorded_at, evidence_attachment_id
          FROM payout_payment
         WHERE payout_item_id = :i AND org_id = :org AND deleted_at IS NULL
         ORDER BY payment_date, recorded_at
    """), {"i": str(item_id), "org": str(org_id)}).mappings().all()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────────────────────────────────
# 배치 생성
# ──────────────────────────────────────────────────────────────────────────

def create_batch(
    db: Session, *, org_id: UUID, actor_user_id: UUID, title: str,
    period_year_month: str | None = None, memo: str | None = None,
    source_distribution_run_id: UUID | None = None, _commit: bool = True,
) -> UUID:
    batch_id = db.execute(text("""
        INSERT INTO payout_batch (
          org_id, business_key, title, period_year_month, memo,
          source_distribution_run_id, status, created_by, updated_by
        ) VALUES (:org, :bk, :title, :ym, :memo, :src, 'DRAFT', :actor, :actor)
        RETURNING id
    """), {
        "org": str(org_id), "bk": _next_business_key(db, org_id), "title": title,
        "ym": period_year_month, "memo": memo,
        "src": str(source_distribution_run_id) if source_distribution_run_id else None,
        "actor": str(actor_user_id),
    }).scalar_one()
    record_audit(
        db, org_id=org_id, domain_table="payout_batch", domain_row_id=batch_id,
        action="CREATE", actor_user_id=actor_user_id,
        after={"title": title, "period_year_month": period_year_month},
    )
    if _commit:
        db.commit()
    return UUID(str(batch_id))


def create_batch_from_distribution(
    db: Session, *, org_id: UUID, year_month: str, actor_user_id: UUID,
    due_date: date | None = None, request_id: str | None = None,
) -> dict[str, Any]:
    """확정 배분 → 지급 배치.

    승인된 배분에서만 만든다. `CALCULATED` 상태에서 지급 배치를 만들면
    아직 검토되지 않은 금액을 지급 대상으로 올리게 된다.
    """
    from app.services import distribution

    run = distribution.get_current_run(db, org_id=org_id, year_month=year_month)
    if run is None:
        raise ConflictError(
            f"{year_month} 의 확정 수익배분이 없습니다. 배분을 먼저 확정하세요."
        )
    if run["status"] != "APPROVED":
        raise ConflictError(
            f"승인된 배분에서만 지급 배치를 만들 수 있습니다(현재 {run['status']}).",
            detail={"status": run["status"]},
        )

    existing = db.execute(text("""
        SELECT id FROM payout_batch
         WHERE source_distribution_run_id = :run
           AND status <> 'CANCELLED' AND deleted_at IS NULL
    """), {"run": str(run["id"])}).first()
    if existing:
        raise ConflictError(
            "이 배분에서 만든 지급 배치가 이미 있습니다.",
            detail={"payout_batch_id": str(existing.id)},
        )

    batch_id = create_batch(
        db, org_id=org_id, actor_user_id=actor_user_id,
        title=f"{year_month} 파트너 수익배분",
        period_year_month=year_month,
        source_distribution_run_id=UUID(str(run["id"])),
        _commit=False,
    )

    # 파트너별로 우선분배·비율분배를 **합쳐서** 한 항목으로 만든다.
    # 실제로 송금하는 것은 한 번인데 항목이 둘이면 부분지급 계산이 어긋난다.
    merged: dict[UUID, dict[str, Any]] = {}
    for r in run["results"]:
        if int(r["amount"]) <= 0:
            continue
        pid = UUID(str(r["partner_id"]))
        entry = merged.setdefault(pid, {
            "name": r["partner_name_snapshot"], "amount": 0,
            "result_id": r["id"], "kinds": [],
        })
        entry["amount"] += int(r["amount"])
        entry["kinds"].append(r["distribution_kind"])

    created = 0
    for pid, entry in merged.items():
        add_item(
            db, org_id=org_id, actor_user_id=actor_user_id, batch_id=batch_id,
            item_type="PARTNER_DISTRIBUTION", payee_name=entry["name"],
            payee_partner_id=pid, due_amount=entry["amount"],
            source_distribution_result_id=(
                UUID(str(entry["result_id"])) if len(entry["kinds"]) == 1 else None
            ),
            due_date=due_date,
            memo=" + ".join(entry["kinds"]) if len(entry["kinds"]) > 1 else None,
            _commit=False,
        )
        created += 1

    if created == 0:
        raise ValidationError(
            f"{year_month} 의 배분 금액이 모두 0원입니다. 지급할 항목이 없습니다."
        )

    record_audit(
        db, org_id=org_id, domain_table="payout_batch", domain_row_id=batch_id,
        action="CREATE", actor_user_id=actor_user_id,
        after={"source_distribution_run_id": str(run["id"]), "item_count": created},
        reason=f"{year_month} 확정 배분에서 지급 배치 생성", request_id=request_id,
    )
    db.commit()
    return get_batch(db, org_id=org_id, batch_id=batch_id)


def add_item(
    db: Session, *, org_id: UUID, actor_user_id: UUID, batch_id: UUID,
    item_type: str, payee_name: str, due_amount: int,
    payee_partner_id: UUID | None = None,
    source_distribution_result_id: UUID | None = None,
    source_expense_id: UUID | None = None,
    due_date: date | None = None, memo: str | None = None,
    _commit: bool = True,
) -> UUID:
    if item_type not in ACCOUNTING_EFFECT:
        raise ValidationError(
            f"알 수 없는 지급 유형입니다: {item_type}",
            detail={"allowed": sorted(ACCOUNTING_EFFECT)},
        )
    if due_amount <= 0:
        raise ValidationError("지급 예정 금액은 0보다 커야 합니다.")
    if item_type == "EXPENSE_REIMBURSEMENT" and source_expense_id is None:
        raise ValidationError("상환 항목은 원본 비용을 지정해야 합니다.")

    batch = db.execute(text("""
        SELECT status FROM payout_batch
         WHERE id = :b AND org_id = :org AND deleted_at IS NULL
    """), {"b": str(batch_id), "org": str(org_id)}).mappings().first()
    if batch is None:
        raise NotFoundError("지급 배치를 찾을 수 없습니다.")
    if batch["status"] in ("PAID", "CANCELLED"):
        raise ConflictError(
            f"{batch['status']} 상태의 배치에는 항목을 추가할 수 없습니다."
        )

    item_id = db.execute(text("""
        INSERT INTO payout_item (
          org_id, batch_id, item_type, accounting_effect,
          payee_partner_id, payee_name_snapshot,
          source_distribution_result_id, source_expense_id,
          due_amount, status, due_date, memo, created_by, updated_by
        ) VALUES (
          :org, :b, :type, :effect, :pid, :pname, :src_dist, :src_exp,
          :due, 'CALCULATED', :due_date, :memo, :actor, :actor
        )
        RETURNING id
    """), {
        "org": str(org_id), "b": str(batch_id), "type": item_type,
        "effect": ACCOUNTING_EFFECT[item_type],
        "pid": str(payee_partner_id) if payee_partner_id else None,
        "pname": payee_name,
        "src_dist": str(source_distribution_result_id) if source_distribution_result_id else None,
        "src_exp": str(source_expense_id) if source_expense_id else None,
        "due": int(due_amount), "due_date": due_date, "memo": memo,
        "actor": str(actor_user_id),
    }).scalar_one()

    # 항목 추가는 "이 사람에게 이 금액을 준다" 를 만드는 행위다. 배치 생성만
    # 기록하고 여기를 빠뜨리면 금액이 언제 누구에 의해 들어왔는지 설명할 수 없다.
    record_audit(
        db, org_id=org_id, domain_table="payout_item",
        domain_row_id=UUID(str(item_id)), action="CREATE",
        actor_user_id=actor_user_id,
        after={
            "item_type": item_type, "payee_name": payee_name,
            "due_amount": int(due_amount),
            "due_date": due_date.isoformat() if due_date else None,
        },
    )

    if _commit:
        db.commit()
    return UUID(str(item_id))


def approve_batch(
    db: Session, *, org_id: UUID, batch_id: UUID, actor_user_id: UUID,
    request_id: str | None = None,
) -> dict[str, Any]:
    """지급 승인. 이후 지급 기록을 남길 수 있다."""
    row = db.execute(text("""
        SELECT status FROM payout_batch
         WHERE id = :b AND org_id = :org AND deleted_at IS NULL FOR UPDATE
    """), {"b": str(batch_id), "org": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("지급 배치를 찾을 수 없습니다.")
    if row["status"] != "DRAFT":
        raise ConflictError(f"승인할 수 없는 상태입니다(현재 {row['status']}).")

    n = db.execute(text("""
        SELECT COUNT(*) FROM payout_item
         WHERE batch_id = :b AND deleted_at IS NULL AND status <> 'CANCELLED'
    """), {"b": str(batch_id)}).scalar() or 0
    if n == 0:
        raise ValidationError("항목이 없는 배치는 승인할 수 없습니다.")

    db.execute(text("""
        UPDATE payout_batch
           SET status = 'APPROVED', approved_at = now(), approved_by = :actor,
               updated_by = :actor
         WHERE id = :b AND status = 'DRAFT'
    """), {"b": str(batch_id), "actor": str(actor_user_id)})
    db.execute(text("""
        UPDATE payout_item SET status = 'APPROVED', updated_by = :actor
         WHERE batch_id = :b AND status = 'CALCULATED' AND deleted_at IS NULL
    """), {"b": str(batch_id), "actor": str(actor_user_id)})

    record_audit(
        db, org_id=org_id, domain_table="payout_batch", domain_row_id=batch_id,
        action="STATUS_CHANGE", actor_user_id=actor_user_id,
        before={"status": "DRAFT"}, after={"status": "APPROVED"},
        reason="지급 승인", request_id=request_id,
    )
    db.commit()
    return get_batch(db, org_id=org_id, batch_id=batch_id)


# ──────────────────────────────────────────────────────────────────────────
# 지급 기록
# ──────────────────────────────────────────────────────────────────────────

def record_payment(
    db: Session, *, org_id: UUID, actor_user_id: UUID, item_id: UUID,
    payment_date: date, amount: int, payment_reference: str | None = None,
    memo: str | None = None, evidence_attachment_id: UUID | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """실제 지급 1건 기록.

    `paid_amount` 와 항목 상태는 **DB 트리거가 갱신한다.** 여기서 직접 쓰지 않는다 —
    그러면 지급 기록 없이 상태만 바꾸는 경로가 생긴다.

    과지급은 DB CHECK 가 막지만, 여기서 먼저 확인해 사용자에게 얼마가 남았는지
    알려 준다. CHECK 위반 메시지만으로는 무엇을 해야 할지 알 수 없다.
    """
    if amount <= 0:
        raise ValidationError("지급 금액은 0보다 커야 합니다.")

    item = db.execute(text("""
        SELECT i.id, i.due_amount, i.paid_amount, i.status, i.payee_name_snapshot,
               b.status AS batch_status
          FROM payout_item i
          JOIN payout_batch b ON b.id = i.batch_id
         WHERE i.id = :i AND i.org_id = :org AND i.deleted_at IS NULL
         FOR UPDATE OF i
    """), {"i": str(item_id), "org": str(org_id)}).mappings().first()
    if item is None:
        raise NotFoundError("지급 항목을 찾을 수 없습니다.")
    if item["batch_status"] == "DRAFT":
        raise ConflictError(
            "승인되지 않은 배치에는 지급을 기록할 수 없습니다. 지급 승인을 먼저 받으세요."
        )
    if item["status"] in ("CANCELLED", "CARRIED_FORWARD"):
        raise ConflictError(f"{item['status']} 항목에는 지급을 기록할 수 없습니다.")

    outstanding = int(item["due_amount"]) - int(item["paid_amount"])
    if amount > outstanding:
        raise ValidationError(
            f"남은 지급액({outstanding:,}원)을 초과합니다. "
            f"입력한 금액은 {amount:,}원입니다.",
            detail={"outstanding": outstanding, "requested": amount,
                    "due_amount": int(item["due_amount"]),
                    "paid_amount": int(item["paid_amount"])},
        )

    payment_id = db.execute(text("""
        INSERT INTO payout_payment (
          org_id, payout_item_id, payment_date, amount, payment_reference,
          memo, recorded_by, evidence_attachment_id, created_by
        ) VALUES (:org, :i, :d, :amt, :ref, :memo, :actor, :ev, :actor)
        RETURNING id
    """), {
        "org": str(org_id), "i": str(item_id), "d": payment_date, "amt": int(amount),
        "ref": payment_reference, "memo": memo, "actor": str(actor_user_id),
        "ev": str(evidence_attachment_id) if evidence_attachment_id else None,
    }).scalar_one()

    record_audit(
        db, org_id=org_id, domain_table="payout_payment",
        domain_row_id=UUID(str(payment_id)), action="CREATE",
        actor_user_id=actor_user_id,
        after={"payout_item_id": str(item_id), "amount": int(amount),
               "payment_date": str(payment_date),
               "payee": item["payee_name_snapshot"]},
        reason="지급 기록", request_id=request_id,
    )
    db.commit()

    _sync_batch_status(db, org_id=org_id, item_id=item_id)
    db.commit()

    # 트리거가 갱신한 뒤의 값을 돌려준다. 화면이 다시 조회하지 않아도
    # "얼마 남았는지" 를 바로 보여줄 수 있어야 한다.
    after = db.execute(text("""
        SELECT id, batch_id, due_amount, paid_amount, status
          FROM payout_item WHERE id = :i
    """), {"i": str(item_id)}).mappings().one()
    return {
        "payment_id": str(payment_id),
        "item": {
            **dict(after),
            "outstanding_amount": int(after["due_amount"]) - int(after["paid_amount"]),
        },
    }


def _sync_batch_status(db: Session, *, org_id: UUID, item_id: UUID) -> None:
    """배치 상태를 항목 상태에서 계산한다.

    배치에 별도 진실을 두지 않는다 — 항목이 진실이고 배치는 그 요약이다.
    """
    db.execute(text("""
        UPDATE payout_batch b
           SET status = CASE
                 WHEN b.status = 'CANCELLED' THEN 'CANCELLED'
                 WHEN b.status = 'DRAFT' THEN 'DRAFT'
                 WHEN agg.open_count = 0 AND agg.paid_count > 0 THEN 'PAID'
                 WHEN agg.paid_total > 0 THEN 'PARTIALLY_PAID'
                 ELSE 'APPROVED'
               END
          FROM (
            SELECT batch_id,
                   COUNT(*) FILTER (
                     WHERE status IN ('CALCULATED','APPROVED','PARTIALLY_PAID')
                   ) AS open_count,
                   COUNT(*) FILTER (WHERE status = 'PAID') AS paid_count,
                   COALESCE(SUM(paid_amount), 0) AS paid_total
              FROM payout_item
             WHERE deleted_at IS NULL AND status <> 'CANCELLED'
             GROUP BY batch_id
          ) agg
         WHERE agg.batch_id = b.id
           AND b.id = (SELECT batch_id FROM payout_item WHERE id = :i)
           AND b.org_id = :org
    """), {"i": str(item_id), "org": str(org_id)})


def cancel_payment(
    db: Session, *, org_id: UUID, actor_user_id: UUID, payment_id: UUID,
    reason: str,
) -> None:
    """잘못 입력한 지급 기록 취소.

    행을 지우지 않고 `deleted_at` 을 찍는다 — 트리거가 `deleted_at IS NULL` 만
    합산하므로 `paid_amount` 는 자동으로 줄어들고, 무엇을 취소했는지는 남는다.
    """
    if not reason or not reason.strip():
        raise ValidationError("지급 기록 취소에는 사유가 필요합니다.")

    row = db.execute(text("""
        SELECT payout_item_id, amount FROM payout_payment
         WHERE id = :p AND org_id = :org AND deleted_at IS NULL
    """), {"p": str(payment_id), "org": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("지급 기록을 찾을 수 없습니다.")

    db.execute(text("""
        UPDATE payout_payment SET deleted_at = now(), deleted_by = :actor
         WHERE id = :p
    """), {"p": str(payment_id), "actor": str(actor_user_id)})
    record_audit(
        db, org_id=org_id, domain_table="payout_payment", domain_row_id=payment_id,
        action="DELETE", actor_user_id=actor_user_id,
        before={"amount": int(row["amount"])}, reason=reason,
    )
    db.commit()
    _sync_batch_status(db, org_id=org_id, item_id=UUID(str(row["payout_item_id"])))
    db.commit()


# ──────────────────────────────────────────────────────────────────────────
# 개인카드 상환 (Phase 34)
# ──────────────────────────────────────────────────────────────────────────

def list_reimbursement_candidates(
    db: Session, *, org_id: UUID, year_month: str | None = None,
    card_holder_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """상환 대상 후보 = 개인카드로 결제한 사업용 비용 중 아직 상환 항목이 없는 것.

    이미 상환 항목이 있는 비용은 제외한다 — 두 번 갚지 않기 위해서다.
    (DB UNIQUE 인덱스도 같은 것을 막지만, 후보 목록에 보이면 사용자가
     추가하려다 오류를 만난다.)
    """
    where = [
        "e.org_id = :org", "e.deleted_at IS NULL",
        "e.is_personal_card = TRUE", "e.is_business_use = TRUE",
    ]
    p: dict[str, Any] = {"org": str(org_id)}
    if year_month:
        where.append("to_char(e.expense_date, 'YYYY-MM') = :ym")
        p["ym"] = year_month
    if card_holder_id:
        where.append("e.payment_card_holder_id = :holder")
        p["holder"] = str(card_holder_id)

    rows = db.execute(text(f"""
        SELECT e.id AS expense_id, e.expense_date, e.description, e.gross_amount,
               e.payment_card_holder_id, p.name AS card_holder_name,
               e.evidence_status::text AS evidence_status
          FROM expenses e
          LEFT JOIN partner p ON p.id = e.payment_card_holder_id
         WHERE {" AND ".join(where)}
           AND NOT EXISTS (
             SELECT 1 FROM payout_item pi
              WHERE pi.source_expense_id = e.id
                AND pi.item_type = 'EXPENSE_REIMBURSEMENT'
                AND pi.status <> 'CANCELLED' AND pi.deleted_at IS NULL
           )
         ORDER BY e.expense_date DESC
    """), p).mappings().all()
    return [dict(r) for r in rows]


def create_reimbursement_batch(
    db: Session, *, org_id: UUID, actor_user_id: UUID, expense_ids: list[UUID],
    title: str, due_date: date | None = None, request_id: str | None = None,
) -> dict[str, Any]:
    """선택한 개인카드 비용들로 상환 배치를 만든다.

    ⚠️ **새 비용을 만들지 않는다.** 원래 비용이 이미 손익에 반영돼 있다.
    여기서 만드는 것은 "그 돈을 카드 소유자에게 돌려줘야 한다" 는 채무일 뿐이다.
    """
    if not expense_ids:
        raise ValidationError("상환할 비용을 선택하세요.")

    rows = db.execute(text("""
        SELECT e.id, e.gross_amount, e.description, e.expense_date,
               e.is_personal_card, e.payment_card_holder_id,
               p.name AS holder_name
          FROM expenses e
          LEFT JOIN partner p ON p.id = e.payment_card_holder_id
         WHERE e.org_id = :org AND e.deleted_at IS NULL
           AND e.id = ANY(CAST(:ids AS UUID[]))
    """), {"org": str(org_id), "ids": [str(i) for i in expense_ids]}).mappings().all()

    found = {str(r["id"]) for r in rows}
    missing = [str(i) for i in expense_ids if str(i) not in found]
    if missing:
        raise NotFoundError(f"비용을 찾을 수 없습니다: {missing}")

    not_personal = [str(r["id"]) for r in rows if not r["is_personal_card"]]
    if not_personal:
        raise ValidationError(
            "개인카드 결제가 아닌 비용은 상환 대상이 아닙니다.",
            detail={"expense_ids": not_personal},
        )

    batch_id = create_batch(
        db, org_id=org_id, actor_user_id=actor_user_id, title=title,
        memo="개인카드 사용분 상환 — 원본 비용은 이미 손익에 반영되어 있습니다.",
        _commit=False,
    )
    for r in rows:
        add_item(
            db, org_id=org_id, actor_user_id=actor_user_id, batch_id=batch_id,
            item_type="EXPENSE_REIMBURSEMENT",
            payee_name=r["holder_name"] or "카드 소유자 미지정",
            payee_partner_id=(
                UUID(str(r["payment_card_holder_id"])) if r["payment_card_holder_id"] else None
            ),
            due_amount=int(r["gross_amount"]),
            source_expense_id=UUID(str(r["id"])),
            due_date=due_date,
            memo=f"{r['expense_date']} {r['description']}",
            _commit=False,
        )

    record_audit(
        db, org_id=org_id, domain_table="payout_batch", domain_row_id=batch_id,
        action="CREATE", actor_user_id=actor_user_id,
        after={"expense_count": len(rows),
               "total": sum(int(r["gross_amount"]) for r in rows)},
        reason="개인카드 상환 배치 생성", request_id=request_id,
    )
    db.commit()
    return get_batch(db, org_id=org_id, batch_id=batch_id)


def reimbursement_status_of(
    db: Session, *, org_id: UUID, expense_id: UUID
) -> dict[str, Any]:
    """비용 한 건의 상환 상태.

    **`expenses` 에 상태 컬럼을 두지 않는다.** 지급 기록이 진실이고 여기서 계산한다.
    두 곳에 상태가 있으면 반드시 어긋난다.
    """
    exp = db.execute(text("""
        SELECT is_personal_card, is_business_use, gross_amount
          FROM expenses WHERE id = :e AND org_id = :org AND deleted_at IS NULL
    """), {"e": str(expense_id), "org": str(org_id)}).mappings().first()
    if exp is None:
        raise NotFoundError("비용을 찾을 수 없습니다.")

    if not (exp["is_personal_card"] and exp["is_business_use"]):
        return {"status": "NOT_REQUIRED", "due_amount": 0, "paid_amount": 0}

    item = db.execute(text("""
        SELECT id, due_amount, paid_amount, status FROM payout_item
         WHERE source_expense_id = :e AND item_type = 'EXPENSE_REIMBURSEMENT'
           AND status <> 'CANCELLED' AND deleted_at IS NULL
    """), {"e": str(expense_id)}).mappings().first()
    if item is None:
        return {
            "status": "PENDING", "due_amount": int(exp["gross_amount"]),
            "paid_amount": 0, "payout_item_id": None,
        }

    due, paid = int(item["due_amount"]), int(item["paid_amount"])
    if paid == 0:
        status = "PENDING"
    elif paid < due:
        status = "PARTIALLY_PAID"
    else:
        status = "PAID"
    return {
        "status": status, "due_amount": due, "paid_amount": paid,
        "payout_item_id": str(item["id"]),
    }
