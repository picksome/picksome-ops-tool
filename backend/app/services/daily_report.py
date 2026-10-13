"""일일 운영 리포트 — **결정적 데이터셋**.

이 리포트가 하려는 것
------------------
사용자가 아침에 한 화면만 보고 "오늘 내가 손댈 것" 을 알 수 있게 한다.
나머지는 Seller OS 가 이미 봤다는 뜻이다.

AI 가 없어도 완성된다
------------------
이 모듈은 AI 를 부르지 않는다. 숫자는 전부 기존 서비스가 계산한 것이고,
여기서는 **모으기만** 한다. AI 가 붙으면 이 결과를 요약할 뿐이다.

```
deterministic dataset   ← 여기 (숫자의 진실)
        ↓
AI summary              ← 나중에 (읽기 좋은 문장)
```

순서를 뒤집으면 AI 가 raw 데이터를 보고 재무 숫자를 새로 계산하게 되고, 그
숫자는 화면과 다를 수 있다. 그러면 어느 쪽이 맞는지 아무도 모른다.

확인 필요한 것을 맨 위에
--------------------
숫자를 먼저 보여주면 사용자는 매일 같은 숫자를 훑다가 결국 안 보게 된다.
**오늘 사람이 해야 할 일**이 먼저다.

모르는 것은 0 이 아니다
--------------------
집계할 데이터가 없으면 `null` 과 `"데이터 없음"` 으로 표시한다. 0 으로 채우면
"오늘 매출 0원" 과 "아직 집계 안 됨" 이 같아 보인다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.timezone import now_local


def _today() -> date:
    return now_local().date()


def report_date_now() -> date:
    """리포트가 다룰 날짜. **KST 기준**이다.

    UTC 로 날짜를 세면 한국 시간 오전 9시 이전에 돈 실행이 전날 리포트를
    만든다. 새벽에 도는 스케줄에서 정확히 그 일이 일어난다.

    러너가 `_today()` 라는 비공개 이름에 기대지 않도록 공개 이름을 둔다.
    """
    return _today()


def _unknown(reason: str) -> dict[str, Any]:
    """값이 없음을 **말로** 남긴다. 0 을 넣지 않는다."""
    return {"value": None, "unknown_reason": reason}


def build(
    db: Session, *, org_id: UUID, report_date: date | None = None,
) -> dict[str, Any]:
    """하루치 운영 데이터셋.

    각 구획은 독립적으로 실패할 수 있다. 정산 조회가 터졌다고 주문 숫자까지
    사라지면 리포트를 쓸 수 없다 — 구획별로 "확인 불가" 를 남긴다.
    """
    day = report_date or _today()

    sections: dict[str, Any] = {}
    for key, fn in (
        ("orders", _orders),
        ("margin", _margin),
        ("sourcing", _sourcing),
        ("settlement", _settlement),
        ("cashflow", _cashflow),
        ("cost_changes", _cost_changes),
        ("issues", _issues),
        ("approvals", _approvals),
        ("integrations", _integrations),
        ("automation", _automation),
    ):
        try:
            sections[key] = fn(db, org_id=org_id, day=day)
        except Exception as exc:  # noqa: BLE001
            # 어느 구획이 왜 비었는지 남긴다. 조용히 빈칸으로 두면 사용자는
            # "오늘은 아무 일도 없었다" 로 읽는다.
            sections[key] = {"available": False, "error": type(exc).__name__}
            # **롤백이 없으면 한 구획의 실패가 나머지를 전부 죽인다.**
            # Postgres 는 실패한 트랜잭션에서 이후 모든 쿼리를 거부한다.
            # 스모크 테스트에서 실제로 그랬다 — 현금 구획이 터지자 자동화
            # 구획까지 "InternalError" 로 나왔고, 원인은 자동화와 무관했다.
            db.rollback()

    return {
        "report_date": day.isoformat(),
        "generated_at": now_local().isoformat(),
        # 맨 위에 오는 것 — 오늘 사람이 봐야 할 일.
        # 예외 센터와 **같은 읽기 모델**을 쓴다. 예전엔 여기서 따로 셌는데,
        # 그러면 리포트가 "3건" 이라 하고 예외 센터가 "5건" 이라 하는 일이
        # 생긴다. 사용자는 어느 쪽도 못 믿게 된다.
        "needs_attention": _needs_attention(sections),
        "exception_summary": _exception_summary(db, org_id=org_id),
        "verification": _verification_summary(db, org_id=org_id),
        "sections": sections,
    }


def _verification_summary(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """검증센터의 핵심 이상만.

    **같은 서비스를 부른다.** 여기서 따로 세면 리포트가 "오류 2건" 이라 하고
    검증센터가 "3건" 이라 하는 일이 생기고, 사용자는 어느 쪽도 못 믿게 된다
    (`_exception_summary` 가 예외 센터에 대해 이미 지키는 규칙과 같다).

    실패해도 리포트 전체를 막지 않는다 — 검증이 안 됐다는 사실만 남긴다.
    """
    try:
        from app.services import verification as ver

        s = ver.summary(db, org_id=org_id)
        return {
            "overall": s["overall"],
            "failed_checks": s["counts"].get(ver.FAIL, 0),
            "not_verified_checks": s["counts"].get(ver.NOT_VERIFIED, 0),
            "margin_not_calculated": s["confidence"]["no_margin_row"],
            "settlement_unmatched": s["settlement"]["by_status"].get("UNMATCHED", 0),
            "settlement_amount_differences": s["settlement"]["amount_failure_count"],
        }
    except Exception:  # noqa: BLE001 — 한 구획의 실패가 리포트를 막지 않는다
        db.rollback()
        return _unknown("검증 요약을 계산하지 못했습니다.")


# ──────────────────────────────────────────────────────────────────────────
# 확인 필요 (맨 위)
# ──────────────────────────────────────────────────────────────────────────

def _exception_summary(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """예외 센터 요약. **리포트가 스스로 세지 않는다.**

    실패해도 리포트 전체를 죽이지 않는다 — 다른 구획은 여전히 쓸모가 있다.
    """
    from app.services import exception_center

    try:
        return exception_center.summary(db, org_id=org_id)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return {"unavailable": True, "reason": type(exc).__name__}


def _needs_attention(sections: dict[str, Any]) -> list[dict[str, Any]]:
    """오늘 사람이 손대야 하는 것만.

    **순서가 의미다.** 돌이킬 수 없는 것(마감·승인)이 먼저, 관찰(마진)이 뒤다.
    """
    out: list[dict[str, Any]] = []

    issues = sections.get("issues") or {}
    if issues.get("blocking"):
        out.append({
            "kind": "MONTH_CLOSE_BLOCKED", "severity": "BLOCKING",
            "count": issues["blocking"],
            "message": f"마감을 막는 이슈 {issues['blocking']}건",
            "link": "/operations/issues?severity=BLOCKING",
        })

    approvals = sections.get("approvals") or {}
    if approvals.get("pending"):
        out.append({
            "kind": "APPROVAL_PENDING", "severity": "WARNING",
            "count": approvals["pending"],
            "message": f"승인 대기 {approvals['pending']}건",
            "link": "/approvals",
        })

    orders = sections.get("orders") or {}
    # `claims` 가 None 이면 "0건" 이 아니라 "모른다" 다. 확인 목록에 올리지 않는다.
    if orders.get("claims"):
        out.append({
            "kind": "CLAIM", "severity": "WARNING", "count": orders["claims"],
            "message": f"취소·반품 {orders['claims']}건",
            "link": "/sales",
        })

    settlement = sections.get("settlement") or {}
    if settlement.get("ambiguous"):
        # 사람이 고르면 끝나는 일. 미매칭과 **다른 줄**로 낸다.
        out.append({
            "kind": "SETTLEMENT_AMBIGUOUS", "severity": "WARNING",
            "count": settlement["ambiguous"],
            "message": f"정산 라인 선택 대기 {settlement['ambiguous']}건",
            "link": "/settlements/marketplace",
        })
    if settlement.get("unmatched"):
        out.append({
            "kind": "SETTLEMENT_UNMATCHED", "severity": "WARNING",
            "count": settlement["unmatched"],
            "message": f"정산 대사 미매칭 {settlement['unmatched']}건",
            "link": "/settlements/marketplace",
        })

    margin = sections.get("margin") or {}
    if margin.get("below_threshold"):
        out.append({
            "kind": "MARGIN_RISK", "severity": "INFO",
            "count": margin["below_threshold"],
            "message": f"마진 기준 미달 {margin['below_threshold']}건",
            "link": "/products",
        })

    integrations = sections.get("integrations") or {}
    blocked = [a for a in (integrations.get("accounts") or [])
               if a.get("state") == "CREDENTIAL_NOT_CONFIGURED"]
    if blocked:
        out.append({
            "kind": "INTEGRATION_BLOCKED", "severity": "INFO",
            "count": len(blocked),
            "message": f"자격증명이 없어 멈춘 연동 {len(blocked)}건",
            "link": "/integrations/external",
        })

    if issues.get("warning"):
        out.append({
            "kind": "OPERATION_ISSUE", "severity": "INFO",
            "count": issues["warning"],
            "message": f"확인이 필요한 운영 이슈 {issues['warning']}건",
            "link": "/operations/issues",
        })
    return out


# ──────────────────────────────────────────────────────────────────────────
# 구획
# ──────────────────────────────────────────────────────────────────────────

def _orders(db: Session, *, org_id: UUID, day: date) -> dict[str, Any]:
    """어제 하루의 주문.

    "오늘" 이 아니라 **어제**를 본다. 오늘은 아직 안 끝났고, 진행 중인 날의
    숫자를 확정된 것처럼 보여주면 사용자는 그걸로 판단하게 된다.
    """
    yesterday = day - timedelta(days=1)
    row = db.execute(text("""
        SELECT COUNT(*) AS orders,
               COALESCE(SUM(sale_price), 0) AS revenue,
               COUNT(DISTINCT marketplace) AS channels
          FROM sales_order
         WHERE org_id = :o AND deleted_at IS NULL AND sale_date = :d
    """), {"o": str(org_id), "d": yesterday}).mappings().one()

    total = int(row["orders"])
    return {
        "available": True,
        "date": yesterday.isoformat(),
        "orders": total,
        # 주문이 0건이면 매출도 0 이 맞다 — 이건 모르는 게 아니라 없는 것이다.
        "revenue": int(row["revenue"]),
        "channels": int(row["channels"]),
        # 취소·반품은 **따로 센다.** 주문과 한 칸에 묶으면 클레임 수집이
        # 실패해도 "주문·클레임 정상" 으로 보인다.
        **_claims(db, org_id=org_id, day=yesterday),
        "note": None if total else "이 날짜에 기록된 주문이 없습니다.",
    }


def _claims(db: Session, *, org_id: UUID, day: date) -> dict[str, Any]:
    """어제 확정된 취소·반품과 그 매출 영향.

    **연동이 없는 것과 0건인 것을 구분한다.** 0 으로 표시하면 "오늘 취소
    없음" 이 되고, 실제로는 수집이 안 되고 있을 수 있다.

    판정 근거는 **클레임 수집이 실제로 실행된 적이 있는가**다. 주문 동기화가
    성공했다는 것으로는 부족하다 — 둘은 다른 endpoint 이고 따로 켠다. 주문만
    들어오는 동안 클레임을 0건이라고 쓰면 매출이 부푼 채로 조용해진다.
    예외 센터와 같은 신호를 쓴다(두 화면의 숫자가 갈라지지 않게).
    """
    connected = db.execute(text("""
        SELECT 1 FROM automation_action
         WHERE org_id = :o AND action_type = 'CLAIM_SYNC' AND outcome = 'EXECUTED'
         LIMIT 1
    """), {"o": str(org_id)}).first() is not None
    if not connected:
        return {
            "claims": None,
            "claims_unavailable_reason": "클레임 수집이 아직 확인되지 않았습니다.",
            "claim_revenue_impact": None,
            "claims_unresolved": None,
        }

    row = db.execute(text("""
        SELECT COUNT(*) FILTER (WHERE status_changed_at::date = :d) AS effective_today,
               COUNT(*) FILTER (WHERE financial_effect = 'UNRESOLVED') AS unresolved
          FROM sales_claim
         WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org_id), "d": day}).mappings().one()

    impact = db.execute(text("""
        SELECT COALESCE(SUM(amount_delta), 0) FROM sales_adjustment
         WHERE org_id = :o AND deleted_at IS NULL
           AND created_at::date = :d
    """), {"o": str(org_id), "d": day}).scalar_one()

    return {
        "claims": int(row["effective_today"]),
        "claims_unavailable_reason": None,
        "claim_revenue_impact": int(impact),
        # 여기가 0 이 아니면 그만큼 매출이 아직 안 줄었다는 뜻이다.
        "claims_unresolved": int(row["unresolved"]),
    }


