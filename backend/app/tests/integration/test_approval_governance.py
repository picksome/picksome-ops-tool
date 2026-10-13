"""승인 거버넌스 — 우회 차단 / 필드 소유권 / 민감정보 / 반려 통계 / 실행 권한.

이 파일이 검증하는 것은 "승인 워크플로가 실제로 강제되는가" 다.
승인 화면을 만들어 놓고 일반 수정 API 를 열어두면 승인은 장식이 된다.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import ApprovalRequiredError, NotFoundError, ValidationError
from app.core.sensitive import (
    PERM_VIEW_SENSITIVE_DIFF,
    build_redacted_diff,
    can_view_sensitive_diff,
    redact_approval_row,
)
from app.domain.product import ProductUpdate
from app.services import approval as svc
from app.services import approval_targets as targets
from app.services import product as product_svc

ORG = uuid.UUID("00000000-0000-0000-0000-0000000000ff")


@pytest.fixture
def sessions(_test_db_ready: str) -> Iterator[tuple[Session, Session]]:
    eng = create_engine(_test_db_ready, future=True)
    Maker = sessionmaker(bind=eng, future=True, expire_on_commit=False)
    a, b = Maker(), Maker()
    try:
        yield a, b
    finally:
        for s in (a, b):
            s.rollback()
            s.close()
        eng.dispose()


def _user(db: Session, email: str, role: str, org: uuid.UUID = ORG) -> uuid.UUID:
    row = db.execute(text("SELECT id FROM app_user WHERE org_id = :o AND email = :e"),
                     {"o": str(org), "e": email}).first()
    if row:
        db.execute(text("UPDATE app_user SET role = :r WHERE id = :i"),
                   {"r": role, "i": str(row.id)})
        db.commit()
        return uuid.UUID(str(row.id))
    uid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO app_user (id, org_id, email, password_hash, display_name, role, is_active)
        VALUES (:i, :o, :e, 'x', :n, :r, TRUE)
    """), {"i": str(uid), "o": str(org), "e": email, "n": email.split("@")[0], "r": role})
    db.commit()
    return uid


