import { api, ApiError, type ListResponse } from "@/lib/api";
import {
  EXPENSE_FIELD_LABELS, EXPENSE_REQUEST_TYPE_LABELS, EXPENSE_VALUE_LABELS,
} from "@/lib/api/expenses-approval";

/**
 * 승인 워크플로 API 클라이언트 (M018/M019).
 *
 * **이 파일은 mock 이 아니다.** 실제 백엔드 `/api/v1/approvals` 를 호출한다.
 * 어댑터 fallback 을 두지 않는다 — 승인은 권한·감사와 직결돼서
 * mock 이 실패를 가리면 안 된다.
 */

export type ApprovalStatus =
  | "DRAFT" | "PENDING" | "APPROVED" | "REJECTED" | "APPLIED" | "CANCELLED";

export type AutoApplyStatus =
  | "PENDING" | "RETRYING" | "HELD_BY_LOCK" | "PERMANENT_FAILURE" | "DONE" | null;

export interface ApprovalRequest {
  id: string;
  business_key: string;
  org_id: string;
  target_type: string;
  target_id: string;
  request_type: string;
  status: ApprovalStatus;

  requested_by: string;
  requested_at: string;
  submitted_at: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  applied_by: string | null;
  applied_at: string | null;
  cancelled_at: string | null;

  reason: string | null;
  review_comment: string | null;
  apply_error: string | null;

  before_snapshot_json: Record<string, unknown> | null;
  proposed_changes_json: Record<string, unknown> | null;
  approval_policy_snapshot_json: ApprovalPolicySnapshot | null;

  base_version: string | null;
  effective_from: string | null;
  version: number;
  idempotency_key: string | null;

  /* M019 자동 적용 추적 */
  request_fingerprint: string | null;
  attempt_count: number;
  last_attempt_at: string | null;
  last_error_code: string | null;
  last_error_message: string | null;
  next_retry_at: string | null;
  auto_apply_status: AutoApplyStatus;
  applied_by_type: "USER" | "SYSTEM" | null;

  created_at: string;
  updated_at: string;
}

export interface ApprovalPolicySnapshot {
  self_approve_allowed: boolean;
  required_roles: string[];
  thresholds: Record<string, number | null>;
  /** 임계값이 아직 설정되지 않았으면 false — 화면이 "미확정" 을 표시해야 한다 */
  thresholds_resolved: boolean;
  policy_source: string;
}

export interface ApprovalListParams {
  status?: ApprovalStatus | "";
  target_type?: string;
  target_id?: string;
  request_type?: string;
  requested_by?: string;
  reviewed_by?: string;
  mine?: boolean;
  pending_for_me?: boolean;
  q?: string;
  from?: string;
  to?: string;
  page?: number;
  page_size?: number;
}

export interface CreateApprovalBody {
  target_type: string;
  target_id: string;
  request_type: string;
  proposed_changes: Record<string, unknown>;
  reason?: string | null;
  effective_from?: string | null;
  submit?: boolean;
}

export interface RunDueResult {
  batch_id: string;
  started_at: string;
  finished_at: string | null;
  scanned: number;
  applied: number;
  already_applied: number;
  held_by_lock: number;
  validation_failed: number;
  other_failed: number;
  details: Array<{ request_id: string; business_key: string; result: string; message?: string }>;
}

export const approvalsApi = {
  /** 지원 대상 유형. 화면이 하드코딩하지 않도록 서버가 알려준다. */
  targetTypes: () => api.get<Record<string, string[]>>("/approvals/target-types"),

  list: (params: ApprovalListParams) =>
    api.get<ListResponse<ApprovalRequest>>("/approvals", {
      query: {
        status: params.status || undefined,
        target_type: params.target_type || undefined,
        target_id: params.target_id || undefined,
        request_type: params.request_type || undefined,
        requested_by: params.requested_by || undefined,
        reviewed_by: params.reviewed_by || undefined,
        mine: params.mine || undefined,
        pending_for_me: params.pending_for_me || undefined,
        q: params.q || undefined,
        from: params.from || undefined,
        to: params.to || undefined,
        page: params.page ?? 1,
        page_size: params.page_size ?? 50,
      },
    }),

  get: (id: string) => api.get<ApprovalRequest>(`/approvals/${id}`),

  create: (body: CreateApprovalBody, idempotencyKey?: string) =>
    // 재시도로 요청이 두 건 생기는 것을 막는다.
    api.post<ApprovalRequest>(
      "/approvals",
      body,
      idempotencyKey ? { headers: { "Idempotency-Key": idempotencyKey } } : undefined,
    ),

  updateDraft: (id: string, body: Partial<CreateApprovalBody>) =>
    api.patch<ApprovalRequest>(`/approvals/${id}`, body),

  submit: (id: string) => api.post<ApprovalRequest>(`/approvals/${id}/submit`),
  approve: (id: string, comment?: string) =>
    api.post<ApprovalRequest>(`/approvals/${id}/approve`, { comment }),
  reject: (id: string, comment: string) =>
    api.post<ApprovalRequest>(`/approvals/${id}/reject`, { comment }),
  cancel: (id: string, reason?: string) =>
    api.post<ApprovalRequest>(`/approvals/${id}/cancel`, { reason }),
  apply: (id: string) => api.post<ApprovalRequest>(`/approvals/${id}/apply`),

  /** 적용일 도래분 일괄 적용 (ADMIN). cron 이 없어도 수동으로 돌릴 수 있다. */
  runDue: (batchSize = 50) =>
    api.post<RunDueResult>(`/approvals/run-due?batch_size=${batchSize}`),
};

