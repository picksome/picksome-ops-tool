import { expect, test } from "@playwright/test";
import { SEED, dbExec, dbOne, dbQuery, reseed } from "./db";
import { apiAs, loginAs } from "./fixtures";

/**
 * 외부 시스템 연동 기반 E2E (실제 FastAPI + PostgreSQL).
 *
 * 실제 커넥터가 없는 상태를 검증한다
 * ------------------------------
 * NEXT ENGINE credential 이 없으므로 커넥터도 없다. 그 상태에서 화면과 API 가
 * **정직하게 "아직 못 한다"** 고 말하는지를 본다 — 가짜 성공을 만들지 않는 것이
 * 이 기반의 핵심이다.
 *
 * 테스트 전용 커넥터는 여기서 쓰지 않는다. 그건 백엔드 테스트에서 등록/해제를
 * 통제할 수 있을 때만 안전하다. E2E 는 실제 서버 프로세스라 등록해 두면
 * 그 프로세스 전체에 남는다.
 */

test.beforeEach(() => {
  reseed();
});

type Api = Awaited<ReturnType<typeof apiAs>>;

async function json(res: Awaited<ReturnType<Api["get"]>>) {
  expect(res.ok(), await res.text()).toBeTruthy();
  return (await res.json()).data;
}

test("외부 시스템 카탈로그는 연동 여부를 구분해서 알려준다", async () => {
  const admin = await apiAs("approver");
  const systems = await json(await admin.get("/api/v1/external/systems"));

  expect(systems.length).toBeGreaterThan(0);
  const ne = systems.find((s: { code: string }) => s.code === "NEXT_ENGINE");
  expect(ne, "카탈로그에 NEXT ENGINE 이 있어야 한다").toBeTruthy();

  // 주문 읽기는 실데이터로 검증됐다(docs/77·78·79).
  expect(ne.connector_available).toBe(true);
  expect(ne.capabilities.ORDER_IMPORT).toBe(true);
  // 클레임은 READ 만 되고 재무 반영이 없다. 넓게 선언하면 화면이 거짓말을 한다.
  expect(ne.capabilities.CLAIM_IMPORT).toBe(false);
  expect(ne.capabilities.PRODUCT_IMPORT).toBe(false);
  // 외부 쓰기는 영원히 없다.
  for (const [cap, ok] of Object.entries(ne.capabilities)) {
    if (cap.endsWith("_EXPORT")) expect(ok, `${cap} 는 false 여야 한다`).toBe(false);
  }

  // 연동이 없는 시스템은 전 capability 가 false 다 — 목록이 비어 있는 것과 다르다.
  unimplementedSystemCode();
  const again = await json(await admin.get("/api/v1/external/systems"));
  const none = again.find((s: { code: string }) => s.code === "E2E_NO_CONNECTOR");
  expect(none.connector_available).toBe(false);
  expect(Object.keys(none.capabilities).length).toBeGreaterThan(0);
  expect(Object.values(none.capabilities).every((v) => v === false)).toBe(true);

  await admin.dispose();
});

test("계정을 등록해도 비밀은 어디에도 나오지 않는다", async () => {
  const admin = await apiAs("approver");
  const systems = await json(await admin.get("/api/v1/external/systems"));
  const ne = systems.find((s: { code: string }) => s.code === "NEXT_ENGINE");

  const account = await json(await admin.post("/api/v1/external/accounts", {
    data: {
      external_system_id: ne.id,
      display_name: "본사 계정",
      account_code: "MAIN",
      external_account_identifier: "CORP-001",
    },
  }));

  expect(account.credential_configured).toBe(false);
  const body = JSON.stringify(account);
  for (const banned of ["password", "secret", "access_token", "vault_entry_id",
                        "credential_secret_ref", "ciphertext"]) {
    expect(body, `${banned} 가 응답에 있다`).not.toContain(banned);
  }

  await admin.dispose();
});

