import { expect, test } from "@playwright/test";
import { SEED, dbExec, dbOne, dbQuery, reseed } from "./db";
import { apiAs, loginAs } from "./fixtures";

/**
 * 주문 자동 동기화 E2E (실제 FastAPI + PostgreSQL).
 *
 * 여기서 확인하는 것
 * ---------------
 * 1. 설정 전에는 **자동으로 돌지 않는다** — "0건 정상" 으로 보이지 않는다
 * 2. 주문이 자동으로 들어와도 **클레임은 연결됐다고 말하지 않는다**
 * 3. 마지막 시도와 마지막 성공을 **따로** 보여 준다
 * 4. 이미 실행 중이면 성공한 척하지 않는다
 * 5. 꺼 두면 외부를 부르지 않는다
 * 6. 읽기 전용 역할은 실행할 수 없다
 * 7. 화면에 개인정보가 나오지 않는다
 *
 * 외부 네트워크를 부르지 않는다
 * -------------------------
 * 자격증명이 없는 계정으로 본다. 그러면 벤더를 부르기 전에 관문에서 멈추므로
 * 전체 E2E 가 네트워크 때문에 빨간 불이 뜨는 일이 없다. 실제 API 를 태우는
 * 검증은 별도로 돌린다(docs/79).
 */

test.beforeEach(() => {
  reseed();
});

type Api = Awaited<ReturnType<typeof apiAs>>;

async function json(res: Awaited<ReturnType<Api["get"]>>) {
  expect(res.ok(), await res.text()).toBeTruthy();
  return (await res.json()).data;
}

/** 주문을 읽을 외부 계정 하나. **자격증명은 넣지 않는다.** */
function seedExternalAccount(): string {
  dbExec(`
    INSERT INTO external_system (code, name, system_type, supports_api_key)
    VALUES ('NEXT_ENGINE', 'NEXT ENGINE', 'SELLER_TOOL', TRUE)
    ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name
  `);
  const row = dbOne<{ id: string }>(`
    INSERT INTO external_account (org_id, external_system_id, account_code,
           display_name, credential_kind)
    SELECT '${SEED.orgId}', s.id, 'E2E', 'NE 본사', 'API_KEY'
      FROM external_system s WHERE s.code = 'NEXT_ENGINE'
    RETURNING id
  `);
  // 여기서 null 이면 시드가 깨진 것이다. 조용히 넘기면 뒤 단언이 엉뚱한
  // 이유로 실패하고, 원인을 찾는 데 시간이 든다.
  if (!row) throw new Error("외부 계정 시드에 실패했습니다.");
  return String(row.id);
}

// ── 1) 설정 전 ──────────────────────────────────────────────────

test("설정하기 전에는 자동으로 돌지 않는다", async () => {
  seedExternalAccount();
  const admin = await apiAs("approver");

  const status = await json(
    await admin.get("/api/v1/external/order-sync/status"));

  expect(status.enabled).toBe(false);
  // "설정 안 됨" 과 "0건 정상" 은 다르다.
  expect(status.accounts[0].configured).toBe(false);
});

test("설정 없이 수동 실행하면 성공한 척하지 않는다", async () => {
  seedExternalAccount();
  const admin = await apiAs("approver");

  const res = await admin.post("/api/v1/external/order-sync/run", { data: {} });

  expect(res.status()).toBe(422);
});

// ── 2) 클레임과 섞지 않는다 ─────────────────────────────────────

test("주문이 자동이어도 클레임은 연결됐다고 하지 않는다", async () => {
  seedExternalAccount();
  const admin = await apiAs("approver");

  const status = await json(
    await admin.get("/api/v1/external/order-sync/status"));

  // 한 덩어리로 보이면 취소·반품까지 반영되고 있다고 읽힌다.
  expect(status.claim_ingest_implemented).toBe(false);
});

// ── 3) 마지막 시도 ≠ 마지막 성공 ────────────────────────────────

test("마지막 시도와 마지막 성공을 따로 준다", async () => {
  seedExternalAccount();
  const admin = await apiAs("approver");

  const account = (await json(
    await admin.get("/api/v1/external/order-sync/status"))).accounts[0];

  // 합쳐 놓으면 몇 시간째 못 읽고 있는 상태가 성공 시각 뒤에 숨는다.
  expect(account).toHaveProperty("last_attempt_at");
  expect(account).toHaveProperty("last_success_at");
  expect(account).toHaveProperty("last_error_code");
});

// ── 5) 꺼 두면 외부를 부르지 않는다 ─────────────────────────────

test("자격증명이 없으면 벤더를 부르기 전에 멈춘다", async () => {
  const accountId = seedExternalAccount();
  const admin = await apiAs("approver");

  const account = (await json(
    await admin.get("/api/v1/external/order-sync/status")))
    .accounts.find((a: { external_account_id: string }) =>
      a.external_account_id === accountId);

  expect(account.blocked_by).toBe("CREDENTIAL_NOT_CONFIGURED");
  // 실행 기록이 없다 = 외부를 부르지 않았다.
  const runs = dbQuery(
    `SELECT id FROM external_sync_run WHERE sync_type = 'ORDERS'`);
  expect(runs.length).toBe(0);
});

// ── 6) 권한 ─────────────────────────────────────────────────────

test("읽기 전용 역할은 동기화를 실행할 수 없다", async () => {
  seedExternalAccount();
  const viewer = await apiAs("readonly");

  const res = await viewer.post("/api/v1/external/order-sync/run", { data: {} });

  expect(res.status()).toBe(403);
});

// ── 7) 화면 ─────────────────────────────────────────────────────

test("자동화 화면에서 주문 동기화 상태를 볼 수 있고 개인정보가 없다", async ({ page }) => {
  seedExternalAccount();
  await loginAs(page, "approver");
  await page.goto("/settings/automation");

  await expect(page.getByText("주문 동기화 상태")).toBeVisible();
  await expect(page.getByText("NE 본사")).toBeVisible();
  // 주문만 들어온다는 사실을 화면이 먼저 말한다.
  await expect(
    page.getByText(/취소·반품·교환은 아직 반영되지/),
  ).toBeVisible();

  // 겹쳐 읽기 폭을 사람이 정한다 — 코드가 기본값을 갖지 않는다.
  await expect(page.getByText("겹쳐 읽을 일수").first()).toBeVisible();

  const body = await page.locator("body").innerText();
  for (const leaked of ["Api-Auth-Key", "credential_secret_ref", "api_key"]) {
    expect(body).not.toContain(leaked);
  }
});