/* ────────────────────────────────────────────────────────────
 * 오류 → 사용자 안내
 *
 * "실패했습니다" 하나로 뭉치면 사용자가 무엇을 해야 할지 모른다.
 * 코드마다 **다음 행동**을 알려준다.
 * ──────────────────────────────────────────────────────────── */

export interface ApprovalErrorGuide {
  title: string;
  description: string;
  /** 목록·상세를 다시 불러와야 하는가 (409 계열) */
  shouldRefetch: boolean;
  /** 기존 요청으로 이동할 수 있으면 그 id */
  existingRequestId?: string;
}

export function describeApprovalError(err: unknown): ApprovalErrorGuide {
  if (!(err instanceof ApiError)) {
    return {
      title: "요청을 처리하지 못했습니다",
      description: "네트워크 상태를 확인한 뒤 다시 시도해 주세요.",
      shouldRefetch: false,
    };
  }
  const detail = (err.details ?? {}) as Record<string, unknown>;
  const existing = typeof detail.existing_request_id === "string" ? detail.existing_request_id : undefined;

  switch (err.code) {
    case "APPROVAL_OPEN_REQUEST_EXISTS":
      return {
        title: "이미 진행 중인 승인 요청이 있습니다",
        description:
          "이 상품에는 아직 처리되지 않은 승인 요청이 있습니다. " +
          "기존 요청을 확인한 후 적용하거나 취소해 주세요.",
        shouldRefetch: true,
        existingRequestId: existing,
      };
    case "APPROVAL_INVALID_TRANSITION":
      return {
        title: "지금은 할 수 없는 동작입니다",
        description:
          "다른 사람이 먼저 이 요청을 처리했을 수 있습니다. 최신 상태를 다시 불러옵니다.",
        shouldRefetch: true,
      };
    case "APPROVAL_SELF_APPROVAL_FORBIDDEN":
      return {
        title: "본인 요청은 승인할 수 없습니다",
        description:
          "다른 승인자에게 검토를 요청하세요. " +
          "혼자 운영하는 조직이라면 설정에서 자기 승인을 허용할 수 있습니다.",
        shouldRefetch: false,
      };
    case "APPROVAL_PERMISSION_DENIED":
      return {
        title: "권한이 없습니다",
        description: "이 동작에 필요한 역할이 없습니다. 관리자에게 문의하세요.",
        shouldRefetch: false,
      };
    case "APPROVAL_VERSION_CONFLICT":
      return {
        title: "요청 이후 대상이 변경되었습니다",
        description:
          "누군가 이 상품을 직접 수정했습니다. 변경 내용을 다시 확인하고 새 요청을 만들어 주세요.",
        shouldRefetch: true,
      };
    case "APPROVAL_TARGET_UNSUPPORTED":
      return {
        title: "아직 지원하지 않는 승인 대상입니다",
        description: "현재는 상품 변경만 승인 워크플로를 지원합니다.",
        shouldRefetch: false,
      };
    case "APPROVAL_TARGET_NOT_FOUND":
    case "NOT_FOUND":
      return {
        title: "대상을 찾을 수 없습니다",
        description: "삭제되었거나 접근 권한이 없는 대상입니다.",
        shouldRefetch: true,
      };
    case "SETTLEMENT_LOCKED":
      return {
        title: "잠긴 월이라 적용할 수 없습니다",
        description:
          "적용일이 속한 월이 마감·잠금되어 있습니다. " +
          "관리자가 월 잠금을 해제한 뒤 다시 적용해 주세요. 승인 자체는 유효합니다.",
        shouldRefetch: true,
      };
    case "IDEMPOTENCY_KEY_REUSED":
      return {
        title: "같은 요청 키로 다른 내용이 전송되었습니다",
        description: "새로고침한 뒤 다시 시도해 주세요.",
        shouldRefetch: true,
        existingRequestId: existing,
      };
    case "APPROVAL_ALREADY_APPLIED":
      return {
        title: "이미 적용된 요청입니다",
        description: "변경 내용이 이미 반영되었습니다.",
        shouldRefetch: true,
      };
    case "PRODUCT_VERSION_CONFLICT":
      return {
        title: "다른 사용자가 먼저 수정했습니다",
        description:
          "이 상품을 열어 둔 사이 다른 사람이 저장했습니다. " +
          "덮어쓰지 않도록 저장을 중단했습니다. 최신 내용을 불러온 뒤 다시 변경해 주세요.",
        shouldRefetch: true,
      };
    case "APPROVAL_REQUIRED":
      return {
        title: "승인이 필요한 항목입니다",
        description:
          "원가·상품명·상태 변경은 바로 저장할 수 없고 승인을 거쳐야 합니다. " +
          "승인 요청으로 다시 시도해 주세요.",
        shouldRefetch: true,
      };
    case "APPROVAL_EXECUTOR_ALREADY_RUNNING":
      return {
        title: "자동 적용이 이미 실행 중입니다",
        description: "앞선 실행이 끝난 뒤 다시 시도해 주세요.",
        shouldRefetch: true,
      };
    default:
      return {
        title: "요청을 처리하지 못했습니다",
        description: err.message || "잠시 후 다시 시도해 주세요.",
        shouldRefetch: err.status === 409,
      };
  }
}

