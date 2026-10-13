"""PICKSOME 내부 전용 초기화 스크립트.

이 스크립트는 **SaaS 빌드/배포에 포함되지 않는다.** 픽썸 내부 운영 진입 전
PICKSOME 조직 데이터를 한 번만 적재한다.

실행:
    python -m scripts.internal.picksome_init

수행 작업 (순서):
  1. PICKSOME organization 생성 (JOINT_BUSINESS, reopen_limit=3)
  2. SYSTEM 템플릿(M002) → PICKSOME 으로 8테이블 정책 복사
     - expense_category_policy / evidence_type_policy / tax_policy /
       target_margin_policy / settlement_policy / marketplace_fee_policy
  3. partner 2명 INSERT (파트너A / 파트너B)
  4. admin app_user 1명 INSERT (argon2 placeholder hash, 첫 로그인 시 reset 강제)
  5. partner_share_policy: 파트너A 0.6 / 파트너B 0.4
  6. payment_fee_policy: 사업자카드 2.5%

향후 SaaS signup API 는 단계 1+2 와 동일 로직을 재사용한다 (Post-MVP).

CLAUDE.md Security Rule:
  admin 초기 비밀번호는 코드에 평문으로 두지 않는다. ADMIN_SEED_PASSWORD env 로 주입하거나,
  미설정 시 실행 시점에 랜덤 생성해 콘솔에 1회만 출력한다 (git/log 어디에도 남기지 않음).
  운영 진입 전 반드시 별도 비밀번호로 교체할 것.

멱등성:
  이미 PICKSOME org 가 존재하면 즉시 종료 (중복 실행 안전).
"""
from __future__ import annotations

import os
import secrets
import sys

from sqlalchemy import text

from app.core.security import hash_password
from app.db.session import SessionLocal

SYSTEM_ORG_ID = "00000000-0000-0000-0000-0000000000ff"
PICKSOME_ORG_ID = "00000000-0000-0000-0000-000000000001"
ADMIN_USER_ID = "00000000-0000-0000-0000-000000000010"
PARTNER_HMK_ID = "00000000-0000-0000-0000-000000000101"
PARTNER_SSW_ID = "00000000-0000-0000-0000-000000000102"

# SYSTEM → 신규 org 복사 대상 정책 테이블 (가입 hook 도 동일 목록 재사용).
COPYABLE_POLICY_TABLES: tuple[tuple[str, str], ...] = (
    # (테이블명, "정책 고유 컬럼 목록 — POLICY_COLS/AUDIT_COLS 제외")
    ("expense_category_policy", "name, kind, default_tax_deductible, default_settlement_deductible, sort_order"),
    ("evidence_type_policy", "code, label, is_official, description"),
    ("tax_policy", "tax_type, business_type, vat_rate, calculation_method, default_vat_inclusion, applies_to"),
    ("target_margin_policy", "category_path, margin_type, target_rate, warning_lower_rate"),
    ("settlement_policy", "loss_policy, priority_distribution_json, close_day"),
    ("marketplace_fee_policy", "marketplace, category_path, fee_type, rate, fixed_amount, tier_json"),
)


def _copy_from_system_template(db, target_org_id: str) -> None:
    """SYSTEM 템플릿의 활성 정책 row 를 target_org 로 복사.

    공통 정책 컬럼은 그대로 가져가되 org_id 만 target_org_id 로 치환.
    """
    for table, cols in COPYABLE_POLICY_TABLES:
        db.execute(
            text(
                f"""
                INSERT INTO {table} (
                  org_id, version, effective_from, effective_to,
                  source_type, source_url, source_checked_at,
                  approved_by, approved_at, reliability_status, memo, is_active,
                  {cols}
                )
                SELECT
                  :target_org, 1, effective_from, effective_to,
                  source_type, source_url, source_checked_at,
                  NULL, NULL, reliability_status, memo, is_active,
                  {cols}
                FROM {table}
                WHERE org_id = :system_org AND deleted_at IS NULL
                """
            ),
            {"target_org": target_org_id, "system_org": SYSTEM_ORG_ID},
        )


