/**
 * 변경 분류 · diff 단위 테스트.
 *
 * 여기서 잡으려는 사고
 * -------------------
 * · 원가 변경이 승인을 안 거치고 바로 저장되는 것 (승인 워크플로 무력화)
 * · 안 바꾼 필드가 승인 diff 에 섞여 승인자가 진짜 변경을 못 찾는 것
 * · 빈 문자열과 null 을 같게 다뤄 "값 지우기" 가 동작하지 않는 것
 *
 * 셋 다 타입 검사로는 절대 안 잡힌다.
 */

import { describe, expect, it } from "vitest";
import {
  classifyProductChanges,
  diffAgainst,
  type FieldOwnership,
} from "@/lib/api/products";

/** 백엔드 `/approvals/field-ownership` 이 실제로 내려주는 모양. */
const OWNERSHIP: FieldOwnership = {
  request_types: {
    PRODUCT_INFO_CHANGE: ["name_ko", "name_en", "category_path", "supply_url", "supplier_id"],
    COST_CHANGE: ["cost_amount", "cost_currency", "cost_fx_rate", "cost_amount_foreign"],
    STATUS_CHANGE: ["status", "margin_type"],
  },
  approval_fields: [
    "name_ko", "name_en", "category_path", "supply_url", "supplier_id",
    "cost_amount", "cost_currency", "cost_fx_rate", "cost_amount_foreign",
    "status", "margin_type",
  ],
  sensitive_fields: ["cost_amount", "cost_amount_foreign", "cost_fx_rate", "cost_currency"],
};

describe("classifyProductChanges", () => {
  it("승인 대상이 아닌 필드는 직접 저장으로 간다", () => {
    const r = classifyProductChanges({ memo: "메모 수정", sku: "PS-0001" }, OWNERSHIP);
    expect(r.direct).toEqual({ memo: "메모 수정", sku: "PS-0001" });
    expect(r.approval).toEqual({});
    expect(r.hasApproval).toBe(false);
  });

  it("원가는 절대 직접 저장으로 가지 않는다", () => {
    const r = classifyProductChanges({ cost_amount: 20000 }, OWNERSHIP);
    expect(r.direct).toEqual({});
    expect(r.approval).toEqual({ COST_CHANGE: { cost_amount: 20000 } });
  });

  it("요청 유형별로 나눠 담는다 — 한 요청에 섞이면 소유권이 깨진다", () => {
    const r = classifyProductChanges(
      { cost_amount: 20000, name_ko: "새 이름", status: "PAUSED" },
      OWNERSHIP,
    );
    expect(r.approval).toEqual({
      COST_CHANGE: { cost_amount: 20000 },
      PRODUCT_INFO_CHANGE: { name_ko: "새 이름" },
      STATUS_CHANGE: { status: "PAUSED" },
    });
  });

  it("직접 저장과 승인이 섞여도 각자 제 경로로 간다", () => {
    const r = classifyProductChanges({ memo: "메모", cost_amount: 5000 }, OWNERSHIP);
    expect(r.direct).toEqual({ memo: "메모" });
    expect(r.approval).toEqual({ COST_CHANGE: { cost_amount: 5000 } });
    expect(r.hasDirect).toBe(true);
    expect(r.hasApproval).toBe(true);
  });

  it("표를 못 받았으면 승인 요청을 지어내지 않는다", () => {
    // 추측으로 승인 요청을 만들면 사용자가 의도하지 않은 요청이 승인함에 쌓인다.
    // 직접 저장으로 보내면 백엔드가 403 으로 정확히 거절한다.
    const r = classifyProductChanges({ cost_amount: 20000 }, null);
    expect(r.direct).toEqual({ cost_amount: 20000 });
    expect(r.approval).toEqual({});
  });

  it("승인 대상인데 소유 유형이 없으면 unroutable 로 모은다", () => {
    const broken: FieldOwnership = {
      ...OWNERSHIP,
      approval_fields: [...OWNERSHIP.approval_fields, "mystery_field"],
    };
    const r = classifyProductChanges({ mystery_field: 1 }, broken);
    expect(r.unroutable).toEqual(["mystery_field"]);
    // 직접 저장으로 흘려보내지 않는다 — 403 이 날 게 뻔한 요청을 보내지 않는다.
    expect(r.direct).toEqual({});
  });

  it("null 값도 승인 경로로 간다 — '값 비우기' 도 변경이다", () => {
    const r = classifyProductChanges({ supply_url: null }, OWNERSHIP);
    expect(r.approval).toEqual({ PRODUCT_INFO_CHANGE: { supply_url: null } });
  });

  it("변경이 없으면 아무 경로도 타지 않는다", () => {
    const r = classifyProductChanges({}, OWNERSHIP);
    expect(r.hasDirect).toBe(false);
    expect(r.hasApproval).toBe(false);
  });

  it("0 을 falsy 로 취급해 빠뜨리지 않는다", () => {
    // `if (value)` 로 걸렀다면 원가 0원 변경이 통째로 사라진다.
    const r = classifyProductChanges({ cost_amount: 0 }, OWNERSHIP);
    expect(r.approval).toEqual({ COST_CHANGE: { cost_amount: 0 } });
  });
});

