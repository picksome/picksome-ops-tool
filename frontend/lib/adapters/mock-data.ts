/**
 * Mock 데이터 — 실제 셀러 업무처럼 현실적인 한국어 데이터.
 *
 * 목적은 "화면이 비어 보이지 않게" 가 아니라 **화면 설계를 검증**하는 것이다.
 * 그래서 일부러 다음 케이스를 섞어 넣었다:
 *   - 적자 상품 (반품률이 높거나 광고비가 과한 것)
 *   - 소스 URL 이 없는 상품 (경고 대상)
 *   - 원가 적용일이 오래된 상품 (경고 대상)
 *   - 정산 불일치 건 (예상 ≠ 실입금)
 *   - 증빙 없는 비용
 * 전부 정상 데이터만 넣으면 경고 영역과 빈 상태를 검증할 수 없다.
 */

import type {
  DeliveryChannel,
  DistributionRule,
  ExpenseCategory,
  ExpenseRow,
  MarketplaceConnection,
  MarketplaceSettlementRow,
  Member,
  NotificationItem,
  NotificationRule,
  ProductDetail,
  ProductRow,
} from "@/lib/adapters/types";
import type { MarketplaceFeePolicy, TaxRule } from "@/lib/calc/types";

/* ────────────────────────────────────────────────────────────
 * 세금 규칙
 *
 * ⚠️ 아래 수치는 **구조 검증용 기본값**이다. 실제 신고에 쓰기 전
 * 세무 전문가 확인이 필요하다 (verifiedAt = null 인 규칙은 화면에 경고가 뜬다).
 * ──────────────────────────────────────────────────────────── */

export const MOCK_TAX_RULES: TaxRule[] = [
  {
    id: "tax-general-2025",
    taxProfileType: "GENERAL",
    effectiveFrom: "2025-01-01",
    effectiveTo: null,
    outputVatPolicy: "INCLUDED_IN_PRICE",
    inputVatCreditPolicy: "FULL_CREDIT",
    importVatCreditPolicy: "CREDITABLE",
    taxRateParameters: {
      vatRate: 0.1,
      incomeTaxEffectiveRate: 0.15,
    },
    description: "일반과세자",
    verifiedAt: null,
    ruleVersion: 1,
  },
  {
    id: "tax-simple-2025",
    taxProfileType: "SIMPLE",
    effectiveFrom: "2025-01-01",
    effectiveTo: null,
    outputVatPolicy: "DEEMED_VALUE_ADDED",
    inputVatCreditPolicy: "FLAT_RATE_CREDIT",
    importVatCreditPolicy: "NOT_CREDITABLE",
    taxRateParameters: {
      vatRate: 0.1,
      // 소매업/전자상거래 소매업 기준. 업종 판정은 세무사 확인 필요.
      deemedValueAddedRate: 0.15,
      inputCreditFlatRate: 0.005,
      incomeTaxEffectiveRate: 0.1,
    },
    description: "간이과세자 (소매업)",
    verifiedAt: null,
    ruleVersion: 1,
  },
  {
    id: "tax-exempt-2025",
    taxProfileType: "EXEMPT",
    effectiveFrom: "2025-01-01",
    effectiveTo: null,
    outputVatPolicy: "NONE",
    inputVatCreditPolicy: "NO_CREDIT",
    importVatCreditPolicy: "NOT_CREDITABLE",
    taxRateParameters: { vatRate: 0, incomeTaxEffectiveRate: 0.1 },
    description: "면세사업자",
    verifiedAt: null,
    ruleVersion: 1,
  },
];

/* ────────────────────────────────────────────────────────────
 * 판매처 수수료 정책
 * 실제 요율은 마켓 공지/계약에 따라 다르다. sourceReference 로 근거를 남긴다.
 * ──────────────────────────────────────────────────────────── */

export const MOCK_FEE_POLICIES: MarketplaceFeePolicy[] = [
  {
    id: "fee-coupang-default", marketplace: "COUPANG", category: null,
    feeType: "RATE", feeRate: 0.108, fixedFee: null, feeVatIncluded: false,
    effectiveFrom: "2025-01-01", effectiveTo: null,
    description: "쿠팡 기본 수수료 (카테고리 평균)",
    sourceReference: "쿠팡 Wing 판매자 수수료 안내", ruleVersion: 2,
  },
  {
    id: "fee-coupang-fashion", marketplace: "COUPANG", category: "패션의류",
    feeType: "RATE", feeRate: 0.105, fixedFee: null, feeVatIncluded: false,
    effectiveFrom: "2025-01-01", effectiveTo: null,
    description: "쿠팡 패션의류", sourceReference: "쿠팡 카테고리별 수수료표", ruleVersion: 1,
  },
  {
    id: "fee-smartstore-default", marketplace: "SMARTSTORE", category: null,
    feeType: "RATE", feeRate: 0.0574, fixedFee: null, feeVatIncluded: true,
    effectiveFrom: "2025-01-01", effectiveTo: null,
    description: "스마트스토어 (연동수수료+결제수수료)",
    sourceReference: "네이버 커머스 수수료 안내", ruleVersion: 3,
  },
  {
    id: "fee-elevenst-default", marketplace: "ELEVENST", category: null,
    feeType: "RATE", feeRate: 0.13, fixedFee: null, feeVatIncluded: false,
    effectiveFrom: "2025-01-01", effectiveTo: null,
    description: "11번가 기본", sourceReference: null, ruleVersion: 1,
  },
  {
    id: "fee-gmarket-default", marketplace: "GMARKET", category: null,
    feeType: "RATE", feeRate: 0.12, fixedFee: null, feeVatIncluded: false,
    effectiveFrom: "2025-01-01", effectiveTo: null,
    description: "G마켓 기본", sourceReference: null, ruleVersion: 1,
  },
  {
    id: "fee-ohou-default", marketplace: "OHOUSE", category: null,
    feeType: "RATE", feeRate: 0.15, fixedFee: null, feeVatIncluded: false,
    effectiveFrom: "2026-01-01", effectiveTo: null,
    description: "오늘의집 (추정 — 확인 필요)", sourceReference: null, ruleVersion: 1,
  },
  {
    id: "fee-toss-default", marketplace: "TOSS", category: null,
    feeType: "RATE", feeRate: 0.03, fixedFee: null, feeVatIncluded: false,
    effectiveFrom: "2025-01-01", effectiveTo: null,
    description: "토스쇼핑", sourceReference: null, ruleVersion: 1,
  },
];

