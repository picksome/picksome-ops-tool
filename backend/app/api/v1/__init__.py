"""v1 라우터 일괄 등록 (docs/22_api_spec.md).

main.py 가 `from app.api.v1 import api_v1_router` 후 prefix `/api/v1` 로 include.

등록 순서 (의도적):
  1. auth                      — 토큰 발급 / me
  2. admin_users               — 사용자 관리 (ADMIN)
  3. admin_audit               — 감사 로그 조회/CSV (ADMIN)
  4. admin_vault               — vault 보관위치 (ADMIN/OPERATOR)
  5. notifications + prefs     — 본인 알림 + 환경설정 (D-281)
  6. policies                  — 11종 정책 CRUD (Generalization Principle)
  7. products                  — 상품/옵션/변형 (D-286)
  8. margin                    — expected/simulate/actual (박제)
  9. expenses                  — 사업비용 (D-284 VAT)
 10. settlement                — 월별 정산 (D-285 REOPEN)
 11. reports                   — 세무사 export 7종 + 대시보드 집계 (D-282)
 12. sales                     — 판매 주문 CRUD
 13. marketplace_settlements   — 마켓플레이스 정산 레코드 (읽기 전용)
 14. sourcing                  — 상품 소싱 후보 CRUD + 승인/거절
 15. sale_channel               — 판매채널 동적 카탈로그 (M007, D-287)
 16. exchange_rate              — 환율 자동화 (M008, D-288)
 17. integration                — 봇/API 연동 계정 + ingest (M009, D-289)
 18. search                     — 통합 검색 (⌘K 팔레트, docs/29_global_search.md)
 19. monthly_close              — 월 마감/잠금 (M017, docs/43 Phase 2)
 20. profit_status              — 이익 4단계 (M016, docs/43 Phase 1)
 21. approvals                  — 승인 워크플로 (M018, docs/43 Phase 3)
 22. partners                   — 파트너 목록 (읽기 전용, 비용 결제자·귀속 선택용)
 23. expense_attachments        — 증빙 파일 업로드·다운로드·삭제 (M027)
 24. external_partners          — 거래처 목록 (읽기 전용, 비용 supplier_id 선택용)

새 라우터 추가 시: import 1줄 + include 1줄만 추가."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.admin_audit import router as admin_audit_router
from app.api.v1.admin_users import router as admin_users_router
from app.api.v1.admin_vault import router as admin_vault_router
from app.api.v1.ai_access import router as ai_access_router
from app.api.v1.approvals import router as approvals_router
from app.api.v1.auth import router as auth_router
from app.api.v1.automation import router as automation_router
from app.api.v1.cashflow import router as cashflow_router
from app.api.v1.channel_prices import listing_router as channel_listing_router
from app.api.v1.channel_prices import product_router as channel_listing_product_router
from app.api.v1.exceptions import router as exceptions_router
from app.api.v1.exchange_rate import router as exchange_rate_router
from app.api.v1.expense_attachments import attachment_router as expense_attachment_router
from app.api.v1.expense_attachments import expense_scoped_router as expense_attachment_scoped_router
from app.api.v1.expenses import router as expenses_router
from app.api.v1.external import router as external_router
from app.api.v1.external_partners import router as external_partners_router
from app.api.v1.integration import router as integration_router
from app.api.v1.inventory import po_router as purchase_orders_router
from app.api.v1.inventory import router as inventory_router
from app.api.v1.inventory_import import router as inventory_import_router
from app.api.v1.margin import router as margin_router
from app.api.v1.marketplace_settlements import router as marketplace_settlements_router
from app.api.v1.mcp_gateway import router as mcp_gateway_router
from app.api.v1.monthly_close import profit_router as profit_status_router
from app.api.v1.monthly_close import router as monthly_close_router
from app.api.v1.notifications import prefs_router as notification_prefs_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.operations import router as operations_router
from app.api.v1.partners import directory_router as user_directory_router
from app.api.v1.partners import router as partners_router
from app.api.v1.payouts import distribution_router as distributions_router
from app.api.v1.payouts import router as payouts_router
from app.api.v1.policies import router as policies_router
from app.api.v1.product_source_import import router as source_import_router
from app.api.v1.products import router as products_router
from app.api.v1.reports import router as reports_router
from app.api.v1.sale_channel import router as sale_channel_router
from app.api.v1.sales import router as sales_router
from app.api.v1.search import router as search_router
from app.api.v1.settlement import router as settlement_router
from app.api.v1.settlement_import import router as settlement_import_router
from app.api.v1.sourcing import router as sourcing_router
from app.api.v1.sourcing_core import product_router as sourcing_product_router
from app.api.v1.sourcing_core import router as sourcing_core_router
from app.api.v1.verification import router as verification_router

api_v1_router = APIRouter()

api_v1_router.include_router(auth_router)
api_v1_router.include_router(admin_users_router)
api_v1_router.include_router(admin_audit_router)
api_v1_router.include_router(admin_vault_router)
# 자동화 · AI 접근. MCP 게이트웨이는 서비스 계정 토큰으로만 인증되고,
# MCP_ENABLED 가 꺼져 있으면 404 다 — 경로가 없는 것처럼 보인다.
api_v1_router.include_router(automation_router)
api_v1_router.include_router(ai_access_router)
api_v1_router.include_router(mcp_gateway_router)
api_v1_router.include_router(notifications_router)
api_v1_router.include_router(notification_prefs_router)
api_v1_router.include_router(policies_router)
api_v1_router.include_router(products_router)
# 판매처 listing — 상품 하위 경로(생성·목록)와 독립 경로(상세·가격)를 나눠 등록한다.
# 승인 화면은 상품 id 를 모르고 listing id 만 알기 때문에 독립 경로가 필요하다.
api_v1_router.include_router(channel_listing_product_router)
api_v1_router.include_router(channel_listing_router)
api_v1_router.include_router(margin_router)
api_v1_router.include_router(expenses_router)
# 증빙 — 비용 하위 경로(목록·업로드)와 첨부 단독 경로(다운로드·삭제)를 나눠 등록한다.
api_v1_router.include_router(expense_attachment_scoped_router)
api_v1_router.include_router(expense_attachment_router)
api_v1_router.include_router(partners_router)
# 사용자 이름 조회 — 승인 화면이 UUID 를 사람 이름으로 바꾸는 데 쓴다.
# `/admin/users` 는 ADMIN 전용이라 OPERATOR 가 승인함을 열면 이름을 못 붙인다.
api_v1_router.include_router(user_directory_router)
api_v1_router.include_router(external_partners_router)
api_v1_router.include_router(settlement_router)
api_v1_router.include_router(reports_router)
api_v1_router.include_router(sales_router)
api_v1_router.include_router(marketplace_settlements_router)
api_v1_router.include_router(settlement_import_router)
api_v1_router.include_router(sourcing_router)
api_v1_router.include_router(sale_channel_router)
api_v1_router.include_router(exchange_rate_router)
api_v1_router.include_router(integration_router)
api_v1_router.include_router(search_router)
api_v1_router.include_router(monthly_close_router)
api_v1_router.include_router(profit_status_router)
api_v1_router.include_router(approvals_router)
# 확정 수익배분 · 지급 장부 (Phase 31~34).
# 배분은 '얼마를 나눠야 하는가', 지급은 '실제로 보냈는가' — 성질이 달라 라우터도 나눈다.
api_v1_router.include_router(distributions_router)
api_v1_router.include_router(payouts_router)
# 운영 이슈 센터 + worker 관측 (Phase 35~38).
api_v1_router.include_router(operations_router)
# 검증센터 — 원천과 파생을 맞대어 본다. 읽기 전용이고, 기준값 승인만 쓰기다.
api_v1_router.include_router(verification_router)
api_v1_router.include_router(cashflow_router)
api_v1_router.include_router(sourcing_core_router)
api_v1_router.include_router(sourcing_product_router)
api_v1_router.include_router(external_router)
api_v1_router.include_router(exceptions_router)
api_v1_router.include_router(inventory_import_router)
api_v1_router.include_router(source_import_router)
api_v1_router.include_router(inventory_router)
api_v1_router.include_router(purchase_orders_router)

__all__ = ["api_v1_router"]
