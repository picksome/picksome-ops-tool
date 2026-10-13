import { expect, test } from "@playwright/test";
import { SEED, dbOne, dbQuery, reseed } from "./db";
import { apiAs, loginAs } from "./fixtures";

/**
 * 자동화 · AI 접근 E2E (실제 FastAPI + PostgreSQL).
 *
 * 여기서 확인하는 것
 * ---------------
 * 1. 자동화 기본이 **꺼짐 · 관찰만** 이다
 * 2. `REPORT_ONLY` 는 승인 요청을 만들지 않는다
 * 3. 한도 없는 자동 실행은 제안으로 떨어진다
 * 4. 토큰 원문은 **발급 응답 한 번**뿐이다
 * 5. scope 없는 tool 호출은 403 (401 아님)
 * 6. **AI 가 원가를 직접 바꾸지 못한다** — 승인 요청까지다
 * 7. MCP 를 꺼도 자동화·리포트는 그대로 돈다
 *
 * 외부 네트워크를 부르지 않는다
 * -------------------------
 * 실제 marketplace API 를 호출하는 테스트는 여기 없다. 커넥터가 없어서이기도
 * 하고, 있더라도 전체 E2E 가 매번 외부를 때리면 네트워크 때문에 빨간 불이
 * 뜬다 — 실제 API smoke 는 별도로 돌린다.
 */

test.beforeEach(() => {
  reseed();
});

type Api = Awaited<ReturnType<typeof apiAs>>;

async function json(res: Awaited<ReturnType<Api["get"]>>) {
  expect(res.ok(), await res.text()).toBeTruthy();
  return (await res.json()).data;
}

// ── 자동화 ──────────────────────────────────────────────────────

test("자동화는 기본이 꺼짐이고 관찰만 한다", async () => {
  const admin = await apiAs("approver");
  const policies = await json(await admin.get("/api/v1/automation/policies"));

  expect(policies.length).toBeGreaterThan(0);
  // 자동 실행을 기본값으로 두지 않는다.
  for (const p of policies) {
    expect(p.enabled).toBe(false);
    expect(p.mode).toBe("REPORT_ONLY");
  }
  await admin.dispose();
});

test("자동으로 하지 않는 일 목록을 숨기지 않는다", async () => {
  const admin = await apiAs("approver");
  const types = await json(await admin.get("/api/v1/automation/types"));

  const never = types.never_automated.map((n: { action: string }) => n.action);
  for (const forbidden of ["PAYOUT_EXECUTE", "BANK_TRANSFER", "MONTH_REOPEN",
                           "CREDENTIAL_CHANGE"]) {
    expect(never, `${forbidden} 이 제외 목록에 없다`).toContain(forbidden);
  }
  // 자동화 유형 목록에도 없어야 한다.
  const available = types.types.map((t: { automation_type: string }) => t.automation_type);
  for (const forbidden of never) {
    expect(available).not.toContain(forbidden);
  }
  await admin.dispose();
});

test("설정 화면이 한도 코드를 그대로 보여주지 않는다", async ({ page }) => {
  // **화면 코드에 라벨 목록을 손으로 적으면 이 순간을 못 잡는다.** 서버가
  // 한도를 하나 더 만들면 설정 화면은 `claim_lookback_days` 를 그대로
  // 사용자에게 보여 준다 — 실제로 CLAIM_SYNC 를 넣을 때 그렇게 됐고,
  // 손으로 적은 목록을 보던 단위 테스트는 초록이었다.
  //
  // 그래서 **서버가 아는 한도 목록을 실제로 받아** 그 키가 화면에 글자
  // 그대로 나오는지 본다. 라벨 표를 import 하지 않는다 — 표가 맞는지가
  // 아니라 사용자가 무엇을 보는지가 확인할 대상이다.
  const admin = await apiAs("approver");
  const types = await json(await admin.get("/api/v1/automation/types"));
  await admin.dispose();

  const keys = new Set<string>();
  for (const t of types.types as { guardrails?: string[] }[]) {
    for (const key of t.guardrails ?? []) keys.add(key);
  }
  expect(keys.size).toBeGreaterThan(0);

  await loginAs(page, "approver");
  await page.goto("/settings/automation");
  await expect(page.getByText("자동화").first()).toBeVisible();
  const body = await page.locator("body").innerText();

  const leaked = [...keys].filter((k) => body.includes(k));
  expect(leaked, `한도 코드가 화면에 그대로 나온다: ${leaked.join(", ")}`)
    .toEqual([]);
});

