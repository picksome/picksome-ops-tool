"""M027 — 증빙 파일 실제 저장 + 거래처 활성 플래그.

`expense_attachment` 는 M001 부터 있었지만 **껍데기**였다.
`storage_path`·`file_hash` 컬럼만 있고 어디에도 쓰이지 않았다 —
화면은 브라우저 메모리에만 파일을 올렸고, 새로고침하면 사라졌다.

무엇을 더하나
------------
1. **조직 격리** — `org_id`. `expense` 를 조인해야만 조직을 알 수 있으면
   다운로드 권한 검사가 매번 조인에 의존한다. 파일은 조직 경계를 넘으면 안 되는
   자산이라 행 자체가 소유 조직을 들고 있어야 한다.
2. **수명 상태** — `status`. 삭제는 두 단계다(§아래).
3. **스캔 상태** — `scan_status`. **스캐너가 없다.** 그래서 기본값이
   `SCAN_UNAVAILABLE` 이다. 검사하지 않은 파일을 `CLEAN` 으로 표시하면
   사용자는 검사를 통과했다고 믿는다.
4. **삭제 재시도** — `delete_attempt_count` / `delete_next_attempt_at` / `delete_last_error`.

왜 별도 outbox 테이블을 만들지 않았나
-----------------------------------
`channel_price_publish_outbox`(M024) 는 별도 테이블이어야 했다. 한 가격 버전이
**여러 판매처 계정으로** 나가고, 시도 이력이 각각 남아야 하며, 대상(가격)과
작업(전송)의 수명이 다르기 때문이다.

첨부 삭제는 다르다. 한 첨부에 대해 **동시에 존재할 수 있는 삭제 작업은 하나뿐**이고,
작업의 수명이 곧 그 행의 수명이다. 1:1 관계를 별도 테이블로 쪼개면 조회마다
조인이 붙고 "행은 있는데 작업이 없는" 불일치 상태가 새로 생긴다.
그래서 **행 자체가 큐**다 — 상태·재시도 시각·시도 횟수를 행이 들고 있고,
worker 는 `PENDING_DELETE` 를 집어간다. 재시도·백오프·lease 회수라는
**패턴은 M024 와 같게** 유지한다.

삭제가 두 단계인 이유
-------------------
승인 적용 트랜잭션 안에서 실제 파일을 지우면, 그 트랜잭션이 뒤에 롤백됐을 때
**파일만 사라지고 DB 는 살아 있다.** 되돌릴 방법이 없다.
그래서 트랜잭션은 상태만 `PENDING_DELETE` 로 바꾸고, 실제 삭제는 worker 가 한다.
storage 삭제가 실패하면 행은 `PENDING_DELETE` 로 남아 재시도된다 —
**삭제 완료로 표시하지 않는다.**
"""

from alembic import op

