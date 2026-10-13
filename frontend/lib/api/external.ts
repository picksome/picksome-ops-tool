/**
 * 외부 시스템 연동 API 클라이언트 (M038/M039).
 *
 * 판매처 연결과 다른 화면이다
 * ------------------------
 * `/integrations/marketplaces` 는 쿠팡·스마트스토어 **판매자 계정**이다.
 * 여기는 셀러툴처럼 우리가 **나가서 호출하는** 외부 시스템이다.
 * 책임이 달라 합치지 않았다(docs/58).
 *
 * 비밀은 내려오지 않는다
 * ------------------
 * 계정 응답에 secret 도 vault 항목 id 도 없다. `credential_configured`
 * (불리언)뿐이다.
 *
 * 카탈로그에 있다고 연동되는 게 아니다
 * -------------------------------
 * `connector_available` 이 false 면 아직 구현되지 않은 것이다. 화면은 동기화
 * 버튼을 비활성화하고 이유를 말한다 — 눌렀는데 아무 일도 안 나면 고장으로 읽힌다.
 */

import { api, type ListResponse } from "@/lib/api";

export type SystemType = "SELLER_TOOL" | "SUPPLIER_PLATFORM" | "OTHER";

export const SYSTEM_TYPE_LABEL: Record<SystemType, string> = {
  SELLER_TOOL: "셀러툴",
  SUPPLIER_PLATFORM: "공급 플랫폼",
  OTHER: "기타",
};

export type AccountStatus = "DISCONNECTED" | "CONNECTED" | "ERROR" | "DISABLED";

export const ACCOUNT_STATUS_LABEL: Record<AccountStatus, string> = {
  DISCONNECTED: "미연결",
  CONNECTED: "연결됨",
  ERROR: "오류",
  DISABLED: "사용 중지",
};

/** 연결 시험 결과. 무엇을 고쳐야 할지 알 수 있어야 한다. */
export type TestStatus =
  | "OK" | "CONNECTOR_UNAVAILABLE" | "CREDENTIAL_NOT_CONFIGURED"
  /** 참조는 있는데 저장소에서 못 읽는다. 미설정과 **다른** 상태다. */
  | "CREDENTIAL_BROKEN"
  | "AUTHENTICATION_FAILED" | "CONNECTION_FAILED" | "RATE_LIMITED";

export const TEST_STATUS_LABEL: Record<TestStatus, string> = {
  OK: "연결 정상",
  CONNECTOR_UNAVAILABLE: "연동 미구현 — 우리 쪽 준비가 안 됐습니다",
  CREDENTIAL_NOT_CONFIGURED: "자격증명 미설정",
  CREDENTIAL_BROKEN: "자격증명을 읽을 수 없음 — 다시 설정해야 합니다",
  AUTHENTICATION_FAILED: "인증 실패 — 자격증명을 확인하세요",
  CONNECTION_FAILED: "연결 실패 — 네트워크나 상대 서버 문제",
  RATE_LIMITED: "요청 한도 초과 — 잠시 후 다시",
};

/**
 * 자격증명 형태와 그 형태가 요구하는 항목.
 *
 * vendor 별 필드를 만들지 않는다 — `next_engine_password` 같은 이름이 생기기
 * 시작하면 셀러툴을 하나 붙일 때마다 화면과 스키마가 늘어난다. 서버의
 * `core/secret_store.REQUIRED_FIELDS` 와 같은 목록이다.
 */
export const CREDENTIAL_KIND_LABEL: Record<string, string> = {
  API_KEY: "API 키",
  ACCESS_SECRET: "액세스 키 + 시크릿",
  CLIENT_CREDENTIALS: "OAuth 클라이언트",
  OAUTH_REFRESH: "OAuth 갱신 토큰",
  BASIC: "아이디 · 비밀번호",
  CUSTOM: "기타",
};

export interface CredentialField {
  key: string;
  label: string;
  /** 자동완성이 붙으면 브라우저 비밀번호 관리자에 저장될 수 있다. */
  secret: boolean;
}

