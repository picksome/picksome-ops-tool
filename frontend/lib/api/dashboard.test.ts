/**
 * 대시보드 API 클라이언트 계약 테스트 (Phase 45).
 *
 * 왜 이 테스트가 있나
 * ----------------
 * 대시보드는 예전에 `MOCK_PRODUCTS`/`MOCK_EXPENSES` 로 숫자를 만들었다.
 * 그 파일이 사라진 자리에 조용히 다시 생기는 것을 막는다.
 *
 * 1. 실제 백엔드 경로를 부른다 (경로는 문자열이라 tsc·lint·build 가 못 잡는다 — D2)
 * 2. **응답을 화면이 다시 계산하지 않는다** — 값 그대로 쓴다
 * 3. `null` 을 0으로 바꾸지 않는다
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { dashboardApi, SOURCE_LABEL, issueTypeLabel } from "./dashboard";

const calls: { url: string; method: string }[] = [];

function jsonResponse(data: unknown) {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify({ data }),
  } as unknown as Response;
}

let nextPayload: unknown = {};

beforeEach(() => {
  calls.length = 0;
  nextPayload = {};
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), method: init?.method ?? "GET" });
      return jsonResponse(nextPayload);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const lastUrl = () => calls[calls.length - 1].url;

describe("대시보드 클라이언트가 실제 백엔드 경로를 부른다", () => {
  it("경로는 /reports/dashboard 다", async () => {
    await dashboardApi.load();
    expect(lastUrl()).toContain("/reports/dashboard");
    // dashboard-summary(구 엔드포인트)로 새어 나가지 않는다.
    expect(lastUrl()).not.toContain("dashboard-summary");
  });

  it("기준 월과 트렌드 길이를 쿼리로 보낸다", async () => {
    await dashboardApi.load({ year_month: "2026-07", trend_months: 6 });
    expect(lastUrl()).toContain("year_month=2026-07");
    expect(lastUrl()).toContain("trend_months=6");
  });

  it("파라미터가 없으면 쿼리를 붙이지 않는다 — 서버가 이번 달을 정한다", async () => {
    await dashboardApi.load();
    expect(lastUrl()).not.toContain("year_month=");
  });
});

describe("응답을 그대로 쓴다 — 클라이언트가 다시 계산하지 않는다", () => {
  it("서버가 준 KPI 값을 변형하지 않는다", async () => {
    nextPayload = {
      kpis: [
        { key: "net_profit", label: "잠정 순이익 (미확정)", value: 85_894,
          format: "money", source: "LIVE", availability: "AVAILABLE",
          higher_is_better: true, hint: null, previous: 220_695,
          previous_source: "LIVE", comparable: true },
      ],
    };
    const res = await dashboardApi.load();
    expect(res.kpis[0].value).toBe(85_894);
    expect(res.kpis[0].label).toBe("잠정 순이익 (미확정)");
  });

  it("정산 미연결 KPI 의 null 을 0 으로 바꾸지 않는다", async () => {
    nextPayload = {
      kpis: [
        { key: "settlement_amount", label: "정산액", value: null,
          format: "money", source: "LIVE", availability: "NOT_CONNECTED",
          higher_is_better: true, hint: null, previous: null,
          previous_source: "LIVE", comparable: false },
      ],
    };
    const res = await dashboardApi.load();
    expect(res.kpis[0].value).toBeNull();
    expect(res.kpis[0].availability).toBe("NOT_CONNECTED");
  });
});

describe("표시 라벨", () => {
  it("source 코드마다 사람이 읽는 이름이 있다", () => {
    expect(SOURCE_LABEL.LIVE).toBe("진행 중 집계");
    expect(SOURCE_LABEL.MONTH_CLOSE_SNAPSHOT).toBe("확정 snapshot");
  });

  it("모르는 이슈 유형은 코드를 그대로 보여준다 — 이름을 지어내지 않는다", () => {
    expect(issueTypeLabel("MISSING_EVIDENCE")).toBe("증빙 누락");
    expect(issueTypeLabel("BRAND_NEW_SCANNER_RULE")).toBe("BRAND_NEW_SCANNER_RULE");
  });
});
