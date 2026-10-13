/**
 * 현금흐름 API 클라이언트 계약 테스트.
 *
 * 왜 이 테스트가 있나
 * ----------------
 * 이 저장소에서 프론트가 없는 경로를 부르는 결함이 네 번 났다(D2/D6/D9/D11).
 * 경로는 그냥 문자열이라 타입체크도 린트도 빌드도 잡지 못한다.
 *
 * 현금흐름은 특히 위험하다 — 조용히 실패하면 화면이 "잔액 0원" 을 그리고,
 * 그건 "통장이 비었다" 로 읽힌다. 그래서 경로·필드·응답 해석을 여기에 고정한다.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CASH_SOURCE_LABEL,
  CASH_STATUS_LABEL,
  cashflowApi,
  type CashflowSummary,
} from "./cashflow";

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

describe("현금흐름 클라이언트가 실제 백엔드 경로를 부른다", () => {
  it("요약은 /cashflow 다", async () => {
    await cashflowApi.summary({ preset: "30d" });
    expect(lastUrl()).toContain("/cashflow");
    expect(lastUrl()).toContain("preset=30d");
    // 손익 라우터로 새지 않는다 — 현금흐름은 다른 질문에 답한다.
    expect(lastUrl()).not.toContain("/reports/");
    expect(lastUrl()).not.toContain("/monthly-close");
  });

  it("사용자 지정 기간은 date_from/date_to 로 보낸다", async () => {
    await cashflowApi.summary({ date_from: "2026-05-01", date_to: "2026-05-31" });
    expect(lastUrl()).toContain("date_from=2026-05-01");
    expect(lastUrl()).toContain("date_to=2026-05-31");
  });

  it("기준 잔액은 GET/PUT /cashflow/baseline 이다", async () => {
    nextPayload = { data: null };
    await cashflowApi.getBaseline();
    expect(lastUrl()).toContain("/cashflow/baseline");
    expect(lastCall().method).toBe("GET");

    await cashflowApi.setBaseline({ as_of_date: "2026-05-01", amount: 10_000_000 });
    expect(lastCall().method).toBe("PUT");
    expect(lastCall().body).toEqual({ as_of_date: "2026-05-01", amount: 10_000_000 });
  });

  it("수동 기록 목록은 기간이 필수다", async () => {
    await cashflowApi.listManualEvents({
      date_from: "2026-05-01",
      date_to: "2026-05-31",
    });
    expect(lastUrl()).toContain("/cashflow/manual-events");
    expect(lastUrl()).toContain("date_from=2026-05-01");
  });

  it("수동 기록 생성은 POST 하고 방향·상태를 그대로 보낸다", async () => {
    nextPayload = { data: { id: "x" } };
    await cashflowApi.createManualEvent({
      direction: "INFLOW",
      status: "ACTUAL",
      amount: 3_000_000,
      occurred_at: "2026-05-08",
    });
    expect(lastCall().method).toBe("POST");
    expect(lastCall().body).toEqual({
      direction: "INFLOW",
      status: "ACTUAL",
      amount: 3_000_000,
      occurred_at: "2026-05-08",
    });
  });

  it("삭제는 사유를 body 로 보낸다 — 사유 없는 삭제 경로가 없다", async () => {
    nextPayload = { data: { deleted: true } };
    await cashflowApi.deleteManualEvent("evt-1", "잘못 입력");
    expect(lastUrl()).toContain("/cashflow/manual-events/evt-1");
    expect(lastCall().method).toBe("DELETE");
    expect(lastCall().body).toEqual({ reason: "잘못 입력" });
  });
});

describe("응답 해석", () => {
  it("기준 잔액 없음(null)을 값으로 착각하지 않는다", async () => {
    // D7 과 같은 함정: `data` 가 정당하게 null 인 응답이다.
    nextPayload = { data: null };
    const baseline = await cashflowApi.getBaseline();
    expect(baseline).toBeNull();
  });

  it("잔액 미설정은 0원이 아니라 null 로 내려온다", async () => {
    const summary: CashflowSummary = {
      range: { from: "2026-05-01", to: "2026-05-31", preset: null },
      baseline: null,
      balance_available: false,
      inflow: { expected: 0, confirmed: 0, actual: 0 },
      outflow: { expected: 0, confirmed: 0, actual: 500_000 },
      projected_ending_balance: null,
      lowest_projected_balance: null,
      unpaid_payout: 0,
      overdue_payout: 0,
      timeline: [
        { date: "2026-05-09", inflow: 0, outflow: 500_000, net: -500_000, balance: null },
      ],
      rows: [],
      actual_inflow_source_available: false,
    };
    nextPayload = { data: summary };

    const got = await cashflowApi.summary();
    expect(got.balance_available).toBe(false);
    expect(got.projected_ending_balance).toBeNull();
    // 유출은 실제로 50만원이다 — "모른다" 와 "0" 을 섞지 않는다.
    expect(got.outflow.actual).toBe(500_000);
  });
});

describe("사전", () => {
  it("상태 라벨이 세 가지를 구분한다", () => {
    // "확정" 과 "실제" 를 같은 말로 쓰면 사용자가 안 들어온 돈을 들어온 것으로 읽는다.
    expect(CASH_STATUS_LABEL.EXPECTED).not.toBe(CASH_STATUS_LABEL.CONFIRMED);
    expect(CASH_STATUS_LABEL.CONFIRMED).not.toBe(CASH_STATUS_LABEL.ACTUAL);
  });

  it("백엔드가 내려주는 모든 source_type 에 라벨이 있다", () => {
    // 라벨이 없으면 화면에 원시 테이블명이 그대로 뜬다.
    for (const t of [
      "payout_payment",
      "payout_item",
      "marketplace_settlement_record",
      "manual_cash_event",
    ]) {
      expect(CASH_SOURCE_LABEL[t]).toBeTruthy();
    }
  });
});