/* ────────────────────────────────────────────────────────────
 * 변경 전후 비교
 * ──────────────────────────────────────────────────────────── */

/** 서버가 관리하는 필드 — 화면에 노출하지 않는다 (백엔드 SERVER_MANAGED_FIELDS 와 동기). */
const SERVER_MANAGED = new Set([
  "id", "org_id", "business_key", "created_at", "created_by",
  "updated_at", "updated_by", "deleted_at", "deleted_by", "version",
  // `status` 는 제외 — 상품 상태는 정당한 승인 대상이다(STATUS_CHANGE).
  "profit_status", "close_state",
  "reviewed_by", "reviewed_at", "applied_by", "applied_at",
  "decision_outcome", "auto_apply_status", "applied_by_type",
]);

/**
 * 요청 유형 한글 라벨. 사용자에게 `COST_CHANGE` 를 그대로 보여줄 수는 없다.
 *
 * **승인 대상이 늘어나면 여기에도 추가한다.** 승인함은 대상을 가리지 않고 한 목록에
 * 섞어 보여주기 때문에, 비용 요청 유형이 빠져 있으면 비용 승인 행만
 * `EXPENSE_FINANCIAL_CHANGE` 라는 날것으로 뜬다(실제로 그랬다).
 */
export const REQUEST_TYPE_LABELS: Record<string, string> = {
  PRODUCT_INFO_CHANGE: "상품 정보 변경",
  COST_CHANGE: "원가 변경",
  STATUS_CHANGE: "상태 변경",
  CHANNEL_PRICE_CHANGE: "판매처 가격 변경",
  ...EXPENSE_REQUEST_TYPE_LABELS,
};

/** 승인 대상 유형 라벨. 화면마다 각자 들고 있으면 한쪽만 갱신된다. */
export const TARGET_TYPE_LABELS: Record<string, string> = {
  product: "상품",
  listing: "판매처 가격",
  expense: "사업비용",
  // 첨부 삭제는 비용과 **다른 대상**이다 — 지우는 것은 첨부 한 건이지
  // 비용 전체가 아니다(target_id 가 첨부 id).
  expense_attachment: "증빙 파일",
};

/** product 필드 한글 라벨. 대상이 늘면 target_type 별 맵으로 확장한다. */
export const PRODUCT_FIELD_LABELS: Record<string, string> = {
  name_ko: "상품명",
  cost_amount: "원가",
  cost_currency: "매입 통화",
  cost_fx_rate: "환율",
  cost_amount_foreign: "현지 매입가",
  category_path: "카테고리",
  supply_url: "소스 상품 URL",
  memo: "메모",
  status: "상품 상태",
};

/** listing(판매처 가격) 필드 한글 라벨. */
export const LISTING_FIELD_LABELS: Record<string, string> = {
  regular_price: "정상가",
  sale_price: "판매가",
  sale_start_at: "할인 시작",
  sale_end_at: "할인 종료",
  currency: "통화",
  product_variant_id: "옵션",
  effective_from: "적용 시작일",
  effective_to: "적용 종료일",
};

