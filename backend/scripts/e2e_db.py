"""E2E 전용 DB 준비 / seed / 정리.

왜 별도 스크립트인가
-------------------
Playwright 는 브라우저를 몰고 다니지만 **DB 를 직접 확인할 수단이 없다.**
"저장했다" 를 화면 문구로만 확인하면, 화면만 맞고 DB 는 옛날 값인 상태를
그대로 통과시킨다 — 그게 바로 이번에 고친 mock 사고였다.

그래서 이 스크립트가 세 가지를 한다.
  · `prepare` — 전용 DB 생성 + migration head + 결정적 seed
  · `query`   — 임의 SELECT 를 JSON 으로 반환 (테스트가 실제 값을 검증)
  · `exec`    — 사전 조건 조작 (월 잠금 등). 테스트에서만 쓴다
  · `drop`    — 정리

안전장치
-------
**운영 DB 를 절대 건드리지 않는다.** DB 이름이 `_e2e` 로 끝나지 않으면 즉시 중단한다.
`drop` 은 그 위에 한 겹 더 확인한다. 실수로 개발 DB 를 날리면 복구 수단이 없다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg

BACKEND_DIR = Path(__file__).resolve().parent.parent

# ── 결정적 고정 ID ───────────────────────────────────────────
# 랜덤 UUID 를 쓰면 실패했을 때 어떤 행이 문제였는지 로그로 추적할 수 없고,
# 테스트가 seed 를 다시 조회해야 해서 왕복이 늘어난다.
ORG_ID = "e2e0e2e0-0000-0000-0000-000000000001"
USER_OPERATOR = "e2e0e2e0-0000-0000-0000-0000000000a1"
USER_APPROVER = "e2e0e2e0-0000-0000-0000-0000000000a2"
USER_READONLY = "e2e0e2e0-0000-0000-0000-0000000000a3"
USER_ACCOUNTANT = "e2e0e2e0-0000-0000-0000-0000000000a4"
# 6개 역할이 전부 있어야 "이 역할은 못 본다" 를 브라우저에서 확인할 수 있다.
# 네 개만 두면 SOURCING·EXTERNAL_ACCOUNTANT 의 차단은 아무도 검증하지 않는다.
USER_SOURCING = "e2e0e2e0-0000-0000-0000-0000000000a5"
USER_EXT_ACCOUNTANT = "e2e0e2e0-0000-0000-0000-0000000000a6"
PRODUCT_MEMO = "e2e0e2e0-0000-0000-0000-0000000000b1"
PRODUCT_NAME = "e2e0e2e0-0000-0000-0000-0000000000b2"
PRODUCT_COST = "e2e0e2e0-0000-0000-0000-0000000000b3"
PRODUCT_LOCK = "e2e0e2e0-0000-0000-0000-0000000000b4"
PRODUCT_RACE = "e2e0e2e0-0000-0000-0000-0000000000b5"
PRODUCT_OPTIONS = "e2e0e2e0-0000-0000-0000-0000000000b6"
PRODUCT_PRICE = "e2e0e2e0-0000-0000-0000-0000000000b7"
INTEGRATION_ACCOUNT = "e2e0e2e0-0000-0000-0000-0000000000d1"
INTEGRATION_ACCOUNT_2 = "e2e0e2e0-0000-0000-0000-0000000000d2"
EXPENSE_OPEN = "e2e0e2e0-0000-0000-0000-0000000000e1"
EXPENSE_LOCKED = "e2e0e2e0-0000-0000-0000-0000000000e2"
EXPENSE_CATEGORY = "e2e0e2e0-0000-0000-0000-0000000000f1"
# ⚠️ c1 은 product_option_group 이 이미 쓴다(아래 seed 참고). 테이블이 달라
#    충돌은 안 나지만 로그에서 같은 id 가 두 의미로 보여 추적이 어려워진다.
PARTNER_MAIN = "e2e0e2e0-0000-0000-0000-0000000000f2"
# 열린 달(2026-08) 정산 행 — 마감 흐름 E2E 대상
MONTHLY_OPEN = "e2e0e2e0-0000-0000-0000-0000000000c1"
SUPPLIER_ACTIVE = "e2e0e2e0-0000-0000-0000-0000000000f3"
SUPPLIER_INACTIVE = "e2e0e2e0-0000-0000-0000-0000000000f4"

# 대시보드 검증용 판매 — **2026-05** 에 심는다.
#
# 마감 흐름 E2E 가 쓰는 2026-06 에 심으면 그 테스트가 확정하는 숫자가 달라지고,
# 두 테스트가 실행 순서에 따라 서로를 깨뜨린다. 대시보드 전용 달을 따로 둔다.
DASHBOARD_YM = "2026-05"
SALE_COUPANG = "e2e0e2e0-0000-0000-0000-0000000000a5"
SALE_SMARTSTORE = "e2e0e2e0-0000-0000-0000-0000000000a6"

# 평문 비밀번호는 **테스트 계정 전용**이다. 운영 데이터에는 존재하지 않는 조직이고
# DB 이름도 `_e2e` 로 격리돼 있다. seed 외 어디에도 기록하지 않는다.
E2E_PASSWORD = "e2e-Passw0rd!"


def _url() -> str:
    url = os.environ.get("E2E_DATABASE_URL")
    if not url:
        raise SystemExit("E2E_DATABASE_URL 이 필요합니다.")
    dbname = url.rpartition("/")[2].partition("?")[0]
    if not dbname.endswith("_e2e"):
        # 이 검사를 지우지 마라. E2E 는 데이터를 지우는 작업을 한다.
        raise SystemExit(f"안전장치: E2E DB 이름은 '_e2e' 로 끝나야 합니다 (받은 값: {dbname})")
    return url


def _psycopg_dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _admin_dsn(url: str) -> str:
    return f"{_psycopg_dsn(url).rpartition('/')[0]}/postgres"


def prepare() -> None:
    url = _url()
    dsn = _psycopg_dsn(url)
    dbname = dsn.rpartition("/")[2]

    with psycopg.connect(_admin_dsn(url), autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{dbname}"')

    # migration 을 서브프로세스로 돌린다 — alembic 은 프로세스 단위 설정을 읽는다.
    env = {**os.environ, "DATABASE_URL": url}
    # ⚠️ `encoding` 을 명시한다. 없으면 Windows 기본 코덱(cp949)으로 디코딩하는데,
    #    alembic 은 마이그레이션 docstring 첫 줄을 그대로 찍는다. 그 줄에 em dash 나
    #    한글이 있으면 **마이그레이션은 성공했는데 로그를 읽다가 죽는다.**
    #    (실제로 M027 을 추가하자마자 E2E 전체가 여기서 멈췄다.)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise SystemExit(f"migration 실패:\n{result.stdout}\n{result.stderr}")

    seed(dsn)
    print(json.dumps({"database": dbname, "org_id": ORG_ID}))


def seed(dsn: str) -> None:
    """결정적 seed. 여러 번 실행해도 같은 상태가 된다(idempotent)."""
    # 애플리케이션과 **같은** 해시 함수를 쓴다. 여기서 다른 알고리즘을 쓰면
    # seed 는 성공하는데 로그인만 실패해서 원인을 찾기 어렵다.
    from app.core.security import hash_password

    pwd = hash_password(E2E_PASSWORD)

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("""
            INSERT INTO organization (id, business_name, business_type)
            VALUES (%s, 'E2E 테스트 조직', 'SOLE_PROPRIETOR')
            ON CONFLICT (id) DO NOTHING
        """, (ORG_ID,))

        # 도메인에 `.local` 을 쓰지 마라 — pydantic EmailStr 이 special-use TLD 로 보고
        # 422 로 거절한다. seed 는 통과하는데 로그인만 실패해서 원인 찾기가 어렵다.
        for uid, email, role, name in (
            (USER_OPERATOR, "operator@e2e-test.com", "OPERATOR", "이운영"),
            (USER_APPROVER, "approver@e2e-test.com", "ADMIN", "김승인"),
            (USER_READONLY, "readonly@e2e-test.com", "READONLY", "박조회"),
            (USER_ACCOUNTANT, "accountant@e2e-test.com", "ACCOUNTING", "최정산"),
            (USER_SOURCING, "sourcing@e2e-test.com", "SOURCING", "정소싱"),
            (USER_EXT_ACCOUNTANT, "extaccountant@e2e-test.com", "EXTERNAL_ACCOUNTANT", "외부세무"),
        ):
            conn.execute("""
                INSERT INTO app_user (id, org_id, email, password_hash, display_name, role, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (id) DO UPDATE
                   SET email = EXCLUDED.email, password_hash = EXCLUDED.password_hash,
                       display_name = EXCLUDED.display_name, role = EXCLUDED.role,
                       is_active = TRUE
            """, (uid, ORG_ID, email, pwd, name, role))

        for pid, bk, nm, memo in (
            (PRODUCT_MEMO,    "E2E-0001", "E2E 메모저장 상품",  "초기 메모"),
            (PRODUCT_NAME,    "E2E-0002", "E2E 이름변경 상품",  "이름 승인 대상"),
            (PRODUCT_COST,    "E2E-0003", "E2E 원가 상품",      "원가 승인 대상"),
            (PRODUCT_LOCK,    "E2E-0004", "E2E 월잠금 상품",    "월 잠금 대상"),
            (PRODUCT_RACE,    "E2E-0005", "E2E 동시성 상품",    "동시 저장 대상"),
            (PRODUCT_OPTIONS, "E2E-0006", "E2E 옵션 상품",      "옵션 조회 대상"),
            (PRODUCT_PRICE,   "E2E-0007", "E2E 가격 상품",      "판매처 가격 대상"),
        ):
            conn.execute("""
                INSERT INTO product (id, org_id, business_key, name_ko, margin_type,
                                     status, cost_amount, memo, has_variants)
                VALUES (%s, %s, %s, %s, 'GENERAL_RESELL', 'ACTIVE', 10000, %s, FALSE)
                ON CONFLICT (id) DO UPDATE
                   SET name_ko = EXCLUDED.name_ko, memo = EXCLUDED.memo,
                       cost_amount = 10000, status = 'ACTIVE', deleted_at = NULL
            """, (pid, ORG_ID, bk, nm, memo))

        # 옵션 상품에는 실제 옵션 그룹/값을 심는다 — 옵션 API 를 live 로 검증하기 위해.
        # 가격은 variant 를 참조하므로 **옵션보다 먼저** 지운다.
        # 순서를 바꾸면 product_channel_price_product_variant_id_fkey 위반이다.
        conn.execute("DELETE FROM channel_price_publish_outbox WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM product_channel_price WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM product_channel_listing WHERE org_id = %s", (ORG_ID,))

        # **모든 seed 상품**의 옵션을 초기화한다. 한 상품만 지우면 다른 상품에
        # 앞선 테스트의 variant 가 남아 같은 variant_key 로 409 가 난다.
        # 값 → variant → 그룹 순서로 지운다. 반대로 하면 FK 위반이다.
        conn.execute("""
            DELETE FROM product_option_value
             WHERE option_group_id IN (
                   SELECT id FROM product_option_group WHERE org_id = %s)
        """, (ORG_ID,))
        conn.execute("DELETE FROM product_variant WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM product_option_group WHERE org_id = %s", (ORG_ID,))
        gid = "e2e0e2e0-0000-0000-0000-0000000000c1"
        conn.execute("""
            INSERT INTO product_option_group (id, org_id, product_id, name, sort_order)
            VALUES (%s, %s, %s, '색상', 1)
        """, (gid, ORG_ID, PRODUCT_OPTIONS))
        for i, val in enumerate(("네이비", "그레이"), start=1):
            conn.execute("""
                INSERT INTO product_option_value (id, org_id, option_group_id, value, sort_order)
                VALUES (gen_random_uuid(), %s, %s, %s, %s)
            """, (ORG_ID, gid, val, i))
        conn.execute("UPDATE product SET has_variants = TRUE WHERE id = %s", (PRODUCT_OPTIONS,))

        # 판매처 계정 2개 — 같은 판매처에 스토어가 둘인 상황을 만든다.
        channel = conn.execute(
            "SELECT id FROM sale_channel WHERE code = 'COUPANG' AND org_id IS NULL LIMIT 1"
        ).fetchone()[0]
        for aid, label in ((INTEGRATION_ACCOUNT, "E2E 본점"),
                           (INTEGRATION_ACCOUNT_2, "E2E 2호점")):
            conn.execute("""
                INSERT INTO integration_account
                       (id, org_id, provider, account_label, status, sale_channel_id, auth_kind)
                VALUES (%s, %s, 'COUPANG', %s, 'ACTIVE', %s, 'API_KEY')
                ON CONFLICT (id) DO UPDATE
                   SET status = 'ACTIVE', account_label = EXCLUDED.account_label,
                       deleted_at = NULL
            """, (aid, ORG_ID, label, channel))

        # ── 월 마감 관련 정리 ──
        #
        # M028~M030 이후 monthly_settlement 를 참조하는 테이블이 늘었다.
        # 순서를 지키지 않으면 FK 위반으로 seed 자체가 실패한다.
        #
        # snapshot 은 DELETE 트리거로 보호된다. **테스트 DB 정리 전용**으로
        # 유지보수 탈출구를 연다 — 애플리케이션 코드에는 이 플래그가 없다.
        # ⚠️ `SET LOCAL` 이 아니라 `SET` 이다. 이 연결은 autocommit 이라
        #    트랜잭션 블록이 없고, `SET LOCAL` 은 조용히 아무 일도 하지 않는다.
        #    이 연결은 seed 전용이고 곧 닫히므로 세션 범위로 충분하다.
        conn.execute("SET picksome.allow_snapshot_delete = 'on'")
        conn.execute("""
            DELETE FROM payout_payment WHERE org_id = %s
        """, (ORG_ID,))
        conn.execute("DELETE FROM payout_item WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM payout_batch WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM distribution_result WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM distribution_run WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM operation_issue WHERE org_id = %s", (ORG_ID,))
        # 현금 기록도 비운다. 남겨 두면 "이 기록이 거부돼 아무 행도 안 생겼다" 를
        # 확인하는 테스트가 **앞 테스트가 만든 행**을 보고 실패한다 —
        # worker_run·notification 과 같은 이유다.
        # 외부 연동도 비운다. 남겨 두면 "카탈로그에 N개" 나 매핑 개수를 세는
        # 테스트가 앞 테스트가 만든 행을 보고 실행 순서에 좌우된다.
        # FK 순서: 충돌 -> 실행 -> 매핑 -> 계정.
        conn.execute("DELETE FROM external_sync_conflict WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM external_sync_run WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM external_entity_mapping WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM external_field_authority WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM external_account WHERE org_id = %s", (ORG_ID,))
        # 보관함 항목은 external_account 가 참조한다 — 계정을 지운 뒤에 지운다.
        # (여기에 들어가는 건 보관 **위치**뿐이다. 비밀은 애초에 없다.)
        conn.execute("DELETE FROM password_vault_entry WHERE org_id = %s", (ORG_ID,))
        # 자격증명 암호문(M042). 남겨 두면 "살아 있는 비밀은 하나" 를 세는
        # 테스트가 앞 테스트가 만든 행을 보고 실행 순서에 좌우된다.
        # 여기 들어가는 값은 전부 가짜다 — 운영 credential 을 E2E 에 넣지 않는다.
        conn.execute("DELETE FROM credential_secret WHERE org_id = %s", (ORG_ID,))
        # 자동화·서비스 계정(M044/M045). 남겨 두면 "기본이 꺼짐" 이나
        # "정책 N개" 를 세는 테스트가 앞 테스트가 만든 행을 보고 실행 순서에
        # 좌우된다. FK 순서: 액션 -> 실행 -> 정책, 토큰 -> 계정.
        conn.execute("DELETE FROM automation_action WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM automation_run WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM automation_policy WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM api_token WHERE org_id = %s", (ORG_ID,))
        # 승인 요청이 서비스 계정을 참조할 수 있다(M046) — 먼저 끊는다.
        conn.execute("""
            UPDATE approval_request SET requested_by_service_account_id = NULL
             WHERE org_id = %s AND requested_by_service_account_id IS NOT NULL
        """, (ORG_ID,))
        conn.execute("DELETE FROM service_account WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM supplier_import_run WHERE org_id = %s", (ORG_ID,))
        # 소싱 데이터도 비운다. 남겨 두면 "공급처 N곳" 을 확인하는 테스트가
        # 앞 테스트가 만든 공급처를 세고, 실행 순서에 결과가 좌우된다.
        # FK 순서: 원가 version -> 소싱처 -> 공급처.
        # 재고·발주도 비운다 (M048/M050). 남겨 두면 "재고 N개" 를 확인하는
        # 테스트가 앞 테스트가 넣은 원장을 세고, 실행 순서에 결과가 좌우된다.
        # FK 순서: 발주 품목 -> 발주 -> (소싱처). 원장은 상품만 참조한다.
        conn.execute("DELETE FROM inventory_transaction WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM inventory_import_run WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM purchase_order_line WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM purchase_order WHERE org_id = %s", (ORG_ID,))
        conn.execute("""
            DELETE FROM product_source_cost_version WHERE org_id = %s
        """, (ORG_ID,))
        conn.execute("DELETE FROM product_source WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM supplier_site WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM fx_rate_snapshot WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM manual_cash_event WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM cash_balance_baseline WHERE org_id = %s", (ORG_ID,))
        # worker 실행 기록도 지운다. 남겨 두면 "한 번도 스캔한 적 없다" 를 검증하는
        # 테스트가 **앞 테스트의 스캔 기록**을 보고 실패한다 — 실행 순서에 따라
        # 결과가 달라지는 테스트가 된다. 이 DB 는 E2E 전용이라 전량 삭제해도 된다.
        conn.execute("DELETE FROM worker_run")
        conn.execute("""
            UPDATE monthly_settlement
               SET close_snapshot_id = NULL, last_check_run_id = NULL
             WHERE org_id = %s
        """, (ORG_ID,))
        conn.execute("DELETE FROM monthly_close_snapshot WHERE org_id = %s", (ORG_ID,))
        conn.execute("""
            DELETE FROM settlement_reopen_audit
             WHERE monthly_settlement_id IN (
               SELECT id FROM monthly_settlement WHERE org_id = %s
             )
        """, (ORG_ID,))
        conn.execute("DELETE FROM monthly_close_check_run WHERE org_id = %s", (ORG_ID,))
        conn.execute("""
            DELETE FROM loss_carryover_ledger WHERE org_id = %s
        """, (ORG_ID,))
        conn.execute("DELETE FROM monthly_settlement WHERE org_id = %s", (ORG_ID,))

        # ── 사업비용 ──
        # 열린 달 1건 + 잠긴 달 1건. 잠긴 달은 조정 흐름을 검증하는 데 쓴다.
        # 첨부는 expenses 를 참조한다 — 먼저 지운다.
        conn.execute("DELETE FROM expense_attachment WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM expense_adjustment WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM period_recalc_flag WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM expenses WHERE org_id = %s", (ORG_ID,))
        # E2E 조직 전용 카테고리. 픽썸 조직 것을 빌려 쓰면 조직 격리가 깨진다.
        conn.execute("""
            INSERT INTO expense_category_policy
                   (id, org_id, version, effective_from, source_type,
                    reliability_status, is_active, name, kind,
                    default_tax_deductible, default_settlement_deductible, sort_order)
            VALUES (%s, %s, 1, '2020-01-01', 'MANUAL',
                    'CONFIRMED', TRUE, 'E2E 광고비', 'MARKETING', TRUE, TRUE, 1)
            ON CONFLICT (id) DO NOTHING
        """, (EXPENSE_CATEGORY, ORG_ID))
        # 결제자로 쓸 파트너. **app_user 와 다르다** — 파트너는 수익을 배분받는
        # 주체고 로그인 계정이 없을 수도 있다. 이게 없으면 목록의 결제자 열과
        # 드로어의 결제자 선택이 빈 채로만 검증된다.
        #
        # `ON CONFLICT (id)` 만으로는 부족하다 — 이름에 UNIQUE 가 걸려 있어서,
        # seed 의 고정 id 를 바꾸면 **옛 id 의 같은 이름 행**과 충돌한다.
        # 지우고 넣는다: 이 조직은 E2E 전용이라 남길 이력이 없다.
        # 배분 정책이 파트너를 참조한다 — 파트너를 지우기 전에 먼저 지운다.
        # 순서를 바꾸면 FK 위반으로 seed 자체가 실패한다.
        conn.execute("DELETE FROM partner_share_policy WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM partner WHERE org_id = %s", (ORG_ID,))
        conn.execute("""
            INSERT INTO partner (id, org_id, name, display_order, is_active)
            VALUES (%s, %s, 'E2E 파트너', 1, TRUE)
        """, (PARTNER_MAIN, ORG_ID))

        # 배분 비율 100%.
        #
        # ⚠️ 합이 1.0 이 **아니면** 마감 검증이 `DISTRIBUTION_RATIO_INVALID`(BLOCKING)
        #    를 올려 어떤 달도 확정할 수 없게 된다. 파트너를 늘리면 합을 다시 1.0 으로
        #    맞춰야 한다. 아예 없으면(합 0) 검사를 건너뛰지만 배분 결과도 비어 있다.
        conn.execute("""
            INSERT INTO partner_share_policy
                   (id, org_id, version, effective_from, partner_id, share_ratio,
                    is_active, reliability_status, source_type)
            VALUES (gen_random_uuid(), %s, 1, '2020-01-01', %s, 1.0,
                    TRUE, 'CONFIRMED', 'MANUAL')
        """, (ORG_ID, PARTNER_MAIN))

        # 거래처(external_partner). **partner 와 다른 테이블이다** — 이쪽은
        # 돈을 지불하는 상대다. 비활성 1건을 같이 심는다: 선택 목록에서는 빠지지만
        # 과거 비용의 이름은 남아야 한다는 규칙을 검증할 대상이 필요하다.
        conn.execute("""
            DELETE FROM external_partner
             WHERE org_id = %s
               AND NOT EXISTS (SELECT 1 FROM product p WHERE p.supplier_id = external_partner.id)
        """, (ORG_ID,))
        conn.execute("""
            INSERT INTO external_partner (id, org_id, name, kind, is_active)
            VALUES (%s, %s, 'E2E 물류사', 'LOGISTICS', TRUE),
                   (%s, %s, 'E2E 폐업거래처', 'SUPPLIER', FALSE)
            ON CONFLICT (id) DO UPDATE
               SET name = EXCLUDED.name, kind = EXCLUDED.kind,
                   is_active = EXCLUDED.is_active, deleted_at = NULL
        """, (SUPPLIER_ACTIVE, ORG_ID, SUPPLIER_INACTIVE, ORG_ID))

        category = (EXPENSE_CATEGORY,)
        if category:
            for eid, day, amount in ((EXPENSE_OPEN, "2026-08-10", 120000),
                                     (EXPENSE_LOCKED, "2026-07-10", 300000)):
                conn.execute("""
                    INSERT INTO expenses
                           (id, org_id, expense_date, category_id, description,
                            gross_amount, supply_amount, vat_amount, vat_inclusion_type,
                            tax_type, payment_method, payment_card_holder_id,
                            is_personal_card, is_business_use,
                            evidence_type, evidence_status,
                            is_tax_deductible, is_settlement_deductible)
                    VALUES (%s, %s, %s, %s, 'E2E 비용',
                            %s, %s, 0, 'INCLUDED',
                            'GENERAL', 'BUSINESS_CARD', %s, FALSE, TRUE,
                            'CARD_SLIP', 'PRESENT', TRUE, TRUE)
                """, (eid, ORG_ID, day, category[0], amount, amount, PARTNER_MAIN))

        # 7월을 잠근다 — 조정 경로 검증용.
        approver = USER_APPROVER
        conn.execute("""
            INSERT INTO monthly_settlement
                   (id, org_id, business_key, year_month, status, close_state,
                    confirmed_at, confirmed_by)
            VALUES (gen_random_uuid(), %s, 'E2E-MS-LOCK', '2026-07', 'LOCKED', 'LOCKED',
                    now(), %s)
        """, (ORG_ID, approver))

        # 마감 흐름 E2E 가 검증→확정→잠금을 태울 **열린 달**.
        #
        # ⚠️ "이번 달" 을 쓰지 않는다. `product-save.spec.ts` 가 KST 오늘이 속한
        #    달의 정산 행을 직접 INSERT 하는데, 여기서 같은 달을 미리 심으면
        #    UNIQUE(org_id, year_month) 로 충돌해 그 테스트가 깨진다.
        #    실행 시각에 따라 결과가 달라지지 않도록 고정된 과거 달을 쓴다.
        conn.execute("""
            INSERT INTO monthly_settlement
                   (id, org_id, business_key, year_month, status, close_state,
                    total_revenue, total_direct_cost, total_common_expense, net_profit)
            VALUES (%s, %s, 'E2E-MS-OPEN', '2026-06', 'DRAFT', 'OPEN', 0, 0, 0, 0)
        """, (MONTHLY_OPEN, ORG_ID))

        # 대시보드 전용 달도 열린 정산 행이 있어야 한다.
        # 확정(`monthly_close.confirm`)은 `monthly_settlement` 행을 요구한다 —
        # 행이 없으면 "정산이 아직 생성되지 않았습니다" 로 거절된다.
        conn.execute("""
            INSERT INTO monthly_settlement
                   (id, org_id, business_key, year_month, status, close_state,
                    total_revenue, total_direct_cost, total_common_expense, net_profit)
            VALUES (gen_random_uuid(), %s, 'E2E-MS-DASH', %s, 'DRAFT', 'OPEN', 0, 0, 0, 0)
        """, (ORG_ID, DASHBOARD_YM))

        # ── 대시보드 검증용 판매 (2026-05) ──
        #
        # 대시보드가 실제 DB 를 집계하는지 확인하려면 0이 아닌 숫자가 필요하다.
        # 판매처를 둘로 나눠 심는다 — 판매처 분해 합계가 총매출과 맞는지 보려면
        # 판매처가 하나뿐이면 검증이 되지 않는다.
        #
        # `purchase_cost` 를 반드시 채운다. 비우면 마감 검증이 `ZERO_COST_SALE`
        # (BLOCKING)을 올려 이 달을 확정할 수 없게 된다.
        # 실마진이 판매를 참조한다 — 실마진을 **먼저** 전부 지운다.
        # 두 seed 건만 지우면 앞선 테스트가 만든 다른 실마진 행이 남아 FK 위반이 난다.
        conn.execute("DELETE FROM product_margin_actual WHERE org_id = %s", (ORG_ID,))
        # 검증 기준 사례도 판매 건을 가리킨다 (M059). 여기서 안 지우면 다음
        # reseed 가 FK 위반으로 죽는다 — 테스트가 사례를 하나라도 만들면
        # **그 다음 실행 전체가** 준비 단계에서 실패한다(실제로 겪었다).
        #
        # ON DELETE CASCADE 를 붙이지 않는 이유: 사람이 원천을 보고 승인한
        # 기준값이 판매 건 정리에 딸려 조용히 사라지면 안 된다. 지우는 자리를
        # 명시하는 편이 낫다.
        conn.execute(
            "DELETE FROM verification_golden_case WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM sales_order WHERE org_id = %s", (ORG_ID,))

        # 쿠팡 주문만 **본점 계정**에 귀속시킨다 (M032).
        # 스마트스토어 주문은 계정 없이 둔다 — legacy NULL 주문이 조회·집계에서
        # 빠지지 않는지 확인할 대상이 필요하다.
        for sid, mkt, price, cost, mfee, day, account in (
            (SALE_COUPANG,    "COUPANG",     300_000, 180_000, 30_000, "05", INTEGRATION_ACCOUNT),
            (SALE_SMARTSTORE, "SMARTSTORE",  100_000,  60_000,  5_000, "12", None),
        ):
            conn.execute("""
                INSERT INTO sales_order
                       (id, org_id, business_key, sale_date, marketplace,
                        marketplace_order_no, line_no, product_id, quantity,
                        sale_price, ad_cost, purchase_cost, shipping_fee,
                        integration_account_id)
                VALUES (%s, %s, %s, %s, %s, %s, 1, %s, 1, %s, 0, %s, 0, %s)
            """, (sid, ORG_ID, f"E2E-SO-{mkt}", f"{DASHBOARD_YM}-{day}", mkt,
                  f"E2E-ORD-{mkt}", PRODUCT_MEMO, price, cost, account))
            # 나머지 비용 컬럼은 NOT NULL 이다. 생략하면 seed 가 실패한다.
            # 0 은 "실제로 0원" 이라는 뜻이고, 이 seed 는 그 조건을 의도한다.
            conn.execute("""
                INSERT INTO product_margin_actual
                       (id, org_id, product_id, sales_order_id,
                        actual_revenue, actual_cost, actual_marketplace_fee,
                        actual_payment_fee, actual_ad_cost, actual_fulfillment_fee,
                        actual_return_cost, actual_vat, actual_shipping_cost,
                        actual_margin_amount, actual_margin_rate)
                VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s,
                        0, 0, 0, 0, 0, 0, %s, %s)
            """, (ORG_ID, PRODUCT_MEMO, sid, price, cost, mfee,
                  price - cost - mfee, round((price - cost - mfee) / price, 4)))

        # 앞선 실행이 남긴 승인 요청을 지운다 — 열린 요청이 남아 있으면 409 로 막힌다.
        conn.execute("DELETE FROM approval_request WHERE org_id = %s", (ORG_ID,))
        conn.execute("DELETE FROM audit_log WHERE org_id = %s", (ORG_ID,))
        # 알림도 비운다. 남겨 두면 "이 사건으로 알림이 생겼는가" 를 확인하는 테스트가
        # **앞 테스트가 만든 알림**을 보고 통과하거나 실패한다 — 실행 순서에 결과가
        # 좌우되는 테스트가 된다(worker_run 과 같은 이유).
        conn.execute("DELETE FROM notification WHERE org_id = %s", (ORG_ID,))



def query(sql: str) -> None:
    """SELECT 결과를 JSON 으로. 테스트가 **DB 실제 값**을 확인하는 통로다."""
    with psycopg.connect(_psycopg_dsn(_url())) as conn:
        cur = conn.execute(sql)
        cols = [d.name for d in cur.description or []]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    print(json.dumps(rows, default=str))


def execute(sql: str) -> None:
    """사전 조건 조작 (월 잠금 등). 테스트 전용이다."""
    with psycopg.connect(_psycopg_dsn(_url()), autocommit=True) as conn:
        cur = conn.execute(sql)
        print(json.dumps({"rowcount": cur.rowcount}))


def hold_lock(seconds: float) -> None:
    """executor advisory lock 을 붙잡고 있는다 — **중복 실행 차단 테스트 전용**.

    HTTP 요청 두 개를 동시에 던져서 겹치기를 기대하는 방식은 못 쓴다. 배치가 비어 있으면
    몇 ms 만에 끝나서 두 요청이 순서대로 처리되고, 테스트가 간헐적으로 통과한다.
    "가끔 통과하는 테스트" 는 방어가 아니다.

    그래서 잠금을 **확실히 붙잡은 상태**를 만들어 놓고 요청을 한 번 던진다.
    준비가 끝나면 stdout 에 LOCKED 를 찍어 테스트가 기다릴 수 있게 한다.
    """
    import time
    import uuid as _uuid

    sys.path.insert(0, str(BACKEND_DIR))
    from app.services.approval_executor import _LOCK_NAMESPACE, _lock_key_for

    key = _lock_key_for(_uuid.UUID(ORG_ID))
    with psycopg.connect(_psycopg_dsn(_url()), autocommit=True) as conn:
        got = conn.execute(
            "SELECT pg_try_advisory_lock(%s, %s)", (_LOCK_NAMESPACE, key)
        ).fetchone()[0]
        if not got:
            raise SystemExit("잠금을 잡지 못했습니다 — 앞선 실행이 아직 붙잡고 있습니다.")
        print("LOCKED", flush=True)
        time.sleep(seconds)
        conn.execute("SELECT pg_advisory_unlock(%s, %s)", (_LOCK_NAMESPACE, key))
    print("RELEASED", flush=True)


def drop() -> None:
    url = _url()
    dsn = _psycopg_dsn(url)
    dbname = dsn.rpartition("/")[2]
    with psycopg.connect(_admin_dsn(url), autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (dbname,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
    print(json.dumps({"dropped": dbname}))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "prepare":
        prepare()
    elif cmd == "query":
        query(sys.argv[2])
    elif cmd == "exec":
        execute(sys.argv[2])
    elif cmd == "hold-lock":
        hold_lock(float(sys.argv[2]))
    elif cmd == "drop":
        drop()
    else:
        raise SystemExit("usage: e2e_db.py {prepare|query|exec|hold-lock|drop} [arg]")
