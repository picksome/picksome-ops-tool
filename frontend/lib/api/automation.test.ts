/**
 * 자동화 · AI 접근 클라이언트 계약.
 *
 * 여기서 지키는 것
 * -------------
 * 1. **토큰 원문을 요청으로 보내지 않는다** — 서버가 만든다
 * 2. 발급 응답의 `token` 은 한 번뿐이라는 사실을 타입이 말한다
 * 3. `BLOCKED` 를 성공·실패와 다른 말로 표시한다
 * 4. 실행 안 함(SKIPPED)과 실패를 구분한다
 * 5. 비율 한도와 금액 한도를 구분한다
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  GUARDRAIL_LABEL, MODE_LABEL, OUTCOME_LABEL, RUN_STATUS_LABEL, SCOPE_LABEL,
  aiAccessApi, automationApi, isRate,
} from "./automation";

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
        ok: true, status: 200,
        text: async () => JSON.stringify(nextPayload),
      } as unknown as Response;
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

const last = () => calls[calls.length - 1];

describe("경로", () => {
  it("자동화 정책·실행·리포트", async () => {
    await automationApi.policies();
    expect(last().url).toContain("/automation/policies");

    await automationApi.runs({ limit: 5 });
    expect(last().url).toContain("/automation/runs");

    await automationApi.dailyReport("2026-08-10");
    expect(last().url).toContain("/automation/daily-report");
    expect(last().url).toContain("report_date=2026-08-10");
  });

  it("정책 저장은 유형당 하나라 PUT 이다", async () => {
    nextPayload = { data: { id: "p1" } };
    await automationApi.savePolicy({
      automation_type: "MARGIN_MONITOR", enabled: true, mode: "REPORT_ONLY",
    });
    expect(last().method).toBe("PUT");
  });
});

describe("토큰", () => {
  it("토큰 문자열을 클라이언트가 만들지 않는다", async () => {
    // 사용자가 값을 정하면 엔트로피를 보장할 수 없고, 그러면 빠른 해시를
    // 쓰는 근거가 무너진다.
    nextPayload = { data: { id: "t1", token: "sos_x" } };
    await aiAccessApi.issueToken("a1", { scopes: ["products:read"] });

    const body = last().body as Record<string, unknown>;
    expect(Object.keys(body)).not.toContain("token");
    expect(Object.keys(body)).not.toContain("token_hash");
  });

  it("토큰을 되읽는 함수가 없다", () => {
    for (const name of Object.keys(aiAccessApi)) {
      expect(name).not.toMatch(/^(get|read|reveal|show)Token/);
    }
  });

  it("폐기는 즉시 사용 불가를 뜻한다", async () => {
    nextPayload = { data: { revoked: true } };
    await aiAccessApi.revokeToken("t1");
    expect(last().method).toBe("DELETE");
    expect(last().url).toContain("/ai-access/tokens/t1");
  });
});

describe("결과를 뭉개지 않는다", () => {
  it("한도 중단을 성공·실패와 다른 말로 쓴다", () => {
    // "아무 일도 없었다" 와 "하려다 한도에 걸렸다" 는 다른 상태다.
    const labels = Object.values(OUTCOME_LABEL);
    expect(new Set(labels).size).toBe(labels.length);
    expect(OUTCOME_LABEL.BLOCKED).toContain("한도");
    expect(OUTCOME_LABEL.BLOCKED).not.toBe(OUTCOME_LABEL.FAILED);
  });

  it("실행 안 함과 실패를 구분한다", () => {
    expect(RUN_STATUS_LABEL.SKIPPED).not.toBe(RUN_STATUS_LABEL.FAILED);
    expect(RUN_STATUS_LABEL.PARTIAL).not.toBe(RUN_STATUS_LABEL.SUCCESS);
  });

  it("모드 셋을 서로 다른 말로 쓴다", () => {
    const labels = Object.values(MODE_LABEL);
    expect(new Set(labels).size).toBe(3);
    expect(MODE_LABEL.AUTO_EXECUTE).toContain("자동");
  });
});

describe("한도", () => {
  it("비율과 금액을 구분한다", () => {
    // %와 원을 같은 칸으로 다루면 0.2 를 20원으로 넣게 된다.
    expect(isRate("min_margin_rate")).toBe(true);
    expect(isRate("max_change_amount")).toBe(false);
    expect(isRate("max_actions_per_run")).toBe(false);
  });

  it("라벨에 코드가 새어 나오지 않는다", () => {
    // 값이 키와 같으면 사용자는 `claim_lookback_days` 를 그대로 보게 된다.
    for (const [key, label] of Object.entries(GUARDRAIL_LABEL)) {
      expect(label).not.toBe(key);
      expect(label).not.toMatch(/_/);
    }
  });
});

describe("권한 표시", () => {
  it("코드를 그대로 보여주지 않는다", () => {
    expect(SCOPE_LABEL["cost:read"]).toContain("원가");
    expect(SCOPE_LABEL["approvals:create"]).toContain("승인");
  });

  it("실행 권한 라벨이 없다 — 그런 scope 를 발급하지 않는다", () => {
    for (const key of Object.keys(SCOPE_LABEL)) {
      expect(key).not.toMatch(/execute|reopen|transfer/);
    }
  });
});
