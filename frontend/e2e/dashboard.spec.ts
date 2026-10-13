import { expect, test } from "@playwright/test";
import { SEED, dbOne, reseed } from "./db";
import { loginAs } from "./fixtures";

/**
 * 대시보드 E2E (실제 FastAPI + PostgreSQL).
 *
 * 왜 이 파일이 있나
 * --------------
 * 대시보드는 직전까지 `MOCK_PRODUCTS`/`MOCK_EXPENSES` 로 숫자를 만들었다.
 * 화면은 멀쩡해 보였고 tsc·lint·build·Vitest 가 전부 통과했다 —
 * **아무도 DB 를 보지 않았기 때문이다.**
 *
 * 그래서 이 테스트는 화면 문구가 아니라 **DB 실제 값과 대조**한다.
 * seed 판매 2건(2026-05, 쿠팡 30만 + 스마트스토어 10만)이 그대로 나와야 한다.
 */

const YM = SEED.months.dashboard;
const { coupang, smartstore } = SEED.sales;

const EXPECTED_GROSS = coupang.salePrice + smartstore.salePrice;
const EXPECTED_DIRECT =
  coupang.cost + coupang.marketplaceFee + smartstore.cost + smartstore.marketplaceFee;
const EXPECTED_NET = EXPECTED_GROSS - EXPECTED_DIRECT;

interface Kpi {
  key: string;
  label: string;
  value: number | null;
  source: string;
  availability: string;
  comparable: boolean;
}

interface DashboardBody {
  period: {
    year_month: string;
    close_state: string;
    source: string;
    profit_basis: string;
    profit_label: string;
    settlement_connected: boolean;
    is_confirmed: boolean;
  };
  comparison: { comparable: boolean; note: string | null };
  kpis: Kpi[];
  waterfall: { key: string; label: string; amount: number; kind: string }[];
  trend: { year_month: string; source: string; gross_sales: number }[];
  by_marketplace: { marketplace: string; gross_sales: number; contribution_profit: number }[];
  margin_distribution: { product_name: string; gross_sales: number }[];
  warnings: { open_total: number; never_scanned: boolean; by_type: unknown[] };
  confirmed_snapshots: { year_month: string; net_profit: number }[];
  available_months: string[];
}

test.beforeEach(() => {
  reseed();
});

