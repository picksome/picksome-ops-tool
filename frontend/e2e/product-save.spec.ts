/**
 * 직접 저장 · 승인 요청 · 적용 — **실제 백엔드와 실제 DB** 로 검증.
 *
 * 모든 단언이 화면과 DB 를 **둘 다** 본다. 화면만 보면 "저장했습니다" 토스트는 뜨는데
 * DB 는 옛날 값인 상태를 통과시킨다 — 그게 이 프로젝트에서 실제로 있었던 사고다.
 */

import { test, expect, apiAs, loginAs, openProductDrawer } from "./fixtures";
import {
  SEED, approvalsFor, auditFor, dbExec, holdExecutorLock, product, reseed,
} from "./db";

test.beforeEach(() => {
  // 테스트마다 초기 상태로. 승인 요청이 남아 있으면 다음 테스트가 409 로 막힌다.
  reseed();
});

test("메모 직접 저장 — 다시 열어도 DB 값이 유지된다", async ({ page }) => {
  await loginAs(page, "operator");
  await openProductDrawer(page, SEED.products.memo);

  // id 로 찾는다 — 라벨 문구가 바뀌어도 안 깨지고, 값으로 찾으면 같은 값을 가진
  // 다른 필드와 헷갈린다.
  const memo = page.locator("#p-memo");
  await expect(memo).toHaveValue("초기 메모");
  await memo.fill("E2E 로 바꾼 메모");

  await page.getByRole("button", { name: "저장" }).click();
  await expect(page.getByText("저장했습니다")).toBeVisible();

  // ① DB 에 실제로 들어갔는가
  expect(product(SEED.products.memo)?.memo).toBe("E2E 로 바꾼 메모");

  // ② 승인 요청이 만들어지지 않았는가 — 메모까지 승인을 태우면 승인함이 잡음으로 찬다
  expect(approvalsFor(SEED.products.memo)).toHaveLength(0);

  // ③ 다시 열어도 살아 있는가 (낙관적 UI 가 아니라 진짜 저장인가)
  await openProductDrawer(page, SEED.products.memo);
  await expect(page.locator("#p-memo")).toHaveValue("E2E 로 바꾼 메모");
});

test("상품명 변경 — 승인 요청 생성 시 원 상품은 그대로", async ({ page }) => {
  await loginAs(page, "operator");
  await openProductDrawer(page, SEED.products.name);

  const before = product(SEED.products.name)!;

  const nameInput = page.locator("#p-name");
  await expect(nameInput).toHaveValue("E2E 이름변경 상품");
  await nameInput.fill("승인 대기 중인 새 이름");

  // 버튼 문구가 바뀌어야 한다 — 누르기 **전에** 승인 대상임을 알아야 한다
  const submit = page.getByRole("button", { name: "승인 요청" });
  await expect(submit).toBeVisible();
  await submit.click();

  await expect(page.getByText("승인 요청을 올릴까요?")).toBeVisible();
  await page.getByPlaceholder("예: 매입처 단가 인상 반영").fill("E2E 사유");
  await page.getByRole("button", { name: "승인 요청 올리기" }).click();

  await expect(page.getByText(/승인 요청 1건을 올렸습니다/)).toBeVisible();

  // ① 승인 요청이 생겼다
  const reqs = approvalsFor(SEED.products.name);
  expect(reqs).toHaveLength(1);
  expect(reqs[0].request_type).toBe("PRODUCT_INFO_CHANGE");
  expect(reqs[0].status).toBe("PENDING");

  // ② **상품은 아직 안 바뀌었다.** 이게 승인 워크플로의 존재 이유다.
  expect(product(SEED.products.name)?.name_ko).toBe(before.name_ko);
});

