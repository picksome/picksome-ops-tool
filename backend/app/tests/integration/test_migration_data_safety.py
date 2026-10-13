"""M016·M017 downgrade/upgrade 데이터 보존 실측 + backfill 멱등성.

왜 이 파일이 따로 있나
----------------------
44번 작업로그에 "downgrade M015 → upgrade head 무손실" 이라고 적었는데,
그건 **마이그레이션이 에러 없이 끝났다**는 뜻이었지 데이터가 보존됐다는 뜻이 아니었다.
M016/M017 의 downgrade 는 컬럼을 DROP 한다 — 그 컬럼에만 있던 값은 사라진다.

말로 "무손실" 이라고 쓰지 말고 **무엇이 남고 무엇이 사라지는지 테스트로 고정**한다.
나중에 누가 downgrade 를 실행할 때 무엇을 잃는지 알고 하도록.

이 테스트는 전용 scratch DB 를 만들어 쓴다. 공용 테스트 DB 에서 alembic downgrade 를
돌리면 같은 세션의 다른 테스트가 스키마를 잃는다.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

_BACKEND_ROOT = Path(__file__).resolve().parents[3]

SYSTEM_ORG_ID = "00000000-0000-0000-0000-0000000000ff"

# 시드용 월 카운터. 호출마다 다른 달을 쓴다(monthly_settlement 에 org+월 유일 제약이 있다).
_ym_counter = itertools.count(1)


def _next_year_month() -> str:
    n = next(_ym_counter)
    return f"{2020 + n // 12:04d}-{(n % 12) + 1:02d}"


def _admin_dsn() -> str:
    override = os.environ.get("TEST_ADMIN_DATABASE_URL")
    if override:
        return override.replace("postgresql+psycopg://", "postgresql://", 1)
    return "postgresql://picksome_test:picksome_test@localhost:5432/postgres"


def _alembic(db_url: str, *args: str) -> None:
    """해당 DB 에 alembic 명령 실행. 실패하면 stdout/stderr 를 그대로 노출한다."""
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": db_url},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"alembic {' '.join(args)} 실패\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[str]:
    """이 모듈 전용 DB. 다른 테스트의 스키마를 건드리지 않기 위해 분리한다.

    같은 DB 를 여러 테스트가 공유하므로 **빈 상태를 전제하는 검사에는 쓰지 않는다**
    — 그런 검사는 `fresh_db` 를 쓴다.
    """
    yield from _make_scratch_database()


def _make_scratch_database() -> Iterator[str]:
    """빈 DB 하나를 만들고, 끝나면 지운다."""
    name = f"picksome_migtest_{uuid.uuid4().hex[:8]}"
    admin = _admin_dsn()
    try:
        conn = psycopg.connect(admin, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"테스트 DB 서버 접속 불가: {exc.__class__.__name__}")

    with conn:
        can_create = conn.execute(
            "SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        if not (can_create and can_create[0]):
            pytest.skip("CREATEDB 권한 없음 — backend/scripts/bootstrap_roles.sql 실행 필요")
        conn.execute(f'CREATE DATABASE "{name}"')

    base = admin.rpartition("/")[0]
    try:
        yield f"{base.replace('postgresql://', 'postgresql+psycopg://', 1)}/{name}"
    finally:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (name,),
            )
            c.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture()
def fresh_db() -> Iterator[str]:
    """**테스트마다 새 DB.**

    `scratch_db` 는 module 스코프라 앞 테스트가 남긴 행이 그대로 있다. 전체 chain
    왕복은 그 상태에 좌우된다 — 실제로 이 파일을 쓰다가 단독 실행은 통과하고
    모듈 전체 실행은 실패하는 것을 겪었다. 빈 DB 를 전제로 하는 검사는
    진짜 빈 DB 에서 해야 한다.
    """
    yield from _make_scratch_database()


def _pg(url: str) -> psycopg.Connection:
    return psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True)


def _seed_sale_and_settlement(conn: psycopg.Connection) -> tuple[str, str]:
    """M017 스키마에서 판매 1건 + 월정산 1건을 만들고, M016/M017 전용 값을 채운다."""
    # scratch_db 는 module 스코프라 여러 테스트가 같은 DB 를 쓴다.
    # business_key 가 고정이면 두 번째 호출에서 UNIQUE 위반이 난다.
    #
    # **hex 를 자르지 않는다.** 6자리(16.7M)로 자르면 누적 행이 늘수록 생일 문제로
    # 충돌 확률이 올라간다 — 실제로 그것 때문에 전체 실행 중 간헐적 fixture ERROR 가 났다.
    tag = uuid.uuid4().hex
    sale_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO sales_order
          (id, org_id, business_key, sale_date, marketplace, marketplace_order_no,
           product_snapshot_name, quantity, sale_price, line_no,
           profit_status, return_window_ends_at)
        VALUES (%s, %s, %s, '2026-03-15', 'COUPANG', %s,
                '마이그레이션 검증 상품', 1, 30000, 1, 'SETTLED', '2026-03-30')
        """,
        (sale_id, SYSTEM_ORG_ID, f"SO-MIGTEST-{tag}", f"MIG-{tag}"),
    )
    ms_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO monthly_settlement
          (id, org_id, business_key, year_month, status, close_state,
           total_revenue, total_direct_cost, total_common_expense, net_profit,
           validation_result_json, acknowledged_warnings_json)
        VALUES (%s, %s, %s, %s, 'DRAFT', 'REVIEW',
                1000000, 400000, 100000, 500000,
                '[{"code":"MISSING_EVIDENCE","blocking":false}]'::jsonb,
                '["MISSING_EVIDENCE"]'::jsonb)
        """,
        # ⚠️ year_month 를 hash() 로 만들면 안 된다. 선택지가 9개뿐이라 같은 모듈에서
        #    두 번 시드할 때 충돌하고(1/9 확률), PYTHONHASHSEED 가 매 실행 랜덤이라
        #    간헐적으로만 터진다 — 실제로 full suite 3회 중 1회 실패했다.
        #    모듈 단위 카운터로 확정적으로 유일하게 만든다.
        (ms_id, SYSTEM_ORG_ID, f"MS-MIGTEST-{tag}", _next_year_month()),
    )
    return sale_id, ms_id


def test_downgrade_upgrade_preserves_m015_data_but_drops_m016_only_columns(scratch_db: str):
    """downgrade 는 스키마를 되돌리지만 **M016 이후에만 있던 값은 되살리지 않는다.**

    보존되는 것 : M015 시점에도 존재하던 컬럼 (판매 행 자체, 금액, 정산 status)
    사라지는 것 : M016/M017 이 추가한 컬럼에만 있던 값
                  (profit_status 의 수동 지정값, return_window_ends_at,
                   close_state 의 REVIEW, validation_result_json, acknowledged_warnings_json)
    """
    _alembic(scratch_db, "upgrade", "head")

    with _pg(scratch_db) as conn:
        sale_id, ms_id = _seed_sale_and_settlement(conn)

        before_sale = conn.execute(
            "SELECT business_key, sale_price, quantity, profit_status, return_window_ends_at"
            "  FROM sales_order WHERE id = %s", (sale_id,)
        ).fetchone()
        before_ms = conn.execute(
            "SELECT business_key, net_profit, status, close_state,"
            "       validation_result_json, acknowledged_warnings_json"
            "  FROM monthly_settlement WHERE id = %s", (ms_id,)
        ).fetchone()

    assert before_sale[3] == "SETTLED"
    assert before_ms[3] == "REVIEW"

    # ── 스키마 왕복 ──
    _alembic(scratch_db, "downgrade", "M015")
    _alembic(scratch_db, "upgrade", "head")

    with _pg(scratch_db) as conn:
        after_sale = conn.execute(
            "SELECT business_key, sale_price, quantity, profit_status, return_window_ends_at"
            "  FROM sales_order WHERE id = %s", (sale_id,)
        ).fetchone()
        after_ms = conn.execute(
            "SELECT business_key, net_profit, status, close_state,"
            "       validation_result_json, acknowledged_warnings_json"
            "  FROM monthly_settlement WHERE id = %s", (ms_id,)
        ).fetchone()

    # ① M015 부터 있던 데이터는 그대로 남는다
    assert after_sale is not None, "판매 행 자체가 사라지면 안 된다"
    assert after_sale[0] == before_sale[0]
    assert after_sale[1] == before_sale[1]
    assert after_sale[2] == before_sale[2]
    assert after_ms[0] == before_ms[0]
    assert after_ms[1] == before_ms[1]
    assert after_ms[2] == before_ms[2] == "DRAFT", (
        "기존 status 는 downgrade 대상이 아니므로 보존된다. "
        "REVIEW 는 legacy status 로는 DRAFT 에 대응한다(monthly_close._transition 매핑)."
    )

    # ② M016/M017 전용 값은 복원되지 않는다 — backfill 기본값으로 재계산될 뿐이다
    assert after_sale[3] == "PROVISIONAL", (
        "수동으로 SETTLED 였던 값이 downgrade 로 사라지고 backfill 기본값으로 돌아온다"
    )
    assert after_sale[4] is None, "return_window_ends_at 은 복원 근거가 없어 NULL 이 된다"
    assert after_ms[3] == "OPEN", (
        "REVIEW 는 M017 에만 있던 상태다. downgrade 로 컬럼이 사라지고, "
        "재upgrade 시 기존 status(DRAFT)에서 backfill 되어 OPEN 으로 돌아온다 — REVIEW 는 복원 불가."
    )
    assert after_ms[4] is None, "validation_result_json 은 소실된다"
    assert after_ms[5] is None, "acknowledged_warnings_json 은 소실된다"


def test_backfill_is_idempotent_and_preserves_manual_values(scratch_db: str):
    """backfill 재실행이 이미 채워진 값을 덮어쓰지 않는다.

    M016/M017 의 backfill 은 전부 `WHERE <컬럼> IS NULL` 조건이 붙어 있다.
    즉 **한 번 값이 들어간 행은 다시 건드리지 않는다.** 그 성질을 고정한다.
    """
    _alembic(scratch_db, "upgrade", "head")

    with _pg(scratch_db) as conn:
        sale_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO sales_order
              (id, org_id, business_key, sale_date, marketplace, marketplace_order_no,
               product_snapshot_name, quantity, sale_price, line_no, profit_status)
            VALUES (%s, %s, 'SO-IDEM-1', '2026-04-02', 'COUPANG', 'IDEM-1',
                    '멱등성 검증 상품', 2, 50000, 1, 'FINAL')
            """,
            (sale_id, SYSTEM_ORG_ID),
        )

        # M016 의 backfill UPDATE 를 그대로 재실행한다
        backfill_sql = """
            UPDATE sales_order so
               SET profit_status = CASE
                     WHEN EXISTS (
                       SELECT 1 FROM product_margin_actual pma
                        WHERE pma.sales_order_id = so.id
                          AND pma.settlement_record_id IS NOT NULL
                          AND pma.deleted_at IS NULL
                     ) THEN 'SETTLED' ELSE 'PROVISIONAL' END
             WHERE profit_status IS NULL
        """
        first = conn.execute(backfill_sql).rowcount
        second = conn.execute(backfill_sql).rowcount

        status = conn.execute(
            "SELECT profit_status FROM sales_order WHERE id = %s", (sale_id,)
        ).fetchone()[0]
        null_left = conn.execute(
            "SELECT COUNT(*) FROM sales_order WHERE profit_status IS NULL"
        ).fetchone()[0]

    assert first == 0, "NOT NULL 제약이 있어 새 행은 항상 값이 있다 — backfill 대상이 0이어야 한다"
    assert second == 0, "두 번째 실행의 변경 건수는 반드시 0"
    assert status == "FINAL", "수동으로 지정한 FINAL 이 backfill 로 덮어써지면 안 된다"
    assert null_left == 0


