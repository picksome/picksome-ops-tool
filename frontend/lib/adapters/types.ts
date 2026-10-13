/**
 * 개편으로 새로 생긴 도메인 타입.
 *
 * 이 중 상당수는 아직 BE 엔드포인트가 없다. 그런 기능은 `MockDataSource` 로 동작시키고,
 * BE 가 생기면 `lib/adapters/index.ts` 의 구현체만 갈아끼운다 —
 * **화면 코드는 한 줄도 바꾸지 않는다.**
 *
 * BE 연동 상태는 `docs/41_redesign_worklog.md` 의 "mock 처리된 API" 표에 정리한다.
 */

import type { Rate, Won } from "@/lib/money";
import type { MarketplaceFeePolicy, TaxRule } from "@/lib/calc/types";

export type { MarketplaceFeePolicy, TaxRule };

/* ── 공통 ── */

export interface Paged<T> {
  items: T[];
  total: number;
}

export interface ListParams {
  q?: string;
  from?: string;
  to?: string;
  page?: number;
  pageSize?: number;
  sort?: string;
  dir?: "asc" | "desc";
  [key: string]: unknown;
}

/* ── 상품 ── */

export type ProductStatus = "CANDIDATE" | "APPROVED" | "ACTIVE" | "PAUSED" | "DISCONTINUED";
export type MarginType = "PURCHASE_AGENCY" | "ROCKET_GROWTH" | "OVERSEAS_DIRECT" | "GENERAL_RESELL";

export interface ProductRow {
  id: string;
  businessKey: string;
  name: string;
  sku: string | null;
  imageUrl: string | null;
  status: ProductStatus;
  marginType: MarginType;
  category: string | null;
  /** 판매처 (복수 가능 — 주 판매처를 대표로) */
  marketplaces: string[];
  /** 판매처 상품번호 (판매처별) */
  marketplaceProductNos: Record<string, string>;
  /** 원본 소스 상품 URL */
  sourceUrl: string | null;
  supplierName: string | null;
  /** 원가 적용일 */
  costEffectiveFrom: string | null;
  /** 판매가 적용일 */
  priceEffectiveFrom: string | null;
  /** 총원가 (부대비용 포함) */
  totalCost: Won | null;
  salePrice: Won | null;
  expectedSettlement: Won | null;
  expectedNetProfit: Won | null;
  marginRate: Rate | null;
  optionCount: number;
  ownerId: string | null;
  ownerName: string | null;
  memo: string | null;
  createdAt: string;
  updatedAt: string;
}

/** 상품 상세 — drawer 가 쓰는 전체 정보 */
export interface ProductDetail extends ProductRow {
  sourcing: {
    sourceUrl: string | null;
    supplierName: string | null;
    purchaseCurrency: string;
    purchasePrice: Won;
    exchangeRate: number;
    domesticShippingAbroad: Won;
    internationalShipping: Won;
    customsDuty: Won;
    importVat: Won;
    domesticShipping: Won;
    packagingCost: Won;
    otherCost: Won;
    costEffectiveFrom: string | null;
    sourceCheckedAt: string | null;
  };
  pricing: {
    salePrice: Won;
    buyerShippingFee: Won;
    priceEffectiveFrom: string | null;
    commissionRate: Rate;
    adCostRate: Rate;
    returnRate: Rate;
    fixedCostAllocation: Won;
    targetMarginRate: Rate;
  };
  channels: ProductChannel[];
  options: ProductOption[];
  history: ProductChangeLog[];

  /**
   * 낙관적 동시성 토큰. 상세에서 받은 값을 저장할 때 그대로 되돌려준다.
   * 그 사이 다른 사람이 저장했으면 `PRODUCT_VERSION_CONFLICT`(409) 로 거절된다.
   */
  versionToken: string | null;

  /** 이 상품에 걸려 있는 진행 중 승인 요청. 화면이 "이미 요청 있음" 을 미리 알려준다. */
  openApprovals: OpenApprovalSummary[];

  /**
   * DB 에 아직 없어서 화면에 0 으로 보이는 필드들.
   *
   * 이 값을 두는 이유: 원가 세부(관세·수입VAT·포장비 등)는 `docs/46` 의 스키마 결정이
   * 끝나야 저장할 수 있다. 그때까지 0 을 진짜 값처럼 보여주면 사용자가
   * **관세가 0원이라고 믿고** 판매가를 정한다. 화면이 "미지원" 을 표시해야 한다.
   */
  unsupportedFields: string[];
}

