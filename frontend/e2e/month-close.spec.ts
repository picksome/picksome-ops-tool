import { expect, test } from "@playwright/test";
import { SEED, dbExec, dbOne, dbQuery, reseed } from "./db";
import { loginAs } from "./fixtures";

/**
 * 월 마감 · 확정 snapshot · 재오픈 승인 E2E (실제 FastAPI + PostgreSQL).
 *
 * 이 파일이 확인하는 것
 * -----------------
 * 화면 문구가 아니라 **DB 실제 값**이다. "확정했습니다" 토스트는 뜨는데
 * snapshot 이 없는 상태를 통과시키지 않는다.
 *
 * 정산 대사(§1~25)는 실제 정산서 파일이 없어 여기서 다루지 않는다.
 * 대신 **정산 미연결이 경고로 드러나는지**를 확인한다 — 없는 데이터를
 * 정상으로 간주하지 않는 것이 이 도메인의 핵심 규칙이다.
 */

const YM = SEED.months.open;

test.beforeEach(() => {
  reseed();
});

test.describe("월 마감 흐름", () => {
  test("검증 → 경고 확인 → 확정 → snapshot 박제", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    // ① 검증
    const validated = await api.post(`/api/v1/monthly-close/${YM}/validate`);
    expect(validated.ok()).toBeTruthy();
    const body = await validated.json();
    const items = body.data as { code: string; grade: string }[];

    // 정산 자료가 없는 것을 "이상 없음" 으로 넘기지 않는다
    expect(items.map((i) => i.code)).toContain("SETTLEMENT_NOT_CONNECTED");

    // 검증 run 이 실제로 기록됐다 — 덮어쓰기가 아니다
    const runs = dbQuery<{ trigger: string; status: string }>(
      `SELECT trigger, status FROM monthly_close_check_run
        WHERE org_id = '${SEED.orgId}' AND year_month = '${YM}'`,
    );
    expect(runs.length).toBeGreaterThan(0);
    expect(runs.every((r) => r.status === "COMPLETED")).toBeTruthy();

    // ② 경고를 확인하지 않으면 확정이 거부된다
    const rejected = await api.post(`/api/v1/monthly-close/${YM}/confirm`, {
      data: { acknowledged_warnings: [] },
    });
    expect(rejected.ok()).toBeFalsy();

    // ③ 확인한 뒤 확정
    const warnings = items.filter((i) => i.grade !== "BLOCKING").map((i) => i.code);
    const confirmed = await api.post(`/api/v1/monthly-close/${YM}/confirm`, {
      data: { acknowledged_warnings: warnings },
    });
    expect(confirmed.ok()).toBeTruthy();

    // ④ snapshot 이 실제로 박제됐는지 DB 로 확인한다
    const snap = dbOne<{
      generation: number;
      taken_at_state: string;
      profit_basis: string;
      settlement_connected: boolean;
      content_hash: string;
    }>(
      `SELECT generation, taken_at_state, profit_basis, settlement_connected, content_hash
         FROM monthly_close_snapshot
        WHERE org_id = '${SEED.orgId}' AND year_month = '${YM}'
          AND superseded_at IS NULL`,
    );
    expect(snap).not.toBeNull();
    expect(snap!.generation).toBe(1);
    expect(snap!.taken_at_state).toBe("CONFIRMED");
    // 정산이 연결되지 않았으므로 "대사 완료" 라고 주장하지 않는다
    expect(snap!.settlement_connected).toBe(false);
    expect(snap!.profit_basis).toBe("SALES_AND_EXPENSE_ONLY");
    expect(snap!.content_hash).toHaveLength(64);

    const ms = dbOne<{ close_state: string; close_generation: number }>(
      `SELECT close_state, close_generation FROM monthly_settlement
        WHERE org_id = '${SEED.orgId}' AND year_month = '${YM}'`,
    );
    expect(ms!.close_state).toBe("CONFIRMED");
    expect(ms!.close_generation).toBe(1);
  });

  test("확정 숫자는 화면에서도 snapshot 값으로 보인다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    const v = await api.post(`/api/v1/monthly-close/${YM}/validate`);
    const items = (await v.json()).data as { code: string; grade: string }[];
    await api.post(`/api/v1/monthly-close/${YM}/confirm`, {
      data: {
        acknowledged_warnings: items
          .filter((i) => i.grade !== "BLOCKING")
          .map((i) => i.code),
      },
    });

    await page.goto(`/settlements/monthly/${YM}`);
    // 정산 미연결이 화면에 명시된다 — 0원과 구분되어야 한다.
    // 검증 목록과 snapshot 패널 양쪽에 나오므로 .first() 로 좁힌다.
    await expect(
      page.getByText("정산 자료가 연결되지 않았습니다", { exact: false }).first(),
    ).toBeVisible();
    // snapshot 에서 읽었다는 사실이 화면에 드러난다
    await expect(page.getByText("기준으로 박제됨", { exact: false })).toBeVisible();
  });

  test("확정 후 데이터가 바뀌면 잠글 수 없다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    const v = await api.post(`/api/v1/monthly-close/${YM}/validate`);
    const items = (await v.json()).data as { code: string; grade: string }[];
    await api.post(`/api/v1/monthly-close/${YM}/confirm`, {
      data: {
        acknowledged_warnings: items
          .filter((i) => i.grade !== "BLOCKING")
          .map((i) => i.code),
      },
    });

    // 확정 후 비용을 하나 더 넣는다(잠금 전이라 DB 는 막지 않는다)
    dbExec(`
      INSERT INTO expenses
             (id, org_id, expense_date, category_id, description,
              gross_amount, supply_amount, vat_amount, vat_inclusion_type,
              tax_type, payment_method, is_personal_card, is_business_use,
              evidence_type, evidence_status,
              is_tax_deductible, is_settlement_deductible)
      VALUES (gen_random_uuid(), '${SEED.orgId}', '${YM}-15',
              '${SEED.expenses.category}', '확정 후 추가 비용',
              50000, 50000, 0, 'INCLUDED', 'GENERAL', 'BUSINESS_CARD',
              FALSE, TRUE, 'CARD_SLIP', 'PRESENT', TRUE, TRUE)
    `);

    const drift = await api.get(`/api/v1/monthly-close/${YM}/drift`);
    expect(((await drift.json()).data as { drifted: boolean }).drifted).toBe(true);

    // 잠금 권한은 ADMIN 만이다. 회계 담당이 부르면 403 —
    // 잠금은 그 달을 영구 봉인하는 행위라 확정보다 좁은 권한을 요구한다.
    const byAccountant = await api.post(`/api/v1/monthly-close/${YM}/lock`);
    expect(byAccountant.status()).toBe(403);

    // ADMIN 이 시도해도 거부되어야 한다 —
    // 박제된 숫자와 어긋난 채 봉인하면 안 된다.
    const admin = await loginAs(page, "approver");
    const locked = await admin.api.post(`/api/v1/monthly-close/${YM}/lock`);
    expect(locked.status()).toBe(409);

    const ms = dbOne<{ close_state: string }>(
      `SELECT close_state FROM monthly_settlement
        WHERE org_id = '${SEED.orgId}' AND year_month = '${YM}'`,
    );
    expect(ms!.close_state).toBe("CONFIRMED");
  });

  test("잠긴 달은 판매·비용 쓰기가 막힌다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    // 7월은 seed 가 LOCKED 로 심는다
    const res = await api.post("/api/v1/expenses", {
      data: {
        expense_date: `${SEED.months.locked}-20`,
        category_id: SEED.expenses.category,
        description: "잠긴 달 비용",
        gross_amount: 10000,
        payment_method: "BUSINESS_CARD",
      },
    });
    expect(res.ok()).toBeFalsy();
    expect(res.status()).toBe(409);
  });
});