export const MOCK_MARKETPLACES = [
  { code: "COUPANG", label: "쿠팡" },
  { code: "SMARTSTORE", label: "스마트스토어" },
  { code: "ELEVENST", label: "11번가" },
  { code: "GMARKET", label: "G마켓" },
  { code: "AUCTION", label: "옥션" },
  { code: "OHOUSE", label: "오늘의집" },
  { code: "KAKAOSHOPPING", label: "카카오쇼핑" },
  { code: "29CM", label: "29CM" },
  { code: "TOSS", label: "토스쇼핑" },
];

export const MOCK_CATEGORIES = [
  "생활용품", "주방용품", "패션의류", "패션잡화", "반려동물", "스포츠레저", "가전", "인테리어",
];

/* ────────────────────────────────────────────────────────────
 * 구성원
 * ──────────────────────────────────────────────────────────── */

export const MOCK_MEMBERS: Member[] = [
  {
    id: "u-001", name: "황지훈", email: "jihun@picksome.kr", avatarUrl: null,
    phone: "010-2345-6789", department: "경영", jobTitle: "대표",
    joinedAt: "2024-03-02", role: "OWNER",
    assignedMarketplaces: ["COUPANG", "SMARTSTORE"], assignedCategories: ["생활용품", "주방용품"],
    isActive: true, lastLoginAt: "2026-08-05T11:20:00Z",
  },
  {
    id: "u-002", name: "유서연", email: "seoyeon@picksome.kr", avatarUrl: null,
    phone: "010-8812-4471", department: "운영", jobTitle: "공동대표",
    joinedAt: "2024-03-02", role: "ADMIN",
    assignedMarketplaces: ["ELEVENST", "GMARKET"], assignedCategories: ["패션의류", "패션잡화"],
    isActive: true, lastLoginAt: "2026-08-05T09:05:00Z",
  },
  {
    id: "u-003", name: "박도현", email: "dohyun@picksome.kr", avatarUrl: null,
    phone: "010-4410-9032", department: "소싱", jobTitle: "매니저",
    joinedAt: "2025-06-16", role: "PRODUCT_MANAGER",
    assignedMarketplaces: ["COUPANG"], assignedCategories: ["반려동물", "스포츠레저"],
    isActive: true, lastLoginAt: "2026-08-04T14:32:00Z",
  },
  {
    id: "u-004", name: "최은비", email: "eunbi@picksome.kr", avatarUrl: null,
    phone: null, department: "정산", jobTitle: "주임",
    joinedAt: "2026-02-02", role: "SETTLEMENT_MANAGER",
    assignedMarketplaces: [], assignedCategories: [],
    isActive: true, lastLoginAt: "2026-08-05T08:41:00Z",
  },
  {
    id: "u-005", name: "김세무", email: "tax@ourtax.kr", avatarUrl: null,
    phone: null, department: "외부", jobTitle: "세무대리인",
    joinedAt: "2025-01-05", role: "VIEWER",
    assignedMarketplaces: [], assignedCategories: [],
    isActive: true, lastLoginAt: "2026-07-28T10:00:00Z",
  },
];

/* ────────────────────────────────────────────────────────────
 * 상품
 * ──────────────────────────────────────────────────────────── */

