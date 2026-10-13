"""M002 baseline seed — PICKSOME OPS TOOL (SaaS 범용 baseline).

목적:
  모든 배포 환경(픽썸 내부 + 향후 SaaS 고객) 공통으로 필요한 글로벌 메타와
  '템플릿 정책' 을 적재한다. **특정 조직(픽썸 등)의 데이터는 절대 포함하지 않는다.**

설계 원칙:
  - 글로벌 메타(`notification_channel`) 는 그대로 적재.
  - org_id 가 NOT NULL 인 정책 테이블은 SYSTEM 템플릿 org(`00000000-...-0000000000ff`)
    아래에 default row 를 적재.
  - 신규 조직 가입(=내부 픽썸 init 스크립트 또는 향후 SaaS signup API) 시
    동일한 INSERT...SELECT 패턴으로 SYSTEM 템플릿을 신규 org_id 로 복사.

이 마이그레이션이 적재하는 내용:
  - organization        : SYSTEM 1건 (이름 '_SYSTEM_TEMPLATE')
  - notification_channel: 6종 (EMAIL/IN_APP active, KAKAO/SLACK/TELEGRAM/WEBHOOK inactive)
  - expense_category_policy : 12종 (SYSTEM)
  - evidence_type_policy    : 7종 (SYSTEM)
  - tax_policy              : 일반과세 10% 기본 (SYSTEM)
  - target_margin_policy    : 30% 기본 (SYSTEM)
  - settlement_policy       : CARRYOVER 기본 (SYSTEM)
  - marketplace_fee_policy  : COUPANG 기본율 (SYSTEM, NEEDS_VERIFICATION)

이 마이그레이션이 적재하지 않는 것 (= 내부 init 스크립트 책임):
  - PICKSOME organization / partner / app_user
  - partner_share_policy (partner FK 필요)
  - payment_fee_policy (조직별 카드사 협상율)

격리 원칙:
  - services.policy.resolve_active_policies 는 org_id 필터 → SYSTEM 정책은 일반 조직 조회에 안 나옴.
  - SYSTEM org 에는 app_user/partner 가 없어서 사용자 노출 위험 없음.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M002"
down_revision: str | None = "M001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# SYSTEM 템플릿 조직 sentinel UUID.
# 신규 가입 hook 이 이 org_id 의 모든 정책 row 를 신규 org_id 로 복사한다.
SYSTEM_ORG_ID = "00000000-0000-0000-0000-0000000000ff"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. SYSTEM 템플릿 organization
    # ------------------------------------------------------------------
    op.execute(
        f"""
        INSERT INTO organization (
          id, business_name, business_type,
          primary_currency, timezone, settlement_reopen_limit
        ) VALUES (
          '{SYSTEM_ORG_ID}',
          '_SYSTEM_TEMPLATE',
          'SOLE_PROPRIETOR',
          'KRW',
          'Asia/Seoul',
          3
        );
        """
    )

    # ------------------------------------------------------------------
    # 2. notification_channel (글로벌, org_id 없음 — D-281)
    # ------------------------------------------------------------------
    op.execute(
        """
        INSERT INTO notification_channel
          (code, display_name, adapter_class, is_mvp_active, requires_config_json, sort_order)
        VALUES
          ('IN_APP', '앱 내부 알림',
            'app.core.notification.channels.in_app.InAppChannel', TRUE,
            '[]'::jsonb, 0),
          ('EMAIL', '이메일',
            'app.core.notification.channels.email_smtp.EmailSmtpChannel', TRUE,
            '["SMTP_HOST","SMTP_PORT","SMTP_USER","SMTP_PASSWORD","SMTP_FROM"]'::jsonb, 1),
          ('KAKAO', '카카오 알림톡',
            'app.core.notification.channels.kakao.KakaoChannel', FALSE,
            '["KAKAO_API_KEY"]'::jsonb, 2),
          ('SLACK', 'Slack',
            'app.core.notification.channels.slack.SlackChannel', FALSE,
            '["SLACK_WEBHOOK_URL"]'::jsonb, 3),
          ('TELEGRAM', 'Telegram',
            'app.core.notification.channels.telegram.TelegramChannel', FALSE,
            '["TELEGRAM_BOT_TOKEN"]'::jsonb, 4),
          ('WEBHOOK', '범용 Webhook',
            'app.core.notification.channels.webhook.WebhookChannel', FALSE,
            '["WEBHOOK_URL"]'::jsonb, 5);
        """
    )

    # ------------------------------------------------------------------
    # 3. expense_category_policy 템플릿 (12종, SYSTEM)
    # ------------------------------------------------------------------
    expense_categories = [
        ("원가/상품매입", "COGS", True, True, 0),
        ("마켓 광고비", "MARKETING", True, True, 1),
        ("외부 광고비", "MARKETING", True, True, 2),
        ("배송비", "LOGISTICS", True, True, 3),
        ("창고/물류 보관비", "LOGISTICS", True, True, 4),
        ("사무용품", "OFFICE", True, True, 5),
        ("통신비", "UTILITY", True, True, 6),
        ("인건비/외주", "LABOR", True, True, 7),
        ("세금/공과금", "TAX", True, False, 8),
        ("수수료", "FEE", True, True, 9),
        ("SaaS 구독", "SUBSCRIPTION", True, True, 10),
        ("기타", "OTHER", True, True, 11),
    ]
    for name, kind, tax_d, settle_d, order in expense_categories:
        op.execute(
            f"""
            INSERT INTO expense_category_policy
              (org_id, version, effective_from, source_type, reliability_status, is_active,
               name, kind, default_tax_deductible, default_settlement_deductible, sort_order)
            VALUES (
              '{SYSTEM_ORG_ID}', 1, '2026-01-01',
              'MANUAL', 'CONFIRMED', TRUE,
              '{name}', '{kind}', {str(tax_d).upper()}, {str(settle_d).upper()}, {order}
            );
            """
        )

    # ------------------------------------------------------------------
    # 4. evidence_type_policy 템플릿 (7종, SYSTEM)
    # ------------------------------------------------------------------
    evidence_types = [
        ("TAX_INVOICE", "세금계산서", True),
        ("CASH_RECEIPT", "현금영수증", True),
        ("CARD_SLIP", "카드매출전표", True),
        ("BANK_TRANSFER", "계좌이체 확인증", True),
        ("SIMPLE_RECEIPT", "간이영수증", False),
        ("SELF_DECLARED", "본인 신고", False),
        ("OTHER", "기타", False),
    ]
    for code, label, official in evidence_types:
        op.execute(
            f"""
            INSERT INTO evidence_type_policy
              (org_id, version, effective_from, source_type, reliability_status, is_active,
               code, label, is_official)
            VALUES (
              '{SYSTEM_ORG_ID}', 1, '2026-01-01',
              'MANUAL', 'CONFIRMED', TRUE,
              '{code}', '{label}', {str(official).upper()}
            );
            """
        )

    # ------------------------------------------------------------------
    # 5. tax_policy 템플릿 (일반과세 10%, SYSTEM)
    # ------------------------------------------------------------------
    op.execute(
        f"""
        INSERT INTO tax_policy
          (org_id, version, effective_from, source_type, reliability_status, is_active,
           tax_type, business_type, vat_rate, calculation_method,
           default_vat_inclusion, applies_to)
        VALUES (
          '{SYSTEM_ORG_ID}', 1, '2026-01-01',
          'MANUAL', 'CONFIRMED', TRUE,
          'GENERAL', NULL, 0.1000, 'STANDARD_RATIO',
          'INCLUDED', '전사'
        );
        """
    )

    # ------------------------------------------------------------------
    # 6. target_margin_policy 템플릿 (30% / 경고 15%, SYSTEM)
    # ------------------------------------------------------------------
    op.execute(
        f"""
        INSERT INTO target_margin_policy
          (org_id, version, effective_from, source_type, reliability_status, is_active,
           target_rate, warning_lower_rate)
        VALUES (
          '{SYSTEM_ORG_ID}', 1, '2026-01-01',
          'MANUAL', 'CONFIRMED', TRUE,
          0.3000, 0.1500
        );
        """
    )

    # ------------------------------------------------------------------
    # 7. settlement_policy 템플릿 (CARRYOVER, close_day=5, SYSTEM)
    # ------------------------------------------------------------------
    op.execute(
        f"""
        INSERT INTO settlement_policy
          (org_id, version, effective_from, source_type, reliability_status, is_active,
           loss_policy, priority_distribution_json, close_day)
        VALUES (
          '{SYSTEM_ORG_ID}', 1, '2026-01-01',
          'MANUAL', 'CONFIRMED', TRUE,
          'CARRYOVER', '[]'::jsonb, 5
        );
        """
    )

    # ------------------------------------------------------------------
    # 8. marketplace_fee_policy 템플릿 (COUPANG 10.8%, SYSTEM, NEEDS_REVIEW)
    # ------------------------------------------------------------------
    op.execute(
        f"""
        INSERT INTO marketplace_fee_policy
          (org_id, version, effective_from, source_type, reliability_status, is_active,
           marketplace, fee_type, rate, memo)
        VALUES (
          '{SYSTEM_ORG_ID}', 1, '2026-01-01',
          'MANUAL', 'NEEDS_REVIEW', TRUE,
          'COUPANG', 'RATE', 0.1080,
          '카테고리 미지정 기본 — 카테고리별 정확한 율은 운영 검증 후 갱신'
        );
        """
    )


def downgrade() -> None:
    # baseline 회수: SYSTEM 템플릿과 글로벌 메타 모두 제거.
    # 운영 데이터(다른 org)는 건드리지 않음.
    op.execute(
        f"""
        DELETE FROM marketplace_fee_policy WHERE org_id = '{SYSTEM_ORG_ID}';
        DELETE FROM settlement_policy      WHERE org_id = '{SYSTEM_ORG_ID}';
        DELETE FROM target_margin_policy   WHERE org_id = '{SYSTEM_ORG_ID}';
        DELETE FROM tax_policy             WHERE org_id = '{SYSTEM_ORG_ID}';
        DELETE FROM evidence_type_policy   WHERE org_id = '{SYSTEM_ORG_ID}';
        DELETE FROM expense_category_policy WHERE org_id = '{SYSTEM_ORG_ID}';
        DELETE FROM notification_channel;
        DELETE FROM organization           WHERE id = '{SYSTEM_ORG_ID}';
        """
    )