test.describe("재오픈 승인", () => {
  test("재오픈은 승인을 거치고, 과거 snapshot 은 남는다", async ({ page }) => {
    const accountant = await loginAs(page, "accountant");

    // 확정 → 잠금
    const v = await accountant.api.post(`/api/v1/monthly-close/${YM}/validate`);
    const items = (await v.json()).data as { code: string; grade: string }[];
    const warnings = items.filter((i) => i.grade !== "BLOCKING").map((i) => i.code);
    await accountant.api.post(`/api/v1/monthly-close/${YM}/confirm`, {
      data: { acknowledged_warnings: warnings },
    });
    // 잠금은 ADMIN 만 할 수 있다.
    const admin = await loginAs(page, "approver");
    const lockRes = await admin.api.post(`/api/v1/monthly-close/${YM}/lock`);
    expect(lockRes.ok(), await lockRes.text()).toBeTruthy();

    // 재오픈 요청 — 이 호출은 달을 열지 않는다
    const reqRes = await accountant.api.post(
      `/api/v1/monthly-close/${YM}/reopen-request`,
      { data: { reason: "정산서가 마감 후 도착해 재확정 필요", submit: true } },
    );
    expect(reqRes.ok()).toBeTruthy();
    const requestId = (await reqRes.json()).data.id as string;

    let ms = dbOne<{ close_state: string }>(
      `SELECT close_state FROM monthly_settlement
        WHERE org_id = '${SEED.orgId}' AND year_month = '${YM}'`,
    );
    expect(ms!.close_state).toBe("LOCKED");

    // ADMIN 이 승인하고 적용해야 열린다
    const approveRes = await admin.api.post(
      `/api/v1/approvals/${requestId}/approve`,
      { data: {} },
    );
    expect(approveRes.ok()).toBeTruthy();
    const applyRes = await admin.api.post(`/api/v1/approvals/${requestId}/apply`, {
      data: {},
    });
    expect(applyRes.ok()).toBeTruthy();

    ms = dbOne<{ close_state: string }>(
      `SELECT close_state FROM monthly_settlement
        WHERE org_id = '${SEED.orgId}' AND year_month = '${YM}'`,
    );
    expect(ms!.close_state).toBe("OPEN");

    // 과거 확정 숫자는 지워지지 않고 superseded 로 남는다
    const snaps = dbQuery<{ generation: number; superseded_at: string | null }>(
      `SELECT generation, superseded_at FROM monthly_close_snapshot
        WHERE org_id = '${SEED.orgId}' AND year_month = '${YM}'
        ORDER BY generation`,
    );
    expect(snaps).toHaveLength(1);
    expect(snaps[0].superseded_at).not.toBeNull();

    // 재오픈 감사 기록에 승인 요청 id 가 남는다
    const audit = dbOne<{ reason: string; snapshot_before_json: Record<string, unknown> }>(
      `SELECT sra.reason, sra.snapshot_before_json
         FROM settlement_reopen_audit sra
         JOIN monthly_settlement ms ON ms.id = sra.monthly_settlement_id
        WHERE ms.org_id = '${SEED.orgId}' AND ms.year_month = '${YM}'`,
    );
    expect(audit).not.toBeNull();
    expect(audit!.snapshot_before_json.approval_request_id).toBe(requestId);
  });

  test("열린 달에는 재오픈 요청을 만들 수 없다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    const res = await api.post(`/api/v1/monthly-close/${YM}/reopen-request`, {
      data: { reason: "열린 달인데 재오픈 시도", submit: true },
    });
    expect(res.ok()).toBeFalsy();
  });
});