function product(
  n: number,
  o: Partial<ProductRow> & Pick<ProductRow, "name" | "salePrice" | "totalCost">,
): ProductRow {
  const salePrice = o.salePrice ?? 0;
  const totalCost = o.totalCost ?? 0;
  const settlement = Math.round(salePrice * 0.89);
  const net = settlement - totalCost - Math.round(salePrice * 0.05);
  return {
    id: `p-${String(n).padStart(3, "0")}`,
    businessKey: `PROD-${String(n).padStart(6, "0")}`,
    name: o.name,
    sku: o.sku ?? `PS-${String(n).padStart(4, "0")}`,
    imageUrl: null,
    status: o.status ?? "ACTIVE",
    marginType: o.marginType ?? "GENERAL_RESELL",
    category: o.category ?? "생활용품",
    marketplaces: o.marketplaces ?? ["COUPANG"],
    marketplaceProductNos: o.marketplaceProductNos ?? { COUPANG: `78${n}${n}4021` },
    sourceUrl: o.sourceUrl ?? null,
    supplierName: o.supplierName ?? null,
    costEffectiveFrom: o.costEffectiveFrom ?? "2026-05-01",
    priceEffectiveFrom: o.priceEffectiveFrom ?? "2026-05-01",
    totalCost,
    salePrice,
    expectedSettlement: o.expectedSettlement ?? settlement,
    expectedNetProfit: o.expectedNetProfit ?? net,
    marginRate: salePrice ? (o.expectedNetProfit ?? net) / salePrice : null,
    optionCount: o.optionCount ?? 0,
    ownerId: o.ownerId ?? "u-003",
    ownerName: o.ownerName ?? "박도현",
    memo: o.memo ?? null,
    createdAt: o.createdAt ?? "2026-05-01T02:00:00Z",
    updatedAt: o.updatedAt ?? "2026-07-28T05:12:00Z",
  };
}

export const MOCK_PRODUCTS: ProductRow[] = [
  product(880, {
    name: "국산 쿨링 스카프 쿨스카프 얼음스카프",
    category: "패션잡화", salePrice: 12900, totalCost: 4200, optionCount: 6,
    marketplaces: ["COUPANG", "SMARTSTORE"],
    marketplaceProductNos: { COUPANG: "7801234021", SMARTSTORE: "NV-559120" },
    sourceUrl: "https://detail.1688.com/offer/774512093812.html",
    supplierName: "이우 텍스타일", marginType: "OVERSEAS_DIRECT",
  }),
  product(881, {
    name: "실리콘 주방 집게 3종 세트",
    category: "주방용품", salePrice: 9900, totalCost: 3350, optionCount: 3,
    sourceUrl: "https://detail.1688.com/offer/661204558931.html", supplierName: "광저우 키친",
  }),
  product(882, {
    name: "강아지 쿨매트 여름 냉감 방석 L",
    category: "반려동물", salePrice: 24900, totalCost: 11200, optionCount: 4,
    marketplaces: ["COUPANG", "OHOUSE"],
    marketplaceProductNos: { COUPANG: "7803341022", OHOUSE: "OH-88120" },
    sourceUrl: "https://www.domeggook.com/26881204", supplierName: "도매꾹 펫라인",
    ownerId: "u-003", ownerName: "박도현",
  }),
  product(883, {
    name: "차량용 무선 청소기 미니 핸디형",
    category: "가전", salePrice: 38900, totalCost: 24800, optionCount: 2,
    // 광고비 과다 + 반품률 높음 → 적자 상품 케이스
    expectedNetProfit: -2400, expectedSettlement: 34620,
    sourceUrl: "https://detail.1688.com/offer/990112384412.html", supplierName: "선전 일렉트로",
    memo: "반품률 12% — 배터리 불량 클레임 다수. 공급처 교체 검토",
  }),
  product(884, {
    name: "접이식 빨래건조대 스탠드형 대형",
    category: "생활용품", salePrice: 32900, totalCost: 16400, optionCount: 0,
    marketplaces: ["SMARTSTORE"], marketplaceProductNos: { SMARTSTORE: "NV-771203" },
    // 소스 URL 없음 → 경고 대상
    sourceUrl: null, supplierName: null,
    ownerId: "u-002", ownerName: "유서연",
  }),
  product(885, {
    name: "메쉬 여름 이불 싱글 리플 홑이불",
    category: "인테리어", salePrice: 19900, totalCost: 8900, optionCount: 9,
    marketplaces: ["COUPANG", "ELEVENST", "GMARKET"],
    marketplaceProductNos: { COUPANG: "7809912045", ELEVENST: "11ST-4402", GMARKET: "GM-99213" },
    sourceUrl: "https://detail.1688.com/offer/551209384412.html", supplierName: "난퉁 홈텍스",
    // 원가 적용일이 오래됨 → 경고 대상
    costEffectiveFrom: "2025-03-14",
  }),
  product(886, {
    name: "휴대용 미니 선풍기 목걸이형",
    category: "가전", salePrice: 14900, totalCost: 6100, optionCount: 5,
    sourceUrl: "https://detail.1688.com/offer/220948123344.html", supplierName: "선전 팬텍",
    status: "PAUSED", memo: "재고 소진 후 단종 예정",
  }),
  product(887, {
    name: "논슬립 요가매트 10mm TPE",
    category: "스포츠레저", salePrice: 27900, totalCost: 12300, optionCount: 4,
    marketplaces: ["COUPANG", "29CM"],
    marketplaceProductNos: { COUPANG: "7811203344", "29CM": "29-11204" },
    sourceUrl: "https://detail.1688.com/offer/883012449102.html", supplierName: "칭다오 스포츠",
  }),
  product(888, {
    name: "스텐 밀폐용기 4종 세트 냉장고 정리",
    category: "주방용품", salePrice: 23900, totalCost: 10800, optionCount: 0,
    status: "APPROVED", priceEffectiveFrom: null,
    sourceUrl: "https://www.domeggook.com/27110234", supplierName: "도매꾹 리빙",
    memo: "8월 2주차 등록 예정",
  }),
  product(889, {
    name: "차량용 햇빛가리개 앞유리 자석식",
    category: "생활용품", salePrice: 16900, totalCost: 7400, optionCount: 3,
    status: "CANDIDATE", priceEffectiveFrom: null, costEffectiveFrom: null,
    sourceUrl: "https://detail.1688.com/offer/104412093381.html", supplierName: "이우 오토",
    memo: "MOQ 200개 — 샘플 요청함",
  }),
  product(890, {
    name: "대용량 텀블러 1L 보온보냉 스텐",
    category: "주방용품", salePrice: 21900, totalCost: 9600, optionCount: 6,
    marketplaces: ["SMARTSTORE", "KAKAOSHOPPING"],
    marketplaceProductNos: { SMARTSTORE: "NV-880412", KAKAOSHOPPING: "KK-33120" },
    sourceUrl: "https://detail.1688.com/offer/771203449812.html", supplierName: "융캉 스틸",
    ownerId: "u-002", ownerName: "유서연",
  }),
  product(891, {
    name: "무선 마우스 정숙형 블루투스 겸용",
    category: "가전", salePrice: 18900, totalCost: 11900, optionCount: 3,
    sourceUrl: "https://detail.1688.com/offer/330124498120.html", supplierName: "선전 페리퍼럴",
    // 저마진 케이스
    expectedNetProfit: 420, expectedSettlement: 16821,
  }),
];

