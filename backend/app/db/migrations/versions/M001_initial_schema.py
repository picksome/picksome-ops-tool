"""M001 initial schema — PICKSOME OPS TOOL (docs/21_erd.md).

extensions:
  pgcrypto  — gen_random_uuid() (UUID v4. v7 채택은 D-283 이지만 Postgres native 미지원으로
              앱 단 ids.py 가 UUID v7 생성, DB default 는 v4 — 박제용 fallback)
  pg_trgm   — 상품/사용자 검색 LIKE 가속

enums (25개): docs/21_erd.md §2
  business_type / vat_inclusion_type / policy_source / policy_reliability /
  settlement_status / audit_action / notification_channel_code 등.

tables (~42개): docs/21_erd.md §3-A ~ §3-K
  A. 조직/사용자  B. 정책 (11종)  C. 상품/옵션 (D-286)
  D. 사업비용/증빙 (D-284)  E. 판매주문/마진
  F. 정산 (D-285)  G. 알림 (D-281)
  H. 감사 로그  I. 외부 연동 (Post-MVP)
  J. vault (보관위치만 — CLAUDE.md Security)
  K. 세무사 export 이력 (D-282)

공통 컬럼:
  AUDIT_COLS   — created_at/by, updated_at/by, deleted_at/by (soft delete)
  POLICY_COLS  — version, effective_from/to, source_*, approved_*, reliability_status

triggers: updated_at 자동 갱신 (BEFORE UPDATE)

확정 결정(D-280~D-286, 2026-06-18) 모두 반영:
  D-280 organization.business_type, partner N명 분배
  D-281 notification_channel 메타, EMAIL/IN_APP MVP
  D-282 tax_report_export 로그 테이블
  D-283 모든 PK UUID, business_key 별도
  D-284 expenses.vat_inclusion_type + tax_policy_id snapshot
  D-285 monthly_settlement.reopen_count + reopen_limit_override
  D-286 product / option_group / option_value / product_variant 정규화
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Common SQL fragments
# ---------------------------------------------------------------------------

AUDIT_COLS = """
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by UUID NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by UUID NULL,
  deleted_at TIMESTAMPTZ NULL,
  deleted_by UUID NULL
"""

POLICY_COLS = """
  org_id UUID NOT NULL REFERENCES organization(id),
  version INT NOT NULL,
  effective_from DATE NOT NULL,
  effective_to DATE NULL,
  source_type enum_policy_source NOT NULL DEFAULT 'MANUAL',
  source_url TEXT NULL,
  source_checked_at TIMESTAMPTZ NULL,
  approved_by UUID NULL REFERENCES app_user(id),
  approved_at TIMESTAMPTZ NULL,
  reliability_status enum_policy_reliability NOT NULL DEFAULT 'CONFIRMED',
  memo TEXT NULL,
  is_active BOOLEAN NOT NULL DEFAULT TRUE