test("커넥터가 없으면 동기화는 미지원이다 — 성공이 아니다", async () => {
  const admin = await apiAs("approver");
  const account = await makeAccount(admin, "X1");

  const res = await admin.post(
    `/api/v1/external/accounts/${account.id}/sync/products`, { data: {} },
  );
  // 501 — 입력이 잘못된 게 아니라 우리가 아직 못 한다.
  expect(res.status()).toBe(501);

  // 시도했다는 사실은 남는다.
  const runs = dbQuery<{ status: string; error_code: string }>(
    `SELECT status, error_code FROM external_sync_run
      WHERE org_id = '${SEED.orgId}' ORDER BY started_at DESC LIMIT 1`,
  );
  expect(runs[0].status).toBe("UNSUPPORTED");
  expect(runs[0].error_code).toBe("CONNECTOR_UNAVAILABLE");

  await admin.dispose();
});

test("한 외부 항목은 내부 하나에만 연결된다", async () => {
  const admin = await apiAs("approver");
  const systems = await json(await admin.get("/api/v1/external/systems"));
  const ne = systems.find((s: { code: string }) => s.code === "NEXT_ENGINE");
  const account = await json(await admin.post("/api/v1/external/accounts", {
    data: { external_system_id: ne.id, display_name: "매핑용", account_code: "MAP" },
  }));

  await json(await admin.post("/api/v1/external/mappings", {
    data: {
      external_account_id: account.id,
      external_entity_id: "12345",
      internal_entity_id: SEED.products.memo,
    },
  }));

  // 같은 외부 항목을 다른 상품에 붙이려 하면 거부된다.
  const dup = await admin.post("/api/v1/external/mappings", {
    data: {
      external_account_id: account.id,
      external_entity_id: "12345",
      internal_entity_id: SEED.products.name,
    },
  });
  expect(dup.status()).toBe(409);

  // 같은 상품에 다른 외부 항목을 붙이는 것은 허용된다.
  const second = await admin.post("/api/v1/external/mappings", {
    data: {
      external_account_id: account.id,
      external_entity_id: "67890",
      internal_entity_id: SEED.products.memo,
    },
  });
  expect(second.status()).toBe(201);

  const mappings = dbQuery(
    `SELECT 1 FROM external_entity_mapping
      WHERE internal_entity_id = '${SEED.products.memo}' AND deleted_at IS NULL`,
  );
  expect(mappings).toHaveLength(2);

  await admin.dispose();
});

test("재연결은 사유가 남는다", async () => {
  const admin = await apiAs("approver");
  const systems = await json(await admin.get("/api/v1/external/systems"));
  const ne = systems.find((s: { code: string }) => s.code === "NEXT_ENGINE");
  const account = await json(await admin.post("/api/v1/external/accounts", {
    data: { external_system_id: ne.id, display_name: "재연결", account_code: "RM" },
  }));
  const mapping = await json(await admin.post("/api/v1/external/mappings", {
    data: {
      external_account_id: account.id,
      external_entity_id: "999",
      internal_entity_id: SEED.products.memo,
    },
  }));

  await json(await admin.post(`/api/v1/external/mappings/${mapping.id}/remap`, {
    data: { internal_entity_id: SEED.products.price, reason: "상품 통합" },
  }));

  const row = dbOne<{ internal_entity_id: string }>(
    `SELECT internal_entity_id FROM external_entity_mapping WHERE id = '${mapping.id}'`,
  );
  expect(row?.internal_entity_id).toBe(SEED.products.price);

  const audit = dbQuery<{ reason: string }>(
    `SELECT reason FROM audit_log
      WHERE domain_table = 'external_entity_mapping' AND action = 'UPDATE'
        AND org_id = '${SEED.orgId}'`,
  );
  expect(audit.some((a) => a.reason?.includes("상품 통합"))).toBe(true);

  await admin.dispose();
});