test.describe("대시보드가 실제 DB 를 집계한다", () => {
  test("KPI 가 seed 판매와 정확히 일치한다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    const res = await api.get(`/api/v1/reports/dashboard?year_month=${YM}&trend_months=3`);
    expect(res.ok()).toBeTruthy();
    const data = (await res.json()).data as DashboardBody;

    const kpi = (key: string) => data.kpis.find((k) => k.key === key)!;

    expect(kpi("gross_sales").value).toBe(EXPECTED_GROSS);
    expect(kpi("contribution_profit").value).toBe(EXPECTED_GROSS - EXPECTED_DIRECT);
    expect(kpi("net_profit").value).toBe(EXPECTED_NET);
    expect(kpi("business_expenses").value).toBe(0);

    // DB 를 직접 읽어 같은 값이 나오는지 확인한다 — API 가 자기 말을 증명하게 두지 않는다.
    const row = dbOne<{ gross: string }>(
      `SELECT COALESCE(SUM(sale_price), 0) AS gross FROM sales_order
        WHERE org_id = '${SEED.orgId}' AND deleted_at IS NULL
          AND to_char(sale_date, 'YYYY-MM') = '${YM}'`,
    );
    expect(Number(row!.gross)).toBe(EXPECTED_GROSS);
  });

  test("진행 중인 달은 LIVE, 확정한 달은 snapshot 이다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    const before = (await (
      await api.get(`/api/v1/reports/dashboard?year_month=${YM}`)
    ).json()).data as DashboardBody;
    expect(before.period.source).toBe("LIVE");
    expect(before.period.is_confirmed).toBe(false);
    expect(before.period.profit_label).toContain("잠정");

    // 이 달을 확정한다.
    const v = await api.post(`/api/v1/monthly-close/${YM}/validate`);
    const items = (await v.json()).data as { code: string; grade: string }[];
    const confirmed = await api.post(`/api/v1/monthly-close/${YM}/confirm`, {
      data: {
        acknowledged_warnings: items.filter((i) => i.grade !== "BLOCKING").map((i) => i.code),
      },
    });
    expect(confirmed.ok()).toBeTruthy();

    const after = (await (
      await api.get(`/api/v1/reports/dashboard?year_month=${YM}`)
    ).json()).data as DashboardBody;
    expect(after.period.source).toBe("MONTH_CLOSE_SNAPSHOT");
    expect(after.period.is_confirmed).toBe(true);
    // 정산이 연결되지 않았으므로 그냥 "확정 순이익" 이라고 부르지 않는다.
    expect(after.period.profit_label).toContain("정산 미연결");
    expect(after.confirmed_snapshots.some((s) => s.year_month === YM)).toBeTruthy();
  });

  test("확정 후 판매가 바뀌어도 그 달 숫자는 움직이지 않는다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    const v = await api.post(`/api/v1/monthly-close/${YM}/validate`);
    const items = (await v.json()).data as { code: string; grade: string }[];
    await api.post(`/api/v1/monthly-close/${YM}/confirm`, {
      data: {
        acknowledged_warnings: items.filter((i) => i.grade !== "BLOCKING").map((i) => i.code),
      },
    });

    // 확정 뒤 판매 금액을 바꾼다(늦게 도착한 정정 등).
    const { dbExec } = await import("./db");
    dbExec(
      `UPDATE sales_order SET sale_price = 9999000 WHERE id = '${coupang.id}'`,
    );

    const after = (await (
      await api.get(`/api/v1/reports/dashboard?year_month=${YM}`)
    ).json()).data as DashboardBody;

    const gross = after.kpis.find((k) => k.key === "gross_sales")!;
    expect(gross.value).toBe(EXPECTED_GROSS);
    expect(gross.source).toBe("MONTH_CLOSE_SNAPSHOT");
  });

  test("판매처 분해 합계가 총매출과 어긋나지 않는다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    const data = (await (
      await api.get(`/api/v1/reports/dashboard?year_month=${YM}`)
    ).json()).data as DashboardBody;

    const sum = data.by_marketplace.reduce((a, m) => a + m.gross_sales, 0);
    expect(sum).toBe(EXPECTED_GROSS);
    expect(data.by_marketplace.map((m) => m.marketplace).sort()).toEqual(
      [coupang.marketplace, smartstore.marketplace].sort(),
    );
  });

  test("워터폴 차감 합이 순이익과 맞는다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    const data = (await (
      await api.get(`/api/v1/reports/dashboard?year_month=${YM}`)
    ).json()).data as DashboardBody;

    const start = data.waterfall.find((s) => s.kind === "start")!;
    const total = data.waterfall.find((s) => s.kind === "total")!;
    const deductions = data.waterfall
      .filter((s) => s.kind === "deduction")
      .reduce((a, s) => a + s.amount, 0);

    expect(start.amount - deductions).toBe(total.amount);
    expect(total.amount).toBe(EXPECTED_NET);
    // 예전 mock 은 매출의 3%를 "예상 세금" 으로 지어냈다. 그런 항목은 없다.
    expect(data.waterfall.map((s) => s.label)).not.toContain("예상 세금");
  });

  test("정산 미연결은 0원이 아니라 값 없음이다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    const data = (await (
      await api.get(`/api/v1/reports/dashboard?year_month=${YM}`)
    ).json()).data as DashboardBody;

    const settlement = data.kpis.find((k) => k.key === "settlement_amount")!;
    expect(settlement.value).toBeNull();
    expect(settlement.availability).toBe("NOT_CONNECTED");
    expect(data.period.settlement_connected).toBe(false);
  });
});

test.describe("대시보드 화면", () => {
  test("실제 금액이 화면에 렌더된다 — mock 숫자가 아니다", async ({ page }) => {
    await loginAs(page, "accountant");
    await page.goto("/dashboard");

    // 월 선택기가 seed 달을 제공한다.
    const monthSelect = page.getByLabel("기준 월");
    await expect(monthSelect).toBeVisible({ timeout: 20_000 });
    await monthSelect.selectOption(YM);

    // 총매출 40만원이 그대로 보인다.
    await expect(page.getByText("400,000 원").first()).toBeVisible({ timeout: 20_000 });

    // 진행 중인 달이라는 사실을 화면이 밝힌다.
    await expect(page.getByText("진행 중 집계").first()).toBeVisible();
    await expect(
      page.getByText("판매처 정산서가 연결되지 않아 정산 금액은 알 수 없습니다"),
    ).toBeVisible();
  });

  test("이슈 스캔 기록이 없으면 '0건' 이라고 단정하지 않는다", async ({ page }) => {
    await loginAs(page, "accountant");
    await page.goto("/dashboard");

    await expect(
      page.getByText("이슈 0건이 아니라 아직 확인한 적이 없다는 뜻입니다"),
    ).toBeVisible({ timeout: 20_000 });
  });

  test("READONLY 도 대시보드를 볼 수 있다", async ({ page }) => {
    const { api } = await loginAs(page, "readonly");
    const res = await api.get(`/api/v1/reports/dashboard?year_month=${YM}`);
    expect(res.status()).toBe(200);
  });
});
