"""월 잠금 가드.

**프론트에서 버튼을 숨기는 것만으로는 부족하다.** 잠긴 월의 데이터를 바꾸는 경로는
API 를 직접 호출하면 얼마든지 열려 있다. 그래서 mutation 서비스의 진입점에서
이 함수를 호출해 백엔드가 강제한다.

호출해야 하는 곳 (docs/43_ops_stability_plan.md Phase 2):
  - 판매 등록/수정/삭제        (services/sales.py)
  - 사업비용 등록/수정/삭제    (services/expense.py)
  - 정산 매칭                  (services/marketplace_settlement.py)
  - 수익배분 규칙 변경         (services/settlement_crud.py)
  - 실 마진 재계산             (services/margin.py)

`close_state` 를 읽는다. M017 이 기존 `status` 에서 backfill 해 두었고,
신규 코드는 전부 `close_state` 를 본다.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import SettlementLockedError

# 쓰기가 막히는 상태. CONFIRMED 는 아직 수정 가능하다 —
# 확정은 "숫자를 맞췄다" 는 뜻이고, 잠금은 "더 이상 못 건드린다" 는 뜻이라 층위가 다르다.
_WRITE_BLOCKED_STATES = ("LOCKED",)


def year_month_of(value: date | str) -> str:
    """date 또는 'YYYY-MM-DD' → 'YYYY-MM'."""
    if isinstance(value, str):
        return value[:7]
    return f"{value.year:04d}-{value.month:02d}"


def get_close_state(db: Session, *, org_id: UUID, year_month: str) -> str:
    """해당 월의 마감 상태. 정산 행이 아직 없으면 OPEN 으로 본다.

    행이 없다는 건 정산을 한 번도 돌리지 않았다는 뜻이고, 그건 열린 달이다.
    """
    row = db.execute(
        text("""
            SELECT close_state FROM monthly_settlement
             WHERE org_id = :org AND year_month = :ym AND deleted_at IS NULL
             ORDER BY created_at DESC LIMIT 1
        """),
        {"org": str(org_id), "ym": year_month},
    ).first()
    return row.close_state if row else "OPEN"


# ─────────────────────────────────────────────────────────────────────────
# 사용자 데이터 쓰기 연산 화이트리스트
#
# ⚠️ 이 집합에 **우회용 값을 넣지 않는다.** 예전 구현은 `SYSTEM_OPERATIONS` 라는
#    문자열 집합을 두고 거기 속하면 잠금을 통과시켰다. 호출부가 전부 리터럴이라
#    당장 악용되지는 않았지만, `operation` 이 평범한 str 이라 나중에 어떤 경로가
#    (예: 승인 워크플로의 적용 단계가 요청 payload 의 값을 그대로 넘기면)
#    "FINALIZE_PROFIT" 을 전달하는 순간 월 잠금이 조용히 뚫린다.
#
#    지금은 **우회 경로 자체를 없앴다.** 잠금 절차 내부 연산은 이 함수를 아예
#    호출하지 않는다(아래 `assert_close_machinery_only` 주석 참조).
#    알 수 없는 operation 은 통과가 아니라 ValueError 다 — 오타나 주입이
#    "통과" 로 해석되면 안 되기 때문이다.
# ─────────────────────────────────────────────────────────────────────────
USER_WRITE_OPERATIONS = frozenset({
    "CREATE", "UPDATE", "DELETE",
    "BULK_UPDATE", "IMPORT", "SYNC", "BACKFILL",
    "RECALC_MARGIN", "RECALC_PROFIT", "ALLOCATE_AD_COST",
    "APPLY_APPROVAL",      # 승인된 변경의 실제 적용 — 명백한 사용자 데이터 쓰기다
})


def assert_period_writable(
    db: Session,
    *,
    org_id: UUID,
    effective_date: date | str,
    operation: str,
    what: str = "이 데이터",
) -> None:
    """사용자 데이터 쓰기의 **단일 진입 가드**. 우회 경로가 없다.

    컨트롤러마다 중복 검사하지 않는다. 서비스 계층에서 이 함수 하나만 부른다.

    `operation` 은 `USER_WRITE_OPERATIONS` 중 하나여야 한다.
    모르는 값이면 **통과시키지 않고 ValueError** 를 던진다.
    """
    if operation not in USER_WRITE_OPERATIONS:
        raise ValueError(
            f"알 수 없는 쓰기 연산: {operation!r}. "
            f"허용값: {sorted(USER_WRITE_OPERATIONS)}. "
            "월 잠금 우회 목적으로 임의 문자열을 넘길 수 없다."
        )
    assert_month_writable(db, org_id=org_id, at=effective_date, what=what)


def assert_close_machinery_only(caller: str) -> None:
    """잠금 절차 내부 연산임을 코드로 명시하는 표식.

    `monthly_close._transition` 과 `profit_status.finalize_month` 는 잠금 절차
    **자체**라서 월 잠금 가드를 통과할 필요가 없다 — 통과 여부를 문자열로 판정하지 않고,
    애초에 `assert_period_writable` 를 호출하지 않는다.

    이 함수는 검사를 하지 않는다. "여기는 의도적으로 가드를 부르지 않는 자리" 라는
    사실을 코드에 남겨, 나중에 읽는 사람이 누락으로 오해하지 않게 한다.
    """
    _ = caller  # 문서화 목적. 런타임 동작 없음.


def assert_month_writable(
    db: Session,
    *,
    org_id: UUID,
    at: date | str,
    what: str = "이 데이터",
) -> None:
    """잠긴 월이면 SettlementLockedError(409). 아니면 조용히 통과.

    `at` 은 판매일·비용 결제일 등 **그 데이터가 귀속되는 날짜**다.
    등록 시각(created_at)이 아니다 — 8월에 7월 비용을 입력하는 일이 흔하고,
    그건 7월이 잠겼는지로 판단해야 한다.
    """
    ym = year_month_of(at)
    state = get_close_state(db, org_id=org_id, year_month=ym)
    if state in _WRITE_BLOCKED_STATES:
        raise SettlementLockedError(
            f"{ym} 은 잠긴 월입니다. {what}를 변경할 수 없습니다. "
            f"수정하려면 관리자가 월 잠금을 해제해야 합니다.",
            detail={"year_month": ym, "close_state": state},
        )


def assert_months_writable(
    db: Session,
    *,
    org_id: UUID,
    dates: list[date | str],
    what: str = "이 데이터",
) -> None:
    """여러 날짜를 한 번에 검사. 대량 import 처럼 여러 월이 섞이는 경우.

    월 단위로 중복을 제거해서 조회 횟수를 줄인다 — 1,000행 import 에서
    행마다 조회하면 쿼리가 1,000번 나간다.
    """
    seen: set[str] = set()
    for d in dates:
        ym = year_month_of(d)
        if ym in seen:
            continue
        seen.add(ym)
        assert_month_writable(db, org_id=org_id, at=f"{ym}-01", what=what)