def main() -> int:
    db = SessionLocal()
    try:
        # ----------------------------------------------------------------
        # 멱등성 체크: 이미 적재되어 있으면 종료.
        # ----------------------------------------------------------------
        existing = db.execute(
            text("SELECT id FROM organization WHERE id = :id"),
            {"id": PICKSOME_ORG_ID},
        ).first()
        if existing:
            print(f"[picksome_init] PICKSOME org 가 이미 존재합니다. ({PICKSOME_ORG_ID})")
            return 0

        # ----------------------------------------------------------------
        # 1) PICKSOME organization
        # ----------------------------------------------------------------
        db.execute(
            text(
                """
                INSERT INTO organization (
                  id, business_name, business_type,
                  primary_currency, timezone, settlement_reopen_limit
                ) VALUES (
                  :id, 'PICKSOME', 'JOINT_BUSINESS',
                  'KRW', 'Asia/Seoul', 3
                )
                """
            ),
            {"id": PICKSOME_ORG_ID},
        )

        # ----------------------------------------------------------------
        # 2) SYSTEM → PICKSOME 정책 복사 (8테이블)
        # ----------------------------------------------------------------
        _copy_from_system_template(db, PICKSOME_ORG_ID)

        # ----------------------------------------------------------------
        # 3) partner (파트너A / 파트너B)
        # ----------------------------------------------------------------
        db.execute(
            text(
                """
                INSERT INTO partner (id, org_id, name, display_order, is_active)
                VALUES
                  (:hmk, :org, '파트너A', 0, TRUE),
                  (:ssw, :org, '파트너B', 1, TRUE)
                """
            ),
            {"hmk": PARTNER_HMK_ID, "ssw": PARTNER_SSW_ID, "org": PICKSOME_ORG_ID},
        )

        # ----------------------------------------------------------------
        # 4) admin app_user
        #   비밀번호는 ADMIN_SEED_PASSWORD env 로 주입하거나, 미설정 시 여기서
        #   랜덤 생성해 콘솔에 1회만 출력한다. 평문은 어디에도 저장하지 않는다.
        # ----------------------------------------------------------------
        admin_email = "admin@picksome.co.kr"
        admin_password = os.environ.get("ADMIN_SEED_PASSWORD") or secrets.token_urlsafe(12)
        admin_hash = hash_password(admin_password)

        db.execute(
            text(
                """
                INSERT INTO app_user (
                  id, org_id, email, password_hash,
                  display_name, role, is_active
                ) VALUES (
                  :id, :org, :email, :hash,
                  'Admin', 'ADMIN', TRUE
                )
                """
            ),
            {"id": ADMIN_USER_ID, "org": PICKSOME_ORG_ID, "email": admin_email, "hash": admin_hash},
        )

        # ----------------------------------------------------------------
        # 3b) settlement_policy 우선분배 설정 (파트너A 100만 / 파트너B 40만)
        #   SYSTEM → PICKSOME 복사 시 priority_distribution_json = '[]'.
        #   파트너 UUID 가 확정된 이 시점에 PICKSOME org 분으로 패치한다.
        # ----------------------------------------------------------------
        db.execute(
            text(
                """
                UPDATE settlement_policy
                   SET priority_distribution_json = :pdj
                 WHERE org_id = :org AND deleted_at IS NULL
                """
            ),
            {
                "org": PICKSOME_ORG_ID,
                "pdj": (
                    f'[{{"partner_id":"{PARTNER_HMK_ID}","fixed_amount":1000000}},'
                    f'{{"partner_id":"{PARTNER_SSW_ID}","fixed_amount":400000}}]'
                ),
            },
        )

        # ----------------------------------------------------------------
        # 5) partner_share_policy (파트너A 0.6 / 파트너B 0.4)
        # ----------------------------------------------------------------
        db.execute(
            text(
                """
                INSERT INTO partner_share_policy (
                  org_id, version, effective_from, source_type, reliability_status, is_active,
                  partner_id, share_ratio
                ) VALUES
                  (:org, 1, '2026-01-01', 'MANUAL', 'CONFIRMED', TRUE, :hmk, 0.6000),
                  (:org, 1, '2026-01-01', 'MANUAL', 'CONFIRMED', TRUE, :ssw, 0.4000)
                """
            ),
            {"org": PICKSOME_ORG_ID, "hmk": PARTNER_HMK_ID, "ssw": PARTNER_SSW_ID},
        )

        # ----------------------------------------------------------------
        # 6) payment_fee_policy (사업자카드 2.5% — 추후 카드사별 조정)
        # ----------------------------------------------------------------
        db.execute(
            text(
                """
                INSERT INTO payment_fee_policy (
                  org_id, version, effective_from, source_type, reliability_status, is_active,
                  method, rate
                ) VALUES (
                  :org, 1, '2026-01-01', 'MANUAL', 'NEEDS_REVIEW', TRUE,
                  'BUSINESS_CARD', 0.0250
                )
                """
            ),
            {"org": PICKSOME_ORG_ID},
        )

        db.commit()
        print(f"[picksome_init] PICKSOME org 적재 완료 ({PICKSOME_ORG_ID}).")
        print("  - partner: 파트너A (share 0.6, priority 100만) / 파트너B (share 0.4, priority 40만)")
        print("  - settlement_policy: CARRYOVER, priority_distribution 적용")
        print("")
        print("  [admin 계정: 이 비밀번호는 여기서만 출력됨, 어디에도 저장되지 않으니 지금 적어둘 것]")
        print(f"    email:    {admin_email}")
        print(f"    password: {admin_password}")
        print("  운영 진입 전 반드시 로그인 후 비밀번호를 교체하세요.")
        return 0

    except Exception as e:
        db.rollback()
        print(f"[picksome_init] 실패: {e}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