export const MOCK_PRODUCT_DETAIL: Record<string, ProductDetail> = Object.fromEntries(
  MOCK_PRODUCTS.map((p) => [
    p.id,
    {
      ...p,
      // mock 은 실제 서버 상태가 아니다 — 동시성 토큰도, 진행 중 승인 요청도 없다.
      versionToken: null,
      openApprovals: [],
      unsupportedFields: [],
      sourcing: {
        sourceUrl: p.sourceUrl,
        supplierName: p.supplierName,
        purchaseCurrency: p.sourceUrl?.includes("1688") ? "CNY" : "KRW",
        purchasePrice: p.sourceUrl?.includes("1688")
          ? Math.round((p.totalCost ?? 0) * 0.55 / 190)
          : Math.round((p.totalCost ?? 0) * 0.7),
        exchangeRate: p.sourceUrl?.includes("1688") ? 190 : 1,
        domesticShippingAbroad: p.sourceUrl?.includes("1688") ? 8 : 0,
        internationalShipping: p.sourceUrl?.includes("1688")
          ? Math.round((p.totalCost ?? 0) * 0.2) : 0,
        customsDuty: 0,
        importVat: p.sourceUrl?.includes("1688") ? Math.round((p.totalCost ?? 0) * 0.06) : 0,
        domesticShipping: 3000,
        packagingCost: 400,
        otherCost: 0,
        costEffectiveFrom: p.costEffectiveFrom,
        sourceCheckedAt: p.costEffectiveFrom,
      },
      pricing: {
        salePrice: p.salePrice ?? 0,
        buyerShippingFee: 0,
        priceEffectiveFrom: p.priceEffectiveFrom,
        commissionRate: 0.108,
        adCostRate: 0.05,
        returnRate: p.id === "p-883" ? 0.12 : 0.02,
        fixedCostAllocation: 500,
        targetMarginRate: 0.25,
      },
      channels: p.marketplaces.map((mk, i) => ({
        id: `${p.id}-ch-${i}`,
        marketplace: mk,
        marketplaceProductNo: p.marketplaceProductNos[mk] ?? null,
        listingUrl: null,
        salePrice: p.salePrice,
        isActive: p.status === "ACTIVE",
        lastSyncedAt: "2026-08-05T02:00:00Z",
      })),
      options: [],
      history: [
        {
          id: `${p.id}-h1`, at: "2026-07-28T05:12:00Z", actor: "박도현",
          field: "판매가", before: String((p.salePrice ?? 0) + 1000), after: String(p.salePrice),
        },
        {
          id: `${p.id}-h2`, at: "2026-05-01T02:00:00Z", actor: "황지훈",
          field: "상품 등록", before: null, after: p.name,
        },
      ],
    } satisfies ProductDetail,
  ]),
);

/** 옵션 예시 — 색상/사이즈 조합 */
export const MOCK_OPTION_GROUPS: Record<string, Array<{ name: string; values: string[] }>> = {
  "p-880": [
    { name: "색상", values: ["네이비", "그레이", "핑크"] },
    { name: "구성", values: ["1개입", "2개입"] },
  ],
  "p-882": [
    { name: "사이즈", values: ["M", "L"] },
    { name: "색상", values: ["아이보리", "그레이"] },
  ],
  "p-885": [
    { name: "사이즈", values: ["싱글", "슈퍼싱글", "퀸"] },
    { name: "색상", values: ["민트", "베이지", "차콜"] },
  ],
};

/* ────────────────────────────────────────────────────────────
 * 쇼핑몰 정산
 * ──────────────────────────────────────────────────────────── */