def test_backfill_is_organization_scoped(scratch_db: str):
    """backfill 이 조직 경계를 넘지 않는지 — 다른 org 행이 섞여도 각자 자기 값을 갖는다.

    M016 의 backfill 은 org 조건이 없다(전 행 대상). 그래도 **행 단위 조건**이라
    다른 org 의 값을 잘못 가져오지 않는다는 것을 고정한다.
    """
    _alembic(scratch_db, "upgrade", "head")

    other_org = str(uuid.uuid4())
    with _pg(scratch_db) as conn:
        conn.execute(
            "INSERT INTO organization (id, business_name, business_type, primary_currency, timezone)"
            " VALUES (%s, 'OTHER_ORG', 'SOLE_PROPRIETOR', 'KRW', 'Asia/Seoul')",
            (other_org,),
        )
        a_id, b_id = str(uuid.uuid4()), str(uuid.uuid4())
        for sid, org, key, status in (
            (a_id, SYSTEM_ORG_ID, "SO-ORG-A", "FINAL"),
            (b_id, other_org, "SO-ORG-B", "PROVISIONAL"),
        ):
            conn.execute(
                """
                INSERT INTO sales_order
                  (id, org_id, business_key, sale_date, marketplace, marketplace_order_no,
                   product_snapshot_name, quantity, sale_price, line_no, profit_status)
                VALUES (%s, %s, %s, '2026-04-03', 'COUPANG', %s, '격리 검증', 1, 10000, 1, %s)
                """,
                (sid, org, key, key, status),
            )

        rows = dict(
            conn.execute(
                "SELECT id::text, profit_status FROM sales_order WHERE id = ANY(%s)",
                ([a_id, b_id],),
            ).fetchall()
        )

    assert rows[a_id] == "FINAL"
    assert rows[b_id] == "PROVISIONAL"


