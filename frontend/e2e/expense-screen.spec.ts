/**
 * 사업비용 **화면** — 실제 백엔드와 실제 DB.
 *
 * `expense-approval.spec.ts` 는 API 계약을 지킨다. 여기는 그 위에 얹힌 화면이
 * 같은 규칙을 지키는지 본다. 화면만 mock 위에 남아 있으면 API 가 아무리 옳아도
 * 사용자는 "저장했다" 를 보고 DB 는 그대로인 상태가 된다 — 이 화면이 실제로
 * 그랬다.
 *
 * 모든 단언이 화면과 DB 를 **둘 다** 본다.
 */

import { test, expect, loginAs } from "./fixtures";
import { SEED, dbOne, dbQuery, reseed } from "./db";
import { attachTimings, phase, phaseSync } from "./timing";

test.beforeEach(() => {
  // seed 도 구간이다. 0.26초로 측정됐지만 "측정했다" 는 기록이 남아야
  // 다음에 느려졌을 때 여기가 아니라고 말할 수 있다.
  phaseSync("reseed", reseed);
});

// 통과했을 때의 구간 값이 있어야 느려진 실행과 비교할 수 있다(e2e/timing.ts).
test.afterEach(attachTimings);

function expenseRow(id: string) {
  return dbOne<{ gross_amount: number; memo: string | null; evidence_status: string }>(
    `SELECT gross_amount, memo, evidence_status FROM expenses WHERE id = '${id}'`,
  );
}

function approvalsForExpense(id: string) {
  return dbQuery<{ request_type: string; status: string; proposed_changes_json: string }>(
    `SELECT request_type, status, proposed_changes_json::text
       FROM approval_request
      WHERE target_id = '${id}' AND target_type = 'expense' AND deleted_at IS NULL`,
  );
}

async function openExpense(page: import("@playwright/test").Page, id: string) {
  // 목록에서 행을 찾아 클릭하지 않는다 — 정렬·페이징에 의존하면 seed 가 늘 때마다 깨진다.
  //
  // 이동과 dialog 대기를 따로 잰다. 둘은 다른 원인으로 느려진다 — 전자는 dev
  // 서버의 라우트 컴파일, 후자는 상세 API 왕복이다.
  await phase("페이지 이동", () => page.goto(`/expenses?expenseId=${id}`));
  await phase("dialog 대기", () =>
    expect(page.getByRole("dialog")).toBeVisible({ timeout: 20_000 }));
}

test("목록이 실제 DB 값을 보여준다 — 카테고리·결제자 이름까지", async ({ page }) => {
  await loginAs(page, "accountant");
  await page.goto("/expenses");

  // 조인된 이름이 UUID 가 아니라 사람이 읽는 값으로 나와야 한다.
  await expect(page.getByRole("cell", { name: "E2E 광고비" }).first()).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByRole("cell", { name: "E2E 파트너" }).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "120,000 원" }).first()).toBeVisible();
});

test("메모는 승인 없이 바로 저장되고 DB 에 남는다", async ({ page }) => {
  await loginAs(page, "accountant");
  await openExpense(page, SEED.expenses.open);

  await page.locator("#e-memo").fill("화면에서 남긴 메모");
  await page.getByRole("button", { name: "저장", exact: true }).click();
  await expect(page.getByText("저장했습니다")).toBeVisible();

  expect(expenseRow(SEED.expenses.open)?.memo).toBe("화면에서 남긴 메모");
  // 메모까지 승인을 태우면 승인함이 잡음으로 찬다.
  expect(approvalsForExpense(SEED.expenses.open)).toHaveLength(0);
});

