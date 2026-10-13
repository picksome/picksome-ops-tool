import { expect, test } from "@playwright/test";
import { SEED, dbOne, dbQuery, reseed } from "./db";
import { apiAs, loginAs } from "./fixtures";

/**
 * 확정 수익배분 E2E (실제 FastAPI + PostgreSQL).
 *
 * 왜 이 파일이 있나
 * --------------
 * 배분 화면은 직전까지 `distributionAdapter`(메모리 배열)를 썼다. "저장했습니다"
 * 토스트는 떴지만 **DB 에는 아무 일도 일어나지 않았고**, 새로고침하면 사라졌다.
 *
 * 그래서 여기서 확인하는 것은 화면 문구가 아니라 **DB 행**이다.
 *   · 확정 배분이 `distribution_run` / `distribution_result` 로 남는가
 *   · 시뮬레이션(미리보기)이 **아무 행도 만들지 않는가**
 *   · 규칙을 바꿔도 이미 확정된 배분이 그대로인가
 */

const YM = SEED.months.dashboard;

/** seed 판매 2건 = 40만, 직접비 27.5만 → 순이익 12.5만 (사업비용 0). */
const EXPECTED_NET =
  SEED.sales.coupang.salePrice + SEED.sales.smartstore.salePrice
  - (SEED.sales.coupang.cost + SEED.sales.coupang.marketplaceFee
     + SEED.sales.smartstore.cost + SEED.sales.smartstore.marketplaceFee);

interface RunBody {
  id: string;
  status: string;
  distributable_base: number;
  total_distributed: number;
  snapshot_generation: number;
  results: Array<{
    partner_name_snapshot: string;
    distribution_kind: string;
    ratio_used: string | null;
    amount: number;
  }>;
}

async function confirmMonth(api: Awaited<ReturnType<typeof loginAs>>["api"]) {
  const v = await api.post(`/api/v1/monthly-close/${YM}/validate`);
  const items = (await v.json()).data as { code: string; grade: string }[];
  const res = await api.post(`/api/v1/monthly-close/${YM}/confirm`, {
    data: {
      acknowledged_warnings: items.filter((i) => i.grade !== "BLOCKING").map((i) => i.code),
    },
  });
  expect(res.ok(), await res.text()).toBeTruthy();
}

function runRows() {
  return dbQuery<{ id: string; status: string; total_distributed: string }>(
    `SELECT id::text, status, total_distributed FROM distribution_run
      WHERE org_id = '${SEED.orgId}' AND year_month = '${YM}'`,
  );
}

test.beforeEach(() => {
  reseed();
});

test.describe("확정 배분", () => {
  test("월을 확정하지 않으면 배분을 만들 수 없다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    const res = await api.post(`/api/v1/distributions/${YM}`, { data: {} });
    expect(res.ok()).toBeFalsy();
    // 근거 없는 배분이 DB 에 남지 않는다.
    expect(runRows()).toHaveLength(0);
  });

  test("확정 후 배분을 만들면 DB 에 실제 행이 남는다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    await confirmMonth(api);

    const created = await api.post(`/api/v1/distributions/${YM}`, { data: {} });
    expect(created.ok(), await created.text()).toBeTruthy();
    const run = (await created.json()).data as RunBody;

    expect(run.distributable_base).toBe(EXPECTED_NET);
    expect(run.snapshot_generation).toBe(1);
    // seed 규칙은 파트너 1명에게 100% 다.
    expect(run.results).toHaveLength(1);
    expect(run.results[0].amount).toBe(EXPECTED_NET);
    expect(run.results[0].distribution_kind).toBe("RATIO");

    // 화면 응답이 아니라 DB 로 확인한다.
    const rows = runRows();
    expect(rows).toHaveLength(1);
    expect(Number(rows[0].total_distributed)).toBe(EXPECTED_NET);

    const results = dbQuery<{ amount: string; partner_name_snapshot: string }>(
      `SELECT amount, partner_name_snapshot FROM distribution_result
        WHERE run_id = '${rows[0].id}'`,
    );
    expect(results).toHaveLength(1);
    expect(Number(results[0].amount)).toBe(EXPECTED_NET);
  });

  test("같은 달에 배분을 두 번 만들 수 없다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    await confirmMonth(api);

    expect((await api.post(`/api/v1/distributions/${YM}`, { data: {} })).ok()).toBeTruthy();
    const second = await api.post(`/api/v1/distributions/${YM}`, { data: {} });
    expect(second.status()).toBe(409);
    expect(runRows()).toHaveLength(1);
  });

  test("승인하지 않은 배분으로는 지급 배치를 만들 수 없다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    await confirmMonth(api);
    await api.post(`/api/v1/distributions/${YM}`, { data: {} });

    const batch = await api.post(`/api/v1/payouts/from-distribution/${YM}`, {
      data: { due_date: null },
    });
    expect(batch.ok()).toBeFalsy();

    const batches = dbQuery(
      `SELECT id FROM payout_batch WHERE org_id = '${SEED.orgId}'`,
    );
    expect(batches).toHaveLength(0);
  });

  test("승인 → 지급 배치 → 지급 장부 연결", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    await confirmMonth(api);
    const run = (await (
      await api.post(`/api/v1/distributions/${YM}`, { data: {} })
    ).json()).data as RunBody;

    const approved = await api.post(
      `/api/v1/distributions/${YM}/approve?run_id=${run.id}`, { data: {} },
    );
    expect(approved.ok(), await approved.text()).toBeTruthy();

    const batch = await api.post(`/api/v1/payouts/from-distribution/${YM}`, {
      data: { due_date: null },
    });
    expect(batch.ok(), await batch.text()).toBeTruthy();

    const row = dbOne<{ source_distribution_run_id: string; status: string }>(
      `SELECT source_distribution_run_id::text, status FROM payout_batch
        WHERE org_id = '${SEED.orgId}'`,
    );
    expect(row!.source_distribution_run_id).toBe(run.id);

    const items = dbQuery<{ due_amount: string; paid_amount: string; status: string }>(
      `SELECT due_amount, paid_amount, status FROM payout_item
        WHERE org_id = '${SEED.orgId}'`,
    );
    expect(items).toHaveLength(1);
    expect(Number(items[0].due_amount)).toBe(EXPECTED_NET);
    // 지급 기록이 없으므로 아직 지급된 것이 아니다.
    expect(Number(items[0].paid_amount)).toBe(0);
  });
});

