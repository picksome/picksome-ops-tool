/**
 * 소싱처 비교표 정렬.
 *
 * 여기서 지키는 것
 * -------------
 * 1. **계산 못 한 줄이 위로 올라오지 않는다** — 올라오면 "가장 싼 곳" 으로 읽힌다
 * 2. 정렬 기준을 바꿔도 없는 값은 여전히 아래다
 * 3. 정렬은 순위가 아니다 — 아무 줄에도 "추천" 표시가 붙지 않는다
 */

import { describe, expect, it } from "vitest";
import { sortRows } from "./product-sourcing-panel";
import type { SourceComparisonRow } from "@/lib/api/sourcing";

function row(over: Partial<SourceComparisonRow> = {}): SourceComparisonRow {
  return {
    product_source_id: over.supplier_name ?? "s1",
    supplier_name: "공급처", supplier_code: "SUP", country_code: null,
    currency: "KRW", is_primary: false, status: "ACTIVE",
    moq: null, lead_time_days: null, last_checked_at: null,
    landed_cost: {
      total_krw: null, currency: "KRW", breakdown: [], fx: null,
      missing: [], is_complete: false,
    },
    difference: null, difference_rate: null,
    ...over,
  };
}

function priced(name: string, krw: number): SourceComparisonRow {
  return row({
    supplier_name: name, product_source_id: name,
    landed_cost: {
      total_krw: krw, currency: "KRW", breakdown: [], fx: null,
      missing: [], is_complete: true,
    },
  });
}

function incalculable(name: string): SourceComparisonRow {
  return row({
    supplier_name: name, product_source_id: name,
    landed_cost: {
      total_krw: null, currency: "CNY", breakdown: [], fx: null,
      missing: ["FX_RATE:CNY/KRW"], is_complete: false,
    },
  });
}

describe("소싱처 비교 정렬", () => {
  it("계산 불가인 줄은 원가 정렬에서 맨 아래로 간다", () => {
    const sorted = sortRows(
      [incalculable("환율없음"), priced("비싼곳", 5000), priced("싼곳", 3000)],
      "landed",
    );
    expect(sorted.map((r) => r.supplier_name)).toEqual(["싼곳", "비싼곳", "환율없음"]);
  });

  it("모두 계산 불가면 순서를 바꾸지 않는다", () => {
    // 임의로 섞으면 사용자는 없는 우열이 있다고 읽는다.
    const rows = [incalculable("A"), incalculable("B")];
    expect(sortRows(rows, "landed").map((r) => r.supplier_name)).toEqual(["A", "B"]);
  });

  it("MOQ·리드타임 정렬에서도 모르는 값은 아래다", () => {
    const rows = [
      row({ supplier_name: "모름", product_source_id: "모름" }),
      row({ supplier_name: "적음", product_source_id: "적음", moq: 10, lead_time_days: 2 }),
      row({ supplier_name: "많음", product_source_id: "많음", moq: 500, lead_time_days: 30 }),
    ];
    expect(sortRows(rows, "moq").map((r) => r.supplier_name))
      .toEqual(["적음", "많음", "모름"]);
    expect(sortRows(rows, "lead_time").map((r) => r.supplier_name))
      .toEqual(["적음", "많음", "모름"]);
  });

  it("최근 확인은 최신이 먼저, 기록 없음은 아래다", () => {
    const rows = [
      row({ supplier_name: "없음", product_source_id: "없음" }),
      row({ supplier_name: "작년", product_source_id: "작년",
            last_checked_at: "2025-01-01T00:00:00Z" }),
      row({ supplier_name: "어제", product_source_id: "어제",
            last_checked_at: "2026-08-09T00:00:00Z" }),
    ];
    expect(sortRows(rows, "checked").map((r) => r.supplier_name))
      .toEqual(["어제", "작년", "없음"]);
  });

  it("정렬해도 원본 배열을 바꾸지 않는다", () => {
    const rows = [priced("비싼곳", 5000), priced("싼곳", 3000)];
    sortRows(rows, "landed");
    expect(rows.map((r) => r.supplier_name)).toEqual(["비싼곳", "싼곳"]);
  });

  it("어떤 줄에도 추천 표시가 붙지 않는다", () => {
    // 최저가를 추천으로 만들면 MOQ·품질처럼 숫자에 안 담기는 것이 사라진다.
    const sorted = sortRows([priced("싼곳", 3000), priced("비싼곳", 5000)], "landed");
    for (const r of sorted) {
      expect(r).not.toHaveProperty("recommended");
      expect(r).not.toHaveProperty("score");
      expect(r).not.toHaveProperty("rank");
    }
  });
});