test("외부 연동 화면이 연동 준비 중임을 정직하게 말한다", async ({ page }) => {
  const admin = await apiAs("approver");
  const systems = await json(await admin.get("/api/v1/external/systems"));
  const ne = systems.find((s: { code: string }) => s.code === "NEXT_ENGINE");
  await json(await admin.post("/api/v1/external/accounts", {
    data: { external_system_id: ne.id, display_name: "화면 확인 계정", account_code: "UI" },
  }));
  await admin.dispose();

  await loginAs(page, "approver");
  await page.goto("/integrations/external");

  await expect(page.getByRole("heading", { name: "외부 시스템 연동" })).toBeVisible();
  await expect(page.getByText("화면 확인 계정")).toBeVisible();

  // 연동이 없다는 사실을 화면이 말한다.
  await expect(page.getByText("연동 준비 중").first()).toBeVisible();
  await expect(page.getByText("미설정").first()).toBeVisible();

  // 동기화 버튼은 눌리지 않는다 — 눌렀는데 아무 일도 없으면 고장으로 읽힌다.
  await expect(page.getByRole("button", { name: "상품 동기화" })).toBeDisabled();
});

test("관리자가 아니면 외부 시스템 메뉴가 없다", async ({ page }) => {
  await loginAs(page, "operator");
  await page.goto("/dashboard");
  await expect(page.getByRole("link", { name: "외부 시스템" })).toHaveCount(0);
});

test("외부 계정은 조직을 넘지 않는다", async () => {
  // 다른 조직 계정 id 로 조회하면 404 여야 한다.
  const admin = await apiAs("approver");
  const res = await admin.get(
    "/api/v1/external/accounts/00000000-0000-0000-0000-0000000000aa",
  );
  expect(res.status()).toBe(404);
  await admin.dispose();
});


/* ────────────────────────────────────────────────────────────
 * 자격증명 — 무인 동기화용 인증정보 (M042)
 *
 * 여기서 쓰는 값은 전부 가짜다. 운영 credential 을 E2E 에 넣지 않는다 —
 * trace, video, CI 로그에 그대로 남는다.
 * ──────────────────────────────────────────────────────────── */

const FAKE_SECRET = "SELLER_OS_SUPER_SECRET_12345";

/**
 * **연동이 없는** 외부 시스템. 자격증명·미구현 검증은 여기에 붙인다.
 *
 * 예전에는 NEXT ENGINE 을 "미구현" 예시로 썼다. 이제 NEXT ENGINE 은 주문
 * 읽기가 실제로 되므로(docs/79), 그걸로 "미구현" 을 검증하면 기능이 붙을
 * 때마다 무관한 테스트가 깨진다.
 */
function unimplementedSystemCode(): string {
  dbExec(`
    INSERT INTO external_system (code, name, system_type, supports_api_key)
    VALUES ('E2E_NO_CONNECTOR', '연동 없는 툴', 'SELLER_TOOL', TRUE)
    ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name
  `);
  return "E2E_NO_CONNECTOR";
}

async function makeAccount(api: Api, code: string) {
  const wanted = unimplementedSystemCode();
  const systems = await json(await api.get("/api/v1/external/systems"));
  const target = systems.find((s: { code: string }) => s.code === wanted);
  return json(await api.post("/api/v1/external/accounts", {
    data: { external_system_id: target.id, display_name: `계정 ${code}`, account_code: code },
  }));
}