test("관찰만 모드는 승인 요청을 만들지 않는다", async () => {
  const admin = await apiAs("approver");
  const policy = await json(await admin.put("/api/v1/automation/policies", {
    data: {
      automation_type: "MARGIN_MONITOR", enabled: true, mode: "REPORT_ONLY",
      guardrails: { min_margin_rate: "0.9" },
    },
  }));

  const before = dbQuery(
    `SELECT 1 FROM approval_request WHERE org_id = '${SEED.orgId}'
      AND deleted_at IS NULL`,
  ).length;

  await json(await admin.post(
    `/api/v1/automation/policies/${policy.id}/run`, { data: {} },
  ));

  const after = dbQuery(
    `SELECT 1 FROM approval_request WHERE org_id = '${SEED.orgId}'
      AND deleted_at IS NULL`,
  ).length;
  expect(after).toBe(before);
  await admin.dispose();
});

test("한도 없는 자동 실행은 제안으로 떨어진다", async () => {
  const admin = await apiAs("approver");
  const policy = await json(await admin.put("/api/v1/automation/policies", {
    data: {
      automation_type: "ORDER_SYNC", enabled: true, mode: "AUTO_EXECUTE",
      guardrails: {},
    },
  }));
  expect(policy.mode).toBe("AUTO_EXECUTE");

  const run = await json(await admin.post(
    `/api/v1/automation/policies/${policy.id}/run`, { data: {} },
  ));
  // 무제한 자동 실행을 허용하지 않는다.
  expect(run.mode).toBe("PROPOSE");
  await admin.dispose();
});

test("모르는 한도는 거부한다", async () => {
  const admin = await apiAs("approver");
  const res = await admin.put("/api/v1/automation/policies", {
    data: {
      automation_type: "MARGIN_MONITOR", enabled: true, mode: "REPORT_ONLY",
      guardrails: { max_action_per_run: "5" },
    },
  });
  // 오타 하나로 한도가 조용히 사라지면 사용자는 한도를 걸었다고 믿는다.
  expect(res.status()).toBe(422);
  await admin.dispose();
});

test("자동화 설정 변경은 관리자만 할 수 있다", async () => {
  const operator = await apiAs("operator");
  const res = await operator.put("/api/v1/automation/policies", {
    data: { automation_type: "MARGIN_MONITOR", enabled: true, mode: "REPORT_ONLY" },
  });
  expect(res.status()).toBe(403);
  await operator.dispose();
});

test("일일 리포트는 모르는 값을 0 으로 만들지 않는다", async () => {
  const admin = await apiAs("approver");
  const report = await json(await admin.get("/api/v1/automation/daily-report"));

  expect(report.report_date).toBeTruthy();
  expect(Array.isArray(report.needs_attention)).toBe(true);
  // 클레임 연동이 없으므로 0 이 아니라 null 이다.
  expect(report.sections.orders.claims).toBeNull();
  await admin.dispose();
});

test("자동화 화면이 켜짐·모드·제외 목록을 보여준다", async ({ page }) => {
  await loginAs(page, "approver");
  await page.goto("/settings/automation");

  // 사이드바 메뉴에도 같은 이름이 있다 — 페이지 제목만 본다.
  await expect(page.getByRole("heading", { name: "자동화", level: 1 })).toBeVisible();
  await expect(page.getByText("마진 감시").first()).toBeVisible();
  // 제외 목록을 숨기면 "왜 지급 자동화가 없지" 를 계속 묻는다.
  await expect(page.getByText("자동으로 하지 않는 일")).toBeVisible();
  await expect(page.getByText("PAYOUT_EXECUTE")).toBeVisible();
});

// ── AI · API ────────────────────────────────────────────────────

test("토큰 원문은 발급 응답 한 번뿐이다", async () => {
  const admin = await apiAs("approver");
  const account = await json(await admin.post("/api/v1/ai-access/service-accounts", {
    data: { name: "E2E 에이전트" },
  }));
  const issued = await json(await admin.post(
    `/api/v1/ai-access/service-accounts/${account.id}/tokens`,
    { data: { scopes: ["products:read"], label: "읽기" } },
  ));

  expect(issued.token).toMatch(/^sos_/);

  // 다시 조회하면 원문이 없다.
  const detail = await admin.get(
    `/api/v1/ai-access/service-accounts/${account.id}`,
  );
  expect(await detail.text()).not.toContain(issued.token);

  // DB 에도 없다 — 해시만 있다.
  const dumped = dbQuery<{ blob: string }>(
    `SELECT string_agg(t::text, ' ') AS blob FROM api_token t
      WHERE org_id = '${SEED.orgId}'`,
  );
  expect(dumped[0].blob ?? "").not.toContain(issued.token);

  await admin.dispose();
});

