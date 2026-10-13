"""자동화 정책 · 실행 · 일일 리포트 (M045).

권한
----
```
조회        ADMIN / OPERATOR / ACCOUNTING
정책 변경    ADMIN
수동 실행    ADMIN / OPERATOR
리포트 조회  ADMIN / OPERATOR / ACCOUNTING
```

정책 변경이 ADMIN 인 이유: 자동 실행 범위를 정하는 일이라 "무엇이 저절로
바뀌는가" 를 결정한다.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.services import automation as auto
from app.services import automation_runners as runners

router = APIRouter(prefix="/automation", tags=["automation"])

_view = require_roles("ADMIN", "OPERATOR", "ACCOUNTING")
_manage = require_roles("ADMIN")
_run = require_roles("ADMIN", "OPERATOR")


class PolicyIn(BaseModel):
    automation_type: str
    name: str | None = Field(None, max_length=100)
    enabled: bool = False
    mode: str = "REPORT_ONLY"
    interval_minutes: int | None = Field(None, ge=5, le=10_080)
    guardrails: dict[str, Any] = Field(default_factory=dict)
    scope: dict[str, Any] = Field(default_factory=dict)


@router.get("/types")
def list_types(user: CurrentUser = Depends(_view)) -> dict:
    """자동화할 수 있는 것과 **하지 않는 것**.

    "왜 지급 자동화가 없지" 를 묻지 않도록 제외 목록도 함께 준다.
    """
    _ = user
    return envelope({
        "types": auto.list_types(),
        "never_automated": [
            {"action": a,
             "reason": "되돌릴 수 없는 일입니다. 사람이 화면에서 처리합니다."}
            for a in auto.NEVER_AUTOMATED
        ],
    })


@router.get("/policies")
def list_policies(
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    """등록된 정책 + 아직 안 만든 유형(꺼진 상태로).

    목록에 없는 자동화는 사용자에게 존재하지 않는 것과 같다.
    """
    rows = auto.list_policies(db, org_id=user.org_id)
    return envelope(rows, {"total": len(rows)})


@router.put("/policies")
def upsert_policy(
    payload: PolicyIn,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> dict:
    """정책 생성·수정. 유형당 하나다."""
    return envelope(auto.upsert_policy(
        db, org_id=user.org_id, actor_user_id=user.user_id, **payload.model_dump(),
    ))


@router.post("/policies/{policy_id}/run")
def run_now(
    policy_id: UUID,
    user: CurrentUser = Depends(_run),
    db: Session = Depends(get_db),
) -> dict:
    """지금 한 번 돌린다.

    꺼진 정책도 돌릴 수 있다 — 켜기 전에 "무엇이 잡히는지" 를 보고 싶기
    때문이다. 다만 그 실행은 `SKIPPED` 로 남는다.
    """
    policy = auto.get_policy(db, org_id=user.org_id, policy_id=policy_id)
    result = runners.run_policy(
        db, org_id=user.org_id, policy=policy, actor_user_id=user.user_id,
        trigger="MANUAL",
    )
    return envelope(result.as_dict())


@router.get("/runs")
def list_runs(
    policy_id: UUID | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    rows = auto.run_history(db, org_id=user.org_id, policy_id=policy_id, limit=limit)
    return envelope(rows, {"total": len(rows)})


@router.get("/runs/{run_id}/actions")
def list_actions(
    run_id: UUID,
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    """무엇을 했는지 하나씩.

    건수만으로는 알 수 없다 — 특히 `BLOCKED` 는 "한도에 걸려 안 했다" 라서
    "아무 일도 없었다" 와 다르다.
    """
    rows = auto.run_actions(db, org_id=user.org_id, run_id=run_id)
    return envelope(rows, {"total": len(rows)})


@router.get("/daily-report")
def daily_report(
    report_date: date | None = Query(None),
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    """일일 운영 리포트 — 결정적 데이터셋.

    AI 를 부르지 않는다. 숫자는 기존 서비스가 계산한 것이고 여기서는 모은다.
    AI 요약은 이 결과 **위에** 얹는다.
    """
    from app.services import daily_report as report

    return envelope(report.build(db, org_id=user.org_id, report_date=report_date))


@router.post("/run-due")
def run_due(
    user: CurrentUser = Depends(_run),
    db: Session = Depends(get_db),
) -> dict:
    """주기가 된 정책을 전부 실행한다.

    스케줄러가 부르는 것과 같은 경로다. 사람이 화면에서 "지금 돌려" 를 눌러도
    같은 코드가 돈다 — 두 경로가 갈라지면 "수동은 되는데 자동은 안 된다" 가
    생긴다.

    **하나가 실패해도 나머지는 돈다.** 실패를 세어서 요약에 남긴다.
    """
    from app.services import worker_runtime as rt

    def _scan() -> rt.WorkerOutcome:
        due = runners.due_policies(db, org_id=user.org_id)
        results = []
        for policy in due:
            try:
                results.append(runners.run_policy(
                    db, org_id=user.org_id, policy=policy,
                    actor_user_id=user.user_id, trigger="SCHEDULED",
                ).as_dict())
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                results.append({"automation_type": policy["automation_type"],
                                "status": "FAILED",
                                "error_code": type(exc).__name__})
        failed = sum(1 for r in results if r.get("status") == "FAILED")
        return rt.WorkerOutcome(
            processed=len(due), success=len(results) - failed, failure=failed,
            summary={"results": results},
        )

    record = rt.run_worker(
        db, worker_name="automation_scan", org_id=user.org_id,
        trigger="API", fn=_scan,
    )
    return envelope(record.as_dict())
