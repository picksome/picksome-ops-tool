"""M019 승인 요청 — 멱등 fingerprint + 자동 적용 추적.

⚠️ 번호 참고: docs/43 초안은 M019 를 `operation_issue`(Phase 4)로 잡았다.
   Phase 4 는 아직 착수하지 않았고, M018 안정화가 먼저 필요해 이 번호를 쓴다.
   `operation_issue` 는 M020 이후로 밀린다 — docs/44 에 기록했다.

무엇을 왜 추가하나
------------------
**① `request_fingerprint`** — idempotency key 안전성.
   지금은 같은 키가 오면 payload 를 보지 않고 기존 요청을 그대로 돌려준다.
   그러면 "키는 재사용했는데 내용이 다른" 요청이 조용히 무시된다. 클라이언트는
   자기 요청이 반영됐다고 믿는다. canonical JSON 의 해시를 저장해 두고
   같은 키·다른 내용이면 409 로 거부한다.

   해시만 저장한다. 원문 payload 를 중복 저장하지 않는다 — 이미
   `proposed_changes_json` 에 있고, 민감값을 두 벌 들고 있을 이유가 없다.

**② 자동 적용 추적** — 미래 `effective_from` 실행기.
   적용 실패를 상태로 만들지 않는다(승인 사실은 유효하다). 대신 시도 정보를 남긴다.
     attempt_count / last_attempt_at / last_error_code / last_error_message
     next_retry_at / auto_apply_status / applied_by_type
   `last_error_message` 에는 **SQL·비밀값·개인정보를 넣지 않는다** — 서비스가
   사용자용 문장만 기록한다.

**③ `applied_by_type`** — 사람이 눌렀는지 실행기가 했는지. 감사에서 구분돼야 한다.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M019"
down_revision: str | None = "M018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE approval_request
          ADD COLUMN IF NOT EXISTS request_fingerprint TEXT,
          ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS last_error_code TEXT,
          ADD COLUMN IF NOT EXISTS last_error_message TEXT,
          ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS auto_apply_status TEXT,
          ADD COLUMN IF NOT EXISTS applied_by_type TEXT;

        ALTER TABLE approval_request
          ADD CONSTRAINT ck_approval_auto_apply_status
          CHECK (auto_apply_status IS NULL OR auto_apply_status IN
                 ('PENDING','RETRYING','HELD_BY_LOCK','PERMANENT_FAILURE','DONE'));

        ALTER TABLE approval_request
          ADD CONSTRAINT ck_approval_applied_by_type
          CHECK (applied_by_type IS NULL OR applied_by_type IN ('USER','SYSTEM'));

        COMMENT ON COLUMN approval_request.request_fingerprint IS
          '요청 본문의 canonical JSON SHA-256. 대상 target_type/target_id/request_type/'
          'proposed_changes/effective_from 을 정렬된 JSON 으로 직렬화해 해싱한다. '
          '같은 idempotency_key 인데 이 값이 다르면 409 IDEMPOTENCY_KEY_REUSED. '
          '원문을 중복 저장하지 않고 해시만 둔다.';

        COMMENT ON COLUMN approval_request.auto_apply_status IS
          '자동 적용기 관점의 처리 상태. status(승인 상태)와 별개다. '
          'PENDING=적용 대기 / RETRYING=일시 오류 재시도 중 / HELD_BY_LOCK=월 잠금으로 보류 '
          '(자동으로 잠금을 풀지 않는다) / PERMANENT_FAILURE=재시도해도 소용없음 / DONE=적용 완료.';

        COMMENT ON COLUMN approval_request.last_error_message IS
          '사용자용 실패 문장. SQL·비밀값·개인정보를 넣지 않는다.';

        COMMENT ON COLUMN approval_request.applied_by_type IS
          'USER=사람이 적용 / SYSTEM=자동 적용기. 감사에서 구분돼야 한다.';

        -- 자동 적용기 스캔 전용 인덱스.
        -- APPROVED + effective_from 도래 + 재시도 시각 조건으로만 훑는다.
        CREATE INDEX IF NOT EXISTS ix_approval_due_for_apply
          ON approval_request (effective_from, requested_at, id)
          WHERE status = 'APPROVED' AND deleted_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS ix_approval_due_for_apply;
        ALTER TABLE approval_request
          DROP CONSTRAINT IF EXISTS ck_approval_auto_apply_status,
          DROP CONSTRAINT IF EXISTS ck_approval_applied_by_type;
        ALTER TABLE approval_request
          DROP COLUMN IF EXISTS request_fingerprint,
          DROP COLUMN IF EXISTS attempt_count,
          DROP COLUMN IF EXISTS last_attempt_at,
          DROP COLUMN IF EXISTS last_error_code,
          DROP COLUMN IF EXISTS last_error_message,
          DROP COLUMN IF EXISTS next_retry_at,
          DROP COLUMN IF EXISTS auto_apply_status,
          DROP COLUMN IF EXISTS applied_by_type;
        """
    )
