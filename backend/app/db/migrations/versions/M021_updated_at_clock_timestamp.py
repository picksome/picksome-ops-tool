"""M021 — set_updated_at() 트리거를 clock_timestamp() 로 교체.

왜
--
`updated_at` 을 낙관적 동시성 토큰으로 쓴다(`product` 에 version 컬럼이 없다).
토큰이 성립하려면 **행이 바뀔 때마다 값이 바뀌어야** 한다.

`now()` 는 PostgreSQL 에서 **트랜잭션 시작 시각**이다. 한 트랜잭션 안에서 상수라
같은 트랜잭션에서 같은 행을 두 번 UPDATE 하면 `updated_at` 이 그대로다.
그러면 첫 수정 뒤에 읽은 토큰으로 두 번째 수정을 통과시킬 수 있다 —
낙관적 잠금이 뚫린다.

`clock_timestamp()` 는 **호출 시점의 실제 시각**이라 문장마다 증가한다.

측정으로 확인한 것 (PG 16.9)
---------------------------
교체 전: 한 트랜잭션의 두 UPDATE → updated_at 동일
교체 후: 두 UPDATE → updated_at 증가

`app/tests/integration/test_updated_at_token.py::
 test_two_updates_in_one_transaction_change_the_token` 이 이걸 고정한다.

영향 범위
--------
`set_updated_at()` 은 M001 에서 `updated_at` 을 가진 **모든 테이블**의 트리거가 공유한다.
함수 하나만 바꾸면 전부 적용된다 — 트리거를 다시 만들 필요가 없다.

부작용은 없다. `clock_timestamp()` 는 `now()` 보다 항상 같거나 크다
(트랜잭션 시작 이후에 실행되므로). 기존 행은 건드리지 않는다.

되돌리기
-------
downgrade 는 함수를 `now()` 로 되돌린다. **데이터 손실은 없지만**
낙관적 잠금이 다시 뚫리는 상태가 되므로, 되돌릴 이유가 생기면
`product` 에 version 컬럼을 먼저 도입해야 한다.
"""

from alembic import op

revision = "M021"
down_revision = "M020"
branch_labels = None
depends_on = None


_CLOCK = """
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  -- clock_timestamp(): 호출 시점의 실제 시각. 문장마다 증가한다.
  -- now() 를 쓰면 트랜잭션 시작 시각으로 고정되어, 같은 트랜잭션의 두 번째 UPDATE 가
  -- 토큰을 바꾸지 않는다 → 낙관적 동시성 검사가 무력화된다.
  NEW.updated_at = clock_timestamp();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_NOW = """
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(_CLOCK)


def downgrade() -> None:
    op.execute(_NOW)
