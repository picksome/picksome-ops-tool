/**
 * 금전 화면 API 계약 회귀 테스트 (Phase 11).
 *
 * 왜 이 파일이 있나
 * ---------------
 * 이 저장소에서 프론트가 없는 경로를 부르는 결함이 네 번 났다.
 * ```
 * D2  /settlements/monthly/…   →  실제는 /monthly-close
 * D6  /policies/marketplace-fees → 실제는 /policies/{kind}
 * D9  unread_count / is_read     → 실제는 unread / read_at
 * D11 /admin/users/{id}/role     → 존재하지 않음
 * ```
 * 넷 다 **타입체크·린트·빌드를 전부 통과했다.** 경로와 필드명은 문자열이기
 * 때문이다. 화면은 조용히 빈 목록이나 0을 그리고, 금액 화면에서 그건
 * "데이터가 없다" 가 아니라 "0원이다" 로 읽힌다.
 *
 * 여기서는 **금액이 걸린 클라이언트**의 경로·파라미터·응답 해석을 고정한다.
 * 백엔드 라우터가 바뀌면 이 테스트가 먼저 깨져야 한다.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { marketplaceApi } from "./marketplace";
import { expensesApi } from "./expenses";
import { operationsApi } from "./operations";
import { payoutsApi } from "./payouts";
import { dashboardApi } from "./dashboard";

const calls: { url: string; method: string; body?: unknown }[] = [];
let nextPayload: unknown = { data: [], meta: { total: 0 } };

beforeEach(() => {
  calls.length = 0;
  nextPayload = { data: [], meta: { total: 0 } };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        url: String(input),
        method: init?.method ?? "GET",
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      });
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify(nextPayload),
      } as unknown as Response;
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const lastUrl = () => calls[calls.length - 1].url;
const lastCall = () => calls[calls.length - 1];

describe("쇼핑몰 정산 클라이언트", () => {
  it("목록·상세는 /marketplace-settlements 다", async () => {
    await marketplaceApi.list();
    expect(lastUrl()).toContain("/marketplace-settlements");
    // 월 마감(/monthly-close)이나 세무 리포트로 새지 않는다.
    expect(lastUrl()).not.toContain("/monthly-close");

    await marketplaceApi.get("rec-1");
    expect(lastUrl()).toContain("/marketplace-settlements/rec-1");
  });

  it("계정별 필터가 실제로 쿼리에 실린다", async () => {
    // 이게 빠지면 본점 화면에 2호점 정산서가 섞여 나온다 (M033).
    await marketplaceApi.list({ integration_account_id: "acc-1" });
    expect(lastUrl()).toContain("integration_account_id=acc-1");
  });

  it("계정 미상(null)을 라벨로 채우지 않는다", async () => {
    nextPayload = {
      data: [
        {
          id: "r1",
          marketplace: "COUPANG",
          integration_account_id: null,
          integration_account_label: null,
          net_settlement: 0,
        },
      ],
      meta: { total: 1 },
    };
    const res = await marketplaceApi.list();
    const row = res.items[0];
    // null 은 "모른다" 다. 채널 이름으로 채우면 "본점" 이라고 단정하는 셈이다.
    expect(row.integration_account_label).toBeNull();
  });
});

describe("사업비용 클라이언트", () => {
  it("CRUD 경로", async () => {
    await expensesApi.list({ from: "2026-05-01", to: "2026-05-31" });
    expect(lastUrl()).toContain("/expenses");
    // 기간 파라미터 이름은 from/to 다 — year_month 가 아니다.
    expect(lastUrl()).toContain("from=2026-05-01");
    expect(lastUrl()).toContain("to=2026-05-31");

    nextPayload = { data: { id: "e1" } };
    await expensesApi.create({
      expense_date: "2026-05-10",
      category_id: "c1",
      description: "사무용품",
      gross_amount: 110000,
      payment_method: "BUSINESS_CARD",
    } as Parameters<typeof expensesApi.create>[0]);
    expect(lastCall().method).toBe("POST");
    expect(lastUrl()).toMatch(/\/expenses$/);

    await expensesApi.update("e1", { gross_amount: 120000 });
    // PUT 이 아니라 PATCH 다 — PUT 이면 서버가 나머지 필드를 비운다.
    expect(lastCall().method).toBe("PATCH");
    expect(lastUrl()).toContain("/expenses/e1");
  });
});

describe("지급 장부 클라이언트", () => {
  it("지급 기록은 금액과 날짜를 그대로 보낸다", async () => {
    nextPayload = { data: { payment_id: "p1", item: {} } };
    await payoutsApi.recordPayment("item-1", {
      payment_date: "2026-05-10",
      amount: 1_000_000,
      payment_reference: "TEST-REF",
    });
    expect(lastUrl()).toContain("/payouts/items/item-1/payments");
    expect(lastCall().body).toMatchObject({
      payment_date: "2026-05-10",
      amount: 1_000_000,
    });
  });

  it("승인은 배치 하위 경로다 — '완료 처리' 같은 별도 경로가 없다", async () => {
    // 지급 완료는 지급 기록으로만 만들어진다. 상태만 바꾸는 경로가 생기면
    // 기록 없는 PAID 가 가능해진다.
    nextPayload = { data: {} };
    await payoutsApi.approve("batch-1");
    expect(lastUrl()).toContain("/payouts/batch-1/approve");
    expect(lastUrl()).not.toContain("/complete");
    expect(lastUrl()).not.toContain("/mark-paid");
  });
});

describe("worker · 시스템 상태 클라이언트", () => {
  it("헬스·실행이력·수동실행 경로", async () => {
    nextPayload = { data: { workers: [], stale_run_threshold_minutes: 30 } };
    await operationsApi.workers();
    expect(lastUrl()).toContain("/operations/workers");

    nextPayload = { data: [], meta: { total: 0 } };
    await operationsApi.workerRuns({ limit: 20 });
    expect(lastUrl()).toContain("/operations/workers/runs");
    expect(lastUrl()).toContain("limit=20");

    nextPayload = { data: {} };
    await operationsApi.runWorker("issue_scan");
    expect(lastUrl()).toContain("/operations/workers/issue_scan/run");
    expect(lastCall().method).toBe("POST");
  });
});

describe("대시보드 클라이언트", () => {
  it("한 번의 요청으로 받는다 — 프론트가 순이익을 다시 계산하지 않는다", async () => {
    nextPayload = { data: { period: {}, kpis: [], cashflow: {} } };
    await dashboardApi.load({ year_month: "2026-05" });
    expect(lastUrl()).toContain("/reports/dashboard");
    expect(lastUrl()).toContain("year_month=2026-05");
  });

  it("현금흐름 카드가 응답에 포함된다", async () => {
    // 별도 요청으로 가져오면 두 화면이 다른 시점의 숫자를 말하게 된다.
    nextPayload = {
      data: {
        period: {},
        kpis: [],
        cashflow: {
          basis: "오늘부터 30일",
          balance_available: false,
          projected_ending_balance: null,
        },
      },
    };
    const res = await dashboardApi.load({ year_month: "2026-05" });
    expect(res.cashflow?.basis).toBe("오늘부터 30일");
    // 잔액을 계산할 수 없을 때 0 이 아니라 null 이다.
    expect(res.cashflow?.projected_ending_balance).toBeNull();
    expect(calls).toHaveLength(1);
  });

  it("현금흐름을 볼 수 없는 역할에게는 카드 키 자체가 없다", async () => {
    // 서버가 아예 빼고 준다. 화면은 이때 0원이 아니라 카드를 그리지 않는다.
    nextPayload = { data: { period: {}, kpis: [] } };
    const res = await dashboardApi.load({ year_month: "2026-05" });
    expect(res.cashflow).toBeUndefined();
  });
});
