"""M018 승인 워크플로 — approval_request.

배경 (docs/43 §2 Phase 3, §8)
  권한이 있어도 중요한 값은 바로 적용되지 않아야 한다. 원가·판매가·정책 변경은
  누군가 한 번 더 보고 나서 반영되는 게 맞다.

설계 판단
---------
**단일 공통 테이블.** 엔티티별 승인 테이블을 만들지 않는다. 승인 흐름은 대상이
무엇이든 동일하고(요청 → 검토 → 적용), 대상별로 나누면 "지금 내가 승인할 게 몇 건인가"
를 물어보려고 테이블 N개를 UNION 해야 한다.

**승인 요청은 immutable proposal 이다.** 상태 레코드가 아니다.
  - `before_snapshot_json` : 요청 시점의 대상 값 (박제)
  - `proposed_changes_json`: 바꾸려는 값
  - `base_version`         : 요청 시점 대상의 지문. 적용 직전에 다시 비교한다.
  PENDING 이후에는 target/request_type/proposed_changes 를 바꾸지 못한다
  (서비스 상태 머신이 강제. DB CHECK 로 과도하게 구현하지 않는다).

**상태는 TEXT + CHECK.** M016/M017 과 같은 이유 (docs/43 §1-2).
  enum 은 값 제거가 불가능해 downgrade 가 원상복구되지 않고, 추가한 값을 같은
  마이그레이션의 backfill 에 쓸 수도 없다.

**전이 규칙은 DB 가 아니라 서비스가 검증한다.** CHECK 로 전이표를 흉내 내면
  (예: APPLIED 면 applied_at NOT NULL) 규칙이 두 곳에 흩어지고,
  정책이 바뀔 때마다 마이그레이션을 새로 파야 한다.
  DB 에는 **되돌릴 수 없는 사실**만 제약으로 건다 — 상태값 도메인, 유일성, FK.

JSONB 3개는 임의 저장소가 아니다. 구조를 아래 COMMENT 에 고정한다.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M018"
down_revision: str | None = "M017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE approval_request (
          id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id                   UUID NOT NULL REFERENCES organization(id),
          business_key             TEXT NOT NULL,

          -- 대상 식별
          target_type              TEXT NOT NULL,
          target_id                UUID,
          request_type             TEXT NOT NULL,

          -- 상태
          status                   TEXT NOT NULL DEFAULT 'DRAFT',

          -- 사람과 시각
          requested_by             UUID NOT NULL REFERENCES app_user(id),
          requested_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
          submitted_at             TIMESTAMPTZ,
          reviewed_by              UUID REFERENCES app_user(id),
          reviewed_at              TIMESTAMPTZ,
          applied_by               UUID REFERENCES app_user(id),
          applied_at               TIMESTAMPTZ,
          cancelled_at             TIMESTAMPTZ,

          -- 사유
          reason                   TEXT,
          review_comment           TEXT,
          apply_error              TEXT,

          -- proposal 본문
          before_snapshot_json     JSONB,
          proposed_changes_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
          approval_policy_snapshot_json JSONB,

          -- 낙관적 동시성
          base_version             TEXT,
          effective_from           DATE,
          version                  INTEGER NOT NULL DEFAULT 1,

          -- 멱등성
          idempotency_key          TEXT,

          created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by               UUID,
          updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by               UUID,
          deleted_at               TIMESTAMPTZ,
          deleted_by               UUID,

          CONSTRAINT ck_approval_status CHECK (
            status IN ('DRAFT','PENDING','APPROVED','REJECTED','APPLIED','CANCELLED')
          )
        );

        COMMENT ON TABLE approval_request IS
          '승인 워크플로 공통 테이블. 엔티티별로 나누지 않는다 — 흐름이 동일하고, '
          '나누면 "내가 승인할 건" 을 물어볼 때 테이블 N개를 UNION 해야 한다.';

        COMMENT ON COLUMN approval_request.status IS
          'DRAFT=작성중 / PENDING=승인대기 / APPROVED=승인됨(아직 미적용) / '
          'REJECTED=반려 / APPLIED=실제 데이터 반영 완료 / CANCELLED=취소. '
          '전이 규칙은 services/approval.py 의 상태 머신이 검증한다 — DB CHECK 는 도메인만 강제.';

        COMMENT ON COLUMN approval_request.before_snapshot_json IS
          '요청 시점 대상 값. target adapter 의 build_snapshot() 이 만든 평면 dict. '
          '민감정보(비밀번호/API키/webhook URL)는 어댑터가 마스킹해 넣는다. '
          '구조 예) product 는 name_ko, cost_amount, sale_price 등 변경 대상 필드만.';

        COMMENT ON COLUMN approval_request.proposed_changes_json IS
          '바꾸려는 값. before_snapshot 과 **같은 키 집합의 부분집합**이어야 한다. '
          '임의 payload 저장소가 아니다 — 어댑터의 validate_proposed_changes() 가 키를 검증한다.';

        COMMENT ON COLUMN approval_request.approval_policy_snapshot_json IS
          '적용된 승인 정책 박제. 구조: self_approve_allowed(bool), '
          'required_roles(text[]), threshold_used(object), policy_source(text). '
          '나중에 조직 설정이 바뀌어도 "그때 어떤 규칙으로 승인했는지" 를 재현한다.';

        COMMENT ON COLUMN approval_request.base_version IS
          '요청 시점 대상의 지문(어댑터의 get_current_version()). 적용 직전에 다시 비교한다. '
          '다르면 그 사이 누가 대상을 바꿨다는 뜻이므로 자동 적용하지 않고 409 로 거부한다.';

        COMMENT ON COLUMN approval_request.version IS
          '이 요청 행 자체의 낙관적 잠금 값. 상태 전이 때마다 +1 되고, '
          'UPDATE 는 읽은 version 과 일치할 때만 성공한다(compare-and-swap).';

        COMMENT ON COLUMN approval_request.apply_error IS
          '적용 실패 사유. 적용에 실패해도 status 는 APPROVED 로 남는다 — '
          '승인이라는 사실은 사라지면 안 되고, 실패는 상태가 아니라 사건이다.';

        COMMENT ON COLUMN approval_request.effective_from IS
          'NULL = 승인 즉시 적용. 미래 날짜 = 그 날짜부터 적용(스케줄러가 처리).';

        -- ── 인덱스 ──────────────────────────────────────────────
        CREATE INDEX ix_approval_org_status
          ON approval_request (org_id, status, requested_at DESC, id);

        CREATE INDEX ix_approval_org_target
          ON approval_request (org_id, target_type, target_id, requested_at DESC);

        CREATE INDEX ix_approval_requested_by
          ON approval_request (org_id, requested_by, requested_at DESC);

        CREATE INDEX ix_approval_requested_at
          ON approval_request (org_id, requested_at DESC, id);

        -- 승인 대기함 전용. PENDING 만 담아 인덱스를 작게 유지한다.
        CREATE INDEX ix_approval_pending_queue
          ON approval_request (org_id, requested_at, id)
          WHERE status = 'PENDING' AND deleted_at IS NULL;

        -- ── 유일성 ──────────────────────────────────────────────
        -- I-2: 같은 대상·같은 요청유형에 열린 요청은 최대 1건.
        -- 애플리케이션 검사만으로는 동시 요청을 못 막는다.
        CREATE UNIQUE INDEX ux_approval_open_target
          ON approval_request (org_id, target_type, target_id, request_type)
          WHERE status IN ('DRAFT','PENDING','APPROVED') AND deleted_at IS NULL;

        -- 멱등성: 같은 키로 재요청해도 행이 두 개 생기지 않는다.
        CREATE UNIQUE INDEX ux_approval_idempotency
          ON approval_request (org_id, idempotency_key)
          WHERE idempotency_key IS NOT NULL AND deleted_at IS NULL;

        CREATE UNIQUE INDEX ux_approval_business_key
          ON approval_request (org_id, business_key)
          WHERE deleted_at IS NULL;
        """
    )


def downgrade() -> None:
    # 신규 테이블이므로 통째로 제거하면 원상복구된다.
    # 이 테이블에만 있던 승인 이력은 사라진다 — M016/M017 과 같은 성질이며
    # docs/44 P0.5 §2 의 "downgrade 는 무손실이 아니다" 원칙이 그대로 적용된다.
    op.execute("DROP TABLE IF EXISTS approval_request")
