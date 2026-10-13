/**
 * 정책 API → 마진 엔진 타입 변환 (Phase 13 마무리).
 *
 * 왜 이 파일이 있나
 * ---------------
 * 마진 엔진(`lib/calc/engine.ts`)은 `TaxRule`/`MarketplaceFeePolicy` 를 받아
 * 계산한다. 직전까지 그 값의 출처는 `settingsAdapter` 라는 **메모리 배열**이었다.
 * 즉 마진 계산기·판매장부·상품 드로어에 뜨는 수수료와 부가세가 전부 지어낸
 * 숫자였다. 저장해도 사라지고, 조직마다 다른 값을 쓸 수도 없었다.
 *
 * 여기서 실제 `/policies/{kind}` 응답을 엔진이 아는 모양으로 옮긴다.
 * **읽는 곳이 하나여야 한다** — 설정 화면은 쓰고 계산 화면은 읽는데, 둘이 다른
 * 출처를 보면 사용자가 값을 바꿔도 계산이 안 움직인다.
 *
 * 모르는 값을 지어내지 않는다
 * -----------------------
 * DB 에 없는 값은 `null` 로 둔다(M036 로 컬럼을 만들었지만 기존 행은 비어 있다).
 * 특히 `feeVatIncluded` 를 잘못 잡으면 **수수료가 통째로 10% 어긋난다.**
 * 그래서 기본값으로 채우지 않고, 확인되지 않았다는 사실을 `verifiedAt: null` 로
 * 전달해 화면이 "확인 필요" 를 띄우게 한다.
 */

import type {
  ImportVatCreditPolicy,
  InputVatCreditPolicy,
  MarketplaceFeePolicy,
  OutputVatPolicy,
  TaxProfileType,
  TaxRule,
} from "@/lib/calc/types";
import type { Policy } from "./policies";

/** `tax_policy.calculation_method` + `default_vat_inclusion` → 엔진의 매출세액 정책. */
function outputVatOf(fields: Record<string, unknown>): OutputVatPolicy {
  const method = String(fields.calculation_method ?? "");
  if (method === "SIMPLE_DEEMED") return "DEEMED_VALUE_ADDED";
  if (method === "EXEMPT") return "NONE";
  if (method === "OVERSEAS_ZERO_RATED") return "ZERO_RATED";
  // STANDARD_RATIO 는 판매가 표기 방식이 결정한다.
  return String(fields.default_vat_inclusion ?? "INCLUDED") === "EXCLUDED"
    ? "ADDED_TO_PRICE"
    : "INCLUDED_IN_PRICE";
}

function taxProfileOf(fields: Record<string, unknown>): TaxProfileType {
  // 법인은 `business_type` 이 결정한다 — 과세 유형(tax_type)과 다른 축이다.
  if (String(fields.business_type ?? "") === "CORPORATION") return "CORPORATE";
  const t = String(fields.tax_type ?? "GENERAL");
  if (t === "SIMPLE") return "SIMPLE";
  if (t === "EXEMPT") return "EXEMPT";
  return "GENERAL";
}

/**
 * 세금 정책 1건을 엔진 타입으로.
 *
 * `verifiedAt` 은 **엔진이 계산에 쓰는 값이 전부 확인됐을 때만** 채운다.
 * 승인 도장(`approved_at`)만 보고 채우면, 승인 당시 존재하지도 않던 컬럼이
 * 비어 있는데도 "확인된 규칙" 으로 표시된다.
 */
export function toTaxRule(p: Policy): TaxRule {
  const f = p.fields as Record<string, unknown>;
  const input = (f.input_vat_credit_policy as InputVatCreditPolicy | null) ?? null;
  const imported = (f.import_vat_credit_policy as ImportVatCreditPolicy | null) ?? null;
  const complete = input !== null && imported !== null;

  return {
    id: p.id,
    taxProfileType: taxProfileOf(f),
    effectiveFrom: p.effective_from,
    effectiveTo: p.effective_to,
    outputVatPolicy: outputVatOf(f),
    // 미확인이면 가장 보수적인 쪽을 쓴다 — 공제를 잡아 주면 마진이 부풀어 보이고,
    // 사용자는 그 상태로 가격을 정한다. 동시에 verifiedAt=null 로 경고를 띄운다.
    inputVatCreditPolicy: input ?? "NO_CREDIT",
    importVatCreditPolicy: imported ?? "NOT_CREDITABLE",
    taxRateParameters: { vatRate: Number(f.vat_rate ?? 0.1) },
    description: p.memo ?? `${f.tax_type ?? ""} ${f.applies_to ?? ""}`.trim(),
    verifiedAt: complete ? p.approved_at : null,
    ruleVersion: p.version,
  };
}

/** 판매처 수수료 정책 1건을 엔진 타입으로. */
export function toFeePolicy(p: Policy): MarketplaceFeePolicy {
  const f = p.fields as Record<string, unknown>;
  const vatIncluded = f.fee_vat_included;

  return {
    id: p.id,
    marketplace: String(f.marketplace ?? ""),
    category: (f.category_path as string | null) ?? null,
    feeType: (f.fee_type as MarketplaceFeePolicy["feeType"]) ?? "RATE",
    feeRate: f.rate == null ? null : Number(f.rate),
    fixedFee: f.fixed_amount == null ? null : Number(f.fixed_amount),
    tiers: (f.tier_json as MarketplaceFeePolicy["tiers"]) ?? undefined,
    // 미확인이면 "부가세 별도" 로 본다 — 수수료를 더 크게 잡는 쪽이라
    // 마진을 부풀리지 않는다. 화면은 이 상태를 별도로 표시한다.
    feeVatIncluded: vatIncluded == null ? false : Boolean(vatIncluded),
    effectiveFrom: p.effective_from,
    effectiveTo: p.effective_to,
    description: p.memo ?? "",
    sourceReference: p.source_url,
    ruleVersion: p.version,
  };
}

/** 부가세 포함 여부가 확인되지 않은 수수료 정책. 화면이 경고를 띄운다. */
export function feeVatUnverified(p: Policy): boolean {
  return (p.fields as Record<string, unknown>).fee_vat_included == null;
}