test.describe("시뮬레이션은 아무것도 만들지 않는다", () => {
  test("미리보기를 여러 번 눌러도 배분·지급·마감이 생기지 않는다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    for (let i = 0; i < 3; i += 1) {
      const res = await api.get(`/api/v1/distributions/${YM}/preview`);
      expect(res.ok()).toBeTruthy();
      const body = (await res.json()).data as { is_provisional: boolean };
      // 확정 전이므로 "확정 배분액" 이 아니다.
      expect(body.is_provisional).toBe(true);
    }

    expect(runRows()).toHaveLength(0);
    expect(dbQuery(`SELECT id FROM payout_batch WHERE org_id = '${SEED.orgId}'`)).toHaveLength(0);
    expect(
      dbQuery(
        `SELECT id FROM monthly_close_snapshot WHERE org_id = '${SEED.orgId}'
          AND year_month = '${YM}'`,
      ),
    ).toHaveLength(0);

    const ms = dbOne<{ close_state: string }>(
      `SELECT close_state FROM monthly_settlement
        WHERE org_id = '${SEED.orgId}' AND year_month = '${YM}'`,
    );
    expect(ms!.close_state).toBe("OPEN");
  });
});

/**
 * 정책 쓰기 권한은 **ADMIN / OPERATOR** 다 (`api/v1/policies.py`).
 *
 * 배분을 확정하는 권한(ADMIN/ACCOUNTING)과 다르다 — 정산 담당자는 확정된 규칙으로
 * 배분을 돌릴 수 있지만 규칙 자체를 바꿀 수는 없다. 업무 분리다.
 */
function partnerId(): string {
  return dbOne<{ id: string }>(
    `SELECT id::text FROM partner WHERE org_id = '${SEED.orgId}' LIMIT 1`,
  )!.id;
}

