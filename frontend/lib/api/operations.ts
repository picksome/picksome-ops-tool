/**
 * 운영 이슈 센터 + worker 상태 API 클라이언트 (Phase 35~38).
 *
 * 이슈는 대시보드 경고를 **처리 가능한 업무**로 만든 것이다.
 * 스캐너는 사람이 닫은 이슈를 다시 열지 않는다 — 그래서 화면이
 * "무시함" 상태를 신뢰할 수 있다.
 */

import { api, type ListResponse } from "@/lib/api";

export type IssueSeverity = "BLOCKING" | "WARNING" | "INFO";
export type IssueStatus =
  | "OPEN"
  | "IN_PROGRESS"
  | "WAITING"
  | "RESOLVED"
  | "IGNORED";

export const ISSUE_STATUS_LABEL: Record<IssueStatus, string> = {
  OPEN: "미처리",
  IN_PROGRESS: "처리중",
  WAITING: "대기",
  RESOLVED: "해결",
  IGNORED: "무시",
};

export const ISSUE_SEVERITY_LABEL: Record<IssueSeverity, string> = {
  BLOCKING: "차단",
  WARNING: "경고",
  INFO: "참고",
};

export interface OperationIssue {
  id: string;
  issue_type: string;
  severity: IssueSeverity;
  entity_type: string | null;
  entity_id: string | null;
  year_month: string | null;
  dedupe_key: string;
  title: string;
  description: string | null;
  affected_count: number;
  status: IssueStatus;
  assigned_to: string | null;
  assigned_to_name: string | null;
  due_date: string | null;
  blocks_month_close: boolean;
  auto_resolve: boolean;
  first_detected_at: string;
  last_detected_at: string;
  resolved_at: string | null;
  resolution_note: string | null;
  is_overdue: boolean;
}

export interface IssueSummary {
  open_total: number;
  blocking: number;
  warning: number;
  info: number;
  overdue: number;
  unassigned: number;
  by_type: { issue_type: string; severity: IssueSeverity; n: number }[];
  last_scan: { started_at: string; finished_at: string | null; status: string } | null;
}

/**
 * worker 상태.
 *
 * **화면이 임계값을 만들지 않는다.** "몇 시간 넘으면 오류" 같은 판단은 서버가 하고
 * (`services/worker_runtime.derive_state`), 화면은 그 코드를 번역만 한다.
 * 화면마다 다른 기준을 세우면 같은 상태가 어떤 화면에선 정상, 어떤 화면에선 오류가 된다.
 */
export type WorkerState =
  | "NEVER_RAN"
  | "RUNNING"
  | "STALE_RUNNING"
  | "LAST_RUN_FAILED"
  | "PENDING_QUEUE"
  | "IDLE";

export const WORKER_STATE_LABEL: Record<WorkerState, string> = {
  NEVER_RAN: "실행된 적 없음",
  RUNNING: "실행 중",
  STALE_RUNNING: "중단 추정",
  LAST_RUN_FAILED: "마지막 실행 실패",
  PENDING_QUEUE: "대기 건 있음",
  IDLE: "정상",
};

/** 사람이 읽는 worker 이름. 코드를 그대로 보여주지 않는다. */
export const WORKER_LABEL: Record<string, string> = {
  due_approvals: "승인 자동 적용",
  attachment_delete: "증빙 파일 삭제",
  channel_publish: "판매처 가격 반영",
  period_recalc: "기간 재계산",
  operation_issue_scan: "운영 이슈 스캔",
};

export function workerLabel(name: string): string {
  return WORKER_LABEL[name] ?? name;
}

