"""도메인 예외 계층.

docs/22_api_spec.md 의 표준 에러코드와 1:1 매핑.
HTTP 변환은 `core.response.domain_error_handler` 에서 일괄 처리한다.

설계 의도:
- 서비스/도메인 계층은 HTTP 를 모른다 — `DomainError` 만 raise.
- 라우터/핸들러도 try/except 를 흩뿌리지 않는다 — FastAPI exception_handler 가 envelope 화.
- 에러코드는 프론트에서 분기·번역에 쓰이므로 변경 시 docs 와 함께 갱신.
"""

from typing import Any


class DomainError(Exception):
    """모든 비즈니스 예외의 상위 타입.

    - `code`: 프론트 노출 코드 (안정적, 변경 시 docs 동기화 필요)
    - `http_status`: response.py 핸들러가 그대로 사용
    - `detail`: 추가 정보 (예: REOPEN 한도 초과 시 limit/used 값)
    """

    code: str = "DOMAIN_ERROR"
    http_status: int = 400

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class NotFoundError(DomainError):
    """리소스 없음 (404). UUID 미존재, soft-delete 포함."""

    code = "NOT_FOUND"
    http_status = 404


class ConflictError(DomainError):
    """상태 충돌 (409). 중복 키, 잘못된 상태 전이 등."""

    code = "CONFLICT"
    http_status = 409


class ValidationError(DomainError):
    """입력값 검증 실패 (422). Pydantic 외 도메인 규칙 위반."""

    code = "VALIDATION_ERROR"
    http_status = 422


class ForbiddenError(DomainError):
    """권한 부족 (403). RBAC role 불일치 또는 feature flag off."""

    code = "FORBIDDEN"
    http_status = 403


class ReopenLimitExceededError(ConflictError):
    """D-285: 월정산 REOPEN 횟수 한도 초과 (org 기본 3회).

    detail = {limit: int, used: int}
    """

    code = "REOPEN_LIMIT_EXCEEDED"


class VariantInUseError(ConflictError):
    """D-286: 옵션 variant 가 판매·정산 이력에 사용 중 — 삭제 불가."""

    code = "VARIANT_IN_USE"


class FeatureDisabledError(ForbiddenError):
    """FEATURE_* 플래그 off (예: EXTERNAL_ACCOUNTANT 라우트 Post-MVP)."""

    code = "FEATURE_DISABLED"


class InvalidVatInputError(ValidationError):
    """D-284: vat_inclusion_type 과 입력 금액 조합이 유효하지 않음."""

    code = "INVALID_VAT_INPUT"


class MaskedSecretDetectedError(ValidationError):
    """vault 에 평문 비밀번호로 추정되는 값이 입력됨 (Security Rules)."""

    code = "MASKED_SECRET_DETECTED"


class SettlementLockedError(ConflictError):
    """LOCKED 정산월에 속한 데이터 (expense 등) 변경 시도."""

    code = "SETTLEMENT_LOCKED"


# ── 승인 워크플로 (M018) ────────────────────────────────────────
# 프론트가 오류마다 다른 안내와 다음 행동을 보여줄 수 있도록 코드를 분리한다.
# "실패했습니다" 하나로 뭉치면 사용자가 무엇을 해야 할지 알 수 없다.


class ApprovalOpenRequestExistsError(ConflictError):
    """같은 대상에 이미 진행 중인 승인 요청이 있음. details 에 기존 요청 id."""

    code = "APPROVAL_OPEN_REQUEST_EXISTS"


class ApprovalInvalidTransitionError(ConflictError):
    """허용되지 않은 상태 전이."""

    code = "APPROVAL_INVALID_TRANSITION"


class ApprovalVersionConflictError(ConflictError):
    """요청 이후 대상이 바뀌었거나(base_version), 다른 요청이 먼저 상태를 바꿨다."""

    code = "APPROVAL_VERSION_CONFLICT"


class ApprovalAlreadyAppliedError(ConflictError):
    """이미 적용된 요청."""

    code = "APPROVAL_ALREADY_APPLIED"


