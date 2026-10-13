"use client";

/**
 * 판매처별 가격 패널 — 상품 drawer 의 "판매처 정보" 탭.
 *
 * 세 가지 상태를 **분리해서** 보여준다.
 *   승인      : approval_request 가 진실
 *   내부 적용 : 가격 버전의 유효기간이 진실
 *   외부 반영 : publish_status 가 진실
 *
 * 합쳐 보여주면 "승인은 났는데 판매처 반영이 실패한" 상태를 표현할 수 없다.
 * 그게 사용자가 가장 알아야 하는 상태다.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Info, Loader2, Plus, Send } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Field } from "@/components/shell/page-shell";
import { MoneyInput, TextInput } from "@/components/ui/money-input";
import {
  channelPricesApi, canPublish, formatPriceCell, internalPhaseOf,
  publishLabel, INTERNAL_PHASE_LABEL, NOT_ENTERED,
  type ChannelListing, type ChannelPrice, type ChannelPriceProposal,
  type PricePreview,
} from "@/lib/api/channel-prices";
import { approvalsApi, describeApprovalError } from "@/lib/api/approvals";
import { ListingCreateDialog } from "@/components/products/listing-create-dialog";
import { formatDateTime } from "@/lib/format";

const LISTING_STATUS_LABEL: Record<string, string> = {
  DRAFT: "등록 전", ACTIVE: "판매 중", PAUSED: "일시중지", ENDED: "종료",
};

export function ChannelPricePanel({
  productId,
  variantOptions,
}: {
  productId: string;
  /** 옵션별 가격을 매길 수 있게 variant 목록을 받는다 */
  variantOptions: Array<{ id: string; label: string }>;
}) {
  const qc = useQueryClient();
  const [openForm, setOpenForm] = React.useState<string | null>(null);
  const [connectOpen, setConnectOpen] = React.useState(false);

  const { data: listings, isLoading } = useQuery({
    queryKey: ["channelListings", productId],
    queryFn: () => channelPricesApi.listForProduct(productId),
  });

  if (isLoading) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-16 w-full" />
      </div>
    );
  }

  const items = listings?.items ?? [];

  if (items.length === 0) {
    return (
      <>
        <div className="rounded-md border border-dashed border-[--color-border] px-4 py-8 text-center">
          <p className="text-xs text-[--color-muted-foreground]">
            연결된 판매처 상품이 없습니다.
          </p>
          <p className="mt-1 text-[11px] text-[--color-muted-foreground]">
            판매처 상품을 연결하면 판매처별 가격과 가격 이력을 관리할 수 있습니다.
          </p>
          <Button variant="outline" size="sm" className="mt-3"
                  onClick={() => setConnectOpen(true)}>
            <Plus className="mr-1 h-3.5 w-3.5" aria-hidden />
            판매처 상품 연결
          </Button>
        </div>
        <ListingCreateDialog
          productId={productId} open={connectOpen} onOpenChange={setConnectOpen}
        />
      </>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Button variant="outline" size="sm" onClick={() => setConnectOpen(true)}>
          <Plus className="mr-1 h-3.5 w-3.5" aria-hidden />
          판매처 상품 연결
        </Button>
      </div>
      <ListingCreateDialog
        productId={productId} open={connectOpen} onOpenChange={setConnectOpen}
      />
      {items.map((listing) => (
        <ListingCard
          key={listing.id}
          listing={listing}
          variantOptions={variantOptions}
          formOpen={openForm === listing.id}
          onToggleForm={() => setOpenForm(openForm === listing.id ? null : listing.id)}
          onDone={() => {
            setOpenForm(null);
            qc.invalidateQueries({ queryKey: ["channelPrices", listing.id] });
            qc.invalidateQueries({ queryKey: ["approvals"] });
          }}
        />
      ))}
    </div>
  );
}