test.describe("배분 규칙", () => {
  test("정산 담당자는 배분 규칙을 바꿀 수 없다 — 업무 분리", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    const res = await api.post("/api/v1/policies/partner_share", {
      data: {
        version: 5,
        effective_from: "2026-01-01",
        fields: { partner_id: partnerId(), share_ratio: 0.5, applies_year_month: null },
      },
    });
    expect(res.status()).toBe(403);
  });

  test("새 규칙을 승인해도 이미 확정된 배분은 그대로다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    await confirmMonth(api);
    const run = (await (
      await api.post(`/api/v1/distributions/${YM}`, { data: {} })
    ).json()).data as RunBody;
    const before = run.results[0].amount;

    // 규칙은 ADMIN 이 바꾼다. 기존 정책은 두고 새 버전을 넣는다.
    const admin = await apiAs("approver");
    const created = await admin.post("/api/v1/policies/partner_share", {
      data: {
        version: 2,
        effective_from: "2026-01-01",
        fields: { partner_id: partnerId(), share_ratio: 0.5, applies_year_month: null },
      },
    });
    expect(created.ok(), await created.text()).toBeTruthy();
    const id = (await created.json()).data.id;
    expect((await admin.post(`/api/v1/policies/partner_share/${id}/approve`, {
      data: { note: "E2E 승인" },
    })).ok()).toBeTruthy();

    const after = (await (
      await api.get(`/api/v1/distributions/${YM}`)
    ).json()).data as RunBody;
    expect(after.results[0].amount).toBe(before);
  });

  /**
   * ⚠️ 실제 동작을 고정한다 (D8).
   *
   * `services/policy_crud.py` 의 주석은 오랫동안 "생성 = DRAFT(비활성), 승인해야 활성"
   * 이라고 적혀 있었지만 **구현은 정반대였다** — `create_policy` 가 `is_active=True` 로
   * 넣고, `approve_policy` 는 `reliability_status` 만 바꾼다.
   *
   * 그 주석을 믿고 화면에 "승인해야 적용됩니다" 라고 안내하면 거짓말이 된다.
   * 저장하는 순간 계산이 바뀌고, 비율 합이 100% 가 아니면 그 즉시 월 마감이 막힌다.
   *
   * 승인 게이트가 필요한지는 별도 결정 사항이다. 여기서는 **지금 무엇이 사실인지**를
   * 고정해서, 동작이 바뀌면 이 테스트가 먼저 깨지게 한다.
   */
  test("규칙은 저장하는 순간 활성이다 — 승인 게이트가 없다", async ({ page }) => {
    await loginAs(page, "approver");
    const admin = await apiAs("approver");

    const created = await admin.post("/api/v1/policies/partner_share", {
      data: {
        version: 9,
        effective_from: "2026-01-01",
        fields: { partner_id: partnerId(), share_ratio: 0.25, applies_year_month: null },
      },
    });
    expect(created.ok(), await created.text()).toBeTruthy();
    const id = (await created.json()).data.id;

    const fresh = dbOne<{ is_active: boolean; approved_by: string | null }>(
      `SELECT is_active, approved_by::text FROM partner_share_policy WHERE id = '${id}'`,
    );
    expect(fresh!.is_active).toBe(true);
    expect(fresh!.approved_by).toBeNull();

    // approve 는 활성 여부가 아니라 "사람이 확인했다" 를 기록한다.
    expect((await admin.post(`/api/v1/policies/partner_share/${id}/approve`, {
      data: { note: "E2E 확인" },
    })).ok()).toBeTruthy();

    const after = dbOne<{ is_active: boolean; reliability_status: string; approved_by: string | null }>(
      `SELECT is_active, reliability_status, approved_by::text
         FROM partner_share_policy WHERE id = '${id}'`,
    );
    expect(after!.is_active).toBe(true);
    expect(after!.reliability_status).toBe("CONFIRMED");
    expect(after!.approved_by).not.toBeNull();
  });

  test("비율 합이 100%가 아니면 그 즉시 월 마감이 막힌다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    const admin = await apiAs("approver");

    // seed 는 이미 100% 다. 여기에 25% 를 더하면 125% 가 된다.
    expect((await admin.post("/api/v1/policies/partner_share", {
      data: {
        version: 3,
        effective_from: "2026-01-01",
        fields: { partner_id: partnerId(), share_ratio: 0.25, applies_year_month: null },
      },
    })).ok()).toBeTruthy();

    const v = await api.post(`/api/v1/monthly-close/${YM}/validate`);
    const items = (await v.json()).data as { code: string; grade: string }[];
    const ratio = items.find((i) => i.code === "DISTRIBUTION_RATIO_INVALID");
    expect(ratio, "비율 오류가 검증에 나와야 한다").toBeTruthy();
    expect(ratio!.grade).toBe("BLOCKING");

    // 확인 처리로 넘어갈 수 없다 — BLOCKING 은 확정을 거부한다.
    const confirmed = await api.post(`/api/v1/monthly-close/${YM}/confirm`, {
      data: { acknowledged_warnings: items.map((i) => i.code) },
    });
    expect(confirmed.ok()).toBeFalsy();
  });
});

test.describe("배분 화면", () => {
  test("확정 배분이 없으면 지어내지 않고 없다고 말한다", async ({ page }) => {
    await loginAs(page, "accountant");
    await page.goto(`/settlements/distribution?from=${YM}`);

    await expect(
      page.getByText(`의 확정 배분이 없습니다`, { exact: false }),
    ).toBeVisible({ timeout: 20_000 });
  });

  test("규칙 탭이 지원하지 않는 개념을 명시한다", async ({ page }) => {
    await loginAs(page, "accountant");
    await page.goto("/settlements/distribution?tab=rules");

    await expect(page.getByText("지원하지 않는 배분 개념")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText("최대 지급 한도(cap) — 미지원")).toBeVisible();
  });

  test("OPERATOR 는 배분을 볼 수 있지만 지급 장부는 못 본다", async ({ page }) => {
    const { api } = await loginAs(page, "operator");

    expect((await api.get(`/api/v1/distributions/${YM}`)).status()).toBe(200);
    // 누가 얼마를 가져가는지는 운영 업무에 필요하지 않다.
    expect((await api.get("/api/v1/payouts")).status()).toBe(403);
  });
});