test.describe("운영 이슈", () => {
  test("스캔이 이슈를 만들고, 무시한 이슈를 다시 열지 않는다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    const first = await api.post("/api/v1/operations/issues/scan", { data: {} });
    expect(first.ok(), await first.text()).toBeTruthy();

    const listed = await api.get("/api/v1/operations/issues?open_only=true&limit=100");
    const issues = (await listed.json()).data as { id: string; issue_type: string }[];
    expect(issues.length).toBeGreaterThan(0);

    const target = issues[0];
    const ignored = await api.patch(`/api/v1/operations/issues/${target.id}`, {
      data: { status: "IGNORED", resolution_note: "E2E — 확인 후 무시" },
    });
    expect(ignored.ok()).toBeTruthy();

    // 조건이 그대로여도 다시 열리지 않는다
    await api.post("/api/v1/operations/issues/scan", { data: {} });
    const after = dbOne<{ status: string; resolution_note: string }>(
      `SELECT status, resolution_note FROM operation_issue WHERE id = '${target.id}'`,
    );
    expect(after!.status).toBe("IGNORED");
    expect(after!.resolution_note).toBe("E2E — 확인 후 무시");
  });

  test("사유 없이 무시할 수 없다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    await api.post("/api/v1/operations/issues/scan", { data: {} });
    const listed = await api.get("/api/v1/operations/issues?open_only=true&limit=1");
    const issues = (await listed.json()).data as { id: string }[];
    test.skip(issues.length === 0, "스캔에서 이슈가 나오지 않았다");

    const res = await api.patch(`/api/v1/operations/issues/${issues[0].id}`, {
      data: { status: "IGNORED" },
    });
    expect(res.ok()).toBeFalsy();
  });

  test("이슈 센터 화면이 실제 데이터를 보여준다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    await api.post("/api/v1/operations/issues/scan", { data: {} });

    await page.goto("/operations/issues");
    await expect(page.getByRole("heading", { name: "운영 이슈" })).toBeVisible();
    // 스캔이 만든 이슈가 실제로 목록에 나온다.
    // 빈 상태 문구가 없다는 것만 보면 로딩 중과 구분되지 않는다.
    await expect(
      page.getByText("정산 자료가 연결되지 않았습니다", { exact: false }).first(),
    ).toBeVisible();
  });
});

