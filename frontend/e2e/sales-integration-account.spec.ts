import { expect, test } from "@playwright/test";
import { SEED, dbOne, dbQuery, reseed } from "./db";
import { apiAs, loginAs } from "./fixtures";

/**
 * 주문의 판매자 계정 귀속 E2E (M032, Phase 5).
 *
 * 왜 이 파일이 있나
 * --------------
 * `sales_order` 가 판매처를 아는 유일한 방법이 `marketplace` 문자열이었다.
 * 쿠팡 본점과 쿠팡 2호점은 둘 다 `COUPANG` 이라 **스토어별 매출을 나눌 수 없었다.**
 *
 * seed 는 의도적으로 두 가지 상태를 함께 심는다.
 *   · 쿠팡 주문 → 본점 계정에 귀속
 *   · 스마트스토어 주문 → 계정 없음 (legacy NULL)
 *
 * 두 번째가 중요하다 — 계정이 없는 주문이 조회나 집계에서 **조용히 빠지지 않는지**
 * 확인할 대상이 필요하다.
 */

const YM = SEED.months.dashboard;
const { coupang, smartstore } = SEED.sales;
const EXPECTED_GROSS = coupang.salePrice + smartstore.salePrice;

interface OrderRow {
  id: string;
  marketplace: string;
  marketplace_order_no: string;
  integration_account_id: string | null;
  integration_account_label: string | null;
}

test.beforeEach(() => {
  reseed();
});

test.describe("주문 조회에 계정이 실려 나온다", () => {
  test("계정이 있는 주문과 없는 주문이 함께 보인다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    const res = await api.get("/api/v1/sales?page=1&page_size=50");
    expect(res.ok()).toBeTruthy();
    const items = (await res.json()).data as OrderRow[];

    const cou = items.find((o) => o.marketplace_order_no === "E2E-ORD-COUPANG")!;
    const sma = items.find((o) => o.marketplace_order_no === "E2E-ORD-SMARTSTORE")!;

    expect(cou.integration_account_id).toBe(coupang.integrationAccountId);
    expect(cou.integration_account_label).toBe("E2E 본점");

    // legacy NULL — 목록에서 빠지지 않는다.
    expect(sma.integration_account_id).toBeNull();
    expect(sma.integration_account_label).toBeNull();
    // 계정 라벨 자리에 채널 이름이 새어 들어오지 않는다.
    expect(sma.integration_account_label).not.toBe(sma.marketplace);
  });

  test("계정 필터가 스토어별로 좁힌다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    const filtered = await api.get(
      `/api/v1/sales?integration_account_id=${coupang.integrationAccountId}&page=1&page_size=50`,
    );
    const items = (await filtered.json()).data as OrderRow[];

    expect(items).toHaveLength(1);
    expect(items[0].marketplace_order_no).toBe("E2E-ORD-COUPANG");

    // 아무 주문도 귀속되지 않은 2호점은 0건이다 — 채널이 같다고 섞이지 않는다.
    const second = await api.get(
      `/api/v1/sales?integration_account_id=${SEED.accounts.second}&page=1&page_size=50`,
    );
    expect((await second.json()).data).toHaveLength(0);
  });
});

