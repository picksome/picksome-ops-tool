"use client";

import { useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { saleChannelsApi } from "@/lib/api/sale-channels";

/**
 * 판매채널 동적 목록(M007, docs/27 D-287) — 하드코딩 대신 org 커스텀 채널까지 포함해 조회.
 *
 * `labelOf` 는 `useCallback` 으로 고정한다. 매 렌더마다 새 함수가 되면
 * 이걸 의존성으로 쓰는 `useMemo`(테이블 컬럼 정의 등)가 전부 매번 다시 계산되고,
 * lint 가 "의존성 누락" 을 경고하는데 넣으면 메모가 무의미해지는 상태가 된다.
 */
export function useSaleChannels() {
  const { data, isLoading } = useQuery({
    queryKey: ["sale-channels"],
    queryFn: () => saleChannelsApi.list(),
    staleTime: 5 * 60 * 1000,
  });

  const channels = useMemo(() => data ?? [], [data]);

  /** 모르는 코드는 코드 그대로 보여준다 — 이름을 지어내지 않는다. */
  const labelOf = useCallback(
    (code: string) => channels.find((c) => c.code === code)?.display_name ?? code,
    [channels],
  );

  return { channels, isLoading, labelOf };
}
