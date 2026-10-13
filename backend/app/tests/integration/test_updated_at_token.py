"""`updated_at` 이 동시성 토큰으로 **쓸 수 있는 값인지** 검증한다.

왜 별도 파일인가
---------------
`product` 에 version 컬럼이 없어 `updated_at` 을 토큰으로 쓰기로 했다. 그 선택이
성립하려면 두 가지가 참이어야 한다.

  ① 상품 행이 바뀔 때마다 토큰이 **반드시** 바뀐다
  ② 토큰 검사와 실제 쓰기 사이에 **틈이 없다**

둘 중 하나라도 깨지면 조용한 덮어쓰기가 일어난다. 그건 감사 로그를 뒤져야만
발견되는 종류의 데이터 손실이라, 여기서 못 잡으면 운영에서 못 잡는다.

발견한 결함 (2026-08-06)
-----------------------
· `set_updated_at()` 트리거가 `now()` 를 썼다. PostgreSQL 의 `now()` 는
  **트랜잭션 시작 시각**이라 한 트랜잭션 안에서 상수다 → 같은 트랜잭션에서
  두 번 UPDATE 하면 토큰이 그대로다. → `clock_timestamp()` 로 교체(M021)
· 검사가 SELECT → UPDATE 순서였다. 그 사이 남이 커밋하면 그대로 덮어썼다(TOCTOU).
  → `updated_at` 을 UPDATE 의 WHERE 에 넣고 영향 행 0 이면 409
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import ProductVersionConflictError
from app.domain.product import ProductUpdate
from app.services import product as product_svc

ORG = uuid.UUID("00000000-0000-0000-0000-0000000000ff")


@pytest.fixture
def engine(_test_db_ready: str):
    eng = create_engine(_test_db_ready, future=True)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def db(engine) -> Iterator[Session]:
    s = sessionmaker(bind=engine, future=True, expire_on_commit=False)()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def _actor(db: Session) -> uuid.UUID:
    row = db.execute(text("SELECT id FROM app_user WHERE org_id = :o LIMIT 1"),
                     {"o": str(ORG)}).first()
    if row:
        return uuid.UUID(str(row.id))
    uid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO app_user (id, org_id, email, password_hash, display_name, role, is_active)
        VALUES (:i, :o, 'token-test@picksome.local', 'x', '토큰테스트', 'OPERATOR', TRUE)
    """), {"i": str(uid), "o": str(ORG)})
    db.commit()
    return uid