test("자격증명은 암호문으로만 저장되고 응답에 나오지 않는다", async () => {
  const admin = await apiAs("approver");
  const account = await makeAccount(admin, "CRED1");

  const updated = await json(await admin.put(
    `/api/v1/external/accounts/${account.id}/credential`,
    { data: { credential_kind: "API_KEY", secret: { api_key: FAKE_SECRET } } },
  ));

  expect(updated.credential_configured).toBe(true);
  expect(updated.credential_kind).toBe("API_KEY");
  expect(JSON.stringify(updated)).not.toContain(FAKE_SECRET);

  // DB 를 직접 뒤져도 평문이 없다.
  const dumped = dbQuery<{ blob: string }>(
    `SELECT string_agg(t::text, ' ') AS blob FROM credential_secret t
      WHERE org_id = '${SEED.orgId}'`,
  );
  expect(dumped[0].blob ?? "").not.toContain(FAKE_SECRET);
  // 계정 행에도 참조만 있다.
  const acc = dbOne<{ credential_secret_ref: string }>(
    `SELECT credential_secret_ref FROM external_account WHERE id = '${account.id}'`,
  );
  expect(acc?.credential_secret_ref).toBeTruthy();
  expect(acc?.credential_secret_ref).not.toContain(FAKE_SECRET);

  await admin.dispose();
});

test("검증에 걸린 입력값이 응답으로 되돌아오지 않는다", async () => {
  // 실제로 새고 있던 결함이다 — Pydantic 오류 항목의 `input` 이 그대로 나갔다.
  const admin = await apiAs("approver");
  const account = await makeAccount(admin, "CRED2");

  const res = await admin.put(
    `/api/v1/external/accounts/${account.id}/credential`,
    { data: { credential_kind: "API_KEY", secret: FAKE_SECRET } },
  );
  expect(res.status()).toBe(422);
  expect(await res.text(), "제출한 값이 응답에 돌아왔다").not.toContain(FAKE_SECRET);

  await admin.dispose();
});

test("필수 항목이 빠지면 거부하고, 값은 말하지 않는다", async () => {
  const admin = await apiAs("approver");
  const account = await makeAccount(admin, "CRED2B");

  const res = await admin.put(
    `/api/v1/external/accounts/${account.id}/credential`,
    { data: { credential_kind: "ACCESS_SECRET", secret: { access_key: "AK" } } },
  );
  expect(res.status()).toBe(422);
  const body = await res.text();
  // 어떤 칸이 비었는지는 알려 준다.
  expect(body).toContain("secret_key");
  expect(body).not.toContain(FAKE_SECRET);

  await admin.dispose();
});

test("자격증명 변경은 관리자만 할 수 있다", async () => {
  const admin = await apiAs("approver");
  const account = await makeAccount(admin, "CRED3");

  const operator = await apiAs("operator");
  const set = await operator.put(
    `/api/v1/external/accounts/${account.id}/credential`,
    { data: { credential_kind: "API_KEY", secret: { api_key: FAKE_SECRET } } },
  );
  expect(set.status()).toBe(403);

  const del = await operator.delete(
    `/api/v1/external/accounts/${account.id}/credential`,
  );
  expect(del.status()).toBe(403);

  await operator.dispose();
  await admin.dispose();
});

test("연결 시험은 실패 사유를 구분해서 남긴다", async () => {
  const admin = await apiAs("approver");
  const account = await makeAccount(admin, "CRED4");

  // 커넥터도 자격증명도 없다. 둘을 같은 "연결 실패" 로 뭉치면 무엇을 고쳐야
  // 할지 알 수 없다.
  const result = await json(await admin.post(
    `/api/v1/external/accounts/${account.id}/test-connection`, { data: {} },
  ));
  expect(result.status).toBe("CONNECTOR_UNAVAILABLE");

  const row = dbOne<{ last_test_status: string }>(
    `SELECT last_test_status FROM external_account WHERE id = '${account.id}'`,
  );
  expect(row?.last_test_status).toBe("CONNECTOR_UNAVAILABLE");

  await admin.dispose();
});