test("자기 승인 차단 → 다른 승인자가 승인하면 실제 상품 값이 바뀐다", async ({ page }) => {
  const operator = await loginAs(page, "operator");
  await openProductDrawer(page, SEED.products.name);

  await expect(page.locator("#p-name")).toHaveValue("E2E 이름변경 상품");
  await page.locator("#p-name").fill("승인 후 반영될 이름");
  await page.getByRole("button", { name: "승인 요청" }).click();
  await page.getByRole("button", { name: "승인 요청 올리기" }).click();
  await expect(page.getByText(/승인 요청 1건을 올렸습니다/)).toBeVisible();

  const requestId = approvalsFor(SEED.products.name)[0].id;

  // ── 요청자 본인은 승인할 수 없다 ──
  const selfApprove = await operator.api.post(`/api/v1/approvals/${requestId}/approve`, {
    data: { comment: "내가 내 걸 승인" },
  });
  expect(selfApprove.status()).toBe(403);
  expect((await selfApprove.json()).error.code).toBe("APPROVAL_SELF_APPROVAL_FORBIDDEN");

  // ── 다른 승인자로 승인 + 적용 ──
  const approver = await apiAs("approver");
  expect((await approver.post(`/api/v1/approvals/${requestId}/approve`, {
    data: { comment: "E2E 승인" },
  })).ok()).toBeTruthy();
  expect((await approver.post(`/api/v1/approvals/${requestId}/apply`)).ok()).toBeTruthy();
  await approver.dispose();

  // ① 상품이 실제로 바뀌었다
  expect(product(SEED.products.name)?.name_ko).toBe("승인 후 반영될 이름");

  // ② 요청이 APPLIED 로 끝났다
  const reqs = approvalsFor(SEED.products.name);
  expect(reqs[0].status).toBe("APPLIED");
  expect(reqs[0].decision_outcome).toBe("APPROVED");

  // ③ 감사 로그가 남았다 — 누가 언제 무엇을 바꿨는지 추적할 수 없으면 승인은 무의미하다
  const audit = auditFor(SEED.products.name);
  expect(audit.length).toBeGreaterThan(0);
  expect(audit.some((a) => a.domain_table === "product")).toBe(true);
});

test("원가 승인 — 민감 diff 권한이 없으면 API 응답에 값이 없다", async ({ page }) => {
  const operator = await loginAs(page, "operator");
  await openProductDrawer(page, SEED.products.cost);

  // 요청 생성은 API 로 한다 — 이 시나리오가 검증하는 건 **redaction** 이지 입력 폼이 아니다.
  const created = await operator.api.post("/api/v1/approvals", {
    data: {
      target_type: "product",
      target_id: SEED.products.cost,
      request_type: "COST_CHANGE",
      proposed_changes: { cost_amount: 77000 },
      reason: "E2E 원가 인상",
      submit: true,
    },
  });
  expect(created.ok()).toBeTruthy();
  const requestId = (await created.json()).data.id;

  // ── 권한 없는 사용자(READONLY) ──
  const readonly = await apiAs("readonly");
  const redacted = await readonly.get(`/api/v1/approvals/${requestId}`);
  expect(redacted.ok()).toBeTruthy();
  const redactedBody = (await redacted.json()).data;

  // **응답 본문에 실제 금액이 없어야 한다.** CSS 로 가리는 건 방어가 아니다.
  expect(JSON.stringify(redactedBody)).not.toContain("77000");
  expect(redactedBody.sensitive_visible).toBe(false);
  // 키는 남아야 한다 — 지우면 "변경 없음" 과 "볼 수 없음" 이 구분되지 않는다
  expect(Object.keys(redactedBody.proposed_changes_json)).toContain("cost_amount");
  await readonly.dispose();

  // ── 권한 있는 사용자(ADMIN) ──
  const admin = await apiAs("approver");
  const visibleBody = (await (await admin.get(`/api/v1/approvals/${requestId}`)).json()).data;
  expect(visibleBody.sensitive_visible).toBe(true);
  expect(visibleBody.proposed_changes_json.cost_amount).toBe(77000);
  await admin.dispose();

  // 원가는 아직 안 바뀌었다
  expect(product(SEED.products.cost)?.cost_amount).toBe(10000);
});

test("updated_at 충돌 — 늦게 저장한 쪽이 409 를 받고 앞사람 값이 남는다", async ({ page }) => {
  const operator = await loginAs(page, "operator");
  await page.goto("/products");

  const detail = await operator.api.get(`/api/v1/products/${SEED.products.race}`);
  const staleToken = (await detail.json()).data.version_token;
  expect(staleToken).toBeTruthy();

  // A 가 먼저 저장한다.
  const first = await operator.api.patch(`/api/v1/products/${SEED.products.race}`, {
    data: { memo: "A 가 먼저 저장", expected_version: staleToken },
  });
  expect(first.ok()).toBeTruthy();

  // B 는 예전 토큰을 들고 저장한다.
  const second = await operator.api.patch(`/api/v1/products/${SEED.products.race}`, {
    data: { memo: "B 가 덮어쓰기 시도", expected_version: staleToken },
  });
  expect(second.status()).toBe(409);

  const err = (await second.json()).error;
  expect(err.code).toBe("PRODUCT_VERSION_CONFLICT");
  // 프론트가 최신값 위에서 다시 판단하려면 현재 토큰이 필요하다
  expect(err.details.current_version).toBeTruthy();
  expect(err.details.current_version).not.toBe(staleToken);

  // **A 의 값이 살아 있어야 한다.**
  expect(product(SEED.products.race)?.memo).toBe("A 가 먼저 저장");
});

