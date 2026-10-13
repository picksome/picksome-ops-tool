"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ExternalLink, Info, Loader2, PackageOpen, Settings2, ShieldCheck, X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle,
} from "@/components/ui/sheet";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Field, PageTabs } from "@/components/shell/page-shell";
import { MoneyInput, PercentInput, TextInput } from "@/components/ui/money-input";
import { CalcBreakdown, CalcHelpButton, CalcSummary, TargetPrices } from "@/components/calc/calc-breakdown";
import {
  productAdapter, toBeProductFields, MOCK_CATEGORIES,
} from "@/lib/adapters";
import { useFeePolicies, useTaxRules } from "@/hooks/usePolicies";
import { ROLE_LABELS, usersApi } from "@/lib/api/admin";
import { classifyProductChanges, diffAgainst } from "@/lib/api/products";
import { ChannelPricePanel } from "@/components/products/channel-price-panel";
import { ProductSourcingPanel } from "@/components/products/product-sourcing-panel";
import {
  describeApprovalError, PRODUCT_FIELD_LABELS, REQUEST_TYPE_LABELS,
} from "@/lib/api/approvals";
import type { ProductDetail, ProductStatus } from "@/lib/adapters/types";
import { calculateMargin, resolveFeePolicy, resolveTaxRule } from "@/lib/calc/engine";
import type { MarginCalcInput } from "@/lib/calc/types";
import { formatDateTime, formatPercent } from "@/lib/format";
import { todayKst } from "@/lib/date-range";

const STATUS_LABEL: Record<ProductStatus, string> = {
  CANDIDATE: "소싱검토", APPROVED: "승인", ACTIVE: "판매중",
  PAUSED: "일시중지", DISCONTINUED: "단종",
};

const STATUS_TONE: Record<ProductStatus, "info" | "success" | "warning" | "default"> = {
  CANDIDATE: "default", APPROVED: "info", ACTIVE: "success",
  PAUSED: "warning", DISCONTINUED: "default",
};

const TABS = [
  { key: "basic", label: "기본 정보" },
  { key: "sourcing", label: "소싱" },
  { key: "pricing", label: "가격·마진" },
  { key: "channels", label: "판매처 정보" },
  { key: "options", label: "옵션" },
  { key: "history", label: "변경 기록" },
];