"""


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Extensions
    # ------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ------------------------------------------------------------------
    # 2. updated_at trigger function
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
          NEW.updated_at = now();
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    # ------------------------------------------------------------------
    # 3. Enums (25)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TYPE enum_user_role AS ENUM (
          'ADMIN','OPERATOR','SOURCING','ACCOUNTING','READONLY','EXTERNAL_ACCOUNTANT'
        );
        CREATE TYPE enum_margin_type AS ENUM (
          'PURCHASE_AGENCY','ROCKET_GROWTH','OVERSEAS_DIRECT','GENERAL_RESELL'
        );
        CREATE TYPE enum_product_status AS ENUM (
          'CANDIDATE','APPROVED','ACTIVE','PAUSED','DISCONTINUED'
        );
        CREATE TYPE enum_sourcing_status AS ENUM (
          'CANDIDATE','APPROVED','REJECTED'
        );
        CREATE TYPE enum_sale_channel AS ENUM (
          'COUPANG','SMARTSTORE','ELEVENST','GMARKET','AUCTION','SELF_MALL','OTHER'
        );
        CREATE TYPE enum_settlement_status AS ENUM (
          'DRAFT','CONFIRMED','LOCKED','REOPENED'
        );
        CREATE TYPE enum_evidence_status AS ENUM (
          'NONE','NEEDS_CHECK','PRESENT','SUBMITTED_TO_ACCOUNTANT'
        );
        CREATE TYPE enum_evidence_type AS ENUM (
          'TAX_INVOICE','CASH_RECEIPT','CARD_SLIP','BANK_TRANSFER',
          'SIMPLE_RECEIPT','SELF_DECLARED','OTHER'
        );
        CREATE TYPE enum_payment_method AS ENUM (
          'BUSINESS_CARD','PERSONAL_CARD','BANK_TRANSFER','CASH','OTHER'
        );
        CREATE TYPE enum_expense_category_kind AS ENUM (
          'COGS','MARKETING','LOGISTICS','OFFICE','UTILITY','LABOR',
          'TAX','FEE','SUBSCRIPTION','OTHER'
        );
        CREATE TYPE enum_marketplace_fee_type AS ENUM (
          'RATE','FIXED','TIERED'
        );
        CREATE TYPE enum_tax_type AS ENUM (
          'GENERAL','SIMPLE','EXEMPT'
        );
        CREATE TYPE enum_policy_source AS ENUM (
          'MANUAL','CRAWLED','API','IMPORTED'
        );
        CREATE TYPE enum_policy_reliability AS ENUM (
          'CONFIRMED','AUTO_DETECTED','NEEDS_REVIEW','EXPIRED','MANUAL_OVERRIDE','DEPRECATED'
        );
        CREATE TYPE enum_audit_action AS ENUM (
          'CREATE','UPDATE','DELETE','RESTORE','STATUS_CHANGE','POLICY_OVERRIDE',
          'LOGIN','LOGOUT'
        );
        CREATE TYPE enum_notification_kind AS ENUM (
          'SETTLEMENT_DEADLINE','VAT_DEADLINE','MARGIN_DROP','STOCKOUT_RISK',
          'POLICY_REVIEW','ACCOUNTANT_COMMENT','SYSTEM'
        );
        CREATE TYPE enum_carryover_event AS ENUM (
          'LOSS_ADDED','OFFSET_APPLIED','MANUAL_ADJUST'
        );
        CREATE TYPE enum_distribution_kind AS ENUM (
          'PRIORITY','RATIO'
        );
        CREATE TYPE enum_business_type AS ENUM (
          'SOLE_PROPRIETOR','JOINT_BUSINESS','CORPORATION','TEAM_OPERATION'
        );
        CREATE TYPE enum_notification_channel_code AS ENUM (
          'EMAIL','IN_APP','KAKAO','SLACK','TELEGRAM','WEBHOOK'
        );
        CREATE TYPE enum_vat_inclusion_type AS ENUM (
          'INCLUDED','EXCLUDED','EXEMPT'
        );
        CREATE TYPE enum_tax_calc_method AS ENUM (
          'STANDARD_RATIO','SIMPLE_DEEMED','EXEMPT','OVERSEAS_ZERO_RATED'
        );
        """
    )

    # ------------------------------------------------------------------
    # 4. Section 3-A — organization / app_user / partner
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE organization (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          business_name TEXT NOT NULL,
          business_no_masked TEXT NULL,
          business_type enum_business_type NOT NULL DEFAULT 'JOINT_BUSINESS',
          address TEXT NULL,
          primary_currency TEXT NOT NULL DEFAULT 'KRW',
          timezone TEXT NOT NULL DEFAULT 'Asia/Seoul',
          settlement_reopen_limit INT NOT NULL DEFAULT 3,
          settings_json JSONB NOT NULL DEFAULT '{{}}',
          {AUDIT_COLS},
          CONSTRAINT ck_organization_reopen_limit CHECK (settlement_reopen_limit >= 0)
        );

        CREATE TABLE app_user (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          email TEXT NOT NULL,
          password_hash TEXT NOT NULL,
          display_name TEXT NOT NULL,
          role enum_user_role NOT NULL,
          is_active BOOLEAN NOT NULL DEFAULT TRUE,
          last_login_at TIMESTAMPTZ NULL,
          invited_by UUID NULL REFERENCES app_user(id),
          invitation_token TEXT NULL,
          invitation_expires_at TIMESTAMPTZ NULL,
          {AUDIT_COLS},
          CONSTRAINT ck_app_user_password_not_plain CHECK (password_hash !~ '^plain:')
        );
        CREATE UNIQUE INDEX uq_app_user_org_email
          ON app_user(org_id, email) WHERE deleted_at IS NULL;
        CREATE UNIQUE INDEX uq_app_user_invitation_token
          ON app_user(invitation_token) WHERE invitation_token IS NOT NULL;
        CREATE INDEX ix_app_user_org_role_active ON app_user(org_id, role, is_active);

        CREATE TABLE partner (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          name TEXT NOT NULL,
          display_order INT NOT NULL DEFAULT 0,
          linked_user_id UUID NULL REFERENCES app_user(id),
          is_active BOOLEAN NOT NULL DEFAULT TRUE,
          {AUDIT_COLS}
        );
        CREATE UNIQUE INDEX uq_partner_org_name
          ON partner(org_id, name) WHERE deleted_at IS NULL;
        """
    )

    # Now we can safely add the FKs from audit columns (created_by / updated_by / deleted_by)
    # to app_user.id for ALL tables (organization/app_user/partner included).
    # We add these FKs at the end of M001 (section 99) once every table exists.

    # ------------------------------------------------------------------
    # 5. Section 3-B — Policy tables (12)
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE marketplace_fee_policy (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          {POLICY_COLS},
          marketplace enum_sale_channel NOT NULL,
          category_path TEXT NULL,
          fee_type enum_marketplace_fee_type NOT NULL,
          rate NUMERIC(7,4) NULL,
          fixed_amount BIGINT NULL,
          tier_json JSONB NULL,
          {AUDIT_COLS},
          CONSTRAINT ck_mfp_fee_value CHECK (
            (fee_type = 'RATE'   AND rate IS NOT NULL)
         OR (fee_type = 'FIXED'  AND fixed_amount IS NOT NULL)
         OR (fee_type = 'TIERED' AND tier_json IS NOT NULL)
          )
        );
        CREATE UNIQUE INDEX uq_mfp_org_mkt_cat_ver
          ON marketplace_fee_policy(org_id, marketplace, category_path, version)
          NULLS NOT DISTINCT
          WHERE deleted_at IS NULL;
        CREATE INDEX ix_mfp_org_mkt_eff
          ON marketplace_fee_policy(org_id, marketplace, effective_from DESC);

        CREATE TABLE payment_fee_policy (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          {POLICY_COLS},
          method enum_payment_method NOT NULL,
          rate NUMERIC(7,4) NOT NULL,
          {AUDIT_COLS}
        );
        CREATE UNIQUE INDEX uq_pfp_org_method_ver
          ON payment_fee_policy(org_id, method, version) WHERE deleted_at IS NULL;

        CREATE TABLE tax_policy (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          {POLICY_COLS},
          tax_type enum_tax_type NOT NULL,
          business_type enum_business_type NULL,
          vat_rate NUMERIC(7,4) NOT NULL DEFAULT 0.1000,
          calculation_method enum_tax_calc_method NOT NULL DEFAULT 'STANDARD_RATIO',
          default_vat_inclusion enum_vat_inclusion_type NOT NULL DEFAULT 'INCLUDED',
          applies_to TEXT NOT NULL,
          {AUDIT_COLS}
        );
        CREATE UNIQUE INDEX uq_tax_policy_unique
          ON tax_policy(org_id, tax_type, business_type, applies_to, version)
          NULLS NOT DISTINCT
          WHERE deleted_at IS NULL;
        CREATE INDEX ix_tax_policy_org_eff
          ON tax_policy(org_id, effective_from DESC) WHERE effective_to IS NULL;

        CREATE TABLE fulfillment_fee_policy (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          {POLICY_COLS},
          provider TEXT NOT NULL,
          inbound_fee BIGINT NULL,
          outbound_fee BIGINT NULL,
          storage_fee_per_day BIGINT NULL,
          return_handling_fee BIGINT NULL,
          extra_json JSONB NULL,
          {AUDIT_COLS}
        );
        CREATE UNIQUE INDEX uq_ffp_org_provider_ver
          ON fulfillment_fee_policy(org_id, provider, version) WHERE deleted_at IS NULL;

        CREATE TABLE return_risk_policy (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          {POLICY_COLS},
          marketplace enum_sale_channel NULL,
          category_path TEXT NULL,
          return_rate NUMERIC(7,4) NOT NULL,
          reason_json JSONB NULL,
          {AUDIT_COLS}
        );

        CREATE TABLE ad_cost_policy (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          {POLICY_COLS},
          marketplace enum_sale_channel NOT NULL,
          category_path TEXT NULL,
          target_roas NUMERIC(7,4) NULL,
          default_ad_ratio NUMERIC(7,4) NULL,
          {AUDIT_COLS}
        );

        CREATE TABLE target_margin_policy (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          {POLICY_COLS},
          category_path TEXT NULL,
          margin_type enum_margin_type NULL,
          target_rate NUMERIC(7,4) NOT NULL,
          warning_lower_rate NUMERIC(7,4) NULL,
          {AUDIT_COLS}
        );

        CREATE TABLE settlement_policy (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          {POLICY_COLS},
          loss_policy TEXT NOT NULL DEFAULT 'CARRYOVER',
          priority_distribution_json JSONB NULL,
          close_day INT NOT NULL DEFAULT 5,
          {AUDIT_COLS}
        );

        CREATE TABLE partner_share_policy (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          {POLICY_COLS},
          partner_id UUID NOT NULL REFERENCES partner(id),
          share_ratio NUMERIC(7,4) NOT NULL,
          applies_year_month TEXT NULL,
          {AUDIT_COLS}
        );
        CREATE INDEX ix_psp_org_eff ON partner_share_policy(org_id, effective_from DESC);

        CREATE TABLE expense_category_policy (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          {POLICY_COLS},
          name TEXT NOT NULL,
          kind enum_expense_category_kind NOT NULL,
          default_tax_deductible BOOLEAN NOT NULL DEFAULT TRUE,
          default_settlement_deductible BOOLEAN NOT NULL DEFAULT TRUE,
          sort_order INT NOT NULL DEFAULT 0,
          {AUDIT_COLS}
        );
        CREATE UNIQUE INDEX uq_ecp_org_name_ver
          ON expense_category_policy(org_id, name, version) WHERE deleted_at IS NULL;

        CREATE TABLE evidence_type_policy (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          {POLICY_COLS},
          code enum_evidence_type NOT NULL,
          label TEXT NOT NULL,
          is_official BOOLEAN NOT NULL DEFAULT TRUE,
          description TEXT NULL,
          {AUDIT_COLS}
        );
        CREATE UNIQUE INDEX uq_etp_org_code_ver
          ON evidence_type_policy(org_id, code, version) WHERE deleted_at IS NULL;

        CREATE TABLE policy_change_history (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          policy_table TEXT NOT NULL,
          policy_row_id UUID NOT NULL,
          version INT NOT NULL,
          change_action enum_audit_action NOT NULL,
          before_json JSONB NULL,
          after_json JSONB NULL,
          reason TEXT NULL,
          changed_by UUID NOT NULL REFERENCES app_user(id),
          changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          {AUDIT_COLS}
        );
        CREATE INDEX ix_pch_org_table_time
          ON policy_change_history(org_id, policy_table, changed_at DESC);
        """
    )

    # ------------------------------------------------------------------
    # 6. Section 3-C — policy_snapshot trio
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE policy_snapshot (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          owner_kind TEXT NOT NULL,
          owner_id UUID NOT NULL,
          computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          {AUDIT_COLS}
        );
        CREATE INDEX ix_ps_owner ON policy_snapshot(owner_kind, owner_id);
        CREATE INDEX ix_ps_org_time ON policy_snapshot(org_id, computed_at DESC);

        CREATE TABLE policy_snapshot_ref (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          snapshot_id UUID NOT NULL REFERENCES policy_snapshot(id) ON DELETE CASCADE,
          policy_table TEXT NOT NULL,
          policy_row_id UUID NOT NULL,
          version INT NOT NULL,
          {AUDIT_COLS}
        );
        CREATE INDEX ix_psr_snapshot ON policy_snapshot_ref(snapshot_id);
        CREATE INDEX ix_psr_row ON policy_snapshot_ref(policy_table, policy_row_id);

        CREATE TABLE policy_snapshot_value (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          snapshot_id UUID NOT NULL UNIQUE REFERENCES policy_snapshot(id) ON DELETE CASCADE,
          values_json JSONB NOT NULL,
          {AUDIT_COLS}
        );
        """
    )

    # ------------------------------------------------------------------
    # 7. Section 3-D — product + options (D-286) + sourcing + external_partner
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE password_vault_entry (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          service_name TEXT NOT NULL,
          vault_provider TEXT NOT NULL,
          vault_item_name TEXT NOT NULL,
          account_masked TEXT NULL,
          memo TEXT NULL,
          {AUDIT_COLS},
          CONSTRAINT ck_pve_memo_no_pw1
            CHECK (memo IS NULL OR memo !~* '비밀번호[ :=]*[A-Za-z0-9!@#$]+'),
          CONSTRAINT ck_pve_memo_no_pw2
            CHECK (memo IS NULL OR memo !~* '(pw|pass|pwd)[ :=]+[A-Za-z0-9!@#$]+')
        );
        CREATE INDEX ix_pve_org_service ON password_vault_entry(org_id, service_name);

        CREATE TABLE external_partner (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          name TEXT NOT NULL,
          kind TEXT NOT NULL,
          contact_masked TEXT NULL,
          vault_entry_id UUID NULL REFERENCES password_vault_entry(id),
          memo TEXT NULL,
          {AUDIT_COLS}
        );

        CREATE TABLE product (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          business_key TEXT NOT NULL,
          sku TEXT NULL,
          name_ko TEXT NOT NULL,
          name_en TEXT NULL,
          category_path TEXT NULL,
          margin_type enum_margin_type NOT NULL,
          status enum_product_status NOT NULL DEFAULT 'CANDIDATE',
          supplier_id UUID NULL REFERENCES external_partner(id),
          supply_url TEXT NULL,
          cost_amount BIGINT NULL,
          cost_currency TEXT NULL,
          cost_fx_rate NUMERIC(12,4) NULL,
          has_variants BOOLEAN NOT NULL DEFAULT FALSE,
          memo TEXT NULL,
          {AUDIT_COLS}
        );
        CREATE UNIQUE INDEX uq_product_org_business_key
          ON product(org_id, business_key) WHERE deleted_at IS NULL;
        CREATE INDEX ix_product_org_status ON product(org_id, status);
        CREATE INDEX ix_product_org_margin_type ON product(org_id, margin_type);

        CREATE TABLE product_option_group (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          product_id UUID NOT NULL REFERENCES product(id),
          name TEXT NOT NULL,
          sort_order INT NOT NULL DEFAULT 0,
          {AUDIT_COLS}
        );
        CREATE UNIQUE INDEX uq_pog_product_name
          ON product_option_group(product_id, name) WHERE deleted_at IS NULL;
        CREATE INDEX ix_pog_product_sort ON product_option_group(product_id, sort_order);

        CREATE TABLE product_option_value (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          option_group_id UUID NOT NULL REFERENCES product_option_group(id),
          value TEXT NOT NULL,
          sort_order INT NOT NULL DEFAULT 0,
          {AUDIT_COLS}
        );
        CREATE UNIQUE INDEX uq_pov_group_value
          ON product_option_value(option_group_id, value) WHERE deleted_at IS NULL;
        CREATE INDEX ix_pov_group_sort ON product_option_value(option_group_id, sort_order);

        CREATE TABLE product_variant (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          product_id UUID NOT NULL REFERENCES product(id),
          variant_key TEXT NOT NULL,
          option_values_json JSONB NOT NULL,
          sku_external TEXT NULL,
          cost_amount BIGINT NULL,
          sale_price BIGINT NULL,
          extra_price BIGINT NOT NULL DEFAULT 0,
          stock_quantity INT NULL,
          is_active BOOLEAN NOT NULL DEFAULT TRUE,
          {AUDIT_COLS}
        );
        CREATE UNIQUE INDEX uq_variant_product_key
          ON product_variant(product_id, variant_key) WHERE deleted_at IS NULL;
        CREATE INDEX ix_variant_org_product_active
          ON product_variant(org_id, product_id, is_active);

        CREATE TABLE product_sourcing_candidate (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          product_id UUID NULL REFERENCES product(id),
          supplier_url TEXT NOT NULL,
          supplier_name_snapshot TEXT NULL,
          supply_price BIGINT NULL,
          moq INT NULL,
          category_path TEXT NULL,
          candidate_status enum_sourcing_status NOT NULL DEFAULT 'CANDIDATE',
          rejected_reason TEXT NULL,
          simulated_margin_json JSONB NULL,
          memo TEXT NULL,
          {AUDIT_COLS}
        );
        CREATE INDEX ix_psc_org_status ON product_sourcing_candidate(org_id, candidate_status);
        """
    )

    # ------------------------------------------------------------------
    # 8. Section 3-E — margin
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE product_margin_expected (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          product_id UUID NOT NULL REFERENCES product(id),
          product_variant_id UUID NULL REFERENCES product_variant(id),
          margin_type enum_margin_type NOT NULL,
          marketplace enum_sale_channel NOT NULL,
          input_cost BIGINT NOT NULL,
          input_sale_price BIGINT NOT NULL,
          input_extra_json JSONB NULL,
          computed_marketplace_fee BIGINT NOT NULL,
          computed_payment_fee BIGINT NOT NULL,
          computed_ad_cost BIGINT NOT NULL,
          computed_fulfillment_fee BIGINT NOT NULL,
          computed_return_risk_cost BIGINT NOT NULL,
          computed_vat BIGINT NOT NULL,
          computed_margin_amount BIGINT NOT NULL,
          computed_margin_rate NUMERIC(7,4) NOT NULL,
          snapshot_id UUID NOT NULL REFERENCES policy_snapshot(id),
          override_fields_json JSONB NULL,
          valid_from DATE NOT NULL,
          valid_to DATE NULL,
          {AUDIT_COLS}
        );
        CREATE INDEX ix_pme_org_product_from
          ON product_margin_expected(org_id, product_id, valid_from DESC);
        CREATE INDEX ix_pme_snapshot ON product_margin_expected(snapshot_id);
        CREATE INDEX ix_pme_variant ON product_margin_expected(product_variant_id);
        """
    )

    # ------------------------------------------------------------------
    # 9. Section 3-F — sales_order / marketplace_settlement_record / batch slot
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE batch_run (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          job_id UUID NULL,
          started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          finished_at TIMESTAMPTZ NULL,
          status TEXT NOT NULL DEFAULT 'RUNNING',
          affected_rows INT NOT NULL DEFAULT 0,
          summary_json JSONB NULL,
          {AUDIT_COLS}
        );

        CREATE TABLE sales_order (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          business_key TEXT NOT NULL,
          sale_date DATE NOT NULL,
          marketplace enum_sale_channel NOT NULL,
          marketplace_order_no TEXT NOT NULL,
          product_id UUID NULL REFERENCES product(id),
          product_variant_id UUID NULL REFERENCES product_variant(id),
          product_snapshot_name TEXT NULL,
          quantity INT NOT NULL,
          sale_price BIGINT NOT NULL,
          ad_cost BIGINT NOT NULL DEFAULT 0,
          shipping_fee BIGINT NOT NULL DEFAULT 0,
          vat_inclusion_type enum_vat_inclusion_type NOT NULL DEFAULT 'INCLUDED',
          tax_policy_id UUID NULL REFERENCES tax_policy(id),
          memo TEXT NULL,
          import_source TEXT NOT NULL DEFAULT 'MANUAL',
          import_batch_id UUID NULL REFERENCES batch_run(id),
          {AUDIT_COLS},
          CONSTRAINT ck_sales_order_qty CHECK (quantity > 0),
          CONSTRAINT ck_sales_order_price CHECK (sale_price >= 0)
        );
        CREATE UNIQUE INDEX uq_sales_order_org_business_key
          ON sales_order(org_id, business_key) WHERE deleted_at IS NULL;
        CREATE UNIQUE INDEX uq_sales_order_mkt_orderno
          ON sales_order(org_id, marketplace, marketplace_order_no) WHERE deleted_at IS NULL;
        CREATE INDEX ix_so_org_sale_date ON sales_order(org_id, sale_date);
        CREATE INDEX ix_so_org_mkt_date ON sales_order(org_id, marketplace, sale_date);
        CREATE INDEX ix_so_org_product_date ON sales_order(org_id, product_id, sale_date);

        CREATE TABLE marketplace_settlement_record (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          marketplace enum_sale_channel NOT NULL,
          settlement_period_start DATE NOT NULL,
          settlement_period_end DATE NOT NULL,
          marketplace_order_no TEXT NULL,
          settled_at DATE NULL,
          gross_revenue BIGINT NOT NULL,
          marketplace_fee BIGINT NOT NULL DEFAULT 0,
          ad_cost BIGINT NOT NULL DEFAULT 0,
          return_cost BIGINT NOT NULL DEFAULT 0,
          fulfillment_fee BIGINT NOT NULL DEFAULT 0,
          other_deductions BIGINT NOT NULL DEFAULT 0,
          net_settlement BIGINT NOT NULL,
          raw_json JSONB NULL,
          matched_sales_order_id UUID NULL REFERENCES sales_order(id),
          import_batch_id UUID NULL REFERENCES batch_run(id),
          import_file_hash TEXT NOT NULL,
          {AUDIT_COLS}
        );
        CREATE UNIQUE INDEX uq_msr_unique
          ON marketplace_settlement_record(
            org_id, marketplace, COALESCE(marketplace_order_no, ''), settlement_period_start
          ) WHERE deleted_at IS NULL;
        CREATE INDEX ix_msr_org_settled ON marketplace_settlement_record(org_id, settled_at);
        CREATE INDEX ix_msr_matched_order ON marketplace_settlement_record(matched_sales_order_id);
        CREATE INDEX ix_msr_file_hash ON marketplace_settlement_record(import_file_hash);
        """
    )

    # product_margin_actual references sales_order + msr — create after both exist
    op.execute(
        f"""
        CREATE TABLE product_margin_actual (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          product_id UUID NOT NULL REFERENCES product(id),
          product_variant_id UUID NULL REFERENCES product_variant(id),
          sales_order_id UUID NOT NULL UNIQUE REFERENCES sales_order(id),
          settlement_record_id UUID NULL REFERENCES marketplace_settlement_record(id),
          actual_revenue BIGINT NOT NULL,
          actual_cost BIGINT NOT NULL,
          actual_marketplace_fee BIGINT NOT NULL,
          actual_payment_fee BIGINT NOT NULL,
          actual_ad_cost BIGINT NOT NULL,
          actual_fulfillment_fee BIGINT NOT NULL,
          actual_return_cost BIGINT NOT NULL,
          actual_vat BIGINT NOT NULL,
          actual_margin_amount BIGINT NOT NULL,
          actual_margin_rate NUMERIC(7,4) NOT NULL,
          matched_expected_id UUID NULL REFERENCES product_margin_expected(id),
          delta_margin_amount BIGINT NULL,
          delta_margin_rate NUMERIC(7,4) NULL,
          {AUDIT_COLS}
        );
        CREATE INDEX ix_pma_org_product ON product_margin_actual(org_id, product_id);
        CREATE INDEX ix_pma_sales_order ON product_margin_actual(sales_order_id);
        CREATE INDEX ix_pma_matched ON product_margin_actual(matched_expected_id);
        """
    )

    # ------------------------------------------------------------------
    # 10. Section 3-H — settlement (need monthly_settlement before expenses.linked_settlement_id)
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE monthly_settlement (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          business_key TEXT NOT NULL,
          year_month CHAR(7) NOT NULL,
          status enum_settlement_status NOT NULL DEFAULT 'DRAFT',
          total_revenue BIGINT NOT NULL DEFAULT 0,
          total_direct_cost BIGINT NOT NULL DEFAULT 0,
          total_common_expense BIGINT NOT NULL DEFAULT 0,
          net_profit BIGINT NOT NULL DEFAULT 0,
          carryover_offset BIGINT NOT NULL DEFAULT 0,
          distributable_amount BIGINT NOT NULL DEFAULT 0,
          priority_distribution_total BIGINT NOT NULL DEFAULT 0,
          ratio_distribution_total BIGINT NOT NULL DEFAULT 0,
          reopen_count INT NOT NULL DEFAULT 0,
          reopen_limit_override INT NULL,
          last_reopened_at TIMESTAMPTZ NULL,
          last_reopened_by UUID NULL REFERENCES app_user(id),
          last_reopen_reason TEXT NULL,
          confirmed_at TIMESTAMPTZ NULL,
          confirmed_by UUID NULL REFERENCES app_user(id),
          locked_at TIMESTAMPTZ NULL,
          locked_by UUID NULL REFERENCES app_user(id),
          snapshot_id UUID NULL REFERENCES policy_snapshot(id),
          memo TEXT NULL,
          {AUDIT_COLS},
          CONSTRAINT ck_ms_year_month CHECK (year_month ~ '^[0-9]{{4}}-[0-9]{{2}}$'),
          CONSTRAINT ck_ms_reopen_override CHECK (
            reopen_limit_override IS NULL OR reopen_limit_override >= 0
          ),
          CONSTRAINT ck_ms_confirmed_meta CHECK (
            status NOT IN ('CONFIRMED','LOCKED')
            OR (confirmed_at IS NOT NULL AND confirmed_by IS NOT NULL)
          )
        );
        CREATE UNIQUE INDEX uq_ms_org_ym
          ON monthly_settlement(org_id, year_month) WHERE deleted_at IS NULL;
        CREATE UNIQUE INDEX uq_ms_org_business_key
          ON monthly_settlement(org_id, business_key) WHERE deleted_at IS NULL;
        CREATE INDEX ix_ms_org_status_ym
          ON monthly_settlement(org_id, status, year_month DESC);

        CREATE TABLE partner_settlement (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          monthly_settlement_id UUID NOT NULL REFERENCES monthly_settlement(id),
          partner_id UUID NOT NULL REFERENCES partner(id),
          distribution_kind enum_distribution_kind NOT NULL,
          amount BIGINT NOT NULL,
          ratio_used NUMERIC(7,4) NULL,
          note TEXT NULL,
          {AUDIT_COLS},
          CONSTRAINT uq_part_settle UNIQUE (monthly_settlement_id, partner_id, distribution_kind)
        );
        CREATE INDEX ix_part_settle_partner ON partner_settlement(partner_id);

        CREATE TABLE loss_carryover_ledger (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          event_at DATE NOT NULL,
          event_kind enum_carryover_event NOT NULL,
          source_settlement_id UUID NULL REFERENCES monthly_settlement(id),
          amount BIGINT NOT NULL,
          balance_after BIGINT NOT NULL,
          reason TEXT NULL,
          {AUDIT_COLS}
        );
        CREATE INDEX ix_lcl_org_event ON loss_carryover_ledger(org_id, event_at DESC);

        CREATE TABLE settlement_reopen_audit (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          monthly_settlement_id UUID NOT NULL REFERENCES monthly_settlement(id),
          reopened_by UUID NOT NULL REFERENCES app_user(id),
          reopened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          reason TEXT NOT NULL,
          previous_status enum_settlement_status NOT NULL,
          reopen_count_after INT NOT NULL,
          snapshot_before_json JSONB NULL,
          {AUDIT_COLS}
        );
        CREATE INDEX ix_sra_ms_time
          ON settlement_reopen_audit(monthly_settlement_id, reopened_at DESC);
        """
    )

    # ------------------------------------------------------------------
    # 11. Section 3-G — expenses + attachment + comment
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE expenses (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          expense_date DATE NOT NULL,
          category_id UUID NOT NULL REFERENCES expense_category_policy(id),
          description TEXT NOT NULL,
          gross_amount BIGINT NOT NULL,
          supply_amount BIGINT NOT NULL,
          vat_amount BIGINT NOT NULL DEFAULT 0,
          vat_inclusion_type enum_vat_inclusion_type NOT NULL DEFAULT 'INCLUDED',
          tax_type enum_tax_type NOT NULL DEFAULT 'GENERAL',
          tax_policy_id UUID NULL REFERENCES tax_policy(id),
          is_manual_tax_override BOOLEAN NOT NULL DEFAULT FALSE,
          payment_method enum_payment_method NOT NULL,
          payment_card_holder_id UUID NULL REFERENCES partner(id),
          is_personal_card BOOLEAN NOT NULL DEFAULT FALSE,
          is_business_use BOOLEAN NOT NULL DEFAULT TRUE,
          evidence_type enum_evidence_type NOT NULL DEFAULT 'OTHER',
          evidence_status enum_evidence_status NOT NULL DEFAULT 'NONE',
          is_tax_deductible BOOLEAN NOT NULL DEFAULT TRUE,
          is_settlement_deductible BOOLEAN NOT NULL DEFAULT TRUE,
          attributable_partner_id UUID NULL REFERENCES partner(id),
          supplier_id UUID NULL REFERENCES external_partner(id),
          memo TEXT NULL,
          linked_settlement_id UUID NULL REFERENCES monthly_settlement(id),
          {AUDIT_COLS},
          CONSTRAINT ck_expenses_personal_card CHECK (
            is_personal_card = FALSE OR payment_card_holder_id IS NOT NULL
          ),
          CONSTRAINT ck_expenses_total CHECK (gross_amount = supply_amount + vat_amount),
          CONSTRAINT ck_expenses_exempt_vat CHECK (
            vat_inclusion_type <> 'EXEMPT' OR vat_amount = 0
          )
        );
        CREATE INDEX ix_expenses_org_date ON expenses(org_id, expense_date);
        CREATE INDEX ix_expenses_org_cat_date ON expenses(org_id, category_id, expense_date);
        CREATE INDEX ix_expenses_org_evidence ON expenses(org_id, evidence_status);
        CREATE INDEX ix_expenses_org_linked ON expenses(org_id, linked_settlement_id);
        CREATE INDEX ix_expenses_org_settle_deduct
          ON expenses(org_id, expense_date)
          WHERE is_settlement_deductible = TRUE AND is_business_use = TRUE;

        CREATE TABLE expense_attachment (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          expense_id UUID NOT NULL REFERENCES expenses(id),
          storage_path TEXT NULL,
          original_filename TEXT NULL,
          file_hash TEXT NULL,
          mime_type TEXT NULL,
          uploaded_by UUID NULL REFERENCES app_user(id),
          uploaded_at TIMESTAMPTZ NULL,
          {AUDIT_COLS}
        );

        CREATE TABLE expense_comment (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          expense_id UUID NOT NULL REFERENCES expenses(id),
          author_id UUID NOT NULL REFERENCES app_user(id),
          body TEXT NOT NULL,
          posted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          {AUDIT_COLS}
        );
        CREATE INDEX ix_expense_comment_eid_time
          ON expense_comment(expense_id, posted_at DESC);
        """
    )

    # ------------------------------------------------------------------
    # 12. Section 3-I — tax_report_bundle
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE tax_report_bundle (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          period_kind TEXT NOT NULL,
          period_label TEXT NOT NULL,
          sent_to_user_id UUID NULL REFERENCES app_user(id),
          sent_at TIMESTAMPTZ NULL,
          received_at TIMESTAMPTZ NULL,
          storage_path TEXT NULL,
          memo TEXT NULL,
          {AUDIT_COLS}
        );

        CREATE TABLE tax_report_bundle_item (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          bundle_id UUID NOT NULL REFERENCES tax_report_bundle(id),
          report_kind TEXT NOT NULL,
          file_name TEXT NOT NULL,
          file_hash TEXT NULL,
          generated_at TIMESTAMPTZ NOT NULL,
          {AUDIT_COLS}
        );
        """
    )

    # ------------------------------------------------------------------
    # 13. Section 3-J — audit_log / notification_channel meta + notification / vault already created
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE audit_log (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          domain_table TEXT NOT NULL,
          domain_row_id UUID NOT NULL,
          action enum_audit_action NOT NULL,
          actor_user_id UUID NULL REFERENCES app_user(id),
          occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          before_json JSONB NULL,
          after_json JSONB NULL,
          reason TEXT NULL,
          request_id TEXT NULL,
          {AUDIT_COLS}
        );
        CREATE INDEX ix_audit_org_table_row_time
          ON audit_log(org_id, domain_table, domain_row_id, occurred_at DESC);
        CREATE INDEX ix_audit_org_time ON audit_log(org_id, occurred_at DESC);

        CREATE TABLE notification_channel (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          code enum_notification_channel_code NOT NULL UNIQUE,
          display_name TEXT NOT NULL,
          adapter_class TEXT NOT NULL,
          is_mvp_active BOOLEAN NOT NULL DEFAULT FALSE,
          requires_config_json JSONB NOT NULL DEFAULT '[]',
          sort_order INT NOT NULL DEFAULT 0,
          {AUDIT_COLS}
        );

        CREATE TABLE notification (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          recipient_user_id UUID NOT NULL REFERENCES app_user(id),
          kind enum_notification_kind NOT NULL,
          title TEXT NOT NULL,
          body TEXT NULL,
          channel_code enum_notification_channel_code NOT NULL DEFAULT 'IN_APP',
          target_url TEXT NULL,
          sent_at TIMESTAMPTZ NULL,
          delivery_status TEXT NOT NULL DEFAULT 'PENDING',
          read_at TIMESTAMPTZ NULL,
          error_message TEXT NULL,
          meta_json JSONB NULL,
          {AUDIT_COLS}
        );
        CREATE INDEX ix_notification_recipient_time
          ON notification(recipient_user_id, sent_at DESC);
        CREATE INDEX ix_notification_org_kind_time
          ON notification(org_id, kind, sent_at DESC);
        CREATE INDEX ix_notification_unread
          ON notification(recipient_user_id, read_at) WHERE read_at IS NULL;

        CREATE TABLE notification_pref (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id UUID NOT NULL UNIQUE REFERENCES app_user(id),
          kind_settings_json JSONB NOT NULL,
          channel_settings_json JSONB NOT NULL,
          {AUDIT_COLS}
        );
        """
    )

    # ------------------------------------------------------------------
    # 14. Section 3-K — external integration slots
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE integration_account (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          provider TEXT NOT NULL,
          account_label TEXT NOT NULL,
          credentials_vault_entry_id UUID NOT NULL REFERENCES password_vault_entry(id),
          status TEXT NOT NULL DEFAULT 'INACTIVE',
          last_synced_at TIMESTAMPTZ NULL,
          {AUDIT_COLS}
        );

        CREATE TABLE integration_job (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          account_id UUID NOT NULL REFERENCES integration_account(id),
          job_kind TEXT NOT NULL,
          schedule_cron TEXT NULL,
          is_enabled BOOLEAN NOT NULL DEFAULT FALSE,
          {AUDIT_COLS}
        );

        ALTER TABLE batch_run
          ADD CONSTRAINT fk_batch_run_job_id_integration_job
          FOREIGN KEY (job_id) REFERENCES integration_job(id);

        CREATE TABLE batch_run_log (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          batch_run_id UUID NOT NULL REFERENCES batch_run(id),
          level TEXT NOT NULL,
          message TEXT NOT NULL,
          occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          {AUDIT_COLS}
        );

        CREATE TABLE external_product_mapping (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          account_id UUID NOT NULL REFERENCES integration_account(id),
          product_id UUID NOT NULL REFERENCES product(id),
          external_product_id TEXT NOT NULL,
          last_synced_at TIMESTAMPTZ NULL,
          {AUDIT_COLS},
          CONSTRAINT uq_epm_account_external UNIQUE (account_id, external_product_id)
        );

        CREATE TABLE supplier_price_snapshot (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          supplier_id UUID NOT NULL REFERENCES external_partner(id),
          external_product_id TEXT NOT NULL,
          price BIGINT NOT NULL,
          currency TEXT NOT NULL,
          observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          {AUDIT_COLS}
        );

        CREATE TABLE marketplace_order_import (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          batch_run_id UUID NULL REFERENCES batch_run(id),
          raw_json JSONB NOT NULL,
          imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          {AUDIT_COLS}
        );

        CREATE TABLE marketplace_settlement_import (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          batch_run_id UUID NULL REFERENCES batch_run(id),
          raw_json JSONB NOT NULL,
          imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          {AUDIT_COLS}
        );
        """
    )

    # ------------------------------------------------------------------
    # 99. updated_at triggers on every table
    # ------------------------------------------------------------------
    tables_with_updated_at = [
        # core
        "organization", "app_user", "partner",
        # policies
        "marketplace_fee_policy", "payment_fee_policy", "tax_policy",
        "fulfillment_fee_policy", "return_risk_policy", "ad_cost_policy",
        "target_margin_policy", "settlement_policy", "partner_share_policy",
        "expense_category_policy", "evidence_type_policy", "policy_change_history",
        # snapshot
        "policy_snapshot", "policy_snapshot_ref", "policy_snapshot_value",
        # vault / external partner / product
        "password_vault_entry", "external_partner", "product",
        "product_option_group", "product_option_value", "product_variant",
        "product_sourcing_candidate",
        # margin / sales
        "product_margin_expected", "product_margin_actual",
        "batch_run", "sales_order", "marketplace_settlement_record",
        # settlement
        "monthly_settlement", "partner_settlement", "loss_carryover_ledger",
        "settlement_reopen_audit",
        # expenses / tax bundle
        "expenses", "expense_attachment", "expense_comment",
        "tax_report_bundle", "tax_report_bundle_item",
        # cross
        "audit_log", "notification_channel", "notification", "notification_pref",
        # integration slots
        "integration_account", "integration_job", "batch_run_log",
        "external_product_mapping", "supplier_price_snapshot",
        "marketplace_order_import", "marketplace_settlement_import",
    ]
    for tbl in tables_with_updated_at:
        op.execute(
            f"""
            CREATE TRIGGER trg_{tbl}_updated_at
              BEFORE UPDATE ON {tbl}
              FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            """
        )


def downgrade() -> None:
    # Drop in reverse dependency order. Cascading dropall is acceptable for the
    # initial migration since downgrade rolls back to empty schema.
    tables_reverse = [
        "marketplace_settlement_import", "marketplace_order_import",
        "supplier_price_snapshot", "external_product_mapping",
        "batch_run_log", "integration_job", "integration_account",
        "notification_pref", "notification", "notification_channel",
        "audit_log",
        "tax_report_bundle_item", "tax_report_bundle",
        "expense_comment", "expense_attachment", "expenses",
        "settlement_reopen_audit", "loss_carryover_ledger",
        "partner_settlement", "monthly_settlement",
        "product_margin_actual",
        "marketplace_settlement_record", "sales_order", "batch_run",
        "product_margin_expected",
        "product_sourcing_candidate", "product_variant",
        "product_option_value", "product_option_group", "product",
        "external_partner", "password_vault_entry",
        "policy_snapshot_value", "policy_snapshot_ref", "policy_snapshot",
        "policy_change_history", "evidence_type_policy",
        "expense_category_policy", "partner_share_policy", "settlement_policy",
        "target_margin_policy", "ad_cost_policy", "return_risk_policy",
        "fulfillment_fee_policy", "tax_policy", "payment_fee_policy",
        "marketplace_fee_policy",
        "partner", "app_user", "organization",
    ]
    for tbl in tables_reverse:
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")

    enums_reverse = [
        "enum_tax_calc_method", "enum_vat_inclusion_type",
        "enum_notification_channel_code", "enum_business_type",
        "enum_distribution_kind", "enum_carryover_event",
        "enum_notification_kind", "enum_audit_action",
        "enum_policy_reliability", "enum_policy_source",
        "enum_tax_type", "enum_marketplace_fee_type",
        "enum_expense_category_kind", "enum_payment_method",
        "enum_evidence_type", "enum_evidence_status",
        "enum_settlement_status", "enum_sale_channel",
        "enum_sourcing_status", "enum_product_status",
        "enum_margin_type", "enum_user_role",
    ]
    for en in enums_reverse:
        op.execute(f"DROP TYPE IF EXISTS {en}")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    # Extensions left in place (shared with other schemas potentially).