export const MOCK_SETTLEMENTS: MarketplaceSettlementRow[] = [
  {
    id: "st-001", marketplace: "COUPANG", baseMonth: "2026-07",
    scheduledDate: "2026-08-15", completedDate: null,
    grossRevenue: 8_412_000, commissionFee: 908_500, shippingDeduction: 141_000,
    adAndCouponDeduction: 412_300, otherDeduction: 0,
    expectedSettlement: 6_950_200, actualDeposit: null, difference: null,
    matchStatus: "UNMATCHED", source: "API",
  },
  {
    id: "st-002", marketplace: "SMARTSTORE", baseMonth: "2026-07",
    scheduledDate: "2026-08-10", completedDate: "2026-08-10",
    grossRevenue: 3_204_000, commissionFee: 183_900, shippingDeduction: 0,
    adAndCouponDeduction: 96_100, otherDeduction: 0,
    expectedSettlement: 2_924_000, actualDeposit: 2_924_000, difference: 0,
    matchStatus: "MATCHED", source: "API",
  },
  {
    id: "st-003", marketplace: "COUPANG", baseMonth: "2026-06",
    scheduledDate: "2026-07-15", completedDate: "2026-07-15",
    grossRevenue: 7_881_000, commissionFee: 851_100, shippingDeduction: 132_000,
    adAndCouponDeduction: 388_400, otherDeduction: 12_000,
    expectedSettlement: 6_497_500, actualDeposit: 6_312_880, difference: -184_620,
    // 불일치 케이스 — 반품 차감이 다음 달로 넘어온 상황
    matchStatus: "MISMATCH", source: "API",
  },
  {
    id: "st-004", marketplace: "ELEVENST", baseMonth: "2026-07",
    scheduledDate: "2026-08-20", completedDate: null,
    grossRevenue: 1_120_000, commissionFee: 145_600, shippingDeduction: 22_000,
    adAndCouponDeduction: 0, otherDeduction: 0,
    expectedSettlement: 952_400, actualDeposit: null, difference: null,
    matchStatus: "UNMATCHED", source: "UPLOAD",
  },
  {
    id: "st-005", marketplace: "GMARKET", baseMonth: "2026-06",
    scheduledDate: "2026-07-18", completedDate: "2026-07-19",
    grossRevenue: 884_000, commissionFee: 106_080, shippingDeduction: 18_000,
    adAndCouponDeduction: 24_000, otherDeduction: 0,
    expectedSettlement: 735_920, actualDeposit: 735_920, difference: 0,
    matchStatus: "MATCHED", source: "MANUAL",
  },
];

/* ────────────────────────────────────────────────────────────
 * 비용 카테고리 · 사업비용
 * ──────────────────────────────────────────────────────────── */

const CATEGORY_SEED: Array<[string, ExpenseCategory["costType"], boolean, boolean, number | null]> = [
  ["상품 매입", "variable", true, true, null],
  ["해외 배송", "variable", true, true, null],
  ["국내 배송", "variable", true, true, null],
  ["포장재", "variable", true, true, 300_000],
  ["광고비", "variable", false, true, 2_000_000],
  ["플랫폼 이용료", "fixed", false, true, 150_000],
  ["소프트웨어", "fixed", false, true, 200_000],
  ["인건비", "fixed", false, true, 3_000_000],
  ["외주비", "variable", false, true, 500_000],
  ["사무실", "fixed", false, true, 800_000],
  ["통신비", "fixed", false, true, 120_000],
  ["교통비", "variable", false, true, 200_000],
  ["세무·회계", "fixed", false, true, 150_000],
  ["반품·보상", "variable", false, true, null],
  ["기타", "variable", false, true, null],
];

export const MOCK_EXPENSE_CATEGORIES: ExpenseCategory[] = CATEGORY_SEED.map(
  ([name, costType, inCost, inProfit, budget], i) => ({
    id: `ec-${String(i + 1).padStart(3, "0")}`,
    name,
    parentId: null,
    costType,
    includeInProductCost: inCost,
    includeInProfitCalculation: inProfit,
    monthlyBudget: budget,
    defaultPayerId: name === "광고비" ? "u-001" : null,
    isActive: true,
    usageCount: [12, 8, 21, 4, 33, 6, 9, 3, 2, 6, 6, 11, 6, 5, 14][i] ?? 0,
  }),
);

