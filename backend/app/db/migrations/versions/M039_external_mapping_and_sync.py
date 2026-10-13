"""M039 — 외부 엔티티 매핑 · 동기화 실행 기록.

기존 `external_product_mapping` 을 왜 쓰지 않았나
--------------------------------------------
M001 에 그 테이블이 있다. 하지만:

```
account_id → integration_account   판매처 계정에 묶여 있다
PRODUCT 전용                        entity type 개념이 없다
org_id 없음                         계정을 통해서만 조직을 안다
행 0건, 참조 코드 0곳                실제로 쓰인 적이 없다
```

셀러툴 매핑을 거기에 넣으면 셀러툴 계정이 `integration_account` 여야 한다는
뜻이 되고, 그건 M038 에서 나누기로 한 것과 정면으로 어긋난다.
그래서 새 테이블을 만들고, 옛 것은 **건드리지 않는다**(판매처 쪽 미사용 자리로
남는다 — 정리는 별도 판단이다).

매핑과 provenance 는 다른 것이다
-----------------------------
```
매핑        외부 객체 A = 내부 객체 B          (동일성)
provenance  이 값이 어디서 관측됐는가          (출처)
```

이 테이블은 **동일성만** 담는다. 값의 출처는 각 도메인이 이미 갖고 있다
(`product_source_cost_version.source_type`, `fx_rate_snapshot.source_type` 등).
모든 업무 테이블에 `source_*` 컬럼을 뿌리거나 범용 EAV 를 만들지 않는다 —
지금 필요한 use case 가 없다.

가장 중요한 불변식
---------------
**한 외부 엔티티가 두 내부 엔티티에 붙을 수 없다.**

```
NEXT_ENGINE / 계정A / PRODUCT / 123  →  상품 P1
NEXT_ENGINE / 계정A / PRODUCT / 123  →  상품 P2   ← 금지
```

이게 뚫리면 같은 외부 상품의 재고·원가가 두 상품으로 갈라져 들어온다.
반대(한 내부 상품에 여러 외부 매핑)는 허용한다 — 셀러툴을 둘 쓰는 조직이 있다.
"""

from alembic import op