describe("diffAgainst", () => {
  it("바뀐 것만 남긴다", () => {
    expect(diffAgainst({ a: 1, b: 2 }, { a: 1, b: 3 })).toEqual({ b: 3 });
  });

  it("undefined 는 '안 건드림' 이라 무시한다", () => {
    expect(diffAgainst({ a: 1 }, { a: undefined })).toEqual({});
  });

  it("null 은 '비움' 이라 변경으로 잡는다", () => {
    expect(diffAgainst({ a: "값" }, { a: null })).toEqual({ a: null });
  });

  it("빈 문자열과 null 은 같은 '빈 값' 이다", () => {
    // 이걸 구분하면 아무것도 안 고쳤는데 승인 요청이 만들어진다.
    expect(diffAgainst({ a: null }, { a: "" })).toEqual({});
    expect(diffAgainst({ a: "" }, { a: null })).toEqual({});
    expect(diffAgainst({ a: null }, { a: "   " })).toEqual({});
  });

  it("빈 값 → 실제 값은 변경이다", () => {
    expect(diffAgainst({ a: null }, { a: "새 값" })).toEqual({ a: "새 값" });
  });

  it("0 과 null 은 다르다 — 원가 0원과 원가 미입력은 다른 상태다", () => {
    expect(diffAgainst({ cost_amount: null }, { cost_amount: 0 })).toEqual({ cost_amount: 0 });
    expect(diffAgainst({ cost_amount: 0 }, { cost_amount: null })).toEqual({ cost_amount: null });
  });

  it("false 를 빠뜨리지 않는다", () => {
    expect(diffAgainst({ has_variants: true }, { has_variants: false })).toEqual({
      has_variants: false,
    });
  });

  it("base 에 없던 키도 변경으로 잡는다", () => {
    expect(diffAgainst({}, { memo: "신규" })).toEqual({ memo: "신규" });
  });

  it("숫자 문자열과 숫자는 다르다 — 조용히 같다고 보면 안 된다", () => {
    // 입력 컴포넌트가 문자열을 흘리면 승인 diff 에 "20000" → 20000 이 뜬다.
    // 같다고 처리해 버리면 그 버그가 영원히 안 보인다.
    expect(diffAgainst({ cost_amount: 20000 }, { cost_amount: "20000" })).toEqual({
      cost_amount: "20000",
    });
  });
});

describe("분류와 diff 조합", () => {
  it("안 바뀐 원가는 승인 요청을 만들지 않는다", () => {
    const base = { cost_amount: 10000, memo: "그대로" };
    const next = { cost_amount: 10000, memo: "바뀜" };
    const r = classifyProductChanges(diffAgainst(base, next), OWNERSHIP);
    expect(r.hasApproval).toBe(false);
    expect(r.direct).toEqual({ memo: "바뀜" });
  });

  it("원가만 바꾸면 직접 저장은 한 건도 안 나간다", () => {
    const base = { cost_amount: 10000, memo: "그대로" };
    const next = { cost_amount: 12000, memo: "그대로" };
    const r = classifyProductChanges(diffAgainst(base, next), OWNERSHIP);
    expect(r.hasDirect).toBe(false);
    expect(r.approval).toEqual({ COST_CHANGE: { cost_amount: 12000 } });
  });
});
