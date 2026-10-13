"""검증센터 API — 읽기 전용이 기본이다.

권한
----
재무 성분(원가·마진·수수료)을 그대로 보여주므로 **마진 화면과 같은 권한**을
쓴다(`api/v1/margin.py`). 새 권한 등급을 만들지 않는다.

`EXTERNAL_ACCOUNTANT` 는 제외한다 — 마진 화면이 이미 제외하고 있고, 여기만
열어 주면 같은 데이터가 다른 문으로 새는 셈이다.

쓰기는 하나뿐
-----------
기준값 승인만 쓰기다. 재계산·원가 수정·정산 재매칭은 여기서 하지 않는다 —
검증은 근거를 보여주는 곳이고, 고치는 것은 각자의 명시적 흐름이 한다.

개인정보
-------
응답에 구매자·연락처·주소가 없다. 검증에 필요한 것은 주문 식별자와 금액이다.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.services import golden_evidence as evidence
from app.services import verification as ver
from app.services import verification_golden as golden

router = APIRouter(prefix="/verification", tags=["verification"])

#: 마진 화면과 같은 조합 (`api/v1/margin.py`).
_read = require_roles("ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY")
#: 기준값 승인은 재무 판단이다. 읽기 전용 역할은 승인할 수 없다.
_approve = require_roles("ADMIN", "ACCOUNTING")


class GoldenApproveBody(BaseModel):
    """사람이 확인한 기준값.

    **서버가 채워 주지 않는다.** 값이 비면 거부한다 — 현재 계산 결과로
    채우는 편의를 두는 순간 회귀 검사가 무의미해진다.
    """

    expected: dict[str, int | None] = Field(
        description="확인한 항목만. 모르는 항목은 빼거나 null.")
    source_evidence: str = Field(
        min_length=1, max_length=500,
        description="무엇을 보고 확인했는지 (예: 쿠팡 정산서 2026-01.xlsx)")
    change_reason: str | None = Field(
        default=None, max_length=1000,
        description="이미 승인된 기준값을 바꿀 때 필수")


class RegisterCandidatesBody(BaseModel):
    sales_order_ids: list[UUID] = Field(min_length=1, max_length=50)


class GoldenDeferBody(BaseModel):
    """보류. **실패가 아니다** — "봤고 지금은 못 정한다" 이다."""

    reason: str = Field(description="보류 사유 코드")
    note: str | None = Field(default=None, max_length=1000)


@router.get("/summary")
def summary(
    period_from: date | None = Query(default=None),
    period_to: date | None = Query(default=None),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """검증 한 판. PASS / FAIL / NOT_VERIFIED 를 섞지 않고 그대로 돌려준다."""
    return envelope(ver.summary(
        db, org_id=user.org_id, period_from=period_from, period_to=period_to))


@router.get("/orders/{sales_order_id}")
def order_trace(
    sales_order_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """주문 하나의 계산 추적. 성분마다 출처와 신뢰도가 붙는다."""
    return envelope(ver.order_trace(
        db, org_id=user.org_id, sales_order_id=sales_order_id))


@router.get("/exceptions")
def exceptions(
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """확인이 필요한 것만.

    **새 예외 체계를 만들지 않는다.** 기존 운영 이슈를 그대로 가져오고,
    대사로만 알 수 있는 것을 덧붙인다.
    """
    from app.services import operation_issue as issues

    org_id = user.org_id
    sync = ver.sync_reconciliation(db, org_id=org_id)
    settlement = ver.settlement_reconciliation(db, org_id=org_id)
    integ = ver.integrity(db, org_id=org_id)
    unknown = ver.unknown_treated_as_zero(db, org_id=org_id)
    confidence = ver.margin_confidence(db, org_id=org_id)

    items: list[dict[str, Any]] = []

    def add(code: str, n: int, detail: str, severity: str = "WARNING") -> None:
        if n:
            items.append({"code": code, "count": n, "detail": detail,
                          "severity": severity})

    add("SYNC_RECONCILIATION_FAILED", sync["reconciliation_failures"],
        "수집 실행에서 읽은 건수와 처리한 건수가 맞지 않는다", "BLOCKING")
    add("SYNC_RUN_NOT_SUCCEEDED", sync["runs_not_succeeded"],
        "성공으로 끝나지 않은 수집 실행이 있다")
    add("SETTLEMENT_AMOUNT_DIFFERENCE", settlement["amount_failure_count"],
        "정산서 금액과 재구성한 금액이 원 단위로 다르다", "BLOCKING")
    add("SETTLEMENT_UNMATCHED", settlement["by_status"].get("UNMATCHED", 0),
        "붙일 주문을 찾지 못한 정산 행")
    add("SETTLEMENT_AMBIGUOUS", settlement["by_status"].get("AMBIGUOUS", 0),
        "후보가 여럿이라 자동으로 고를 수 없는 정산 행")
    add("MARGIN_NOT_CALCULATED", confidence["no_margin_row"],
        "실 마진이 아직 계산되지 않은 주문")
    add("CALCULATION_TRACE_INCOMPLETE", confidence["without_calculation_evidence"],
        "계산 근거가 박제돼 있지 않아 왜 이 숫자인지 되짚을 수 없는 주문")
    add("UNKNOWN_COST", unknown["checks"]["unknown_cost_as_zero"],
        "원가를 모르는 채 0원으로 계산된 주문")
    add("UNKNOWN_FEE", unknown["checks"]["unknown_fee_as_zero"],
        "수수료를 모르는 채 0원으로 계산된 주문")
    add("REFUND_NOT_VERIFIED", unknown["checks"]["claim_without_refund_settlement"],
        "취소·반품은 있는데 환불 정산 근거가 없어 마진이 확정이 아닌 주문")
    for name, n in integ["checks"].items():
        add(name.upper(), n, "무결성 검사에서 걸린 행", "BLOCKING")

    return envelope({
        "verification": items,
        # 기존 이슈 센터. 여기서 클릭하면 그쪽 상세로 간다.
        "operation_issues": issues.summary(db, org_id=org_id),
    })


# ── Golden Sample ────────────────────────────────────────────────────

@router.get("/golden-samples")
def golden_samples(
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """등록된 기준 사례와 회귀 결과 · 경로별 커버리지."""
    return envelope({
        "cases": golden.list_cases(db, org_id=user.org_id),
        "regression": golden.regression(db, org_id=user.org_id),
        "coverage": golden.coverage(db, org_id=user.org_id),
        "defer_reasons": list(golden.DEFER_REASONS),
    })


@router.get("/golden-samples/evidence/{sales_order_id}")
def golden_evidence_for_order(
    sales_order_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """한 후보의 원천 · 독립 검산 · 차이를 한 번에 (§5).

    사용자가 엑셀을 열거나 계산기를 두드리지 않아도 되도록, 화면 하나에
    필요한 것을 모두 담아 돌려준다.

    **독립 검산은 production 마진 계산기를 부르지 않는다** — 부르면 그
    함수가 틀려도 기대값이 같이 틀려서 검증이 항상 통과한다.
    """
    return envelope(evidence.build(
        db, org_id=user.org_id, sales_order_id=sales_order_id))


@router.get("/golden-samples/recommended")
def golden_recommended(
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """"이것만 먼저 확인하면 주요 계산 경로를 덮습니다" (§32)."""
    candidates = golden.propose_candidates(db, org_id=user.org_id, limit=50)
    return envelope(golden.recommend_minimum_set(
        db, org_id=user.org_id, candidates=candidates))


@router.get("/golden-samples/candidates")
def golden_candidates(
    limit: int = Query(default=20, ge=1, le=50),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """확인할 만한 주문 후보. **저장하지도 승인하지도 않는다.**"""
    return envelope({
        "candidates": golden.propose_candidates(
            db, org_id=user.org_id, limit=limit),
    })


@router.post("/golden-samples/candidates")
def register_candidates(
    body: RegisterCandidatesBody,
    user: CurrentUser = Depends(_approve),
    db: Session = Depends(get_db),
) -> dict:
    """후보를 사례로 등록한다. 기준값 없이 `CANDIDATE` 로만 만들어진다."""
    proposed = {
        c["sales_order_id"]: c
        for c in golden.propose_candidates(db, org_id=user.org_id, limit=50)
    }
    picked = [proposed[str(i)] for i in body.sales_order_ids if str(i) in proposed]
    if not picked:
        from app.core.exceptions import ValidationError
        raise ValidationError("후보 목록에 없는 주문입니다. 후보를 다시 불러오세요.")
    return envelope(golden.register_candidates(
        db, org_id=user.org_id, actor_user_id=user.user_id, candidates=picked))


@router.post("/golden-samples/{case_id}/approve")
def approve_golden(
    case_id: UUID,
    body: GoldenApproveBody,
    user: CurrentUser = Depends(_approve),
    db: Session = Depends(get_db),
) -> dict:
    """사람이 확인한 기준값을 넣는다.

    서버가 현재 계산 결과로 채워 주는 경로는 없다. 값은 호출자가 준다.

    승인 순간의 근거를 함께 박제한다 (§15). 나중에 원천이 바뀌어도 "그때
    무엇을 보고 승인했는지" 가 남아야, 회귀 실패 원인을 가릴 수 있다.
    **회귀 비교는 여전히 `expected` 하나만 본다** — 근거는 읽기 전용이다.
    """
    case = db.execute(text("""
        SELECT sales_order_id FROM verification_golden_case
         WHERE id = :i AND org_id = :o AND deleted_at IS NULL
    """), {"i": str(case_id), "o": str(user.org_id)}).scalar()
    snapshot = None
    if case is not None:
        try:
            snapshot = evidence.build(
                db, org_id=user.org_id, sales_order_id=UUID(str(case)))
        except Exception:  # noqa: BLE001 — 근거를 못 모아도 승인 자체는 막지 않는다
            db.rollback()
            snapshot = None

    return envelope(golden.approve(
        db, org_id=user.org_id, case_id=case_id, actor_user_id=user.user_id,
        expected=dict(body.expected), source_evidence=body.source_evidence,
        change_reason=body.change_reason, evidence_snapshot=snapshot))


@router.post("/golden-samples/{case_id}/defer")
def defer_golden(
    case_id: UUID,
    body: GoldenDeferBody,
    user: CurrentUser = Depends(_approve),
    db: Session = Depends(get_db),
) -> dict:
    """보류. 계산 오류가 아니라 "지금은 판단할 수 없다" 이다."""
    return envelope(golden.defer(
        db, org_id=user.org_id, case_id=case_id, actor_user_id=user.user_id,
        reason=body.reason, note=body.note))


@router.get("/golden-samples/regression")
def golden_regression(
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """승인된 기준값 vs 지금 계산 결과. 원 단위로 정확히 비교한다."""
    return envelope(golden.regression(db, org_id=user.org_id))
