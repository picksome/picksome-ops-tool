"use client";

import { useEffect, useState } from "react";
import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { MIN_SEARCH_LEN, searchApi, type SearchResult } from "@/lib/api/search";

const STATIC_PAGES = [
  { path: "/dashboard", label: "대시보드", description: "Overview" },
  { path: "/sales", label: "판매 장부", description: "Profit" },
  { path: "/products", label: "상품 관리", description: "Product Ops" },
  { path: "/settlements/monthly", label: "월별 정산", description: "Settlement" },
  { path: "/reports", label: "세무 리포트", description: "Settlement" },
  { path: "/expenses", label: "사업비용", description: "Cost" },
  { path: "/margin/calculator", label: "마진 계산기", description: "Profit" },
  { path: "/admin/users", label: "사용자/권한", description: "Settings" },
  { path: "/me", label: "내 계정", description: "Overview" },
];

const KRW = new Intl.NumberFormat("ko-KR");

/** "12건 중 5건" — 잘린 게 있을 때만 전체 건수를 덧붙인다 */
function groupLabel(base: string, shown: number, total: number) {
  return total > shown ? `${base} (${total}건 중 ${shown}건)` : `${base} (${total}건)`;
}

export function CmdKSearch() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const router = useRouter();

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setOpen((prev) => !prev);
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, []);

  // 타이핑마다 서버를 때리지 않도록 200ms 디바운스.
  useEffect(() => {
    const t = setTimeout(() => setDebounced(query), 200);
    return () => clearTimeout(t);
  }, [query]);

  const { data, isFetching } = useQuery<SearchResult>({
    queryKey: ["search", debounced],
    queryFn: () => searchApi.query(debounced),
    enabled: debounced.trim().length >= MIN_SEARCH_LEN,
    staleTime: 5000,
  });

  function navigate(path: string) {
    setOpen(false);
    setQuery("");
    setDebounced("");
    // 검색 결과 경로는 런타임에 조립되므로(`/products/${id}` 등) typedRoutes로
    // 정적 검증이 불가능하다. Route 단언으로 좁혀 any 사용을 피한다.
    router.push(path as Route);
  }

  const searching = query.trim().length >= MIN_SEARCH_LEN;

  // 페이지는 서버를 거치지 않는다 — 정적 목록이라 클라이언트 필터가 더 빠르다.
  const pages = searching
    ? STATIC_PAGES.filter(
        (p) =>
          p.label.includes(query.trim()) ||
          p.path.includes(query.trim().toLowerCase()),
      )
    : STATIC_PAGES;

  const products = data?.products ?? [];
  const sales = data?.sales ?? [];
  const expenses = data?.expenses ?? [];
  const counts = data?.counts;

  // 디바운스 중이거나 요청 중이면 "결과 없음" 을 띄우지 않는다 — 타이핑 도중
  // 빈 상태가 깜빡이면 실제로 결과가 없는 것처럼 보인다.
  const settled = searching && !isFetching && debounced === query;
  const nothing =
    settled &&
    products.length === 0 &&
    sales.length === 0 &&
    expenses.length === 0 &&
    pages.length === 0;

  return (
    <CommandDialog open={open} onOpenChange={setOpen}>
      <CommandInput
        placeholder="검색 (상품, 판매, 사업비용, 페이지)..."
        value={query}
        onValueChange={setQuery}
      />
      <CommandList>
        {nothing && <CommandEmpty>검색 결과 없음</CommandEmpty>}

        {searching && !settled && products.length === 0 && (
          <div className="px-3 py-6 text-center text-xs text-[--color-muted-foreground]">
            검색 중...
          </div>
        )}

        {products.length > 0 && (
          <CommandGroup heading={groupLabel("상품", products.length, counts?.products ?? products.length)}>
            {products.map((p) => (
              <CommandItem
                key={p.id}
                value={`product-${p.id}`}
                onSelect={() => navigate(`/products/${p.id}`)}
              >
                <span className="mr-2 font-mono text-xs text-[--color-muted-foreground]">
                  {p.sku || p.business_key}
                </span>
                <span className="flex-1 truncate">{p.name}</span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}

        {sales.length > 0 && (
          <CommandGroup heading={groupLabel("판매", sales.length, counts?.sales ?? sales.length)}>
            {sales.map((s) => (
              <CommandItem
                key={s.id}
                value={`sale-${s.id}`}
                onSelect={() => navigate("/sales")}
              >
                <span className="mr-2 font-mono text-xs text-[--color-muted-foreground]">
                  {s.sale_date}
                </span>
                <span className="flex-1 truncate">{s.name ?? "-"}</span>
                <span className="ml-2 text-xs text-[--color-muted-foreground]">
                  {s.marketplace} · {KRW.format(s.sale_price)}원
                </span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}

        {expenses.length > 0 && (
          <CommandGroup heading={groupLabel("사업비용", expenses.length, counts?.expenses ?? expenses.length)}>
            {expenses.map((e) => (
              <CommandItem
                key={e.id}
                value={`expense-${e.id}`}
                onSelect={() => navigate("/expenses")}
              >
                <span className="mr-2 font-mono text-xs text-[--color-muted-foreground]">
                  {e.expense_date}
                </span>
                <span className="flex-1 truncate">{e.name ?? "-"}</span>
                <span className="ml-2 text-xs text-[--color-muted-foreground]">
                  {KRW.format(e.gross_amount)}원
                </span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}

        {pages.length > 0 && (
          <CommandGroup heading="페이지">
            {pages.map((p) => (
              <CommandItem
                key={p.path}
                value={`page-${p.path}`}
                onSelect={() => navigate(p.path)}
              >
                <span className="flex-1">{p.label}</span>
                <span className="text-xs text-[--color-muted-foreground]">
                  {p.description}
                </span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}
      </CommandList>
    </CommandDialog>
  );
}