/** 상품에 걸려 있는 진행 중 승인 요청 요약 (값은 담지 않는다 — 요약이지 diff 가 아니다) */
export interface OpenApprovalSummary {
  id: string;
  businessKey: string;
  requestType: string;
  status: "DRAFT" | "PENDING" | "APPROVED";
  requestedAt: string;
  effectiveFrom: string | null;
}

export interface ProductChannel {
  id: string;
  marketplace: string;
  marketplaceProductNo: string | null;
  listingUrl: string | null;
  salePrice: Won | null;
  isActive: boolean;
  lastSyncedAt: string | null;
}

/** 옵션 — 기본 상품명과 분리해서 관리한다 */
export interface ProductOption {
  id: string;
  /** 옵션 그룹 → 값. 예: {색상: "네이비", 사이즈: "L"} */
  values: Record<string, string>;
  /** 규칙으로 자동 생성된 옵션명 */
  autoName: string;
  /** 사용자가 고친 옵션명. 있으면 이걸 쓴다 */
  customName: string | null;
  optionSku: string | null;
  optionCost: Won | null;
  extraPrice: Won;
  stock: number | null;
  isActive: boolean;
  sourceOptionUrl: string | null;
  marketplaceOptionNo: string | null;
}

export interface ProductOptionGroup {
  name: string;
  values: string[];
}

export interface ProductChangeLog {
  id: string;
  at: string;
  actor: string;
  field: string;
  before: string | null;
  after: string | null;
}

/* ── 판매장부 ── */

export interface SaleRow {
  id: string;
  saleDate: string;
  marketplace: string;
  /**
   * 유입 판매자 계정 (M032). **판매처와 다르다** — 쿠팡 본점과 2호점은
   * 같은 `marketplace` 지만 다른 계정이다.
   * `null` 은 "어느 계정인지 모른다" 이지 "계정 없음" 이 아니다.
   */
  integrationAccountId: string | null;
  /** 계정 표시 이름. 없으면 null — 채널 이름으로 대신 채우지 않는다. */
  integrationAccountLabel: string | null;
  orderNo: string;
  productId: string | null;
  productName: string;
  sku: string | null;
  marketplaceProductNo: string | null;
  buyerRef: string | null;
  quantity: number;
  salePrice: Won;
  /**
   * 주문 상태 — **DB 에 컬럼이 없다.** 항상 null 이다.
   *
   * 예전 어댑터는 여기에 "DELIVERED" 를 넣어서 반품된 주문까지 전부
   * "배송완료" 로 보였다. 판매처에서 상태를 받아오는 경로가 생기기 전까지
   * 화면은 "미지원" 으로 표시한다.
   */
  status: string | null;
  /** 담당자 — DB 에 컬럼이 없다. 항상 null 이다. */
  ownerName: string | null;
  memo: string | null;
  /** 계산 근거 — 있으면 drawer 에서 단계별로 표시 */
  netProfit: Won | null;
  marginRate: Rate | null;
}

/* ── 쇼핑몰 정산 ── */

export type SettlementMatchStatus = "MATCHED" | "UNMATCHED" | "MISMATCH";

export interface MarketplaceSettlementRow {
  id: string;
  marketplace: string;
  /** 정산 기준월 YYYY-MM */
  baseMonth: string;
  scheduledDate: string | null;
  completedDate: string | null;
  grossRevenue: Won;
  commissionFee: Won;
  shippingDeduction: Won;
  adAndCouponDeduction: Won;
  otherDeduction: Won;
  expectedSettlement: Won;
  actualDeposit: Won | null;
  /** 예상 − 실제 */
  difference: Won | null;
  matchStatus: SettlementMatchStatus;
  source: "API" | "UPLOAD" | "MANUAL";
}

/* ── 사업비용 ── */

export type ExpenseApprovalStatus = "DRAFT" | "SUBMITTED" | "APPROVED" | "REJECTED";

export interface ExpenseRow {
  id: string;
  paidAt: string;
  categoryId: string;
  categoryName: string;
  vendor: string | null;
  description: string;
  amount: Won;
  vatAmount: Won;
  payerId: string | null;
  payerName: string | null;
  paymentMethod: string;
  relatedMarketplace: string | null;
  relatedProductId: string | null;
  relatedProductName: string | null;
  relatedProject: string | null;
  attachments: ExpenseAttachment[];
  memo: string | null;
  approvalStatus: ExpenseApprovalStatus;
}

export interface ExpenseAttachment {
  id: string;
  filename: string;
  mimeType: string;
  size: number;
  /** 미리보기 URL. mock 에서는 objectURL */
  url: string;
  uploadedAt: string;
}

