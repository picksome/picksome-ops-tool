# db_schema — 테이블 명세 YAML

> M001_initial_schema.py 의 모든 테이블을 기계 가독 형식(YAML)으로 정리.
> 향후 AI / 코드 생성기 / 모델 검증기 입력으로 사용.

## 형식 (`<table>.yaml`)

```yaml
table: organization
purpose: 짧은 한 줄 — 이 테이블이 도메인에서 하는 역할
section: 3-A 조직/사용자        # docs/21_erd.md 의 섹션
decisions: [D-280, D-285]      # 관련 의사결정 ID (없으면 빈 배열)

columns:
  - name: id
    type: UUID
    nullable: false
    primary_key: true
    default: gen_random_uuid()
  - name: business_type
    type: enum_business_type
    nullable: false
    default: JOINT_BUSINESS
    enum_values: [SOLE_PROPRIETOR, JOINT_BUSINESS, CORPORATION, TEAM_OPERATION]
    decision: D-280
    note: D-280 — 4종 사업자 형태 분기

audit_columns: true            # AUDIT_COLS 매크로 포함 여부
policy_columns: false          # POLICY_COLS 매크로 포함 여부

constraints:
  - name: ck_organization_reopen_limit
    type: CHECK
    expression: settlement_reopen_limit >= 0

indexes:
  - name: uq_partner_org_name
    type: UNIQUE
    columns: [org_id, name]
    where: deleted_at IS NULL

foreign_keys:
  - column: org_id
    references: organization.id
  - column: linked_user_id
    references: app_user.id
    nullable: true

referenced_by:                 # 다른 테이블이 이 테이블을 참조하는 컬럼
  - app_user.org_id
  - partner.org_id

notes: |
  자유 형식 메모. 박제 원칙, SYSTEM 템플릿 적용 여부, 사용 패턴 등.
```

## 매크로

### `audit_columns: true` → AUDIT_COLS 자동 포함
```
created_at TIMESTAMPTZ NOT NULL DEFAULT now()
created_by UUID NULL
updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
updated_by UUID NULL
deleted_at TIMESTAMPTZ NULL
deleted_by UUID NULL
```

### `policy_columns: true` → POLICY_COLS 자동 포함
```
org_id UUID NOT NULL REFERENCES organization(id)
version INT NOT NULL
effective_from DATE NOT NULL
effective_to DATE NULL
source_type enum_policy_source NOT NULL DEFAULT 'MANUAL'
source_url TEXT NULL
source_checked_at TIMESTAMPTZ NULL
approved_by UUID NULL REFERENCES app_user(id)
approved_at TIMESTAMPTZ NULL
reliability_status enum_policy_reliability NOT NULL DEFAULT 'CONFIRMED'
memo TEXT NULL
is_active BOOLEAN NOT NULL DEFAULT TRUE
```

## 출처 / 동기화

- 원본: `backend/app/db/migrations/versions/M001_initial_schema.py`
- 갱신 시점: 새 마이그레이션(M00X) 이 추가/변경한 컬럼/제약을 본 파일에 반영.
- 자동 동기화 도구는 미구현 — 사람이 PR 에서 함께 갱신.

## 인덱스 (테이블 목록)

전 48개 테이블. 카테고리별 분류:

| 섹션 | 테이블 |
|------|--------|
| 3-A 조직/사용자 | organization, app_user, partner |
| 3-B 정책 | marketplace_fee_policy, payment_fee_policy, tax_policy, fulfillment_fee_policy, return_risk_policy, ad_cost_policy, target_margin_policy, settlement_policy, partner_share_policy, expense_category_policy, evidence_type_policy |
| 3-B 정책 부속 | policy_change_history, policy_snapshot, policy_snapshot_ref, policy_snapshot_value |
| 3-C 보안 | password_vault_entry |
| 3-D 외부 파트너 | external_partner |
| 3-E 상품/옵션 | product, product_option_group, product_option_value, product_variant, product_sourcing_candidate |
| 3-F 마진 | product_margin_expected, product_margin_actual |
| 3-G 판매/정산 | sales_order, marketplace_settlement_record, monthly_settlement, partner_settlement, loss_carryover_ledger, settlement_reopen_audit |
| 3-H 사업비용/증빙 | expenses, expense_attachment, expense_comment |
| 3-I 세무사 export | tax_report_bundle, tax_report_bundle_item |
| 3-J 감사 | audit_log |
| 3-K 알림 | notification_channel, notification, notification_pref |
| 3-L 외부 연동 (Post-MVP) | integration_account, integration_job, batch_run, batch_run_log, external_product_mapping, supplier_price_snapshot, marketplace_order_import, marketplace_settlement_import |
