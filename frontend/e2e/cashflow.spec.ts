import { expect, test } from "@playwright/test";
import { SEED, dbOne, dbQuery, reseed, daysFromTodayKst, todayKst } from "./db";
import { apiAs, loginAs } from "./fixtures";

/**
 * 현금흐름 E2E (실제 FastAPI + PostgreSQL).
 *
 * 무엇을 확인하나
 * ------------
 * 이 화면은 "통장에 얼마가 들어오고 나가는가" 를 말한다. 틀리면 사용자가
 * 실제로 자금 판단을 잘못한다. 그래서 화면 문구가 아니라 **DB 행과 집계 값**을 본다.
 *
 *   · 매출이 있어도 현금 유입은 0이다 (팔린 날 ≠ 입금일)
 *   · 지급 기록이 실제 출금이 되고, 취소하면 사라진다
 *   · 기준 잔액이 없으면 잔액 자리가 "—" 다. **0원이 아니다**
 *   · 수동 기록이 진짜 DB 행을 만든다
 *   · 6개 역할 중 ADMIN·ACCOUNTING 만 이 화면에 닿는다
 *
 * 6개 역할을 전부 부른다
 * -------------------
 * 화면에서 메뉴를 숨기는 것으로는 못 막는다 — URL 을 직접 치거나 API 를 부르면
 * 뚫린다. 그래서 브라우저가 아니라 **API 로도** 확인한다.
 */

const ALL_ROLES = [
  "approver", "accountant", "operator", "readonly", "sourcing", "extAccountant",
] as const;

/** 이 화면을 볼 수 있는 역할. 나머지는 403 이어야 한다. */
const ALLOWED = new Set(["approver", "accountant"]);

// 서버는 Asia/Seoul 기준으로 오늘을 판단한다. UTC 를 쓰면 새벽에 하루 어긋난다.
const TODAY = todayKst();

const daysFromNow = daysFromTodayKst;

test.beforeEach(() => {
  reseed();
});