test("자격증명이 없으면 동기화가 나가기 전에 거부된다", async () => {
  const admin = await apiAs("approver");
  const account = await makeAccount(admin, "CRED5");

  // 커넥터가 없어 501 이 먼저다. 그 사실도 실행 기록에 남는다.
  const res = await admin.post(
    `/api/v1/external/accounts/${account.id}/sync/products`, { data: {} },
  );
  expect(res.status()).toBe(501);

  const runs = dbQuery<{ status: string; error_code: string }>(
    `SELECT status, error_code FROM external_sync_run
      WHERE external_account_id = '${account.id}' ORDER BY started_at DESC LIMIT 1`,
  );
  expect(runs[0].status).toBe("UNSUPPORTED");

  await admin.dispose();
});

test("자격증명 전체 흐름 — 설정 · 재조회 · 교체 · 삭제", async ({ page }) => {
  const admin = await apiAs("approver");
  await makeAccount(admin, "UICRED");
  await admin.dispose();

  await loginAs(page, "approver");
  await page.goto("/integrations/external");

  // ① 미설정
  await expect(page.getByText("자격증명").first()).toBeVisible();
  await expect(page.getByText("미설정").first()).toBeVisible();
  await expect(page.getByText("사용 불가 (연동 미구현)")).toBeVisible();

  // ② 설정
  await page.getByRole("button", { name: "설정", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();

  const secretInput = dialog.locator("#cred-api_key");
  // 값이 화면에 그대로 보이면 어깨너머·화면공유·screenshot 으로 샌다.
  await expect(secretInput).toHaveAttribute("type", "password");
  await secretInput.fill(FAKE_SECRET);
  await dialog.getByRole("button", { name: "저장" }).click();
  await expect(page.getByText("자격증명을 설정했습니다")).toBeVisible();
  await expect(page.getByText(/설정됨/).first()).toBeVisible();

  // ③ 새로고침해도 값은 어디에도 없다.
  await page.reload();
  await expect(page.getByText(/설정됨/).first()).toBeVisible();
  expect(await page.content()).not.toContain(FAKE_SECRET);
  const storage = await page.evaluate(() =>
    JSON.stringify({ ...window.localStorage, ...window.sessionStorage }));
  expect(storage).not.toContain(FAKE_SECRET);

  // ④ 교체 — 되읽기 칸이 아니라 빈 칸으로 열린다.
  await page.getByRole("button", { name: "교체" }).click();
  const rotate = page.getByRole("dialog");
  await expect(rotate.locator("#cred-api_key")).toHaveValue("");
  await rotate.locator("#cred-api_key").fill(FAKE_SECRET + "_ROTATED");
  await rotate.getByRole("button", { name: "교체" }).click();
  await expect(page.getByText("자격증명을 교체했습니다")).toBeVisible();

  // 살아 있는 비밀은 하나다.
  const alive = dbQuery(
    `SELECT 1 FROM credential_secret
      WHERE org_id = '${SEED.orgId}' AND deleted_at IS NULL`,
  );
  expect(alive).toHaveLength(1);

  // ⑤ 삭제
  await page.getByRole("button", { name: "삭제" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "삭제" }).click();
  await expect(page.getByText("자격증명을 삭제했습니다")).toBeVisible();
  await expect(page.getByText("미설정").first()).toBeVisible();

  const gone = dbQuery(
    `SELECT 1 FROM credential_secret
      WHERE org_id = '${SEED.orgId}' AND deleted_at IS NULL`,
  );
  expect(gone).toHaveLength(0);
});

test("화면의 연결 시험이 미구현과 실패를 구분해서 말한다", async ({ page }) => {
  const admin = await apiAs("approver");
  await makeAccount(admin, "UITEST");
  await admin.dispose();

  await loginAs(page, "approver");
  await page.goto("/integrations/external");

  await page.getByRole("button", { name: "연결 시험" }).click();
  // "연결 실패" 로 뭉치면 사용자는 자기 자격증명을 의심한다.
  await expect(page.getByText("연동 미구현 — 우리 쪽 준비가 안 됐습니다").first())
    .toBeVisible();
});