/** URL 형식 검증 — http/https 만 허용. */
function isValidUrl(value: string): boolean {
  if (!value.trim()) return true;
  try {
    const u = new URL(value);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

export function ProductDrawer({
  productId,
  open,
  onOpenChange,
  onOpenOptions,
}: {
  productId: string | null;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onOpenOptions?: (productId: string) => void;
}) {
  const qc = useQueryClient();
  const [tab, setTab] = React.useState("basic");
  const [draft, setDraft] = React.useState<ProductDetail | null>(null);
  const [dirty, setDirty] = React.useState(false);
  const [confirmClose, setConfirmClose] = React.useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["product", productId],
    queryFn: () => productAdapter.detail(productId!),
    enabled: !!productId && open,
  });

  // 실제 정책 API 를 읽는다. 예전에는 `settingsAdapter`(메모리 배열)였고,
  // 여기 뜨던 수수료·부가세는 전부 지어낸 값이었다.
  const { taxRules } = useTaxRules();
  const { feePolicies } = useFeePolicies();
  // 담당자 목록도 실제 구성원이다. mock 배열을 쓰면 존재하지 않는 사람에게
  // 상품을 배정할 수 있고, 저장된 ownerId 는 어떤 계정과도 이어지지 않는다.
  const { data: members } = useQuery({
    queryKey: ["users", "directory"],
    queryFn: () => usersApi.directory(),
    staleTime: 5 * 60 * 1000,
  });

  /**
   * 편집 중에 서버 값이 달라졌다는 사실. **덮지 않고 알리기만 한다.**
   */
  const [serverChanged, setServerChanged] = React.useState(false);

  /**
   * `dirty` 를 아래 effect 의 의존성으로 두지 않기 위한 거울. 의존성에 넣으면
   * 첫 글자를 치는 순간 effect 가 다시 돌아 헛된 안내가 뜬다.
   */
  const dirtyRef = React.useRef(dirty);
  dirtyRef.current = dirty;

  /** 이 draft 를 어느 서버 응답으로 채웠나. */
  const filledFromRef = React.useRef<unknown>(null);

  /**
   * 초기화는 **손대지 않은 상태**에서만 한다.
   *
   * 예전에는 `data` 가 바뀔 때마다 draft 를 통째로 덮었다. React Query 의
   * `refetchOnWindowFocus` 기본값이 true 이고 `staleTime` 이 30초라, 상품을
   * 30초 넘게 편집하다가 창을 다녀오면 **입력이 사라졌다.**
   *
   * 서버 값이 같으면 structural sharing 이 같은 객체를 돌려줘 effect 가 안
   * 돌기 때문에 평소에는 드러나지 않았다 — 다른 사람이 같은 상품을 고쳤을
   * 때만 터진다(expense-drawer-dirty.test.tsx 가 같은 결함을 재현했다).
   */
  React.useEffect(() => {
    if (!data) return;
    if (filledFromRef.current === data) return;   // 같은 응답이다

    if (filledFromRef.current !== null && dirtyRef.current) {
      // 사용자가 손댄 값이 source of truth 다.
      setServerChanged(true);
      return;
    }
    filledFromRef.current = data;
    setDraft(structuredClone(data));
    setDirty(false);
    setServerChanged(false);
  }, [data]);

  // 다른 상품을 열면 새로 채워야 한다.
  React.useEffect(() => {
    filledFromRef.current = null;
    setServerChanged(false);
  }, [productId]);

  React.useEffect(() => {
    if (open) setTab("basic");
  }, [open, productId]);

  /**
   * 어떤 필드가 승인 대상인지는 **서버가 알려준다.** 화면이 표를 들고 있으면
   * 백엔드와 어긋나는 날 사용자가 입력을 다 끝낸 뒤에야 403 을 보게 된다.
   */
  const {
    data: ownership,
    isError: ownershipFailed,
    isFetching: ownershipLoading,
    refetch: refetchOwnership,
  } = useQuery({
    queryKey: ["approvalFieldOwnership"],
    queryFn: () => productAdapter.fieldOwnership(),
    staleTime: 5 * 60_000,
    // 자동 재시도를 끄지 않으면 "다시 불러오기" 버튼이 언제 눌러야 하는 건지
    // 사용자가 알 수 없다. 실패를 즉시 드러내고 재시도는 사람이 결정한다.
    retry: 1,
  });

  /** 지금 draft 의 변경분이 어떻게 나뉘는가. 저장 버튼을 누르기 **전에** 보여준다. */
  const classified = React.useMemo(() => {
    if (!draft || !data || !ownership) return null;
    const changes = diffAgainst(toBeProductFields(data), toBeProductFields(draft));
    return classifyProductChanges(changes, ownership);
  }, [draft, data, ownership]);

  const [approvalReason, setApprovalReason] = React.useState("");
  const [effectiveFrom, setEffectiveFrom] = React.useState("");
  const [confirmApproval, setConfirmApproval] = React.useState(false);

  const save = useMutation({
    mutationFn: () =>
      productAdapter.save(productId, draft ?? {}, {
        base: data ?? null,
        ownership: ownership ?? null,
        reason: approvalReason.trim() || undefined,
        effectiveFrom: effectiveFrom || undefined,
        submit: true,
      }),
    onSuccess: (result) => {
      // 무엇이 **지금 반영됐고** 무엇이 **승인 대기로 갔는지** 구분해서 알려준다.
      // "저장했습니다" 하나로 뭉치면 사용자는 원가까지 바뀐 줄 안다.
      if (result.approvals.length > 0 && result.directSaved) {
        toast.success(
          `일부는 저장하고, ${result.approvals.length}건은 승인 요청으로 올렸습니다`,
          { description: "원가·상품명·상태 변경은 승인 후 반영됩니다." },
        );
      } else if (result.approvals.length > 0) {
        toast.success(`승인 요청 ${result.approvals.length}건을 올렸습니다`, {
          description: "승인이 완료되어야 상품에 반영됩니다.",
        });
      } else {
        toast.success("저장했습니다");
      }
      setDirty(false);
      setApprovalReason("");
      setEffectiveFrom("");
      setConfirmApproval(false);
      qc.invalidateQueries({ queryKey: ["products"] });
      qc.invalidateQueries({ queryKey: ["product", productId] });
      qc.invalidateQueries({ queryKey: ["approvals"] });
      onOpenChange(false);
    },
    onError: (err) => {
      const guide = describeApprovalError(err);
      toast.error(guide.title, { description: guide.description });
      if (guide.shouldRefetch) {
        // 409 계열은 화면이 낡았다는 뜻이다. 다시 불러와야 사용자가 최신값 위에서 판단한다.
        qc.invalidateQueries({ queryKey: ["product", productId] });
      }
    },
  });

  /** 승인 대상 변경이 섞여 있으면 사유를 받고 나서 저장한다. */
  function onSaveClick() {
    if (classified?.hasApproval) {
      setConfirmApproval(true);
      return;
    }
    save.mutate();
  }

  function patch(updater: (d: ProductDetail) => void) {
    setDraft((prev) => {
      if (!prev) return prev;
      const next = structuredClone(prev);
      updater(next);
      return next;
    });
    setDirty(true);
  }

  function requestClose(next: boolean) {
    if (!next && dirty) {
      setConfirmClose(true);
      return;
    }
    onOpenChange(next);
  }

  /* ── 인라인 마진 계산 — 저장 전에도 즉시 반영된다 ── */
  const calcInput: MarginCalcInput | null = React.useMemo(() => {
    if (!draft || !taxRules?.length) return null;
    const at = todayKst();
    const taxRule = resolveTaxRule(taxRules, at) ?? taxRules[0];
    const feePolicy = resolveFeePolicy(feePolicies ?? [], draft.marketplaces[0] ?? "COUPANG", draft.category, at);
    const s = draft.sourcing;
    const p = draft.pricing;
    return {
      cost: {
        purchasePrice: s.purchasePrice,
        purchaseCurrency: s.purchaseCurrency,
        exchangeRate: s.exchangeRate,
        domesticShippingAbroad: s.domesticShippingAbroad,
        internationalShipping: s.internationalShipping,
        customsDuty: s.customsDuty,
        importVat: s.importVat,
        domesticShipping: s.domesticShipping,
        packagingCost: s.packagingCost,
        otherCost: s.otherCost,
      },
      selling: {
        salePrice: p.salePrice,
        buyerShippingFee: p.buyerShippingFee,
        marketplace: draft.marketplaces[0] ?? "COUPANG",
        category: draft.category,
        commissionRate: p.commissionRate,
        commissionVatSeparate: true,
        paymentFeeRate: 0,
        sellerCouponAmount: 0,
        sellerCouponRate: 0,
        otherMarketplaceDeduction: 0,
        adCostAmount: 0,
        adCostRate: p.adCostRate,
        returnRate: p.returnRate,
        returnCostPerCase: 3000,
        fixedCostAllocation: p.fixedCostAllocation,
        quantity: 1,
      },
      target: { targetProfit: 0, targetMarginRate: p.targetMarginRate },
      taxRule,
      feePolicy,
    };
  }, [draft, taxRules, feePolicies]);

  const calcResult = React.useMemo(
    () => (calcInput && calcInput.selling.salePrice > 0 ? calculateMargin(calcInput) : null),
    [calcInput],
  );

  const sourceUrlInvalid = !isValidUrl(draft?.sourcing.sourceUrl ?? "");

  /**
   * 승인 정책을 못 받았으면 **저장을 막는다** (fail-closed).
   *
   * 열어두면 화면이 "이건 승인 대상이 아니다" 라고 추정해서 원가까지 직접 저장으로
   * 보내게 된다. 서버가 403 으로 막긴 하지만 그건 최종 안전장치지 UX 가 아니다 —
   * 사용자는 입력을 다 끝낸 뒤에야 실패를 본다. **입력 내용은 그대로 유지한다.**
   */
  const savingBlocked = ownershipFailed || !ownership;

  return (
    <>
      <Sheet open={open} onOpenChange={requestClose}>
        <SheetContent
          side="right"
          showCloseButton={false}
          className="[--sheet-w:var(--layout-drawer-width)] flex w-full flex-col gap-0 p-0 sm:w-[92vw]"
        >
          {/* ── 헤더 (고정) ── */}
          <SheetHeader className="shrink-0 gap-1 border-b border-[--color-border] px-4 py-3">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <SheetTitle className="truncate text-[15px]">
                  {isLoading ? "불러오는 중…" : (draft?.name ?? "상품")}
                </SheetTitle>
                <SheetDescription className="mt-0.5 flex items-center gap-2 text-[11px]">
                  {draft && (
                    <>
                      <span className="font-mono">{draft.businessKey}</span>
                      <Badge variant={STATUS_TONE[draft.status]} className="px-1.5 py-0 text-[10px]">
                        {STATUS_LABEL[draft.status]}
                      </Badge>
                      {dirty && (
                        <span className="text-[--color-warning]">저장되지 않은 변경사항</span>
                      )}
                    </>
                  )}
                </SheetDescription>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                {draft?.sourcing.sourceUrl && (
                  <Button variant="ghost" size="icon-sm" asChild title="소스 상품 새 창에서 열기">
                    <a href={draft.sourcing.sourceUrl} target="_blank" rel="noopener noreferrer">
                      <ExternalLink className="h-3.5 w-3.5" aria-hidden />
                      <span className="sr-only">소스 상품 새 창에서 열기</span>
                    </a>
                  </Button>
                )}
                <Button variant="ghost" size="icon-sm" onClick={() => requestClose(false)} aria-label="닫기">
                  <X className="h-4 w-4" aria-hidden />
                </Button>
              </div>
            </div>
            <div className="-mb-3 mt-1">
              <PageTabs tabs={TABS} value={tab} onChange={setTab} />
            </div>
          </SheetHeader>

          {/* ── 본문 (스크롤) ── */}
          <div className="flex-1 overflow-y-auto px-4 py-4">
            {isLoading || !draft ? (
              <div className="space-y-3">
                <Skeleton className="h-8 w-full" />
                <Skeleton className="h-8 w-2/3" />
                <Skeleton className="h-24 w-full" />
              </div>
            ) : (
              <>
                {/* 편집 중에 서버 값이 달라졌다. 내 입력을 덮지 않았다는 사실과
                    무엇이 일어났는지를 함께 말한다 — 둘 중 하나만 말하면
                    사용자는 저장해도 되는지 판단할 수 없다. */}
                {serverChanged && (
                  <div
                    role="alert"
                    className="mb-3 rounded-md border border-[--color-warning] bg-[--color-warning]/10 px-3 py-2 text-[11px]"
                  >
                    <p className="font-medium">이 상품이 다른 곳에서 바뀌었습니다</p>
                    <p className="mt-1 text-[--color-muted-foreground]">
                      입력하신 값은 그대로 두었습니다. 저장하면 입력하신 값이
                      반영됩니다. 최신 값을 보려면 창을 닫았다 다시 여세요.
                    </p>
                  </div>
                )}

                {/* 승인 정책 로딩 실패 — 저장을 막되 입력은 지키지 않으면 사용자가 두 번 일한다 */}
                {savingBlocked && (
                  <div
                    role="alert"
                    className="mb-3 rounded-md border border-[--color-danger] bg-[--color-danger]/10 px-3 py-2 text-[11px]"
                  >
                    <p className="font-medium">승인 정책을 불러오지 못했습니다.</p>
                    <p className="mt-1 text-[--color-muted-foreground]">
                      어떤 항목이 승인 대상인지 확인할 수 없어 저장을 중단했습니다.
                      입력한 변경 내용은 유지됩니다.
                    </p>
                    <Button
                      variant="outline" size="sm" className="mt-2"
                      onClick={() => refetchOwnership()}
                      disabled={ownershipLoading}
                    >
                      {ownershipLoading && (
                        <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
                      )}
                      다시 불러오기
                    </Button>
                  </div>
                )}

                {/* 이미 열린 승인 요청 — 저장을 누르고 나서 409 를 만나는 대신 미리 알린다 */}
                {draft.openApprovals.length > 0 && (
                  <div className="mb-3 rounded-md border border-[--color-warning] bg-[--color-warning]/10 px-3 py-2 text-[11px]">
                    <p className="flex items-center gap-1.5 font-medium">
                      <ShieldCheck className="h-3.5 w-3.5 shrink-0" aria-hidden />
                      진행 중인 승인 요청 {draft.openApprovals.length}건
                    </p>
                    <ul className="mt-1.5 space-y-1">
                      {draft.openApprovals.map((a) => (
                        <li key={a.id} className="flex items-center gap-2">
                          <span className="font-mono">{a.businessKey}</span>
                          <span>{REQUEST_TYPE_LABELS[a.requestType] ?? a.requestType}</span>
                          <Badge variant="default" className="px-1.5 py-0 text-[10px]">
                            {a.status === "PENDING" ? "승인 대기" : a.status === "APPROVED" ? "적용 대기" : "작성 중"}
                          </Badge>
                          <a
                            href={`/approvals?requestId=${a.id}`}
                            className="ops-focusable underline underline-offset-2"
                          >
                            요청 열기
                          </a>
                        </li>
                      ))}
                    </ul>
                    <p className="mt-1.5 text-[--color-muted-foreground]">
                      같은 유형의 변경은 기존 요청이 처리된 뒤에 다시 올릴 수 있습니다.
                    </p>
                  </div>
                )}

                {/* 아직 DB 에 없는 항목 — 0 을 진짜 값으로 오해하지 않게 명시한다 */}
                {(tab === "sourcing" || tab === "pricing") && (
                  <div className="mb-3 flex items-start gap-1.5 rounded-md border border-[--color-border] bg-[--color-muted] px-3 py-2 text-[11px] text-[--color-muted-foreground]">
                    <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                    <span>
                      원가 세부(관세·수입VAT·포장비 등)와 이 탭의 판매가는 아직 저장되지 않습니다.
                      판매처별 실제 가격은 <strong>판매처 정보</strong> 탭에서 관리합니다.
                      화면의 0 은 실제 값이 아니라 <strong>미입력</strong>입니다. 계산 결과는 참고용입니다.
                    </span>
                  </div>
                )}

                {tab === "basic" && (
                  <div className="grid gap-3 sm:grid-cols-2">
                    <Field label="상품명" required htmlFor="p-name" className="sm:col-span-2">
                      <TextInput
                        id="p-name" value={draft.name}
                        onChange={(e) => patch((d) => { d.name = e.target.value; })}
                      />
                    </Field>
                    <Field label="내부 SKU" htmlFor="p-sku">
                      <TextInput
                        id="p-sku" value={draft.sku ?? ""} placeholder="PS-0000"
                        onChange={(e) => patch((d) => { d.sku = e.target.value || null; })}
                      />
                    </Field>
                    <Field label="상태" htmlFor="p-status">
                      <select
                        id="p-status" value={draft.status}
                        onChange={(e) => patch((d) => { d.status = e.target.value as ProductStatus; })}
                        className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
                      >
                        {(Object.keys(STATUS_LABEL) as ProductStatus[]).map((s) => (
                          <option key={s} value={s}>{STATUS_LABEL[s]}</option>
                        ))}
                      </select>
                    </Field>
                    <Field label="카테고리" htmlFor="p-cat">
                      <select
                        id="p-cat" value={draft.category ?? ""}
                        onChange={(e) => patch((d) => { d.category = e.target.value || null; })}
                        className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
                      >
                        <option value="">미지정</option>
                        {MOCK_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
                      </select>
                    </Field>
                    <Field label="담당자" htmlFor="p-owner">
                      <select
                        id="p-owner" value={draft.ownerId ?? ""}
                        onChange={(e) => patch((d) => {
                          d.ownerId = e.target.value || null;
                          d.ownerName =
                            members?.find((m) => m.id === e.target.value)?.display_name ?? null;
                        })}
                        className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
                      >
                        <option value="">미지정</option>
                        {(members ?? [])
                          .filter((m) => m.is_active)
                          .map((m) => (
                            <option key={m.id} value={m.id}>
                              {m.display_name} ({ROLE_LABELS[m.role]})
                            </option>
                          ))}
                      </select>
                    </Field>
                    <Field label="대표 이미지 URL" htmlFor="p-img" className="sm:col-span-2"
                      hint="이미지 업로드는 백엔드 스토리지 연동 후 지원됩니다. 지금은 URL 로 지정합니다.">
                      <TextInput
                        id="p-img" value={draft.imageUrl ?? ""} placeholder="https://…"
                        onChange={(e) => patch((d) => { d.imageUrl = e.target.value || null; })}
                      />
                    </Field>
                    <Field label="메모" htmlFor="p-memo" className="sm:col-span-2">
                      <textarea
                        id="p-memo" rows={3} value={draft.memo ?? ""}
                        onChange={(e) => patch((d) => { d.memo = e.target.value || null; })}
                        className="ops-focusable w-full rounded-md border border-[--color-border] bg-[--color-card] p-2 text-xs"
                      />
                    </Field>
                  </div>
                )}

                {tab === "sourcing" && (
                  <div className="space-y-4">
                    <Field
                      label="소스 상품 URL" htmlFor="p-src"
                      hint={sourceUrlInvalid ? undefined : "상품 행에 저장되는 대표 링크입니다"}
                    >
                      <div className="flex gap-1.5">
                        <TextInput
                          id="p-src" value={draft.sourcing.sourceUrl ?? ""}
                          placeholder="https://detail.1688.com/offer/…"
                          invalid={sourceUrlInvalid}
                          aria-describedby={sourceUrlInvalid ? "p-src-err" : undefined}
                          onChange={(e) => patch((d) => {
                            d.sourcing.sourceUrl = e.target.value || null;
                            d.sourceUrl = e.target.value || null;
                          })}
                        />
                        {draft.sourcing.sourceUrl && !sourceUrlInvalid && (
                          <Button variant="outline" size="sm" asChild>
                            <a href={draft.sourcing.sourceUrl} target="_blank" rel="noopener noreferrer">
                              <ExternalLink className="h-3.5 w-3.5" aria-hidden />
                              <span className="sr-only">새 창에서 열기</span>
                            </a>
                          </Button>
                        )}
                      </div>
                      {sourceUrlInvalid && (
                        <p id="p-src-err" role="alert" className="text-[11px] text-[--color-danger]">
                          http 또는 https 로 시작하는 주소를 입력하세요
                        </p>
                      )}
                    </Field>

                    {/* 여기부터는 실제 소싱 모델(M037)이다.
                        예전에는 공급처를 문자열로 받고 관세·포장비 같은 항목을
                        입력받아 프론트에서 총원가를 계산했는데, 그 값들은 저장되지
                        않았다 — 다시 열면 사라져 있었다. */}
                    {/* 아직 저장되지 않은 새 상품에는 소싱처를 붙일 수 없다 —
                        붙일 대상 행이 없다. */}
                    {productId ? (
                      <ProductSourcingPanel
                        productId={productId}
                        marketplace={draft.marketplaces[0] ?? "COUPANG"}
                        salePrice={draft.pricing.salePrice}
                      />
                    ) : (
                      <p className="text-[11px] text-[--color-muted-foreground]">
                        상품을 먼저 저장하면 소싱처를 연결할 수 있습니다.
                      </p>
                    )}
                  </div>
                )}

                {tab === "pricing" && (
                  <div className="space-y-4">
                    <div className="grid gap-3 sm:grid-cols-3">
                      <Field label="판매가" htmlFor="p-sale" required>
                        <MoneyInput
                          id="p-sale" value={draft.pricing.salePrice}
                          onChange={(v) => patch((d) => {
                            d.pricing.salePrice = v ?? 0;
                            d.salePrice = v ?? 0;
                          })}
                        />
                      </Field>
                      <Field label="구매자 부담 배송비" htmlFor="p-bship">
                        <MoneyInput
                          id="p-bship" value={draft.pricing.buyerShippingFee}
                          onChange={(v) => patch((d) => { d.pricing.buyerShippingFee = v ?? 0; })}
                        />
                      </Field>
                      <Field label="판매가 적용일" htmlFor="p-pricedate">
                        <TextInput
                          id="p-pricedate" type="date" value={draft.pricing.priceEffectiveFrom ?? ""}
                          onChange={(e) => patch((d) => {
                            d.pricing.priceEffectiveFrom = e.target.value || null;
                            d.priceEffectiveFrom = e.target.value || null;
                          })}
                        />
                      </Field>
                      <Field label="판매수수료율" htmlFor="p-comm"
                        hint={calcInput?.feePolicy ? `정책 자동 적용: ${calcInput.feePolicy.description}` : "정책 없음 — 직접 입력값 사용"}>
                        <PercentInput
                          id="p-comm" value={draft.pricing.commissionRate}
                          disabled={!!calcInput?.feePolicy}
                          onChange={(v) => patch((d) => { d.pricing.commissionRate = v ?? 0; })}
                        />
                      </Field>
                      <Field label="광고비율" htmlFor="p-ad">
                        <PercentInput
                          id="p-ad" value={draft.pricing.adCostRate}
                          onChange={(v) => patch((d) => { d.pricing.adCostRate = v ?? 0; })}
                        />
                      </Field>
                      <Field label="반품 예상률" htmlFor="p-ret">
                        <PercentInput
                          id="p-ret" value={draft.pricing.returnRate}
                          onChange={(v) => patch((d) => { d.pricing.returnRate = v ?? 0; })}
                        />
                      </Field>
                      <Field label="고정비 배분액" htmlFor="p-fixed">
                        <MoneyInput
                          id="p-fixed" value={draft.pricing.fixedCostAllocation}
                          onChange={(v) => patch((d) => { d.pricing.fixedCostAllocation = v ?? 0; })}
                        />
                      </Field>
                      <Field label="목표 마진율" htmlFor="p-target">
                        <PercentInput
                          id="p-target" value={draft.pricing.targetMarginRate}
                          onChange={(v) => patch((d) => { d.pricing.targetMarginRate = v ?? 0; })}
                        />
                      </Field>
                    </div>

                    {calcResult ? (
                      <>
                        <CalcSummary result={calcResult} />
                        <div className="grid gap-3 lg:grid-cols-2">
                          <TargetPrices result={calcResult} />
                          <div className="flex items-start justify-between gap-2 rounded-lg border border-[--color-border] p-3">
                            <div>
                              <p className="text-[12px] font-medium">마진율</p>
                              <p className={cn(
                                "mt-0.5 text-lg font-semibold tabular-nums",
                                (calcResult.marginRate ?? 0) < 0 && "text-[--color-loss]",
                                (calcResult.marginRate ?? 0) >= draft.pricing.targetMarginRate && "text-[--color-profit]",
                              )}>
                                {formatPercent(calcResult.marginRate)}
                              </p>
                              <p className="text-[10px] text-[--color-muted-foreground]">
                                목표 {formatPercent(draft.pricing.targetMarginRate)}
                              </p>
                            </div>
                            <CalcHelpButton />
                          </div>
                        </div>
                        <CalcBreakdown result={calcResult} />
                        {/* 이 미리보기는 상품 행에 저장되는 원가(매입가·통화·환율)만
                            쓴다. 관세·포장비 같은 취득 부대비용은 소싱 탭의
                            원가 version 에 있고, 그 값이 반영된 예상 원가는
                            소싱 탭의 landed cost 가 서버에서 계산해 보여준다. */}
                        <p className="text-[10px] text-[--color-muted-foreground]">
                          상품에 저장된 원가 기준입니다. 취득 부대비용까지 포함한
                          예상 원가는 소싱 탭에서 확인하세요.
                        </p>
                      </>
                    ) : (
                      <p className="rounded-lg border border-dashed border-[--color-border] p-6 text-center text-xs text-[--color-muted-foreground]">
                        판매가를 입력하면 마진이 자동으로 계산됩니다
                      </p>
                    )}
                  </div>
                )}

                {tab === "channels" && (
                  <ChannelPricePanel
                    productId={productId!}
                    variantOptions={(draft.options ?? []).map((o) => ({
                      id: o.id,
                      label: o.customName ?? o.autoName,
                    }))}
                  />
                )}

                {tab === "options" && (
                  <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed border-[--color-border] p-8 text-center">
                    <PackageOpen className="h-8 w-8 text-[--color-muted-foreground] opacity-40" aria-hidden />
                    <p className="text-sm font-medium">
                      옵션 {draft.optionCount}개
                    </p>
                    <p className="max-w-sm text-xs text-[--color-muted-foreground]">
                      옵션은 행이 많아 넓은 화면이 필요합니다. 전용 편집 창에서 그룹·값·가격·재고를
                      한 번에 관리하세요.
                    </p>
                    <Button size="sm" onClick={() => onOpenOptions?.(draft.id)}>
                      <Settings2 className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                      옵션 편집
                    </Button>
                  </div>
                )}

                {tab === "history" && (
                  <ol className="space-y-2">
                    {draft.history.length === 0 && (
                      <p className="text-xs text-[--color-muted-foreground]">변경 기록이 없습니다</p>
                    )}
                    {draft.history.map((h) => (
                      <li key={h.id} className="rounded-lg border border-[--color-border] px-3 py-2 text-xs">
                        <div className="flex items-center justify-between">
                          <span className="font-medium">{h.field}</span>
                          <span className="text-[10px] text-[--color-muted-foreground]">
                            {formatDateTime(h.at)} · {h.actor}
                          </span>
                        </div>
                        <p className="mt-1 text-[--color-muted-foreground]">
                          {h.before !== null && (
                            <>
                              <span className="line-through">{h.before}</span>
                              <span className="mx-1.5" aria-hidden>→</span>
                            </>
                          )}
                          <span className="text-[--color-foreground]">{h.after}</span>
                        </p>
                      </li>
                    ))}
                  </ol>
                )}
              </>
            )}
          </div>

          {/* ── 푸터 (스크롤과 무관하게 고정) ── */}
          <div className="flex shrink-0 items-center justify-between gap-2 border-t border-[--color-border] bg-[--color-muted] px-4 py-3">
            <p className="text-[11px] text-[--color-muted-foreground]">
              {savingBlocked
                ? "승인 정책을 불러오지 못해 저장할 수 없습니다"
                : !dirty
                ? "변경사항 없음"
                : classified?.hasApproval && classified.hasDirect
                  ? "일부는 바로 저장되고, 일부는 승인을 거칩니다"
                  : classified?.hasApproval
                    ? "승인을 거쳐야 반영되는 변경입니다"
                    : "저장하지 않으면 변경사항이 사라집니다"}
            </p>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => requestClose(false)}>
                취소
              </Button>
              <Button
                size="sm"
                onClick={onSaveClick}
                disabled={!dirty || save.isPending || sourceUrlInvalid || savingBlocked}
              >
                {save.isPending && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />}
                {classified?.hasApproval ? "승인 요청" : "저장"}
              </Button>
            </div>
          </div>
        </SheetContent>
      </Sheet>

      {/* 승인 대상 변경이 섞여 있을 때 — 무엇이 승인으로 가는지 보여주고 사유를 받는다 */}
      <AlertDialog open={confirmApproval} onOpenChange={setConfirmApproval}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>승인 요청을 올릴까요?</AlertDialogTitle>
            <AlertDialogDescription>
              아래 변경은 승인을 거쳐야 상품에 반영됩니다. 승인 전까지 상품 값은 그대로입니다.
            </AlertDialogDescription>
          </AlertDialogHeader>

          <div className="space-y-3 text-xs">
            {Object.entries(classified?.approval ?? {}).map(([type, fields]) => (
              <div key={type} className="rounded-md border border-[--color-border] px-3 py-2">
                <p className="font-medium">{REQUEST_TYPE_LABELS[type] ?? type}</p>
                <ul className="mt-1 space-y-0.5 text-[--color-muted-foreground]">
                  {Object.keys(fields).map((f) => (
                    <li key={f}>· {PRODUCT_FIELD_LABELS[f] ?? f}</li>
                  ))}
                </ul>
              </div>
            ))}
            {classified?.hasDirect && (
              <p className="text-[--color-muted-foreground]">
                나머지 {Object.keys(classified.direct).length}개 항목은 승인 없이 바로 저장됩니다.
              </p>
            )}

            <Field label="변경 사유" htmlFor="ap-reason">
              <TextInput
                id="ap-reason" value={approvalReason}
                placeholder="예: 매입처 단가 인상 반영"
                onChange={(e) => setApprovalReason(e.target.value)}
              />
            </Field>
            <Field label="적용 희망일 (비우면 승인 즉시 적용)" htmlFor="ap-eff">
              <input
                id="ap-eff" type="date" value={effectiveFrom}
                onChange={(e) => setEffectiveFrom(e.target.value)}
                className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
              />
            </Field>
          </div>

          <AlertDialogFooter>
            <AlertDialogCancel>계속 편집</AlertDialogCancel>
            <AlertDialogAction onClick={() => save.mutate()} disabled={save.isPending}>
              {save.isPending && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />}
              승인 요청 올리기
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={confirmClose} onOpenChange={setConfirmClose}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>저장하지 않고 닫을까요?</AlertDialogTitle>
            <AlertDialogDescription>
              변경한 내용이 사라집니다. 이 작업은 되돌릴 수 없습니다.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>계속 편집</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setConfirmClose(false);
                setDirty(false);
                onOpenChange(false);
              }}
            >
              저장하지 않고 닫기
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