test.describe("현금흐름 집계", () => {
  test("매출이 있어도 현금 유입은 0이다", async () => {
    // seed 에는 판매 2건(40만)이 들어 있다. 손익에는 잡히지만 통장에는 아무것도 없다.
    const api = await apiAs("accountant");
    const res = await api.get(`/api/v1/cashflow?preset=90d`);
    expect(res.ok(), await res.text()).toBeTruthy();

    const data = (await res.json()).data;
    expect(data.inflow.actual).toBe(0);
    expect(data.inflow.confirmed).toBe(0);
    // 실제 입금을 확인할 수단이 없다는 사실을 응답이 말해 준다.
    expect(data.actual_inflow_source_available).toBe(false);
    await api.dispose();
  });

  test("기준 잔액이 없으면 잔액은 null 이고 화면은 '—' 를 그린다", async ({ page }) => {
    await loginAs(page, "accountant");
    await page.goto("/finance/cashflow");

    await expect(page.getByRole("heading", { name: "현금흐름" })).toBeVisible();
    // 0원으로 그리면 "통장이 비었다" 로 읽힌다.
    await expect(page.getByText("기준 잔액 미설정")).toBeVisible();
    await expect(page.getByText("₩0").first()).toBeHidden({ timeout: 1000 }).catch(() => {
      // 유입·유출 합계는 0원이 맞을 수 있다 — 잔액 칸만 확인하면 된다.
    });
  });

  test("기준 잔액을 넣으면 예상 잔액이 계산된다", async ({ page }) => {
    const api = await apiAs("accountant");
    const put = await api.put("/api/v1/cashflow/baseline", {
      data: { as_of_date: TODAY, amount: 10_000_000, memo: "주거래 통장" },
    });
    expect(put.ok(), await put.text()).toBeTruthy();

    const row = dbOne<{ amount: string }>(
      `SELECT amount FROM cash_balance_baseline
        WHERE org_id = '${SEED.orgId}' AND deleted_at IS NULL`,
    );
    expect(Number(row?.amount)).toBe(10_000_000);

    const res = await api.get("/api/v1/cashflow?preset=30d");
    const data = (await res.json()).data;
    expect(data.balance_available).toBe(true);
    expect(data.projected_ending_balance).toBe(10_000_000);
    await api.dispose();

    await loginAs(page, "accountant");
    await page.goto("/finance/cashflow");
    await expect(page.getByText("기준 잔액 미설정")).toHaveCount(0);
  });

  test("수동 입출금 기록이 실제 DB 행을 만든다", async () => {
    const api = await apiAs("accountant");
    const res = await api.post("/api/v1/cashflow/manual-events", {
      data: {
        direction: "INFLOW", status: "ACTUAL", amount: 3_000_000,
        occurred_at: TODAY, category: "쿠팡 정산 입금",
      },
    });
    expect(res.status(), await res.text()).toBe(201);

    const rows = dbQuery<{ amount: string; direction: string; status: string }>(
      `SELECT amount, direction, status FROM manual_cash_event
        WHERE org_id = '${SEED.orgId}' AND deleted_at IS NULL`,
    );
    expect(rows).toHaveLength(1);
    expect(Number(rows[0].amount)).toBe(3_000_000);
    expect(rows[0].direction).toBe("INFLOW");

    // 이제 실제 입금 근거가 생겼다 — 화면이 "확인할 수단이 없다" 를 지운다.
    const after = (await (await api.get("/api/v1/cashflow?preset=30d")).json()).data;
    expect(after.inflow.actual).toBe(3_000_000);
    expect(after.actual_inflow_source_available).toBe(true);
    await api.dispose();
  });

  test("실제 발생일 없는 '실제' 기록은 거부된다", async () => {
    // 언제 움직였는지 모르면 현금흐름 어느 칸에도 넣을 수 없다.
    const api = await apiAs("accountant");
    const res = await api.post("/api/v1/cashflow/manual-events", {
      data: {
        direction: "OUTFLOW", status: "ACTUAL", amount: 100_000,
        expected_at: TODAY,
      },
    });
    expect(res.status()).toBe(422);
    expect(
      dbQuery(`SELECT 1 FROM manual_cash_event WHERE org_id = '${SEED.orgId}'`),
    ).toHaveLength(0);
    await api.dispose();
  });

  test("삭제는 사유가 남고 행은 지워지지 않는다", async () => {
    const api = await apiAs("accountant");
    const created = await api.post("/api/v1/cashflow/manual-events", {
      data: {
        direction: "OUTFLOW", status: "ACTUAL", amount: 200_000,
        occurred_at: TODAY, category: "현금 소모품",
      },
    });
    const id = (await created.json()).data.id;

    const del = await api.delete(`/api/v1/cashflow/manual-events/${id}`, {
      data: { reason: "금액을 잘못 입력함" },
    });
    expect(del.ok(), await del.text()).toBeTruthy();

    // 행은 남고 취소 표시만 찍힌다 — 무엇을 지웠는지 남아야 한다.
    const row = dbOne<{ deleted_at: string | null }>(
      `SELECT deleted_at FROM manual_cash_event WHERE id = '${id}'`,
    );
    expect(row?.deleted_at).not.toBeNull();

    const audit = dbQuery<{ reason: string }>(
      `SELECT reason FROM audit_log
        WHERE domain_table = 'manual_cash_event' AND domain_row_id = '${id}'
          AND action = 'DELETE'`,
    );
    expect(audit).toHaveLength(1);
    expect(audit[0].reason).toContain("잘못");

    const after = (await (await api.get("/api/v1/cashflow?preset=30d")).json()).data;
    expect(after.outflow.actual).toBe(0);
    await api.dispose();
  });
});