/* ── 비용 카테고리 ── */

export interface ExpenseCategory {
  id: string;
  name: string;
  parentId: string | null;
  costType: "fixed" | "variable";
  /** 상품 원가에 포함되는 성격인지 */
  includeInProductCost: boolean;
  /** 순이익 계산에 차감되는지 */
  includeInProfitCalculation: boolean;
  monthlyBudget: Won | null;
  defaultPayerId: string | null;
  isActive: boolean;
  /** 이 카테고리를 쓰는 비용 건수 — 삭제 가능 여부 판단 */
  usageCount?: number;
}

/* ── 수익배분 ── */

export type ParticipantType =
  | "OWNER"       // 대표자
  | "CO_FOUNDER"  // 공동사업자
  | "INVESTOR"    // 투자자
  | "EMPLOYEE"    // 직원
  | "CONTRACTOR"; // 외주 담당자

export type CalculationBase =
  | "FIXED"                  // 고정 지급액
  | "NET_PROFIT_RATE"        // 순이익 비율
  | "REVENUE_RATE"           // 매출 비율
  | "PRODUCT_PROFIT_RATE"    // 상품별 이익 비율
  | "EXCESS_OVER_THRESHOLD"  // 기준금액 초과분 비율
  | "TIERED_BONUS"           // 단계별 성과급
  | "INVESTOR_PRINCIPAL";    // 투자 원금 우선 회수

export interface DistributionRule {
  id: string;
  participantId: string;
  participantName: string;
  participantType: ParticipantType;
  calculationBase: CalculationBase;
  fixedAmount: Won | null;
  percentage: Rate | null;
  /** 기준금액 (초과분 비율·성과급 구간에 사용) */
  threshold: Won | null;
  /** 최대 지급 한도 */
  capAmount: Won | null;
  /** 최소 보장 */
  minimumAmount: Won | null;
  /** 낮을수록 먼저 배분 */
  priority: number;
  effectiveFrom: string;
  effectiveTo: string | null;
  /** 단계별 성과급 구간 */
  tiers?: Array<{ from: Won; to: Won | null; rate: Rate }>;
  isActive: boolean;
}

export interface DistributionResult {
  yearMonth: string;
  grossRevenue: Won;
  marketplaceDeduction: Won;
  variableCost: Won;
  businessExpense: Won;
  taxAndReturnReserve: Won;
  /** 고정 지급액 합계 */
  fixedPayouts: Won;
  /** 투자자 우선 배분 */
  investorPriority: Won;
  /** 잔여 배분 가능 이익 */
  distributableProfit: Won;
  payouts: Array<{
    participantId: string;
    participantName: string;
    participantType: ParticipantType;
    ruleLabel: string;
    amount: Won;
    /** 배분 근거 설명 */
    basis: string;
  }>;
  steps: Array<{ label: string; amount: Won; isDeduction: boolean; note?: string }>;
  warnings: string[];
}

/* ── 알림 ── */

export type NotificationSeverity = "INFO" | "WARNING" | "CRITICAL";
export type NotificationCategory =
  | "MARGIN" | "SETTLEMENT" | "COST" | "SYNC" | "PRODUCT" | "SYSTEM";

export interface NotificationItem {
  id: string;
  severity: NotificationSeverity;
  category: NotificationCategory;
  title: string;
  body: string;
  entityType: string | null;
  entityId: string | null;
  createdAt: string;
  readAt: string | null;
  deliveredChannels: string[];
  deliveryStatus: "PENDING" | "SENT" | "PARTIAL" | "FAILED";
}

export interface NotificationRule {
  id: string;
  code: string;
  name: string;
  /** 사람이 읽는 발동 조건 */
  conditionText: string;
  /** 임계값 (있는 규칙만) */
  threshold: number | null;
  thresholdUnit: string | null;
  severity: NotificationSeverity;
  category: NotificationCategory;
  enabled: boolean;
  channels: string[];
}

export type ChannelKind = "IN_APP" | "EMAIL" | "SLACK" | "DISCORD" | "KAKAO";

export interface DeliveryChannel {
  kind: ChannelKind;
  label: string;
  connected: boolean;
  /** 마스킹된 설정값 — 원문은 절대 클라이언트로 내려오지 않는다 */
  maskedTarget: string | null;
  lastTestedAt: string | null;
  lastTestResult: "SUCCESS" | "FAILED" | null;
  /** 아직 준비 안 된 채널 (카카오 알림톡 사전심사 등) */
  availability: "READY" | "SETUP_REQUIRED" | "COMING_SOON";
  setupNote: string;
}

