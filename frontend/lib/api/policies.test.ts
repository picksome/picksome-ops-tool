/**
 * 정책 API 클라이언트 계약 테스트.
 *
 * 왜 이 테스트가 있나
 * ----------------
 * 이 파일의 이전 버전은 `/policies/marketplace-fees`, `/policies/tax` 를 불렀다.
 * 백엔드 라우터는 `/policies/{kind}` 이고 kind 는 snake_case 다.
 * **그 경로는 존재하지 않았고, tsc·lint·build 가 전부 통과했다** — 경로는 문자열이다.
 * (D2 와 같은 종류의 결함. 그때는 월정산 화면 전체가 404 였다.)
 *
 * 그래서 경로와 kind 를 테스트로 고정한다. 백엔드 라우터가 바뀌면 여기가 깨져야 한다.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { policiesApi, POLICY_KIND_LABEL, type PolicyKind } from "./policies";

const calls: { url: string; method: string; body?: unknown }[] = [];

beforeEach(() => {
  calls.length = 0;
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
        text: async () => JSON.stringify({ data: [], meta: { total: 0 } }),
      } as unknown as Response;
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const last = () => calls[calls.length - 1];

/** 백엔드 `app/domain/policy_crud.py` 의 PolicyKind 와 **정확히** 같아야 한다. */
const BACKEND_KINDS: PolicyKind[] = [
  "marketplace_fee",
  "payment_fee",
  "tax",
  "fulfillment",
  "return_risk",
  "ad_cost",
  "target_margin",
  "settlement",
  "partner_share",
  "expense_category",
  "evidence_type",
];

describe("정책 경로가 실제 백엔드 라우터와 같다", () => {
  it("kind 는 snake_case 이고 경로는 /policies/{kind} 다", async () => {
    await policiesApi.list("partner_share");
    expect(last().url).toContain("/policies/partner_share");
    // 예전의 잘못된 경로로 돌아가지 않는다.
    expect(last().url).not.toContain("marketplace-fees");
  });

  it("모든 kind 에 한글 라벨이 있다 — 화면이 코드를 그대로 보여주지 않는다", () => {
    for (const kind of BACKEND_KINDS) {
      expect(POLICY_KIND_LABEL[kind], kind).toBeTruthy();
    }
    expect(Object.keys(POLICY_KIND_LABEL).sort()).toEqual([...BACKEND_KINDS].sort());
  });

  it("유효 정책 조회는 /active 다", async () => {
    await policiesApi.active("settlement", "2026-05-31");
    expect(last().url).toContain("/policies/settlement/active");
    expect(last().url).toContain("at=2026-05-31");
  });

  it("생성은 version + effective_from + fields 를 보낸다", async () => {
    await policiesApi.create("partner_share", {
      version: 2,
      effective_from: "2026-05-01",
      fields: { partner_id: "p-1", share_ratio: 0.6, applies_year_month: null },
    });
    expect(last().method).toBe("POST");
    expect(last().url).toContain("/policies/partner_share");
    expect(last().body).toMatchObject({
      version: 2,
      effective_from: "2026-05-01",
      fields: { partner_id: "p-1", share_ratio: 0.6 },
    });
  });

  it("승인·폐기 경로가 분리돼 있다", async () => {
    await policiesApi.approve("partner_share", "id-1");
    expect(last().url).toContain("/policies/partner_share/id-1/approve");

    await policiesApi.deprecate("partner_share", "id-1", "종료");
    expect(last().url).toContain("/policies/partner_share/id-1/deprecate");
    expect(last().body).toMatchObject({ reason: "종료" });
  });
});
