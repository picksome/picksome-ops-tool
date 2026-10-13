"""상품 낙관적 동시성 토큰 + executor 중복 실행 방지.

여기서 막는 사고
----------------
1. 두 사람이 같은 상품을 열어 두고 각자 저장 → 나중 사람이 앞사람 수정을 조용히 덮어씀.
   금액 데이터에서는 감사 로그를 뒤져야만 알 수 있는 손실이다.
2. 자동 적용 배치가 동시에 여러 개 돌기 → 로그·통계가 뒤섞이고 버튼 연타가 그대로
   배치 수로 이어짐. 프론트 disabled 로는 막히지 않는다.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import (
    ApprovalExecutorAlreadyRunningError,
    ProductVersionConflictError,
)
from app.domain.product import ProductUpdate
from app.services import approval as svc
from app.services import approval_executor as executor
from app.services import product as product_svc

ORG = uuid.UUID("00000000-0000-0000-0000-0000000000ff")


@pytest.fixture
def db(_test_db_ready: str) -> Iterator[Session]:
    eng = create_engine(_test_db_ready, future=True)
    s = sessionmaker(bind=eng, future=True, expire_on_commit=False)()
    try:
        yield s
    finally:
        s.rollback()
        s.close()
        eng.dispose()


def _user(db: Session, email: str, role: str) -> uuid.UUID:
    row = db.execute(text("SELECT id FROM app_user WHERE org_id = :o AND email = :e"),
                     {"o": str(ORG), "e": email}).first()
    if row:
        return uuid.UUID(str(row.id))
    uid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO app_user (id, org_id, email, password_hash, display_name, role, is_active)
        VALUES (:i, :o, :e, 'x', :n, :r, TRUE)
    """), {"i": str(uid), "o": str(ORG), "e": email, "n": email.split("@")[0], "r": role})
    db.commit()
    return uid


