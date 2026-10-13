"use client";

/**
 * 목록 화면 공통 상태 — URL query parameter 와 동기화.
 *
 * 왜 URL 인가:
 *   - 새로고침·뒤로가기에서 필터가 유지된다.
 *   - "이 조건으로 본 화면"을 그대로 공유할 수 있다 (운영 툴에서 자주 필요하다).
 *   - 저장된 보기(saved view)를 URL 문자열 하나로 저장할 수 있다.
 *
 * 설계:
 *   - 기본값과 같은 값은 URL 에 쓰지 않는다. 주소가 짧게 유지되고,
 *     "기본 상태"와 "명시적으로 기본값을 고른 상태"를 구분할 필요가 없다.
 *   - 필터를 바꾸면 page 를 1로 되돌린다. 3페이지를 보다 필터를 좁히면
 *     결과가 1페이지뿐이라 빈 화면이 뜨는 흔한 함정을 막는다.
 *   - `replace` 로 히스토리를 쌓지 않는다. 타이핑 한 글자마다 뒤로가기가 생기면 안 된다.
 */

import { useCallback, useMemo } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { Route } from "next";

export type SortDir = "asc" | "desc";

export interface ListQueryState {
  /** 키워드 통합 검색 */
  q: string;
  /** 기간 시작 YYYY-MM-DD */
  from: string;
  /** 기간 종료 YYYY-MM-DD */
  to: string;
  /** 판매처 코드 */
  channel: string;
  /** 담당자 (user id 또는 business key) */
  owner: string;
  /** 상태 (화면마다 의미가 다름) */
  status: string;
  /** 추가 필터 — 화면별 자유 키 */
  extra: Record<string, string>;
  sort: string;
  dir: SortDir;
  page: number;
  pageSize: number;
  /** 활성 탭 */
  tab: string;
}

export const DEFAULT_LIST_QUERY: ListQueryState = {
  q: "",
  from: "",
  to: "",
  channel: "",
  owner: "",
  status: "",
  extra: {},
  sort: "",
  dir: "desc",
  page: 1,
  pageSize: 50,
  tab: "",
};

/** URL 에 직접 대응하는 알려진 키. 나머지는 extra 로 들어간다. */
const KNOWN_KEYS = new Set([
  "q", "from", "to", "channel", "owner", "status", "sort", "dir", "page", "pageSize", "tab",
]);

export interface UseListQueryOptions {
  /** 화면별 기본값 (예: pageSize, 기본 정렬, 기본 탭) */
  defaults?: Partial<ListQueryState>;
  /** 이 키들이 바뀌면 page 를 1로 되돌린다. 기본: page/pageSize 를 제외한 전부 */
  resetPageOnChange?: boolean;
}

