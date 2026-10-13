"""M052 — 외부 매핑에 판매처 계정 유형을 더한다.

왜 지금 필요한가 — 실제 커넥터가 증명한 gap
--------------------------------------
NEXT ENGINE 주문에는 어느 스토어에서 온 주문인지가 `mall_cd` 로 온다.

```
mall00100  11번가        mall00300  스마트스토어        mall02400  쿠팡
```

이걸 우리 판매처 계정(`integration_account`)에 이어야 매출이 어느 스토어
것인지 알 수 있다. 그런데 `external_entity_mapping` 은 **이미 범용 구조**인데
CHECK 가 `PRODUCT` 하나만 허용해 그 행을 넣을 수 없었다(`CheckViolation`).

M039 가 그 CHECK 를 달면서 남긴 주석이 이 마이그레이션을 예고하고 있었다.

```
지금 실제로 다루는 것은 PRODUCT 뿐이다. 지원하지 않는 유형을 미리 열어 두면
"매핑은 됐는데 아무 일도 안 일어나는" 상태가 생긴다. **늘릴 때 CHECK 를 함께
늘린다.**
```

지금이 그때다. 커넥터가 실제로 그 유형을 다루게 됐다.

무엇을 하지 않았나
---------------
```
nextengine_mall_cd 컬럼 추가          ✗  vendor 가 Core 스키마에 들어온다
별도 nextengine_shop_mapping 테이블    ✗  셀러툴마다 테이블이 하나씩 생긴다
integration_account.config_json 에 저장 ✗  "한 코드는 한 계정" 을 DB 가 못 지킨다
```

세 번째가 특히 위험했다. JSON 안에서는 유일성을 걸 수 없어서, 같은 `mall_cd`
가 두 판매계정에 붙어도 DB 가 막지 못한다. 그러면 매출이 어느 스토어 것인지
사람마다 다르게 읽는다.

어휘를 `integration_account` 로 두지 않은 이유
----------------------------------------
이 컬럼의 기존 값은 `PRODUCT` 다. **테이블 이름이 아니라 업무 개념**이고,
외부/내부 양쪽에 같은 토큰을 쓴다.

```
external_entity_type  PRODUCT              internal_entity_type  PRODUCT
external_entity_type  MARKETPLACE_ACCOUNT  internal_entity_type  MARKETPLACE_ACCOUNT
```

`internal_entity_type = 'integration_account'` 로 두면 어휘에 물리 테이블
이름이 섞인다. 그러면 나중에 그 테이블을 쪼개거나 이름을 바꿀 때 **데이터에
적힌 문자열**까지 따라 고쳐야 한다. 개념 이름은 그대로 두고, 개념→테이블
연결은 코드가 안다.

(`credential_secret.owner_type` 은 `external_account` 처럼 테이블 이름을
쓰지만 그건 "어느 테이블이 이 비밀을 소유하나" 라는 **물리적 소유 포인터**라
성격이 다르다. 여기는 `entity_type` 이다.)

대칭을 제약으로 굳힌다
------------------
상품을 판매계정에 매핑하는 것은 어떤 경우에도 말이 되지 않는다. 지금까지는
유형이 하나뿐이라 그런 행이 생길 수 없었는데, 둘이 되는 순간 가능해진다.
`ck_eem_type_symmetry` 가 그걸 막는다 — 코드 실수 하나가 조용한 오매핑이
되는 경로를 미리 닫는다.

이미 있는 것은 그대로 쓴다
---------------------
```
uq_eem_external_identity  (external_account_id, entity_type, external_entity_id)
```

이 유일 인덱스가 **"같은 mall_cd 는 한 판매계정에만"** 을 그대로 보장한다.
새 인덱스가 필요 없다. 조직 격리와 남의 조직 계정 금지는 FK 를 걸 수 없어
(대상 테이블이 유형마다 다르다) 서비스가 검증하고 테스트가 지킨다.
"""

from alembic import op

revision = "M052"
down_revision = "M051"
branch_labels = None
depends_on = None

#: 이 마이그레이션 이후 허용되는 유형.
ENTITY_TYPES = ("PRODUCT", "MARKETPLACE_ACCOUNT")


def upgrade() -> None:
    # 기존 PRODUCT 행은 한 글자도 건드리지 않는다. CHECK 를 넓히기만 한다.
    op.execute("""
        ALTER TABLE external_entity_mapping
            DROP CONSTRAINT ck_eem_external_type,
            DROP CONSTRAINT ck_eem_internal_type,
            ADD CONSTRAINT ck_eem_external_type
                CHECK (external_entity_type IN ('PRODUCT', 'MARKETPLACE_ACCOUNT')),
            ADD CONSTRAINT ck_eem_internal_type
                CHECK (internal_entity_type IN ('PRODUCT', 'MARKETPLACE_ACCOUNT'))
    """)

    # 외부·내부 유형이 어긋난 매핑은 만들 수 없다.
    op.execute("""
        ALTER TABLE external_entity_mapping
            ADD CONSTRAINT ck_eem_type_symmetry
                CHECK (external_entity_type = internal_entity_type)
    """)

    op.execute("""
        COMMENT ON COLUMN external_entity_mapping.external_entity_type IS
        '업무 개념 이름(PRODUCT/MARKETPLACE_ACCOUNT). 물리 테이블 이름을 넣지 않는다 — 개념→테이블 연결은 코드가 안다.'
    """)


def downgrade() -> None:
    # PRODUCT 전용으로 되돌리려면 그 사이 생긴 판매처 계정 매핑을 어떻게 할지
    # 정해야 한다. **되돌리기가 업무 데이터를 판단하지 않는다** — 남아 있으면
    # 실패시키고, 사람이 먼저 정리하게 한다.
    #
    # 조용히 지우면 어느 주문이 어느 스토어 것이었는지가 사라지고, 그 사실은
    # 다음 정산 때까지 아무도 모른다.
    op.execute("""
        DO $$
        DECLARE remaining INT;
        BEGIN
            SELECT COUNT(*) INTO remaining FROM external_entity_mapping
             WHERE external_entity_type = 'MARKETPLACE_ACCOUNT' AND deleted_at IS NULL;
            IF remaining > 0 THEN
                RAISE EXCEPTION
                    'MARKETPLACE_ACCOUNT 매핑이 % 건 남아 있습니다. 먼저 정리한 뒤 되돌리세요.',
                    remaining;
            END IF;
        END $$
    """)
    op.execute("""
        ALTER TABLE external_entity_mapping
            DROP CONSTRAINT ck_eem_type_symmetry,
            DROP CONSTRAINT ck_eem_external_type,
            DROP CONSTRAINT ck_eem_internal_type,
            ADD CONSTRAINT ck_eem_external_type
                CHECK (external_entity_type IN ('PRODUCT')),
            ADD CONSTRAINT ck_eem_internal_type
                CHECK (internal_entity_type IN ('PRODUCT'))
    """)