def _margin(db: Session, *, org_id: UUID, day: date) -> dict[str, Any]:
    """마진 감시 정책이 마지막으로 찾은 것.

    여기서 마진을 다시 계산하지 않는다 — 자동화가 계산한 결과를 읽는다.
    두 곳에서 계산하면 리포트와 화면의 숫자가 갈라진다.
    """
    _ = day
    policy = db.execute(text("""
        SELECT id, enabled FROM automation_policy
         WHERE org_id = :o AND automation_type = 'MARGIN_MONITOR'
           AND deleted_at IS NULL
    """), {"o": str(org_id)}).mappings().first()

    if policy is None or not policy["enabled"]:
        return {
            "available": False,
            "reason": "마진 감시가 켜져 있지 않습니다.",
            "below_threshold": None,
        }

    count = db.execute(text("""
        SELECT COUNT(*) FROM automation_action a
          JOIN automation_run r ON r.id = a.run_id
         WHERE r.policy_id = :p AND a.action_type = 'MARGIN_BELOW_THRESHOLD'
           AND r.started_at = (
             SELECT MAX(started_at) FROM automation_run WHERE policy_id = :p
           )
    """), {"p": str(policy["id"])}).scalar()
    return {"available": True, "below_threshold": int(count or 0)}


def _sourcing(db: Session, *, org_id: UUID, day: date) -> dict[str, Any]:
    """원가가 달라진 소싱처. 자동화 결과를 읽는다."""
    _ = day
    policy = db.execute(text("""
        SELECT id, enabled FROM automation_policy
         WHERE org_id = :o AND automation_type = 'SOURCE_COST_MONITOR'
           AND deleted_at IS NULL
    """), {"o": str(org_id)}).mappings().first()
    if policy is None or not policy["enabled"]:
        return {"available": False, "reason": "소싱 원가 감시가 켜져 있지 않습니다.",
                "diverged": None}

    count = db.execute(text("""
        SELECT COUNT(*) FROM automation_action a
          JOIN automation_run r ON r.id = a.run_id
         WHERE r.policy_id = :p
           AND a.action_type IN ('SOURCE_COST_DIVERGED', 'COST_CHANGE_PROPOSED')
           AND r.started_at = (
             SELECT MAX(started_at) FROM automation_run WHERE policy_id = :p
           )
    """), {"p": str(policy["id"])}).scalar()
    return {"available": True, "diverged": int(count or 0)}


