"use client";

/**
 * 판매처 상품 연결 생성.
 *
 * connector 지원 여부는 **서버가 알려준다.** 화면이 판매처 이름으로 추측하면
 * connector 가 생겼을 때 이 화면을 찾아 고쳐야 하고, 빠뜨린 화면이 남는다.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Info, Loader2 } from "lucide-react";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Field } from "@/components/shell/page-shell";
import { TextInput } from "@/components/ui/money-input";
import { channelPricesApi, type ListingStatus } from "@/lib/api/channel-prices";
import { integrationsApi } from "@/lib/api/integrations";
import { describeApprovalError } from "@/lib/api/approvals";

const STATUS_OPTIONS: Array<{ value: ListingStatus; label: string }> = [
  { value: "DRAFT", label: "등록 전" },
  { value: "ACTIVE", label: "판매 중" },
  { value: "PAUSED", label: "일시중지" },
];

/** http/https 만 허용. 빈 값은 통과(선택 입력). */
function isValidUrl(value: string): boolean {
  if (!value.trim()) return true;
  try {
    const u = new URL(value);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

export function ListingCreateDialog({
  productId, open, onOpenChange,
}: {
  productId: string;
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const qc = useQueryClient();
  const [accountId, setAccountId] = React.useState("");
  const [externalId, setExternalId] = React.useState("");
  const [url, setUrl] = React.useState("");
  const [status, setStatus] = React.useState<ListingStatus>("DRAFT");
  const [autoPublish, setAutoPublish] = React.useState(false);

  const { data: accounts } = useQuery({
    queryKey: ["integrationAccounts"],
    queryFn: () => integrationsApi.list(),
    enabled: open,
  });
  const { data: support } = useQuery({
    queryKey: ["connectorSupport"],
    queryFn: () => channelPricesApi.connectorSupport(),
    enabled: open,
  });

  // 비활성 계정은 목록에 남기되 선택할 수 없게 한다 —
  // 아예 숨기면 "내 계정이 왜 안 보이지" 를 찾느라 헤맨다.
  const options = accounts ?? [];
  const selected = options.find((a) => a.id === accountId);
  const marketplace = selected?.sale_channel_code ?? selected?.provider ?? null;
  const connector = support?.find((s) => s.marketplace === marketplace?.toUpperCase());
  const publishSupported = connector?.price_publish_supported ?? false;

  // 계정을 바꿔서 미지원 판매처가 되면 자동 반영을 꺼야 한다.
  // 안 그러면 켜진 채로 제출돼 서버가 403 을 낸다.
  React.useEffect(() => {
    if (!publishSupported && autoPublish) setAutoPublish(false);
  }, [publishSupported, autoPublish]);

  const urlInvalid = !isValidUrl(url);

  const create = useMutation({
    mutationFn: () =>
      channelPricesApi.create(productId, {
        integration_account_id: accountId,
        external_product_id: externalId.trim() || null,
        external_product_url: url.trim() || null,
        status,
        auto_publish_price: autoPublish,
      }),
    onSuccess: () => {
      toast.success("판매처 상품을 연결했습니다");
      qc.invalidateQueries({ queryKey: ["channelListings", productId] });
      reset();
      onOpenChange(false);
    },
    onError: (err) => {
      // 409 를 그대로 보여주지 않는다 — 무엇이 중복인지 알려줘야 한다.
      const guide = describeApprovalError(err);
      toast.error(guide.title, { description: guide.description });
    },
  });

  function reset() {
    setAccountId(""); setExternalId(""); setUrl("");
    setStatus("DRAFT"); setAutoPublish(false);
  }

  const canSubmit = !!accountId && !urlInvalid && !create.isPending
    && selected?.status === "ACTIVE";

  return (
    <AlertDialog
      open={open}
      onOpenChange={(v) => { if (!v) reset(); onOpenChange(v); }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>판매처 상품 연결</AlertDialogTitle>
          <AlertDialogDescription>
            판매처 계정에 이 상품을 연결하면 판매처별 가격과 가격 이력을 관리할 수 있습니다.
          </AlertDialogDescription>
        </AlertDialogHeader>

        <div className="space-y-3">
          <Field label="판매처 계정" required htmlFor="lc-account">
            <select
              id="lc-account" value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
            >
              <option value="">선택하세요</option>
              {options.map((a) => (
                <option key={a.id} value={a.id} disabled={a.status !== "ACTIVE"}>
                  {(a.sale_channel_code ?? a.provider)} · {a.account_label}
                  {a.status !== "ACTIVE" ? " (비활성)" : ""}
                </option>
              ))}
            </select>
          </Field>

          {selected && selected.status !== "ACTIVE" && (
            <p role="alert" className="text-[11px] text-[--color-danger]">
              비활성 계정에는 상품을 연결할 수 없습니다. 설정에서 계정을 먼저 활성화하세요.
            </p>
          )}

          <Field label="외부 상품번호" htmlFor="lc-ext">
            <TextInput
              id="lc-ext" value={externalId} placeholder="판매처가 발급한 번호"
              onChange={(e) => setExternalId(e.target.value)}
            />
          </Field>

          <Field label="외부 상품 URL" htmlFor="lc-url">
            <TextInput
              id="lc-url" value={url} placeholder="https://…"
              aria-invalid={urlInvalid}
              aria-describedby={urlInvalid ? "lc-url-err" : undefined}
              onChange={(e) => setUrl(e.target.value)}
            />
          </Field>
          {urlInvalid && (
            <p id="lc-url-err" role="alert" className="text-[11px] text-[--color-danger]">
              http 또는 https 로 시작하는 주소를 입력하세요.
            </p>
          )}

          <Field label="상태" htmlFor="lc-status">
            <select
              id="lc-status" value={status}
              onChange={(e) => setStatus(e.target.value as ListingStatus)}
              className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
            >
              {STATUS_OPTIONS.map((s) => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
          </Field>

          <div className="rounded-md border border-[--color-border] px-3 py-2">
            <label className="flex items-start gap-2 text-xs">
              <input
                type="checkbox" checked={autoPublish}
                disabled={!publishSupported}
                onChange={(e) => setAutoPublish(e.target.checked)}
                className="mt-0.5"
              />
              <span>
                <span className={publishSupported ? "" : "text-[--color-muted-foreground]"}>
                  가격 자동 반영
                </span>
                <span className="block text-[11px] text-[--color-muted-foreground]">
                  승인된 가격을 판매처에 자동으로 전송합니다.
                </span>
              </span>
            </label>

            {accountId && !publishSupported && (
              <p className="mt-2 flex items-start gap-1.5 text-[11px] text-[--color-muted-foreground]">
                <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                <span>
                  이 판매처는 가격 변경 API가 아직 연결되지 않았습니다.
                  내부 가격과 승인 이력은 관리할 수 있지만 자동 반영은 사용할 수 없습니다.
                </span>
              </p>
            )}
          </div>
        </div>

        <AlertDialogFooter>
          <AlertDialogCancel>취소</AlertDialogCancel>
          <AlertDialogAction
            disabled={!canSubmit}
            onClick={(e) => { e.preventDefault(); create.mutate(); }}
          >
            {create.isPending && (
              <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
            )}
            연결
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