test.describe("주문 생성", () => {
  test("계정을 지정하면 그 계정에 귀속된다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    const created = await api.post("/api/v1/sales", {
      data: {
        sale_date: `${YM}-20`,
        marketplace: "COUPANG",
        marketplace_order_no: "E2E-ORD-SECOND",
        product_snapshot_name: "2호점 주문",
        quantity: 1,
        sale_price: 55_000,
        integration_account_id: SEED.accounts.second,
      },
    });
    expect(created.ok(), await created.text()).toBeTruthy();

    const row = dbOne<{ integration_account_id: string }>(
      `SELECT integration_account_id::text FROM sales_order
        WHERE org_id = '${SEED.orgId}' AND marketplace_order_no = 'E2E-ORD-SECOND'`,
    );
    expect(row!.integration_account_id).toBe(SEED.accounts.second);
  });

  test("같은 판매처의 두 계정 주문이 섞이지 않는다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    await api.post("/api/v1/sales", {
      data: {
        sale_date: `${YM}-21`, marketplace: "COUPANG",
        marketplace_order_no: "E2E-ORD-MIX", product_snapshot_name: "2호점",
        quantity: 1, sale_price: 70_000,
        integration_account_id: SEED.accounts.second,
      },
    });

    const main = dbQuery(
      `SELECT id FROM sales_order WHERE org_id = '${SEED.orgId}'
         AND integration_account_id = '${SEED.accounts.main}'`,
    );
    const second = dbQuery(
      `SELECT id FROM sales_order WHERE org_id = '${SEED.orgId}'
         AND integration_account_id = '${SEED.accounts.second}'`,
    );
    expect(main).toHaveLength(1);
    expect(second).toHaveLength(1);
  });

  test("존재하지 않는 계정은 거부한다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    const res = await api.post("/api/v1/sales", {
      data: {
        sale_date: `${YM}-22`, marketplace: "COUPANG",
        marketplace_order_no: "E2E-ORD-BAD", product_snapshot_name: "잘못된 계정",
        quantity: 1, sale_price: 10_000,
        integration_account_id: "00000000-0000-0000-0000-0000000000ff",
      },
    });
    expect(res.ok()).toBeFalsy();

    expect(
      dbQuery(`SELECT id FROM sales_order WHERE org_id = '${SEED.orgId}'
                 AND marketplace_order_no = 'E2E-ORD-BAD'`),
    ).toHaveLength(0);
  });

  test("계정 없이도 주문을 만들 수 있다 — 선택값이다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    const res = await api.post("/api/v1/sales", {
      data: {
        sale_date: `${YM}-23`, marketplace: "COUPANG",
        marketplace_order_no: "E2E-ORD-NOACC", product_snapshot_name: "계정 미지정",
        quantity: 1, sale_price: 20_000,
      },
    });
    expect(res.ok(), await res.text()).toBeTruthy();

    const row = dbOne<{ integration_account_id: string | null }>(
      `SELECT integration_account_id::text FROM sales_order
        WHERE org_id = '${SEED.orgId}' AND marketplace_order_no = 'E2E-ORD-NOACC'`,
    );
    expect(row!.integration_account_id).toBeNull();
  });
});

test.describe("집계 회귀 — 계정 컬럼이 숫자를 바꾸지 않는다", () => {
  test("대시보드 총매출에 legacy NULL 주문이 그대로 포함된다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    const data = (await (
      await api.get(`/api/v1/reports/dashboard?year_month=${YM}`)
    ).json()).data as {
      kpis: Array<{ key: string; value: number | null }>;
      by_marketplace: Array<{ marketplace: string; gross_sales: number }>;
    };

    const gross = data.kpis.find((k) => k.key === "gross_sales")!.value;
    // 40만원 = 쿠팡(계정 있음) 30만 + 스마트스토어(계정 없음) 10만.
    // 계정 없는 주문이 빠지면 30만이 된다.
    expect(gross).toBe(EXPECTED_GROSS);
    expect(data.by_marketplace.map((m) => m.marketplace).sort()).toEqual(
      ["COUPANG", "SMARTSTORE"],
    );
  });

  test("월 마감 확정 숫자도 그대로다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    const v = await api.post(`/api/v1/monthly-close/${YM}/validate`);
    const items = (await v.json()).data as { code: string; grade: string }[];
    const confirmed = await api.post(`/api/v1/monthly-close/${YM}/confirm`, {
      data: {
        acknowledged_warnings: items.filter((i) => i.grade !== "BLOCKING").map((i) => i.code),
      },
    });
    expect(confirmed.ok(), await confirmed.text()).toBeTruthy();

    const snap = dbOne<{ gross_sales: string }>(
      `SELECT gross_sales FROM monthly_close_snapshot
        WHERE org_id = '${SEED.orgId}' AND year_month = '${YM}' AND superseded_at IS NULL`,
    );
    expect(Number(snap!.gross_sales)).toBe(EXPECTED_GROSS);
  });
});