def _settlement(db: Session, *, org_id: UUID, day: date) -> dict[str, Any]:
    """판매처 정산 대사 상태."""
    _ = day
    # `matching_status`(M055) 로 센다. **제외 처리한 행을 미매칭으로 세지
    # 않는다** — 광고비 정산행처럼 애초에 주문과 무관한 행이 계속 잡히면
    # 숫자가 영원히 줄지 않고, 그러면 사람이 이 줄을 안 본다.
    #
    # `ambiguous` 를 따로 내보내는 이유: 그건 사람이 고르면 끝나는 일이고
    # `unmatched` 는 데이터를 기다리는 일이다. 리포트가 둘을 합치면
    # "무엇을 해야 하는가" 가 사라진다.
    row = db.execute(text("""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE matching_status = 'UNMATCHED') AS unmatched,
               COUNT(*) FILTER (WHERE matching_status = 'AMBIGUOUS') AS ambiguous
          FROM marketplace_settlement_record
         WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org_id)}).mappings().one()

    if int(row["total"]) == 0:
        return {"available": False, "reason": "가져온 정산 내역이 없습니다.",
                "unmatched": None, "ambiguous": None}
    return {"available": True, "unmatched": int(row["unmatched"]),
            "ambiguous": int(row["ambiguous"]), "total": int(row["total"])}


def _cashflow(db: Session, *, org_id: UUID, day: date) -> dict[str, Any]:
    """현금 잔액. 기준선이 없으면 **모른다** — 0 으로 두지 않는다."""
    _ = day
    baseline = db.execute(text("""
        SELECT amount, as_of_date FROM cash_balance_baseline
         WHERE org_id = :o AND deleted_at IS NULL
         ORDER BY as_of_date DESC LIMIT 1
    """), {"o": str(org_id)}).mappings().first()
    if baseline is None:
        return {"available": False,
                "reason": "현금 기준 잔액이 등록되지 않아 잔액을 계산할 수 없습니다.",
                **_unknown("기준 잔액 없음")}
    return {"available": True, "baseline_amount": int(baseline["amount"]),
            "baseline_date": baseline["as_of_date"].isoformat()}


def _cost_changes(db: Session, *, org_id: UUID, day: date) -> dict[str, Any]:
    """최근 원가 version 변동.

    "급등"·"위험" 같은 판정을 붙이지 않는다 — 몇 %가 위험한지는 업무 기준이고
    우리가 정할 수 없다. **변화량 자체**를 보여주고 판단은 사람이 한다.
    """
    since = day - timedelta(days=7)
    rows = db.execute(text("""
        SELECT p.name_ko, v.purchase_price, v.currency, v.valid_from,
               prev.purchase_price AS previous_price
          FROM product_source_cost_version v
          JOIN product_source ps ON ps.id = v.product_source_id
          JOIN product p ON p.id = ps.product_id AND p.deleted_at IS NULL
          LEFT JOIN LATERAL (
            SELECT purchase_price FROM product_source_cost_version pv
             WHERE pv.product_source_id = v.product_source_id
               AND pv.valid_from < v.valid_from
             ORDER BY pv.valid_from DESC LIMIT 1
          ) prev ON TRUE
         WHERE v.org_id = :o AND v.created_at >= :since
         ORDER BY v.created_at DESC LIMIT 20
    """), {"o": str(org_id), "since": since}).mappings().all()

    changed = []
    for r in rows:
        # 첫 원가는 "변화" 가 아니다 — 비교 대상이 없다.
        if r["previous_price"] is None:
            continue
        changed.append({
            "product_name": r["name_ko"],
            "previous": int(r["previous_price"]),
            "current": int(r["purchase_price"]),
            "delta": int(r["purchase_price"]) - int(r["previous_price"]),
            "currency": r["currency"],
            "valid_from": r["valid_from"].isoformat(),
        })

    return {"available": True, "since": since.isoformat(),
            "changed_count": len(changed), "items": changed[:10],
            "first_time_count": len(rows) - len(changed)}


def _integrations(db: Session, *, org_id: UUID, day: date) -> dict[str, Any]:
    """외부 연동 상태.

    **미구현 커넥터를 "정상" 으로 보여주지 않는다.** 사용자는 연동이 돌고 있다고
    믿은 채 확인을 멈추게 된다.
    """
    _ = day
    rows = db.execute(text("""
        SELECT s.code, s.name, a.display_name, a.status,
               a.credential_secret_ref IS NOT NULL AS has_credential,
               a.last_successful_sync_at, a.last_test_status
          FROM external_account a
          JOIN external_system s ON s.id = a.external_system_id
         WHERE a.org_id = :o AND a.deleted_at IS NULL
         ORDER BY s.code
    """), {"o": str(org_id)}).mappings().all()

    from app.services import connector as conn

    accounts = []
    for r in rows:
        available = conn.has_connector(r["code"])
        accounts.append({
            "system_code": r["code"], "system_name": r["name"],
            "account": r["display_name"],
            "connector_available": available,
            "credential_configured": bool(r["has_credential"]),
            "last_successful_sync_at": (
                r["last_successful_sync_at"].isoformat()
                if r["last_successful_sync_at"] else None
            ),
            # 셋을 하나로 뭉치지 않는다 — 대응이 다르다.
            "state": (
                "CONNECTOR_UNAVAILABLE" if not available
                else "CREDENTIAL_NOT_CONFIGURED" if not r["has_credential"]
                else "READY"
            ),
        })

    from app.mcp import is_enabled as mcp_enabled

    return {
        "available": True,
        "accounts": accounts,
        "ready_count": sum(1 for a in accounts if a["state"] == "READY"),
        # MCP 는 외부 연동이 아니라 **우리 쪽 접근 통로**다. 같은 칸에 두되
        # 상태 이름을 구분한다.
        "mcp_enabled": mcp_enabled(),
    }


def _issues(db: Session, *, org_id: UUID, day: date) -> dict[str, Any]:
    _ = day
    from app.services import operation_issue

    s = operation_issue.summary(db, org_id=org_id)
    return {"available": True, "blocking": s.get("blocking", 0),
            "warning": s.get("warning", 0), "info": s.get("info", 0),
            "open_total": s.get("open_total", 0)}


def _approvals(db: Session, *, org_id: UUID, day: date) -> dict[str, Any]:
    _ = day
    row = db.execute(text("""
        SELECT COUNT(*) FILTER (WHERE status = 'PENDING') AS pending,
               COUNT(*) FILTER (WHERE status = 'PENDING'
                                  AND request_type = 'COST_CHANGE') AS cost_change
          FROM approval_request WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org_id)}).mappings().one()
    return {"available": True, "pending": int(row["pending"]),
            "cost_change_pending": int(row["cost_change"])}