export const FIELD_LABELS_BY_TARGET: Record<string, Record<string, string>> = {
  product: PRODUCT_FIELD_LABELS,
  listing: LISTING_FIELD_LABELS,
  expense: EXPENSE_FIELD_LABELS,
};

/** 금액 필드 — 원화 포맷으로 표시한다. */
const MONEY_FIELDS = new Set([
  "cost_amount", "cost_amount_foreign",
  "regular_price", "sale_price",
  // 비용 3종. 승인자가 봐야 할 것은 `185000` 이 아니라 `185,000원` 이다.
  "gross_amount", "supply_amount", "vat_amount",
]);
/** 날짜 필드 — KST 포맷. */
const DATE_FIELDS = new Set([
  "cost_effective_from", "price_effective_from",
  "effective_from", "effective_to",
  "sale_start_at", "sale_end_at",
  // 비용 귀속일. 이게 바뀌면 두 달의 순이익이 모두 바뀐다.
  "expense_date",
]);

/**
 * 다른 행을 가리키는 UUID 필드.
 *
 * 그대로 두면 승인자에게 `9d3f…-a41` 이 보인다. 그건 무엇이 바뀌는지 알려주지
 * 않는다. 화면이 이름을 아는 만큼 `resolve` 로 바꿔 넣고, 모르면 "다른 항목" 이라고
 * 말한다 — **UUID 를 사람에게 보여주지 않는다**(D-283).
 */
const REF_FIELDS = new Set([
  "category_id", "payment_card_holder_id", "attributable_partner_id",
  "supplier_id", "product_variant_id",
]);

/** enum 값 라벨. 필드명 → 값 → 라벨. 도메인 파일이 소유하고 여기서 합친다. */
export const VALUE_LABELS: Record<string, Record<string, string>> = {
  ...EXPENSE_VALUE_LABELS,
  // 상품 상태 — 비용의 evidence_status 와 필드명이 겹치지 않는다.
  status: { DRAFT: "임시", ACTIVE: "판매중", PAUSED: "중지", ARCHIVED: "보관" },
};

export interface DiffRow {
  field: string;
  label: string;
  before: unknown;
  after: unknown;
  changed: boolean;
  kind: "money" | "date" | "url" | "ref" | "text";
  /** enum 값 라벨표. 없으면 값을 그대로 보여준다. */
  valueLabels?: Record<string, string>;
}

/** 참조 id → 사람이 읽는 이름. 화면이 아는 목록으로 만들어 넘긴다. */
export type RefResolver = (field: string, id: string) => string | undefined;

/**
 * before/proposed → 필드별 비교 행.
 *
 * **JSON 을 그대로 노출하지 않는다.** 사용자는 컬럼명이 아니라 무엇이 어떻게 바뀌는지를 봐야 한다.
 */
export function buildDiffRows(
  targetType: string,
  before: Record<string, unknown> | null,
  proposed: Record<string, unknown> | null,
  resolve?: RefResolver,
): DiffRow[] {
  const b = before ?? {};
  const p = proposed ?? {};
  const labels = FIELD_LABELS_BY_TARGET[targetType] ?? {};
  const keys = Array.from(new Set([...Object.keys(b), ...Object.keys(p)]))
    .filter((k) => !SERVER_MANAGED.has(k))
    .sort();

  return keys.map((field) => {
    const raw = (v: unknown) =>
      // 참조 필드는 이름으로 바꿔서 담는다. 못 찾으면 null 이 아니라 원값을 남긴다 —
      // null 로 만들면 "없음" 이 되어 값이 지워진 것처럼 보인다.
      REF_FIELDS.has(field) && typeof v === "string"
        ? resolve?.(field, v) ?? "다른 항목"
        : v;
    const beforeVal = raw(b[field] ?? null);
    const afterVal = raw(field in p ? p[field] ?? null : b[field] ?? null);
    const kind: DiffRow["kind"] = MONEY_FIELDS.has(field)
      ? "money"
      : DATE_FIELDS.has(field)
        ? "date"
        : field.toLowerCase().includes("url")
          ? "url"
          : REF_FIELDS.has(field)
            ? "ref"
            : "text";
    return {
      field,
      label: labels[field] ?? field,
      before: beforeVal,
      after: afterVal,
      changed: field in p && afterVal !== beforeVal,
      kind,
      valueLabels: VALUE_LABELS[field],
    };
  });
}
