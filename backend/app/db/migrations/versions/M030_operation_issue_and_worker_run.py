"""M030 — 운영 이슈 센터 + worker 실행 이력.

문제
----
대시보드가 "미매칭 정산 3건" 같은 경고를 보여주지만 **거기서 끝난다.**
누가 언제 처리하는지, 처리했는지, 왜 무시했는지가 어디에도 남지 않는다.
그래서 같은 경고가 몇 달째 떠 있어도 아무도 모른다.

`operation_issue` 는 그 경고를 **처리 가능한 업무**로 만든다.

검증 run(M028)과 무엇이 다른가
---------------------------
```
monthly_close_check_item   "14:32 에 미매칭 정산 3건이 보였다"      측정. 불변
operation_issue            "그 3건을 누가 언제까지 처리한다"        작업. 변한다
```
둘을 `dedupe_key` 로 잇는다. 같은 상태를 두 테이블에 두지 않는다.

스캐너가 사람의 결정을 덮어쓰지 않는다
---------------------------------
이게 이 설계의 핵심이다. 매 스캔마다 조건이 여전히 참이라고 해서
사람이 `RESOLVED`/`IGNORED` 로 바꾼 이슈를 다시 `OPEN` 으로 되돌리면,
사용자는 이슈 센터를 신뢰하지 않게 된다.

  · 같은 dedupe_key 의 **열린** 이슈가 있으면 → `last_detected_at` 만 갱신
  · `RESOLVED` / `IGNORED` 이슈가 있으면 → **건드리지 않는다**
  · 조건이 사라졌을 때 자동 해결할지는 **이슈 유형마다 다르다**
    (`auto_resolve` 컬럼). 데이터 조건이 명확한 것만 자동 해결한다

worker 실행 이력
--------------
기존 `batch_run` 은 `integration_job` 에 묶여 있다(`job_id` FK). 승인 실행기·
첨부 삭제·가격 반영·이슈 스캔은 job 행이 없어서 그 테이블을 쓸 수 없다.

그렇다고 generic job 테이블을 새로 만들지 않는다 — 각 worker 의 **업무 로직은
그대로 두고** 실행·관측 기반만 공통화한다. `worker_run` 은 "언제 돌았고 몇 건
처리했고 실패했나" 만 안다. 무엇을 했는지는 각 worker 의 도메인 테이블에 있다.
"""

from alembic import op