def test_check_constraint_rejects_unknown_profit_status(scratch_db: str):
    """TEXT + CHECK 가 실제로 값을 막는지. enum 대신 CHECK 를 쓴 선택의 근거를 고정한다."""
    _alembic(scratch_db, "upgrade", "head")
    with _pg(scratch_db) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO sales_order
                  (org_id, business_key, sale_date, marketplace, marketplace_order_no,
                   product_snapshot_name, quantity, sale_price, line_no, profit_status)
                VALUES (%s, 'SO-BAD', '2026-04-04', 'COUPANG', 'BAD-1',
                        '잘못된 상태', 1, 10000, 1, 'NOT_A_STATUS')
                """,
                (SYSTEM_ORG_ID,),
            )


# ──────────────────────────────────────────────────────────────
# M018 (승인 워크플로)
# ──────────────────────────────────────────────────────────────

def test_m018_upgrade_downgrade_upgrade_and_constraints(scratch_db: str):
    """M017 → M018 → M017 → M018 왕복 + 제약·인덱스 존재 확인.

    M018 은 신규 테이블이라 downgrade 가 DROP TABLE 이다.
    그 테이블에만 있던 승인 이력은 사라진다 — M016/M017 과 같은 성질이다.
    """
    _alembic(scratch_db, "upgrade", "head")

    with _pg(scratch_db) as conn:
        # M016/M017 데이터가 M018 왕복으로 영향받지 않는지 확인할 기준점
        sale_id, ms_id = _seed_sale_and_settlement(conn)

        idx = {r[0] for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'approval_request'"
        ).fetchall()}
        assert "ux_approval_open_target" in idx, "같은 대상 중복 요청 차단 인덱스"
        assert "ux_approval_idempotency" in idx, "멱등성 인덱스"
        assert "ix_approval_pending_queue" in idx, "승인 대기함 인덱스"
        assert "ix_approval_org_status" in idx
        assert "ix_approval_org_target" in idx
        assert "ix_approval_requested_by" in idx
        assert "ix_approval_requested_at" in idx

        # 상태 도메인 CHECK 가 실제로 막는지.
        # requested_by 는 app_user FK 라 실제 행이 필요하다 —
        # 없으면 SELECT 가 0행이 되어 INSERT 가 조용히 통과하고 CHECK 가 평가되지 않는다.
        actor = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO app_user (id, org_id, email, password_hash, display_name, role, is_active)
            VALUES (%s, %s, %s, 'x', 'migtest', 'ADMIN', TRUE)
            """,
            (actor, SYSTEM_ORG_ID, f"migtest-{uuid.uuid4().hex}@local"),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO approval_request
                  (org_id, business_key, target_type, target_id, request_type, status, requested_by)
                VALUES (%s, %s, 'product', gen_random_uuid(), 'COST_CHANGE',
                        'NOT_A_STATUS', %s)
                """,
                (SYSTEM_ORG_ID, f"APR-BAD-{uuid.uuid4().hex}", actor),
            )

    # M018 → M017 → M018
    _alembic(scratch_db, "downgrade", "M017")
    with _pg(scratch_db) as conn:
        gone = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            " WHERE table_schema='public' AND table_name='approval_request'"
        ).fetchone()
        assert gone is None, "downgrade 후 테이블이 남아 있으면 안 된다"

        # M016/M017 데이터는 그대로여야 한다 — M018 왕복이 다른 테이블을 건드리면 안 된다
        assert conn.execute(
            "SELECT 1 FROM sales_order WHERE id = %s", (sale_id,)
        ).fetchone() is not None
        assert conn.execute(
            "SELECT close_state FROM monthly_settlement WHERE id = %s", (ms_id,)
        ).fetchone()[0] == "REVIEW", "M018 downgrade 가 M017 상태를 건드리면 안 된다"

    _alembic(scratch_db, "upgrade", "head")
    with _pg(scratch_db) as conn:
        back = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            " WHERE table_schema='public' AND table_name='approval_request'"
        ).fetchone()
        assert back is not None


# ──────────────────────────────────────────────────────────────────────────
# 전체 chain 왕복 (M002 downgrade 조사 결과)
#
# 배경: `alembic downgrade base` 가 FK 위반으로 실패하는 것을 보고 "M002 결함" 으로
# 기록했었다. **틀린 진단이었다.** 빈 DB 에서는 정상 동작한다.
#
# 실제 원인은 테스트 위생 문제다. 여러 통합 테스트가 M002 가 만든 SYSTEM 템플릿
# 조직(`00000000-…ff`)을 자기 조직처럼 쓰고 사용자 행을 커밋한다. 그 상태에서
# M002 의 downgrade 가 자기가 만든 조직을 지우려 하면 그 사용자들이 FK 로 막는다.
#
# 즉 M002 는 **자기가 만든 것만 지운다** — 소유권은 올바르다.
# 남의 데이터를 지우지 않기 때문에 오히려 막히는 것이다. CASCADE 를 붙이면
# 통과는 하지만 그 사용자들을 조용히 지우게 된다.
# ──────────────────────────────────────────────────────────────────────────

def test_full_chain_round_trips_on_a_fresh_database(fresh_db: str):
    """빈 DB 에서 head → base → head 가 동작한다.

    CI 가 이걸 매번 확인한다. downgrade 를 쓰지 않는 마이그레이션은 롤백이
    필요한 순간에야 깨진 걸 알게 된다.
    """
    _alembic(fresh_db, "upgrade", "head")
    _alembic(fresh_db, "downgrade", "base")

    with _pg(fresh_db) as conn:
        # base 로 내려가면 스키마가 비어야 한다. 남아 있으면 어느 downgrade 가
        # 자기 테이블을 안 지운 것이다.
        left = conn.execute("""
            SELECT table_name FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name <> 'alembic_version'
             ORDER BY table_name
        """).fetchall()
        assert left == [], f"downgrade base 후 테이블이 남았다: {[r[0] for r in left]}"

    _alembic(fresh_db, "upgrade", "head")

    with _pg(fresh_db) as conn:
        n = conn.execute("""
            SELECT COUNT(*) FROM information_schema.tables
             WHERE table_schema = 'public'
        """).fetchone()[0]
        assert n > 40, f"다시 올렸는데 테이블이 {n}개뿐이다"


def test_downgrade_destroys_data_it_does_not_preserve_it(fresh_db: str):
    """**왕복 성공은 무손실이 아니다.**

    이 구분을 문서가 아니라 테스트로 박아 둔다. 44번 작업로그가 "무손실" 이라고
    적었다가 틀린 적이 있다 — 실제로는 "에러 없이 끝났다" 는 뜻이었다.

    downgrade 는 테이블을 DROP 한다. 그 안의 값은 사라진다. 운영에서 downgrade 를
    실행하려는 사람은 이걸 알고 해야 한다.
    """
    _alembic(fresh_db, "upgrade", "head")

    org = str(uuid.uuid4())
    with _pg(fresh_db) as conn:
        conn.execute(
            "INSERT INTO organization (id, business_name, business_type)"
            " VALUES (%s, 'downgrade 실험', 'SOLE_PROPRIETOR')",
            (org,),
        )
        conn.execute(
            "INSERT INTO manual_cash_event"
            " (org_id, direction, status, amount, occurred_at)"
            " VALUES (%s, 'INFLOW', 'ACTUAL', 1234567, CURRENT_DATE)",
            (org,),
        )

    # M034 를 내리면 manual_cash_event 테이블 자체가 사라진다.
    _alembic(fresh_db, "downgrade", "M033")
    with _pg(fresh_db) as conn:
        exists = conn.execute("""
            SELECT to_regclass('public.manual_cash_event') IS NOT NULL
        """).fetchone()[0]
        assert exists is False, "M034 를 내렸는데 테이블이 남아 있다"

    # 다시 올려도 값은 돌아오지 않는다. 스키마만 돌아온다.
    _alembic(fresh_db, "upgrade", "head")
    with _pg(fresh_db) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM manual_cash_event WHERE org_id = %s", (org,),
        ).fetchone()[0]
        assert rows == 0, "downgrade 로 지워진 데이터가 되살아났다 — 그럴 리 없다"

        # 조직 자체는 M001 소관이라 M034 왕복에 영향받지 않는다.
        assert conn.execute(
            "SELECT COUNT(*) FROM organization WHERE id = %s", (org,),
        ).fetchone()[0] == 1


def test_seed_downgrade_refuses_to_delete_rows_it_does_not_own(fresh_db: str):
    """M002 의 downgrade 는 자기가 만든 조직만 지운다.

    그 조직에 남이 사용자를 넣어 두면 FK 가 막는다 — 그게 맞는 동작이다.
    CASCADE 를 붙이면 통과하지만 남의 사용자를 조용히 지우게 된다.

    이 테스트는 그 경계를 고정한다: **막히는 것이 정상이다.**
    """
    _alembic(fresh_db, "upgrade", "head")

    system_org = SYSTEM_ORG_ID
    with _pg(fresh_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM organization WHERE id = %s", (system_org,),
        ).fetchone()[0] == 1, "M002 가 SYSTEM 조직을 만들지 않았다"

        conn.execute(
            "INSERT INTO app_user (id, org_id, email, password_hash, display_name, role)"
            " VALUES (%s, %s, 'squatter@test.local', 'argon2:test', '무단 사용자',"
            " CAST('ADMIN' AS enum_user_role))",
            (str(uuid.uuid4()), system_org),
        )

    with pytest.raises(AssertionError) as err:
        _alembic(fresh_db, "downgrade", "base")
    assert "app_user" in str(err.value), "다른 이유로 실패했다"

    # 정리하면 다시 내려간다 — 스키마 문제가 아니라 데이터 소유권 문제라는 증거다.
    with _pg(fresh_db) as conn:
        conn.execute("DELETE FROM app_user WHERE org_id = %s", (system_org,))
    _alembic(fresh_db, "downgrade", "base")


# ──────────────────────────────────────────────────────────────────────
# M055 — 정산 매칭 상태 · 정산 조정
# ──────────────────────────────────────────────────────────────────────

def _seed_settlement_record(conn: psycopg.Connection, **kw) -> str:
    """정산 레코드 1건.

    `import_idempotency_key`(M055)와 `row_type`(M056) 둘 다 기본값 없는
    NOT NULL 이다 — 일부러 그렇게 뒀다. 식별자나 행 종류를 안 정한 적재가
    조용히 통과하면 안 되기 때문이고, 그래서 시드도 명시적으로 넣는다.
    """
    tag = uuid.uuid4().hex
    rid = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO marketplace_settlement_record
          (id, org_id, marketplace, settlement_period_start, settlement_period_end,
           marketplace_order_no, gross_revenue, net_settlement, import_file_hash,
           import_idempotency_key, matching_status, match_reason, row_type)
        VALUES (%s, %s, 'COUPANG', '2026-04-01', '2026-04-07',
                %s, 30000, 27000, %s, %s, %s, %s, %s)
        """,
        (rid, SYSTEM_ORG_ID, kw.get("order_no"), f"h-{tag}", f"MIG-{tag}",
         kw.get("matching_status", "UNMATCHED"), kw.get("match_reason"),
         kw.get("row_type", "PRODUCT_SALE")),
    )
    return rid