revision = "M027"
down_revision = "M026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE expense_attachment
          ADD COLUMN org_id UUID REFERENCES organization(id),
          -- storage 내부 키. **API 응답에 절대 싣지 않는다.**
          -- 경로가 새면 스토리지 구조가 드러나고, S3 로 옮길 때 클라이언트가 깨진다.
          ADD COLUMN storage_key TEXT,
          ADD COLUMN size_bytes BIGINT,
          ADD COLUMN status TEXT NOT NULL DEFAULT 'AVAILABLE',
          ADD COLUMN scan_status TEXT NOT NULL DEFAULT 'SCAN_UNAVAILABLE',
          ADD COLUMN delete_requested_at TIMESTAMPTZ,
          ADD COLUMN delete_requested_by UUID REFERENCES app_user(id),
          ADD COLUMN delete_approval_request_id UUID REFERENCES approval_request(id)
                     ON DELETE SET NULL,
          ADD COLUMN delete_attempt_count INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN delete_next_attempt_at TIMESTAMPTZ,
          ADD COLUMN delete_last_error TEXT,
          ADD COLUMN storage_deleted_at TIMESTAMPTZ
    """)

    # 기존 행이 있다면 소속 비용에서 조직을 가져온다.
    op.execute("""
        UPDATE expense_attachment a
           SET org_id = e.org_id
          FROM expenses e
         WHERE e.id = a.expense_id AND a.org_id IS NULL
    """)
    # 조직을 못 채운 행은 소속 비용이 사라진 고아다. 조직 없는 파일은 권한을
    # 판정할 수 없으므로 남겨두지 않는다.
    op.execute("DELETE FROM expense_attachment WHERE org_id IS NULL")
    op.execute("ALTER TABLE expense_attachment ALTER COLUMN org_id SET NOT NULL")

    op.execute("""
        ALTER TABLE expense_attachment
          ADD CONSTRAINT ck_expense_attachment_status CHECK (
            status IN ('PENDING_SCAN','AVAILABLE','PENDING_DELETE','DELETED','FAILED')
          ),
          ADD CONSTRAINT ck_expense_attachment_scan CHECK (
            scan_status IN ('SCAN_UNAVAILABLE','PENDING','CLEAN','INFECTED','SCAN_FAILED')
          )
    """)

    # 같은 비용에 같은 파일을 두 번 올리는 것을 막는다.
    # **살아 있는 행만** 대상이다 — 지운 뒤 다시 올리는 건 정상 작업이다.
    op.execute("""
        CREATE UNIQUE INDEX ux_expense_attachment_live_hash
            ON expense_attachment (org_id, expense_id, file_hash)
         WHERE status IN ('PENDING_SCAN','AVAILABLE','PENDING_DELETE')
           AND deleted_at IS NULL
    """)

    # 목록 조회 — 비용 상세가 매번 부른다.
    op.execute("""
        CREATE INDEX ix_expense_attachment_expense
            ON expense_attachment (expense_id, status)
         WHERE deleted_at IS NULL
    """)

    # 삭제 worker 가 집어갈 행.
    op.execute("""
        CREATE INDEX ix_expense_attachment_pending_delete
            ON expense_attachment (delete_next_attempt_at)
         WHERE status = 'PENDING_DELETE'
    """)

    # `updated_at` 트리거는 M021 이 이미 전 테이블에 걸었다. 여기서 다시 만들면
    # DuplicateObject 로 마이그레이션이 통째로 실패한다.

    # ── 거래처 활성 플래그 ────────────────────────────────────────
    #
    # `partner`(수익을 배분받는 내부 파트너)에는 `is_active` 가 있는데
    # `external_partner`(거래처)에는 없었다. 거래를 끊은 거래처를 선택 목록에서
    # 빼려면 행을 지우는 수밖에 없었고, 지우면 **과거 비용의 거래처 이름이 사라진다.**
    #
    # 두 테이블을 합치지 않는다 — 내부 파트너는 수익 배분 주체고
    # 거래처는 돈을 지불하는 상대다. 의미가 다르다.
    op.execute("""
        ALTER TABLE external_partner
          ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE
    """)
    op.execute("""
        CREATE INDEX ix_external_partner_org_active
            ON external_partner (org_id, is_active)
         WHERE deleted_at IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_external_partner_org_active")
    op.execute("ALTER TABLE external_partner DROP COLUMN IF EXISTS is_active")

    # updated_at 트리거는 M021 소유다 — 여기서 지우지 않는다.
    op.execute("DROP INDEX IF EXISTS ix_expense_attachment_pending_delete")
    op.execute("DROP INDEX IF EXISTS ix_expense_attachment_expense")
    op.execute("DROP INDEX IF EXISTS ux_expense_attachment_live_hash")
    op.execute("""
        ALTER TABLE expense_attachment
          DROP CONSTRAINT IF EXISTS ck_expense_attachment_status,
          DROP CONSTRAINT IF EXISTS ck_expense_attachment_scan
    """)
    op.execute("""
        ALTER TABLE expense_attachment
          DROP COLUMN IF EXISTS org_id,
          DROP COLUMN IF EXISTS storage_key,
          DROP COLUMN IF EXISTS size_bytes,
          DROP COLUMN IF EXISTS status,
          DROP COLUMN IF EXISTS scan_status,
          DROP COLUMN IF EXISTS delete_requested_at,
          DROP COLUMN IF EXISTS delete_requested_by,
          DROP COLUMN IF EXISTS delete_approval_request_id,
          DROP COLUMN IF EXISTS delete_attempt_count,
          DROP COLUMN IF EXISTS delete_next_attempt_at,
          DROP COLUMN IF EXISTS delete_last_error,
          DROP COLUMN IF EXISTS storage_deleted_at
    """)
