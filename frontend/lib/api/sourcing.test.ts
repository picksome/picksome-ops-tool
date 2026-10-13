/**
 * 소싱 API 클라이언트 계약 테스트.
 *
 * 왜 이 테스트가 있나
 * ----------------
 * 이 저장소에서 프론트가 없는 경로를 부르는 결함이 네 번 났다(D2/D6/D9/D11).
 * 소싱은 특히 위험하다 — 조용히 실패하면 화면이 "원가 0원" 을 그리고, 사용자는
 * 그 숫자로 판매가를 정한다.
 *
 * 라우트 함정도 함께 고정한다: 기존 소싱 후보 라우터가 `/sourcing/{item_id}` 를
 * 갖고 있어서 `/sourcing/suppliers` 는 그쪽에 먼저 잡힌다. 그래서 경로를
 * `/supplier-sites` · `/product-sources` 로 분리했다.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  missingLabel, requestCostAdoption, sourcingApi,
  type CostComparison, type Recommendation,
} from "./sourcing";

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

const lastUrl = () => calls[calls.length - 1].url;
const lastCall = () => calls[calls.length - 1];

describe("경로", () => {
  it("공급처는 /supplier-sites 다 — /sourcing 이 아니다", async () => {
    await sourcingApi.listSuppliers();
    expect(lastUrl()).toContain("/supplier-sites");
    // `/sourcing/{item_id}` 가 먼저 잡아 UUID 파싱 422 가 난다.
    expect(lastUrl()).not.toContain("/sourcing/suppliers");
  });

  it("소싱처·원가·환율 경로", async () => {
    await sourcingApi.listSources("p1");
    expect(lastUrl()).toContain("/products/p1/sources");

    await sourcingApi.costVersions("s1");
    expect(lastUrl()).toContain("/product-sources/s1/cost-versions");

    await sourcingApi.listFxRates({ base_currency: "CNY" });
    expect(lastUrl()).toContain("/fx-rates");
    expect(lastUrl()).toContain("base_currency=CNY");
  });

  it("비교표는 서버가 한 번에 계산한다", async () => {
    // 소싱처마다 landed-cost 를 따로 부르면 화면이 계산을 조립하게 된다.
    nextPayload = { data: { product_id: "p1", sources: [], comparable_count: 0 } };
    await sourcingApi.sourceComparison("p1");
    expect(lastUrl()).toContain("/products/p1/source-comparison");
    expect(calls).toHaveLength(1);
  });

  it("주 소싱처 교체는 한 번의 POST 다", async () => {
    // 내리고 올리기를 두 번 보내면 중간에 끊길 때 primary 가 없어진다.
    nextPayload = { data: { id: "s1", is_primary: true } };
    await sourcingApi.setPrimary("s1");
    expect(lastCall().method).toBe("POST");
    expect(lastUrl()).toContain("/product-sources/s1/primary");
    expect(calls).toHaveLength(1);
  });

  it("원가는 새 버전 POST 로만 만든다 — 수정 경로가 없다", async () => {
    nextPayload = { data: { id: "cv1" } };
    await sourcingApi.addCostVersion("s1", {
      purchase_price: 120, currency: "CNY", valid_from: "2026-08-01",
    });
    expect(lastCall().method).toBe("POST");
    expect(sourcingApi).not.toHaveProperty("updateCostVersion");
  });

  it("삭제는 사유를 body 로 보낸다", async () => {
    nextPayload = { data: { archived: true } };
    await sourcingApi.archiveSupplier("sup1", "거래 종료");
    expect(lastCall().method).toBe("DELETE");
    expect(lastCall().body).toEqual({ reason: "거래 종료" });
  });
});

describe("계산 불가를 0 으로 만들지 않는다", () => {
  it("환율이 없으면 total 이 null 로 온다", async () => {
    const payload: CostComparison = {
      product_id: "p1", product_name: "상품", supplier_name: "1688",
      current_cost_amount: 28_000,
      landed_cost: {
        total_krw: null, currency: "CNY", breakdown: [], fx: null,
        missing: ["FX_RATE:CNY/KRW"], is_complete: false,
      },
      difference: null, difference_rate: null,
    };
    nextPayload = { data: payload };

    const res = await sourcingApi.landedCost("s1");
    expect(res.landed_cost.total_krw).toBeNull();
    // 한쪽을 모르면 차이도 모른다 — 0 이면 "차이 없음" 으로 읽힌다.
    expect(res.difference).toBeNull();
  });

  it("정책이 없으면 권장가가 available:false 로 온다", async () => {
    const payload: Recommendation = {
      available: false, missing: ["POLICY:tax_policy"],
    };
    nextPayload = { data: payload };
    const res = await sourcingApi.recommendation("s1", "COUPANG");
    expect(res.available).toBe(false);
    expect(res.prices).toBeUndefined();
  });

  it("미입력 항목과 실제 0 을 구분한다", async () => {
    nextPayload = {
      data: {
        landed_cost: {
          total_krw: 29_000, currency: "KRW", is_complete: true, missing: [], fx: null,
          breakdown: [
            { key: "purchase_price", label: "매입가", amount: 29000,
              currency: "KRW", amount_krw: 29000, provided: true },
            { key: "customs_duty", label: "관세", amount: 0,
              currency: "KRW", amount_krw: 0, provided: false },
          ],
        },
        current_cost_amount: null, difference: null, difference_rate: null,
        product_id: "p1", product_name: "x", supplier_name: "y",
      },
    };
    const res = await sourcingApi.landedCost("s1");
    const duty = res.landed_cost.breakdown.find((b) => b.key === "customs_duty");
    expect(duty?.provided).toBe(false);
  });
});

describe("사람이 읽는 사유", () => {
  it("코드를 그대로 보여주지 않는다", () => {
    expect(missingLabel("SOURCE_COST")).toContain("매입가");
    expect(missingLabel("FX_RATE:CNY/KRW")).toContain("CNY/KRW");
    expect(missingLabel("POLICY:tax_policy")).toContain("세금");
  });

  it("모르는 코드는 그대로 돌려준다 — 이름을 지어내지 않는다", () => {
    expect(missingLabel("SOMETHING_NEW")).toBe("SOMETHING_NEW");
  });
});

describe("상품 원가 채택", () => {
  it("직접 PATCH 하지 않고 COST_CHANGE 승인 요청을 만든다", async () => {
    nextPayload = { data: { id: "req1" } };
    await requestCostAdoption({
      productId: "p1", landedCostKrw: 32_400, supplierName: "1688",
      costVersionId: "abcdef12-3456", fxRate: "205",
    });

    const req = lastCall();
    expect(req.url).toContain("/approvals");
    expect(req.body).toMatchObject({
      target_type: "product",
      request_type: "COST_CHANGE",
      target_id: "p1",
      proposed_changes: { cost_amount: 32_400 },
      submit: true,
    });
    // 상품을 직접 고치는 요청이 나가면 안 된다.
    expect(calls.some((c) => c.method === "PATCH" && c.url.includes("/products/")))
      .toBe(false);
  });

  it("근거를 사유에 남긴다", async () => {
    // 승인 payload 는 평면 스칼라만 담는다 — 승인자는 무엇을 근거로
    // 바뀌는지 알아야 한다.
    nextPayload = { data: { id: "req1" } };
    await requestCostAdoption({
      productId: "p1", landedCostKrw: 32_400, supplierName: "1688",
      costVersionId: "abcdef12-3456", fxRate: "205",
    });
    const reason = (lastCall().body as { reason: string }).reason;
    expect(reason).toContain("1688");
    expect(reason).toContain("205");
    expect(reason).toContain("abcdef12");
  });
});