def test_m055_downgrade_refuses_while_adjustments_exist(fresh_db: str):
    """조정이 남아 있으면 **되돌리기가 실패한다.**

    조용히 지우면 어느 달 정산이 왜 달라졌는지가 사라지고, 그 사실은 다음
    정산 때까지 아무도 모른다. 빈 DB 로 검증하면 이 경로를 아예 안 지난다 —
    M054 에서 배운 것이다.
    """
    _alembic(fresh_db, "upgrade", "head")

    with _pg(fresh_db) as conn:
        rid = _seed_settlement_record(conn)
        conn.execute(
            """
            INSERT INTO settlement_adjustment
              (org_id, original_record_id, adjustment_type, adjustment_reason,
               adjustment_period, net_settlement_delta)
            VALUES (%s, %s, 'LATE_ENTRY', '반품 차감 누락', '2026-04', -50000)
            """,
            (SYSTEM_ORG_ID, rid),
        )

    with pytest.raises(AssertionError) as err:
        _alembic(fresh_db, "downgrade", "M054")
    # ASCII 코드로 확인한다. alembic stderr 의 한글은 콘솔 인코딩에 따라
    # 깨져 들어와 문자열 비교가 환경마다 달라진다.
    assert "M055_ADJUSTMENTS_REMAIN" in str(err.value), "다른 이유로 실패했다"

    # 정리하면 내려간다 — 스키마가 아니라 데이터 소유권 문제라는 증거다.
    with _pg(fresh_db) as conn:
        conn.execute("DELETE FROM settlement_adjustment")
        conn.execute("DELETE FROM marketplace_settlement_record")
    _alembic(fresh_db, "downgrade", "M054")
    _alembic(fresh_db, "upgrade", "head")


