/**
 * 판매처 listing / 가격 API 클라이언트 (M022).
 *
 * **mock 이 없다.** 가격은 승인·마진과 직결돼서 mock 이 실패를 가리면
 * 사용자가 존재하지 않는 가격으로 판매가를 정한다.
 *
 * 가격 해결(variant → 기본가 fallback, 할인 기간 판정)은 **서버가 한다.**
 * 여기서 다시 구현하지 않는다 — 두 군데 있으면 언젠가 갈린다.
 */

import { api, type ListResponse } from "@/lib/api";

/** 내부 가격 상태. 이 가격 버전이 우리 DB 에서 유효한가. */
export type PriceStatus = "ACTIVE" | "SUPERSEDED" | "CANCELLED";

/**
 * 외부 반영 상태. **내부 상태와 절대 합치지 않는다.**
 *
 * 합치면 "승인은 났는데 판매처 반영이 실패한" 상태를 표현할 수 없다.
 * 그 상태가 사용자가 가장 알아야 하는 상태다.
 */
export type PublishStatus =
  | "NOT_REQUESTED" | "QUEUED" | "PUBLISHING"
  | "PUBLISHED" | "FAILED" | "VERIFICATION_FAILED";

export type ListingStatus = "DRAFT" | "ACTIVE" | "PAUSED" | "ENDED";

export interface ChannelListing {
  id: string;
  org_id: string;
  integration_account_id: string;
  product_id: string;
  external_product_id: string | null;
  external_product_url: string | null;
  status: ListingStatus;
  auto_publish_price: boolean;
  created_at: string;
  updated_at: string | null;

  /** 표시용 — 화면이 계정 UUID 를 보여줄 수는 없다 */
  account_label: string | null;
  marketplace: string | null;
  account_status: string | null;

  /** connector 지원 여부. **화면이 판매처 이름으로 추측하지 않는다** */
  price_publish_supported: boolean;
  price_publish_unsupported_reason: string | null;

  version_token?: string | null;
}

export interface ChannelPrice {
  id: string;
  listing_id: string;
  product_variant_id: string | null;
  currency: string;
  /** 미입력은 **null**. 0 이 아니다 */
  regular_price: number | null;
  sale_price: number | null;
  sale_start_at: string | null;
  sale_end_at: string | null;
  effective_from: string;
  effective_to: string | null;
  status: PriceStatus;
  approval_request_id: string | null;
  publish_status: PublishStatus;
  publish_requested_at: string | null;
  published_at: string | null;
  publish_error: string | null;
  publish_attempt_count: number;
  created_at: string;
}

export interface ResolvedPrice {
  listing_id: string;
  product_variant_id: string | null;
  at: string;
  /** 실제 적용가. 할인 중이면 할인가. **없으면 null(미입력)** */
  resolved_price: number | null;
  regular_price: number | null;
  sale_price: number | null;
  sale_start_at: string | null;
  sale_end_at: string | null;
  on_sale: boolean;
  currency: string | null;
  source_price_id: string | null;
  /** variant 가격이 없어 상품 기본 가격을 썼는가 */
  variant_fallback: boolean;
  effective_from: string | null;
  effective_to: string | null;
}

export interface ConnectorSupport {
  marketplace: string;
  price_publish_supported: boolean;
  reason: string | null;
}

/** 가격 승인 요청 payload. 서버 `ChannelPriceIn` 과 같은 모양이어야 한다. */
export interface ChannelPriceProposal {
  product_variant_id?: string | null;
  currency?: string;
  regular_price?: number | null;
  sale_price?: number | null;
  sale_start_at?: string | null;
  sale_end_at?: string | null;
  effective_from: string;
  effective_to?: string | null;
}

/**
 * 마진 영향 미리보기.
 *
 * **판단 보조값이다.** 실제 가격 데이터의 source of truth 가 아니다.
 * 권한이 없으면 서버가 원가와 역산 가능한 값을 **지워서** 보낸다 —
 * 내려준 뒤 CSS 로 가리는 게 아니다.
 */
export interface PricePreviewSide {
  sale_price: number;
  cost_amount: number | null;
  marketplace_fee: number | null;
  payment_fee: number | null;
  ad_cost: number | null;
  fulfillment_fee: number | null;
  return_risk_cost: number | null;
  vat: number | null;
  expected_settlement: number | null;
  expected_net_profit: number | null;
  margin_rate: string | null;
  applied_policies: Record<string, { table: string; version: number;
    reliability_status: string | null; source_type: string | null }>;
  warnings: string[];
  /** 원가 미입력 등으로 계산할 수 없을 때만 채워진다 */
  unavailable_reason?: string;
}

export interface PricePreview {
  listing_id: string;
  product_variant_id: string | null;
  at: string;
  marketplace: string;
  currency: string;
  /** 현재 적용가 기준. 가격이 없으면 null */
  before: PricePreviewSide | null;
  after: PricePreviewSide | null;
  target_margin_rate: string | null;
  /** 목표 대비 차이(%p). 음수면 목표 미달 */
  margin_gap_pp: string | null;
  meets_target: boolean | null;
  policy_warnings: string[];
  policies_verified: boolean;
  computed_at: string;
  sensitive_visible: boolean;
  redacted_message?: string;
}

