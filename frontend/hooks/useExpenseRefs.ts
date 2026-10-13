"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import {
  expenseCategoriesApi, externalPartnersApi, partnersApi,
} from "@/lib/api/partners";
import type { RefResolver } from "@/lib/api/approvals";

/**
 * 비용이 가리키는 참조 목록 — 파트너(결제자·귀속) + 비용 카테고리.
 *
 * 비용 화면과 승인 상세가 **같은 이름을 보여야 한다.** 각자 따로 조회하면
 * 한쪽만 갱신돼 같은 UUID 가 두 이름으로 보인다. 캐시 키를 공유해 한 번만 받는다.
 *
 * 비활성 파트너도 받는다 — 그만둔 파트너가 결제한 작년 비용의 결제자 이름이
 * 사라지면 목록이 "미지정" 으로 보이고, 상환 대상이 누구였는지 알 수 없다.
 * 선택 목록에는 `activePartners` 를 쓴다(과거 값은 보이되 새로 고를 수는 없다).
 */
export function useExpenseRefs() {
  const { data: partners } = useQuery({
    queryKey: ["partners", "all"],
    queryFn: () => partnersApi.list(true),
    staleTime: 5 * 60 * 1000,
  });
  const { data: categories } = useQuery({
    queryKey: ["expense-categories"],
    queryFn: () => expenseCategoriesApi.list(),
    staleTime: 5 * 60 * 1000,
  });

  /**
   * 거래처 — **비활성·삭제된 것도 받는다.**
   *
   * 승인 diff 에서 "거래처를 A → B 로 바꾼다" 를 보여줘야 하는데, 그 사이
   * A 가 비활성이 됐다고 이름이 사라지면 승인자는 무엇에서 무엇으로 바뀌는지
   * 알 수 없다. 선택 목록(활성만)과 이름 해석(전체)은 다른 요구다.
   */
  const { data: suppliers } = useQuery({
    queryKey: ["external-partners", "all-for-resolve"],
    queryFn: () => externalPartnersApi.list({ page_size: 200 }),
    staleTime: 5 * 60 * 1000,
  });

  const partnerList = React.useMemo(() => partners ?? [], [partners]);
  const categoryList = React.useMemo(() => categories ?? [], [categories]);
  const supplierList = React.useMemo(() => suppliers?.items ?? [], [suppliers]);

  const resolve = React.useCallback<RefResolver>(
    (field, id) => {
      if (field === "category_id") return categoryList.find((c) => c.id === id)?.name;
      if (field === "payment_card_holder_id" || field === "attributable_partner_id") {
        return partnerList.find((p) => p.id === id)?.name;
      }
      if (field === "supplier_id") {
        const hit = supplierList.find((s) => s.id === id);
        if (hit) return hit.display_name;
        // 목록에 없다 = 삭제된 거래처다. **"다른 항목" 으로 뭉뚱그리지 않는다** —
        // 승인자는 그게 "이름을 못 찾음" 인지 "지워진 거래처" 인지 알아야 한다.
        return "삭제된 거래처";
      }
      // product_variant_id 는 아직 목록 API 가 없다. 없는 이름을 추측해 만들지 않는다.
      return undefined;
    },
    [partnerList, categoryList, supplierList],
  );

  return {
    partners: partnerList,
    activePartners: partnerList.filter((p) => p.is_active),
    categories: categoryList,
    suppliers: supplierList,
    resolve,
  };
}