def _product(db: Session) -> uuid.UUID:
    """테스트 상품.

    `business_key` 에 UUID hex 를 **자르지 않고** 전부 쓴다.
    6자리(16.7M)로 자르면 테스트 DB 에 행이 쌓일수록 생일 문제로 충돌 확률이 오른다 —
    행이 2,600개 쌓인 시점에 삽입당 약 0.016%, 전체 실행당 약 1.6% 였고
    실제로 fixture ERROR 로 나타났다. PK 와 같은 값을 쓰면 충돌이 구조적으로 불가능하다.
    """
    pid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO product (id, org_id, business_key, name_ko, margin_type, status, cost_amount, memo)
        VALUES (:i, :o, :bk, '토큰 테스트 상품', 'GENERAL_RESELL', 'ACTIVE', 10000, '초기')
    """), {"i": str(pid), "o": str(ORG), "bk": f"PROD-{pid.hex}"})
    db.commit()
    return pid


@pytest.fixture
def ctx(db: Session):
    yield {"db": db, "actor": _actor(db), "product": _product(db)}


def _token(db: Session, pid: uuid.UUID) -> str:
    return db.execute(
        text("SELECT updated_at FROM product WHERE id = :i"), {"i": str(pid)}
    ).scalar().isoformat()


# ──────────────────────────────────────────────────────────────
# ① 토큰이 반드시 바뀌는가
# ──────────────────────────────────────────────────────────────

def test_trigger_owns_updated_at(ctx):
    """트리거가 `updated_at` 을 관리한다 — 애플리케이션이 무엇을 넣든 덮어쓴다.

    이걸 고정하는 이유: raw SQL 로 상품을 고치는 경로(마이그레이션, 배치, 수동 패치)가
    `updated_at` 을 빠뜨려도 토큰은 갱신된다. 토큰의 신뢰 근거가 애플리케이션 규율이
    아니라 **DB 제약**이어야 한다.
    """
    db, pid = ctx["db"], ctx["product"]
    before = _token(db, pid)

    # updated_at 을 일부러 과거로 밀어 넣는다. 트리거가 없으면 그대로 들어간다.
    db.execute(
        text("UPDATE product SET memo = '트리거 확인', updated_at = '2000-01-01T00:00:00Z' "
             "WHERE id = :i"),
        {"i": str(pid)},
    )
    db.commit()

    after = _token(db, pid)
    assert not after.startswith("2000-"), "트리거가 updated_at 을 관리하지 않는다"
    assert after > before


def test_two_updates_in_one_transaction_change_the_token(ctx):
    """**핵심 회귀 테스트.** 같은 트랜잭션에서 두 번 고치면 토큰도 두 번 바뀌어야 한다.

    트리거가 `now()`(= 트랜잭션 시작 시각)를 쓰면 여기서 두 값이 같아진다.
    그러면 첫 수정 뒤에 발급한 토큰으로 두 번째 수정을 통과시킬 수 있다 —
    낙관적 잠금이 그냥 뚫린다. `clock_timestamp()` 여야 한다.
    """
    db, pid = ctx["db"], ctx["product"]

    db.execute(text("UPDATE product SET memo = '첫 번째' WHERE id = :i"), {"i": str(pid)})
    first = db.execute(text("SELECT updated_at FROM product WHERE id = :i"),
                       {"i": str(pid)}).scalar()

    db.execute(text("UPDATE product SET memo = '두 번째' WHERE id = :i"), {"i": str(pid)})
    second = db.execute(text("SELECT updated_at FROM product WHERE id = :i"),
                        {"i": str(pid)}).scalar()

    db.commit()
    assert second > first, (
        "같은 트랜잭션의 두 UPDATE 가 같은 토큰을 냈다 — "
        "트리거가 now() 를 쓰고 있다(트랜잭션 시작 시각으로 고정). clock_timestamp() 필요."
    )


def test_every_service_update_path_changes_the_token(ctx):
    """서비스 경로로 연속 저장해도 매번 토큰이 바뀐다."""
    db, pid, actor = ctx["db"], ctx["product"], ctx["actor"]
    seen = []
    for i in range(3):
        out = product_svc.update_product(
            db, org_id=ORG, actor=actor, product_id=pid,
            body=ProductUpdate(memo=f"연속 저장 {i}"),
        )
        seen.append(out.version_token)
    assert len(set(seen)) == 3, f"토큰이 중복됐다: {seen}"
    assert seen == sorted(seen), "토큰이 단조 증가하지 않는다"


# ──────────────────────────────────────────────────────────────
# ② 검사와 쓰기 사이에 틈이 없는가 (TOCTOU)
# ──────────────────────────────────────────────────────────────

def test_concurrent_commit_between_check_and_write_is_rejected(ctx, engine, monkeypatch):
    """**핵심 회귀 테스트.** 토큰 확인 직후 남이 커밋해도 덮어쓰지 않는다.

    `update_product` 는 ① 상품을 읽어 토큰을 비교하고 ② UPDATE 를 실행한다.
    ①과 ② 사이에 다른 커넥션이 커밋하면, READ COMMITTED 에서 ①은 옛 값을 봤으므로
    검사를 통과하고 ②가 남의 수정을 그대로 덮어쓴다.

    그 틈을 이 테스트가 강제로 벌린다 — `_now()` 는 검사와 UPDATE 사이에 정확히
    한 번 불리므로, 거기서 다른 커넥션의 커밋을 끼워 넣는다.

    방어는 WHERE 절의 `updated_at = :expected` 여야 한다. SELECT 로 미리 확인하는 것만으로는
    구조적으로 막을 수 없다 — 확인한 값이 쓰는 순간까지 유효하다는 보장이 없다.
    """
    db, pid, actor = ctx["db"], ctx["product"], ctx["actor"]
    stale = _token(db, pid)

    real_now = product_svc._now
    fired = {"done": False}

    def now_with_interleaved_commit():
        if not fired["done"]:
            fired["done"] = True
            # 다른 커넥션이 먼저 저장하고 커밋한다.
            with engine.connect() as other:
                other.execute(
                    text("UPDATE product SET memo = '다른 사람이 먼저' WHERE id = :i"),
                    {"i": str(pid)},
                )
                other.commit()
        return real_now()

    monkeypatch.setattr(product_svc, "_now", now_with_interleaved_commit)

    with pytest.raises(ProductVersionConflictError):
        product_svc.update_product(
            db, org_id=ORG, actor=actor, product_id=pid,
            body=ProductUpdate(memo="내가 덮어씀", expected_version=stale),
        )

    monkeypatch.undo()
    db.rollback()
    assert fired["done"], "테스트 전제: 끼어들기가 실행돼야 한다"

    memo = db.execute(text("SELECT memo FROM product WHERE id = :i"), {"i": str(pid)}).scalar()
    assert memo == "다른 사람이 먼저", "먼저 커밋한 쪽의 값이 덮어써졌다"


def test_two_connections_racing_only_one_wins(ctx, engine):
    """같은 토큰을 든 두 저장 중 하나만 성공한다."""
    db, pid, actor = ctx["db"], ctx["product"], ctx["actor"]
    token = _token(db, pid)

    Maker = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    a, b = Maker(), Maker()
    try:
        product_svc.update_product(
            a, org_id=ORG, actor=actor, product_id=pid,
            body=ProductUpdate(memo="A 저장", expected_version=token),
        )
        with pytest.raises(ProductVersionConflictError):
            product_svc.update_product(
                b, org_id=ORG, actor=actor, product_id=pid,
                body=ProductUpdate(memo="B 저장", expected_version=token),
            )
    finally:
        for s in (a, b):
            s.rollback()
            s.close()

    memo = db.execute(text("SELECT memo FROM product WHERE id = :i"), {"i": str(pid)}).scalar()
    assert memo == "A 저장"


def test_conflict_response_carries_current_token(ctx):
    """409 에 현재 토큰이 실려야 프론트가 최신값 위에서 다시 판단할 수 있다."""
    db, pid, actor = ctx["db"], ctx["product"], ctx["actor"]
    stale = _token(db, pid)
    product_svc.update_product(
        db, org_id=ORG, actor=actor, product_id=pid, body=ProductUpdate(memo="선점"))

    with pytest.raises(ProductVersionConflictError) as exc:
        product_svc.update_product(
            db, org_id=ORG, actor=actor, product_id=pid,
            body=ProductUpdate(memo="충돌", expected_version=stale))

    current = exc.value.detail["current_version"]
    assert current != stale
    assert current == _token(db, pid)


def test_deleted_product_is_404_not_409(ctx):
    """삭제된 상품은 '충돌' 이 아니라 '없음' 이다.

    WHERE 에 토큰을 넣으면 삭제된 행도 영향 행 0 이 된다. 그걸 전부 409 로 내면
    사용자는 "다시 불러오세요" 를 보고 새로고침했다가 상품이 없는 걸 알게 된다.
    """
    from app.core.exceptions import NotFoundError

    db, pid, actor = ctx["db"], ctx["product"], ctx["actor"]
    token = _token(db, pid)
    db.execute(text("UPDATE product SET deleted_at = now() WHERE id = :i"), {"i": str(pid)})
    db.commit()

    with pytest.raises(NotFoundError):
        product_svc.update_product(
            db, org_id=ORG, actor=actor, product_id=pid,
            body=ProductUpdate(memo="삭제된 상품", expected_version=token))


# ──────────────────────────────────────────────────────────────
# ③ 단조 증가 보장 (M023) — 측정이 아니라 보장
# ──────────────────────────────────────────────────────────────

def test_token_increases_even_when_clock_goes_backwards(ctx):
    """**핵심 회귀 테스트.** 시스템 시계가 뒤로 가도 토큰은 반드시 커진다.

    `clock_timestamp()` 만 쓰면 NTP 보정으로 시계가 뒤로 갔을 때 새 토큰이
    옛 토큰보다 작아진다. 그러면 **이전에 발급한 토큰이 다시 유효해져서**
    낙관적 잠금이 뚫린다.

    시계를 실제로 되돌릴 수는 없으니, `updated_at` 을 미래로 밀어 같은 상황을 만든다
    (트리거를 잠시 끄고 직접 넣는다 — 이 테스트에서만 하는 조작이다).
    """
    db, pid = ctx["db"], ctx["product"]

    db.execute(text("ALTER TABLE product DISABLE TRIGGER trg_product_updated_at"))
    db.execute(text("UPDATE product SET updated_at = now() + interval '1 hour' WHERE id = :i"),
               {"i": str(pid)})
    db.execute(text("ALTER TABLE product ENABLE TRIGGER trg_product_updated_at"))
    db.commit()

    before = db.execute(text("SELECT updated_at FROM product WHERE id = :i"),
                        {"i": str(pid)}).scalar()
    db.execute(text("UPDATE product SET memo = '시계 역행 후 저장' WHERE id = :i"),
               {"i": str(pid)})
    db.commit()
    after = db.execute(text("SELECT updated_at FROM product WHERE id = :i"),
                       {"i": str(pid)}).scalar()

    assert after > before, (
        "시계가 뒤로 갔을 때 토큰이 작아졌다 — GREATEST(clock_timestamp(), OLD+1µs) 가 "
        "적용되지 않았다(M023)."
    )


def test_rapid_consecutive_updates_never_collide(ctx):
    """왕복 없는 연속 UPDATE 에서도 토큰이 매번 달라진다.

    타이머 해상도가 거친 환경에서는 `clock_timestamp()` 가 같은 값을 반환할 수 있다.
    서버 안에서 왕복 없이 돌려 최악의 경우를 만든다.
    """
    db, pid = ctx["db"], ctx["product"]

    db.execute(text("CREATE TEMP TABLE IF NOT EXISTS _tok_log(seq int, t timestamptz)"))
    db.execute(text("TRUNCATE _tok_log"))
    db.execute(text(f"""
        DO $$
        DECLARE i int; v timestamptz;
        BEGIN
          FOR i IN 1..500 LOOP
            UPDATE product SET memo = i::text WHERE id = '{pid}' RETURNING updated_at INTO v;
            INSERT INTO _tok_log VALUES (i, v);
          END LOOP;
        END $$;
    """))

    total, distinct = db.execute(
        text("SELECT count(*), count(DISTINCT t) FROM _tok_log")).first()
    violations = db.execute(text("""
        SELECT count(*) FROM (
          SELECT t, lag(t) OVER (ORDER BY seq) AS prev FROM _tok_log) s
         WHERE prev IS NOT NULL AND t <= prev
    """)).scalar()
    db.rollback()

    assert distinct == total, f"{total}회 중 {total - distinct}개 토큰이 중복됐다"
    assert violations == 0, f"단조 증가 위반 {violations}건"


def test_token_survives_string_round_trip_with_microseconds(ctx):
    """마이크로초가 포함된 토큰이 문자열 왕복 후에도 그대로 맞아야 한다.

    프론트는 토큰을 **문자열 그대로** 보관해서 되돌려준다. 중간에 `Date` 로 바꾸면
    JavaScript 는 밀리초까지만 다뤄 마이크로초가 잘리고, 저장이 항상 409 가 난다.
    여기서는 서버 쪽 왕복(파싱 → 비교)이 정확한지 고정한다.
    """
    db, pid, actor = ctx["db"], ctx["product"], ctx["actor"]

    out = product_svc.get_product(db, org_id=ORG, product_id=pid)
    token = out.version_token
    assert token is not None
    # 마이크로초 자리가 실제로 존재해야 이 테스트가 의미를 갖는다.
    assert "." in token, f"토큰에 소수점 이하가 없다: {token}"

    saved = product_svc.update_product(
        db, org_id=ORG, actor=actor, product_id=pid,
        body=ProductUpdate(memo="마이크로초 왕복", expected_version=token),
    )
    assert saved.memo == "마이크로초 왕복"


def test_millisecond_truncated_token_is_rejected(ctx):
    """밀리초로 잘린 토큰은 **거절돼야 한다.**

    JavaScript `new Date(t).toISOString()` 을 거치면 마이크로초가 잘린다.
    그 값이 통과해 버리면 낙관적 잠금이 사실상 밀리초 해상도로 내려앉고,
    같은 밀리초 안의 다른 수정을 구분하지 못한다.
    """
    db, pid, actor = ctx["db"], ctx["product"], ctx["actor"]

    out = product_svc.get_product(db, org_id=ORG, product_id=pid)
    token = out.version_token or ""
    # 마이크로초 6자리 → 밀리초 3자리로 자른다 (JS 직렬화와 같은 손실).
    head, _, tail = token.partition(".")
    frac, _, tz = tail.partition("+")
    truncated = f"{head}.{frac[:3]}+{tz}" if tz else f"{head}.{frac[:3]}"

    if truncated == token:
        pytest.skip("마이크로초 자리가 이미 0이라 손실을 만들 수 없다")

    with pytest.raises(ProductVersionConflictError):
        product_svc.update_product(
            db, org_id=ORG, actor=actor, product_id=pid,
            body=ProductUpdate(memo="잘린 토큰", expected_version=truncated),
        )


def test_nil_uuid_variant_is_blocked(ctx):
    """nil UUID 를 id 로 가진 variant 를 만들 수 없다 (M023).

    M022 의 `variant_scope` 는 `product_variant_id IS NULL`(상품 기본 가격)을
    nil UUID 로 접어 넣는다. 실제 variant 가 그 id 를 가지면 **그 variant 의 가격과
    상품 기본 가격이 같은 것으로 취급되어** 둘 중 하나만 존재할 수 있게 된다.
    """
    db, pid = ctx["db"], ctx["product"]
    with pytest.raises(Exception) as exc:
        db.execute(text("""
            INSERT INTO product_variant (id, org_id, product_id, variant_key, option_values_json)
            VALUES ('00000000-0000-0000-0000-000000000000', :o, :p, 'nil', '{}'::jsonb)
        """), {"o": str(ORG), "p": str(pid)})
        db.commit()
    assert "ck_pv_id_not_nil" in str(exc.value)
    db.rollback()


def test_normal_variant_id_is_allowed(ctx):
    """정상 UUID variant 는 당연히 만들어진다 — 위 CHECK 가 과하게 막지 않는다."""
    db, pid = ctx["db"], ctx["product"]
    vid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO product_variant (id, org_id, product_id, variant_key, option_values_json)
        VALUES (:i, :o, :p, '색상=네이비', '{"색상":"네이비"}'::jsonb)
    """), {"i": str(vid), "o": str(ORG), "p": str(pid)})
    db.commit()
    assert db.execute(text("SELECT 1 FROM product_variant WHERE id = :i"),
                      {"i": str(vid)}).scalar() == 1