def _product(db: Session) -> uuid.UUID:
    pid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO product (id, org_id, business_key, name_ko, margin_type, status, cost_amount, memo)
        VALUES (:i, :o, :bk, '동시성 테스트 상품', 'GENERAL_RESELL', 'ACTIVE', 10000, '초기')
    """), {"i": str(pid), "o": str(ORG), "bk": f"PROD-{pid.hex}"})
    db.commit()
    return pid


@pytest.fixture
def ctx(db: Session):
    db.execute(text("DELETE FROM approval_request WHERE org_id = :o"), {"o": str(ORG)})
    db.commit()
    yield {
        "db": db,
        "actor": _user(db, "conc-actor@picksome.local", "OPERATOR"),
        "product": _product(db),
    }
    db.execute(text("DELETE FROM approval_request WHERE org_id = :o"), {"o": str(ORG)})
    db.commit()


# ──────────────────────────────────────────────────────────────
# 1. version token
# ──────────────────────────────────────────────────────────────

def test_detail_exposes_version_token(ctx):
    out = product_svc.get_product(ctx["db"], org_id=ORG, product_id=ctx["product"])
    assert out.updated_at is not None
    # 프론트가 저장 시 그대로 되돌려줄 값. 문자열이어야 JSON 왕복에서 정밀도가 안 깨진다.
    assert out.version_token == out.updated_at.isoformat()


def test_save_without_expected_version_still_works(ctx):
    """토큰을 안 보내면 검사하지 않는다 — 기존 호출자·스크립트 호환."""
    out = product_svc.update_product(
        ctx["db"], org_id=ORG, actor=ctx["actor"], product_id=ctx["product"],
        body=ProductUpdate(memo="토큰 없이 저장"),
    )
    assert out.memo == "토큰 없이 저장"


def test_save_with_matching_version_succeeds(ctx):
    before = product_svc.get_product(ctx["db"], org_id=ORG, product_id=ctx["product"])
    out = product_svc.update_product(
        ctx["db"], org_id=ORG, actor=ctx["actor"], product_id=ctx["product"],
        body=ProductUpdate(memo="정상 저장", expected_version=before.version_token),
    )
    assert out.memo == "정상 저장"
    # 저장하면 토큰이 바뀐다 — 안 바뀌면 두 번째 저장이 무조건 통과해 버린다.
    assert out.version_token != before.version_token


def test_stale_version_is_rejected(ctx):
    """A 가 읽고 → B 가 저장 → A 가 저장. A 는 거절돼야 한다."""
    a_snapshot = product_svc.get_product(ctx["db"], org_id=ORG, product_id=ctx["product"])

    product_svc.update_product(
        ctx["db"], org_id=ORG, actor=ctx["actor"], product_id=ctx["product"],
        body=ProductUpdate(memo="B 가 먼저 저장"),
    )

    with pytest.raises(ProductVersionConflictError) as exc:
        product_svc.update_product(
            ctx["db"], org_id=ORG, actor=ctx["actor"], product_id=ctx["product"],
            body=ProductUpdate(memo="A 가 나중에 저장",
                               expected_version=a_snapshot.version_token),
        )
    assert exc.value.code == "PRODUCT_VERSION_CONFLICT"
    # 프론트가 최신값을 다시 보여주려면 현재 토큰이 필요하다.
    assert exc.value.detail["current_version"] != a_snapshot.version_token

    # B 의 저장이 살아 있어야 한다 — 충돌 시 A 가 덮어쓰지 않았다는 증거.
    now = product_svc.get_product(ctx["db"], org_id=ORG, product_id=ctx["product"])
    assert now.memo == "B 가 먼저 저장"


def test_version_conflict_is_checked_before_write(ctx):
    """충돌이면 **아무것도 쓰지 않는다.** 일부만 반영되면 뭐가 저장됐는지 알 수 없다."""
    stale = product_svc.get_product(ctx["db"], org_id=ORG, product_id=ctx["product"])
    product_svc.update_product(
        ctx["db"], org_id=ORG, actor=ctx["actor"], product_id=ctx["product"],
        body=ProductUpdate(memo="선점"),
    )
    with pytest.raises(ProductVersionConflictError):
        product_svc.update_product(
            ctx["db"], org_id=ORG, actor=ctx["actor"], product_id=ctx["product"],
            body=ProductUpdate(memo="덮어쓰기", sku="SHOULD-NOT-EXIST",
                               expected_version=stale.version_token),
        )
    after = product_svc.get_product(ctx["db"], org_id=ORG, product_id=ctx["product"])
    assert after.sku != "SHOULD-NOT-EXIST"
    assert after.memo == "선점"


def test_expected_version_is_not_written_as_a_column(ctx):
    """`expected_version` 은 제어값이지 상품 필드가 아니다.

    UPDATE 문에 섞여 들어가면 `column "expected_version" does not exist` 로 터진다.
    """
    before = product_svc.get_product(ctx["db"], org_id=ORG, product_id=ctx["product"])
    out = product_svc.update_product(
        ctx["db"], org_id=ORG, actor=ctx["actor"], product_id=ctx["product"],
        body=ProductUpdate(memo="ok", expected_version=before.version_token),
    )
    assert not hasattr(out, "expected_version")


def test_expected_version_alone_is_a_no_op(ctx):
    """토큰만 보내고 바꿀 필드가 없으면 아무 일도 일어나지 않는다."""
    before = product_svc.get_product(ctx["db"], org_id=ORG, product_id=ctx["product"])
    out = product_svc.update_product(
        ctx["db"], org_id=ORG, actor=ctx["actor"], product_id=ctx["product"],
        body=ProductUpdate(expected_version="완전히-엉뚱한-토큰"),
    )
    # 쓸 게 없으므로 검사도 하지 않고 현재 상태를 그대로 돌려준다.
    assert out.version_token == before.version_token


# ──────────────────────────────────────────────────────────────
# 2. 대상별 열린 승인 요청 요약
# ──────────────────────────────────────────────────────────────

def test_open_approvals_for_target(ctx):
    db, pid = ctx["db"], ctx["product"]
    assert svc.list_open_approvals_for_target(
        db, org_id=ORG, target_type="product", target_id=pid) == []

    req = svc.create_request(
        db, org_id=ORG, actor_user_id=ctx["actor"],
        target_type="product", target_id=pid, request_type="COST_CHANGE",
        proposed_changes={"cost_amount": 20000}, reason="원가 인상",
    )
    open_rows = svc.list_open_approvals_for_target(
        db, org_id=ORG, target_type="product", target_id=pid)
    assert [r["id"] for r in open_rows] == [uuid.UUID(str(req["id"]))] or \
           [str(r["id"]) for r in open_rows] == [str(req["id"])]
    assert open_rows[0]["request_type"] == "COST_CHANGE"
    # 요약이지 diff 가 아니다 — 민감값은 실리지 않는다.
    assert "proposed_changes_json" not in open_rows[0]


def test_open_approvals_excludes_closed_requests(ctx):
    db, pid = ctx["db"], ctx["product"]
    req = svc.create_request(
        db, org_id=ORG, actor_user_id=ctx["actor"],
        target_type="product", target_id=pid, request_type="COST_CHANGE",
        proposed_changes={"cost_amount": 20000}, reason="원가 인상",
    )
    svc.cancel(db, org_id=ORG, request_id=uuid.UUID(str(req["id"])),
               actor_user_id=ctx["actor"], actor_role="OPERATOR", reason="취소")
    assert svc.list_open_approvals_for_target(
        db, org_id=ORG, target_type="product", target_id=pid) == []


def test_open_approvals_is_org_scoped(ctx):
    """다른 조직의 요청이 새어 나오면 안 된다."""
    other_org = uuid.UUID("00000000-0000-0000-0000-0000000000fe")
    rows = svc.list_open_approvals_for_target(
        ctx["db"], org_id=other_org, target_type="product", target_id=ctx["product"])
    assert rows == []


# ──────────────────────────────────────────────────────────────
# 3. executor 중복 실행 방지
# ──────────────────────────────────────────────────────────────

def test_executor_runs_when_lock_is_free(ctx):
    result = executor.run_due_approvals(ctx["db"], org_id=ORG, batch_size=5)
    assert result.batch_id


def test_executor_rejects_concurrent_run(ctx, _test_db_ready):
    """다른 연결이 잠금을 쥐고 있으면 409. 조용히 통과시키면 배치가 겹쳐 돈다."""
    eng = create_engine(_test_db_ready, future=True)
    holder = eng.connect()
    try:
        got = holder.execute(
            text("SELECT pg_try_advisory_lock(:ns, :k)"),
            {"ns": executor._LOCK_NAMESPACE, "k": executor._lock_key_for(ORG)},
        ).scalar()
        assert got is True, "테스트 전제: 잠금이 비어 있어야 한다"

        with pytest.raises(ApprovalExecutorAlreadyRunningError) as exc:
            executor.run_due_approvals(ctx["db"], org_id=ORG, batch_size=5)
        assert exc.value.code == "APPROVAL_EXECUTOR_ALREADY_RUNNING"
        assert exc.value.http_status == 409
    finally:
        holder.close()
        eng.dispose()


def test_executor_lock_is_released_after_run(ctx):
    """실행이 끝나면 잠금이 풀려야 한다 — 안 풀리면 두 번째 실행이 영영 막힌다."""
    executor.run_due_approvals(ctx["db"], org_id=ORG, batch_size=5)
    executor.run_due_approvals(ctx["db"], org_id=ORG, batch_size=5)


def test_executor_lock_is_released_after_failure(ctx, monkeypatch):
    """실패해도 잠금은 풀린다. finally 가 없으면 한 번 터진 뒤 영구히 잠긴다."""
    def boom(*a, **k):
        raise RuntimeError("의도된 실패")

    monkeypatch.setattr(executor, "_claim_due_requests", boom)
    with pytest.raises(RuntimeError):
        executor.run_due_approvals(ctx["db"], org_id=ORG, batch_size=5)

    monkeypatch.undo()
    # 잠금이 남아 있었다면 여기서 409 가 난다.
    executor.run_due_approvals(ctx["db"], org_id=ORG, batch_size=5)


def test_executor_lock_is_per_org(ctx):
    """다른 조직 배치는 서로를 막지 않는다."""
    other = uuid.UUID("00000000-0000-0000-0000-0000000000fe")
    assert executor._lock_key_for(ORG) != executor._lock_key_for(other)
    # 전역 실행 키는 어떤 조직 키와도 겹치지 않는다.
    assert executor._lock_key_for(None) == 0
    assert executor._lock_key_for(ORG) != 0


def test_duplicate_variant_key_is_409_not_500(ctx):
    """같은 옵션 조합을 두 번 만들면 **409** 다.

    번역하지 않으면 UNIQUE 위반이 500 으로 나간다. 사용자는 "서버 오류" 를 보고
    다시 시도하는데 몇 번을 해도 같은 결과다 — 무엇이 문제인지 알 수 없다.
    (실제로 E2E 가 이 500 을 잡았다.)
    """
    from app.core.exceptions import ConflictError
    from app.domain.product import VariantCreate
    from app.services.product import create_variant

    db, pid, actor = ctx["db"], ctx["product"], ctx["actor"]
    body = VariantCreate(variant_key="색상=네이비", option_values_json={"색상": "네이비"})

    create_variant(db, org_id=ORG, actor=actor, product_id=pid, body=body)
    with pytest.raises(ConflictError) as exc:
        create_variant(db, org_id=ORG, actor=actor, product_id=pid, body=body)
    assert "같은 옵션 조합" in str(exc.value)
    assert exc.value.http_status == 409
    db.rollback()