revision = "M039"
down_revision = "M038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ──────────────────────────────────────────────────────────────
    # 1. 외부 엔티티 매핑
    # ──────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE external_entity_mapping (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          external_account_id UUID NOT NULL REFERENCES external_account(id),

          -- 외부 쪽
          external_entity_type TEXT NOT NULL,
          external_entity_id   TEXT NOT NULL,

          -- 내부 쪽. **FK 를 걸지 않는다** — entity_type 마다 대상 테이블이 다르다.
          -- 대신 서비스가 존재를 검증하고, 고아 매핑은 데이터 품질이 잡는다.
          internal_entity_type TEXT NOT NULL,
          internal_entity_id   UUID NOT NULL,

          mapping_status TEXT NOT NULL DEFAULT 'ACTIVE',

          first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_seen_at  TIMESTAMPTZ,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID REFERENCES app_user(id),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by UUID REFERENCES app_user(id),
          deleted_at TIMESTAMPTZ,
          deleted_by UUID REFERENCES app_user(id),

          -- 지금 실제로 다루는 것은 PRODUCT 뿐이다. 지원하지 않는 유형을
          -- 미리 열어 두면 "매핑은 됐는데 아무 일도 안 일어나는" 상태가 생긴다.
          -- 늘릴 때 CHECK 를 함께 늘린다.
          CONSTRAINT ck_eem_external_type
            CHECK (external_entity_type IN ('PRODUCT')),
          CONSTRAINT ck_eem_internal_type
            CHECK (internal_entity_type IN ('PRODUCT')),
          CONSTRAINT ck_eem_status
            CHECK (mapping_status IN ('ACTIVE', 'SUSPENDED', 'CONFLICT'))
        )
    """)

    # 같은 계정의 같은 외부 엔티티는 내부 하나에만 붙는다.
    #
    # 애플리케이션 검사만으로는 동시 sync 두 개가 각각 다른 상품에 붙일 수 있다.
    op.execute("""
        CREATE UNIQUE INDEX uq_eem_external_identity
            ON external_entity_mapping
               (external_account_id, external_entity_type, external_entity_id)
         WHERE deleted_at IS NULL
    """)
    # 역방향 조회: "이 상품이 어느 외부 시스템들에 있나".
    op.execute("""
        CREATE INDEX ix_eem_internal
            ON external_entity_mapping
               (org_id, internal_entity_type, internal_entity_id)
         WHERE deleted_at IS NULL
    """)

    # ──────────────────────────────────────────────────────────────
    # 2. 동기화 실행 기록
    # ──────────────────────────────────────────────────────────────
    #
    # `worker_run` 을 재사용하지 않은 이유: worker_run 은 "어떤 worker 가 언제
    # 돌았나" 이고 계정 개념이 없다. 계정별 마지막 성공 시각·충돌 건수를
    # summary_json 에 넣으면 조회할 때마다 JSON 을 파싱해야 하고 인덱스도 못 건다.
    #
    # 대신 worker 가 돌린 sync 는 `worker_run_id` 로 이어 둔다 — 두 기록이
    # 서로를 모르면 "이 worker 실행이 무엇을 했나" 를 추적할 수 없다.
    op.execute("""
        CREATE TABLE external_sync_run (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          external_account_id UUID NOT NULL REFERENCES external_account(id),

          sync_type TEXT NOT NULL,
          trigger   TEXT NOT NULL DEFAULT 'MANUAL',
          status    TEXT NOT NULL DEFAULT 'RUNNING',

          started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          finished_at TIMESTAMPTZ,

          -- 무엇이 일어났는지. 0 과 NULL 을 구분한다 — 아직 안 끝난 실행의
          -- 건수를 0 으로 보면 "아무것도 못 가져왔다" 로 읽힌다.
          read_count     INTEGER,
          created_count  INTEGER,
          updated_count  INTEGER,
          skipped_count  INTEGER,
          conflict_count INTEGER,
          error_count    INTEGER,

          error_code    TEXT,
          error_message TEXT,

          -- worker 가 돌렸으면 그 실행을 가리킨다.
          worker_run_id UUID REFERENCES worker_run(id),

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID REFERENCES app_user(id),

          CONSTRAINT ck_esr_status
            CHECK (status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED', 'UNSUPPORTED')),
          CONSTRAINT ck_esr_trigger
            CHECK (trigger IN ('MANUAL', 'SCHEDULED', 'WEBHOOK')),
          CONSTRAINT ck_esr_type
            CHECK (sync_type IN
                   ('PRODUCTS', 'ORDERS', 'INVENTORY', 'SOURCES', 'PURCHASES'))
        )
    """)
    op.execute("""
        CREATE INDEX ix_esr_account_recent
            ON external_sync_run (external_account_id, started_at DESC)
    """)

    # ──────────────────────────────────────────────────────────────
    # 3. 동기화 충돌
    # ──────────────────────────────────────────────────────────────
    #
    # 외부 값과 내부 값이 다른데 **자동으로 정할 수 없는** 경우를 남긴다.
    # 조용히 덮으면 사용자가 고쳐 놓은 값이 다음 sync 에 사라진다.
    op.execute("""
        CREATE TABLE external_sync_conflict (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          sync_run_id UUID NOT NULL REFERENCES external_sync_run(id),
          mapping_id  UUID REFERENCES external_entity_mapping(id),

          field_name     TEXT NOT NULL,
          external_value TEXT,
          internal_value TEXT,

          resolution TEXT NOT NULL DEFAULT 'PENDING',
          resolved_at TIMESTAMPTZ,
          resolved_by UUID REFERENCES app_user(id),

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

          CONSTRAINT ck_esc_resolution
            CHECK (resolution IN ('PENDING', 'KEPT_INTERNAL', 'TOOK_EXTERNAL', 'IGNORED'))
        )
    """)
    op.execute("""
        CREATE INDEX ix_esc_pending
            ON external_sync_conflict (org_id, resolution) WHERE resolution = 'PENDING'
    """)

    # ──────────────────────────────────────────────────────────────
    # 4. 필드 권위 (authority)
    # ──────────────────────────────────────────────────────────────
    #
    # "이 상품의 이름은 누가 정하는가" 를 기록한다.
    #
    # 사용자가 손으로 고친 값을 다음 sync 가 되돌리면 안 된다. 그 사실을 어딘가
    # 남겨야 하는데, audit_log 를 매 sync 마다 뒤지는 것은 느리고 취약하다.
    # 필드 단위로 한 줄만 둔다.
    op.execute("""
        CREATE TABLE external_field_authority (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),

          internal_entity_type TEXT NOT NULL,
          internal_entity_id   UUID NOT NULL,
          field_name           TEXT NOT NULL,

          authority TEXT NOT NULL,

          -- 누가 언제 그렇게 정했는지. MANUAL_OVERRIDE 는 사람이 만든다.
          set_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          set_by UUID REFERENCES app_user(id),

          CONSTRAINT ck_efa_authority
            CHECK (authority IN
                   ('EXTERNAL_AUTHORITATIVE', 'INTERNAL_AUTHORITATIVE', 'MANUAL_OVERRIDE'))
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_efa_field
            ON external_field_authority
               (org_id, internal_entity_type, internal_entity_id, field_name)
    """)
    op.execute("""
        COMMENT ON TABLE external_field_authority IS
        '필드별 권위. MANUAL_OVERRIDE 인 필드는 외부 sync 가 덮지 않는다.'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS external_field_authority")
    op.execute("DROP TABLE IF EXISTS external_sync_conflict")
    op.execute("DROP TABLE IF EXISTS external_sync_run")
    op.execute("DROP TABLE IF EXISTS external_entity_mapping")