def test_m055_downgrade_refuses_to_erase_human_decisions(fresh_db: str):
    """사람이 강제로 연결하거나 제외한 것을 조용히 풀지 않는다.

    되돌리면 `matching_status` 가 사라져 MATCHED_MANUAL 과 MATCHED_EXACT 를
    구분할 수 없게 되고, 다음 판정이 사람의 결정을 규칙으로 덮어쓴다.
    """
    _alembic(fresh_db, "upgrade", "head")

    with _pg(fresh_db) as conn:
        _seed_settlement_record(
            conn, matching_status="EXCLUDED", match_reason="광고비 정산행")

    with pytest.raises(AssertionError) as err:
        _alembic(fresh_db, "downgrade", "M054")
    assert "M055_HUMAN_DECISIONS" in str(err.value), "다른 이유로 실패했다"

    with _pg(fresh_db) as conn:
        conn.execute("DELETE FROM marketplace_settlement_record")
    _alembic(fresh_db, "downgrade", "M054")
    _alembic(fresh_db, "upgrade", "head")


def test_m055_downgrade_keeps_rows_but_drops_matching_columns(fresh_db: str):
    """규칙이 붙인 매칭은 되돌릴 수 있다. **행은 남고 상태만 사라진다.**

    무엇이 남고 무엇이 사라지는지를 말이 아니라 테스트로 고정한다.
    """
    _alembic(fresh_db, "upgrade", "head")

    with _pg(fresh_db) as conn:
        rid = _seed_settlement_record(conn, order_no="ORD-MIG")
        conn.execute(
            "UPDATE marketplace_settlement_record"
            "   SET matching_status = 'AMBIGUOUS' WHERE id = %s", (rid,))
        assert conn.execute(
            "SELECT gross_revenue FROM marketplace_settlement_record WHERE id = %s",
            (rid,)).fetchone()[0] == 30000

    _alembic(fresh_db, "downgrade", "M054")
    with _pg(fresh_db) as conn:
        # 행과 M001 컬럼은 그대로.
        assert conn.execute(
            "SELECT gross_revenue FROM marketplace_settlement_record WHERE id = %s",
            (rid,)).fetchone()[0] == 30000
        # M055 컬럼은 사라진다.
        assert conn.execute("""
            SELECT COUNT(*) FROM information_schema.columns
             WHERE table_name = 'marketplace_settlement_record'
               AND column_name IN ('matching_status', 'import_idempotency_key')
        """).fetchone()[0] == 0
        # 기간 기반 UNIQUE 가 되살아난다 — 원래 있던 결함까지 같이.
        assert conn.execute("""
            SELECT COUNT(*) FROM pg_indexes
             WHERE tablename = 'marketplace_settlement_record'
               AND indexname = 'uq_msr_unique'
        """).fetchone()[0] == 1

    # 다시 올리면 식별자를 지어낼 수 없으므로 **막힌다.** 이게 의도다.
    with pytest.raises(AssertionError) as err:
        _alembic(fresh_db, "upgrade", "head")
    assert "M055_EXISTING_RECORDS" in str(err.value), "다른 이유로 실패했다"

    with _pg(fresh_db) as conn:
        conn.execute("DELETE FROM marketplace_settlement_record")
    _alembic(fresh_db, "upgrade", "head")


