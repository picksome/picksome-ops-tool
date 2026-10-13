"""외부 시스템 연동 라우터 (M038/M039).

권한
----
```
조회        ADMIN / OPERATOR / SOURCING
계정 관리    ADMIN
동기화 실행  ADMIN / OPERATOR
```

**비밀은 어떤 응답에도 나가지 않는다.** 계정 응답은 `credential_configured`
(불리언)만 준다 — vault 항목 id 조차 내보내지 않는다.

**판매처 계정(`/integrations`)과 다른 화면이다.** 저기는 쿠팡·스마트스토어
판매자 계정이고 여기는 셀러툴 같은 외부 시스템이다. 책임이 달라 합치지 않았다
(docs/58).
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.services import external_sync as ex

router = APIRouter(prefix="/external", tags=["external"])

_view = require_roles("ADMIN", "OPERATOR", "SOURCING")
_manage = require_roles("ADMIN")
_run = require_roles("ADMIN", "OPERATOR")
# 자격증명은 계정 관리보다 좁다. 계정 이름을 고치는 것과 인증정보를 바꾸는 것은
# 다른 무게다 — 후자는 외부 시스템에 대한 우리 권한 전체를 좌우한다.
_credentials = require_roles("ADMIN")


class AccountIn(BaseModel):
    external_system_id: UUID
    display_name: str = Field(..., min_length=1, max_length=200)
    account_code: str = Field(..., min_length=1, max_length=100)
    external_account_identifier: str | None = Field(None, max_length=200)
    # 자격증명은 여기서 받지 않는다 — 등록 폼에 비밀번호 칸을 두면 그 값이
    # 요청 로그·브라우저 히스토리에 남는다.


class StatusIn(BaseModel):
    status: str


class VaultNoteIn(BaseModel):
    """사람이 찾아갈 **쪽지**를 연결한다. 값이 아니다.

    이것만으로는 무인 동기화가 되지 않는다 — 쪽지에는 값이 없다.
    """

    vault_entry_id: UUID


class CredentialIn(BaseModel):
    """무인 동기화용 인증정보 (M042).

    **이 요청 본문만이 비밀을 담는다.** 계정 생성·수정에는 섞지 않는다 —
    섞으면 일반 요청 로그가 전부 비밀 취급 대상이 된다.

    `secret` 의 모양은 `credential_kind` 가 정한다(core/secret_store.py):

    ```
    API_KEY             api_key
    ACCESS_SECRET       access_key, secret_key
    CLIENT_CREDENTIALS  client_id, client_secret
    OAUTH_REFRESH       client_id, client_secret, refresh_token
    BASIC               username, password
    CUSTOM              (자유)
    ```

    vendor 별 필드를 스키마에 만들지 않는다 — 셀러툴을 하나 붙일 때마다
    모델이 늘고 Core 가 vendor 를 알게 된다.
    """

    credential_kind: str = Field(
        ..., description="API_KEY | ACCESS_SECRET | CLIENT_CREDENTIALS "
                         "| OAUTH_REFRESH | BASIC | CUSTOM",
    )
    secret: dict[str, str] = Field(..., description="형태에 맞는 항목들. 응답에 절대 나오지 않는다.")


class MappingIn(BaseModel):
    external_account_id: UUID
    external_entity_type: str = "PRODUCT"
    external_entity_id: str = Field(..., min_length=1, max_length=200)
    internal_entity_type: str = "PRODUCT"
    internal_entity_id: UUID


class RemapIn(BaseModel):
    internal_entity_id: UUID
    reason: str = Field(..., min_length=1, max_length=300)


@router.get("/systems")
def list_systems(
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    """외부 시스템 카탈로그 + 커넥터 구현 여부.

    카탈로그에 있다고 연동되는 게 아니다. `connector_available` 이 false 면
    화면은 동기화 버튼을 비활성화하고 이유를 말한다.
    """
    _ = user
    rows = ex.list_systems(db)
    return envelope(rows, {"total": len(rows)})


@router.get("/accounts")
def list_accounts(
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    rows = ex.list_accounts(db, org_id=user.org_id)
    return envelope(rows, {"total": len(rows)})


@router.post("/accounts", status_code=201)
def create_account(
    payload: AccountIn,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(ex.create_account(
        db, org_id=user.org_id, actor_user_id=user.user_id, **payload.model_dump(),
    ))


@router.get("/accounts/{account_id}")
def get_account(
    account_id: UUID,
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(ex.get_account(db, org_id=user.org_id, account_id=account_id))


@router.patch("/accounts/{account_id}/status")
def set_status(
    account_id: UUID,
    payload: StatusIn,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> dict:
    """비활성화는 삭제가 아니다 — 과거 매핑과 동기화 기록이 남는다."""
    return envelope(ex.set_account_status(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        account_id=account_id, status=payload.status,
    ))


@router.put("/accounts/{account_id}/credential")
def set_credential(
    account_id: UUID,
    payload: CredentialIn,
    user: CurrentUser = Depends(_credentials),
    db: Session = Depends(get_db),
) -> dict:
    """무인 동기화용 인증정보 저장(신규·교체 공통, M042).

    평문은 여기서 끝난다
    -----------------
    ```
    요청 본문  →  SecretStore  →  암호문(DB) + 불투명 참조
    ```

    응답에는 `credential_configured` 같은 사실만 담긴다. 값도, 암호문도,
    참조도 나가지 않는다. 이 앱에는 요청 본문을 읽는 미들웨어가 없어서
    본문이 로그로 흘러갈 경로 자체가 없다 — 그 사실을
    `test_credential_security.py` 가 고정한다.

    마스터 키가 없으면 **503** 이다 — 평문으로 대신 저장하지 않는다.
    """
    return envelope(ex.set_credential_secret(
        db, org_id=user.org_id, actor_user_id=user.user_id, account_id=account_id,
        credential_kind=payload.credential_kind, payload=payload.secret,
    ))


@router.delete("/accounts/{account_id}/credential")
def remove_credential(
    account_id: UUID,
    user: CurrentUser = Depends(_credentials),
    db: Session = Depends(get_db),
) -> dict:
    """인증정보 삭제. 이후 동기화는 `CREDENTIAL_NOT_CONFIGURED` 로 거부된다.

    다시 보여주기(재조회) 기능은 없다. 잊었으면 새로 넣는다 — 되읽기 경로를
    만드는 순간 그 경로가 가장 약한 고리가 된다.
    """
    return envelope(ex.remove_credential(
        db, org_id=user.org_id, actor_user_id=user.user_id, account_id=account_id,
    ))


@router.post("/accounts/{account_id}/vault-note")
def link_vault_note(
    account_id: UUID,
    payload: VaultNoteIn,
    user: CurrentUser = Depends(_credentials),
    db: Session = Depends(get_db),
) -> dict:
    """사람이 찾아갈 쪽지 연결(`password_vault_entry`).

    **이걸로 동기화가 되지는 않는다.** 쪽지에는 값이 없다. 담당자가 바뀔 때
    "이 계정 비밀번호 어디 있죠" 의 단서로 남긴다.
    """
    return envelope(ex.link_vault_note(
        db, org_id=user.org_id, actor_user_id=user.user_id, account_id=account_id,
        vault_entry_id=payload.vault_entry_id,
    ))


@router.post("/accounts/{account_id}/test-connection")
def test_connection(
    account_id: UUID,
    user: CurrentUser = Depends(_run),
    db: Session = Depends(get_db),
) -> dict:
    """연결 시험. 실패 사유를 하나로 뭉치지 않는다 —
    무엇을 고쳐야 할지 알 수 있어야 한다."""
    return envelope(ex.test_connection(
        db, org_id=user.org_id, actor_user_id=user.user_id, account_id=account_id,
    ))


@router.get("/mappings")
def list_mappings(
    external_account_id: UUID | None = Query(None),
    internal_entity_id: UUID | None = Query(None),
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    rows = ex.list_mappings(
        db, org_id=user.org_id, external_account_id=external_account_id,
        internal_entity_id=internal_entity_id,
    )
    return envelope(rows, {"total": len(rows)})


@router.post("/mappings", status_code=201)
def create_mapping(
    payload: MappingIn,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> dict:
    """외부 항목 ↔ 내부 항목 연결.

    이미 다른 내부 항목에 붙어 있으면 409 다 — 조용히 옮기지 않는다.
    """
    return envelope(ex.upsert_mapping(
        db, org_id=user.org_id, actor_user_id=user.user_id, **payload.model_dump(),
    ))


@router.post("/mappings/{mapping_id}/remap")
def remap(
    mapping_id: UUID,
    payload: RemapIn,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(ex.remap_mapping(
        db, org_id=user.org_id, actor_user_id=user.user_id, mapping_id=mapping_id,
        internal_entity_id=payload.internal_entity_id, reason=payload.reason,
    ))


@router.post("/accounts/{account_id}/sync/products")
def sync_products(
    account_id: UUID,
    since: date | None = Query(None),
    user: CurrentUser = Depends(_run),
    db: Session = Depends(get_db),
) -> dict:
    """상품 동기화.

    커넥터가 없으면 **501** 이다 — 입력이 잘못된 게 아니라 우리가 아직 못 한다.
    성공으로 만들지 않는다.
    """
    result = ex.sync_products(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        account_id=account_id, since=since,
    )
    return envelope(result.as_dict())


@router.get("/order-sync/status")
def order_sync_status(
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    """주문 자동 동기화 상태.

    운영자가 답해야 하는 질문은 다섯 개다.

    ```
    자동으로 돌고 있나?        enabled · interval_minutes
    마지막으로 언제 봤나?      last_attempt_at
    그게 성공했나?            last_status · last_error_code
    마지막 성공은 언제인가?    last_success_at
    몇 건이나 들어왔나?        last counts
    ```

    **개인정보는 없다.** 건수와 상태 토큰만 나간다.
    """
    from app.services import order_sync as osync

    return envelope(osync.status(db, org_id=user.org_id))


@router.post("/order-sync/run")
def run_order_sync(
    account_id: UUID | None = Query(None),
    user: CurrentUser = Depends(_run),
    db: Session = Depends(get_db),
) -> dict:
    """지금 한 번 동기화한다.

    자동 실행을 대체하지 않는다 — 진단·장애 복구용이다. 자동 실행이 돌고
    있으면 **성공한 척하지 않고** `ALREADY_RUNNING` 으로 끝난다. 잠금은 자동과
    같은 것을 쓴다.
    """
    from app.services import order_sync as osync

    outcomes = osync.run_now(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        external_account_id=account_id)
    return envelope([o.as_dict() for o in outcomes],
                    {"total": len(outcomes)})


@router.get("/claim-sync/status")
def claim_sync_status(
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    """취소·반품 수집 상태와 그 재무 영향.

    주문과 **한 칸으로 묶지 않는다.** 주문이 잘 들어와도 클레임 수집이
    실패하고 있으면 매출이 실제보다 크게 잡혀 있다.
    """
    from app.services import claim_sync as csync

    return envelope(csync.status(db, org_id=user.org_id))


@router.post("/claim-sync/run")
def run_claim_sync(
    account_id: UUID | None = Query(None),
    user: CurrentUser = Depends(_run),
    db: Session = Depends(get_db),
) -> dict:
    """지금 한 번 수집한다. 자동 실행이 돌고 있으면 겹치지 않는다."""
    from app.services import claim_sync as csync

    outcomes = csync.run_now(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        external_account_id=account_id)
    return envelope([o.as_dict() for o in outcomes], {"total": len(outcomes)})


@router.get("/sync-runs")
def list_runs(
    account_id: UUID | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    rows = ex.list_runs(db, org_id=user.org_id, account_id=account_id, limit=limit)
    return envelope(rows, {"total": len(rows)})


@router.get("/conflicts")
def list_conflicts(
    pending_only: bool = Query(True),
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    """자동으로 정할 수 없어 남긴 충돌.

    사람이 고친 값을 외부가 덮으려 할 때 여기 쌓인다 — 조용히 덮으면
    사용자는 같은 수정을 매번 반복하게 된다.
    """
    rows = ex.list_conflicts(
        db, org_id=user.org_id, pending_only=pending_only, viewer_role=user.role,
    )
    return envelope(rows, {"total": len(rows)})


# ──────────────────────────────────────────────────────────────────────────
# 판매처 스토어 매핑 (M052)
# ──────────────────────────────────────────────────────────────────────────
#
# 셀러툴이 "이 주문은 mall02400 것" 이라고 말할 때, 그 코드가 우리 어느 판매처
# 계정인지를 **사람이 정해 준다.** 이름으로 자동 연결하지 않는다 — `mall_name`
# 이 '쿠팡' 이라고 첫 번째 쿠팡 계정에 붙이면, 2호점이 생기는 날 과거 주문까지
# 전부 잘못된 계정에 귀속된다.


class StoreMappingIn(BaseModel):
    """스토어 코드 → 판매처 계정.

    **`mall_name` 을 받지 않는다.** 이름을 받으면 언젠가 그 이름으로 계정을
    찾는 코드가 붙는다. 받는 것은 사용자가 고른 계정의 id 뿐이다.
    """

    mall_cd: str = Field(..., min_length=1, max_length=100)
    integration_account_id: UUID


@router.get("/accounts/{account_id}/store-mappings")
def list_store_mappings(
    account_id: UUID,
    user: CurrentUser = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    from app.services import next_engine_order_sync as nes

    rows = nes.list_store_mappings(
        db, org_id=user.org_id, external_account_id=account_id)
    return envelope(rows, {"total": len(rows)})


@router.put("/accounts/{account_id}/store-mappings")
def map_store(
    account_id: UUID,
    payload: StoreMappingIn,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> dict:
    """스토어를 판매처 계정에 연결한다.

    이미 다른 계정에 연결돼 있으면 **409** 다. 조용히 갈아끼우면 그 스토어로
    이미 들어온 매출의 귀속이 통째로 바뀐다.
    """
    from app.services import next_engine_order_sync as nes

    return envelope(nes.map_store_to_account(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        external_account_id=account_id, mall_cd=payload.mall_cd,
        integration_account_id=payload.integration_account_id,
    ))


@router.delete("/accounts/{account_id}/store-mappings/{mall_cd}", status_code=204,
               response_model=None)
def unmap_store(
    account_id: UUID,
    mall_cd: str,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> None:
    from app.services import next_engine_order_sync as nes

    nes.unmap_store(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        external_account_id=account_id, mall_cd=mall_cd)


class ProductMappingIn(BaseModel):
    """외부 상품코드 → 우리 상품.

    부트스트랩(판매자상품코드 정확 일치)이 0건인 조직에서는 이 경로가 유일한
    연결 수단이다. **상품명을 받지 않는다** — 이름으로 잇는 코드가 붙을 자리를
    만들지 않는다.
    """

    ne_product_cd: str = Field(..., min_length=1, max_length=200)
    product_id: UUID


@router.put("/accounts/{account_id}/product-mappings")
def map_product(
    account_id: UUID,
    payload: ProductMappingIn,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> dict:
    from app.services import next_engine_order_sync as nes

    return envelope(nes.map_product_to_internal(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        external_account_id=account_id, ne_product_cd=payload.ne_product_cd,
        product_id=payload.product_id,
    ))


@router.delete("/accounts/{account_id}/product-mappings/{ne_product_cd}",
               status_code=204, response_model=None)
def unmap_product(
    account_id: UUID,
    ne_product_cd: str,
    user: CurrentUser = Depends(_manage),
    db: Session = Depends(get_db),
) -> None:
    """상품 연결 해제.

    연결이 틀렸다는 것은 나중에야 드러난다. 되돌릴 길이 없으면 그때 할 수 있는
    일이 DB 를 직접 고치는 것뿐이고, 그건 감사에도 남지 않는다.

    이미 적재된 주문은 건드리지 않는다 — 연결을 끊는 것과 과거 매출을 고치는
    것은 다른 결정이다.
    """
    from app.services import next_engine_order_sync as nes

    nes.unmap_product(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        external_account_id=account_id, ne_product_cd=ne_product_cd)
