/**
 * 정책 → 마진 엔진 변환 계약 테스트.
 *
 * 왜 이 테스트가 있나
 * ----------------
 * 여기는 **DB 컬럼명과 계산 코드가 만나는 지점**이다. 키 하나를 잘못 읽으면
 * 예외가 나지 않고 `undefined` 가 되어 기본값으로 조용히 대체된다. 그 결과는
 * "화면이 안 뜬다" 가 아니라 **틀린 마진**이다 — 사용자는 그 숫자로 가격을 정한다.
 *
 * 특히 `fee_vat_included` 는 틀리면 수수료가 통째로 10% 어긋난다.
 */

import { describe, expect, it } from "vitest";
import { feeVatUnverified, toFeePolicy, toTaxRule } from "./policy-mapping";
import type { Policy } from "./policies";

function policy(fields: Record<string, unknown>, over: Partial<Policy> = {}): Policy {
  return {
    id: "p1",
    version: 3,
    effective_from: "2026-01-01",
    effective_to: null,
    source_type: "MANUAL",
    source_url: null,
    reliability_status: "CONFIRMED",
    memo: null,
    is_active: true,
    approved_by: null,
    approved_at: "2026-01-02T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    fields,
    ...over,
  };
}

describe("세금 정책 변환", () => {
  it("일반과세 + 부가세 포함가 → 판매가에 포함", () => {
    const rule = toTaxRule(policy({
      tax_type: "GENERAL",
      calculation_method: "STANDARD_RATIO",
      default_vat_inclusion: "INCLUDED",
      vat_rate: 0.1,
      input_vat_credit_policy: "FULL_CREDIT",
      import_vat_credit_policy: "CREDITABLE",
    }));

    expect(rule.taxProfileType).toBe("GENERAL");
    expect(rule.outputVatPolicy).toBe("INCLUDED_IN_PRICE");
    expect(rule.taxRateParameters.vatRate).toBe(0.1);
    expect(rule.ruleVersion).toBe(3);
  });

  it("부가세 별도 표기는 판매가에 더하는 방식이 된다", () => {
    const rule = toTaxRule(policy({
      calculation_method: "STANDARD_RATIO",
      default_vat_inclusion: "EXCLUDED",
      input_vat_credit_policy: "FULL_CREDIT",
      import_vat_credit_policy: "CREDITABLE",
    }));
    expect(rule.outputVatPolicy).toBe("ADDED_TO_PRICE");
  });

  it("간이·면세·영세율은 계산 방식이 따로 정한다", () => {
    const of = (m: string) =>
      toTaxRule(policy({ calculation_method: m, default_vat_inclusion: "INCLUDED" }))
        .outputVatPolicy;

    expect(of("SIMPLE_DEEMED")).toBe("DEEMED_VALUE_ADDED");
    expect(of("EXEMPT")).toBe("NONE");
    expect(of("OVERSEAS_ZERO_RATED")).toBe("ZERO_RATED");
  });

  it("법인은 과세 유형이 아니라 사업자 형태가 정한다", () => {
    const rule = toTaxRule(policy({ tax_type: "GENERAL", business_type: "CORPORATION" }));
    expect(rule.taxProfileType).toBe("CORPORATE");
  });

  it("공제 방식이 비면 확인되지 않은 규칙으로 표시된다", () => {
    // 승인 도장(approved_at)이 찍혀 있어도 계산에 쓰는 값이 비었으면 확인된 게 아니다.
    const rule = toTaxRule(policy({
      calculation_method: "STANDARD_RATIO",
      input_vat_credit_policy: null,
      import_vat_credit_policy: null,
    }));

    expect(rule.verifiedAt).toBeNull();
    // 미확인이면 공제를 잡아 주지 않는다 — 마진을 부풀리지 않는 쪽이다.
    expect(rule.inputVatCreditPolicy).toBe("NO_CREDIT");
    expect(rule.importVatCreditPolicy).toBe("NOT_CREDITABLE");
  });

  it("공제 방식이 모두 지정되면 확인된 시각이 실린다", () => {
    const rule = toTaxRule(policy({
      calculation_method: "STANDARD_RATIO",
      input_vat_credit_policy: "FULL_CREDIT",
      import_vat_credit_policy: "CREDITABLE",
    }));
    expect(rule.verifiedAt).toBe("2026-01-02T00:00:00Z");
  });
});

describe("수수료 정책 변환", () => {
  it("정률 정책의 값이 그대로 옮겨진다", () => {
    const fee = toFeePolicy(policy({
      marketplace: "COUPANG",
      category_path: "패션",
      fee_type: "RATE",
      rate: 0.108,
      fixed_amount: null,
      fee_vat_included: true,
    }));

    expect(fee.marketplace).toBe("COUPANG");
    expect(fee.category).toBe("패션");
    expect(fee.feeType).toBe("RATE");
    expect(fee.feeRate).toBe(0.108);
    expect(fee.feeVatIncluded).toBe(true);
  });

  it("정액 정책은 fixed_amount 를 읽는다", () => {
    const fee = toFeePolicy(policy({
      marketplace: "SMARTSTORE", fee_type: "FIXED", rate: null, fixed_amount: 1500,
    }));
    expect(fee.fixedFee).toBe(1500);
    expect(fee.feeRate).toBeNull();
  });

  it("부가세 포함 여부가 비면 '별도'로 계산하고 미확인으로 표시한다", () => {
    // 틀리면 수수료가 통째로 10% 어긋난다. 모를 때는 수수료를 크게 잡는 쪽으로
    // 기울여 마진을 부풀리지 않는다.
    const p = policy({ marketplace: "COUPANG", fee_type: "RATE", rate: 0.1 });

    expect(toFeePolicy(p).feeVatIncluded).toBe(false);
    expect(feeVatUnverified(p)).toBe(true);
  });

  it("포함 여부가 false 로 지정된 것은 미확인이 아니다", () => {
    // `null`(모름)과 `false`(별도라고 확인함)를 구분하지 못하면 경고가 영원히 뜬다.
    const p = policy({ marketplace: "COUPANG", fee_vat_included: false });
    expect(feeVatUnverified(p)).toBe(false);
  });

  it("카테고리 없음은 전체 기본값이다", () => {
    const fee = toFeePolicy(policy({ marketplace: "COUPANG", category_path: null }));
    expect(fee.category).toBeNull();
  });
});