revision = "M030"
down_revision = "M029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ──────────────────────────────────────────────────────────────
    # 1. 운영 이슈
    # ──────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE operation_issue (
          id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id   UUID NOT NULL REFERENCES organization(id),

          issue_type TEXT NOT NULL,
          severity   TEXT NOT NULL,

          -- 원본으로 이동할 수 있어야 한다. 이슈만 보고 끝나면 처리가 안 된다.
          entity_type TEXT,
          entity_id   UUID,
          -- 월 단위 이슈(마감 blocker 등)는 entity 가 없고 기간이 있다.
          year_month  TEXT,

          -- 같은 문제를 매 스캔마다 새로 만들지 않기 위한 키.
          dedupe_key TEXT NOT NULL,

          title       TEXT NOT NULL,
          description TEXT,
          -- 몇 건짜리 문제인가. 대표 entity 하나만 걸고 수는 여기 담는다.
          affected_count INTEGER NOT NULL DEFAULT 1,

          status      TEXT NOT NULL DEFAULT 'OPEN',
          assigned_to UUID REFERENCES app_user(id),
          due_date    DATE,

          -- 이 이슈가 월 마감을 막는가. close_checks 가 이 값을 읽는다.
          blocks_month_close BOOLEAN NOT NULL DEFAULT FALSE,
          -- 조건이 사라지면 스캐너가 자동으로 닫아도 되는가.
          -- 데이터 조건이 명확한 것만 TRUE 다.
          auto_resolve BOOLEAN NOT NULL DEFAULT FALSE,

          -- 사람이 아니라 스캐너가 만든 이슈인가. 수동 이슈를 스캔이 건드리면 안 된다.
          created_by_scanner BOOLEAN NOT NULL DEFAULT TRUE,

          first_detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_detected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          resolved_at TIMESTAMPTZ,
          resolved_by UUID REFERENCES app_user(id),
          resolution_note TEXT,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by UUID,
          deleted_at TIMESTAMPTZ,
          deleted_by UUID,

          CONSTRAINT ck_oi_severity CHECK (severity IN ('BLOCKING','WARNING','INFO')),
          CONSTRAINT ck_oi_status CHECK (
            status IN ('OPEN','IN_PROGRESS','WAITING','RESOLVED','IGNORED')
          ),
          CONSTRAINT ck_oi_ym CHECK (
            year_month IS NULL OR year_month ~ '^[0-9]{4}-[0-9]{2}$'
          ),
          -- 종료 상태는 언제 끝났는지가 있어야 한다. 없으면 "닫혔는데 언제인지 모름" 이다.
          CONSTRAINT ck_oi_resolved_at CHECK (
            status NOT IN ('RESOLVED','IGNORED') OR resolved_at IS NOT NULL
          ),
          -- ⚠️ IGNORED 는 사유가 필수다. 사유 없이 무시한 이슈는
          --    나중에 아무도 왜 무시했는지 설명하지 못한다.
          CONSTRAINT ck_oi_ignore_reason CHECK (
            status <> 'IGNORED'
            OR (resolution_note IS NOT NULL AND length(btrim(resolution_note)) > 0)
          ),
          CONSTRAINT ck_oi_detect_order CHECK (last_detected_at >= first_detected_at)
        )
    """)

    # 같은 dedupe_key 로 **열린** 이슈는 하나뿐이다.
    # 닫힌 이슈는 여러 개 쌓일 수 있다 — 같은 문제가 재발한 이력이기 때문이다.
    op.execute("""
        CREATE UNIQUE INDEX ux_oi_open_dedupe
            ON operation_issue (org_id, dedupe_key)
         WHERE status IN ('OPEN','IN_PROGRESS','WAITING') AND deleted_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_oi_list ON operation_issue (org_id, status, severity, last_detected_at DESC)
         WHERE deleted_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_oi_assignee ON operation_issue (org_id, assigned_to)
         WHERE assigned_to IS NOT NULL AND deleted_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_oi_entity ON operation_issue (org_id, entity_type, entity_id)
         WHERE entity_id IS NOT NULL AND deleted_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_oi_blocking ON operation_issue (org_id, year_month)
         WHERE blocks_month_close = TRUE AND deleted_at IS NULL
    """)
    op.execute("""
        CREATE TRIGGER trg_operation_issue_updated_at
          BEFORE UPDATE ON operation_issue
          FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    # ──────────────────────────────────────────────────────────────
    # 2. worker 실행 이력
    # ──────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE worker_run (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          -- 조직 전체를 도는 실행은 org_id 가 NULL 이다.
          org_id UUID REFERENCES organization(id),

          worker_name TEXT NOT NULL,
          -- 무엇이 실행을 시작했나. 수동 실행과 스케줄 실행을 구분해야
          -- "왜 어제는 안 돌았나" 를 답할 수 있다.
          trigger TEXT NOT NULL DEFAULT 'MANUAL',

          status TEXT NOT NULL DEFAULT 'RUNNING',

          started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          finished_at TIMESTAMPTZ,
          duration_ms INTEGER,

          processed_count INTEGER NOT NULL DEFAULT 0,
          success_count   INTEGER NOT NULL DEFAULT 0,
          failure_count   INTEGER NOT NULL DEFAULT 0,
          skipped_count   INTEGER NOT NULL DEFAULT 0,

          -- 실패 원인. **stack trace 를 넣지 않는다** — 관리 화면에 그대로 노출되면
          -- 내부 경로와 쿼리가 새어나간다. 코드와 짧은 메시지만.
          error_code    TEXT,
          error_message TEXT,

          summary_json JSONB,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

          CONSTRAINT ck_wr_status CHECK (status IN ('RUNNING','SUCCESS','FAILED','SKIPPED')),
          CONSTRAINT ck_wr_trigger CHECK (trigger IN ('MANUAL','SCHEDULED','API','STARTUP')),
          CONSTRAINT ck_wr_counts CHECK (
            processed_count >= 0 AND success_count >= 0
            AND failure_count >= 0 AND skipped_count >= 0
          ),
          CONSTRAINT ck_wr_finished CHECK (status = 'RUNNING' OR finished_at IS NOT NULL)
        )
    """)
    op.execute("""
        CREATE INDEX ix_wr_worker ON worker_run (worker_name, started_at DESC)
    """)
    op.execute("""
        CREATE INDEX ix_wr_running ON worker_run (worker_name)
         WHERE status = 'RUNNING'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS worker_run")
    op.execute("DROP TABLE IF EXISTS operation_issue")