test.describe("계정 수명주기가 과거 주문을 깨뜨리지 않는다", () => {
  test("계정을 비활성화해도 과거 주문이 조회된다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    const { dbExec } = await import("./db");

    dbExec(
      `UPDATE integration_account SET status = 'INACTIVE'
        WHERE id = '${coupang.integrationAccountId}'`,
    );

    const res = await api.get(
      `/api/v1/sales?integration_account_id=${coupang.integrationAccountId}&page=1&page_size=50`,
    );
    const items = (await res.json()).data as OrderRow[];
    expect(items).toHaveLength(1);
    expect(items[0].integration_account_label).toBe("E2E 본점");
  });

  test("계정을 참조하는 주문이 있으면 하드 삭제가 거부된다", async ({ page }) => {
    await loginAs(page, "approver");
    const { dbExec } = await import("./db");

    // FK 는 ON DELETE NO ACTION 이다 — 매출 기록이 계정 정리에 딸려 사라지면 안 된다.
    let rejected = false;
    try {
      dbExec(`DELETE FROM integration_account WHERE id = '${coupang.integrationAccountId}'`);
    } catch {
      rejected = true;
    }
    expect(rejected, "참조된 계정 삭제가 FK 로 막히지 않았다").toBeTruthy();

    expect(
      dbQuery(`SELECT id FROM sales_order WHERE id = '${coupang.id}'`),
    ).toHaveLength(1);
  });
});

test.describe("봇 ingest 는 실행 계정에 귀속시킨다", () => {
  test("페이로드가 주장하는 계정이 아니라 키의 계정으로 들어간다", async ({ page }) => {
    await loginAs(page, "approver");
    const admin = await apiAs("approver");

    // 새 계정을 발급받아 그 키로 ingest 한다.
    const created = await admin.post("/api/v1/integrations", {
      data: {
        provider: "COUPANG",
        account_label: "E2E ingest 계정",
        sale_channel_code: "COUPANG",
      },
    });
    expect(created.ok(), await created.text()).toBeTruthy();
    const account = (await created.json()).data as { id: string; api_key: string };

    const res = await admin.post(`/api/v1/integrations/${account.id}/ingest/orders`, {
      headers: { "X-Integration-Key": account.api_key },
      data: {
        items: [{
          sale_date: `${YM}-25`,
          marketplace: "COUPANG",
          marketplace_order_no: "E2E-ORD-BOT",
          quantity: 1,
          sale_price: 33_000,
          product_snapshot_name: "봇 주문",
          // 봇이 다른 계정이라고 주장한다 — 무시돼야 한다.
          integration_account_id: SEED.accounts.second,
        }],
      },
    });
    expect(res.ok(), await res.text()).toBeTruthy();

    const row = dbOne<{ integration_account_id: string }>(
      `SELECT integration_account_id::text FROM sales_order
        WHERE org_id = '${SEED.orgId}' AND marketplace_order_no = 'E2E-ORD-BOT'`,
    );
    expect(row!.integration_account_id).toBe(account.id);
    expect(row!.integration_account_id).not.toBe(SEED.accounts.second);
  });
});

test.describe("판매 화면", () => {
  test("계정이 없는 주문은 '연동계정 미확인' 으로 표시한다", async ({ page }) => {
    await loginAs(page, "accountant");
    await page.goto("/sales");

    // 스마트스토어 주문(계정 없음)을 연다.
    await page.getByText("E2E-ORD-SMARTSTORE").first().click();
    await expect(page.getByText("연동계정 미확인")).toBeVisible({ timeout: 20_000 });
  });

  test("계정이 있는 주문은 계정 이름을 보여준다", async ({ page }) => {
    await loginAs(page, "accountant");
    await page.goto("/sales");

    await page.getByText("E2E-ORD-COUPANG").first().click();
    await expect(page.getByText("E2E 본점")).toBeVisible({ timeout: 20_000 });
  });
});
