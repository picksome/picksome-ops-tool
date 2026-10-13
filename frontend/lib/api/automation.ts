/**
 * 자동화 · AI 접근 API 클라이언트 (M044/M045).
 *
 * 두 가지가 섞이지 않게 한다
 * -----------------------
 * ```
 * 자동화   Seller OS 가 스스로 하는 일        AI 없이 돈다
 * AI·API   외부 에이전트가 Seller OS 를 읽음   선택 기능
 * ```
 *
 * 화면도 설정도 나눈다. 합치면 "AI 를 안 켜면 자동화도 안 되나" 로 읽힌다.
 */

import { api, type ListResponse } from "@/lib/api";
import type { ExceptionCategorySummary } from "@/lib/api/exceptions";

export type AutomationMode = "REPORT_ONLY" | "PROPOSE" | "AUTO_EXECUTE";

export const MODE_LABEL: Record<AutomationMode, string> = {
  REPORT_ONLY: "관찰만",
  PROPOSE: "승인 요청 만들기",
  AUTO_EXECUTE: "자동 실행",
};

export const MODE_HELP: Record<AutomationMode, string> = {
  REPORT_ONLY: "찾기만 하고 아무것도 바꾸지 않습니다.",
  PROPOSE: "승인 요청을 만듭니다. 사람이 승인해야 반영됩니다.",
  AUTO_EXECUTE: "허용 범위 안에서 직접 실행합니다. 범위를 정하지 않으면 제안까지만 합니다.",
};

/** 실행 결과. `BLOCKED` 를 성공·실패와 뭉치지 않는다. */
export type ActionOutcome =
  | "REPORTED" | "PROPOSED" | "EXECUTED" | "BLOCKED" | "FAILED";

export const OUTCOME_LABEL: Record<ActionOutcome, string> = {
  REPORTED: "찾음",
  PROPOSED: "승인 요청함",
  EXECUTED: "실행함",
  // "아무 일도 없었다" 와 다르다 — 하려다 한도에 걸린 것이다.
  BLOCKED: "한도로 중단",
  FAILED: "실패",
};

export const RUN_STATUS_LABEL: Record<string, string> = {
  RUNNING: "실행 중",
  SUCCESS: "완료",
  PARTIAL: "일부만 처리",
  FAILED: "실패",
  // 꺼져 있어서 안 돈 것. 실패가 아니다.
  SKIPPED: "실행 안 함",
};

/** 한도 이름 → 사람이 읽는 말. 서버가 아는 키와 같아야 한다. */
export const GUARDRAIL_LABEL: Record<string, string> = {
  min_margin_rate: "최소 마진율",
  min_diff_rate: "최소 차이율",
  max_change_rate: "최대 변경률",
  max_change_amount: "최대 변경 금액",
  max_actions_per_run: "한 번에 처리할 최대 건수",
  daily_limit: "하루 최대 건수",
  // 외부에 수정일이 없어 매번 최근 구간을 통째로 다시 읽는다. 그 폭이다.
  overlap_days: "겹쳐 읽을 일수",
  // 주문의 `overlap_days` 와 뜻이 다르다. 판매처가 클레임을 찾을 때 쓰는
  // 날짜가 클레임 날짜가 아니라 **원 주문 날짜**라서, "최근 며칠" 이 아니라
  // "반품이 아직 들어올 수 있는 주문 기간" 이다.
  claim_lookback_days: "클레임 조회 기간",
  velocity_days: "판매 속도 관찰 기간",
  coverage_days: "확보할 재고 일수",
};

/** 한도별 보충 설명. 숫자 칸만 있으면 무엇을 넣어야 할지 모른다. */
export const GUARDRAIL_HELP: Record<string, string> = {
  overlap_days:
    "판매처에 수정일이 없어 매번 최근 며칠을 다시 읽습니다. 최대 10일입니다.",
  claim_lookback_days:
    "며칠 전 주문까지 취소·반품을 찾아볼지 정합니다. 판매처가 클레임을 "
    + "'클레임 날짜' 가 아니라 '원 주문 날짜' 로 찾기 때문에, 이 값보다 오래된 "
    + "주문의 반품은 자동으로 보이지 않습니다. 넓힐수록 요청이 크게 늘어납니다.",
  velocity_days: "판매 속도를 며칠 치로 볼지 정합니다.",
  coverage_days: "발주로 며칠 치를 확보할지 정합니다.",
};

/** 비율 한도인가. 화면이 %로 보여줄지 정하는 데 쓴다. */
export function isRate(key: string): boolean {
  return key.endsWith("_rate");
}

export interface AutomationType {
  automation_type: string;
  label: string;
  description: string;
  modes: AutomationMode[];
  guardrails: string[];
}

export interface AutomationPolicy {
  /** null 이면 아직 만들지 않은 유형이다 — 꺼진 상태로 보여준다. */
  id: string | null;
  automation_type: string;
  name: string;
  label: string;
  description: string | null;
  enabled: boolean;
  mode: AutomationMode;
  interval_minutes: number | null;
  guardrail_json: Record<string, string>;
  scope_json: Record<string, unknown>;
  last_run_at: string | null;
  supported_modes: AutomationMode[];
  available_guardrails: string[];
}

export interface AutomationRun {
  id: string;
  policy_id: string;
  automation_type: string;
  name: string;
  mode: AutomationMode;
  trigger: string;
  status: string;
  /** null = 아직 안 끝났다. 0 과 다르다. */
  evaluated_count: number | null;
  action_count: number | null;
  skipped_count: number | null;
  started_at: string;
  finished_at: string | null;
  error_code: string | null;
  outcomes: Record<string, number>;
}

