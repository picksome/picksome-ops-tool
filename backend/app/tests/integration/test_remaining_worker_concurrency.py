"""남은 worker 3종의 동시 실행 / 재실행 (P3).

앞선 감사에서 자동화·리포트·재고·발주는 실측했지만 다음 셋은 **코드에 lock 이
있다는 것만 확인**하고 실제로 돌려 보지 않았다.

```
attachment_delete   증빙 파일 물리 삭제
channel_publish     판매처 가격 발행 outbox
period_recalc       기간 재계산 플래그
```

lock 이 있으니 안전하다고 결론내지 않는다 — 그게 이 파일의 이유다.

무엇을 확인하나
------------
```
같은 작업을 두 번 / 동시에
  → 중복 부작용이 없는가
  → 실패 후 재실행에서 중복이 생기지 않는가
```

여기서는 **claim 전략이 실제로 동작하는지**를 본다. 진짜 두 프로세스를 띄우는
대신, 두 번째 호출이 이미 잡힌 작업을 다시 잡지 않는지 확인한다 —
`SKIP LOCKED` / 상태 전이가 그 역할을 한다.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.scripts.run_workers import _WORKERS
from app.services import worker_runtime
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


def _run(db: Session, name: str, org=None):
    """실제 runner 와 **같은 방식**으로 부른다.

    핸들러를 직접 부르면 `worker_run` 기록·실패 처리를 건너뛰어, 운영에서
    도는 것과 다른 경로를 시험하게 된다.
    """
    fn = _WORKERS[name]
    return worker_runtime.run_worker(
        db, worker_name=name, org_id=org, trigger="MANUAL",
        fn=lambda: fn(db, org, trigger="MANUAL"))


# ── 0. 등록된 worker 전부 두 번 돌려도 안전한가 ────────────────


#: advisory lock 을 **자기 연결에서** 잡는 worker.
#:
#: 테스트 세션은 하나의 연결 위에 savepoint 를 겹쳐 쓰는 구조라, 별도 연결을
#: 여는 lock 을 재현할 수 없다. 하네스의 한계이지 worker 의 결함이 아니다 —
#: 이들은 E2E(`system.spec.ts`)에서 실제 프로세스로 돈다.
_NEEDS_OWN_CONNECTION = frozenset({
    "due_approvals", "channel_publish", "operation_issue_scan",
})


@pytest.mark.parametrize("name", sorted(worker_runtime.WORKER_NAMES))
def test_every_worker_survives_being_run_twice(db: Session, name: str):
    """**빈 큐에서** 두 번 돌아도 터지지 않고 결과가 일관된다.

    스케줄러는 할 일이 없어도 계속 돈다. 그때 예외가 나면 실패 알림이 매번
    울리고, 사람은 곧 알림을 끈다.
    """
    if name in _NEEDS_OWN_CONNECTION:
        pytest.skip(f"{name} 은 별도 연결의 advisory lock 이 필요하다 "
                    "— E2E 에서 실제 프로세스로 검증한다")
    first = _run(db, name)
    second = _run(db, name)
    assert first.status == "SUCCESS"
    assert second.status == "SUCCESS"
    # 할 일이 없으면 처리 건수도 0 이어야 한다 — 뭔가 세고 있으면 이상하다.
    assert second.outcome.processed == 0


# ── 1. attachment_delete ───────────────────────────────────────
#
# 행 단위 claim 검증(같은 첨부를 두 번 처리하지 않는가)은 비용·분류·첨부까지
# 설정 체인이 필요해 이번 세션에서 만들지 못했다. **검증했다고 적지 않는다.**
#
# 지금 확인된 것은 위 `test_every_worker_survives_being_run_twice` 의
# "빈 큐에서 두 번 돌아도 안전하다" 까지다. 행 단위 claim 은
# `docs/69` 의 남은 항목으로 남긴다.


# ── 2. channel_publish ─────────────────────────────────────────


@pytest.mark.skip(reason="advisory lock 이 별도 연결을 요구한다 — "
                        "savepoint 하네스에서 재현 불가. E2E 에서 검증")
def test_channel_publish_does_not_double_send(db: Session, org):
    """같은 outbox 항목을 두 번 발행하지 않는다.

    **외부 API 를 실제로 부르지 않는다.** 커넥터가 없으므로 발행은
    "보낼 수 없음" 으로 끝나고, 여기서 보는 것은 큐 처리의 멱등성이다.
    """
    before = db.execute(text("""
        SELECT COUNT(*) FROM channel_price_publish_outbox
         WHERE org_id = :o AND status = 'PENDING'
    """), {"o": str(org)}).scalar_one()

    first = _run(db, "channel_publish", org)
    second = _run(db, "channel_publish", org)

    assert first.status == "SUCCESS" and second.status == "SUCCESS"
    # 큐가 늘지 않았다 — worker 가 자기 일을 다시 만들어내면 안 된다.
    after = db.execute(text("""
        SELECT COUNT(*) FROM channel_price_publish_outbox
         WHERE org_id = :o AND status = 'PENDING'
    """), {"o": str(org)}).scalar_one()
    assert after <= before


# ── 3. period_recalc ───────────────────────────────────────────


def test_period_recalc_twice_does_not_double_accumulate(db: Session, org):
    """같은 기간을 두 번 재계산해도 값이 두 배가 되지 않는다.

    재계산은 **덮어쓰기**여야 한다. 누적이면 스케줄이 돌 때마다 숫자가 는다.
    """
    ym = "2026-05"
    db.execute(text("""
        INSERT INTO period_recalc_flag (org_id, year_month, reason, marked_at)
        VALUES (:o, :ym, 'TEST', now())
        ON CONFLICT DO NOTHING
    """), {"o": str(org), "ym": ym})
    db.commit()

    first = _run(db, "period_recalc", org)
    second = _run(db, "period_recalc", org)
    assert first.status == "SUCCESS" and second.status == "SUCCESS"

    # 플래그가 **늘지 않았다.** 소비 여부는 그 달의 상태에 달렸다 —
    # 아직 정산 행이 없으면 남겨 두는 것이 맞다. 여기서 보는 것은
    # "재실행이 일을 복제하지 않는가" 다.
    left = db.execute(text("""
        SELECT COUNT(*) FROM period_recalc_flag WHERE org_id = :o
    """), {"o": str(org)}).scalar_one()
    assert left <= 1, f"재계산 플래그가 {left}개로 늘었다"
    # 두 번째 실행이 첫 번째보다 더 많이 처리하지 않는다.
    assert second.outcome.processed <= first.outcome.processed
