"use client";

/**
 * 마진 계산이 쓰는 정책 읽기 (Phase 13 마무리).
 *
 * 왜 훅으로 묶나
 * ------------
 * 마진 계산기·판매장부·상품 드로어가 각자 정책을 불러 썼고, 그 출처가
 * `settingsAdapter`(메모리 배열)였다. 화면마다 따로 부르면 설정 화면에서 값을
 * 바꿔도 어떤 화면은 옛 값을 계속 쓴다 — 같은 상품의 마진이 화면마다 달라진다.
 *
 * **읽는 곳을 하나로 만든다.** queryKey 도 정책 화면과 공유해서, 설정을 저장하면
 * 계산 화면이 자동으로 갱신된다.
 */

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { policiesApi } from "@/lib/api/policies";
import { feeVatUnverified, toFeePolicy, toTaxRule } from "@/lib/api/policy-mapping";
import type { MarketplaceFeePolicy, TaxRule } from "@/lib/calc/types";

export function useTaxRules(): { taxRules: TaxRule[]; isLoading: boolean } {
  const { data, isLoading } = useQuery({
    queryKey: ["policies", "tax"],
    queryFn: () => policiesApi.list("tax", { limit: 100 }),
    staleTime: 5 * 60 * 1000,
  });
  const taxRules = useMemo(() => (data?.items ?? []).map(toTaxRule), [data]);
  return { taxRules, isLoading };
}

export function useFeePolicies(): {
  feePolicies: MarketplaceFeePolicy[];
  /** 부가세 포함 여부가 확인되지 않은 정책 수. 화면이 경고에 쓴다. */
  unverifiedVatCount: number;
  isLoading: boolean;
} {
  const { data, isLoading } = useQuery({
    queryKey: ["policies", "marketplace_fee"],
    queryFn: () => policiesApi.list("marketplace_fee", { limit: 200 }),
    staleTime: 5 * 60 * 1000,
  });

  const feePolicies = useMemo(() => (data?.items ?? []).map(toFeePolicy), [data]);
  const unverifiedVatCount = useMemo(
    () => (data?.items ?? []).filter(feeVatUnverified).length,
    [data],
  );

  return { feePolicies, unverifiedVatCount, isLoading };
}
