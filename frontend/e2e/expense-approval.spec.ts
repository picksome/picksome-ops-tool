/**
 * 사업비용 승인 + 잠긴 월 조정 — **실제 백엔드와 실제 DB**.
 *
 * 여기서 지키는 것
 *   · 승인 전에는 장부 금액이 그대로다
 *   · 잠긴 달 비용은 **승인으로도** 못 바꾼다
 *   · 잠긴 달 정정은 열린 달 조정으로 처리하고 원본은 불변이다
 *   · 금액은 권한 없는 사용자에게 내려가지 않는다
 */

import { test, expect, apiAs, loginAs } from "./fixtures";
import { SEED, dbQuery, dbOne, reseed } from "./db";

test.beforeEach(() => {
  reseed();
});

function expenseRow(id: string) {
  return dbOne<{
    gross_amount: number; expense_date: string; memo: string | null;
  }>(`SELECT gross_amount, expense_date::text, memo FROM expenses WHERE id = '${id}'`);
}

function adjustmentsOf(originalId: string) {
  return dbQuery<{
    adjustment_type: string; amount_delta: number;
    original_period: string; adjustment_period: string;
    adjustment_expense_id: string;
  }>(`SELECT adjustment_type, amount_delta, original_period, adjustment_period,
             adjustment_expense_id::text
        FROM expense_adjustment
       WHERE original_expense_id = '${originalId}' AND deleted_at IS NULL`);
}

async function requestChange(
  api: import("@playwright/test").APIRequestContext,
  expenseId: string, changes: Record<string, unknown>,
) {
  const res = await api.post("/api/v1/approvals", {
    data: {
      target_type: "expense", target_id: expenseId,
      request_type: "EXPENSE_FINANCIAL_CHANGE",
      proposed_changes: changes, reason: "E2E 비용 정정", submit: true,
    },
  });
  expect(res.ok(), await res.text()).toBeTruthy();
  return (await res.json()).data.id as string;
}

test("승인 대상 필드는 직접 수정 API 로 못 바꾼다", async ({ page }) => {
  const op = await loginAs(page, "operator");

  const blocked = await op.api.patch(`/api/v1/expenses/${SEED.expenses.open}`, {
    data: { gross_amount: 999999 },
  });
  expect(blocked.status()).toBe(403);
  expect((await blocked.json()).error.code).toBe("APPROVAL_REQUIRED");

  // 장부는 그대로다.
  expect(expenseRow(SEED.expenses.open)?.gross_amount).toBe(120000);
});

test("메모는 승인 없이 바로 저장된다", async ({ page }) => {
  const op = await loginAs(page, "operator");
  const res = await op.api.patch(`/api/v1/expenses/${SEED.expenses.open}`, {
    data: { memo: "E2E 운영 메모" },
  });
  expect(res.ok(), await res.text()).toBeTruthy();
  expect(expenseRow(SEED.expenses.open)?.memo).toBe("E2E 운영 메모");
});

test("금액 변경 — 승인 전 미반영, 적용 후 반영", async ({ page }) => {
  const op = await loginAs(page, "operator");
  const requestId = await requestChange(op.api, SEED.expenses.open, {
    gross_amount: 185000,
  });

  // ① 승인 전에는 장부가 그대로다
  expect(expenseRow(SEED.expenses.open)?.gross_amount).toBe(120000);

  const accountant = await apiAs("accountant");
  expect((await accountant.post(`/api/v1/approvals/${requestId}/approve`, {
    data: { comment: "확인함" },
  })).ok()).toBeTruthy();
  const applied = await accountant.post(`/api/v1/approvals/${requestId}/apply`);
  expect(applied.ok(), await applied.text()).toBeTruthy();
  await accountant.dispose();

  // ② 적용 후 반영됐다
  expect(expenseRow(SEED.expenses.open)?.gross_amount).toBe(185000);

  // ③ 그 달이 "재계산 필요" 로 표시됐다 — 재계산 자체는 하지 않는다
  const flags = dbQuery<{ year_month: string }>(
    `SELECT year_month FROM period_recalc_flag
      WHERE org_id = '${SEED.orgId}' AND resolved_at IS NULL`,
  );
  expect(flags.map((f) => f.year_month)).toContain("2026-08");
});

test("요청자 본인은 승인할 수 없다", async ({ page }) => {
  const op = await loginAs(page, "operator");
  const requestId = await requestChange(op.api, SEED.expenses.open, {
    gross_amount: 150000,
  });

  const self = await op.api.post(`/api/v1/approvals/${requestId}/approve`, {
    data: { comment: "내가 내 걸" },
  });
  expect(self.status()).toBe(403);
  expect(expenseRow(SEED.expenses.open)?.gross_amount).toBe(120000);
});

test("잠긴 달 비용은 승인으로도 바꿀 수 없다", async ({ page }) => {
  const op = await loginAs(page, "operator");
  const requestId = await requestChange(op.api, SEED.expenses.locked, {
    gross_amount: 350000,
  });

  const accountant = await apiAs("accountant");
  // 승인 자체는 된다 — 판단은 유효하다.
  expect((await accountant.post(`/api/v1/approvals/${requestId}/approve`, {
    data: { comment: "확인" },
  })).ok()).toBeTruthy();

  // **적용에서 막힌다.** 승인이 월 잠금을 우회하지 않는다.
  const applied = await accountant.post(`/api/v1/approvals/${requestId}/apply`);
  expect(applied.status()).toBe(409);
  expect((await applied.json()).error.code).toBe("SETTLEMENT_LOCKED");
  await accountant.dispose();

  // 원본은 그대로다.
  expect(expenseRow(SEED.expenses.locked)?.gross_amount).toBe(300000);
});