export const CREDENTIAL_FIELDS: Record<string, CredentialField[]> = {
  API_KEY: [{ key: "api_key", label: "API 키", secret: true }],
  ACCESS_SECRET: [
    { key: "access_key", label: "액세스 키", secret: false },
    { key: "secret_key", label: "시크릿 키", secret: true },
  ],
  CLIENT_CREDENTIALS: [
    { key: "client_id", label: "클라이언트 ID", secret: false },
    { key: "client_secret", label: "클라이언트 시크릿", secret: true },
  ],
  OAUTH_REFRESH: [
    { key: "client_id", label: "클라이언트 ID", secret: false },
    { key: "client_secret", label: "클라이언트 시크릿", secret: true },
    { key: "refresh_token", label: "리프레시 토큰", secret: true },
  ],
  BASIC: [
    { key: "username", label: "아이디", secret: false },
    { key: "password", label: "비밀번호", secret: true },
  ],
  CUSTOM: [],
};

export const CAPABILITY_LABEL: Record<string, string> = {
  PRODUCT_IMPORT: "상품 가져오기",
  PRODUCT_EXPORT: "상품 내보내기",
  SOURCE_INFO_IMPORT: "소싱 정보 가져오기",
  SOURCE_URL_IMPORT: "소싱 링크 가져오기",
  SOURCE_COST_IMPORT: "매입가 가져오기",
  ORDER_IMPORT: "주문 가져오기",
  CLAIM_IMPORT: "클레임 가져오기",
  INVENTORY_IMPORT: "재고 가져오기",
  PURCHASE_IMPORT: "발주 가져오기",
  PRICE_IMPORT: "가격 가져오기",
  PRICE_EXPORT: "가격 내보내기",
};

export interface ExternalSystem {
  id: string;
  code: string;
  name: string;
  system_type: SystemType;
  supports_oauth: boolean;
  supports_api_key: boolean;
  supports_webhook: boolean;
  documentation_url: string | null;
  /** false 면 아직 구현되지 않았다. 카탈로그에 있는 것과 다르다. */
  connector_available: boolean;
  capabilities: Record<string, boolean>;
}

export interface ExternalAccount {
  id: string;
  account_code: string;
  display_name: string;
  external_account_identifier: string | null;
  status: AccountStatus;
  last_connected_at: string | null;
  last_successful_sync_at: string | null;
  /**
   * 프로그램이 인증할 수 있는가 = **무인 동기화가 가능한가**.
   *
   * 비밀도, 암호문도, 저장소 참조도 내려오지 않는다. 이 불리언 하나뿐이다.
   */
  credential_configured: boolean;
  /**
   * 사람이 찾아갈 쪽지(Bitwarden 항목 등)가 연결돼 있는가.
   *
   * `credential_configured` 와 **다른 것이다.** 쪽지에는 값이 없어서 새벽
   * 배치가 열 수 없다. 하나로 합치면 왜 동기화가 계속 실패하는지 알 수 없다.
   */
  vault_note_linked: boolean;
  credential_kind: string | null;
  credential_updated_at: string | null;
  credential_updated_by: string | null;
  /** 마지막 연결 시험. 실패 사유를 구분한다 — "연결 실패" 하나로 뭉치지 않는다. */
  last_test_status: TestStatus | null;
  last_test_at: string | null;
  last_test_message: string | null;
  connector_available: boolean;
  capabilities: Record<string, boolean>;
  mapping_count: number;
  system_code: string;
  system_name: string;
  system_type: SystemType;
}

export interface EntityMapping {
  id: string;
  external_entity_type: string;
  external_entity_id: string;
  internal_entity_type: string;
  internal_entity_id: string;
  mapping_status: "ACTIVE" | "SUSPENDED" | "CONFLICT";
  first_seen_at: string;
  last_seen_at: string | null;
  account_name: string;
  system_code: string;
}

export interface SyncRun {
  id: string;
  external_account_id: string;
  sync_type: string;
  trigger: string;
  status: "RUNNING" | "SUCCESS" | "PARTIAL" | "FAILED" | "UNSUPPORTED";
  started_at: string;
  finished_at: string | null;
  /** null = 아직 안 끝났다. 0 과 다르다. */
  read_count: number | null;
  created_count: number | null;
  updated_count: number | null;
  skipped_count: number | null;
  conflict_count: number | null;
  error_count: number | null;
  error_code: string | null;
  error_message: string | null;
}