test("금액 수정 — 버튼이 '승인 요청' 으로 바뀌고 장부는 그대로다", async ({ page }) => {
  await loginAs(page, "accountant");
  await openExpense(page, SEED.expenses.open);

  // 합계금액 입력. 자동 분리라 supply/vat 은 서버가 다시 계산한다.
  const gross = page.locator("#e-gross");
  await expect(gross).toHaveValue("120000");
  await gross.fill("185000");
  await gross.blur();

  // 누르기 **전에** 승인 대상임을 알아야 한다.
  const submit = page.getByRole("button", { name: "승인 요청", exact: true });
  await expect(submit).toBeVisible();
  await submit.click();

  await expect(page.getByText("승인 요청을 올릴까요?")).toBeVisible();
  // 무엇이 승인으로 가는지 한글 라벨로 보여야 한다 — JSON 키가 아니라.
  await expect(page.getByText("금액·정산 변경")).toBeVisible();
  await page.getByPlaceholder("예: 카드사 청구금액과 대사 후 정정").fill("E2E 화면 사유");
  await page.getByRole("button", { name: "승인 요청 올리기" }).click();
  await expect(page.getByText(/승인 요청 .*건을 올렸습니다/)).toBeVisible();

  // ① 승인 전에는 장부가 그대로다
  expect(expenseRow(SEED.expenses.open)?.gross_amount).toBe(120000);

  // ② 요청은 실제로 만들어졌고, 제안 내용에 새 금액이 담겼다
  const reqs = approvalsForExpense(SEED.expenses.open);
  expect(reqs).toHaveLength(1);
  expect(reqs[0].request_type).toBe("EXPENSE_FINANCIAL_CHANGE");
  expect(reqs[0].status).toBe("PENDING");
  expect(JSON.parse(reqs[0].proposed_changes_json).gross_amount).toBe(185000);
});

test("승인 대기 중이면 목록에서 바로 보인다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  const res = await acc.api.post("/api/v1/approvals", {
    data: {
      target_type: "expense", target_id: SEED.expenses.open,
      request_type: "EXPENSE_FINANCIAL_CHANGE",
      proposed_changes: { gross_amount: 150000 }, reason: "E2E", submit: true,
    },
  });
  expect(res.ok(), await res.text()).toBeTruthy();

  await page.goto("/expenses");
  // 드로어를 열어야만 알 수 있으면 목록에서 무엇이 대기 중인지 볼 수 없다.
  await expect(page.getByText("승인 1").first()).toBeVisible({ timeout: 20_000 });
});

test("잠긴 달 비용 — 화면이 저장을 막고 무엇을 해야 하는지 알려준다", async ({ page }) => {
  await loginAs(page, "accountant");
  await openExpense(page, SEED.expenses.locked);

  await expect(page.getByText("잠긴 월의 비용입니다")).toBeVisible();
  await expect(page.getByText(/2026년 7월/)).toBeVisible();
  // "수정할 수 없습니다" 로 끝나지 않는다 — 다음 행동을 준다.
  await expect(page.getByText(/정정 조정/).first()).toBeVisible();

  // 입력 자체가 잠겨 있어야 한다. 다 입력한 뒤 409 를 만나면 늦다.
  await expect(page.locator("#e-desc")).toBeDisabled();
  await expect(page.getByRole("button", { name: "저장", exact: true })).toBeDisabled();
});