export interface AutomationAction {
  action_type: string;
  target_table: string | null;
  target_id: string | null;
  outcome: ActionOutcome;
  reason: string | null;
  /** 한도에 걸렸다면 어느 한도인지. */
  blocked_by: string | null;
  approval_request_id: string | null;
  operation_issue_id: string | null;
  created_at: string;
}

export interface NeedsAttention {
  kind: string;
  severity: "BLOCKING" | "WARNING" | "INFO";
  count: number;
  message: string;
  link: string;
}

export interface DailyReport {
  report_date: string;
  generated_at: string;
  /** 맨 위에 오는 것 — 오늘 사람이 봐야 할 일. */
  needs_attention: NeedsAttention[];
  /**
   * 예외 센터와 **같은 읽기 모델**에서 온 숫자.
   *
   * 리포트가 따로 세지 않는다 — 두 화면이 다른 숫자를 말하면 사용자는 둘 다
   * 믿지 않게 된다. 서버(`daily_report._exception_summary`)가 예외 센터를
   * 호출한 결과다.
   *
   * 조회에 실패하면 `unavailable` 이다. 0 이 아니다.
   */
  exception_summary?: {
    total?: number;
    by_severity?: Record<string, number>;
    categories?: ExceptionCategorySummary[];
    unavailable?: boolean;
    reason?: string;
  };
  sections: Record<string, Record<string, unknown>>;
}

export const automationApi = {
  types: () => api.get<{ types: AutomationType[];
    never_automated: { action: string; reason: string }[] }>("/automation/types"),

  policies: () => api.get<ListResponse<AutomationPolicy>>("/automation/policies"),

  savePolicy: (body: {
    automation_type: string;
    enabled: boolean;
    mode: AutomationMode;
    interval_minutes?: number | null;
    guardrails?: Record<string, string>;
  }) => api.put<AutomationPolicy>("/automation/policies", body),

  /** 지금 한 번. 꺼진 정책도 눌러볼 수 있다 — 켜기 전에 무엇이 잡히는지 본다. */
  run: (policyId: string) =>
    api.post<{ run_id: string; status: string; outcomes: Record<string, number> }>(
      `/automation/policies/${policyId}/run`, {},
    ),

  runs: (params: { policy_id?: string; limit?: number } = {}) =>
    api.get<ListResponse<AutomationRun>>("/automation/runs", { query: params }),

  actions: (runId: string) =>
    api.get<ListResponse<AutomationAction>>(`/automation/runs/${runId}/actions`),

  dailyReport: (reportDate?: string) =>
    api.get<DailyReport>("/automation/daily-report", {
      query: { report_date: reportDate },
    }),
};

// ── AI · API ────────────────────────────────────────────────────

export interface ApiTokenSummary {
  id: string;
  token_prefix: string;
  label: string | null;
  scopes: string[];
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  /** 살아 있는가. 폐기와 만료를 구분해서도 준다. */
  active: boolean;
  revoked: boolean;
  expired: boolean;
}

export interface ServiceAccount {
  id: string;
  name: string;
  description: string | null;
  status: "ACTIVE" | "DISABLED";
  last_used_at: string | null;
  created_at: string;
  tokens: ApiTokenSummary[];
  /** 0 이면 이 계정은 아무것도 할 수 없다 — 화면이 말해야 한다. */
  active_token_count: number;
}

/** 발급 응답. **`token` 은 이때 한 번만 존재한다.** */
export interface IssuedToken {
  id: string;
  token_prefix: string;
  scopes: string[];
  label: string | null;
  expires_at: string | null;
  token: string;
  warning: string;
}

export const SCOPE_LABEL: Record<string, string> = {
  "products:read": "상품 조회",
  "sourcing:read": "소싱 조회",
  "cost:read": "원가·마진 조회",
  "sales:read": "판매 조회",
  "orders:read": "주문 조회",
  "settlements:read": "정산 조회",
  "expenses:read": "비용 조회",
  "cashflow:read": "현금흐름 조회",
  "issues:read": "운영 이슈 조회",
  "reports:read": "리포트 조회",
  "approvals:create": "승인 요청 만들기",
};

export const aiAccessApi = {
  scopes: () => api.get<{
    read: string[]; propose: string[];
    forbidden: { scope: string; reason: string }[];
  }>("/ai-access/scopes"),

  accounts: () => api.get<ListResponse<ServiceAccount>>("/ai-access/service-accounts"),

  createAccount: (body: { name: string; description?: string | null }) =>
    api.post<ServiceAccount>("/ai-access/service-accounts", body),

  setStatus: (id: string, status: "ACTIVE" | "DISABLED") =>
    api.patch<ServiceAccount>(`/ai-access/service-accounts/${id}/status`, { status }),

  /** ⚠️ 응답의 `token` 은 다시 볼 수 없다. */
  issueToken: (accountId: string, body: {
    scopes: string[]; label?: string | null; expires_at?: string | null;
  }) => api.post<IssuedToken>(
    `/ai-access/service-accounts/${accountId}/tokens`, body,
  ),

  revokeToken: (tokenId: string) =>
    api.delete<{ revoked: boolean }>(`/ai-access/tokens/${tokenId}`),

  mcpStatus: () => api.get<{ enabled: boolean; note: string }>("/ai-access/mcp-status"),
};
