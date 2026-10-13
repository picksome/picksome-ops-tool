"use client";

/**
 * 재고 · 재주문.
 *
 * 이 화면이 답하는 질문
 * -------------------
 * ```
 * 지금 뭐가 몇 개 있나          재고 현황
 * 뭘 언제까지 주문해야 하나      재주문
 * 왜 추천이 안 나오나           추천 불가 목록
 * ```
 *
 * 세 번째가 이 화면에서 가장 중요하다
 * -------------------------------
 * 재주문 추천이 안 나오는 이유는 대개 **데이터가 없어서**다. 그걸 숨기면
 * 사용자는 "우리 상품은 다 여유 있구나" 로 읽고, 어느 날 갑자기 품절난다.
 *
 * 그래서 추천 불가 목록을 접어 두지 않고, 무엇을 채우면 되는지 함께 쓴다.
 *
 * 기간을 사용자가 정한다
 * -------------------
 * 관찰 기간·목표 커버 일수에 "권장값" 을 코드에 박아 두지 않는다. 회전율은
 * 업종마다 다르고, 화면에 뜬 숫자가 어디서 왔는지 사용자가 알아야 한다.
 * 처음 들어오면 **비어 있고**, 정해야 계산이 돈다.
 */

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import type { Route } from "next";
import { AlertCircle, FileUp, PackageX, TriangleAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PageShell, Section } from "@/components/shell/page-shell";
import {
  BLOCK_ACTION,
  PRICE_BASIS_LABEL,
  inventoryApi,
  type BlockReason,
} from "@/lib/api/inventory";
import { InventoryDetail } from "@/components/inventory/inventory-detail";
import { PurchaseOrderCreate } from "@/components/purchasing/purchase-order-create";
import { purchaseOrderApi, type ReorderGroup } from "@/lib/api/inventory";
import { formatKRW } from "@/lib/format";