test("잠긴 달 안내 — 화면이 저장을 누르기 전에 알 수 있다", async ({ page }) => {
  const op = await loginAs(page, "operator");

  const locked = await op.api.get(`/api/v1/expenses/${SEED.expenses.locked}/lock-state`);
  expect(locked.ok()).toBeTruthy();
  const lockedBody = (await locked.json()).data;
  expect(lockedBody.year_month).toBe("2026-07");
  expect(lockedBody.close_state).toBe("LOCKED");
  expect(lockedBody.requires_adjustment).toBe(true);

  const open = await op.api.get(`/api/v1/expenses/${SEED.expenses.open}/lock-state`);
  const openBody = (await open.json()).data;
  expect(openBody.requires_adjustment).toBe(false);
});

test("잠긴 달 정정 — 열린 달에 조정을 만들고 원본은 불변", async ({ page }) => {
  const op = await loginAs(page, "operator");

  const created = await op.api.post(
    `/api/v1/expenses/${SEED.expenses.locked}/adjustments`,
    {
      data: {
        adjustment_type: "CORRECTION",
        adjustment_reason: "7월 광고비 과소 계상",
        adjustment_date: "2026-08-20",
        amount_delta: 50000,
      },
    },
  );
  expect(created.ok(), await created.text()).toBeTruthy();
  const body = (await created.json()).data;

  expect(body.original_period).toBe("2026-07");
  expect(body.adjustment_period).toBe("2026-08");

  // ① **원본은 금액도 날짜도 그대로다**
  const original = expenseRow(SEED.expenses.locked)!;
  expect(original.gross_amount).toBe(300000);
  expect(original.expense_date).toBe("2026-07-10");

  // ② 조정은 열린 달에 실제 비용 행을 만든다 — 집계가 자동으로 포함하도록
  const adjExpense = expenseRow(body.adjustment_expense_id)!;
  expect(adjExpense.expense_date).toBe("2026-08-20");
  expect(adjExpense.gross_amount).toBe(50000);

  // ③ 원본과 연결돼 있다
  const links = adjustmentsOf(SEED.expenses.locked);
  expect(links).toHaveLength(1);
  expect(links[0].adjustment_type).toBe("CORRECTION");
  expect(links[0].amount_delta).toBe(50000);
});

test("조정도 잠긴 달에는 못 넣는다", async ({ page }) => {
  const op = await loginAs(page, "operator");

  // 7월(잠김)에 조정을 넣으려 하면 거절된다 — 그러면 잠금 우회가 된다.
  const res = await op.api.post(
    `/api/v1/expenses/${SEED.expenses.locked}/adjustments`,
    {
      data: {
        adjustment_type: "CORRECTION", adjustment_reason: "잠긴 달로 시도",
        adjustment_date: "2026-07-20", amount_delta: 10000,
      },
    },
  );
  expect(res.status()).toBe(409);
});

test("조정 사유는 필수", async ({ page }) => {
  const op = await loginAs(page, "operator");
  const res = await op.api.post(
    `/api/v1/expenses/${SEED.expenses.locked}/adjustments`,
    {
      data: {
        adjustment_type: "CORRECTION", adjustment_reason: "",
        adjustment_date: "2026-08-20", amount_delta: 10000,
      },
    },
  );
  expect(res.ok()).toBeFalsy();
});

test("확정 달은 재계산 대상으로 표시되지 않는다", async ({ page }) => {
  const op = await loginAs(page, "operator");

  await op.api.post(`/api/v1/expenses/${SEED.expenses.locked}/adjustments`, {
    data: {
      adjustment_type: "CORRECTION", adjustment_reason: "정정",
      adjustment_date: "2026-08-20", amount_delta: 50000,
    },
  });

  const flags = dbQuery<{ year_month: string }>(
    `SELECT year_month FROM period_recalc_flag
      WHERE org_id = '${SEED.orgId}' AND resolved_at IS NULL`,
  ).map((f) => f.year_month);

  // 조정이 들어간 열린 달만 표시된다. 확정된 7월을 표시하면
  // 언젠가 배치가 실행해 확정 숫자를 덮어쓴다.
  expect(flags).toContain("2026-08");
  expect(flags).not.toContain("2026-07");
});

test("비용 금액 민감정보 — 권한 없는 사용자에게 내려주지 않는다", async ({ page }) => {
  const op = await loginAs(page, "operator");
  const requestId = await requestChange(op.api, SEED.expenses.open, {
    gross_amount: 777777,
  });

  const readonly = await apiAs("readonly");
  const body = (await (await readonly.get(`/api/v1/approvals/${requestId}`)).json()).data;
  // **응답 본문에 금액이 없어야 한다.** CSS 로 가리는 건 방어가 아니다.
  expect(JSON.stringify(body)).not.toContain("777777");
  expect(body.sensitive_visible).toBe(false);
  // 키는 남는다 — "변경 없음" 과 "볼 수 없음" 은 다른 상태다.
  expect(Object.keys(body.proposed_changes_json)).toContain("gross_amount");
  await readonly.dispose();

  const accountant = await apiAs("accountant");
  const visible = (await (await accountant.get(
    `/api/v1/approvals/${requestId}`)).json()).data;
  expect(visible.sensitive_visible).toBe(true);
  expect(visible.proposed_changes_json.gross_amount).toBe(777777);
  await accountant.dispose();
});