def _product(db: Session, org: uuid.UUID = ORG, cost: int = 10000) -> uuid.UUID:
    pid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO product (id, org_id, business_key, name_ko, margin_type, status, cost_amount, memo)
        VALUES (:i, :o, :bk, '거버넌스 테스트 상품', 'GENERAL_RESELL', 'ACTIVE', :c, '초기 메모')
    """), {"i": str(pid), "o": str(org), "bk": f"PROD-{pid.hex}", "c": cost})
    db.commit()
    return pid


def _cleanup(db: Session) -> None:
    db.execute(text("DELETE FROM audit_log WHERE org_id = :o"), {"o": str(ORG)})
    db.execute(text("DELETE FROM approval_request WHERE org_id = :o"), {"o": str(ORG)})
    db.commit()


@pytest.fixture
def ctx(sessions):
    a, b = sessions
    _cleanup(a)
    yield {
        "a": a, "b": b,
        "requester": _user(a, "gov-req@picksome.local", "OPERATOR"),
        "approver": _user(a, "gov-apr@picksome.local", "ADMIN"),
        "product": _product(a),
    }
    _cleanup(a)


# ──────────────────────────────────────────────────────────────
# 1. 승인 우회 차단
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("field,value", [
    ("cost_amount", 99999),
    ("name_ko", "우회 시도"),
    ("supply_url", "https://evil.example.com"),
    ("status", "PAUSED"),
    ("category_path", "우회"),
    ("margin_type", "ROCKET_GROWTH"),
])
def test_approval_fields_cannot_be_changed_directly(ctx, field, value):
    """승인 대상 필드는 일반 상품 수정 API 로 바꿀 수 없다.

    승인 워크플로를 만들어 놓고 PATCH /products 를 열어두면 승인이 장식이 된다.
    """
    a = ctx["a"]
    with pytest.raises(ApprovalRequiredError) as exc:
        product_svc.update_product(
            a, org_id=ORG, actor=ctx["requester"], product_id=ctx["product"],
            body=ProductUpdate(**{field: value}),
        )
    a.rollback()
    assert exc.value.code == "APPROVAL_REQUIRED"
    assert field in exc.value.detail["approval_required_fields"]

    # 실제로 안 바뀌었는지 확인 — 예외만 던지고 값은 바뀌는 경우를 막는다
    row = a.execute(text(f"SELECT {field} FROM product WHERE id = :i"),
                    {"i": str(ctx["product"])}).scalar()
    assert str(row) != str(value)


def test_direct_save_fields_still_work(ctx):
    """`memo` 같은 직접 저장 필드는 승인 없이 저장된다.

    운영 영향이 없는 값까지 승인을 태우면 승인함이 잡음으로 가득 찬다.
    """
    a = ctx["a"]
    out = product_svc.update_product(
        a, org_id=ORG, actor=ctx["requester"], product_id=ctx["product"],
        body=ProductUpdate(memo="직접 저장된 메모"),
    )
    assert out.memo == "직접 저장된 메모"
    saved = a.execute(text("SELECT memo FROM product WHERE id = :i"),
                      {"i": str(ctx["product"])}).scalar()
    assert saved == "직접 저장된 메모"


def test_mixed_payload_is_rejected_entirely(ctx):
    """직접 저장 필드와 승인 필드를 섞어 보내면 **전부** 거부한다.

    일부만 저장하면 사용자는 무엇이 반영됐는지 알 수 없다.
    """
    a = ctx["a"]
    with pytest.raises(ApprovalRequiredError):
        product_svc.update_product(
            a, org_id=ORG, actor=ctx["requester"], product_id=ctx["product"],
            body=ProductUpdate(memo="같이 보냄", cost_amount=55555),
        )
    a.rollback()
    memo = a.execute(text("SELECT memo FROM product WHERE id = :i"),
                     {"i": str(ctx["product"])}).scalar()
    assert memo == "초기 메모", "거부됐으면 직접 저장 필드도 반영되면 안 된다"


def test_approval_apply_path_can_write_approval_fields(ctx):
    """승인 적용 경로는 통과한다 — 그게 유일한 정식 경로다."""
    a = ctx["a"]
    r = svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                           target_type="product", target_id=ctx["product"],
                           request_type="COST_CHANGE",
                           proposed_changes={"cost_amount": 13000}, submit=True)
    svc.approve(a, org_id=ORG, request_id=r["id"],
                actor_user_id=ctx["approver"], actor_role="ADMIN")
    svc.apply(a, org_id=ORG, request_id=r["id"],
              actor_user_id=ctx["approver"], actor_role="ADMIN")
    cost = a.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                     {"i": str(ctx["product"])}).scalar()
    assert cost == 13000


def test_other_org_product_is_not_visible(ctx):
    """조직 격리 — 다른 조직 상품은 404."""
    a = ctx["a"]
    other = uuid.uuid4()
    a.execute(text("""
        INSERT INTO organization (id, business_name, business_type, primary_currency, timezone)
        VALUES (:i, 'GOV-OTHER', 'SOLE_PROPRIETOR', 'KRW', 'Asia/Seoul')
    """), {"i": str(other)})
    a.commit()
    with pytest.raises(NotFoundError):
        product_svc.update_product(
            a, org_id=other, actor=ctx["requester"], product_id=ctx["product"],
            body=ProductUpdate(memo="다른 조직에서 수정 시도"),
        )
    a.rollback()


# ──────────────────────────────────────────────────────────────
# 2. 요청 유형별 필드 소유권
# ──────────────────────────────────────────────────────────────

def test_field_ownership_has_no_overlap():
    """한 필드가 두 request_type 에 속하면 두 승인이 서로를 덮어쓴다.

    import 시점 검증이 이미 돌지만, 표 자체를 테스트로도 고정한다.
    """
    ownership = targets.get_adapter("product").FIELD_OWNERSHIP
    seen: dict[str, str] = {}
    for rt, fields in ownership.items():
        for f in fields:
            assert f not in seen, f"{f} 가 {seen.get(f)} 와 {rt} 양쪽에 있다"
            seen[f] = rt


@pytest.mark.parametrize("request_type,field", [
    ("COST_CHANGE", "cost_amount"),
    ("PRODUCT_INFO_CHANGE", "name_ko"),
    ("STATUS_CHANGE", "status"),
])
def test_owned_field_is_accepted(ctx, request_type, field):
    a = ctx["a"]
    value = 12345 if field == "cost_amount" else ("PAUSED" if field == "status" else "새 이름")
    r = svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                           target_type="product", target_id=ctx["product"],
                           request_type=request_type, proposed_changes={field: value})
    assert r["request_type"] == request_type


@pytest.mark.parametrize("request_type,field", [
    ("COST_CHANGE", "name_ko"),
    ("PRODUCT_INFO_CHANGE", "cost_amount"),
    ("STATUS_CHANGE", "supply_url"),
])
def test_foreign_field_is_rejected(ctx, request_type, field):
    """다른 유형이 소유한 필드를 넣으면 422 + 누가 소유자인지 알려준다."""
    a = ctx["a"]
    value = 1 if field == "cost_amount" else "x"
    with pytest.raises(ValidationError) as exc:
        svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                           target_type="product", target_id=ctx["product"],
                           request_type=request_type, proposed_changes={field: value})
    assert field in exc.value.detail.get("owned_by", {})


def test_executor_path_also_validates_ownership(ctx):
    """DB 를 직접 조작해 다른 유형의 필드를 심어도 적용 단계에서 막힌다.

    생성만 검증하면 DB 를 건드리는 경로로 뚫린다.
    """
    a = ctx["a"]
    r = svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                           target_type="product", target_id=ctx["product"],
                           request_type="COST_CHANGE",
                           proposed_changes={"cost_amount": 14000}, submit=True)
    svc.approve(a, org_id=ORG, request_id=r["id"],
                actor_user_id=ctx["approver"], actor_role="ADMIN")

    # 승인 후 payload 를 다른 유형의 필드로 바꿔치기
    a.execute(text("""
        UPDATE approval_request
           SET proposed_changes_json = '{"name_ko": "몰래 바꾸기"}'::jsonb
         WHERE id = :i
    """), {"i": str(r["id"])})
    a.commit()

    with pytest.raises(ValidationError):
        svc.apply(a, org_id=ORG, request_id=r["id"],
                  actor_user_id=ctx["approver"], actor_role="ADMIN")
    a.rollback()

    name = a.execute(text("SELECT name_ko FROM product WHERE id = :i"),
                     {"i": str(ctx["product"])}).scalar()
    assert name != "몰래 바꾸기"


# ──────────────────────────────────────────────────────────────
# 3. 민감정보 redaction
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role,expected", [
    ("ADMIN", True), ("ACCOUNTING", True), ("OPERATOR", True), ("SOURCING", True),
    ("READONLY", False), ("EXTERNAL_ACCOUNTANT", False),
])
def test_permission_matrix(role, expected):
    assert can_view_sensitive_diff(role) is expected
    assert (PERM_VIEW_SENSITIVE_DIFF in
            __import__("app.core.sensitive", fromlist=["x"]).PERMISSION_MATRIX[role]) is expected


def test_redaction_removes_values_not_just_hides(ctx):
    """민감값이 **응답에서 제거**된다. 키는 남아 무엇이 가려졌는지는 알 수 있다."""
    a = ctx["a"]
    r = svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                           target_type="product", target_id=ctx["product"],
                           request_type="COST_CHANGE",
                           proposed_changes={"cost_amount": 15000})

    denied = redact_approval_row(r, allowed=False)
    assert denied["before_snapshot_json"]["cost_amount"] is None
    assert denied["proposed_changes_json"]["cost_amount"] is None
    assert "cost_amount" in denied["before_snapshot_json"], "키까지 지우면 무엇이 가려졌는지 모른다"
    assert denied["sensitive_visible"] is False

    allowed = redact_approval_row(r, allowed=True)
    assert allowed["proposed_changes_json"]["cost_amount"] == 15000


def test_non_sensitive_fields_stay_visible(ctx):
    """상품명·URL 은 가리지 않는다 — 승인자가 판단하려면 봐야 한다."""
    a = ctx["a"]
    r = svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                           target_type="product", target_id=ctx["product"],
                           request_type="PRODUCT_INFO_CHANGE",
                           proposed_changes={"name_ko": "공개 필드"})
    denied = redact_approval_row(r, allowed=False)
    assert denied["proposed_changes_json"]["name_ko"] == "공개 필드"


def test_diff_marks_redacted_distinctly_from_null():
    """`null`(값 없음)과 `redacted`(볼 수 없음)를 구분한다."""
    rows = build_redacted_diff(
        "product",
        {"cost_amount": 10000, "name_ko": "이름", "supply_url": None},
        {"cost_amount": 20000},
        allowed=False,
    )
    by_field = {r["field"]: r for r in rows}
    assert by_field["cost_amount"]["redacted"] is True
    assert by_field["cost_amount"]["sensitive"] is True
    assert by_field["cost_amount"]["before"] is None
    assert "message" in by_field["cost_amount"]

    assert by_field["supply_url"]["redacted"] is False
    assert by_field["supply_url"]["before"] is None
    assert "message" not in by_field["supply_url"], "값이 없는 것과 가려진 것은 다르다"


def test_redaction_applies_to_list_and_detail_shape(ctx):
    """목록과 상세가 같은 규칙을 쓴다. 목록만 가리면 상세 API 로 뚫린다."""
    a = ctx["a"]
    svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                       target_type="product", target_id=ctx["product"],
                       request_type="COST_CHANGE", proposed_changes={"cost_amount": 16000})
    rows, _ = svc.list_requests(db=a, org_id=ORG)
    for r in rows:
        red = redact_approval_row(r, allowed=False)
        assert red["proposed_changes_json"].get("cost_amount") is None


# ──────────────────────────────────────────────────────────────
# 4. 반려 통계 보존
# ──────────────────────────────────────────────────────────────

def test_rejection_survives_archiving(ctx):
    """반려 후 취소로 아카이빙해도 반려 사실이 남는다.

    현재 상태만 세면 반려 건이 취소로 분류되어 통계가 틀어진다.
    """
    a = ctx["a"]
    r = svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                           target_type="product", target_id=ctx["product"],
                           request_type="COST_CHANGE",
                           proposed_changes={"cost_amount": 17000}, submit=True)
    r = svc.reject(a, org_id=ORG, request_id=r["id"], actor_user_id=ctx["approver"],
                   actor_role="ADMIN", comment="근거 부족")
    assert r["decision_outcome"] == "REJECTED"

    r = svc.cancel(a, org_id=ORG, request_id=r["id"],
                   actor_user_id=ctx["approver"], actor_role="ADMIN", reason="목록 정리")
    assert r["status"] == "CANCELLED"
    assert r["decision_outcome"] == "REJECTED", "아카이빙이 반려 사실을 지우면 안 된다"
    assert r["review_comment"] == "근거 부족", "반려 사유가 유지돼야 한다"
    assert r["reviewed_by"] is not None

    # 반려 통계는 status 가 아니라 decision_outcome 으로 센다
    rejected, n = svc.list_requests(db=a, org_id=ORG, decision_outcome="REJECTED")
    assert n == 1
    by_status, n2 = svc.list_requests(db=a, org_id=ORG, status="REJECTED")
    assert n2 == 0, "status 로 세면 아카이빙된 반려가 누락된다 — 그래서 decision_outcome 이 필요하다"


def test_approval_sets_decision_outcome(ctx):
    a = ctx["a"]
    r = svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                           target_type="product", target_id=ctx["product"],
                           request_type="COST_CHANGE",
                           proposed_changes={"cost_amount": 18000}, submit=True)
    r = svc.approve(a, org_id=ORG, request_id=r["id"],
                    actor_user_id=ctx["approver"], actor_role="ADMIN")
    assert r["decision_outcome"] == "APPROVED"


def test_draft_has_no_decision(ctx):
    a = ctx["a"]
    r = svc.create_request(a, org_id=ORG, actor_user_id=ctx["requester"],
                           target_type="product", target_id=ctx["product"],
                           request_type="COST_CHANGE", proposed_changes={"cost_amount": 19000})
    assert r["decision_outcome"] == "NONE"