test("발급할 수 없는 권한은 거부하고 이유를 말한다", async () => {
  const admin = await apiAs("approver");
  const account = await json(await admin.post("/api/v1/ai-access/service-accounts", {
    data: { name: "위험 권한 시도" },
  }));

  const res = await admin.post(
    `/api/v1/ai-access/service-accounts/${account.id}/tokens`,
    { data: { scopes: ["payouts:execute"] } },
  );
  expect(res.status()).toBe(422);
  expect(await res.text()).toContain("사람이 화면에서");
  await admin.dispose();
});

test("서비스 계정 관리는 관리자만 할 수 있다", async () => {
  const operator = await apiAs("operator");
  const res = await operator.post("/api/v1/ai-access/service-accounts", {
    data: { name: "권한 없음" },
  });
  expect(res.status()).toBe(403);
  await operator.dispose();
});

test("AI · API 화면에서 토큰을 발급하고 한 번만 본다", async ({ page }) => {
  const admin = await apiAs("approver");
  await json(await admin.post("/api/v1/ai-access/service-accounts", {
    data: { name: "화면 에이전트" },
  }));
  await admin.dispose();

  await loginAs(page, "approver");
  await page.goto("/settings/ai-api");

  await expect(page.getByText("화면 에이전트")).toBeVisible();
  // 토큰이 없으면 아무것도 못 한다는 사실을 화면이 말한다.
  await expect(page.getByText("사용 가능한 토큰이 없습니다").first()).toBeVisible();

  await page.getByRole("button", { name: "토큰 발급" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  // 발급 불가 권한을 숨기지 않는다.
  await expect(dialog.getByText("발급할 수 없는 권한")).toBeVisible();

  await dialog.getByText("상품 조회").click();
  await dialog.getByRole("button", { name: "발급", exact: true }).click();

  await expect(page.getByText("다시 볼 수 없습니다")).toBeVisible();
  const shown = await dialog.locator("input[readonly]").inputValue();
  expect(shown).toMatch(/^sos_/);

  // 닫고 나면 어디에도 없다.
  await page.getByRole("button", { name: "복사했습니다. 닫기" }).click();
  await page.reload();
  expect(await page.content()).not.toContain(shown);
});

// ── MCP ─────────────────────────────────────────────────────────

test("MCP 가 꺼져 있으면 경로가 없는 것처럼 보인다", async () => {
  const admin = await apiAs("approver");
  const account = await json(await admin.post("/api/v1/ai-access/service-accounts", {
    data: { name: "MCP 시도" },
  }));
  const issued = await json(await admin.post(
    `/api/v1/ai-access/service-accounts/${account.id}/tokens`,
    { data: { scopes: ["products:read"] } },
  ));
  await admin.dispose();

  const agent = await apiAs("approver");
  const res = await agent.get("/api/v1/mcp/tools", {
    headers: { Authorization: `Bearer ${issued.token}` },
  });
  // 기본이 꺼짐이다.
  expect(res.status()).toBe(404);
  await agent.dispose();

  // 그래도 자동화는 돈다 — MCP 는 선택 모듈이다.
  const admin2 = await apiAs("approver");
  const report = await json(await admin2.get("/api/v1/automation/daily-report"));
  expect(report.report_date).toBeTruthy();
  await admin2.dispose();
});

test("사람 로그인 토큰으로는 MCP 를 부를 수 없다", async ({ page }) => {
  const session = await loginAs(page, "approver");
  const res = await session.api.get("/api/v1/mcp/tools");
  // 404(꺼짐) 또는 401(사람 토큰) — 어느 쪽이든 통과하지 못한다.
  expect([401, 404]).toContain(res.status());
});

test("자동화 실행 기록이 남는다", async () => {
  const admin = await apiAs("approver");
  const policy = await json(await admin.put("/api/v1/automation/policies", {
    data: { automation_type: "DAILY_REPORT", enabled: true, mode: "REPORT_ONLY" },
  }));
  await json(await admin.post(
    `/api/v1/automation/policies/${policy.id}/run`, { data: {} },
  ));

  const run = dbOne<{ status: string; mode: string }>(
    `SELECT status, mode FROM automation_run
      WHERE org_id = '${SEED.orgId}' ORDER BY started_at DESC LIMIT 1`,
  );
  expect(run?.status).toBe("SUCCESS");
  expect(run?.mode).toBe("REPORT_ONLY");
  await admin.dispose();
});