/* ── 구성원 · 권한 ── */

export type MemberRole =
  | "OWNER" | "ADMIN" | "PRODUCT_MANAGER" | "SETTLEMENT_MANAGER"
  | "EXPENSE_APPROVER" | "VIEWER";

export type PermissionKey =
  | "product.read" | "product.write" | "cost.read" | "margin.read"
  | "sales.read" | "expense.write" | "expense.approve"
  | "settlement.read" | "distribution.read" | "distribution.write"
  | "integration.manage" | "member.manage";

export interface Member {
  id: string;
  name: string;
  email: string;
  avatarUrl: string | null;
  phone: string | null;
  department: string | null;
  jobTitle: string | null;
  joinedAt: string | null;
  role: MemberRole;
  /** 담당 판매처 */
  assignedMarketplaces: string[];
  /** 담당 카테고리 */
  assignedCategories: string[];
  isActive: boolean;
  lastLoginAt: string | null;
}

/* ── 판매처 연동 (커넥터) ── */

export type SyncStatus = "NEVER" | "OK" | "RUNNING" | "FAILED";

export interface SyncResult {
  startedAt: string;
  finishedAt: string | null;
  status: SyncStatus;
  productCount: number;
  orderCount: number;
  settlementCount: number;
  message: string | null;
}

export interface MarketplaceConnection {
  marketplace: string;
  displayName: string;
  connected: boolean;
  /** 마스킹된 API 키 — 원문은 서버 밖으로 나오지 않는다 */
  maskedApiKey: string | null;
  status: SyncStatus;
  lastSyncedAt: string | null;
  lastResult: SyncResult | null;
  /** 실제 API 지원 여부 */
  apiAvailability: "AVAILABLE" | "PARTNER_ONLY" | "UNAVAILABLE";
  note: string;
}

/**
 * 판매처 커넥터 인터페이스.
 *
 * 쿠팡부터 구현하되, 인증정보가 없으면 mock 커넥터가 대신 응답한다.
 * 마켓별 구현이 이 인터페이스만 만족하면 화면은 어느 마켓인지 몰라도 된다.
 */
export interface MarketplaceConnector {
  marketplace: string;
  connect(credentials: Record<string, string>): Promise<MarketplaceConnection>;
  disconnect(): Promise<void>;
  syncProducts(): Promise<SyncResult>;
  syncOrders(range: { from: string; to: string }): Promise<SyncResult>;
  syncSettlements(range: { from: string; to: string }): Promise<SyncResult>;
  getSyncStatus(): Promise<SyncStatus>;
  getLastSyncResult(): Promise<SyncResult | null>;
}

/* ── 대시보드 ── */

export interface DashboardFilters {
  from: string;
  to: string;
  marketplace?: string;
  ownerId?: string;
}

export interface KpiValue {
  key: string;
  label: string;
  value: Won | number;
  /** 이전 기간 값 */
  previous: Won | number;
  /** 표시 형식 */
  format: "money" | "percent" | "count";
  /** 값이 클수록 좋은가 — 증감 색상 판단에 사용 */
  higherIsBetter: boolean;
  hint?: string;
}

export interface TrendPoint {
  bucket: string;
  revenue: Won;
  netProfit: Won;
  marginRate: Rate | null;
}

export interface WaterfallStep {
  label: string;
  amount: Won;
  kind: "start" | "deduction" | "total";
}

export interface MarketplaceBreakdown {
  marketplace: string;
  revenue: Won;
  netProfit: Won;
  marginRate: Rate | null;
  orderCount: number;
}

export interface ProductScatterPoint {
  productId: string;
  productName: string;
  revenue: Won;
  marginRate: Rate;
  netProfit: Won;
}

export interface CostBreakdown {
  categoryName: string;
  amount: Won;
  ratio: Rate;
}

export interface SettlementStatusSummary {
  scheduled: { count: number; amount: Won };
  completed: { count: number; amount: Won };
  mismatched: { count: number; amount: Won };
}

export interface DashboardAlert {
  key: string;
  severity: NotificationSeverity;
  title: string;
  count: number;
  description: string;
  /** 클릭 시 이동할 목록 화면의 쿼리 */
  href: string;
}

export interface DashboardData {
  kpis: KpiValue[];
  trend: TrendPoint[];
  waterfall: WaterfallStep[];
  byMarketplace: MarketplaceBreakdown[];
  productScatter: ProductScatterPoint[];
  costBreakdown: CostBreakdown[];
  settlementStatus: SettlementStatusSummary;
  alerts: DashboardAlert[];
}