export const MOCK_EXPENSES: ExpenseRow[] = [
  {
    id: "ex-001", paidAt: "2026-08-03", categoryId: "ec-005", categoryName: "광고비",
    vendor: "쿠팡 애즈", description: "7월 쿠팡 광고비 정산", amount: 1_284_000, vatAmount: 116_727,
    payerId: "u-001", payerName: "황지훈", paymentMethod: "BUSINESS_CARD",
    relatedMarketplace: "COUPANG", relatedProductId: null, relatedProductName: null,
    relatedProject: null, attachments: [], memo: null, approvalStatus: "APPROVED",
  },
  {
    id: "ex-002", paidAt: "2026-08-02", categoryId: "ec-004", categoryName: "포장재",
    vendor: "박스팩토리", description: "택배박스 2호 500매 + 완충재", amount: 187_000, vatAmount: 17_000,
    payerId: "u-003", payerName: "박도현", paymentMethod: "PERSONAL_CARD",
    relatedMarketplace: null, relatedProductId: null, relatedProductName: null,
    relatedProject: null, attachments: [], memo: "개인카드 결제 — 정산 필요",
    approvalStatus: "SUBMITTED",
  },
  {
    id: "ex-003", paidAt: "2026-08-01", categoryId: "ec-010", categoryName: "사무실",
    vendor: "위워크 선릉", description: "8월 사무실 임대료", amount: 770_000, vatAmount: 70_000,
    payerId: "u-001", payerName: "황지훈", paymentMethod: "BANK_TRANSFER",
    relatedMarketplace: null, relatedProductId: null, relatedProductName: null,
    relatedProject: null, attachments: [], memo: null, approvalStatus: "APPROVED",
  },
  {
    id: "ex-004", paidAt: "2026-07-29", categoryId: "ec-002", categoryName: "해외 배송",
    vendor: "이우물류", description: "7월 4주차 컨테이너 분할 운송", amount: 462_000, vatAmount: 0,
    payerId: "u-002", payerName: "유서연", paymentMethod: "BANK_TRANSFER",
    relatedMarketplace: null, relatedProductId: "p-880", relatedProductName: "국산 쿨링 스카프",
    relatedProject: "2026 여름 시즌", attachments: [], memo: null, approvalStatus: "APPROVED",
  },
  {
    id: "ex-005", paidAt: "2026-07-25", categoryId: "ec-007", categoryName: "소프트웨어",
    vendor: "Adobe", description: "포토샵 연간 구독", amount: 297_000, vatAmount: 27_000,
    payerId: "u-002", payerName: "유서연", paymentMethod: "BUSINESS_CARD",
    relatedMarketplace: null, relatedProductId: null, relatedProductName: null,
    relatedProject: null, attachments: [], memo: null, approvalStatus: "DRAFT",
  },
  {
    id: "ex-006", paidAt: "2026-07-22", categoryId: "ec-014", categoryName: "반품·보상",
    vendor: null, description: "차량용 청소기 불량 보상 3건", amount: 116_700, vatAmount: 0,
    payerId: "u-004", payerName: "최은비", paymentMethod: "BANK_TRANSFER",
    relatedMarketplace: "COUPANG", relatedProductId: "p-883",
    relatedProductName: "차량용 무선 청소기", relatedProject: null,
    attachments: [], memo: "공급처 클레임 진행 중", approvalStatus: "APPROVED",
  },
];

/* ────────────────────────────────────────────────────────────
 * 수익배분 규칙
 *
 * 직원 급여는 **비용**이라 순이익 계산 전에 빠지고,
 * 동업자 배분은 **이익 처분**이라 순이익 계산 후에 나뉜다. 이 둘을 같은 표에서
 * 다루되 계산 단계를 다르게 태우는 것이 이 모델의 핵심이다.
 * ──────────────────────────────────────────────────────────── */

export const MOCK_DISTRIBUTION_RULES: DistributionRule[] = [
  {
    id: "dr-001", participantId: "u-001", participantName: "황지훈", participantType: "OWNER",
    calculationBase: "FIXED", fixedAmount: 1_000_000, percentage: null,
    threshold: null, capAmount: null, minimumAmount: null, priority: 10,
    effectiveFrom: "2025-01-01", effectiveTo: null, isActive: true,
  },
  {
    id: "dr-002", participantId: "u-002", participantName: "유서연", participantType: "CO_FOUNDER",
    calculationBase: "FIXED", fixedAmount: 400_000, percentage: null,
    threshold: null, capAmount: null, minimumAmount: null, priority: 10,
    effectiveFrom: "2025-01-01", effectiveTo: null, isActive: true,
  },
  {
    id: "dr-003", participantId: "u-001", participantName: "황지훈", participantType: "OWNER",
    calculationBase: "NET_PROFIT_RATE", fixedAmount: null, percentage: 0.6,
    threshold: null, capAmount: null, minimumAmount: null, priority: 30,
    effectiveFrom: "2025-01-01", effectiveTo: null, isActive: true,
  },
  {
    id: "dr-004", participantId: "u-002", participantName: "유서연", participantType: "CO_FOUNDER",
    calculationBase: "NET_PROFIT_RATE", fixedAmount: null, percentage: 0.4,
    threshold: null, capAmount: null, minimumAmount: null, priority: 30,
    effectiveFrom: "2025-01-01", effectiveTo: null, isActive: true,
  },
  {
    id: "dr-005", participantId: "u-003", participantName: "박도현", participantType: "EMPLOYEE",
    calculationBase: "FIXED", fixedAmount: 2_800_000, percentage: null,
    threshold: null, capAmount: null, minimumAmount: null, priority: 5,
    effectiveFrom: "2025-06-16", effectiveTo: null, isActive: true,
  },
  {
    id: "dr-006", participantId: "u-003", participantName: "박도현", participantType: "EMPLOYEE",
    calculationBase: "EXCESS_OVER_THRESHOLD", fixedAmount: null, percentage: 0.05,
    threshold: 5_000_000, capAmount: 1_000_000, minimumAmount: null, priority: 20,
    effectiveFrom: "2026-01-01", effectiveTo: null, isActive: true,
  },
];

