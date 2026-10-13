"""M020 승인 결정 결과 보존 — decision_outcome.

문제
----
`REJECTED → CANCELLED` 전이를 허용하고 있다(목록 정리용 아카이빙).
그런데 **현재 상태만 집계하면 반려 건이 취소로 분류된다.**
"이번 달 반려 5건" 을 세려고 `WHERE status='REJECTED'` 를 쓰면,
아카이빙된 반려 건이 빠져 통계가 틀어진다.

선택지 비교 (docs/44)
--------------------
A. `decision_outcome` 별도 컬럼   ← **채택**
B. audit_log 의 PENDING→REJECTED 전이로 집계
   → 쿼리가 복잡하고 감사 로그 보존 정책에 종속된다
C. 기존 `reviewed_*` 재사용
   → `reviewed_at` 은 승인·반려 모두에 채워져 결과를 구분하지 못한다.
     `review_comment` 로 추정하는 건 문자열 추론이라 안 된다

A 를 고른 이유: **한 번 정해진 결정은 이후 상태 변화와 무관하게 유지돼야 한다.**
상태(`status`)는 "지금 어디에 있나", 결정(`decision_outcome`)은 "무엇으로 판정됐나" 다.
층위가 달라서 한 컬럼으로 합칠 수 없다.

backfill
--------
기존 행은 현재 상태에서 결정을 유추한다.
  REJECTED                     → REJECTED
  APPROVED / APPLIED           → APPROVED
  그 외(DRAFT/PENDING/CANCELLED) → NONE
과거에 반려 후 취소된 행은 이 시점에 구분할 수 없다(그 정보가 없었다).
audit_log 로 보정하지 않는다 — 추정으로 통계를 만들면 그게 더 위험하다.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M020"
down_revision: str | None = "M019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE approval_request
          ADD COLUMN IF NOT EXISTS decision_outcome TEXT;

        UPDATE approval_request
           SET decision_outcome = CASE
                 WHEN status = 'REJECTED' THEN 'REJECTED'
                 WHEN status IN ('APPROVED', 'APPLIED') THEN 'APPROVED'
                 ELSE 'NONE'
               END
         WHERE decision_outcome IS NULL;

        ALTER TABLE approval_request
          ALTER COLUMN decision_outcome SET DEFAULT 'NONE',
          ALTER COLUMN decision_outcome SET NOT NULL;

        ALTER TABLE approval_request
          ADD CONSTRAINT ck_approval_decision_outcome
          CHECK (decision_outcome IN ('NONE', 'APPROVED', 'REJECTED'));

        COMMENT ON COLUMN approval_request.decision_outcome IS
          '검토 결정 결과. status 와 층위가 다르다 — status 는 "지금 어디에 있나", '
          'decision_outcome 은 "무엇으로 판정됐나". '
          '한 번 REJECTED 로 정해지면 이후 CANCELLED 로 아카이빙돼도 유지된다. '
          '반려 통계는 status 가 아니라 이 컬럼으로 센다.';

        CREATE INDEX IF NOT EXISTS ix_approval_decision
          ON approval_request (org_id, decision_outcome, reviewed_at DESC)
          WHERE deleted_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS ix_approval_decision;
        ALTER TABLE approval_request
          DROP CONSTRAINT IF EXISTS ck_approval_decision_outcome;
        ALTER TABLE approval_request
          DROP COLUMN IF EXISTS decision_outcome;
        """
    )