export const channelPricesApi = {
  listForProduct: (productId: string, status?: ListingStatus) =>
    api.get<ListResponse<ChannelListing>>(
      `/products/${productId}/channel-listings`,
      { query: { status: status || undefined } },
    ),

  create: (productId: string, body: {
    integration_account_id: string;
    external_product_id?: string | null;
    external_product_url?: string | null;
    status?: ListingStatus;
    auto_publish_price?: boolean;
  }) => api.post<ChannelListing>(`/products/${productId}/channel-listings`, body),

  get: (listingId: string) => api.get<ChannelListing>(`/channel-listings/${listingId}`),

  update: (listingId: string, body: Record<string, unknown>) =>
    api.patch<ChannelListing>(`/channel-listings/${listingId}`, body),

  deactivate: (listingId: string) =>
    api.post<ChannelListing>(`/channel-listings/${listingId}/deactivate`),

  /** 가격 이력. 과거·현재·미래가 모두 온다 — 구분은 `today_kst` 로 화면이 한다. */
  prices: (listingId: string, params: {
    variant_id?: string; status?: PriceStatus; include_default?: boolean;
  } = {}) =>
    api.get<ListResponse<ChannelPrice>>(`/channel-listings/${listingId}/prices`, {
      query: {
        variant_id: params.variant_id || undefined,
        status: params.status || undefined,
        include_default: params.include_default || undefined,
      },
    }),

  /** 특정 시점·variant 의 실제 적용가. **서버가 계산한다.** */
  resolved: (listingId: string, params: { variant_id?: string; at?: string } = {}) =>
    api.get<ResolvedPrice>(`/channel-listings/${listingId}/resolved-price`, {
      query: { variant_id: params.variant_id || undefined, at: params.at || undefined },
    }),

  /**
   * 판매처 반영 요청. **외부 API 를 부르는 게 아니라 큐에 넣는다.**
   * connector 가 없으면 서버가 403(`CONNECTOR_UNAVAILABLE`)으로 거절한다.
   */
  publish: (priceId: string) =>
    api.post<{ job_id: string; status: string }>(
      `/channel-listings/prices/${priceId}/publish`,
    ),

  /** 가격 변경 전후 마진 영향. 서버가 기존 계산 엔진으로 계산한다. */
  preview: (listingId: string, body: ChannelPriceProposal) =>
    api.post<PricePreview>(`/channel-listings/${listingId}/price-preview`, body),

  connectorSupport: () =>
    api.get<ConnectorSupport[]>("/channel-listings/connector-support"),
};

/* ────────────────────────────────────────────────────────────
 * 표시 규칙 — 순수 함수라 테스트 가능하다
 * ──────────────────────────────────────────────────────────── */

/** 미입력 표기. **0 원으로 표시하지 않는다.** */
export const NOT_ENTERED = "미입력";

/**
 * 금액 표시. `null` 은 "미입력", `0` 은 "0원".
 *
 * 이 둘을 같게 표시하면 사용자가 **판매가가 0원이라고 믿고** 마진을 판단한다.
 * (DB 는 0 을 거부하지만, 화면 규칙은 그것과 별개로 명확해야 한다.)
 */
export function formatPriceCell(value: number | null | undefined): string {
  if (value === null || value === undefined) return NOT_ENTERED;
  return `${value.toLocaleString("ko-KR")}원`;
}

/** 내부 가격 상태 — 기준일과 비교해 "적용 중 / 적용 예정 / 종료" 를 정한다. */
export type InternalPhase = "ACTIVE" | "SCHEDULED" | "EXPIRED" | "CANCELLED";

export function internalPhaseOf(price: ChannelPrice, todayKst: string): InternalPhase {
  if (price.status === "CANCELLED") return "CANCELLED";
  // 반개구간 [from, to) — 종료일 당일은 이미 다음 가격이 적용된다.
  if (price.effective_to && price.effective_to <= todayKst) return "EXPIRED";
  if (price.effective_from > todayKst) return "SCHEDULED";
  return "ACTIVE";
}

export const INTERNAL_PHASE_LABEL: Record<InternalPhase, string> = {
  ACTIVE: "적용 중",
  SCHEDULED: "적용 예정",
  EXPIRED: "종료",
  CANCELLED: "취소됨",
};

/**
 * 외부 반영 상태 라벨.
 *
 * connector 가 없으면 `NOT_REQUESTED` 라도 "미요청" 이 아니라 **"미연결"** 이다 —
 * 사용자가 "요청하면 되겠네" 라고 생각하게 두면 안 된다.
 */
export function publishLabel(
  status: PublishStatus,
  connectorSupported: boolean,
): string {
  if (!connectorSupported) return "미연결";
  return {
    NOT_REQUESTED: "미요청",
    QUEUED: "반영 대기",
    PUBLISHING: "반영 중",
    PUBLISHED: "반영 완료",
    FAILED: "반영 실패",
    VERIFICATION_FAILED: "반영 확인 실패",
  }[status];
}

/** 반영 버튼을 누를 수 있는가. 이유도 함께 돌려준다. */
export function canPublish(
  listing: Pick<ChannelListing, "price_publish_supported" | "price_publish_unsupported_reason">,
  price: Pick<ChannelPrice, "publish_status">,
): { enabled: boolean; reason: string | null } {
  if (!listing.price_publish_supported) {
    return {
      enabled: false,
      reason:
        listing.price_publish_unsupported_reason ??
        "이 판매처의 가격 변경 API가 아직 연결되지 않았습니다.",
    };
  }
  if (price.publish_status === "QUEUED" || price.publish_status === "PUBLISHING") {
    return { enabled: false, reason: "이미 반영 작업이 진행 중입니다." };
  }
  return { enabled: true, reason: null };
}
