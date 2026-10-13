"""재무 정책 변경 승인 (Phase A-2).

이 파일이 고정하는 것
------------------
1. 금액을 움직이는 필드는 **일반 PATCH 로 못 바꾼다** (승인 우회 차단)
2. 금액과 무관한 필드(메모·근거 링크)는 **직접 바꿀 수 있다**
3. 승인 적용은 기존 행 UPDATE 가 아니라 **새 version 생성**이다
4. 옛 version 은 지워지지 않고 적용일 전날까지 유효하게 닫힌다
5. **과거 월의 계산 근거가 바뀌지 않는다** — 이 전체의 목적이다
6. 구간별 수수료(TIERED)는 backend 가 검증한다

왜 3~5 가 중요한가
----------------
정책 행을 그 자리에서 고치면 2026-07 확정 snapshot 이 근거로 삼은 `v7` 의
수수료율이 바뀐다. 그 달을 재현하면 다른 숫자가 나온다 — 그건 확정이 아니다.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ApprovalRequiredError, ValidationError
from app.domain.policy_crud import PolicyCreate, PolicyUpdate
from app.services import policy_crud
from app.services.approval_policy import validate_tiers
from app.services.approval_targets import get_adapter
from app.tests.fixtures.factories import make_app_user, make_organization

TODAY = date(2026, 8, 10)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


def _fee_policy(db: Session, org: uuid.UUID, actor: uuid.UUID, *, rate: str = "0.108"):
    db.execute(text("""
        INSERT INTO sale_channel (org_id, code, display_name)
        VALUES (:o, 'COUPANG', '쿠팡') ON CONFLICT DO NOTHING
    """), {"o": str(org)})
    return policy_crud.create_policy(
        db, org_id=org, actor=actor, kind="marketplace_fee",
        body=PolicyCreate(
            version=1, effective_from=date(2026, 1, 1),
            fields={
                "marketplace": "COUPANG", "fee_type": "RATE",
                "rate": rate, "fee_vat_included": True,
            },
        ),
    )


def _tax_policy(db: Session, org: uuid.UUID, actor: uuid.UUID):
    return policy_crud.create_policy(
        db, org_id=org, actor=actor, kind="tax",
        body=PolicyCreate(
            version=1, effective_from=date(2026, 1, 1),
            fields={
                "tax_type": "GENERAL", "vat_rate": "0.1",
                "calculation_method": "STANDARD_RATIO",
                "default_vat_inclusion": "INCLUDED", "applies_to": "ALL",
            },
        ),
    )


# ──────────────────────────────────────────────────────────────────────────
# 우회 차단
# ──────────────────────────────────────────────────────────────────────────

def test_fee_rate_cannot_be_changed_by_direct_patch(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """수수료율은 마진의 입력값이다. 일반 수정 API 로 바꿀 수 없다."""
    p = _fee_policy(db, org, actor)

    with pytest.raises(ApprovalRequiredError) as err:
        policy_crud.update_policy(
            db, org_id=org, actor=actor, kind="marketplace_fee", policy_id=p.id,
            body=PolicyUpdate(fields={"rate": "0.2"}),
        )
    assert "rate" in err.value.detail["approval_required_fields"]
    assert err.value.detail["target_type"] == "policy_marketplace_fee"
    assert err.value.detail["request_type"] == "FEE_POLICY_CHANGE"


def test_tax_rate_cannot_be_changed_by_direct_patch(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    p = _tax_policy(db, org, actor)
    with pytest.raises(ApprovalRequiredError):
        policy_crud.update_policy(
            db, org_id=org, actor=actor, kind="tax", policy_id=p.id,
            body=PolicyUpdate(fields={"vat_rate": "0.15"}),
        )


def test_mixing_allowed_and_blocked_fields_rejects_everything(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """일부만 저장하면 무엇이 반영됐는지 알 수 없다."""
    p = _fee_policy(db, org, actor)

    with pytest.raises(ApprovalRequiredError):
        policy_crud.update_policy(
            db, org_id=org, actor=actor, kind="marketplace_fee", policy_id=p.id,
            body=PolicyUpdate(memo="근거 갱신", fields={"rate": "0.2"}),
        )

    after = policy_crud.get_policy(db, org_id=org, kind="marketplace_fee", policy_id=p.id)
    assert after.memo != "근거 갱신", "거부됐는데 메모가 저장됐다"


def test_non_financial_fields_stay_directly_editable(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """근거 링크를 고치는 데 승인을 요구하면 사람들이 승인 자체를 우회한다."""
    p = _fee_policy(db, org, actor)

    out = policy_crud.update_policy(
        db, org_id=org, actor=actor, kind="marketplace_fee", policy_id=p.id,
        body=PolicyUpdate(memo="쿠팡 공지 2026-08", source_url="https://example.test/notice"),
    )
    assert out.memo == "쿠팡 공지 2026-08"


def test_non_financial_policy_kinds_are_not_gated(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """비용 카테고리 이름은 금액을 움직이지 않는다. 승인 대상이 아니다."""
    p = policy_crud.create_policy(
        db, org_id=org, actor=actor, kind="expense_category",
        body=PolicyCreate(
            version=1, effective_from=date(2026, 1, 1),
            fields={"name": "사무용품", "kind": "OFFICE",
                    "default_tax_deductible": True,
                    "default_settlement_deductible": True, "sort_order": 1},
        ),
    )
    out = policy_crud.update_policy(
        db, org_id=org, actor=actor, kind="expense_category", policy_id=p.id,
        body=PolicyUpdate(fields={"name": "사무용품·소모품"}),
    )
    assert out.fields["name"] == "사무용품·소모품"


# ──────────────────────────────────────────────────────────────────────────
# 적용 = 새 version
# ──────────────────────────────────────────────────────────────────────────

def test_approval_creates_a_new_version_instead_of_updating(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """기존 행을 고치면 그 정책을 근거로 확정한 달을 재현할 수 없게 된다."""
    p = _fee_policy(db, org, actor, rate="0.108")
    adapter = get_adapter("policy_marketplace_fee")

    adapter.apply_changes(
        db, org_id=org, target_id=p.id, proposed={"rate": "0.12"},
        actor_user_id=actor, request_type="FEE_POLICY_CHANGE",
        effective_from=TODAY,
    )
    db.commit()

    rows = db.execute(text("""
        SELECT version, rate, effective_from, effective_to, is_active
          FROM marketplace_fee_policy
         WHERE org_id = :o AND deleted_at IS NULL
         ORDER BY version
    """), {"o": str(org)}).mappings().all()

    assert len(rows) == 2, "새 version 이 생기지 않았거나 옛 version 이 사라졌다"

    old, new = rows
    # 옛 version 의 **비율은 그대로다** — 여기가 핵심이다.
    assert Decimal(str(old["rate"])) == Decimal("0.108")
    assert old["effective_to"] == TODAY - timedelta(days=1)
    assert old["is_active"] is False

    assert Decimal(str(new["rate"])) == Decimal("0.12")
    assert new["effective_from"] == TODAY
    assert new["effective_to"] is None
    assert new["is_active"] is True


def test_the_past_still_resolves_to_the_old_version(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """정책 변경 이전 날짜로 조회하면 옛 값이 나와야 한다.

    이것이 승인 적용을 UPDATE 가 아니라 새 version 으로 만든 이유 전부다.
    """
    p = _fee_policy(db, org, actor, rate="0.108")
    get_adapter("policy_marketplace_fee").apply_changes(
        db, org_id=org, target_id=p.id, proposed={"rate": "0.12"},
        actor_user_id=actor, request_type="FEE_POLICY_CHANGE", effective_from=TODAY,
    )
    db.commit()

    def rate_at(at: date) -> Decimal:
        v = db.execute(text("""
            SELECT rate FROM marketplace_fee_policy
             WHERE org_id = :o AND deleted_at IS NULL
               AND effective_from <= :at
               AND (effective_to IS NULL OR effective_to >= :at)
        """), {"o": str(org), "at": at}).scalar_one()
        return Decimal(str(v))

    assert rate_at(date(2026, 7, 1)) == Decimal("0.108"), "과거 수수료가 바뀌었다"
    assert rate_at(TODAY) == Decimal("0.12")
    # 두 정책이 같은 날 동시에 유효하면 계산이 어느 쪽을 쓸지 알 수 없다.
    n = db.execute(text("""
        SELECT COUNT(*) FROM marketplace_fee_policy
         WHERE org_id = :o AND deleted_at IS NULL
           AND effective_from <= :at AND (effective_to IS NULL OR effective_to >= :at)
    """), {"o": str(org), "at": TODAY}).scalar()
    assert n == 1, "같은 날 유효한 정책이 둘이다"


def test_applying_before_the_existing_start_is_rejected(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """소급 적용은 이미 확정한 달의 근거를 바꾼다."""
    p = _fee_policy(db, org, actor)
    with pytest.raises(ValidationError):
        get_adapter("policy_marketplace_fee").apply_changes(
            db, org_id=org, target_id=p.id, proposed={"rate": "0.12"},
            actor_user_id=actor, request_type="FEE_POLICY_CHANGE",
            effective_from=date(2025, 12, 1),
        )


def test_unowned_field_is_rejected_at_apply_time(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """DB 를 직접 건드려 심어도 적용 단계에서 막힌다."""
    p = _fee_policy(db, org, actor)
    with pytest.raises(ValidationError):
        get_adapter("policy_marketplace_fee").apply_changes(
            db, org_id=org, target_id=p.id, proposed={"memo": "우회 시도"},
            actor_user_id=actor, request_type="FEE_POLICY_CHANGE", effective_from=TODAY,
        )


def test_a_rate_entered_as_percent_is_rejected(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """10.8% 를 10.8 로 넣는 실수는 흔하다.

    그대로 통과시키면 수수료가 1080% 가 되고 마진이 크게 음수로 나온다 —
    사용자는 원가가 잘못됐다고 생각하고 엉뚱한 곳을 고친다.
    """
    p = _fee_policy(db, org, actor)
    with pytest.raises(ValidationError) as err:
        get_adapter("policy_marketplace_fee").apply_changes(
            db, org_id=org, target_id=p.id, proposed={"rate": "10.8"},
            actor_user_id=actor, request_type="FEE_POLICY_CHANGE", effective_from=TODAY,
        )
    assert "0.108" in str(err.value)


def test_policy_approval_does_not_leak_across_orgs(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    other = make_organization(db)
    other_actor = make_app_user(db, org_id=other, role="ADMIN")
    p = _fee_policy(db, other, other_actor)

    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        get_adapter("policy_marketplace_fee").validate_target(
            db, org_id=org, target_id=p.id,
        )


# ──────────────────────────────────────────────────────────────────────────
# 구간별 수수료 (Phase A-3) — backend 검증
#
# 프론트에서만 막으면 API 를 직접 부르는 순간 뚫린다.
# ──────────────────────────────────────────────────────────────────────────

def test_valid_tiers_pass():
    validate_tiers([
        {"min": 0, "max": 10_000_000, "rate": 0.108},
        {"min": 10_000_001, "max": 30_000_000, "rate": 0.102},
        {"min": 30_000_001, "max": None, "rate": 0.098},
    ])


@pytest.mark.parametrize("tiers,reason", [
    ([], "빈 구간"),
    ([{"min": 100, "max": None, "rate": 0.1}], "0원부터 시작하지 않음"),
    ([{"min": 0, "max": 1000, "rate": 0.1}], "마지막 구간에 상한이 있음"),
    (
        [{"min": 0, "max": 1000, "rate": 0.1}, {"min": 500, "max": None, "rate": 0.2}],
        "구간 겹침",
    ),
    (
        [{"min": 0, "max": 1000, "rate": 0.1}, {"min": 2000, "max": None, "rate": 0.2}],
        "구간 사이 빔",
    ),
    ([{"min": 0, "max": 0, "rate": 0.1}], "max <= min"),
    ([{"min": 0, "max": None, "rate": -0.1}], "음수 수수료율"),
    ([{"min": 0, "max": None, "rate": 10.8}], "비율이 아니라 퍼센트"),
    ([{"min": -1, "max": None, "rate": 0.1}], "음수 최소 금액"),
    ([{"min": 0, "max": None}], "수수료율 누락"),
])
def test_invalid_tiers_are_rejected(tiers, reason):
    """겹치거나 비면 같은 판매가에 수수료가 둘이거나 하나도 없게 된다."""
    with pytest.raises(ValidationError):
        validate_tiers(tiers)


def test_tiered_policy_is_validated_through_the_approval_path(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    p = _fee_policy(db, org, actor)
    adapter = get_adapter("policy_marketplace_fee")

    with pytest.raises(ValidationError):
        adapter.apply_changes(
            db, org_id=org, target_id=p.id, actor_user_id=actor,
            request_type="FEE_POLICY_CHANGE", effective_from=TODAY,
            proposed={
                "fee_type": "TIERED",
                "tier_json": [{"min": 0, "max": 1000, "rate": 0.1}],  # 상한 있는 마지막 구간
            },
        )

    adapter.apply_changes(
        db, org_id=org, target_id=p.id, actor_user_id=actor,
        request_type="FEE_POLICY_CHANGE", effective_from=TODAY,
        proposed={
            "fee_type": "TIERED",
            "tier_json": [
                {"min": 0, "max": 10_000_000, "rate": 0.108},
                {"min": 10_000_001, "max": None, "rate": 0.098},
            ],
        },
    )
    db.commit()

    stored = db.execute(text("""
        SELECT tier_json FROM marketplace_fee_policy
         WHERE org_id = :o AND is_active = TRUE AND deleted_at IS NULL
    """), {"o": str(org)}).scalar_one()
    assert len(stored) == 2


def test_tiers_travel_as_a_json_string_through_the_approval_payload(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """승인 요청은 평면 스칼라만 담는다 — 구간 목록은 JSON 문자열로 온다.

    중첩을 허용하면 어떤 컬럼이 바뀌는지 정적으로 알 수 없고 변경 전후 비교 UI 도
    만들 수 없다(`domain/approval.ProposedChanges`). 그래서 문자열로 받고
    어댑터가 되돌린다.
    """
    import json as _json

    from app.domain.approval import ProposedChanges

    p = _fee_policy(db, org, actor)
    tiers = [
        {"min": 0, "max": 10_000_000, "rate": 0.108},
        {"min": 10_000_001, "max": None, "rate": 0.098},
    ]
    payload = {"fee_type": "TIERED", "tier_json": _json.dumps(tiers)}

    # 요청 검증을 실제로 통과해야 한다 — 리스트로 보내면 여기서 막힌다.
    adapter = get_adapter("policy_marketplace_fee")
    ProposedChanges.validate_payload(payload, allowed_fields=adapter.allowed_fields)

    with pytest.raises(ValidationError):
        ProposedChanges.validate_payload(
            {"tier_json": tiers}, allowed_fields=adapter.allowed_fields,
        )

    adapter.apply_changes(
        db, org_id=org, target_id=p.id, proposed=payload, actor_user_id=actor,
        request_type="FEE_POLICY_CHANGE", effective_from=TODAY,
    )
    db.commit()

    stored = db.execute(text("""
        SELECT tier_json FROM marketplace_fee_policy
         WHERE org_id = :o AND is_active = TRUE AND deleted_at IS NULL
    """), {"o": str(org)}).scalar_one()
    # DB 에는 문자열이 아니라 JSONB 로 들어가야 한다.
    assert isinstance(stored, list)
    assert stored[0]["rate"] == 0.108