// 한 번 46.8초에서 죽었던 테스트다. 제한을 늘리는 대신 구간을 재고 있다 —
// 다음에 느려지면 어디서 늘어난 것인지 첨부된 표가 말해 준다(e2e/timing.ts).
test("잠긴 달 정정 — 화면에서 조정을 만들면 원본은 그대로다", async ({ page }) => {
  await phase("로그인", () => loginAs(page, "accountant"));
  await openExpense(page, SEED.expenses.locked);

  await phase("조정 대화상자 열기", async () => {
    await page.getByRole("button", { name: "정정 조정 만들기" }).click();
    await expect(page.getByText("정정 조정을 만들까요?")).toBeVisible();
  });

  await phase("입력", async () => {
    // 채운 뒤 **값이 실제로 남았는지** 확인한다.
    //
    // 이 확인이 없으면, 입력이 유실됐을 때 다음 줄의 클릭이 비활성 버튼을
    // 45초 동안 기다리다 죽는다. 계측으로 그 43.7초가 이 클릭에 있다는 것을
    // 확인했고(docs/61), 실패 스냅샷에서 입력칸이 비어 있고 타이핑한 값이
    // 텍스트 노드로 떠 있는 것을 봤다 — 대화상자의 입력 요소가 타이핑 도중
    // 교체된다는 뜻이다.
    //
    // 여기서 확인하면 **원인 지점에서 즉시** 실패한다. 재시도로 덮는 것이
    // 아니라, 같은 결함을 더 빨리·더 정확히 드러낸다.
    const date = page.locator("#adj-date");
    const delta = page.locator("#adj-delta");
    const reason = page.locator("#adj-reason");

    await date.fill("2026-08-15");
    await expect(date).toHaveValue("2026-08-15");

    await delta.fill("50000");
    await expect(delta).not.toHaveValue("");

    await reason.fill("E2E 화면 조정 사유");
    await expect(reason).toHaveValue("E2E 화면 조정 사유");
    // 여기까지 왔으면 버튼이 활성이어야 한다. 아니면 저장 조건이 문제다.
    await expect(page.getByRole("button", { name: "조정 만들기" })).toBeEnabled();
  });

  await phase("저장 왕복", async () => {
    await page.getByRole("button", { name: "조정 만들기" }).click();
    await expect(page.getByText("정정 조정을 만들었습니다")).toBeVisible();
  });

  // ① 원본은 불변 — 확정된 달의 순이익·배분이 흔들리면 안 된다
  phaseSync("DB 확인", () => {
    expect(expenseRow(SEED.expenses.locked)?.gross_amount).toBe(300000);
  });

  // ② 조정은 열린 달에 실제 비용 행으로 생긴다
  const adj = dbQuery<{
    amount_delta: number; original_period: string; adjustment_period: string;
    adjustment_type: string;
  }>(`SELECT amount_delta, original_period, adjustment_period, adjustment_type
        FROM expense_adjustment
       WHERE original_expense_id = '${SEED.expenses.locked}' AND deleted_at IS NULL`);
  expect(adj).toHaveLength(1);
  expect(adj[0].amount_delta).toBe(50000);
  expect(adj[0].original_period).toBe("2026-07");
  expect(adj[0].adjustment_period).toBe("2026-08");
});

test("승인함이 비용 요청을 한글로 보여준다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  const res = await acc.api.post("/api/v1/approvals", {
    data: {
      target_type: "expense", target_id: SEED.expenses.open,
      request_type: "EXPENSE_FINANCIAL_CHANGE",
      // 요청 유형마다 소유 필드가 다르다 — 섞어 보내면 서버가 거부한다.
      proposed_changes: { gross_amount: 99000 },
      reason: "E2E 라벨 확인", submit: true,
    },
  });
  expect(res.ok(), await res.text()).toBeTruthy();

  await page.goto("/approvals?tab=all");
  // enum 이 그대로 뜨면 승인자는 무엇을 승인하는지 읽을 수 없다.
  await expect(page.getByRole("cell", { name: "금액·정산 변경" }).first())
    .toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("EXPENSE_FINANCIAL_CHANGE")).toHaveCount(0);
  await expect(page.getByRole("cell", { name: "사업비용" }).first()).toBeVisible();
});


/**
 * 배경 refetch 가 입력을 덮지 않는 것은 **여기서 검증하지 않는다.**
 *
 * 시도해 봤고 안 됐다. React Query v5 의 refetch 계기(focusManager /
 * onlineManager)는 헤드리스 브라우저에서 이벤트를 흉내 내는 것으로 켜지지
 * 않는다. 억지로 켜려면 라이브러리 내부를 건드려야 하는데, 그러면 **흉내낸
 * 것이 맞는지**를 검증하는 테스트가 된다 — 구현이 바뀌면 조용히 무의미해진다.
 *
 * 그래서 그 계약은 `components/expense/expense-drawer-dirty.test.tsx` 가
 * 컴포넌트 + QueryClient 수준에서 확정적으로 검증한다. 거기서는 실제 refetch 를
 * 일으킬 수 있고, 고치기 전에 실패하는 것을 확인했다(docs/62).
 */