export interface WorkerHealth {
  worker_name: string;
  last_started_at: string | null;
  last_success_at: string | null;
  last_failure_at: string | null;
  running_now: number;
  stale_running: number;
  processed_total: number;
  success_total: number;
  failure_total: number;
  /** 오류 코드와 한 줄 메시지만. stack trace·SQL 은 서버가 저장조차 하지 않는다. */
  last_error_code: string | null;
  last_error_message: string | null;
  queue_depth: number;
  oldest_pending_age_seconds: number | null;
  never_ran: boolean;
  /** 대기 큐 개념이 있는가. 없으면 "대기 0건" 이 좋은 소식도 나쁜 소식도 아니다. */
  has_queue: boolean;
  /** 서버가 판단한 상태. 화면이 다시 계산하지 않는다. */
  state: WorkerState;
}

export interface WorkerRun {
  id: string;
  org_id: string | null;
  worker_name: string;
  trigger: string;
  status: "RUNNING" | "SUCCESS" | "FAILED";
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  processed_count: number | null;
  success_count: number | null;
  failure_count: number | null;
  skipped_count: number | null;
  error_code: string | null;
  error_message: string | null;
}

export interface WorkerRunResult {
  worker: string;
  status: "SUCCESS" | "FAILED";
  run_id?: string;
  duration_ms?: number;
  processed?: number;
  success?: number;
  failure?: number;
  skipped?: number;
  error_code: string | null;
  error_message: string | null;
}

export interface IssueListParams {
  /** FastAPI 가 배열 쿼리를 `?status=A&status=B` 로 받는다. */
  status?: IssueStatus[];
  severity?: IssueSeverity[];
  issue_type?: string;
  assigned_to?: string;
  year_month?: string;
  keyword?: string;
  open_only?: boolean;
  overdue_only?: boolean;
  limit?: number;
  offset?: number;
}

type QueryValue =
  | string
  | number
  | boolean
  | null
  | undefined
  | readonly (string | number)[];

/** 빈 배열은 보내지 않는다 — `?status=` 로 나가면 FastAPI 가 빈 문자열로 받는다. */
function toQuery(params: IssueListParams): Record<string, QueryValue> {
  const out: Record<string, QueryValue> = {};
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) {
      if (value.length) out[key] = value;
      continue;
    }
    out[key] = value as QueryValue;
  }
  return out;
}

export const operationsApi = {
  listIssues: (params: IssueListParams = {}) =>
    api.get<ListResponse<OperationIssue>>("/operations/issues", {
      query: toQuery(params),
    }),

  issueSummary: () => api.get<IssueSummary>("/operations/issues/summary"),

  getIssue: (id: string) => api.get<OperationIssue>(`/operations/issues/${id}`),

  updateIssue: (
    id: string,
    body: {
      status?: IssueStatus;
      assigned_to?: string | null;
      clear_assignee?: boolean;
      due_date?: string | null;
      /** IGNORED 로 바꾸려면 필수다. 없으면 422 로 거부된다. */
      resolution_note?: string;
    },
  ) => api.patch<OperationIssue>(`/operations/issues/${id}`, body),

  createIssue: (body: {
    issue_type: string;
    severity: IssueSeverity;
    title: string;
    description?: string;
    year_month?: string;
    assigned_to?: string;
    due_date?: string;
  }) => api.post<OperationIssue>("/operations/issues", body),

  scan: () => api.post<{ processed: number; success: number; summary: unknown }>(
    "/operations/issues/scan",
    {},
  ),

  workers: () =>
    api.get<{ workers: WorkerHealth[]; stale_run_threshold_minutes: number }>(
      "/operations/workers",
    ),

  workerRuns: (params: { worker_name?: string; limit?: number } = {}) =>
    api.get<ListResponse<WorkerRun>>("/operations/workers/runs", { query: params }),

  /**
   * worker 한 번 실행 (ADMIN 전용).
   *
   * 서버는 CLI 와 **같은 어댑터**를 쓴다 — cron 이 하는 일과 이 버튼이 하는 일이
   * 다르면 안 된다. 실패해도 500 이 아니라 `status: "FAILED"` 로 돌아온다.
   */
  runWorker: (name: string) =>
    api.post<WorkerRunResult>(`/operations/workers/${name}/run`, {}),

  reclaimStale: () =>
    api.post<{ reclaimed: number }>("/operations/workers/reclaim-stale", {}),
};