export default function InventoryPage() {
  // 빈 문자열로 시작한다. 0 이나 30 으로 시작하면 사용자가 정하지 않은 값을
  // 정한 값처럼 믿게 된다.
  const [velocityDays, setVelocityDays] = React.useState("");
  const [coverageDays, setCoverageDays] = React.useState("");
  const [applied, setApplied] = React.useState<{ v: number; c: number } | null>(
    null,
  );
  // 어느 행을 펼쳤나. 상품+옵션 조합이 키다.
  const [openKey, setOpenKey] = React.useState<string | null>(null);
  // 재주문 추천에서 발주 초안으로 넘길 내용.
  const [draftFrom, setDraftFrom] = React.useState<ReorderGroup | null>(null);

  const stock = useQuery({
    queryKey: ["inventory", "list"],
    queryFn: () => inventoryApi.list({ limit: 200 }),
  });

  const reorder = useQuery({
    queryKey: ["inventory", "reorder", applied?.v, applied?.c],
    queryFn: () => inventoryApi.reorder(applied!.v, applied!.c, true),
    enabled: applied !== null,
  });

  const items = stock.data?.items ?? [];
  const negative = items.filter((i) => i.negative);

  return (
    <PageShell
      title="재고 · 재주문"
      variant="detail"
      actions={
        <Link href={"/inventory/import" as Route}>
          <Button variant="secondary" size="sm" className="gap-1">
            <FileUp className="size-4" />
            재고 가져오기
          </Button>
        </Link>
      }
    >
      {/* ── 재고가 음수인 것부터 ─────────────────────────────── */}
      {negative.length > 0 && (
        <Section title="재고가 음수입니다">
          <p className="mb-3 text-sm text-[--color-muted-foreground]">
            판매가 재고보다 많이 기록됐습니다. 기초 재고가 빠졌거나 입고가 누락된
            경우가 대부분입니다. 0 으로 맞추지 말고 원인을 찾은 뒤 조정하세요.
          </p>
          <ul className="space-y-1">
            {negative.map((i) => (
              <li
                key={`${i.product_id}:${i.product_variant_id ?? ""}`}
                className="flex items-center justify-between rounded-md border border-[--color-border] px-3 py-2 text-sm"
              >
                <span className="truncate">{i.product_name}</span>
                <Badge variant="danger">{i.on_hand}개</Badge>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {/* 추천값을 그대로 확정하지 않는다 — 사용자가 수량·단가를 보고 만든다. */}
      <PurchaseOrderCreate
        open={draftFrom !== null}
        onOpenChange={(open) => {
          if (!open) setDraftFrom(null);
        }}
        prefill={
          draftFrom
            ? {
                supplier_site_id: draftFrom.supplier_site_id,
                lines: draftFrom.lines.map((c) => ({
                  product_id: c.product_id,
                  product_name: c.product_name,
                  quantity: c.recommended_quantity,
                  unit_price: c.unit_price,
                })),
              }
            : undefined
        }
      />

      {/* ── 재주문 기준 ──────────────────────────────────────── */}
      <Section title="재주문 기준">
        <p className="mb-3 text-sm text-[--color-muted-foreground]">
          판매속도를 며칠 치로 볼지, 발주로 며칠 치를 확보할지 정하세요. 회전이
          빠른 상품이 많으면 관찰 기간을 짧게, 시즌을 타면 길게 봅니다.
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-sm">
            <span className="mb-1 block text-[--color-muted-foreground]">
              판매속도 관찰 기간 (일)
            </span>
            <Input
              type="number"
              min={1}
              max={365}
              value={velocityDays}
              onChange={(e) => setVelocityDays(e.target.value)}
              className="w-40"
              placeholder="예: 30"
            />
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-[--color-muted-foreground]">
              목표 커버 일수 (일)
            </span>
            <Input
              type="number"
              min={1}
              max={365}
              value={coverageDays}
              onChange={(e) => setCoverageDays(e.target.value)}
              className="w-40"
              placeholder="예: 30"
            />
          </label>
          <Button
            disabled={!velocityDays || !coverageDays}
            onClick={() =>
              setApplied({
                v: Number(velocityDays),
                c: Number(coverageDays),
              })
            }
          >
            계산
          </Button>
        </div>
      </Section>

      {applied === null ? (
        <Section>
          <p className="text-sm text-[--color-muted-foreground]">
            기간을 정하면 재주문 후보를 계산합니다. 기본값을 정해 두지 않는 것은
            의도된 것입니다 — 적정 재고 수준은 사업마다 다릅니다.
          </p>
        </Section>
      ) : reorder.isLoading ? (
        <Section>
          <p className="text-sm text-[--color-muted-foreground]">계산 중…</p>
        </Section>
      ) : (
        <>
          {/* ── 발주 후보 ─────────────────────────────────────── */}
          <Section
            title={`발주 후보 ${reorder.data?.summary.candidate_count ?? 0}건`}
            actions={
              (reorder.data?.summary.overdue_count ?? 0) > 0 ? (
                <Badge variant="danger">
                  기한 경과 {reorder.data!.summary.overdue_count}건
                </Badge>
              ) : null
            }
          >
            {(reorder.data?.groups?.length ?? 0) === 0 ? (
              <p className="text-sm text-[--color-muted-foreground]">
                지금 발주할 상품이 없습니다.
              </p>
            ) : (
              <div className="space-y-4">
                {reorder.data!.groups!.map((g) => (
                  <div
                    key={g.supplier_site_id}
                    className="rounded-lg border border-[--color-border]"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[--color-border] px-3 py-2">
                      <span className="text-sm font-medium">
                        {g.supplier_name ?? "공급처 미상"}
                      </span>
                      <span className="flex items-center gap-3">
                        <span className="text-sm tabular-nums">
                          {g.lines.length}품목 · {formatKRW(g.total_amount)}
                        </span>
                        <DraftButton group={g} onStart={setDraftFrom} />
                      </span>
                    </div>
                    <div className="divide-y divide-[--color-border]">
                      {g.lines.map((c) => (
                        <div
                          key={`${c.product_id}:${c.product_variant_id ?? ""}`}
                          className="px-3 py-2 text-sm"
                        >
                          <div className="flex items-start justify-between gap-3">
                            <span className="min-w-0 flex-1 truncate">
                              {c.product_name}
                            </span>
                            <span className="shrink-0 tabular-nums">
                              {c.recommended_quantity}개 ·{" "}
                              {formatKRW(c.estimated_amount)}
                            </span>
                          </div>
                          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-[--color-muted-foreground]">
                            {c.overdue && (
                              <Badge variant="danger" className="gap-1">
                                <TriangleAlert className="size-3" />
                                기한 경과
                              </Badge>
                            )}
                            <span>재고 {c.on_hand}개</span>
                            {c.inbound > 0 && <span>입고예정 {c.inbound}개</span>}
                            <span>{c.daily_velocity}개/일</span>
                            <span>{c.stockout_date} 소진</span>
                            <span>발주 기한 {c.reorder_by_date}</span>
                            {/* 단가 출처를 밝힌다 — 금액이 이상하면 여기를 본다 */}
                            <span>단가 출처: {PRICE_BASIS_LABEL[c.price_basis]}</span>
                            {c.moq > 1 && <span>MOQ {c.moq}</span>}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Section>

          {/* ── 추천할 수 없는 것들 — 숨기지 않는다 ─────────────── */}
          {(reorder.data?.blocked.length ?? 0) > 0 && (
            <Section title={`추천 불가 ${reorder.data!.blocked.length}건`}>
              <p className="mb-3 flex items-start gap-2 text-sm text-[--color-muted-foreground]">
                <AlertCircle className="mt-0.5 size-4 shrink-0" />
                <span>
                  아래 상품은 데이터가 모자라 계산하지 못했습니다.{" "}
                  <strong>여유가 있다는 뜻이 아닙니다.</strong>
                </span>
              </p>
              <ul className="space-y-1">
                {reorder.data!.blocked.map((b) => (
                  <li
                    key={b.product_id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-[--color-border] px-3 py-2 text-sm"
                  >
                    <span className="min-w-0 flex-1 truncate">{b.product_name}</span>
                    <span className="text-xs text-[--color-muted-foreground]">
                      {b.block_message}
                    </span>
                    <Badge variant="info">
                      {BLOCK_ACTION[b.block_reason as BlockReason]}
                    </Badge>
                  </li>
                ))}
              </ul>
            </Section>
          )}
        </>
      )}

      {/* ── 재고 현황 ────────────────────────────────────────── */}
      <Section title={`재고 현황 ${items.length}건`}>
        {/* 예약 수량을 0 으로 그리지 않는다. 우리는 미출고 주문을 모른다. */}
        {stock.data && (
          <p className="mb-3 text-xs text-[--color-muted-foreground]">
            {stock.data.reserved_note}
          </p>
        )}
        {items.length === 0 ? (
          <p className="flex items-center gap-2 text-sm text-[--color-muted-foreground]">
            <PackageX className="size-4" />
            재고 기록이 없습니다. 기초 재고를 입력하면 여기에 표시됩니다.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-[--color-muted-foreground]">
                <tr className="border-b border-[--color-border]">
                  <th className="py-2 pr-3 font-medium">상품</th>
                  <th className="py-2 pr-3 text-right font-medium">현재고</th>
                  <th className="py-2 pr-3 text-right font-medium">입고예정</th>
                  <th className="py-2 text-right font-medium">최근 변동</th>
                </tr>
              </thead>
              <tbody>
                {items.map((i) => {
                  const key = `${i.product_id}:${i.product_variant_id ?? ""}`;
                  return (
                  <React.Fragment key={key}>
                  <tr
                    onClick={() => setOpenKey(openKey === key ? null : key)}
                    className="cursor-pointer border-b border-[--color-border] last:border-0 hover:bg-[--color-muted]"
                  >
                    <td className="max-w-0 truncate py-2 pr-3">
                      {i.product_name}
                      {i.variant_key && (
                        <span className="ml-1 text-xs text-[--color-muted-foreground]">
                          {i.variant_key}
                        </span>
                      )}
                    </td>
                    <td
                      className={`py-2 pr-3 text-right tabular-nums ${
                        i.negative ? "text-[--color-destructive]" : ""
                      }`}
                    >
                      {i.on_hand}
                    </td>
                    <td className="py-2 pr-3 text-right tabular-nums text-[--color-muted-foreground]">
                      {i.inbound || "—"}
                    </td>
                    <td className="py-2 text-right text-xs text-[--color-muted-foreground]">
                      {i.last_movement_at?.slice(0, 10) ?? "—"}
                    </td>
                  </tr>
                  {openKey === key && (
                    <tr>
                      <td colSpan={4} className="pb-3">
                        <InventoryDetail item={i} />
                      </td>
                    </tr>
                  )}
                  </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </PageShell>
  );
}


/**
 * 재주문 추천 → 발주 초안.
 *
 * 이미 처리 안 된 초안이 있으면 새로 만들지 않는다
 * -------------------------------------------
 * 같은 근거로 초안이 쌓이면 중복 발주가 된다. 대신 **기존 초안으로 보내준다** —
 * "이미 있습니다" 만 말하고 어디 있는지 안 알려주면 사용자가 찾아 헤맨다.
 */
function DraftButton({
  group,
  onStart,
}: {
  group: ReorderGroup;
  onStart: (g: ReorderGroup) => void;
}) {
  const { data } = useQuery({
    queryKey: ["purchase-orders", "open-draft", group.supplier_site_id],
    queryFn: () => purchaseOrderApi.list(["DRAFT"]),
  });

  const existing = (data?.items ?? []).find(
    (o) => o.supplier_name === group.supplier_name,
  );

  if (existing) {
    return (
      <Link
        href={"/purchase-orders" as Route}
        className="text-sm text-[--color-primary] hover:underline"
      >
        기존 초안 {existing.business_key} 보기
      </Link>
    );
  }

  return (
    <Button size="sm" variant="secondary" onClick={() => onStart(group)}>
      발주 초안 만들기
    </Button>
  );
}
