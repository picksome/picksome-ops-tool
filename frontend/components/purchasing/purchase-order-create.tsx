"use client";

/**
 * 발주 초안 만들기.
 *
 * 왜 이 화면이 필요한가
 * -------------------
 * 지금까지 발주 초안은 자동화나 MCP 만 만들 수 있었다. 사람이 "이거 지금
 * 주문해야겠다" 고 판단했을 때 쓸 수 있는 길이 없으면 결국 엑셀로 돌아간다.
 *
 * 주 소싱처를 자동 확정하지 않는다
 * ----------------------------
 * 상품에 주 소싱처가 있으면 **후보로 보여주되** 사용자가 고르게 한다. 자동으로
 * 정하면 "왜 이 공급처로 나갔지" 를 나중에 설명할 수 없고, 실제로는 이번만
 * 다른 곳에서 사는 일이 흔하다.
 *
 * 단가를 지어내지 않는다
 * -------------------
 * 소싱 원가가 있으면 채워 넣지만, 없으면 **비워 둔다.** 0 으로 채우면 0원짜리
 * 발주가 나가고 그게 그대로 취득원가가 된다.
 *
 * 초안까지만이다
 * ------------
 * 이 화면은 `DRAFT` 를 만든다. 공급처에 아무것도 보내지 않는다. 확정은 발주
 * 상세에서 별도로 누른다 — 돈을 쓰기로 하는 결정을 만들기와 같은 동작에
 * 묶지 않는다.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { purchaseOrderApi } from "@/lib/api/inventory";
import { productsApi } from "@/lib/api/products";
import { sourcingApi } from "@/lib/api/sourcing";
import { formatKRW } from "@/lib/format";

interface DraftLine {
  key: string;
  product_id: string;
  product_name: string;
  quantity: string;
  unit_price: string;
  /** 단가를 소싱 원가에서 채웠는지. 화면이 «추천» 임을 밝힌다. */
  price_suggested: boolean;
}