export const SYNC_STATUS_LABEL: Record<SyncRun["status"], string> = {
  RUNNING: "실행 중",
  SUCCESS: "성공",
  PARTIAL: "일부 실패",
  FAILED: "실패",
  // 실패와 구분한다 — 우리가 아직 못 하는 것이지 잘못된 게 아니다.
  UNSUPPORTED: "미지원",
};

export interface SyncConflict {
  id: string;
  sync_run_id: string;
  mapping_id: string | null;
  field_name: string;
  /** 원가 조회 권한이 없으면 서버가 지운다 — `values_masked` 로 구분한다. */
  external_value: string | null;
  internal_value: string | null;
  /** true 면 값이 없는 게 아니라 **가려진** 것이다. 둘을 같게 보이면 안 된다. */
  values_masked: boolean;
  resolution: "PENDING" | "KEPT_INTERNAL" | "TOOK_EXTERNAL" | "IGNORED";
  created_at: string;
  external_entity_id: string | null;
  internal_entity_type: string | null;
  internal_entity_name: string | null;
  account_name: string | null;
}

export const externalApi = {
  systems: () => api.get<ListResponse<ExternalSystem>>("/external/systems"),

  accounts: () => api.get<ListResponse<ExternalAccount>>("/external/accounts"),

  getAccount: (id: string) => api.get<ExternalAccount>(`/external/accounts/${id}`),

  createAccount: (body: {
    external_system_id: string;
    display_name: string;
    account_code: string;
    external_account_identifier?: string | null;
  }) => api.post<ExternalAccount>("/external/accounts", body),

  /** 비활성화는 삭제가 아니다 — 과거 매핑과 동기화 기록이 남는다. */
  setStatus: (id: string, status: AccountStatus) =>
    api.patch<ExternalAccount>(`/external/accounts/${id}/status`, { status }),

  mappings: (params: { external_account_id?: string; internal_entity_id?: string } = {}) =>
    api.get<ListResponse<EntityMapping>>("/external/mappings", { query: params }),

  /** 이미 다른 내부 항목에 붙어 있으면 409 — 조용히 옮기지 않는다. */
  createMapping: (body: {
    external_account_id: string;
    external_entity_id: string;
    internal_entity_id: string;
    external_entity_type?: string;
    internal_entity_type?: string;
  }) => api.post<{ id: string; created: boolean }>("/external/mappings", body),

  remap: (mappingId: string, internalEntityId: string, reason: string) =>
    api.post<{ id: string }>(`/external/mappings/${mappingId}/remap`, {
      internal_entity_id: internalEntityId,
      reason,
    }),

  /** 커넥터가 없으면 501 이다. 성공으로 처리하지 않는다. */
  syncProducts: (accountId: string) =>
    api.post<{
      run_id: string; status: string; read_count: number;
      updated_count: number; skipped_count: number; conflict_count: number;
      error_count: number; messages: string[];
    }>(`/external/accounts/${accountId}/sync/products`, {}),

  /**
   * 무인 동기화용 인증정보 저장 (신규·교체 공통).
   *
   * **비밀이 요청 본문에 담기는 유일한 경로다.** 계정 생성·수정에 섞지 않는다 —
   * 섞으면 일반 요청이 전부 비밀 취급 대상이 된다.
   *
   * 저장한 뒤 값을 다시 읽는 경로는 없다. 잊었으면 새로 넣는다.
   */
  setCredential: (accountId: string, body: {
    credential_kind: string;
    secret: Record<string, string>;
  }) => api.put<ExternalAccount>(`/external/accounts/${accountId}/credential`, body),

  removeCredential: (accountId: string) =>
    api.delete<ExternalAccount>(`/external/accounts/${accountId}/credential`),

  /** 사람이 찾아갈 쪽지 연결. 이걸로 동기화가 되지는 않는다. */
  linkVaultNote: (accountId: string, vaultEntryId: string) =>
    api.post<ExternalAccount>(`/external/accounts/${accountId}/vault-note`, {
      vault_entry_id: vaultEntryId,
    }),

  testConnection: (accountId: string) =>
    api.post<{ status: TestStatus; message: string; account: ExternalAccount }>(
      `/external/accounts/${accountId}/test-connection`, {},
    ),

  runs: (params: { account_id?: string; limit?: number } = {}) =>
    api.get<ListResponse<SyncRun>>("/external/sync-runs", { query: params }),

  conflicts: (pendingOnly = true) =>
    api.get<ListResponse<SyncConflict>>("/external/conflicts", {
      query: { pending_only: pendingOnly },
    }),

  orderSyncStatus: () =>
    api.get<OrderSyncStatus>("/external/order-sync/status"),

  claimSyncStatus: () =>
    api.get<ClaimSyncStatus>("/external/claim-sync/status"),

  runOrderSync: (accountId?: string) =>
    api.post<ListResponse<OrderSyncOutcome>>(
      "/external/order-sync/run", undefined,
      accountId ? { query: { account_id: accountId } } : undefined,
    ),
};

