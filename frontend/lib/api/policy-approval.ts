/**
 * 재무 정책 저장 — 승인 대상과 직접 수정을 나눈다 (Phase A-2).
 *
 * 왜 이 파일이 있나
 * ---------------
 * 세율·수수료·배분 비율은 마진 → 순이익 → 수익배분으로 그대로 이어진다.
 * 그래서 백엔드가 일반 `PATCH /policies/{kind}/{id}` 로는 그 필드들을 못 바꾸게
 * 막았다(403 `APPROVAL_REQUIRED`).
 *
 * 화면이 그걸 모르면 사용자는 저장 버튼을 눌렀는데 403 만 보게 된다.
 * 여기서 **한 번의 저장을 두 갈래로 나눈다**:
 *
 * ```
 * 메모·근거 링크          → PATCH          (즉시 반영)
 * 세율·수수료·비율        → 승인 요청 생성  (승인돼야 새 version 이 생긴다)
 * ```
 *
 * 승인되면 기존 행을 고치는 게 아니라 새 version 이 만들어진다.
 * 그래서 이미 확정한 달의 계산 근거는 그대로 남는다.
 *
 * **목록은 백엔드와 같아야 한다** (`services/approval_policy.FINANCIAL_POLICIES`).
 * 어긋나면 화면이 직접 보낸 필드가 403 으로 튕기거나, 승인이 필요 없는 필드에
 * 승인을 요구하게 된다. 계약 테스트가 이 목록을 고정한다.
 */

import { policiesApi, type PolicyKind } from "./policies";
import { approvalsApi } from "./approvals";

/** 정책 종류 → 승인 없이 바꿀 수 없는 필드. 백엔드 `FINANCIAL_POLICIES` 와 같다. */
export const APPROVAL_FIELDS: Partial<Record<PolicyKind, readonly string[]>> = {
  tax: [
    "tax_type", "business_type", "vat_rate", "calculation_method",
    "default_vat_inclusion", "applies_to",
    "input_vat_credit_policy", "import_vat_credit_policy",
  ],
  marketplace_fee: [
    "marketplace", "category_path", "fee_type",
    "rate", "fixed_amount", "tier_json", "fee_vat_included",
  ],
  payment_fee: ["method", "rate"],
  partner_share: ["partner_id", "share_ratio", "applies_year_month"],
  settlement: ["loss_policy", "priority_distribution_json", "close_day"],
};

/** 정책 종류 → 승인 대상 target_type / request_type. */
export const APPROVAL_TARGET: Partial<
  Record<PolicyKind, { targetType: string; requestType: string }>
> = {
  tax: { targetType: "policy_tax", requestType: "TAX_POLICY_CHANGE" },
  marketplace_fee: {
    targetType: "policy_marketplace_fee", requestType: "FEE_POLICY_CHANGE",
  },
  payment_fee: { targetType: "policy_payment_fee", requestType: "FEE_POLICY_CHANGE" },
  partner_share: {
    targetType: "policy_partner_share", requestType: "DISTRIBUTION_POLICY_CHANGE",
  },
  settlement: {
    targetType: "policy_settlement", requestType: "DISTRIBUTION_POLICY_CHANGE",
  },
};

/**
 * 승인 요청에 담을 수 있는 모양으로.
 *
 * 배열·객체는 JSON 문자열로 만든다 — 백엔드가 중첩 값을 거부한다.
 * 어댑터가 적용 시점에 되돌린다.
 */
function toScalar(value: unknown): unknown {
  if (value !== null && typeof value === "object") return JSON.stringify(value);
  return value;
}

export function requiresApproval(kind: PolicyKind, field: string): boolean {
  return (APPROVAL_FIELDS[kind] ?? []).includes(field);
}

export interface PolicySaveResult {
  /** 즉시 반영된 필드 */
  appliedDirectly: string[];
  /** 승인 요청으로 넘어간 필드 */
  sentForApproval: string[];
  approvalRequestId: string | null;
}

/**
 * 정책 한 건 저장.
 *
 * `fields` 중 승인 대상만 골라 요청을 만들고, 나머지는 바로 저장한다.
 * **바뀐 값만 보낸다** — 안 바뀐 필드까지 승인에 태우면 "수수료율 변경 승인"
 * 목록이 메모만 고친 요청으로 가득 찬다.
 */
export async function savePolicyWithApproval(args: {
  kind: PolicyKind;
  policyId: string;
  /** 현재 저장된 값. 무엇이 바뀌었는지 판단하는 기준이다. */
  current: Record<string, unknown>;
  next: Record<string, unknown>;
  memo?: string | null;
  sourceUrl?: string | null;
  /** 새 version 이 유효해질 날. 생략하면 서버가 오늘로 잡는다. */
  effectiveFrom?: string;
  reason: string;
}): Promise<PolicySaveResult> {
  const { kind, policyId, current, next } = args;

  const changed = Object.keys(next).filter(
    (k) => JSON.stringify(next[k]) !== JSON.stringify(current[k]),
  );
  const needsApproval = changed.filter((k) => requiresApproval(kind, k));
  const direct = changed.filter((k) => !requiresApproval(kind, k));

  if (direct.length > 0 || args.memo !== undefined || args.sourceUrl !== undefined) {
    await policiesApi.update(kind, policyId, {
      ...(args.memo !== undefined ? { memo: args.memo } : {}),
      ...(args.sourceUrl !== undefined ? { source_url: args.sourceUrl } : {}),
      ...(direct.length > 0
        ? { fields: Object.fromEntries(direct.map((k) => [k, next[k]])) }
        : {}),
    });
  }

  if (needsApproval.length === 0) {
    return { appliedDirectly: direct, sentForApproval: [], approvalRequestId: null };
  }

  const target = APPROVAL_TARGET[kind];
  if (!target) {
    throw new Error(`승인 대상이 아닌 정책 종류입니다: ${kind}`);
  }

  const request = await approvalsApi.create({
    target_type: target.targetType,
    target_id: policyId,
    request_type: target.requestType,
    reason: args.reason,
    effective_from: args.effectiveFrom ?? null,
    // **평면 dict 다.** 중첩을 허용하면 어떤 컬럼이 바뀌는지 정적으로 알 수 없고
    // 변경 전후 비교 UI 도 만들 수 없다(backend `ProposedChanges`).
    // 구간 목록처럼 값 자체가 배열인 것은 JSON 문자열로 보낸다.
    proposed_changes: Object.fromEntries(
      needsApproval.map((k) => [k, toScalar(next[k])]),
    ),
    submit: true,
  });

  return {
    appliedDirectly: direct,
    sentForApproval: needsApproval,
    approvalRequestId: request.id,
  };
}

/** 이 정책에 아직 처리되지 않은 승인 요청이 있는가. 화면이 중복 요청을 막는다. */
export async function pendingApprovalFor(
  kind: PolicyKind, policyId: string,
): Promise<boolean> {
  const target = APPROVAL_TARGET[kind];
  if (!target) return false;
  const res = await approvalsApi.list({
    target_type: target.targetType,
    target_id: policyId,
    status: "PENDING",
  });
  return res.total > 0;
}
