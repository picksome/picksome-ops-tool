"""M058 — 외부 동기화 실행 주체 어휘를 worker 실행 기록에 맞춘다.

무엇이 문제인가
--------------
`external_sync_run.trigger` CHECK 가 `MANUAL / SCHEDULED / WEBHOOK` 만
허용한다. 그런데 실제로 이 실행을 시키는 주체는 네 종류다.

```
MANUAL     사람이 화면에서 눌렀다
SCHEDULED  주기가 되어 돌았다          → 안 돌았으면 스케줄이 멈춘 것
API        관리자가 worker 를 직접 실행했다 (`POST /operations/workers/{n}/run`)
STARTUP    PC 를 켜고 못 돈 기간을 따라잡았다 → 안 돌았으면 PC 가 꺼져 있던 것
```

지금까지는 자동화 실행기가 `trigger="SCHEDULED"` 를 **못박아** 넘겨서 이
차이가 드러나지 않았다. 못박힌 값을 풀면 CHECK 에 걸리므로, 어휘를 먼저
맞춘다.

전부 `SCHEDULED` 로 적으면 기록이 거짓이 된다. "어제는 왜 안 돌았나" 에
답할 수 없고, PC 를 안 켠 날이 스케줄러 장애로 보인다. 이 저장소가 M057 에서
고친 것과 같은 종류의 거짓이다.

`worker_run.trigger` 는 이미 `MANUAL/SCHEDULED/API/STARTUP` 을 허용한다
(M030). **새 값을 지어내는 것이 아니라** 그때 정해 둔 어휘를 외부 동기화
기록에도 맞추는 것이다. `WEBHOOK` 은 외부 동기화에만 있으므로 남긴다.
"""

from alembic import op

revision = "M058"
down_revision = "M057"
branch_labels = None
depends_on = None

_OLD = "'MANUAL', 'SCHEDULED', 'WEBHOOK'"
_NEW = _OLD + ", 'API', 'STARTUP'"
_ADDED = "'API', 'STARTUP'"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE external_sync_run DROP CONSTRAINT IF EXISTS ck_esr_trigger
    """)
    op.execute(f"""
        ALTER TABLE external_sync_run
          ADD CONSTRAINT ck_esr_trigger CHECK (trigger IN ({_NEW}))
    """)
    op.execute("""
        COMMENT ON COLUMN external_sync_run.trigger IS
        '무엇이 이 실행을 시켰나. STARTUP 은 기동 직후 따라잡기 — 주기가 되어 돈 SCHEDULED 와 구분해야 "왜 안 돌았나"에 답할 수 있다(M058).'
    """)


def downgrade() -> None:
    # 새 값이 남아 있으면 되돌리지 않는다. 조용히 `SCHEDULED` 로 바꾸면
    # 되돌리기가 "PC 가 꺼져 있었다" 를 "스케줄러가 돌았다" 로 위조하는
    # 셈이다 — M057 downgrade 와 같은 판단이다.
    op.execute(f"""
        DO $$
        DECLARE remaining INT;
        BEGIN
            SELECT COUNT(*) INTO remaining FROM external_sync_run
             WHERE trigger IN ({_ADDED});
            IF remaining > 0 THEN
                RAISE EXCEPTION
                    '[M058_NEW_TRIGGER_RUNS_EXIST] API/STARTUP 실행 기록이 % 건 있습니다. '
                    '되돌리면 이 기록들이 스케줄 실행과 구분되지 않습니다. '
                    '먼저 정리한 뒤 되돌리세요.', remaining;
            END IF;
        END $$
    """)
    op.execute("""
        ALTER TABLE external_sync_run DROP CONSTRAINT IF EXISTS ck_esr_trigger
    """)
    op.execute(f"""
        ALTER TABLE external_sync_run
          ADD CONSTRAINT ck_esr_trigger CHECK (trigger IN ({_OLD}))
    """)