/* ────────────────────────────────────────────────────────────
 * 알림
 * ──────────────────────────────────────────────────────────── */

export const MOCK_NOTIFICATION_RULES: NotificationRule[] = [
  {
    id: "nr-001", code: "MARGIN_BELOW", name: "마진율 기준 미달",
    conditionText: "상품 마진율이 기준값 아래로 떨어지면 알립니다. 매일 09:00 검사.",
    threshold: 10, thresholdUnit: "%", severity: "WARNING", category: "MARGIN",
    enabled: true, channels: ["IN_APP", "EMAIL"],
  },
  {
    id: "nr-002", code: "LOSS_SALE", name: "적자 판매 발생",
    conditionText: "판매 1건의 순이익이 0원 미만이면 즉시 알립니다.",
    threshold: null, thresholdUnit: null, severity: "CRITICAL", category: "MARGIN",
    enabled: true, channels: ["IN_APP", "EMAIL", "DISCORD"],
  },
  {
    id: "nr-003", code: "COST_CHANGED", name: "원가 변경",
    conditionText: "상품 원가가 변경되면 알립니다. 판매가 재검토가 필요합니다.",
    threshold: null, thresholdUnit: null, severity: "INFO", category: "PRODUCT",
    enabled: true, channels: ["IN_APP"],
  },
  {
    id: "nr-004", code: "PRICE_UPDATE_NEEDED", name: "판매가 적용 필요",
    conditionText: "원가가 바뀐 뒤 판매가가 갱신되지 않은 상품이 있으면 알립니다.",
    threshold: 7, thresholdUnit: "일", severity: "WARNING", category: "PRODUCT",
    enabled: true, channels: ["IN_APP"],
  },
  {
    id: "nr-005", code: "SETTLEMENT_MISMATCH", name: "정산 불일치",
    conditionText: "예상 정산액과 실제 입금액의 차이가 기준값을 넘으면 알립니다.",
    threshold: 50_000, thresholdUnit: "원", severity: "CRITICAL", category: "SETTLEMENT",
    enabled: true, channels: ["IN_APP", "EMAIL", "SLACK"],
  },
  {
    id: "nr-006", code: "SYNC_FAILED", name: "API 동기화 실패",
    conditionText: "쇼핑몰 API 동기화가 실패하면 즉시 알립니다.",
    threshold: null, thresholdUnit: null, severity: "CRITICAL", category: "SYNC",
    enabled: true, channels: ["IN_APP", "DISCORD"],
  },
  {
    id: "nr-007", code: "BUDGET_EXCEEDED", name: "사업비용 예산 초과",
    conditionText: "카테고리별 월 예산의 기준 비율을 넘으면 알립니다.",
    threshold: 90, thresholdUnit: "%", severity: "WARNING", category: "COST",
    enabled: true, channels: ["IN_APP"],
  },
  {
    id: "nr-008", code: "SOURCE_URL_MISSING", name: "상품 소스 URL 누락",
    conditionText: "소스 상품 URL 이 없는 활성 상품이 있으면 주 1회 알립니다.",
    threshold: null, thresholdUnit: null, severity: "INFO", category: "PRODUCT",
    enabled: false, channels: ["IN_APP"],
  },
  {
    id: "nr-009", code: "COST_STALE", name: "원가 확인일 경과",
    conditionText: "마지막 원가 확인일로부터 기준 일수가 지난 상품을 알립니다.",
    threshold: 90, thresholdUnit: "일", severity: "WARNING", category: "PRODUCT",
    enabled: true, channels: ["IN_APP"],
  },
];

export const MOCK_NOTIFICATIONS: NotificationItem[] = [
  {
    id: "n-001", severity: "CRITICAL", category: "SETTLEMENT",
    title: "쿠팡 6월 정산 불일치 184,620원",
    body: "예상 정산액 6,497,500원 대비 실입금 6,312,880원. 반품 차감이 다음 달로 이월된 것으로 보입니다.",
    entityType: "settlement", entityId: "st-003",
    createdAt: "2026-08-05T01:12:00Z", readAt: null,
    deliveredChannels: ["IN_APP", "EMAIL"], deliveryStatus: "SENT",
  },
  {
    id: "n-002", severity: "CRITICAL", category: "MARGIN",
    title: "적자 판매 — 차량용 무선 청소기",
    body: "8월 4일 쿠팡 주문 2건이 건당 −2,400원으로 판매되었습니다.",
    entityType: "product", entityId: "p-883",
    createdAt: "2026-08-04T23:40:00Z", readAt: null,
    deliveredChannels: ["IN_APP", "EMAIL", "DISCORD"], deliveryStatus: "SENT",
  },
  {
    id: "n-003", severity: "WARNING", category: "PRODUCT",
    title: "원가 확인일 경과 상품 1건",
    body: "메쉬 여름 이불 — 원가 적용일 2025-03-14 이후 확인되지 않았습니다.",
    entityType: "product", entityId: "p-885",
    createdAt: "2026-08-04T00:05:00Z", readAt: "2026-08-04T09:10:00Z",
    deliveredChannels: ["IN_APP"], deliveryStatus: "SENT",
  },
  {
    id: "n-004", severity: "WARNING", category: "COST",
    title: "광고비 예산 92% 소진",
    body: "8월 광고비 예산 2,000,000원 중 1,840,000원 집행되었습니다.",
    entityType: "expenseCategory", entityId: "ec-005",
    createdAt: "2026-08-03T09:00:00Z", readAt: "2026-08-03T09:30:00Z",
    deliveredChannels: ["IN_APP"], deliveryStatus: "SENT",
  },
  {
    id: "n-005", severity: "INFO", category: "SYNC",
    title: "스마트스토어 주문 동기화 완료",
    body: "주문 32건, 정산 1건을 가져왔습니다.",
    entityType: null, entityId: null,
    createdAt: "2026-08-05T02:00:00Z", readAt: null,
    deliveredChannels: ["IN_APP"], deliveryStatus: "SENT",
  },
];