def test_m056_downgrade_refuses_to_erase_row_kind(fresh_db: str):
    """배송료 행이 남아 있으면 되돌리기가 실패한다.

    `row_type` 이 사라지면 배송비 매출과 상품 매출을 구분할 수 없게 되고,
    그 상태로 실마진을 계산하면 **상품 마진에 배송비가 얹힌다.**
    """
    _alembic(fresh_db, "upgrade", "head")

    with _pg(fresh_db) as conn:
        _seed_settlement_record(conn, order_no="ORD-SHIP",
                                row_type="SHIPPING_FEE_BASE")

    with pytest.raises(AssertionError) as err:
        _alembic(fresh_db, "downgrade", "M055")
    assert "M056_NON_PRODUCT_ROWS" in str(err.value), "다른 이유로 실패했다"

    # 상품 행만 남기면 내려간다 — 스키마가 아니라 데이터 의미의 문제다.
    with _pg(fresh_db) as conn:
        conn.execute("DELETE FROM marketplace_settlement_record")
        _seed_settlement_record(conn, order_no="ORD-P", row_type="PRODUCT_SALE")
    _alembic(fresh_db, "downgrade", "M055")

    with _pg(fresh_db) as conn:
        # 행은 남고 M056 컬럼만 사라진다.
        assert conn.execute("""
            SELECT COUNT(*) FROM marketplace_settlement_record
        """).fetchone()[0] == 1
        assert conn.execute("""
            SELECT COUNT(*) FROM information_schema.columns
             WHERE table_name = 'marketplace_settlement_record'
               AND column_name IN ('row_type', 'line_reference',
                                   'expected_settlement_date')
        """).fetchone()[0] == 0
        # batch_run.org_id 도 함께 사라진다.
        assert conn.execute("""
            SELECT COUNT(*) FROM information_schema.columns
             WHERE table_name = 'batch_run' AND column_name = 'org_id'
        """).fetchone()[0] == 0

    # 다시 올리면 행 종류를 지어낼 수 없으므로 막힌다.
    with pytest.raises(AssertionError) as err:
        _alembic(fresh_db, "upgrade", "head")
    assert "M056_EXISTING_RECORDS" in str(err.value), "다른 이유로 실패했다"

    with _pg(fresh_db) as conn:
        conn.execute("DELETE FROM marketplace_settlement_record")
    _alembic(fresh_db, "upgrade", "head")