test("월 잠금 — HELD_BY_LOCK 으로 보류되고 재오픈 후 적용된다", async ({ page }) => {
  const operator = await loginAs(page, "operator");
  await page.goto("/products");

  // 적용일을 KST 오늘로 둔다 — executor 가 KST 기준으로 도래 여부를 판단한다.
  const today = new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 10);
  const ym = today.slice(0, 7);

  const created = await operator.api.post("/api/v1/approvals", {
    data: {
      target_type: "product",
      target_id: SEED.products.lock,
      request_type: "PRODUCT_INFO_CHANGE",
      proposed_changes: { name_ko: "월잠금 통과 후 이름" },
      reason: "E2E 월 잠금",
      effective_from: today,
      submit: true,
    },
  });
  expect(created.ok()).toBeTruthy();
  const requestId = (await created.json()).data.id;

  const admin = await apiAs("approver");
  const approved = await admin.post(`/api/v1/approvals/${requestId}/approve`, {
    data: { comment: "E2E 월잠금 승인" },
  });
  expect(approved.ok(), await approved.text()).toBeTruthy();

  // ── 해당 월을 잠근다 ──
  // 잠금 판정은 **`close_state`** 가 한다 (M017). `status` 가 아니다 —
  // `status` 는 정산 진행 단계고, `close_state` 가 쓰기 가드의 근거다.
  // 둘을 헷갈리면 "잠갔는데 안 잠긴다".
  //
  // `ck_ms_confirmed_meta`: LOCKED/CONFIRMED 는 confirmed_at·confirmed_by 가 있어야 한다.
  // 누가 언제 잠갔는지 없는 잠금은 추적이 안 되므로 DB 가 막는다.
  dbExec(`INSERT INTO monthly_settlement
            (id, org_id, business_key, year_month, status, close_state,
             confirmed_at, confirmed_by)
          VALUES (gen_random_uuid(), '${SEED.orgId}', 'E2E-MS-1', '${ym}', 'LOCKED',
                  'LOCKED', now(), '${SEED.users.approver.id}')`);

  // ── executor 실행 → 잠금 때문에 보류 ──
  expect((await admin.post("/api/v1/approvals/run-due?batch_size=10")).ok()).toBeTruthy();

  let reqs = approvalsFor(SEED.products.lock);
  expect(reqs[0].auto_apply_status).toBe("HELD_BY_LOCK");
  // **자동 적용 실패를 반려로 바꾸지 않는다.** 승인 자체는 여전히 유효하다.
  expect(reqs[0].status).toBe("APPROVED");
  expect(product(SEED.products.lock)?.name_ko).toBe("E2E 월잠금 상품");

  // ── 월을 다시 연다 ──
  dbExec(`UPDATE monthly_settlement SET status = 'REOPENED', close_state = 'OPEN'
           WHERE org_id = '${SEED.orgId}' AND year_month = '${ym}'`);
  // HELD_BY_LOCK 은 자동 재시도하지 않는다 — 잠금을 풀지 결정하는 건 사람이다.
  // 재개도 사람의 행동이므로 여기서 명시적으로 큐에 되돌린다.
  dbExec(`UPDATE approval_request SET auto_apply_status = 'PENDING', next_retry_at = NULL
           WHERE id = '${requestId}'`);

  expect((await admin.post("/api/v1/approvals/run-due?batch_size=10")).ok()).toBeTruthy();
  await admin.dispose();

  reqs = approvalsFor(SEED.products.lock);
  expect(reqs[0].status).toBe("APPLIED");
  expect(product(SEED.products.lock)?.name_ko).toBe("월잠금 통과 후 이름");
});

test("executor 중복 실행 — 잠금이 잡혀 있으면 409 로 차단된다", async ({ page }) => {
  const admin = await loginAs(page, "approver");
  await page.goto("/approvals");

  // HTTP 요청 두 개를 동시에 던져 겹치기를 기대하면 안 된다 — 배치가 비어 있으면
  // 몇 ms 만에 끝나 순서대로 처리되고, 테스트가 간헐적으로 통과한다.
  // "가끔 통과하는 테스트" 는 방어가 아니다. 잠금을 확실히 붙잡고 한 번 던진다.
  const release = await holdExecutorLock(30);
  try {
    const blocked = await admin.api.post("/api/v1/approvals/run-due?batch_size=50");
    expect(blocked.status()).toBe(409);
    expect((await blocked.json()).error.code).toBe("APPROVAL_EXECUTOR_ALREADY_RUNNING");
  } finally {
    release();
  }

  // 잠금이 풀리면 정상 실행된다 — 한 번 막혔다고 영구히 잠기면 안 된다.
  await expect
    .poll(
      async () => (await admin.api.post("/api/v1/approvals/run-due?batch_size=50")).status(),
      { timeout: 15_000 },
    )
    .toBe(200);
});