export function PurchaseOrderCreate({
  open,
  onOpenChange,
  /** 재주문 화면에서 넘어온 경우. 추천 수량이 미리 채워진다. */
  prefill,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  prefill?: {
    supplier_site_id: string;
    lines: {
      product_id: string;
      product_name: string;
      quantity: number;
      unit_price: number;
    }[];
  };
}) {
  const qc = useQueryClient();
  const [supplierId, setSupplierId] = React.useState("");
  const [expectedAt, setExpectedAt] = React.useState("");
  const [memo, setMemo] = React.useState("");
  const [lines, setLines] = React.useState<DraftLine[]>([]);
  const [search, setSearch] = React.useState("");

  // 열릴 때만 초기화한다. 열려 있는 동안 부모가 다시 그려도 사용자가 채운
  // 내용을 지우지 않는다 — 이 저장소에서 이미 겪은 종류의 결함이다.
  const openedRef = React.useRef(false);
  React.useEffect(() => {
    if (!open) {
      openedRef.current = false;
      return;
    }
    if (openedRef.current) return;
    openedRef.current = true;
    setSupplierId(prefill?.supplier_site_id ?? "");
    setExpectedAt("");
    setMemo("");
    setLines(
      (prefill?.lines ?? []).map((l, i) => ({
        key: `${l.product_id}:${i}`,
        product_id: l.product_id,
        product_name: l.product_name,
        quantity: String(l.quantity),
        unit_price: String(l.unit_price),
        price_suggested: true,
      })),
    );
  }, [open, prefill]);

  const suppliers = useQuery({
    queryKey: ["sourcing", "suppliers", "for-po"],
    queryFn: () => sourcingApi.listSuppliers({ limit: 200 }),
    enabled: open,
  });

  const products = useQuery({
    queryKey: ["products", "for-po", search],
    queryFn: () => productsApi.list({ q: search, page_size: 20 }),
    enabled: open && search.length > 0,
  });

  const create = useMutation({
    mutationFn: () =>
      purchaseOrderApi.create({
        supplier_site_id: supplierId,
        lines: lines.map((l) => ({
          product_id: l.product_id,
          quantity: Number(l.quantity),
          unit_price: Number(l.unit_price),
        })),
        expected_at: expectedAt || null,
        memo: memo || null,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["purchase-orders"] });
      onOpenChange(false);
    },
  });

  function addProduct(id: string, name: string, cost: number | null) {
    if (lines.some((l) => l.product_id === id)) return;
    setLines((prev) => [
      ...prev,
      {
        key: `${id}:${prev.length}`,
        product_id: id,
        product_name: name,
        quantity: "1",
        // 원가를 모르면 **비워 둔다.** 0 으로 채우면 0원짜리 발주가 나가고
        // 그게 그대로 취득원가가 된다.
        unit_price: cost === null ? "" : String(cost),
        price_suggested: cost !== null,
      },
    ]);
    setSearch("");
  }

  function patchLine(key: string, field: "quantity" | "unit_price", value: string) {
    setLines((prev) =>
      prev.map((l) =>
        l.key === key
          ? { ...l, [field]: value, ...(field === "unit_price" ? { price_suggested: false } : {}) }
          : l,
      ),
    );
  }

  const valid =
    supplierId !== "" &&
    lines.length > 0 &&
    lines.every((l) => Number(l.quantity) > 0 && l.unit_price !== "" && Number(l.unit_price) >= 0);

  const total = lines.reduce(
    (sum, l) => sum + (Number(l.quantity) || 0) * (Number(l.unit_price) || 0),
    0,
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>발주 초안 만들기</DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          {/* ── 공급처 ─────────────────────────────────────────── */}
          <div className="flex flex-wrap items-end gap-3">
            <label className="text-sm">
              <span className="mb-1 block text-[--color-muted-foreground]">
                공급처
              </span>
              <select
                value={supplierId}
                onChange={(e) => setSupplierId(e.target.value)}
                className="h-9 w-64 rounded-md border border-[--color-border] bg-transparent px-2 text-sm"
              >
                <option value="">선택하세요</option>
                {(suppliers.data?.items ?? []).map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-sm">
              <span className="mb-1 block text-[--color-muted-foreground]">
                예상 입고일
              </span>
              <Input
                type="date"
                value={expectedAt}
                onChange={(e) => setExpectedAt(e.target.value)}
                className="w-44"
              />
            </label>
          </div>
          {prefill && (
            <p className="text-xs text-[--color-muted-foreground]">
              재주문 분석에서 넘어온 <strong>추천값</strong>입니다. 수량과 단가를
              확인한 뒤 만드세요.
            </p>
          )}

          {/* ── 품목 추가 ──────────────────────────────────────── */}
          <div>
            <label className="text-sm">
              <span className="mb-1 block text-[--color-muted-foreground]">
                품목 추가
              </span>
              <Input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="상품명 또는 SKU 로 검색"
                className="w-full"
              />
            </label>
            {search && (products.data?.items?.length ?? 0) > 0 && (
              <ul className="mt-1 max-h-48 overflow-y-auto rounded-md border border-[--color-border]">
                {products.data!.items.map((p) => (
                  <li key={p.id}>
                    <button
                      type="button"
                      onClick={() => addProduct(p.id, p.name_ko, p.cost_amount ?? null)}
                      className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm hover:bg-[--color-muted]"
                    >
                      <span className="truncate">{p.name_ko}</span>
                      <span className="shrink-0 text-xs text-[--color-muted-foreground]">
                        {p.cost_amount === null
                          ? "원가 없음"
                          : formatKRW(p.cost_amount)}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* ── 품목 ───────────────────────────────────────────── */}
          {lines.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-xs text-[--color-muted-foreground]">
                  <tr className="border-b border-[--color-border]">
                    <th className="py-1 pr-3 font-medium">품목</th>
                    <th className="py-1 pr-3 text-right font-medium">수량</th>
                    <th className="py-1 pr-3 text-right font-medium">단가</th>
                    <th className="py-1 pr-3 text-right font-medium">금액</th>
                    <th className="py-1" />
                  </tr>
                </thead>
                <tbody>
                  {lines.map((l) => (
                    <tr key={l.key} className="border-b border-[--color-border] last:border-0">
                      <td className="max-w-0 truncate py-1 pr-3">
                        {l.product_name}
                      </td>
                      <td className="py-1 pr-3 text-right">
                        <Input
                          type="number"
                          min={1}
                          value={l.quantity}
                          onChange={(e) => patchLine(l.key, "quantity", e.target.value)}
                          className="ml-auto h-8 w-20 text-right"
                        />
                      </td>
                      <td className="py-1 pr-3 text-right">
                        <Input
                          type="number"
                          min={0}
                          value={l.unit_price}
                          onChange={(e) => patchLine(l.key, "unit_price", e.target.value)}
                          placeholder="필수"
                          className="ml-auto h-8 w-28 text-right"
                        />
                        {l.price_suggested && (
                          <span className="mt-0.5 block text-[10px] text-[--color-muted-foreground]">
                            소싱 원가에서 가져옴
                          </span>
                        )}
                      </td>
                      <td className="py-1 pr-3 text-right tabular-nums">
                        {formatKRW(
                          (Number(l.quantity) || 0) * (Number(l.unit_price) || 0),
                        )}
                      </td>
                      <td className="py-1 text-right">
                        <button
                          type="button"
                          onClick={() =>
                            setLines((prev) => prev.filter((x) => x.key !== l.key))
                          }
                          className="text-[--color-muted-foreground] hover:text-[--color-foreground]"
                          aria-label="품목 제거"
                        >
                          <X className="size-4" />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <label className="block text-sm">
            <span className="mb-1 block text-[--color-muted-foreground]">메모</span>
            <Input value={memo} onChange={(e) => setMemo(e.target.value)} />
          </label>

          <div className="flex items-center justify-between gap-3 border-t border-[--color-border] pt-3">
            <span className="text-sm tabular-nums">합계 {formatKRW(total)}</span>
            <div className="flex items-center gap-2">
              <span className="text-xs text-[--color-muted-foreground]">
                초안만 만듭니다. 공급처에 보내지 않습니다.
              </span>
              <Button
                disabled={!valid || create.isPending}
                onClick={() => create.mutate()}
                className="gap-1"
              >
                <Plus className="size-4" />
                초안 만들기
              </Button>
            </div>
          </div>
          {!valid && lines.length > 0 && (
            <p className="text-xs text-[--color-muted-foreground]">
              공급처와 모든 품목의 수량·단가가 필요합니다. 단가를 모르면 0 으로
              채우지 말고 소싱 원가를 먼저 확인하세요.
            </p>
          )}
          {create.isError && (
            <p className="text-sm text-[--color-destructive]">
              {(create.error as Error).message}
            </p>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
