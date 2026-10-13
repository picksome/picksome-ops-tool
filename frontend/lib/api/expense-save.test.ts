/**
 * 비용 저장 오케스트레이션 · 승인 diff 표시 단위 테스트.
 *
 * 여기서 막는 사고
 *   · 금액이 직접 저장 경로(PATCH)로 새어 나가는 것
 *   · 직접 저장이 먼저 나가서, 승인 요청 생성이 실패했을 때 일부만 반영되는 것
 *   · 승인 화면이 UUID·`true`·`INCLUDED` 를 그대로 보여주는 것
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { FieldOwnership } from "@/lib/api/products";

const calls: string[] = [];
const createApproval = vi.fn(async (body: Record<string, unknown>) => {
  calls.push(`approval:${body.request_type}`);
  return { id: `ap-${calls.length}`, ...body };
});
const updateExpense = vi.fn(async (_id: string, patch: Record<string, unknown>) => {
  calls.push("patch");
  return { id: _id, ...patch };
});

vi.mock("@/lib/api/approvals", async (orig) => {
  const actual = await orig<typeof import("@/lib/api/approvals")>();
  return { ...actual, approvalsApi: { create: (b: Record<string, unknown>) => createApproval(b) } };
});
vi.mock("@/lib/api/expenses", () => ({
  expensesApi: {
    update: (id: string, patch: Record<string, unknown>) => updateExpense(id, patch),
  },
}));

const { saveExpenseWithApproval } = await import("@/lib/api/expense-save");
const { buildDiffRows } = await import("@/lib/api/approvals");

const OWNERSHIP: FieldOwnership = {
  request_types: {
    EXPENSE_FINANCIAL_CHANGE: [
      "gross_amount", "expense_date", "category_id", "payment_method",
      "payment_card_holder_id", "is_tax_deductible", "description",
    ],
    EXPENSE_EVIDENCE_CHANGE: ["evidence_type", "evidence_status"],
  },
  approval_fields: [
    "gross_amount", "expense_date", "category_id", "payment_method",
    "payment_card_holder_id", "is_tax_deductible", "description",
    "evidence_type", "evidence_status",
  ],
  sensitive_fields: ["gross_amount", "payment_card_holder_id"],
};

const BASE = {
  gross_amount: 120000,
  expense_date: "2026-07-10",
  memo: null,
  evidence_status: "NONE",
};

beforeEach(() => {
  calls.length = 0;
  createApproval.mockClear();
  updateExpense.mockClear();
});

describe("saveExpenseWithApproval", () => {
  it("금액 변경은 승인 요청으로만 나간다 — PATCH 는 호출되지 않는다", async () => {
    const r = await saveExpenseWithApproval(
      "e1", BASE, { ...BASE, gross_amount: 185000 }, { ownership: OWNERSHIP },
    );
    expect(updateExpense).not.toHaveBeenCalled();
    expect(r.directSaved).toBe(false);
    expect(r.approvals).toHaveLength(1);
    expect(createApproval.mock.calls[0][0]).toMatchObject({
      target_type: "expense",
      target_id: "e1",
      request_type: "EXPENSE_FINANCIAL_CHANGE",
      proposed_changes: { gross_amount: 185000 },
    });
  });

  it("메모만 바꾸면 승인 없이 바로 저장한다", async () => {
    const r = await saveExpenseWithApproval(
      "e1", BASE, { ...BASE, memo: "카드사 대사 완료" }, { ownership: OWNERSHIP },
    );
    expect(createApproval).not.toHaveBeenCalled();
    expect(r.directSaved).toBe(true);
    expect(updateExpense).toHaveBeenCalledWith("e1", { memo: "카드사 대사 완료" });
  });

  it("섞여 있으면 승인 요청을 **먼저** 만든다", async () => {
    // 직접 저장이 먼저 나가면, 승인 생성이 실패했을 때 메모만 바뀐 상태로 남고
    // 사용자는 무엇이 저장됐는지 알 수 없다.
    await saveExpenseWithApproval(
      "e1", BASE,
      { ...BASE, gross_amount: 200000, memo: "정정" },
      { ownership: OWNERSHIP },
    );
    expect(calls).toEqual(["approval:EXPENSE_FINANCIAL_CHANGE", "patch"]);
  });

  it("승인 유형이 다르면 요청도 나뉜다", async () => {
    await saveExpenseWithApproval(
      "e1", BASE,
      { ...BASE, gross_amount: 1, evidence_status: "PRESENT" },
      { ownership: OWNERSHIP },
    );
    expect(calls.filter((c) => c.startsWith("approval:")).sort()).toEqual([
      "approval:EXPENSE_EVIDENCE_CHANGE", "approval:EXPENSE_FINANCIAL_CHANGE",
    ]);
  });

  it("바뀐 게 없으면 아무것도 보내지 않는다", async () => {
    const r = await saveExpenseWithApproval("e1", BASE, { ...BASE }, { ownership: OWNERSHIP });
    expect(calls).toEqual([]);
    expect(r.directSaved).toBe(false);
    expect(r.updated).toBeNull();
  });

  it("소유권 표 없이는 저장하지 않는다", async () => {
    // 추측으로 보내면 금액이 직접 저장 경로로 흘러 403 이 난다. 그 전에 멈춘다.
    await expect(
      saveExpenseWithApproval("e1", BASE, { ...BASE, gross_amount: 5 }, { ownership: null }),
    ).rejects.toThrow(/승인 정책/);
    expect(calls).toEqual([]);
  });
});

describe("승인 diff — 사람이 읽을 수 있어야 한다", () => {
  it("비용 필드가 한글 라벨로 나온다", () => {
    const rows = buildDiffRows(
      "expense", { gross_amount: 120000 }, { gross_amount: 185000 },
    );
    const row = rows.find((r) => r.field === "gross_amount")!;
    expect(row.label).toBe("금액");
    expect(row.kind).toBe("money");
    expect(row.changed).toBe(true);
  });

  it("귀속일은 날짜로 다룬다", () => {
    const rows = buildDiffRows(
      "expense", { expense_date: "2026-07-10" }, { expense_date: "2026-08-01" },
    );
    expect(rows[0].kind).toBe("date");
  });

  it("enum 값에는 라벨표가 붙는다", () => {
    const rows = buildDiffRows(
      "expense", { payment_method: "BUSINESS_CARD" }, { payment_method: "PERSONAL_CARD" },
    );
    expect(rows[0].valueLabels?.PERSONAL_CARD).toBe("개인카드");
  });

  it("참조 UUID 는 이름으로 바뀐다", () => {
    const rows = buildDiffRows(
      "expense", { category_id: "c1" }, { category_id: "c2" },
      (field, id) => (field === "category_id" ? { c1: "광고비", c2: "물류비" }[id] : undefined),
    );
    expect(rows[0].before).toBe("광고비");
    expect(rows[0].after).toBe("물류비");
  });

  it("이름을 못 찾아도 UUID 를 노출하지 않는다", () => {
    const rows = buildDiffRows("expense", { supplier_id: "s1" }, { supplier_id: "s2" });
    expect(rows[0].before).toBe("다른 항목");
    expect(rows[0].after).toBe("다른 항목");
    // 값이 바뀌었는데 둘 다 "다른 항목" 이면 변경 표시가 사라진다 —
    // 이름을 모를 때는 diff 를 "바뀜" 으로 남겨야 승인자가 원본을 확인한다.
    expect(rows[0].kind).toBe("ref");
  });

  it("제안에 없는 필드는 변경으로 표시하지 않는다", () => {
    const rows = buildDiffRows(
      "expense", { gross_amount: 1000, memo: "그대로" }, { gross_amount: 2000 },
    );
    expect(rows.find((r) => r.field === "memo")!.changed).toBe(false);
  });
});
