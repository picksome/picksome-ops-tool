/**
 * 옵션 어댑터 매핑 · 조합 키 단위 테스트.
 *
 * 백엔드 필드명과 화면 필드명이 다르고(`stock_quantity` vs `stock`),
 * DB 에 없는 필드도 있다. 매핑이 틀리면 저장은 성공하는데 값이 안 바뀐다 —
 * 타입 검사로는 절대 안 잡히는 종류의 버그다.
 */

import { describe, expect, it } from "vitest";
import { variantKeyOf, UNSUPPORTED_OPTION_FIELDS } from "@/lib/adapters";

describe("variantKeyOf", () => {
  it("키를 정렬해 같은 조합이 같은 키가 되게 한다", () => {
    // 정렬하지 않으면 {색상,사이즈} 와 {사이즈,색상} 이 다른 키가 되어
    // 같은 조합이 두 번 생긴다.
    expect(variantKeyOf({ 색상: "네이비", 사이즈: "L" }))
      .toBe(variantKeyOf({ 사이즈: "L", 색상: "네이비" }));
  });

  it("값이 다르면 키도 다르다", () => {
    expect(variantKeyOf({ 색상: "네이비" })).not.toBe(variantKeyOf({ 색상: "그레이" }));
  });

  it("단일 옵션도 키를 만든다", () => {
    expect(variantKeyOf({ 색상: "네이비" })).toBe("색상=네이비");
  });

  it("빈 조합은 빈 문자열", () => {
    expect(variantKeyOf({})).toBe("");
  });
});

describe("미지원 옵션 필드", () => {
  it("DB 에 없는 필드를 명시한다", () => {
    // 이 목록이 화면의 "비활성 + 사유 표시" 근거다. 비면 사용자가 입력하고 잃는다.
    expect(UNSUPPORTED_OPTION_FIELDS).toContain("customName");
    expect(UNSUPPORTED_OPTION_FIELDS).toContain("sourceOptionUrl");
    expect(UNSUPPORTED_OPTION_FIELDS).toContain("marketplaceOptionNo");
  });

  it("실제로 저장되는 필드는 목록에 없다", () => {
    for (const supported of ["optionSku", "optionCost", "extraPrice", "stock", "isActive"]) {
      expect(UNSUPPORTED_OPTION_FIELDS).not.toContain(supported);
    }
  });
});
