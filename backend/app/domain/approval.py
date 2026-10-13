"""승인 요청 도메인 모델 — JSONB 실제 검증 (M018 안정화).

컬럼 COMMENT 는 **검증 장치가 아니라 문서**다. 실제 검증을 여기서 한다.

원칙
----
- 임의 key 금지 (`extra="forbid"`)
- 서버 관리 필드는 요청자가 못 바꾼다 (`SERVER_MANAGED`)
- 빈 변경 요청 거부
- before/after 스냅샷 형태 통일
- **API 입력 모델과 service 진입점 양쪽에서 검증한다.**
  라우터만 검증하면 service 를 직접 부르는 경로(실행기·배치·테스트)가 뚫린다.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.exceptions import ValidationError

# 어떤 대상이든 요청자가 손댈 수 없는 필드.
# JSONB 로 들어오면 조용히 UPDATE 문에 섞일 수 있어 이름 단위로 막는다.
#
# ⚠️ `status` 는 여기 넣지 않는다. 상품 상태(`product.status`)는 정당한 승인 대상이고
#    (STATUS_CHANGE), 범용 차단 목록에 넣으면 그 요청 유형이 통째로 막힌다.
#    이름이 흔한 필드는 **대상별 allowlist**(approval_targets.FIELD_OWNERSHIP)가 통제한다.
#    여기에는 어느 테이블에서든 시스템이 소유하는 이름만 남긴다.
SERVER_MANAGED_FIELDS: frozenset[str] = frozenset({
    "id", "org_id", "business_key",
    "created_at", "created_by", "updated_at", "updated_by",
    "deleted_at", "deleted_by", "version",
    # 워크플로 상태 — 승인 요청으로 직접 바꿀 수 있으면 안 되는 값들
    "profit_status", "close_state",
    "reviewed_by", "reviewed_at", "applied_by", "applied_at",
    "decision_outcome", "auto_apply_status", "applied_by_type",
})


class ProposedChanges(BaseModel):
    """`proposed_changes_json` 의 형태.

    평면 dict 다. 중첩 객체를 허용하지 않는다 — 중첩을 허용하면 어떤 컬럼이
    바뀌는지 정적으로 알 수 없고, 변경 전후 비교 UI 도 만들 수 없다.
    """

    model_config = ConfigDict(extra="allow")

    @classmethod
    def validate_payload(
        cls, payload: dict[str, Any], *, allowed_fields: frozenset[str]
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValidationError("변경 내용은 객체여야 합니다.")
        if not payload:
            raise ValidationError("변경할 내용이 없습니다.")

        blocked = sorted(set(payload) & SERVER_MANAGED_FIELDS)
        if blocked:
            raise ValidationError(
                f"서버가 관리하는 필드는 승인으로 변경할 수 없습니다: {blocked}",
                detail={"blocked_fields": blocked},
            )

        unknown = sorted(set(payload) - allowed_fields)
        if unknown:
            raise ValidationError(
                f"승인으로 변경할 수 없는 필드입니다: {unknown}",
                detail={"unknown_fields": unknown, "allowed": sorted(allowed_fields)},
            )

        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                raise ValidationError(
                    f"'{key}' 는 중첩 구조입니다. 값은 문자열·숫자·불리언·null 이어야 합니다.",
                    detail={"field": key},
                )
        return dict(payload)


class ApprovalCreateInput(BaseModel):
    """승인 요청 생성 입력. 라우터와 service 가 같은 모델을 쓴다."""

    model_config = ConfigDict(extra="forbid")

    target_type: str = Field(min_length=1, max_length=64)
    target_id: UUID
    request_type: str = Field(min_length=1, max_length=64)
    proposed_changes: dict[str, Any]
    reason: str | None = Field(default=None, max_length=1000)
    effective_from: date | None = None
    submit: bool = False

    @field_validator("proposed_changes")
    @classmethod
    def _not_empty(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not v:
            raise ValidationError("변경할 내용이 없습니다.")
        return v


def canonical_fingerprint(
    *,
    target_type: str,
    target_id: UUID | str,
    request_type: str,
    proposed_changes: dict[str, Any],
    effective_from: date | str | None,
) -> str:
    """요청 본문의 canonical 해시.

    key 순서·공백 때문에 해시가 달라지면 안 되므로 `sort_keys=True` +
    구분자를 고정한다. 값은 `default=str` 로 직렬화해 date/UUID 를 안정적으로 처리한다.

    민감값을 원문으로 저장하지 않기 위해 해시만 남긴다.
    """
    payload = {
        "target_type": target_type,
        "target_id": str(target_id),
        "request_type": request_type,
        "proposed_changes": proposed_changes,
        "effective_from": str(effective_from) if effective_from else None,
    }
    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class SnapshotDiffRow(BaseModel):
    """변경 전후 비교 한 줄. 프론트가 이 형태를 그대로 렌더한다."""

    field: str
    label: str
    before: Any
    after: Any
    changed: bool


def build_diff(
    before: dict[str, Any] | None,
    proposed: dict[str, Any] | None,
    *,
    labels: dict[str, str] | None = None,
) -> list[SnapshotDiffRow]:
    """before/proposed → 필드별 비교표.

    프론트가 JSON 을 그대로 노출하지 않도록 서버가 비교 구조를 만들어 준다.
    양쪽 키를 합집합으로 훑되 서버 관리 필드는 제외한다.
    """
    before = before or {}
    proposed = proposed or {}
    labels = labels or {}
    keys = sorted((set(before) | set(proposed)) - SERVER_MANAGED_FIELDS)
    rows: list[SnapshotDiffRow] = []
    for k in keys:
        b = before.get(k)
        a = proposed.get(k, b)  # 제안에 없으면 그대로 유지된다
        rows.append(SnapshotDiffRow(
            field=k, label=labels.get(k, k), before=b, after=a, changed=(k in proposed and a != b)
        ))
    return rows
