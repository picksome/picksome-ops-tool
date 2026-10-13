/**
 * 비용 저장 오케스트레이션 — 직접 저장과 승인 요청을 한 번의 "저장" 으로 처리한다.
 *
 * **별도 파일인 이유**: `approvals.ts` 가 `expenses-approval.ts` 의 라벨을 가져다 쓴다.
 * 저장 로직을 `expenses-approval.ts` 에 두면 두 모듈이 서로를 참조해
 * 모듈 초기화 순서에 따라 라벨이 `undefined` 가 되는 순환 참조가 된다.
 *
 * 화면(page/drawer)에 두지 않는 이유: 이 순서(승인 먼저, 직접 저장 나중)는
 * 렌더링과 무관한 규칙이고 단위 테스트로 지켜야 한다.
 */

import { approvalsApi, type ApprovalRequest } from "@/lib/api/approvals";
import { expensesApi, type ExpenseOut } from "@/lib/api/expenses";
import {
  classifyProductChanges, diffAgainst, type FieldOwnership,
} from "@/lib/api/products";

export interface ExpenseSaveResult {
  approvals: ApprovalRequest[];
  /** 승인 없이 바로 저장된 필드가 있었는가 */
  directSaved: boolean;
  /** 직접 저장이 있었으면 갱신된 행. 없으면 null */
  updated: ExpenseOut | null;
}

export interface ExpenseSaveOptions {
  /** 서버가 알려준 expense 필드 소유권 표. 없으면 저장하지 않는다. */
  ownership: FieldOwnership | null;
  reason?: string;
  /** 비우면 승인 즉시 적용. 비용은 귀속일이 따로 있어 보통 비운다. */
  effectiveFrom?: string;
  submit?: boolean;
}

/**
 * 변경분을 나눠 보낸다.
 *
 * 순서가 중요하다 — **승인 요청을 먼저 만든다.** 직접 저장(메모 등)을 먼저 하면
 * 승인 요청 생성이 실패했을 때 일부만 반영된 채로 남고, 사용자는 무엇이 저장됐는지
 * 알 수 없다. 승인 요청이 먼저 실패하면 아무것도 바뀌지 않는다.
 */
export async function saveExpenseWithApproval(
  expenseId: string,
  base: Record<string, unknown>,
  next: Record<string, unknown>,
  opts: ExpenseSaveOptions,
): Promise<ExpenseSaveResult> {
  if (!opts.ownership) {
    // 화면이 저장 버튼을 막는 게 1차 방어고, 이건 그게 뚫렸을 때의 2차 방어다.
    // 소유권 표 없이 보내면 금액이 직접 저장 경로로 흘러 403 을 맞는다.
    throw new Error("승인 정책을 불러오지 못해 저장할 수 없습니다. 다시 불러온 뒤 시도해 주세요.");
  }

  const changes = diffAgainst(base, next);
  const classified = classifyProductChanges(changes, opts.ownership);

  if (classified.unroutable.length > 0) {
    throw new Error(
      `승인 요청 유형을 결정할 수 없는 항목이 있습니다: ${classified.unroutable.join(", ")}`,
    );
  }

  const approvals: ApprovalRequest[] = [];
  for (const [requestType, fields] of Object.entries(classified.approval)) {
    approvals.push(
      await approvalsApi.create({
        target_type: "expense",
        target_id: expenseId,
        request_type: requestType,
        proposed_changes: fields,
        reason: opts.reason,
        effective_from: opts.effectiveFrom,
        submit: opts.submit ?? true,
      }),
    );
  }

  const updated = classified.hasDirect
    ? await expensesApi.update(expenseId, classified.direct)
    : null;

  return { approvals, directSaved: classified.hasDirect, updated };
}