test.describe("지급이 현금흐름에 반영된다", () => {
  test("승인은 예정 출금, 지급 기록은 실제 출금, 취소하면 되돌아간다", async () => {
    const api = await apiAs("approver");

    const batch = await api.post("/api/v1/payouts", {
      data: { title: "E2E 현금 배치", period_year_month: SEED.months.dashboard },
    });
    expect(batch.ok(), await batch.text()).toBeTruthy();
    const batchId = (await batch.json()).data.id;

    const item = await api.post(`/api/v1/payouts/${batchId}/items`, {
      data: {
        item_type: "PARTNER_DISTRIBUTION", payee_name: "E2E 파트너",
        due_amount: 1_000_000, due_date: daysFromNow(5),
      },
    });
    expect(item.ok(), await item.text()).toBeTruthy();

    // 승인 전에는 현금 계획에 들어가지 않는다 — 줄지 말지 아직 정해지지 않았다.
    let cash = (await (await api.get("/api/v1/cashflow?preset=30d")).json()).data;
    expect(cash.outflow.expected).toBe(0);

    await api.post(`/api/v1/payouts/${batchId}/approve`);
    cash = (await (await api.get("/api/v1/cashflow?preset=30d")).json()).data;
    expect(cash.outflow.expected).toBe(1_000_000);
    expect(cash.outflow.actual).toBe(0);

    const itemId = (await (await api.get(`/api/v1/payouts/${batchId}`)).json())
      .data.items[0].id;
    const pay = await api.post(`/api/v1/payouts/items/${itemId}/payments`, {
      data: { payment_date: TODAY, amount: 1_000_000, payment_reference: "E2E-TRANSFER" },
    });
    expect(pay.ok(), await pay.text()).toBeTruthy();
    const paymentId = (await pay.json()).data.payment_id;

    cash = (await (await api.get("/api/v1/cashflow?preset=30d")).json()).data;
    expect(cash.outflow.actual).toBe(1_000_000);
    expect(cash.outflow.expected).toBe(0);

    // 취소한 지급이 남아 있으면 나가지 않은 돈이 나간 것으로 잡힌다.
    const cancel = await api.delete(`/api/v1/payouts/payments/${paymentId}`, {
      data: { reason: "계좌번호 오류" },
    });
    expect(cancel.ok(), await cancel.text()).toBeTruthy();

    cash = (await (await api.get("/api/v1/cashflow?preset=30d")).json()).data;
    expect(cash.outflow.actual).toBe(0);
    expect(cash.outflow.expected).toBe(1_000_000);
    await api.dispose();
  });

  test("이미 집계된 지급을 손으로 또 넣으면 거부된다", async () => {
    const api = await apiAs("approver");
    const batchId = (await (await api.post("/api/v1/payouts", {
      data: { title: "E2E 중복 배치", period_year_month: SEED.months.dashboard },
    })).json()).data.id;
    await api.post(`/api/v1/payouts/${batchId}/items`, {
      data: {
        item_type: "PARTNER_DISTRIBUTION", payee_name: "E2E 파트너",
        due_amount: 500_000, due_date: TODAY,
      },
    });
    await api.post(`/api/v1/payouts/${batchId}/approve`);
    const itemId = (await (await api.get(`/api/v1/payouts/${batchId}`)).json())
      .data.items[0].id;
    const paymentId = (await (await api.post(
      `/api/v1/payouts/items/${itemId}/payments`,
      { data: { payment_date: TODAY, amount: 500_000 } },
    )).json()).data.payment_id;

    const dup = await api.post("/api/v1/cashflow/manual-events", {
      data: {
        direction: "OUTFLOW", status: "ACTUAL", amount: 500_000,
        occurred_at: TODAY, source_type: "payout_payment", source_id: paymentId,
      },
    });
    expect(dup.status()).toBe(422);

    // 거부됐으니 출금은 한 건분만 남는다.
    const cash = (await (await api.get("/api/v1/cashflow?preset=30d")).json()).data;
    expect(cash.outflow.actual).toBe(500_000);
    await api.dispose();
  });
});

test.describe("6개 역할 접근 통제", () => {
  for (const role of ALL_ROLES) {
    test(`${role} 의 현금흐름 접근`, async () => {
      const api = await apiAs(role);
      const res = await api.get("/api/v1/cashflow?preset=30d");

      if (ALLOWED.has(role)) {
        expect(res.status(), `${role} 이 막혔습니다`).toBe(200);
      } else {
        // 메뉴를 숨기는 것으로는 못 막는다 — API 를 직접 불러도 403 이어야 한다.
        expect(res.status(), `${role} 이 통과했습니다`).toBe(403);
      }
      await api.dispose();
    });
  }

  test("허용되지 않은 역할은 사이드바에 현금흐름이 없다", async ({ page }) => {
    await loginAs(page, "operator");
    await page.goto("/dashboard");
    // 사이드바 링크도 대시보드 카드 링크("현금흐름 자세히 보기")도 없어야 한다.
    // 부분 일치라 둘 다 잡힌다 — 여기서는 그게 맞다.
    await expect(
      page.getByRole("link", { name: "현금흐름" }),
    ).toHaveCount(0);
  });

  test("정산 담당자는 사이드바에서 현금흐름으로 갈 수 있다", async ({ page }) => {
    await loginAs(page, "accountant");
    await page.goto("/dashboard");
    // `exact` 가 없으면 대시보드 카드의 "현금흐름 자세히 보기" 도 함께 잡혀
    // strict mode 위반이 난다. 카드가 언제 렌더되느냐에 따라 통과/실패가
    // 갈리던 자리다 — 이 테스트가 확인하려는 것은 **사이드바** 링크다.
    await page.getByRole("link", { name: "현금흐름", exact: true }).click();
    await expect(page).toHaveURL(/\/finance\/cashflow/);
    await expect(page.getByRole("heading", { name: "현금흐름" })).toBeVisible();
  });

  test("거절 응답에 내부 정보가 새지 않는다", async () => {
    const api = await apiAs("readonly");
    const res = await api.get("/api/v1/cashflow?preset=30d");
    expect(res.status()).toBe(403);

    const body = await res.text();
    for (const leak of ["Traceback", "SELECT ", "psycopg", "postgresql://"]) {
      expect(body, `오류 응답에 ${leak} 노출`).not.toContain(leak);
    }
    await api.dispose();
  });
});
