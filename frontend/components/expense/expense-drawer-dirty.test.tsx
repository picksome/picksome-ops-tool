/**
 * 배경 refetch 가 사용자의 입력을 덮지 않는다.
 *
 * 왜 이 테스트가 있나 — 실제로 덮고 있었다
 * ------------------------------------
 * drawer 의 초기화 effect 가 `expense` 를 의존성에 두고 있었다.
 *
 * ```js
 * React.useEffect(() => {
 *   setDraft(draftOf(expense));   // ← 사용자가 입력한 값을 덮어쓴다
 *   setDirty(false);
 * }, [open, expense, expenseId, isEdit]);
 * ```
 *
 * React Query 기본값은 `refetchOnWindowFocus: true` 이고 이 앱의 `staleTime` 은
 * 30초다. 즉 **비용을 30초 넘게 입력하다가 다른 창을 봤다 돌아오면 입력이
 * 사라진다.** 카드 명세서를 보면서 입력하는 화면이라 정확히 그 상황이 자주 온다.
 *
 * E2E 의 간헐 timeout 도 같은 뿌리였다(docs/61 §7).
 *
 * 규칙
 * ----
 * ```
 * 사용자가 손댄 값(dirty)   → 서버 응답이 와도 유지한다
 * 손대지 않은 값            → 최신 서버 값으로 갱신해도 된다
 * 대상 행이 바뀌면          → 새로 초기화한다
 * ```
 */

import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ExpenseDrawer } from "./expense-drawer";

const getExpense = vi.fn();
const lockState = vi.fn();
const adjustments = vi.fn();

vi.mock("@/lib/api/expenses", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/expenses")>("@/lib/api/expenses");
  return {
    ...actual,
    expensesApi: {
      ...actual.expensesApi,
      get: (...a: unknown[]) => getExpense(...a),
      fieldOwnership: async () => ({
        expense: {
          approval_required: ["gross_amount", "expense_date"],
          direct: ["memo", "description"],
        },
      }),
    },
  };
});

vi.mock("@/lib/api/expenses-approval", async () => {
  const actual = await vi.importActual<
    typeof import("@/lib/api/expenses-approval")
  >("@/lib/api/expenses-approval");
  return {
    ...actual,
    expenseApprovalApi: {
      ...actual.expenseApprovalApi,
      lockState: (...a: unknown[]) => lockState(...a),
      adjustments: (...a: unknown[]) => adjustments(...a),
    },
  };
});

vi.mock("@/hooks/useExpenseRefs", () => ({
  useExpenseRefs: () => ({ activePartners: [], partners: [], categories: [] }),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));

const EXPENSE_ID = "11111111-1111-1111-1111-111111111111";

function serverExpense(over: Record<string, unknown> = {}) {
  return {
    id: EXPENSE_ID,
    expense_date: "2026-08-01",
    category_id: null,
    description: "카드 결제",
    gross_amount: 120_000,
    supply_amount: 109_091,
    vat_amount: 10_909,
    vat_inclusion_type: "INCLUSIVE",
    payment_method: "CARD",
    memo: "서버 메모",
    evidence_type: null,
    evidence_status: "MISSING",
    is_personal_card: false,
    payment_card_holder_id: null,
    attributable_partner_id: null,
    supplier_id: null,
    updated_at: "2026-08-10T00:00:00Z",
    ...over,
  };
}

function renderDrawer(qc: QueryClient) {
  return render(
    <QueryClientProvider client={qc}>
      <ExpenseDrawer expenseId={EXPENSE_ID} open onOpenChange={() => {}} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getExpense.mockReset();
  getExpense.mockResolvedValue(serverExpense());
  lockState.mockResolvedValue({ requires_adjustment: false });
  adjustments.mockResolvedValue([]);
});

describe("배경 refetch 와 사용자 입력", () => {
  it("입력 중에 refetch 가 와도 값이 살아남는다", async () => {
    const user = userEvent.setup();
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 0 } },
    });
    renderDrawer(qc);

    const memo = await screen.findByLabelText(/메모/);
    await user.clear(memo);
    await user.type(memo, "내가 입력한 메모");
    expect(memo).toHaveValue("내가 입력한 메모");

    // 창을 다녀오면 이런 일이 일어난다 — 서버가 **같은 값**을 새 객체로 준다.
    getExpense.mockResolvedValue(serverExpense());
    await qc.refetchQueries({ queryKey: ["expense", EXPENSE_ID] });

    await waitFor(() => expect(getExpense).toHaveBeenCalledTimes(2));
    // 예전에는 여기서 "서버 메모" 로 되돌아갔다.
    expect(memo).toHaveValue("내가 입력한 메모");
  });

  it("여러 번 refetch 가 와도 유지된다", async () => {
    const user = userEvent.setup();
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 0 } },
    });
    renderDrawer(qc);

    const memo = await screen.findByLabelText(/메모/);
    await user.clear(memo);
    await user.type(memo, "A");
    await qc.refetchQueries({ queryKey: ["expense", EXPENSE_ID] });
    await waitFor(() => expect(getExpense).toHaveBeenCalledTimes(2));

    await user.type(memo, "B");
    await qc.refetchQueries({ queryKey: ["expense", EXPENSE_ID] });
    await waitFor(() => expect(getExpense).toHaveBeenCalledTimes(3));

    expect(memo).toHaveValue("AB");
  });

  it("서버 값이 그 사이 바뀌었으면 알려준다 — 조용히 덮지도, 조용히 무시하지도 않는다", async () => {
    const user = userEvent.setup();
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 0 } },
    });
    renderDrawer(qc);

    const memo = await screen.findByLabelText(/메모/);
    await user.clear(memo);
    await user.type(memo, "내 메모");

    // 다른 사람이 같은 비용을 고쳤다.
    getExpense.mockResolvedValue(serverExpense({
      memo: "다른 사람이 고친 메모", updated_at: "2026-08-10T09:00:00Z",
    }));
    await qc.refetchQueries({ queryKey: ["expense", EXPENSE_ID] });
    await waitFor(() => expect(getExpense).toHaveBeenCalledTimes(2));

    // 내 입력은 그대로다.
    expect(memo).toHaveValue("내 메모");
    // 그리고 사실을 알려준다.
    expect(await screen.findByText(/다른 곳에서 바뀌었습니다/)).toBeTruthy();
  });

  it("손대지 않았으면 최신 서버 값으로 갱신한다", async () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 0 } },
    });
    renderDrawer(qc);

    const memo = await screen.findByLabelText(/메모/);
    expect(memo).toHaveValue("서버 메모");

    getExpense.mockResolvedValue(serverExpense({ memo: "갱신된 메모" }));
    await qc.refetchQueries({ queryKey: ["expense", EXPENSE_ID] });

    // 아무것도 안 건드렸으면 최신 값을 보여주는 편이 맞다.
    await waitFor(() => expect(memo).toHaveValue("갱신된 메모"));
  });
});
