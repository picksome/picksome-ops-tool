/**
 * 거래처(external_partner) 선택 — 실제 백엔드 · 실제 DB.
 *
 * 여기서 지키는 것
 *   · 거래처와 내부 파트너를 **섞지 않는다**
 *   · 비활성 거래처는 선택 목록에서 빠지되 **이름은 사라지지 않는다**
 *   · 거래처 변경은 직접 저장으로 새어 나가지 않는다(승인 대상)
 *   · 승인 diff 가 UUID 가 아니라 거래처명을 보여준다
 */

import { test, expect, apiAs, loginAs } from "./fixtures";
import { SEED, dbExec, dbOne, dbQuery, reseed } from "./db";

test.beforeEach(() => {
  reseed();
});

test("거래처 목록 — 활성만 고를 수 있고 파트너와 섞이지 않는다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");

  const active = await acc.api.get("/api/v1/external-partners?active=true");
  expect(active.ok(), await active.text()).toBeTruthy();
  const body = await active.json();
  const names = body.data.map((p: { display_name: string }) => p.display_name);

  expect(names).toContain("E2E 물류사");
  // 비활성 거래처는 선택 목록에 없다
  expect(names).not.toContain("E2E 폐업거래처");
  // 내부 파트너(수익 배분 주체)가 거래처 목록에 섞이면 안 된다
  expect(names).not.toContain("E2E 파트너");

  // 생성 기능이 없다는 사실을 서버가 알려준다 — 화면이 각자 판단하지 않는다
  expect(body.meta.can_create).toBe(false);
  // 스키마에 없는 필드를 있는 척 내려주지 않는다
  expect(body.data[0]).not.toHaveProperty("business_number");

  // 필터를 끄면 비활성도 보인다 — 과거 비용의 이름을 해석하려면 필요하다
  const all = await acc.api.get("/api/v1/external-partners");
  const allNames = (await all.json()).data.map((p: { display_name: string }) => p.display_name);
  expect(allNames).toContain("E2E 폐업거래처");
});

test("거래처 검색은 이름으로 좁혀지고 조직 밖으로 새지 않는다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");

  const hit = await acc.api.get("/api/v1/external-partners?q=물류");
  expect((await hit.json()).data).toHaveLength(1);

  const miss = await acc.api.get("/api/v1/external-partners?q=존재하지않는거래처");
  expect((await miss.json()).data).toHaveLength(0);
});

test("거래처 변경은 직접 저장으로 못 하고 승인을 거친다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");

  // 직접 PATCH 는 막힌다 — supplier_id 는 EXPENSE_FINANCIAL_CHANGE 소유다
  const blocked = await acc.api.patch(`/api/v1/expenses/${SEED.expenses.open}`, {
    data: { supplier_id: SEED.suppliers.active },
  });
  expect(blocked.status()).toBe(403);
  expect((await blocked.json()).error.code).toBe("APPROVAL_REQUIRED");
  expect(dbOne<{ supplier_id: string | null }>(
    `SELECT supplier_id::text FROM expenses WHERE id = '${SEED.expenses.open}'`,
  )?.supplier_id).toBeNull();

  // 승인 경로로는 된다
  const req = await acc.api.post("/api/v1/approvals", {
    data: {
      target_type: "expense", target_id: SEED.expenses.open,
      request_type: "EXPENSE_FINANCIAL_CHANGE",
      proposed_changes: { supplier_id: SEED.suppliers.active },
      reason: "거래처 지정", submit: true,
    },
  });
  expect(req.ok(), await req.text()).toBeTruthy();
  const requestId = (await req.json()).data.id as string;

  const admin = await apiAs("approver");
  const approved = await admin.post(`/api/v1/approvals/${requestId}/approve`,
    { data: { comment: "확인" } });
  expect(approved.ok(), await approved.text()).toBeTruthy();
  const applied = await admin.post(`/api/v1/approvals/${requestId}/apply`);
  expect(applied.ok(), await applied.text()).toBeTruthy();
  await admin.dispose();

  expect(dbOne<{ supplier_id: string }>(
    `SELECT supplier_id::text FROM expenses WHERE id = '${SEED.expenses.open}'`,
  )?.supplier_id).toBe(SEED.suppliers.active);
});

test("목록 API 가 거래처 이름을 함께 준다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  dbExec(`UPDATE expenses SET supplier_id = '${SEED.suppliers.active}'
           WHERE id = '${SEED.expenses.open}'`);

  const res = await acc.api.get("/api/v1/expenses");
  const row = (await res.json()).data.find(
    (e: { id: string }) => e.id === SEED.expenses.open);
  // UUID 를 화면이 이름으로 바꾸려고 행마다 조회하면 N+1 이 난다
  expect(row.supplier_name).toBe("E2E 물류사");
});

test("삭제된 거래처의 이름도 사라지지 않는다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  dbExec(`UPDATE expenses SET supplier_id = '${SEED.suppliers.inactive}'
           WHERE id = '${SEED.expenses.open}'`);
  dbExec(`UPDATE external_partner SET deleted_at = now()
           WHERE id = '${SEED.suppliers.inactive}'`);

  // 목록 조인은 soft delete 와 무관하게 이름을 준다 —
  // 그 비용이 실제로 그 거래처를 가리키고 있기 때문이다
  const res = await acc.api.get("/api/v1/expenses");
  const row = (await res.json()).data.find(
    (e: { id: string }) => e.id === SEED.expenses.open);
  expect(row.supplier_name).toBe("E2E 폐업거래처");

  // 단건 조회도 삭제 여부를 알려주되 이름은 준다
  const one = await acc.api.get(`/api/v1/external-partners/${SEED.suppliers.inactive}`);
  const body = (await one.json()).data;
  expect(body.display_name).toBe("E2E 폐업거래처");
  expect(body.is_deleted).toBe(true);
  expect(body.is_active).toBe(false);
});

test("화면 — 거래처를 골라 승인 요청하면 diff 가 이름으로 보인다", async ({ page }) => {
  await loginAs(page, "accountant");
  await page.goto(`/expenses?expenseId=${SEED.expenses.open}`);
  await expect(page.getByRole("dialog")).toBeVisible({ timeout: 20_000 });

  await page.locator("#e-supplier").click();
  await page.getByRole("option", { name: "E2E 물류사" }).click();

  await page.getByRole("button", { name: "승인 요청", exact: true }).click();
  await expect(page.getByText("승인 요청을 올릴까요?")).toBeVisible();
  await page.getByPlaceholder("예: 카드사 청구금액과 대사 후 정정").fill("거래처 지정");
  await page.getByRole("button", { name: "승인 요청 올리기" }).click();
  await expect(page.getByText(/승인 요청 .*건을 올렸습니다/)).toBeVisible();

  const reqs = dbQuery<{ id: string; proposed_changes_json: string }>(
    `SELECT id::text, proposed_changes_json::text FROM approval_request
      WHERE target_id = '${SEED.expenses.open}' AND deleted_at IS NULL`);
  expect(reqs).toHaveLength(1);
  expect(JSON.parse(reqs[0].proposed_changes_json).supplier_id)
    .toBe(SEED.suppliers.active);

  // 승인 상세에서 UUID 가 아니라 이름이 보여야 승인 판단이 가능하다
  await page.goto(`/approvals?tab=all`);
  await page.getByRole("cell", { name: "금액·정산 변경" }).first().click();
  await expect(page.getByText("거래처").first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("E2E 물류사").first()).toBeVisible();
  await expect(page.getByText(SEED.suppliers.active)).toHaveCount(0);
});
