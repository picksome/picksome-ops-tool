"""M023 — updated_at 단조 증가 보장 + nil UUID variant 차단.

## 1. `updated_at` 단조 증가

M021 에서 `now()` → `clock_timestamp()` 로 바꿔 "한 트랜잭션 안에서 값이 안 바뀌는"
문제를 없앴다. 그런데 그것만으로는 **단조 증가가 보장되지 않는다.**

측정 결과 (이 환경, PG 16.9 / Windows)
--------------------------------------
  · `clock_timestamp()` 해상도: 약 63µs
  · 왕복 없는 서버측 연속 UPDATE 2,000회 → 고유 2,000개, 단조 위반 0건

**측정은 통과했다. 그래도 보강한다.** 위는 이 환경의 관측이지 보장이 아니다.
남는 위험 두 가지:

  · NTP 보정으로 시스템 시계가 **뒤로 갈 수 있다.** 그러면 새 토큰이 옛 토큰보다
    작아지고, 이전에 발급한 토큰이 다시 유효해진다 — 낙관적 잠금이 뚫린다.
  · 타이머 해상도는 플랫폼마다 다르다. 더 거친 환경에서는 같은 값이 나올 수 있다.

낙관적 잠금 전체가 이 값에 걸려 있는데 근거가 "측정해 보니 괜찮더라" 인 건 약하다.
`GREATEST` 한 번으로 **보장**으로 바꾼다.

    NEW.updated_at = GREATEST(clock_timestamp(), OLD.updated_at + 1µs)

시계가 뒤로 가도 토큰은 반드시 커진다. 비용은 UPDATE 당 비교 한 번이다.

**M021 을 고치지 않고 새 migration 을 쓴다.** M021 은 이미 적용됐고, 적용된
migration 을 수정하면 이미 올린 환경과 새로 올리는 환경의 스키마가 갈린다.

## 2. nil UUID variant 차단

M022 의 `variant_scope` 는 `product_variant_id IS NULL`(상품 기본 가격)을
nil UUID(`00000000-...-0000`)로 접어 exclusion constraint 에 넣는다.

만약 **실제 variant 의 id 가 nil UUID 라면**, 그 variant 의 가격과 상품 기본 가격이
같은 scope 로 취급되어 서로를 밀어낸다. 둘 중 하나만 존재할 수 있게 된다.

현재 PK 는 `gen_random_uuid()` 라 nil 이 나올 수 없다. 그래도 CHECK 로 고정한다 —
"불가능해 보인다" 는 근거는 나중에 seed 스크립트나 데이터 이관이 명시적으로 id 를
넣는 순간 깨진다. 제약은 그런 순간을 위해 있다.
"""

import sqlalchemy as sa
from alembic import op

revision = "M023"
down_revision = "M022"
branch_labels = None
depends_on = None

_NIL_UUID = "00000000-0000-0000-0000-000000000000"

_MONOTONIC = """
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  -- 단조 증가 보장.
  --   clock_timestamp() : 호출 시점의 실제 시각 (문장마다 증가)
  --   OLD.updated_at + 1µs : 시계가 뒤로 가거나 해상도가 거칠어도 반드시 커지게
  --
  -- now() 를 쓰면 안 된다 — 트랜잭션 시작 시각으로 고정되어 같은 트랜잭션의
  -- 두 번째 UPDATE 가 토큰을 바꾸지 않는다(M021 참고).
  NEW.updated_at = GREATEST(
    clock_timestamp(),
    OLD.updated_at + interval '1 microsecond'
  );
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

# M021 이 만든 버전. downgrade 는 여기로 되돌린다.
_CLOCK_ONLY = """
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = clock_timestamp();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(_MONOTONIC)

    # 기존 데이터에 nil UUID variant 가 있으면 CHECK 추가가 실패한다.
    # 실패하는 게 맞다 — 조용히 건너뛰면 M022 의 exclusion 이 이미 깨져 있는 채로 남는다.
    bad = op.get_bind().execute(
        sa.text("SELECT count(*) FROM product_variant WHERE id = :nil"),
        {"nil": _NIL_UUID},
    ).scalar()
    if bad:
        raise RuntimeError(
            f"nil UUID 를 id 로 가진 product_variant 가 {bad}건 있습니다. "
            "M022 의 variant_scope 가 이 행과 상품 기본 가격을 같은 것으로 취급합니다. "
            "해당 행의 id 를 먼저 정상 UUID 로 옮기세요."
        )

    op.execute(f"""
        ALTER TABLE product_variant
          ADD CONSTRAINT ck_pv_id_not_nil
          CHECK (id <> '{_NIL_UUID}'::uuid)
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE product_variant DROP CONSTRAINT IF EXISTS ck_pv_id_not_nil")
    op.execute(_CLOCK_ONLY)