/** 주문 동기화 상태. **개인정보는 오지 않는다** — 건수와 상태 토큰만이다. */
export interface OrderSyncAccountStatus {
  external_account_id: string;
  display_name: string;
  system_code: string;
  /** 자동화 대상으로 설정됐는가. "0건 정상" 과 구분해야 한다. */
  configured: boolean;
  initial_sync_from: string | null;
  overlap_days: number | null;
  blocked_by: string | null;
  /** 마지막 시도와 마지막 성공은 **다른 값**이다. 합치면 장애가 숨는다. */
  last_attempt_at: string | null;
  last_status: string | null;
  last_error_code: string | null;
  last_success_at: string | null;
  last_counts: {
    fetched: number | null; created: number | null; updated: number | null;
    skipped: number | null; rejected: number | null;
  } | null;
}

export interface OrderSyncStatus {
  enabled: boolean;
  mode: string | null;
  interval_minutes: number | null;
  last_run_at: string | null;
  /** 주문과 클레임을 한 덩어리로 보이게 하지 않는다. */
  claim_ingest_implemented: boolean;
  accounts: OrderSyncAccountStatus[];
}

/**
 * 클레임 수집 상태. 주문과 **같은 모양**으로 마지막 시도/성공을 따로 준다.
 *
 * 주문 상태를 재사용하지 않는 이유: 주문과 클레임은 다른 endpoint 이고 따로
 * 켠다(M057). 하나로 묶으면 주문이 성공한 날 클레임도 최신인 것처럼 보인다.
 */
export interface ClaimSyncAccountStatus {
  external_account_id: string;
  display_name: string;
  configured: boolean;
  initial_sync_from: string | null;
  claim_lookback_days: number | null;
  blocked_by: string | null;
  last_attempt_at: string | null;
  last_status: string | null;
  last_error_code: string | null;
  last_success_at: string | null;
}

export interface ClaimSyncStatus {
  enabled: boolean;
  mode: string | null;
  interval_minutes: number | null;
  last_run_at: string | null;
  accounts: ClaimSyncAccountStatus[];
  claims: Record<string, number>;
  revenue_impact: number;
  /** 매출은 반영했지만 반품 원가·수수료 환급은 정산서로만 알 수 있다. */
  profit_complete: boolean;
}

export interface OrderSyncOutcome {
  external_account_id: string;
  status: string;
  reason: string | null;
  fetched: number;
  created: number;
  updated: number;
  unchanged: number;
  rejected: number;
  protected: number;
  replayed: number;
}

/** 실패 사유 토큰 → 사람 말. 서버가 정한 토큰을 그대로 받는다. */
export const ORDER_SYNC_REASON_LABEL: Record<string, string> = {
  NOT_CONFIGURED: "설정 필요",
  ALREADY_RUNNING: "이미 실행 중",
  CONNECTOR_UNAVAILABLE: "연동 미구현",
  CREDENTIAL_NOT_CONFIGURED: "자격증명 없음",
  AUTH_FAILED: "인증 실패",
  RATE_LIMITED: "요청 한도 초과",
  TIMEOUT: "응답 지연",
  SERVER_ERROR: "판매처 서버 오류",
  SCHEMA_CHANGED: "응답 형식 변경",
  PAGINATION_STALLED: "페이지 이동 실패",
  RECONCILIATION_FAILED: "건수 대사 실패",
  READ_FAILED: "조회 실패",
};

/** 지원하는 기능만 추린다. 화면이 전체 목록을 나열하면 false 만 가득하다. */
export function supportedCapabilities(caps: Record<string, boolean>): string[] {
  return Object.entries(caps)
    .filter(([, ok]) => ok)
    .map(([k]) => CAPABILITY_LABEL[k] ?? k);
}