test.describe("지급 장부", () => {
  test("지급 기록 없이는 PAID 가 되지 않고, 부분 지급이 반영된다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    // 배분 정책 없이도 지급 배치는 만들 수 있다(급여·기타 지급 경로)
    const created = await api.post("/api/v1/payouts", {
      data: { title: "E2E 지급 배치", period_year_month: YM },
    });
    expect(created.ok()).toBeTruthy();
    const batchId = (await created.json()).data.id as string;

    const item = await api.post(`/api/v1/payouts/${batchId}/items`, {
      data: {
        item_type: "EMPLOYEE_SALARY",
        payee_name: "E2E 직원",
        due_amount: 1_000_000,
      },
    });
    expect(item.ok()).toBeTruthy();
    const itemId = (await item.json()).data.items[0].id as string;

    // 승인 전에는 지급을 기록할 수 없다
    const early = await api.post(`/api/v1/payouts/items/${itemId}/payments`, {
      data: { payment_date: `${YM}-20`, amount: 100000 },
    });
    expect(early.ok()).toBeFalsy();

    // 지급 승인은 ADMIN 만 할 수 있다 — 회계 담당은 만들 수는 있어도 승인은 못 한다.
    const byAccountant = await api.post(`/api/v1/payouts/${batchId}/approve`, {
      data: {},
    });
    expect(byAccountant.status()).toBe(403);

    const admin = await loginAs(page, "approver");
    const approved = await admin.api.post(`/api/v1/payouts/${batchId}/approve`, {
      data: {},
    });
    expect(approved.ok(), await approved.text()).toBeTruthy();

    // 부분 지급
    const paid1 = await api.post(`/api/v1/payouts/items/${itemId}/payments`, {
      data: { payment_date: `${YM}-20`, amount: 600_000, payment_reference: "TRX-1" },
    });
    expect(paid1.ok()).toBeTruthy();

    const row = dbOne<{ paid_amount: number; status: string }>(
      `SELECT paid_amount, status FROM payout_item WHERE id = '${itemId}'`,
    );
    expect(Number(row!.paid_amount)).toBe(600_000);
    expect(row!.status).toBe("PARTIALLY_PAID");

    // 과지급은 거부된다
    const over = await api.post(`/api/v1/payouts/items/${itemId}/payments`, {
      data: { payment_date: `${YM}-21`, amount: 500_000 },
    });
    expect(over.ok()).toBeFalsy();

    // 잔액을 채우면 PAID
    const paid2 = await api.post(`/api/v1/payouts/items/${itemId}/payments`, {
      data: { payment_date: `${YM}-21`, amount: 400_000 },
    });
    expect(paid2.ok()).toBeTruthy();

    const final = dbOne<{
      paid_amount: number;
      status: string;
      accounting_effect: string;
    }>(
      `SELECT paid_amount, status, accounting_effect
         FROM payout_item WHERE id = '${itemId}'`,
    );
    expect(Number(final!.paid_amount)).toBe(1_000_000);
    expect(final!.status).toBe("PAID");
    // 급여는 이미 비용이다. 지급 장부가 비용을 다시 만들지 않는다.
    expect(final!.accounting_effect).toBe("ALREADY_EXPENSED");
  });

  test("개인카드 상환이 새 비용을 만들지 않는다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    // 개인카드 비용 1건을 심는다
    dbExec(`
      UPDATE expenses SET is_personal_card = TRUE, payment_method = 'PERSONAL_CARD'
       WHERE id = '${SEED.expenses.open}'
    `);

    const before = dbOne<{ n: number; total: number }>(
      `SELECT COUNT(*) AS n, COALESCE(SUM(gross_amount), 0) AS total
         FROM expenses WHERE org_id = '${SEED.orgId}' AND deleted_at IS NULL`,
    );

    const created = await api.post("/api/v1/payouts/reimbursements", {
      data: { expense_ids: [SEED.expenses.open], title: "E2E 개인카드 상환" },
    });
    expect(created.ok()).toBeTruthy();

    const after = dbOne<{ n: number; total: number }>(
      `SELECT COUNT(*) AS n, COALESCE(SUM(gross_amount), 0) AS total
         FROM expenses WHERE org_id = '${SEED.orgId}' AND deleted_at IS NULL`,
    );
    expect(Number(after!.n)).toBe(Number(before!.n));
    expect(Number(after!.total)).toBe(Number(before!.total));

    // 같은 비용을 두 번 상환할 수 없다
    const again = await api.post("/api/v1/payouts/reimbursements", {
      data: { expense_ids: [SEED.expenses.open], title: "중복 상환" },
    });
    expect(again.ok()).toBeFalsy();
  });
});