function ListingCard({
  listing, variantOptions, formOpen, onToggleForm, onDone,
}: {
  listing: ChannelListing;
  variantOptions: Array<{ id: string; label: string }>;
  formOpen: boolean;
  onToggleForm: () => void;
  onDone: () => void;
}) {
  const [variantId, setVariantId] = React.useState<string>("");

  const { data: prices, isLoading } = useQuery({
    queryKey: ["channelPrices", listing.id],
    queryFn: () => channelPricesApi.prices(listing.id),
  });
  const { data: resolved } = useQuery({
    queryKey: ["resolvedPrice", listing.id, variantId],
    queryFn: () => channelPricesApi.resolved(listing.id,
      variantId ? { variant_id: variantId } : {}),
  });

  const todayKst = (prices as { meta?: { today_kst?: string } } | undefined)?.meta?.today_kst
    ?? new Date(Date.now() + 9 * 3600_000).toISOString().slice(0, 10);
  const rows = prices?.items ?? [];

  return (
    <div className="rounded-md border border-[--color-border]">
      {/* ── 헤더: 판매처 · 계정 · 외부 상품번호 ── */}
      <div className="flex flex-wrap items-start justify-between gap-2 border-b border-[--color-border] bg-[--color-muted] px-3 py-2">
        <div className="min-w-0 text-xs">
          <p className="flex flex-wrap items-center gap-1.5 font-medium">
            <span>{listing.marketplace ?? "판매처"}</span>
            <span className="text-[--color-muted-foreground]">·</span>
            <span>{listing.account_label ?? "계정"}</span>
            <Badge variant="default" className="px-1.5 py-0 text-[10px]">
              {LISTING_STATUS_LABEL[listing.status] ?? listing.status}
            </Badge>
          </p>
          <p className="mt-0.5 text-[11px] text-[--color-muted-foreground]">
            외부 상품번호{" "}
            {listing.external_product_id ? (
              listing.external_product_url ? (
                <a
                  href={listing.external_product_url}
                  target="_blank" rel="noopener noreferrer"
                  className="ops-focusable font-mono underline underline-offset-2"
                >
                  {listing.external_product_id}
                </a>
              ) : (
                <span className="font-mono">{listing.external_product_id}</span>
              )
            ) : (
              <span>{NOT_ENTERED}</span>
            )}
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={onToggleForm}>
          <Plus className="mr-1 h-3.5 w-3.5" aria-hidden />
          가격 변경 요청
        </Button>
      </div>

      {/* ── 현재 적용가 ── */}
      <div className="grid gap-3 px-3 py-3 sm:grid-cols-2">
        <div>
          <p className="text-[11px] text-[--color-muted-foreground]">현재 적용가</p>
          <p className="mt-0.5 text-sm font-medium">
            {formatPriceCell(resolved?.resolved_price)}
            {resolved?.on_sale && (
              <Badge variant="warning" className="ml-1.5 px-1.5 py-0 text-[10px]">
                할인 중
              </Badge>
            )}
          </p>
          {resolved?.variant_fallback && (
            // 옵션 가격이 없어서 기본가를 쓴다는 걸 알려야 한다 —
            // 모르면 "옵션 가격을 넣었는데 왜 안 바뀌지" 를 한참 찾는다.
            <p className="mt-0.5 text-[11px] text-[--color-muted-foreground]">
              기본 가격 사용 (이 옵션의 별도 가격 없음)
            </p>
          )}
          <p className="mt-0.5 text-[11px] text-[--color-muted-foreground]">
            정상가 {formatPriceCell(resolved?.regular_price)}
            {resolved?.sale_price != null && (
              <> · 할인가 {formatPriceCell(resolved.sale_price)}</>
            )}
          </p>
        </div>

        {variantOptions.length > 0 && (
          <Field label="옵션별 가격 확인" htmlFor={`vsel-${listing.id}`}>
            <select
              id={`vsel-${listing.id}`}
              value={variantId}
              onChange={(e) => setVariantId(e.target.value)}
              className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
            >
              <option value="">상품 기본 가격</option>
              {variantOptions.map((v) => (
                <option key={v.id} value={v.id}>{v.label}</option>
              ))}
            </select>
          </Field>
        )}
      </div>

      {formOpen && (
        <PriceRequestForm
          listing={listing}
          variantOptions={variantOptions}
          onDone={onDone}
        />
      )}

      {/* ── 가격 이력 ── */}
      <div className="border-t border-[--color-border] px-3 py-2">
        <p className="mb-1.5 text-[11px] font-medium">가격 이력</p>
        {isLoading ? (
          <Skeleton className="h-12 w-full" />
        ) : rows.length === 0 ? (
          <p className="text-[11px] text-[--color-muted-foreground]">
            아직 등록된 가격이 없습니다. 가격 변경 요청으로 등록하세요.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-[11px]">
              <thead className="text-[--color-muted-foreground]">
                <tr className="border-b border-[--color-border]">
                  <th className="py-1 text-left font-medium">적용 기간</th>
                  <th className="py-1 text-left font-medium">옵션</th>
                  <th className="py-1 text-right font-medium">정상가</th>
                  <th className="py-1 text-right font-medium">할인가</th>
                  <th className="py-1 text-left font-medium">내부 적용</th>
                  <th className="py-1 text-left font-medium">판매처 반영</th>
                  <th className="py-1" />
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <PriceRow
                    key={p.id} price={p} listing={listing} todayKst={todayKst}
                    variantOptions={variantOptions} onDone={onDone}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function PriceRow({
  price, listing, todayKst, variantOptions, onDone,
}: {
  price: ChannelPrice;
  listing: ChannelListing;
  todayKst: string;
  variantOptions: Array<{ id: string; label: string }>;
  onDone: () => void;
}) {
  const phase = internalPhaseOf(price, todayKst);
  const publish = canPublish(listing, price);
  const variantLabel = price.product_variant_id
    ? variantOptions.find((v) => v.id === price.product_variant_id)?.label ?? "옵션"
    : "기본";

  const send = useMutation({
    mutationFn: () => channelPricesApi.publish(price.id),
    onSuccess: () => {
      toast.success("판매처 반영을 요청했습니다");
      onDone();
    },
    onError: (err) => {
      const guide = describeApprovalError(err);
      toast.error(guide.title, { description: guide.description });
    },
  });

  return (
    <tr className="border-b border-[--color-border] last:border-0">
      <td className="py-1.5">
        {price.effective_from} ~ {price.effective_to ?? "무기한"}
      </td>
      <td className="py-1.5">{variantLabel}</td>
      <td className="py-1.5 text-right">{formatPriceCell(price.regular_price)}</td>
      <td className="py-1.5 text-right">
        {formatPriceCell(price.sale_price)}
        {price.sale_start_at && (
          <span className="block text-[10px] text-[--color-muted-foreground]">
            {price.sale_start_at.slice(0, 10)} ~ {price.sale_end_at?.slice(0, 10) ?? "무기한"}
          </span>
        )}
      </td>
      <td className="py-1.5">
        <Badge
          variant={phase === "ACTIVE" ? "success" : phase === "SCHEDULED" ? "info" : "default"}
          className="px-1.5 py-0 text-[10px]"
        >
          {INTERNAL_PHASE_LABEL[phase]}
        </Badge>
      </td>
      <td className="py-1.5">
        <span
          className={
            price.publish_status === "FAILED" ? "text-[--color-danger]" : undefined
          }
        >
          {publishLabel(price.publish_status, listing.price_publish_supported)}
        </span>
        {price.published_at && (
          <span className="block text-[10px] text-[--color-muted-foreground]">
            {formatDateTime(price.published_at)}
          </span>
        )}
        {price.publish_error && (
          <span className="block text-[10px] text-[--color-danger]">
            {price.publish_error}
          </span>
        )}
      </td>
      <td className="py-1.5 text-right">
        {/* 과거 가격에는 수정·반영 버튼을 주지 않는다.
            정정은 새 버전 생성이지 과거 수정이 아니다. */}
        {phase !== "EXPIRED" && (
          <Button
            variant="ghost" size="sm"
            title={publish.reason ?? "판매처에 반영"}
            disabled={!publish.enabled || send.isPending}
            onClick={() => send.mutate()}
          >
            {send.isPending
              ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
              : <Send className="h-3.5 w-3.5" aria-hidden />}
            <span className="sr-only">판매처에 반영</span>
          </Button>
        )}
      </td>
    </tr>
  );
}

/**
 * 가격 변경 요청 폼.
 *
 * **가격을 직접 저장하지 않는다.** 승인 요청을 만든다 — 직접 저장 경로를 열어두면
 * 승인 워크플로가 장식이 된다.
 */
function PriceRequestForm({
  listing, variantOptions, onDone,
}: {
  listing: ChannelListing;
  variantOptions: Array<{ id: string; label: string }>;
  onDone: () => void;
}) {
  const [variantId, setVariantId] = React.useState("");
  const [regular, setRegular] = React.useState<number | null>(null);
  const [sale, setSale] = React.useState<number | null>(null);
  const [saleStart, setSaleStart] = React.useState("");
  const [saleEnd, setSaleEnd] = React.useState("");
  const [from, setFrom] = React.useState("");
  const [reason, setReason] = React.useState("");

  const submit = useMutation({
    mutationFn: () => {
      const proposal: ChannelPriceProposal = {
        effective_from: from,
        // 빈 값은 **null(미입력)** 로 보낸다. 0 으로 보내면 서버가 거부하고,
        // 거부하지 않았다면 "공짜로 판다" 가 저장된다.
        regular_price: regular ?? null,
        sale_price: sale ?? null,
        sale_start_at: sale != null && saleStart ? `${saleStart}T00:00:00+09:00` : null,
        sale_end_at: sale != null && saleEnd ? `${saleEnd}T00:00:00+09:00` : null,
        ...(variantId ? { product_variant_id: variantId } : {}),
      };
      return approvalsApi.create({
        target_type: "product_channel_listing",
        target_id: listing.id,
        request_type: "CHANNEL_PRICE_CHANGE",
        proposed_changes: proposal as unknown as Record<string, unknown>,
        reason: reason.trim() || undefined,
        submit: true,
      });
    },
    onSuccess: () => {
      toast.success("가격 변경 승인 요청을 올렸습니다", {
        description: "승인이 완료되어야 판매가에 반영됩니다.",
      });
      onDone();
    },
    onError: (err) => {
      const guide = describeApprovalError(err);
      toast.error(guide.title, { description: guide.description });
    },
  });

  const canSubmit = !!from && !submit.isPending;

  /**
   * 마진 영향 미리보기. **서버가 기존 계산 엔진으로 계산한다.**
   * 화면에서 새 공식을 만들지 않는다 — 보여준 마진과 실제 마진이 갈라진다.
   */
  const proposal: ChannelPriceProposal | null = from
    ? {
        effective_from: from,
        regular_price: regular ?? null,
        sale_price: sale ?? null,
        sale_start_at: sale != null && saleStart ? `${saleStart}T00:00:00+09:00` : null,
        sale_end_at: sale != null && saleEnd ? `${saleEnd}T00:00:00+09:00` : null,
        ...(variantId ? { product_variant_id: variantId } : {}),
      }
    : null;

  const { data: preview } = useQuery({
    queryKey: ["pricePreview", listing.id, JSON.stringify(proposal)],
    queryFn: () => channelPricesApi.preview(listing.id, proposal!),
    // 시작일과 금액이 모두 있어야 의미가 있다. 없는 값으로 부르면 422 가 쌓인다.
    enabled: !!proposal && (regular != null || sale != null),
    retry: false,
  });

  return (
    <div className="space-y-3 border-t border-[--color-border] bg-[--color-muted] px-3 py-3">
      <p className="flex items-start gap-1.5 text-[11px] text-[--color-muted-foreground]">
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
        <span>
          가격 변경은 승인을 거쳐야 반영됩니다. 승인 전까지 현재 가격은 그대로입니다.
          비워 두면 <strong>미입력</strong>으로 저장되며 0원이 아닙니다.
        </span>
      </p>

      <div className="grid gap-3 sm:grid-cols-2">
        {variantOptions.length > 0 && (
          <Field label="적용 대상" htmlFor="cp-variant">
            <select
              id="cp-variant" value={variantId}
              onChange={(e) => setVariantId(e.target.value)}
              className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
            >
              <option value="">상품 기본 가격</option>
              {variantOptions.map((v) => (
                <option key={v.id} value={v.id}>{v.label}</option>
              ))}
            </select>
          </Field>
        )}
        <Field label="적용 시작일" required htmlFor="cp-from">
          <input
            id="cp-from" type="date" value={from}
            onChange={(e) => setFrom(e.target.value)}
            className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
          />
        </Field>
        <Field label="정상가" htmlFor="cp-regular">
          <MoneyInput
            id="cp-regular" value={regular ?? 0}
            onChange={(v) => setRegular(v || null)}
          />
        </Field>
        <Field label="할인가" htmlFor="cp-sale">
          <MoneyInput
            id="cp-sale" value={sale ?? 0}
            onChange={(v) => setSale(v || null)}
          />
        </Field>
        {sale != null && (
          <>
            <Field label="할인 시작일" required htmlFor="cp-sale-start">
              <input
                id="cp-sale-start" type="date" value={saleStart}
                onChange={(e) => setSaleStart(e.target.value)}
                className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
              />
            </Field>
            <Field label="할인 종료일 (비우면 무기한)" htmlFor="cp-sale-end">
              <input
                id="cp-sale-end" type="date" value={saleEnd}
                onChange={(e) => setSaleEnd(e.target.value)}
                className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
              />
            </Field>
          </>
        )}
        <Field label="변경 사유" htmlFor="cp-reason" className="sm:col-span-2">
          <TextInput
            id="cp-reason" value={reason} placeholder="예: 경쟁사 가격 대응"
            onChange={(e) => setReason(e.target.value)}
          />
        </Field>
      </div>

      {preview && <MarginPreview preview={preview} />}

      <div className="flex justify-end">
        <Button size="sm" disabled={!canSubmit} onClick={() => submit.mutate()}>
          {submit.isPending && (
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
          )}
          승인 요청
        </Button>
      </div>
    </div>
  );
}


/**
 * 마진 영향 표시.
 *
 * **판단 보조값이다.** 승인 여부를 막지 않는다 — 목표 미달이어도 승인은 가능하고,
 * 다만 검토가 필요하다고 알린다. 화면이 업무 판단을 대신하면 안 된다.
 */
function MarginPreview({ preview }: { preview: PricePreview }) {
  if (!preview.sensitive_visible) {
    return (
      <div className="rounded-md border border-[--color-border] bg-[--color-card] px-3 py-2 text-[11px] text-[--color-muted-foreground]">
        {preview.redacted_message ?? "권한이 없어 마진 정보를 확인할 수 없습니다."}
      </div>
    );
  }

  const before = preview.before;
  const after = preview.after;
  const gap = preview.margin_gap_pp ? Number(preview.margin_gap_pp) : null;

  return (
    <div className="space-y-2 rounded-md border border-[--color-border] bg-[--color-card] px-3 py-2 text-[11px]">
      <p className="font-medium">마진 영향</p>

      {after?.unavailable_reason ? (
        <p className="text-[--color-muted-foreground]">{after.unavailable_reason}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[320px]">
            <thead className="text-[--color-muted-foreground]">
              <tr>
                <th className="py-0.5 text-left font-medium" />
                <th className="py-0.5 text-right font-medium">현재</th>
                <th className="py-0.5 text-right font-medium">변경 후</th>
              </tr>
            </thead>
            <tbody>
              <PreviewRow label="판매가"
                before={before?.sale_price} after={after?.sale_price} />
              <PreviewRow label="예상 정산액"
                before={before?.expected_settlement} after={after?.expected_settlement} />
              <PreviewRow label="예상 순이익"
                before={before?.expected_net_profit} after={after?.expected_net_profit} />
              <tr>
                <td className="py-0.5">예상 마진율</td>
                <td className="py-0.5 text-right">{formatRate(before?.margin_rate)}</td>
                <td className="py-0.5 text-right">{formatRate(after?.margin_rate)}</td>
              </tr>
            </tbody>
          </table>
        </div>
      )}

      {preview.target_margin_rate && gap !== null && (
        <p className={gap < 0 ? "text-[--color-warning]" : "text-[--color-muted-foreground]"}>
          {gap < 0
            ? `변경 가격은 목표 마진율보다 ${Math.abs(gap).toFixed(1)}%p 낮습니다. ` +
              "승인은 가능하지만 검토가 필요합니다."
            : `목표 마진율 ${formatRate(preview.target_margin_rate)} 충족`}
        </p>
      )}

      {!preview.policies_verified && (
        <p className="text-[--color-muted-foreground]">
          검증되지 않은 수수료 또는 세금 정책이 사용되었습니다. 결과는 예상값입니다.
        </p>
      )}
    </div>
  );
}

function PreviewRow({ label, before, after }: {
  label: string; before: number | null | undefined; after: number | null | undefined;
}) {
  return (
    <tr>
      <td className="py-0.5">{label}</td>
      <td className="py-0.5 text-right">{formatPriceCell(before)}</td>
      <td className="py-0.5 text-right font-medium">{formatPriceCell(after)}</td>
    </tr>
  );
}

/** 마진율 문자열(0.187) → "18.7%". null 은 미입력. */
function formatRate(value: string | null | undefined): string {
  if (value === null || value === undefined) return NOT_ENTERED;
  return `${(Number(value) * 100).toFixed(1)}%`;
}