class IdempotencyKeyReusedError(ConflictError):
    """같은 idempotency key 로 **다른 내용**의 요청이 들어옴."""

    code = "IDEMPOTENCY_KEY_REUSED"


class ApprovalSelfApprovalForbiddenError(ForbiddenError):
    """본인이 요청한 건은 승인할 수 없음(조직 설정으로 해제 가능)."""

    code = "APPROVAL_SELF_APPROVAL_FORBIDDEN"


class ApprovalPermissionDeniedError(ForbiddenError):
    """이 동작에 필요한 역할이 없음."""

    code = "APPROVAL_PERMISSION_DENIED"


class ApprovalTargetUnsupportedError(ValidationError):
    """아직 승인 대상으로 지원하지 않는 유형. details.supported 에 목록."""

    code = "APPROVAL_TARGET_UNSUPPORTED"


class ApprovalTargetNotFoundError(NotFoundError):
    """승인 대상이 없거나 다른 조직 것. 조직 격리 — 403 이 아니라 404 다."""

    code = "APPROVAL_TARGET_NOT_FOUND"


class ApprovalRequiredError(ForbiddenError):
    """승인이 필요한 필드를 일반 수정 API 로 바꾸려 했다.

    403 인 이유: 인증은 됐고 권한도 있을 수 있지만 **그 경로로는 못 한다**.
    422(검증 실패)로 내면 "값이 잘못됐다" 로 읽혀 사용자가 값을 고치려 든다.
    details.approval_required_fields 로 어떤 필드가 막혔는지 알려준다.
    """

    code = "APPROVAL_REQUIRED"


class ProductVersionConflictError(ConflictError):
    """상품을 읽은 뒤 저장하기 전에 다른 사람이 먼저 저장했다.

    마지막 저장이 이기게 두면 앞사람의 원가 수정이 조용히 사라진다. 금액 데이터에서
    이건 감사 로그를 봐야만 알 수 있는 종류의 손실이라 409 로 막는다.

    `product` 에 version 컬럼이 없어 `updated_at` 을 토큰으로 쓴다. 컬럼을 새로 추가하는
    것보다 마이그레이션 부담이 없고, 같은 상품 행의 모든 쓰기가 `updated_at` 을 갱신하므로
    보호 범위는 동일하다.

    details 에 `current_version` 을 실어 준다 — 프론트가 최신값을 다시 불러와
    사용자가 입력한 값과 비교시켜 줄 수 있어야 한다.
    """

    code = "PRODUCT_VERSION_CONFLICT"


class ApprovalExecutorAlreadyRunningError(ConflictError):
    """자동 적용 배치가 이미 실행 중이다.

    프론트 버튼 disabled 는 방어가 아니다 — 새로고침, 두 번째 탭, 직접 API 호출,
    cron 과 수동 실행 겹침으로 전부 뚫린다. 서버가 거절해야 한다.
    """

    code = "APPROVAL_EXECUTOR_ALREADY_RUNNING"


class ConnectorUnavailableError(ForbiddenError):
    """판매처 가격 전송 connector 가 아직 연결되지 않았다.

    422(검증 실패)가 아니다 — 입력이 잘못된 게 아니라 **그 기능이 아직 없다.**
    사용자가 값을 고쳐도 달라지지 않으므로 그렇게 안내하면 안 된다.

    내부 가격과 승인 기록은 정상이다. 이 오류는 외부 반영에만 해당한다.
    """

    code = "CONNECTOR_UNAVAILABLE"


class ChannelPriceOverlapError(ConflictError):
    """같은 listing·variant 의 가격 유효기간이 겹친다.

    겹치면 "지금 가격" 이 둘이 되고, 마진 계산이 어느 쪽을 쓰는지가 쿼리 순서에 달린다.
    DB exclusion constraint 가 최종 방어선이고 이건 그 번역이다.
    """

    code = "CHANNEL_PRICE_OVERLAP"


class ChannelPriceImmutableError(ConflictError):
    """이미 적용된(또는 지난) 가격은 수정할 수 없다.

    과거 가격을 고치면 "그때 얼마에 팔았나" 를 잃는다. 정정은 **새 버전 생성**이다.
    """

    code = "CHANNEL_PRICE_IMMUTABLE"