def _automation(db: Session, *, org_id: UUID, day: date) -> dict[str, Any]:
    """자동화가 실제로 돌고 있나.

    "자동으로 하고 있다" 는 믿음이 틀렸을 때 가장 위험하다 — 사용자는 확인을
    멈췄는데 아무것도 안 돌고 있는 상태가 된다.
    """
    _ = day
    rows = db.execute(text("""
        SELECT p.automation_type, p.enabled, p.mode, p.last_run_at,
               (SELECT status FROM automation_run
                 WHERE policy_id = p.id ORDER BY started_at DESC LIMIT 1) AS last_status
          FROM automation_policy p
         WHERE p.org_id = :o AND p.deleted_at IS NULL
         ORDER BY p.automation_type
    """), {"o": str(org_id)}).mappings().all()

    enabled = [dict(r) for r in rows if r["enabled"]]
    failing = [r["automation_type"] for r in enabled if r["last_status"] == "FAILED"]
    never_ran = [r["automation_type"] for r in enabled if r["last_run_at"] is None]

    return {
        "available": True,
        "enabled_count": len(enabled),
        "failing": failing,
        "never_ran": never_ran,
        "policies": [
            {"automation_type": r["automation_type"], "mode": r["mode"],
             "last_run_at": r["last_run_at"].isoformat() if r["last_run_at"] else None,
             "last_status": r["last_status"]}
            for r in enabled
        ],
    }
