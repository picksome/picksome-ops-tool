"""M057 — 클레임 동기화 실행을 주문과 분리한다.

무엇이 잘못돼 있었나
------------------
`external_sync_run.sync_type` CHECK 에 `CLAIMS` 가 없어서, 클레임 동기화가
`ORDERS` 로 기록하고 있었다. 코드에도 그 결정이 주석으로 적혀 있다.

```
# 클레임은 주문과 같은 판매 사실 계열이라 `ORDERS` 를 쓰고,
# 무엇을 읽었는지는 자동화 실행 기록이 구분한다.
```

**자동화 실행 기록이 구분하지 못한다.** `external_sync_run.worker_run_id` 가
비어 있어서 `automation_run` 과 이어지지 않는다. 그래서 두 곳이 클레임 실행을
주문 실행으로 읽고 있었다.

```
order_sync.sync_status()      sync_type='ORDERS' 최근 1건 → 주문 화면 숫자
exception_center._integrations sync_type='ORDERS' 실패 → "주문 동기화 실패"
```

실측으로 확인됐다
--------------
```
2026-08-16 08:19  ORDERS  read=16 skipped=16 conflict=14   ← 실제로는 클레임 16건
2026-08-16 08:14  ORDERS  read=0                            ← 실제 주문 동기화
```

주문 화면은 "조회 16건" 을 주문 숫자로 보여준다. 실제 주문은 0건 읽었다.
클레임이 실패하면 화면은 "주문 동기화 실패" 라고 말한다.

주문과 클레임은 **다른 endpoint 이고 따로 켠다.** 하나가 다른 하나의 상태로
보이면, 주문이 멀쩡한데 빨간 불이 뜨거나 그 반대가 된다. docs/80 §11-1 에서
같은 종류의 거짓을 이미 한 번 고쳤다.

과거 행은 재분류하지 않는다
------------------------
할 수가 없다. `worker_run_id` 가 전부 NULL 이라 어느 실행이 클레임이었는지
데이터로 알 수 없고, `read_count = 16` 같은 값으로 추정하는 것은 추측이다.
이 저장소가 반복해서 금지해 온 것이다.

그래서 **앞으로 들어오는 것만** 나눈다. 다음 주문 동기화가 돌면 주문 화면은
그때부터 진짜 주문 실행을 가리킨다. 과거 행은 이력에 남되 새 조회에는
`CLAIMS` 로 안 잡힌다 — 그건 "옛날 기록이 섞여 있다" 는 사실이고 지어낸
값보다 낫다.
"""

from alembic import op

revision = "M057"
down_revision = "M056"
branch_labels = None
depends_on = None

_OLD = "'PRODUCTS', 'ORDERS', 'INVENTORY', 'SOURCES', 'PURCHASES'"
_NEW = _OLD + ", 'CLAIMS'"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE external_sync_run DROP CONSTRAINT IF EXISTS ck_esr_type
    """)
    op.execute(f"""
        ALTER TABLE external_sync_run
          ADD CONSTRAINT ck_esr_type CHECK (sync_type IN ({_NEW}))
    """)
    op.execute("""
        COMMENT ON COLUMN external_sync_run.sync_type IS
        '무엇을 읽은 실행인가. 주문과 클레임은 다른 endpoint 이고 따로 켠다 — 한 값으로 묶으면 하나의 실패가 다른 하나의 상태로 보인다(M057).'
    """)


def downgrade() -> None:
    # 되돌리면 `CLAIMS` 행이 CHECK 를 위반한다. **조용히 ORDERS 로 바꾸지
    # 않는다** — 그렇게 하면 되돌리기가 클레임 실행을 주문 실행으로
    # 위조하는 셈이고, 이 마이그레이션이 고치려던 바로 그 상태로 돌아간다.
    op.execute("""
        DO $$
        DECLARE remaining INT;
        BEGIN
            SELECT COUNT(*) INTO remaining FROM external_sync_run
             WHERE sync_type = 'CLAIMS';
            IF remaining > 0 THEN
                RAISE EXCEPTION
                    '[M057_CLAIM_RUNS_EXIST] 클레임 동기화 실행 기록이 % 건 있습니다. '
                    '되돌리면 이 기록들이 주문 실행과 구분되지 않습니다. '
                    '먼저 정리한 뒤 되돌리세요.', remaining;
            END IF;
        END $$
    """)
    op.execute("""
        ALTER TABLE external_sync_run DROP CONSTRAINT IF EXISTS ck_esr_type
    """)
    op.execute(f"""
        ALTER TABLE external_sync_run
          ADD CONSTRAINT ck_esr_type CHECK (sync_type IN ({_OLD}))
    """)