export function useListQuery(options: UseListQueryOptions = {}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { defaults, resetPageOnChange = true } = options;

  const merged = useMemo<ListQueryState>(
    () => ({ ...DEFAULT_LIST_QUERY, ...defaults, extra: { ...(defaults?.extra ?? {}) } }),
    [defaults],
  );

  const state = useMemo<ListQueryState>(() => {
    const get = (k: string) => searchParams.get(k);
    const extra: Record<string, string> = { ...merged.extra };
    searchParams.forEach((value, key) => {
      if (!KNOWN_KEYS.has(key)) extra[key] = value;
    });
    const pageRaw = Number(get("page"));
    const sizeRaw = Number(get("pageSize"));
    return {
      q: get("q") ?? merged.q,
      from: get("from") ?? merged.from,
      to: get("to") ?? merged.to,
      channel: get("channel") ?? merged.channel,
      owner: get("owner") ?? merged.owner,
      status: get("status") ?? merged.status,
      extra,
      sort: get("sort") ?? merged.sort,
      dir: (get("dir") as SortDir) ?? merged.dir,
      page: Number.isFinite(pageRaw) && pageRaw > 0 ? pageRaw : merged.page,
      pageSize: Number.isFinite(sizeRaw) && sizeRaw > 0 ? sizeRaw : merged.pageSize,
      tab: get("tab") ?? merged.tab,
    };
  }, [searchParams, merged]);

  // 페이지 유지 여부는 `resetPageOnChange` 옵션과 `next.page` 명시 여부로 정해진다
  // (아래 참고). 호출 단위 `keepPage` 인자를 받던 흔적이 있었지만 쓰는 곳이
  // 하나도 없었고, 두 곳에서 같은 결정을 내리면 언젠가 어긋난다.
  const write = useCallback(
    (next: Partial<ListQueryState>) => {
      const usp = new URLSearchParams(searchParams.toString());

      const setOrDelete = (key: string, value: string | number, dflt: string | number) => {
        if (value === dflt || value === "" || value === undefined || value === null) {
          usp.delete(key);
        } else {
          usp.set(key, String(value));
        }
      };

      if ("q" in next) setOrDelete("q", next.q ?? "", merged.q);
      if ("from" in next) setOrDelete("from", next.from ?? "", merged.from);
      if ("to" in next) setOrDelete("to", next.to ?? "", merged.to);
      if ("channel" in next) setOrDelete("channel", next.channel ?? "", merged.channel);
      if ("owner" in next) setOrDelete("owner", next.owner ?? "", merged.owner);
      if ("status" in next) setOrDelete("status", next.status ?? "", merged.status);
      if ("sort" in next) setOrDelete("sort", next.sort ?? "", merged.sort);
      if ("dir" in next) setOrDelete("dir", next.dir ?? "desc", merged.dir);
      if ("pageSize" in next) setOrDelete("pageSize", next.pageSize ?? 50, merged.pageSize);
      if ("tab" in next) setOrDelete("tab", next.tab ?? "", merged.tab);
      if ("extra" in next && next.extra) {
        for (const [k, v] of Object.entries(next.extra)) {
          if (v === "" || v === undefined || v === null) usp.delete(k);
          else usp.set(k, v);
        }
      }

      // 필터가 바뀌면 1페이지로. page 를 직접 지정한 경우는 존중한다.
      if ("page" in next) {
        setOrDelete("page", next.page ?? 1, merged.page);
      } else if (resetPageOnChange) {
        const onlyPageSize = Object.keys(next).length === 1 && "pageSize" in next;
        if (!onlyPageSize) usp.delete("page");
      }

      const qs = usp.toString();
      router.replace((qs ? `${pathname}?${qs}` : pathname) as Route, { scroll: false });
    },
    [searchParams, router, pathname, merged, resetPageOnChange],
  );

  /** 필터 초기화 — 탭은 유지한다(탭은 필터가 아니라 화면 구획이다). */
  const reset = useCallback(() => {
    const usp = new URLSearchParams();
    const tab = searchParams.get("tab");
    if (tab) usp.set("tab", tab);
    const qs = usp.toString();
    router.replace((qs ? `${pathname}?${qs}` : pathname) as Route, { scroll: false });
  }, [router, pathname, searchParams]);

  /** 정렬 토글 — 같은 컬럼을 다시 누르면 방향이 뒤집힌다. */
  const toggleSort = useCallback(
    (column: string) => {
      if (state.sort === column) {
        write({ sort: column, dir: state.dir === "asc" ? "desc" : "asc" });
      } else {
        write({ sort: column, dir: "desc" });
      }
    },
    [state.sort, state.dir, write],
  );

  /** 기본값과 다른 필터가 하나라도 있는지 — "필터 초기화" 버튼 활성화 판단용. */
  const hasActiveFilters = useMemo(() => {
    return Boolean(
      state.q ||
        state.from ||
        state.to ||
        state.channel ||
        state.owner ||
        state.status ||
        Object.values(state.extra).some((v) => v),
    );
  }, [state]);

  /** 현재 상태를 그대로 담은 쿼리 문자열 — 저장된 보기(saved view) 저장용. */
  const queryString = searchParams.toString();

  return { state, set: write, reset, toggleSort, hasActiveFilters, queryString };
}