def test_m057_downgrade_refuses_to_hide_claim_runs(fresh_db: str):
    """클레임 실행 기록이 있으면 되돌리기가 실패한다.

    조용히 `ORDERS` 로 바꾸면 되돌리기가 클레임 실행을 주문 실행으로
    위조하는 셈이고, M057 이 고치려던 바로 그 상태로 돌아간다.
    """
    _alembic(fresh_db, "upgrade", "head")

    with _pg(fresh_db) as conn:
        sys_id = conn.execute("""
            INSERT INTO external_system (code, name, system_type, supports_api_key)
            VALUES ('NEXT_ENGINE', 'NE', 'SELLER_TOOL', TRUE)
            ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id
        """).fetchone()[0]
        acc = conn.execute("""
            INSERT INTO external_account (org_id, external_system_id, account_code,
                   display_name, credential_kind)
            VALUES (%s, %s, 'DEFAULT', 'NE', 'API_KEY') RETURNING id
        """, (SYSTEM_ORG_ID, sys_id)).fetchone()[0]
        conn.execute("""
            INSERT INTO external_sync_run
                   (org_id, external_account_id, sync_type, trigger, status)
            VALUES (%s, %s, 'CLAIMS', 'SCHEDULED', 'SUCCESS')
        """, (SYSTEM_ORG_ID, acc))

    with pytest.raises(AssertionError) as err:
        _alembic(fresh_db, "downgrade", "M056")
    assert "M057_CLAIM_RUNS_EXIST" in str(err.value), "다른 이유로 실패했다"

    # 정리하면 내려간다 — 스키마가 아니라 데이터 의미의 문제다.
    with _pg(fresh_db) as conn:
        conn.execute("DELETE FROM external_sync_run WHERE sync_type = 'CLAIMS'")
    _alembic(fresh_db, "downgrade", "M056")
    _alembic(fresh_db, "upgrade", "head")
