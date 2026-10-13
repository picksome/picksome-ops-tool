"""운영 이슈 센터 (Phase 35~36).

대시보드 경고를 **처리 가능한 업무**로 만든다.

스캐너의 첫 번째 규칙: 사람의 결정을 덮어쓰지 않는다
------------------------------------------------
```
같은 dedupe_key 의 열린 이슈가 있다   → last_detected_at 만 갱신
같은 dedupe_key 가 RESOLVED/IGNORED  → 건드리지 않는다
조건이 사라졌다                       → auto_resolve 인 유형만 자동 종료
```

`IGNORED` 를 매 스캔이 `OPEN` 으로 되돌리면 사용자는 두 번째 스캔부터
이슈 센터를 무시한다. 그 순간 이 기능은 없는 것과 같아진다.

`auto_resolve` 를 유형마다 다르게 두는 이유
--------------------------------------
"미매칭 정산 3건" 은 조건이 데이터로 명확하다 — 매칭되면 사라져야 한다.
반면 "증빙 없음" 은 사용자가 "이 건은 증빙이 원래 없다" 고 판단해
`IGNORED` 로 닫았을 수 있다. 조건은 그대로인데 자동으로 다시 열면 안 된다.

무엇을 스캔하는가
--------------
월 마감 검증(`close_checks`)과 **같은 카탈로그를 재사용한다.** 검사 로직을
두 벌 만들면 반드시 어긋난다. 마감 검증에 없는 상시 항목(가격 반영 실패,
증빙 삭제 실패 등)만 여기서 추가로 본다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.services import close_checks

OPEN_STATUSES = ("OPEN", "IN_PROGRESS", "WAITING")
CLOSED_STATUSES = ("RESOLVED", "IGNORED")

# 상태 전이. 종료된 이슈를 다시 여는 것은 허용한다 — 잘못 닫는 일이 실제로 있다.
_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "OPEN": ("IN_PROGRESS", "WAITING", "RESOLVED", "IGNORED"),
    "IN_PROGRESS": ("OPEN", "WAITING", "RESOLVED", "IGNORED"),
    "WAITING": ("OPEN", "IN_PROGRESS", "RESOLVED", "IGNORED"),
    "RESOLVED": ("OPEN",),
    "IGNORED": ("OPEN",),
}


@dataclass(slots=True)
class IssueCandidate:
    """스캐너가 찾은 것 하나."""

    issue_type: str
    severity: str
    dedupe_key: str
    title: str
    description: str | None = None
    entity_type: str | None = None
    entity_id: UUID | None = None
    year_month: str | None = None
    affected_count: int = 1
    blocks_month_close: bool = False
    auto_resolve: bool = False


@dataclass(slots=True)
class ScanResult:
    created: int = 0
    refreshed: int = 0
    auto_resolved: int = 0
    skipped_closed: int = 0
    candidates: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created, "refreshed": self.refreshed,
            "auto_resolved": self.auto_resolved,
            "skipped_closed": self.skipped_closed,
            "candidates": self.candidates,
        }


def _now() -> datetime:
    return datetime.now(tz=UTC)


# ──────────────────────────────────────────────────────────────────────────
# 조회
# ──────────────────────────────────────────────────────────────────────────

def list_issues(
    db: Session, *, org_id: UUID,
    status: list[str] | None = None, severity: list[str] | None = None,
    issue_type: str | None = None, assigned_to: UUID | None = None,
    entity_type: str | None = None, entity_id: UUID | None = None,
    year_month: str | None = None, keyword: str | None = None,
    open_only: bool = False, overdue_only: bool = False,
    limit: int = 50, offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where = ["i.org_id = :org", "i.deleted_at IS NULL"]
    p: dict[str, Any] = {"org": str(org_id), "lim": limit, "off": offset}

    if open_only:
        where.append("i.status = ANY(:open_st)")
        p["open_st"] = list(OPEN_STATUSES)
    elif status:
        where.append("i.status = ANY(:st)")
        p["st"] = status
    if severity:
        where.append("i.severity = ANY(:sev)")
        p["sev"] = severity
    if issue_type:
        where.append("i.issue_type = :itype")
        p["itype"] = issue_type
    if assigned_to:
        where.append("i.assigned_to = :assignee")
        p["assignee"] = str(assigned_to)
    if entity_type:
        where.append("i.entity_type = :etype")
        p["etype"] = entity_type
    if entity_id:
        where.append("i.entity_id = :eid")
        p["eid"] = str(entity_id)
    if year_month:
        where.append("i.year_month = :ym")
        p["ym"] = year_month
    if keyword:
        where.append("(i.title ILIKE :kw OR i.description ILIKE :kw)")
        p["kw"] = f"%{keyword}%"
    if overdue_only:
        where.append("i.due_date IS NOT NULL AND i.due_date < CURRENT_DATE")
        where.append("i.status = ANY(:open_st2)")
        p["open_st2"] = list(OPEN_STATUSES)

    clause = " AND ".join(where)
    total = db.execute(
        text(f"SELECT COUNT(*) FROM operation_issue i WHERE {clause}"), p
    ).scalar() or 0
    rows = db.execute(text(f"""
        SELECT i.id, i.issue_type, i.severity, i.entity_type, i.entity_id,
               i.year_month, i.dedupe_key, i.title, i.description, i.affected_count,
               i.status, i.assigned_to, u.display_name AS assigned_to_name,
               i.due_date, i.blocks_month_close, i.auto_resolve,
               i.first_detected_at, i.last_detected_at,
               i.resolved_at, i.resolution_note,
               (i.due_date IS NOT NULL AND i.due_date < CURRENT_DATE
                AND i.status = ANY(ARRAY['OPEN','IN_PROGRESS','WAITING'])) AS is_overdue
          FROM operation_issue i
          LEFT JOIN app_user u ON u.id = i.assigned_to
         WHERE {clause}
         ORDER BY
           CASE i.status WHEN 'OPEN' THEN 0 WHEN 'IN_PROGRESS' THEN 1
                         WHEN 'WAITING' THEN 2 ELSE 3 END,
           CASE i.severity WHEN 'BLOCKING' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END,
           i.last_detected_at DESC
         LIMIT :lim OFFSET :off
    """), p).mappings().all()
    return [dict(r) for r in rows], int(total)


def get_issue(db: Session, *, org_id: UUID, issue_id: UUID) -> dict[str, Any]:
    row = db.execute(text("""
        SELECT i.*, u.display_name AS assigned_to_name
          FROM operation_issue i
          LEFT JOIN app_user u ON u.id = i.assigned_to
         WHERE i.id = :id AND i.org_id = :org AND i.deleted_at IS NULL
    """), {"id": str(issue_id), "org": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("운영 이슈를 찾을 수 없습니다.")
    return dict(row)


def summary(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """대시보드용 요약. 경고 배지가 이 숫자를 쓰고, 클릭하면 필터된 목록으로 간다."""
    row = db.execute(text("""
        SELECT
          COUNT(*) FILTER (WHERE status = ANY(ARRAY['OPEN','IN_PROGRESS','WAITING'])) AS open_total,
          COUNT(*) FILTER (WHERE severity = 'BLOCKING'
                             AND status = ANY(ARRAY['OPEN','IN_PROGRESS','WAITING'])) AS blocking,
          COUNT(*) FILTER (WHERE severity = 'WARNING'
                             AND status = ANY(ARRAY['OPEN','IN_PROGRESS','WAITING'])) AS warning,
          COUNT(*) FILTER (WHERE severity = 'INFO'
                             AND status = ANY(ARRAY['OPEN','IN_PROGRESS','WAITING'])) AS info,
          COUNT(*) FILTER (WHERE due_date IS NOT NULL AND due_date < CURRENT_DATE
                             AND status = ANY(ARRAY['OPEN','IN_PROGRESS','WAITING'])) AS overdue,
          COUNT(*) FILTER (WHERE assigned_to IS NULL
                             AND status = ANY(ARRAY['OPEN','IN_PROGRESS','WAITING'])) AS unassigned
          FROM operation_issue
         WHERE org_id = :org AND deleted_at IS NULL
    """), {"org": str(org_id)}).mappings().one()

    by_type = db.execute(text("""
        SELECT issue_type, severity, COUNT(*) AS n
          FROM operation_issue
         WHERE org_id = :org AND deleted_at IS NULL
           AND status = ANY(ARRAY['OPEN','IN_PROGRESS','WAITING'])
         GROUP BY issue_type, severity
         ORDER BY n DESC
    """), {"org": str(org_id)}).mappings().all()

    last_scan = db.execute(text("""
        SELECT started_at, finished_at, status FROM worker_run
         WHERE worker_name = 'operation_issue_scan'
           AND (org_id = :org OR org_id IS NULL)
         ORDER BY started_at DESC LIMIT 1
    """), {"org": str(org_id)}).mappings().first()

    return {
        **dict(row),
        "by_type": [dict(r) for r in by_type],
        "last_scan": dict(last_scan) if last_scan else None,
    }


# ──────────────────────────────────────────────────────────────────────────
# 상태 변경
# ──────────────────────────────────────────────────────────────────────────

def update_issue(
    db: Session, *, org_id: UUID, issue_id: UUID, actor_user_id: UUID,
    status: str | None = None, assigned_to: UUID | None = None,
    due_date: date | None = None, resolution_note: str | None = None,
    clear_assignee: bool = False, request_id: str | None = None,
) -> dict[str, Any]:
    """이슈 갱신. 상태 전이는 허용표를 따른다."""
    current = get_issue(db, org_id=org_id, issue_id=issue_id)

    sets = ["updated_by = :actor"]
    p: dict[str, Any] = {"id": str(issue_id), "org": str(org_id),
                         "actor": str(actor_user_id)}

    if status is not None and status != current["status"]:
        allowed = _ALLOWED_TRANSITIONS.get(current["status"], ())
        if status not in allowed:
            raise ConflictError(
                f"{current['status']} 에서 {status} 로 바꿀 수 없습니다.",
                detail={"from": current["status"], "allowed": list(allowed)},
            )
        # IGNORED 는 사유가 필수다. DB CHECK 도 막지만 여기서 먼저 알려 준다 —
        # 제약 위반 메시지만으로는 무엇을 해야 할지 알 수 없다.
        note = resolution_note if resolution_note is not None else current["resolution_note"]
        if status == "IGNORED" and not (note or "").strip():
            raise ValidationError(
                "이슈를 무시하려면 사유가 필요합니다. "
                "왜 처리하지 않아도 되는지 남겨야 나중에 설명할 수 있습니다.",
                detail={"field": "resolution_note"},
            )
        sets.append("status = :st")
        p["st"] = status
        if status in CLOSED_STATUSES:
            sets.append("resolved_at = now()")
            sets.append("resolved_by = :actor")
        else:
            # 다시 연 이슈는 종료 정보를 지운다. 남겨 두면 "열려 있는데 해결됨" 이 된다.
            sets.append("resolved_at = NULL")
            sets.append("resolved_by = NULL")

    if resolution_note is not None:
        sets.append("resolution_note = :note")
        p["note"] = resolution_note
    if clear_assignee:
        sets.append("assigned_to = NULL")
    elif assigned_to is not None:
        sets.append("assigned_to = :assignee")
        p["assignee"] = str(assigned_to)
    if due_date is not None:
        sets.append("due_date = :due")
        p["due"] = due_date

    db.execute(text(f"""
        UPDATE operation_issue SET {", ".join(sets)}
         WHERE id = :id AND org_id = :org AND deleted_at IS NULL
    """), p)

    record_audit(
        db, org_id=org_id, domain_table="operation_issue", domain_row_id=issue_id,
        action="STATUS_CHANGE" if status else "UPDATE", actor_user_id=actor_user_id,
        before={"status": current["status"], "assigned_to": str(current["assigned_to"])
                if current["assigned_to"] else None},
        after={"status": status or current["status"]},
        reason=resolution_note, request_id=request_id,
    )
    db.commit()
    return get_issue(db, org_id=org_id, issue_id=issue_id)


def create_manual_issue(
    db: Session, *, org_id: UUID, actor_user_id: UUID, issue_type: str,
    severity: str, title: str, description: str | None = None,
    entity_type: str | None = None, entity_id: UUID | None = None,
    year_month: str | None = None, assigned_to: UUID | None = None,
    due_date: date | None = None,
) -> dict[str, Any]:
    """사람이 직접 만든 이슈.

    `created_by_scanner = FALSE` 라 스캐너가 자동 해결 대상으로 보지 않는다 —
    사람이 만든 일을 기계가 닫으면 안 된다.
    """
    if severity not in ("BLOCKING", "WARNING", "INFO"):
        raise ValidationError(f"알 수 없는 심각도입니다: {severity}")

    issue_id = db.execute(text("""
        INSERT INTO operation_issue (
          org_id, issue_type, severity, entity_type, entity_id, year_month,
          dedupe_key, title, description, assigned_to, due_date,
          created_by_scanner, auto_resolve, created_by, updated_by
        ) VALUES (
          :org, :itype, :sev, :etype, :eid, :ym,
          :dedupe, :title, :desc, :assignee, :due,
          FALSE, FALSE, :actor, :actor
        )
        RETURNING id
    """), {
        "org": str(org_id), "itype": issue_type, "sev": severity,
        "etype": entity_type, "eid": str(entity_id) if entity_id else None,
        "ym": year_month,
        # 수동 이슈는 스캐너 키와 겹치면 안 된다. 접두어로 분리한다.
        "dedupe": f"MANUAL:{issue_id_seed(actor_user_id)}",
        "title": title, "desc": description,
        "assignee": str(assigned_to) if assigned_to else None, "due": due_date,
        "actor": str(actor_user_id),
    }).scalar_one()
    db.commit()
    return get_issue(db, org_id=org_id, issue_id=UUID(str(issue_id)))


def issue_id_seed(actor_user_id: UUID) -> str:
    """수동 이슈의 dedupe key 재료. 충돌하지 않으면 되므로 시각 + actor 를 쓴다."""
    return f"{actor_user_id}:{_now().isoformat()}"


# ──────────────────────────────────────────────────────────────────────────
# 스캐너
# ──────────────────────────────────────────────────────────────────────────

def collect_candidates(
    db: Session, *, org_id: UUID, year_months: list[str] | None = None,
) -> list[IssueCandidate]:
    """이슈 후보 수집.

    월 마감 검증 카탈로그를 **재사용한다.** 검사 로직을 두 벌 만들면 어긋난다.
    """
    out: list[IssueCandidate] = []
    months = year_months or _recent_open_months(db, org_id=org_id)

    for ym in months:
        for item in close_checks.run_checks(db, org_id=org_id, year_month=ym):
            out.append(IssueCandidate(
                issue_type=item.code,
                severity=item.grade,
                dedupe_key=close_checks.dedupe_key_for(ym, item.code),
                title=f"[{ym}] {item.message}",
                description=item.hint,
                entity_type=item.entity_type,
                year_month=ym,
                affected_count=max(item.count, 1),
                # 마감 검증의 BLOCKING 은 그 달의 마감을 막는다 —
                # close_checks 가 이미 그렇게 판정했으므로 여기서 다시 정하지 않는다.
                blocks_month_close=False,
                # 마감 검증 항목은 조건이 데이터로 명확하다. 사라지면 닫아도 된다.
                auto_resolve=True,
            ))

    out += _standing_candidates(db, org_id=org_id)
    out += _inventory_candidates(db, org_id=org_id)
    return out


def _inventory_candidates(db: Session, *, org_id: UUID) -> list[IssueCandidate]:
    """재고·발주에서 **데이터로 확실히 판단되는 것**만.

    임계값을 만들지 않는다
    -------------------
    "재고 5개 미만" 같은 기준은 여기 없다. 적정 재고는 회전율·리드타임·계절이
    함께 정하는 것이라 숫자 하나로 못 정하고, 정하면 그 숫자가 어디서 왔는지
    아무도 모른다.

    그 판단은 `REORDER_MONITOR` 정책이 사용자에게 받은 값으로 한다. 여기서는
    **정책 없이도 참인 것**만 본다.

    예외 센터와 겹치지 않나
    --------------------
    겹친다. 하지만 역할이 다르다.

    ```
    예외 센터        지금 화면을 열었을 때의 상태     (조회 시점)
    operation_issue  담당자·기한·해결 이력이 붙는 것  (추적)
    ```

    그래서 **추적할 가치가 있는 것만** 이슈로 올린다. 재주문 기한 경과는
    매일 바뀌므로 이슈로 만들지 않는다 — 예외 센터에서 보면 된다.
    """
    out: list[IssueCandidate] = []

    # ── 음수 재고 ──────────────────────────────────────────────
    #
    # 장부가 실물과 어긋났다는 확실한 신호다. 임계값이 필요 없다 — 0 미만이
    # 곧 문제다. 원인을 찾아 조정해야 사라지므로 추적할 가치가 있다.
    negatives = db.execute(text("""
        SELECT COUNT(*) FROM (
            SELECT product_id, product_variant_id
              FROM inventory_transaction
             WHERE org_id = :org
             GROUP BY product_id, product_variant_id
            HAVING SUM(quantity_delta) < 0
        ) t
    """), {"org": str(org_id)}).scalar() or 0
    if negatives:
        out.append(IssueCandidate(
            issue_type="NEGATIVE_INVENTORY", severity="WARNING",
            dedupe_key="STANDING:NEGATIVE_INVENTORY",
            title=f"재고가 음수인 상품 {negatives}건",
            description="판매가 재고보다 많이 기록됐습니다. 기초 재고가 빠졌거나 "
                        "입고가 누락된 경우가 대부분입니다. 0 으로 맞추지 말고 "
                        "원인을 찾은 뒤 조정하세요.",
            entity_type="product", affected_count=negatives, auto_resolve=True,
        ))

    # ── 입고 지연 ──────────────────────────────────────────────
    #
    # **예상 입고일이 있는 발주만** 본다. 날짜가 없으면 늦었는지 알 수 없고,
    # 그건 "정상" 이 아니라 "모름" 이다 — 이슈로 만들지 않는다.
    #
    # "3일 이상 지연" 같은 유예를 두지 않는다. 그 3일이 어디서 나온 숫자인지
    # 설명할 수 없다.
    overdue = db.execute(text("""
        SELECT COUNT(*) FROM purchase_order
         WHERE org_id = :org AND deleted_at IS NULL
           AND status IN ('ORDERED', 'PARTIALLY_RECEIVED')
           AND expected_at IS NOT NULL
           AND expected_at < (now() AT TIME ZONE 'Asia/Seoul')::date
    """), {"org": str(org_id)}).scalar() or 0
    if overdue:
        out.append(IssueCandidate(
            issue_type="PURCHASE_OVERDUE", severity="WARNING",
            dedupe_key="STANDING:PURCHASE_OVERDUE",
            title=f"예상 입고일이 지난 발주 {overdue}건",
            description="주문했는데 아직 들어오지 않았습니다. 공급처에 확인하거나 "
                        "예상일을 고치세요.",
            entity_type="purchase_order", affected_count=overdue,
            auto_resolve=True,
        ))

    # ── 재주문을 계산할 수 없는 상품 ────────────────────────────
    #
    # 소싱처나 리드타임이 없으면 재주문이 영영 안 돈다. 이건 매일 바뀌는 값이
    # 아니라 **사람이 한 번 채우면 끝나는 일**이라 추적할 가치가 있다.
    #
    # 이미 재고를 관리하는 상품만 센다. 재고를 안 잡은 상품까지 세면 상품
    # 마스터 전체가 이슈가 되고, 그러면 아무도 안 본다.
    #
    # `NO_SOURCE` 를 여기서 따로 만들지 않는 이유: 소싱 쪽 검사와 겹칠 수
    # 있어 사유별로 쪼개지 않고 **한 건으로 묶는다.**
    incomplete = db.execute(text("""
        SELECT COUNT(*) FROM (
            SELECT t.product_id
              FROM inventory_transaction t
              JOIN product p ON p.id = t.product_id AND p.deleted_at IS NULL
              LEFT JOIN product_source s
                     ON s.product_id = t.product_id AND s.deleted_at IS NULL
                    AND s.status = 'ACTIVE' AND s.is_primary
             WHERE t.org_id = :org
             GROUP BY t.product_id
            HAVING COUNT(s.id) = 0
                OR MAX(s.lead_time_days) IS NULL
        ) t
    """), {"org": str(org_id)}).scalar() or 0
    if incomplete:
        out.append(IssueCandidate(
            issue_type="REORDER_DATA_INCOMPLETE",
            # 기능이 안 되는 것이지 마감을 막는 것은 아니다.
            severity="INFO",
            dedupe_key="STANDING:REORDER_DATA_INCOMPLETE",
            title=f"재주문을 계산할 수 없는 상품 {incomplete}건",
            description="주 소싱처나 리드타임이 없어 발주 시점을 계산할 수 "
                        "없습니다. 상품별 소싱 설정에서 채워 주세요.",
            entity_type="product", affected_count=incomplete, auto_resolve=True,
        ))

    return out


def _recent_open_months(db: Session, *, org_id: UUID, limit: int = 3) -> list[str]:
    """스캔 대상 월. 잠긴 달은 제외한다 — 고칠 수 없는 것을 이슈로 올리지 않는다."""
    rows = db.execute(text("""
        SELECT year_month FROM monthly_settlement
         WHERE org_id = :org AND deleted_at IS NULL AND close_state <> 'LOCKED'
         ORDER BY year_month DESC LIMIT :lim
    """), {"org": str(org_id), "lim": limit}).scalars().all()
    if rows:
        return list(rows)
    # 정산 행이 아직 없으면 이번 달을 본다.
    today = date.today()
    return [f"{today.year:04d}-{today.month:02d}"]


def _standing_candidates(db: Session, *, org_id: UUID) -> list[IssueCandidate]:
    """월과 무관한 상시 항목.

    마감 검증에 없는 것만 여기서 본다 — 중복하면 같은 문제가 이슈 두 개로 뜬다.
    """
    out: list[IssueCandidate] = []

    n = db.execute(text("""
        SELECT COUNT(*) FROM expense_attachment
         WHERE org_id = :org AND deleted_at IS NULL
           AND status = 'PENDING_DELETE' AND delete_attempt_count >= 5
    """), {"org": str(org_id)}).scalar() or 0
    if n:
        out.append(IssueCandidate(
            issue_type="ATTACHMENT_DELETE_FAILED", severity="WARNING",
            dedupe_key="STANDING:ATTACHMENT_DELETE_FAILED",
            title=f"삭제에 반복 실패한 증빙 {n}건",
            description="스토리지에서 파일을 지우지 못했습니다. 저장소 상태를 확인하세요.",
            entity_type="expense_attachment", affected_count=n, auto_resolve=True,
        ))

    n = db.execute(text("""
        SELECT COUNT(*) FROM channel_price_publish_outbox
         WHERE org_id = :org AND status = 'FAILED'
    """), {"org": str(org_id)}).scalar() or 0
    if n:
        out.append(IssueCandidate(
            issue_type="CHANNEL_PRICE_PUBLISH_FAILED", severity="WARNING",
            dedupe_key="STANDING:CHANNEL_PRICE_PUBLISH_FAILED",
            title=f"판매처 가격 반영에 실패한 작업 {n}건",
            description="승인된 가격이 판매처에 반영되지 않았습니다.",
            entity_type="channel_price_publish_outbox", affected_count=n,
            auto_resolve=True,
        ))

    # connector 미연결로 대기 중인 작업 — 실패가 아니라 미연결이다.
    # 기다린다고 해결되지 않으므로 사람이 알아야 한다.
    n = db.execute(text("""
        SELECT COUNT(*) FROM channel_price_publish_outbox
         WHERE org_id = :org AND status = 'CONNECTOR_UNAVAILABLE'
    """), {"org": str(org_id)}).scalar() or 0
    if n:
        out.append(IssueCandidate(
            issue_type="CONNECTOR_UNAVAILABLE", severity="INFO",
            dedupe_key="STANDING:CONNECTOR_UNAVAILABLE",
            title=f"판매처 연동이 없어 반영되지 못한 가격 {n}건",
            description="판매처 API 연동이 설정되면 자동으로 반영됩니다. "
                        "그때까지는 판매처에서 직접 변경해야 합니다.",
            entity_type="channel_price_publish_outbox", affected_count=n,
            auto_resolve=True,
        ))

    n = db.execute(text("""
        SELECT COUNT(*) FROM period_recalc_flag
         WHERE org_id = :org AND resolved_at IS NULL
           AND marked_at < now() - INTERVAL '7 days'
    """), {"org": str(org_id)}).scalar() or 0
    if n:
        out.append(IssueCandidate(
            issue_type="PERIOD_RECALC_STALE", severity="WARNING",
            dedupe_key="STANDING:PERIOD_RECALC_STALE",
            title=f"7일 넘게 재계산되지 않은 기간 {n}건",
            description="비용이 바뀐 뒤 집계를 다시 돌리지 않았습니다. "
                        "이 상태로 확정하면 낡은 숫자가 박제됩니다.",
            entity_type="period_recalc_flag", affected_count=n, auto_resolve=True,
        ))

    n = db.execute(text("""
        SELECT COUNT(*) FROM approval_request
         WHERE org_id = :org AND deleted_at IS NULL
           AND auto_apply_status = 'HELD_BY_LOCK'
           AND updated_at < now() - INTERVAL '14 days'
    """), {"org": str(org_id)}).scalar() or 0
    if n:
        out.append(IssueCandidate(
            issue_type="APPROVAL_HELD_LONG", severity="WARNING",
            dedupe_key="STANDING:APPROVAL_HELD_LONG",
            title=f"2주 넘게 월 잠금으로 대기 중인 승인 {n}건",
            description="해당 월을 재오픈하지 않으면 영원히 반영되지 않습니다. "
                        "반영하지 않을 것이라면 요청을 취소하세요.",
            entity_type="approval_request", affected_count=n, auto_resolve=True,
        ))

    n = db.execute(text("""
        SELECT COUNT(*) FROM payout_item
         WHERE org_id = :org AND deleted_at IS NULL
           AND status IN ('APPROVED','PARTIALLY_PAID')
           AND due_date IS NOT NULL AND due_date < CURRENT_DATE
    """), {"org": str(org_id)}).scalar() or 0
    if n:
        out.append(IssueCandidate(
            issue_type="PAYOUT_OVERDUE", severity="WARNING",
            dedupe_key="STANDING:PAYOUT_OVERDUE",
            title=f"지급 예정일이 지난 지급 항목 {n}건",
            description="승인된 지급이 예정일까지 실행되지 않았습니다.",
            entity_type="payout_item", affected_count=n, auto_resolve=True,
        ))

    return out


def scan(
    db: Session, *, org_id: UUID, actor_user_id: UUID | None = None,
    year_months: list[str] | None = None,
) -> ScanResult:
    """이슈 스캔.

    **사람이 닫은 이슈를 다시 열지 않는다.** 이 규칙이 없으면 이슈 센터를
    두 번째 스캔부터 아무도 보지 않는다.
    """
    result = ScanResult()
    candidates = collect_candidates(db, org_id=org_id, year_months=year_months)
    result.candidates = len(candidates)
    seen_keys = {c.dedupe_key for c in candidates}

    for c in candidates:
        existing = db.execute(text("""
            SELECT id, status FROM operation_issue
             WHERE org_id = :org AND dedupe_key = :k AND deleted_at IS NULL
             ORDER BY CASE WHEN status = ANY(ARRAY['OPEN','IN_PROGRESS','WAITING'])
                           THEN 0 ELSE 1 END,
                      last_detected_at DESC
             LIMIT 1
        """), {"org": str(org_id), "k": c.dedupe_key}).mappings().first()

        if existing is None:
            db.execute(text("""
                INSERT INTO operation_issue (
                  org_id, issue_type, severity, entity_type, entity_id, year_month,
                  dedupe_key, title, description, affected_count,
                  blocks_month_close, auto_resolve, created_by_scanner
                ) VALUES (
                  :org, :itype, :sev, :etype, :eid, :ym,
                  :k, :title, :desc, :cnt, :blocks, :auto, TRUE
                )
            """), {
                "org": str(org_id), "itype": c.issue_type, "sev": c.severity,
                "etype": c.entity_type, "eid": str(c.entity_id) if c.entity_id else None,
                "ym": c.year_month, "k": c.dedupe_key, "title": c.title,
                "desc": c.description, "cnt": c.affected_count,
                "blocks": c.blocks_month_close, "auto": c.auto_resolve,
            })
            result.created += 1
            continue

        if existing["status"] in CLOSED_STATUSES:
            # 사람이 닫았다. 조건이 여전히 참이어도 다시 열지 않는다.
            result.skipped_closed += 1
            continue

        # 열려 있다 — 마지막 관측 시각과 건수만 갱신한다.
        # 담당자·마감일·상태는 사람이 정한 값이므로 건드리지 않는다.
        db.execute(text("""
            UPDATE operation_issue
               SET last_detected_at = now(), affected_count = :cnt,
                   title = :title, description = :desc, severity = :sev
             WHERE id = :id
        """), {
            "id": str(existing["id"]), "cnt": c.affected_count,
            "title": c.title, "desc": c.description, "sev": c.severity,
        })
        result.refreshed += 1

    # 조건이 사라진 이슈 자동 종료 — `auto_resolve` 이고 스캐너가 만든 것만.
    resolved = db.execute(text("""
        UPDATE operation_issue
           SET status = 'RESOLVED', resolved_at = now(),
               resolution_note = COALESCE(resolution_note, '조건이 해소되어 자동 종료')
         WHERE org_id = :org AND deleted_at IS NULL
           AND status = ANY(ARRAY['OPEN','IN_PROGRESS','WAITING'])
           AND auto_resolve = TRUE AND created_by_scanner = TRUE
           AND NOT (dedupe_key = ANY(:keys))
        RETURNING id
    """), {"org": str(org_id), "keys": list(seen_keys) or [""]}).scalars().all()
    result.auto_resolved = len(resolved)

    # 스캔 요약은 audit_log 에 남기지 않는다.
    #
    # `audit_log.domain_row_id` 는 NOT NULL 이다 — 감사 로그는 "어느 행에 무슨 일이
    # 있었나" 를 기록하는 구조이고, 대상 행이 없는 벌크 관찰은 표현할 수 없다.
    # 억지로 NULL 을 넣으려다 500 이 났었다(E2E 가 잡았다).
    #
    # 스캔이 언제 누구에 의해 돌았고 몇 건을 만들었는지는 `worker_run` 이 기록한다.
    # 개별 이슈의 상태 변경은 행 id 가 있으므로 `update_issue` 가 감사에 남긴다.
    db.commit()
    return result


def scan_all_orgs(db: Session, *, actor_user_id: UUID | None = None) -> dict[str, Any]:
    """조직 전체 스캔. 한 조직이 실패해도 나머지는 계속한다."""
    orgs = db.execute(text("""
        SELECT id FROM organization WHERE deleted_at IS NULL
    """)).scalars().all()

    totals = ScanResult()
    failures: list[dict[str, str]] = []
    for oid in orgs:
        try:
            r = scan(db, org_id=UUID(str(oid)), actor_user_id=actor_user_id)
        except Exception as exc:  # noqa: BLE001 — 한 조직 실패가 나머지를 막지 않는다
            db.rollback()
            failures.append({"org_id": str(oid), "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        totals.created += r.created
        totals.refreshed += r.refreshed
        totals.auto_resolved += r.auto_resolved
        totals.skipped_closed += r.skipped_closed
        totals.candidates += r.candidates

    return {**totals.as_dict(), "orgs": len(orgs), "failures": failures}


def issues_for_entity(
    db: Session, *, org_id: UUID, entity_type: str, entity_id: UUID
) -> list[dict[str, Any]]:
    """특정 대상에 걸린 열린 이슈. 상세 화면이 배지를 띄울 때 쓴다."""
    rows, _ = list_issues(
        db, org_id=org_id, entity_type=entity_type, entity_id=entity_id,
        open_only=True, limit=20,
    )
    return rows