export const MOCK_CHANNELS: DeliveryChannel[] = [
  {
    kind: "IN_APP", label: "앱 내부 알림", connected: true, maskedTarget: null,
    lastTestedAt: null, lastTestResult: null, availability: "READY",
    setupNote: "항상 활성화됩니다. 헤더의 알림 벨에서 확인합니다.",
  },
  {
    kind: "EMAIL", label: "이메일", connected: true, maskedTarget: "jihun@picksome.kr",
    lastTestedAt: "2026-08-01T05:00:00Z", lastTestResult: "SUCCESS", availability: "READY",
    setupNote: "구성원 프로필의 이메일로 발송됩니다.",
  },
  {
    kind: "DISCORD", label: "Discord", connected: true,
    maskedTarget: "https://discord.com/api/webhooks/••••••/••••",
    lastTestedAt: "2026-08-04T11:20:00Z", lastTestResult: "SUCCESS", availability: "READY",
    setupNote: "서버 설정 → 연동 → 웹후크에서 URL 을 만들어 붙여넣습니다.",
  },
  {
    kind: "SLACK", label: "Slack", connected: false, maskedTarget: null,
    lastTestedAt: null, lastTestResult: null, availability: "READY",
    setupNote: "Slack 앱 → Incoming Webhooks 에서 채널별 URL 을 발급받습니다.",
  },
  {
    kind: "KAKAO", label: "카카오 알림톡", connected: false, maskedTarget: null,
    lastTestedAt: null, lastTestResult: null, availability: "SETUP_REQUIRED",
    setupNote:
      "발신프로필 등록과 템플릿 사전심사가 필요합니다. 심사 승인 후 연결할 수 있습니다.",
  },
];

/* ────────────────────────────────────────────────────────────
 * 판매처 연동
 * ──────────────────────────────────────────────────────────── */

export const MOCK_CONNECTIONS: MarketplaceConnection[] = [
  {
    marketplace: "COUPANG", displayName: "쿠팡", connected: true,
    maskedApiKey: "ck_live_••••••••3f2a", status: "OK",
    lastSyncedAt: "2026-08-05T02:00:00Z",
    lastResult: {
      startedAt: "2026-08-05T02:00:00Z", finishedAt: "2026-08-05T02:01:12Z",
      status: "OK", productCount: 42, orderCount: 118, settlementCount: 1, message: null,
    },
    apiAvailability: "AVAILABLE",
    note: "Wing 판매자센터에서 Access Key / Secret Key 를 발급받아 등록합니다.",
  },
  {
    marketplace: "SMARTSTORE", displayName: "스마트스토어", connected: true,
    maskedApiKey: "nv_••••••••91cc", status: "OK",
    lastSyncedAt: "2026-08-05T02:00:00Z",
    lastResult: {
      startedAt: "2026-08-05T02:00:00Z", finishedAt: "2026-08-05T02:00:48Z",
      status: "OK", productCount: 18, orderCount: 32, settlementCount: 1, message: null,
    },
    apiAvailability: "AVAILABLE",
    note: "네이버 커머스 API 센터에서 애플리케이션을 등록합니다.",
  },
  {
    marketplace: "ELEVENST", displayName: "11번가", connected: false,
    maskedApiKey: null, status: "FAILED", lastSyncedAt: "2026-08-01T02:00:00Z",
    lastResult: {
      startedAt: "2026-08-01T02:00:00Z", finishedAt: "2026-08-01T02:00:05Z",
      status: "FAILED", productCount: 0, orderCount: 0, settlementCount: 0,
      message: "인증 실패 — API 키가 만료되었습니다 (401)",
    },
    apiAvailability: "AVAILABLE", note: "셀러오피스에서 API 키를 재발급하세요.",
  },
  {
    marketplace: "GMARKET", displayName: "G마켓", connected: false,
    maskedApiKey: null, status: "NEVER", lastSyncedAt: null, lastResult: null,
    apiAvailability: "PARTNER_ONLY",
    note: "제휴 심사가 필요합니다. 당분간 엑셀 업로드로 등록하세요.",
  },
  {
    marketplace: "OHOUSE", displayName: "오늘의집", connected: false,
    maskedApiKey: null, status: "NEVER", lastSyncedAt: null, lastResult: null,
    apiAvailability: "UNAVAILABLE",
    note: "공개 API 가 확인되지 않았습니다. 엑셀/CSV 업로드를 사용하세요.",
  },
];
