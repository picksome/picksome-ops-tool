"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Info, Loader2, Lock, ShieldCheck, Trash2, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Field } from "@/components/shell/page-shell";
import { TextInput } from "@/components/ui/money-input";
import { VatInclusionToggle } from "./vat-inclusion-toggle";
import { ExpenseAdjustmentDialog } from "./expense-adjustment-dialog";
import { EvidencePanel } from "./evidence-panel";
import { SupplierSelect } from "./supplier-select";
import { useExpenseRefs } from "@/hooks/useExpenseRefs";
import {
  approvalsApi, describeApprovalError, REQUEST_TYPE_LABELS,
} from "@/lib/api/approvals";
import { classifyProductChanges, diffAgainst } from "@/lib/api/products";
import { saveExpenseWithApproval } from "@/lib/api/expense-save";
import {
  expensesApi, type EvidenceStatus, type ExpenseOut, type VatInclusionType,
} from "@/lib/api/expenses";
import {
  ADJUSTMENT_TYPE_LABEL, EXPENSE_FIELD_LABELS, EXPENSE_VALUE_LABELS,
  expenseApprovalApi, lockGuidance, optionsOf, reimbursementOf, REIMBURSEMENT_LABEL,
} from "@/lib/api/expenses-approval";
import { formatDate, formatKRW } from "@/lib/format";
import { todayKst } from "@/lib/date-range";

const SELECT_CLASS =
  "ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs disabled:cursor-not-allowed disabled:bg-[--color-muted] disabled:text-[--color-muted-foreground]";

/** 화면이 편집하는 필드. 서버 관리 필드(id·created_at·조인 이름)는 넣지 않는다. */
interface Draft {
  expense_date: string;
  category_id: string;
  description: string;
  gross_amount: number;
  supply_amount: number;
  vat_amount: number;
  vat_inclusion_type: VatInclusionType;
  tax_type: string;
  is_manual_tax_override: boolean;
  payment_method: string;
  payment_card_holder_id: string | null;
  is_personal_card: boolean;
  is_business_use: boolean;
  evidence_type: string;
  evidence_status: EvidenceStatus;
  is_tax_deductible: boolean;
  is_settlement_deductible: boolean;
  attributable_partner_id: string | null;
  supplier_id: string | null;
  memo: string | null;
}

function draftOf(e: ExpenseOut): Draft {
  return {
    expense_date: e.expense_date,
    category_id: e.category_id,
    description: e.description,
    gross_amount: e.gross_amount,
    supply_amount: e.supply_amount,
    vat_amount: e.vat_amount,
    vat_inclusion_type: e.vat_inclusion_type,
    tax_type: e.tax_type,
    is_manual_tax_override: e.is_manual_tax_override,
    payment_method: e.payment_method,
    payment_card_holder_id: e.payment_card_holder_id,
    is_personal_card: e.is_personal_card,
    is_business_use: e.is_business_use,
    evidence_type: e.evidence_type,
    evidence_status: e.evidence_status,
    is_tax_deductible: e.is_tax_deductible,
    is_settlement_deductible: e.is_settlement_deductible,
    attributable_partner_id: e.attributable_partner_id,
    supplier_id: e.supplier_id,
    memo: e.memo,
  };
}

function emptyDraft(): Draft {
  return {
    expense_date: todayKst(),
    category_id: "",
    description: "",
    gross_amount: 0,
    supply_amount: 0,
    vat_amount: 0,
    vat_inclusion_type: "INCLUDED",
    tax_type: "GENERAL",
    is_manual_tax_override: false,
    payment_method: "BUSINESS_CARD",
    payment_card_holder_id: null,
    is_personal_card: false,
    is_business_use: true,
    evidence_type: "CARD_SLIP",
    evidence_status: "NONE",
    is_tax_deductible: true,
    is_settlement_deductible: true,
    attributable_partner_id: null,
    supplier_id: null,
    memo: null,
  };
}

function CheckField({
  id, label, hint, checked, disabled, onChange,
}: {
  id: string; label: string; hint?: string;
  checked: boolean; disabled?: boolean; onChange: (v: boolean) => void;
}) {
  return (
    <label
      htmlFor={id}
      className="flex cursor-pointer items-start gap-2 rounded-md border border-[--color-border] px-2.5 py-2"
    >
      <input
        id={id} type="checkbox" checked={checked} disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="ops-focusable mt-0.5 h-3.5 w-3.5 shrink-0 rounded"
      />
      <span className="min-w-0">
        <span className="block text-xs font-medium">{label}</span>
        {hint && <span className="block text-[10px] text-[--color-muted-foreground]">{hint}</span>}
      </span>
    </label>
  );
}

/**
 * 사업비용 등록·수정 드로어.
 *
 * 두 가지 규칙이 이 화면의 뼈대다.
 *
 * 1. **금액은 직접 저장되지 않는다.** 비용 금액은 순이익의 입력값이고 순이익이
 *    곧 파트너 수익배분이다. 승인 대상 필드가 무엇인지는 서버가 알려주고
 *    (`/approvals/field-ownership`), 화면은 저장을 누르기 전에 무엇이 승인으로
 *    가는지 보여준다.
 * 2. **잠긴 달은 아예 편집을 막는다.** 눌러서 409 를 맞은 뒤 "조정을 만드세요" 를
 *    읽으면 이미 입력을 다 끝낸 뒤다. 열기 전에 `lock-state` 로 확인한다.
 */
export function ExpenseDrawer({
  expenseId, open, onOpenChange,
}: {
  /** null 이면 신규 등록 */
  expenseId: string | null;
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const qc = useQueryClient();
  const isEdit = !!expenseId;
  const [draft, setDraft] = React.useState<Draft>(emptyDraft);
  const [dirty, setDirty] = React.useState(false);
  const [reason, setReason] = React.useState("");
  const [confirmApproval, setConfirmApproval] = React.useState(false);
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [adjustOpen, setAdjustOpen] = React.useState(false);

  const { activePartners, partners, categories } = useExpenseRefs();

  const { data: expense, isLoading } = useQuery({
    queryKey: ["expense", expenseId],
    queryFn: () => expensesApi.get(expenseId!),
    enabled: isEdit && open,
  });

  /**
   * 승인 대상 필드 표 — **서버가 알려준다.** 화면이 표를 들고 있으면 백엔드와
   * 어긋나는 날 사용자가 입력을 다 끝낸 뒤에야 403 을 본다.
   */
  const {
    data: ownershipAll, isError: ownershipFailed,
    isFetching: ownershipLoading, refetch: refetchOwnership,
  } = useQuery({
    queryKey: ["approvalFieldOwnership"],
    queryFn: () => expensesApi.fieldOwnership(),
    staleTime: 5 * 60_000,
    retry: 1,
  });
  const ownership = ownershipAll?.expense ?? null;

  /** 이 비용이 귀속된 달을 지금 고칠 수 있는가. */
  const { data: lock } = useQuery({
    queryKey: ["expense-lock", expenseId],
    queryFn: () => expenseApprovalApi.lockState(expenseId!),
    enabled: isEdit && open,
  });

  const { data: adjustments } = useQuery({
    queryKey: ["expense-adjustments", expenseId],
    queryFn: () => expenseApprovalApi.adjustments(expenseId!),
    enabled: isEdit && open,
  });

  /** 이 비용에 이미 걸린 승인 요청. 저장 후 409 를 만나는 대신 미리 알린다. */
  const { data: approvalList } = useQuery({
    queryKey: ["approvals", "expense", expenseId],
    queryFn: () => approvalsApi.list({
      target_type: "expense", target_id: expenseId!, page_size: 20,
    }),
    enabled: isEdit && open,
  });
  const openApprovals = (approvalList?.items ?? []).filter(
    (a) => a.status === "DRAFT" || a.status === "PENDING" || a.status === "APPROVED",
  );

  /**
   * draft 가 **어느 행에서 채워졌는지**. 이게 없으면 폼이 빈 draft 로 먼저 그려지고,
   * 그 다음 렌더에서 값이 들어온다. 부가세 토글의 자동 분리는 값이 바뀔 때 도는데,
   * 그 "값이 바뀜" 을 사용자 입력으로 착각해 **열자마자 저장값을 덮어쓴다** —
   * 사용자가 아무것도 안 건드렸는데 "승인 요청" 버튼이 뜨고, 메모만 고치려던 사람이
   * 금액 변경 승인을 올리게 된다. 실제로 E2E 가 이 상태를 잡았다.
   */
  const [loadedFor, setLoadedFor] = React.useState<string | null>(null);

  /**
   * 편집 중에 서버 값이 달라졌다는 사실. **덮지 않고 알리기만 한다.**
   */
  const [serverChanged, setServerChanged] = React.useState(false);

  /**
   * `dirty` 를 아래 effect 의 **의존성으로 두지 않기 위한** 거울.
   *
   * 의존성에 넣으면 사용자가 첫 글자를 치는 순간(false→true) effect 가 다시 돌고,
   * 서버가 아무것도 안 보냈는데 "값이 바뀌었다" 는 안내가 뜬다.
   */
  const dirtyRef = React.useRef(dirty);
  dirtyRef.current = dirty;

  /** 이 draft 를 어느 서버 응답으로 채웠나. 같은 응답이면 다시 채우지 않는다. */
  const filledFromRef = React.useRef<ExpenseOut | null>(null);

  /**
   * 초기화는 **행이 바뀌거나 손대지 않은 상태**에서만 한다.
   *
   * 예전에는 `expense` 가 바뀔 때마다 draft 를 통째로 다시 채웠다. React Query 의
   * `refetchOnWindowFocus` 기본값이 true 이고 이 앱의 `staleTime` 은 30초라,
   * **카드 명세서를 보다가 창을 다녀오면 입력이 사라졌다.**
   *
   * 평소에는 드러나지 않았다 — 서버 값이 같으면 structural sharing 이 같은
   * 객체를 돌려줘서 effect 가 아예 안 돌기 때문이다. 그래서 "가끔" 사라졌다.
   * 다른 사람이 같은 비용을 고쳤을 때만 터진다(테스트로 재현했다).
   */
  React.useEffect(() => {
    if (!open) return;
    if (isEdit && !expense) return;   // 아직 값이 없다 — 빈 폼을 그리지 않는다

    const sameRow = loadedFor === expenseId;
    if (sameRow && filledFromRef.current === expense) return;  // 같은 응답이다

    if (sameRow && dirtyRef.current) {
      // 사용자가 손댄 값이 source of truth 다. 서버 응답으로 덮지 않는다.
      // 대신 달라졌다는 사실을 알린다 — 조용히 무시해도 안 된다.
      setServerChanged(true);
      return;
    }

    filledFromRef.current = expense ?? null;
    setDraft(expense ? draftOf(expense) : emptyDraft());
    setLoadedFor(expenseId);
    setDirty(false);
    setReason("");
    setServerChanged(false);
  }, [open, expense, expenseId, isEdit, loadedFor]);

  /** 폼을 그려도 되는가 — 신규이거나, 이 행의 값으로 draft 가 채워졌을 때만. */
  const ready = !isEdit || (!!expense && loadedFor === expenseId);

  const locked = !!lock?.requires_adjustment;
  const guidance = lock ? lockGuidance(lock) : null;

  /** 지금 변경분이 어떻게 나뉘는가. 저장 버튼을 누르기 **전에** 보여준다. */
  const classified = React.useMemo(() => {
    if (!isEdit || !expense || !ownership) return null;
    const changes = diffAgainst(
      draftOf(expense) as unknown as Record<string, unknown>,
      draft as unknown as Record<string, unknown>,
    );
    return classifyProductChanges(changes, ownership);
  }, [isEdit, expense, ownership, draft]);

  function patch(updater: (d: Draft) => void) {
    setDraft((prev) => {
      const next = structuredClone(prev);
      updater(next);
      return next;
    });
    setDirty(true);
  }

  const create = useMutation({
    mutationFn: () => expensesApi.create({
      ...draft,
      // 자동 분리를 쓸 때는 supply/vat 을 보내지 않는다 — 서버가 정책 세율로
      // 계산한다. 화면 계산값을 같이 보내면 세율이 다를 때 CHECK 제약에 걸린다.
      supply_amount: draft.is_manual_tax_override ? draft.supply_amount : null,
      vat_amount: draft.is_manual_tax_override ? draft.vat_amount : null,
    }),
    onSuccess: () => {
      toast.success("비용을 등록했습니다");
      qc.invalidateQueries({ queryKey: ["expenses"] });
      onOpenChange(false);
    },
    onError: (err) => {
      const g = describeApprovalError(err);
      toast.error(g.title, { description: g.description });
    },
  });

  const save = useMutation({
    mutationFn: () => saveExpenseWithApproval(
      expenseId!,
      draftOf(expense!) as unknown as Record<string, unknown>,
      draft as unknown as Record<string, unknown>,
      { ownership, reason: reason.trim() || undefined },
    ),
    onSuccess: (r) => {
      // 무엇이 **지금 반영됐고** 무엇이 **승인 대기로 갔는지** 구분해 알린다.
      // "저장했습니다" 하나로 뭉치면 사용자는 금액까지 바뀐 줄 안다.
      if (r.approvals.length > 0 && r.directSaved) {
        toast.success(`일부는 저장하고, ${r.approvals.length}건은 승인 요청으로 올렸습니다`, {
          description: "금액·귀속일·증빙 변경은 승인 후 반영됩니다.",
        });
      } else if (r.approvals.length > 0) {
        toast.success(`승인 요청 ${r.approvals.length}건을 올렸습니다`, {
          description: "승인이 완료되어야 장부 금액이 바뀝니다.",
        });
      } else {
        toast.success("저장했습니다");
      }
      setDirty(false);
      setReason("");
      setConfirmApproval(false);
      qc.invalidateQueries({ queryKey: ["expenses"] });
      qc.invalidateQueries({ queryKey: ["expense", expenseId] });
      qc.invalidateQueries({ queryKey: ["approvals"] });
      onOpenChange(false);
    },
    onError: (err) => {
      const g = describeApprovalError(err);
      toast.error(g.title, { description: g.description });
      if (g.shouldRefetch) qc.invalidateQueries({ queryKey: ["expense", expenseId] });
    },
  });

  const remove = useMutation({
    mutationFn: () => expensesApi.delete(expenseId!),
    onSuccess: () => {
      toast.success("삭제했습니다");
      qc.invalidateQueries({ queryKey: ["expenses"] });
      setConfirmDelete(false);
      onOpenChange(false);
    },
    onError: (err) => {
      const g = describeApprovalError(err);
      toast.error(g.title, { description: g.description });
    },
  });

  function onSaveClick() {
    if (!isEdit) { create.mutate(); return; }
    if (classified?.hasApproval) { setConfirmApproval(true); return; }
    save.mutate();
  }

  const reimbursement = expense
    ? reimbursementOf({
      is_personal_card: expense.is_personal_card,
      gross_amount: expense.gross_amount,
      payment_card_holder_id: expense.payment_card_holder_id,
    })
    : null;

  const incomplete = !draft.description.trim() || !draft.category_id || draft.gross_amount <= 0;
  // 개인카드는 DB CHECK 로 결제자가 필수다. 저장을 눌러 500 을 맞기 전에 막는다.
  const needsHolder = draft.is_personal_card && !draft.payment_card_holder_id;
  const savingBlocked = !ready || (isEdit && (!ownership || locked));
  const pending = save.isPending || create.isPending;

  return (
    <>
      <Sheet open={open} onOpenChange={onOpenChange}>
        <SheetContent
          side="right" showCloseButton={false}
          className="[--sheet-w:600px] flex w-full flex-col gap-0 p-0"
        >
          <SheetHeader className="shrink-0 border-b border-[--color-border] px-4 py-3">
            <div className="flex items-center justify-between">
              <SheetTitle className="text-[15px]">
                {isEdit ? "비용 수정" : "비용 등록"}
              </SheetTitle>
              <div className="flex items-center gap-1">
                {isEdit && (
                  <Button
                    variant="ghost" size="icon-sm" onClick={() => setConfirmDelete(true)}
                    aria-label="이 비용 삭제" disabled={locked}
                  >
                    <Trash2 className="h-3.5 w-3.5 text-[--color-danger]" aria-hidden />
                  </Button>
                )}
                <Button variant="ghost" size="icon-sm" onClick={() => onOpenChange(false)} aria-label="닫기">
                  <X className="h-4 w-4" aria-hidden />
                </Button>
              </div>
            </div>
          </SheetHeader>

          <div className="flex-1 space-y-3 overflow-y-auto p-4">
            {isLoading || !ready ? (
              <p className="py-8 text-center text-xs text-[--color-muted-foreground]">불러오는 중…</p>
            ) : (
              <>
                {/* 편집 중에 서버 값이 달라졌다. 내 입력을 덮지 않았다는 사실과
                    무엇이 일어났는지를 함께 말한다 — 둘 중 하나만 말하면
                    사용자는 저장해도 되는지 판단할 수 없다. */}
                {serverChanged && (
                  <div className="rounded-md border border-[--color-warning] bg-[--color-warning]/10 px-3 py-2 text-[11px]">
                    <p className="font-medium">
                      이 비용이 다른 곳에서 바뀌었습니다
                    </p>
                    <p className="mt-0.5 text-[--color-muted-foreground]">
                      입력하신 값은 그대로 두었습니다. 저장하면 입력하신 값이
                      반영됩니다. 최신 값을 보려면 창을 닫았다 다시 여세요.
                    </p>
                  </div>
                )}

                {/* 잠긴 달 — 무엇을 해야 하는지까지 말한다 */}
                {guidance && (
                  <div className="rounded-md border border-[--color-warning] bg-[--color-warning]/10 px-3 py-2 text-[11px]">
                    <p className="flex items-center gap-1.5 font-medium">
                      <Lock className="h-3.5 w-3.5 shrink-0" aria-hidden />
                      잠긴 월의 비용입니다
                    </p>
                    <p className="mt-1">{guidance}</p>
                    <Button
                      variant="outline" size="xs" className="mt-2"
                      onClick={() => setAdjustOpen(true)}
                    >
                      정정 조정 만들기
                    </Button>
                  </div>
                )}

                {/* 승인 정책을 못 받으면 저장하지 않는다 — 추측으로 보내면 403 이다 */}
                {isEdit && ownershipFailed && (
                  <div className="rounded-md border border-[--color-danger] bg-[--color-danger]/10 px-3 py-2 text-[11px]">
                    <p className="font-medium">승인 정책을 불러오지 못했습니다</p>
                    <p className="mt-1 text-[--color-muted-foreground]">
                      어떤 항목이 승인 대상인지 확인할 수 없어 저장을 막았습니다.
                      입력한 내용은 유지됩니다.
                    </p>
                    <Button
                      variant="outline" size="xs" className="mt-2"
                      onClick={() => refetchOwnership()} disabled={ownershipLoading}
                    >
                      {ownershipLoading && (
                        <Loader2 className="mr-1.5 h-3 w-3 animate-spin" aria-hidden />
                      )}
                      다시 불러오기
                    </Button>
                  </div>
                )}

                {openApprovals.length > 0 && (
                  <div className="rounded-md border border-[--color-warning] bg-[--color-warning]/10 px-3 py-2 text-[11px]">
                    <p className="flex items-center gap-1.5 font-medium">
                      <ShieldCheck className="h-3.5 w-3.5 shrink-0" aria-hidden />
                      진행 중인 승인 요청 {openApprovals.length}건
                    </p>
                    <ul className="mt-1.5 space-y-1">
                      {openApprovals.map((a) => (
                        <li key={a.id} className="flex flex-wrap items-center gap-2">
                          <span className="font-mono">{a.business_key}</span>
                          <span>{REQUEST_TYPE_LABELS[a.request_type] ?? a.request_type}</span>
                          <Badge variant="default" className="px-1.5 py-0 text-[10px]">
                            {a.status === "PENDING" ? "승인 대기"
                              : a.status === "APPROVED" ? "적용 대기" : "작성 중"}
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

                {/* ── 기본 ── */}
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field
                    label="비용 발생일" required htmlFor="e-date"
                    hint={isEdit ? "귀속월이 여기서 정해집니다 — 승인 대상" : "귀속월이 여기서 정해집니다"}
                  >
                    <TextInput
                      id="e-date" type="date" value={draft.expense_date} disabled={locked}
                      onChange={(e) => patch((d) => { d.expense_date = e.target.value; })}
                    />
                  </Field>
                  <Field label="비용 카테고리" required htmlFor="e-cat">
                    <select
                      id="e-cat" className={SELECT_CLASS} value={draft.category_id} disabled={locked}
                      onChange={(e) => {
                        const cat = categories.find((c) => c.id === e.target.value);
                        patch((d) => {
                          d.category_id = e.target.value;
                          // 신규 등록에서만 카테고리 기본값을 따라간다. 기존 비용의
                          // 공제 여부를 카테고리 변경만으로 바꾸면 승인 diff 에
                          // 사용자가 의도하지 않은 항목이 섞인다.
                          if (!isEdit && cat) {
                            d.is_tax_deductible = cat.defaultTaxDeductible;
                            d.is_settlement_deductible = cat.defaultSettlementDeductible;
                          }
                        });
                      }}
                    >
                      <option value="">선택</option>
                      {categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                    </select>
                  </Field>
                  <Field label="내용" required htmlFor="e-desc" className="sm:col-span-2">
                    <TextInput
                      id="e-desc" value={draft.description} disabled={locked}
                      placeholder="예: 쿠팡 광고비 정산"
                      onChange={(e) => patch((d) => { d.description = e.target.value; })}
                    />
                  </Field>
                </div>

                {/* ── 금액·부가세 ── */}
                <div className={locked ? "pointer-events-none opacity-60" : undefined}>
                  {/* key — 다른 비용으로 갈아탈 때 다시 마운트시킨다. 그대로 두면
                      자동 분리 effect 가 새 행의 저장값을 덮어써 가짜 변경분이 생긴다. */}
                  <VatInclusionToggle
                    key={expenseId ?? "new"}
                    value={draft.vat_inclusion_type}
                    onChange={(v) => patch((d) => { d.vat_inclusion_type = v; })}
                    grossAmount={draft.gross_amount}
                    supplyAmount={draft.supply_amount}
                    vatAmount={draft.vat_amount}
                    onGrossChange={(v) => patch((d) => { d.gross_amount = v; })}
                    onSupplyChange={(v) => patch((d) => { d.supply_amount = v; })}
                    onVatChange={(v) => patch((d) => { d.vat_amount = v; })}
                    isManualOverride={draft.is_manual_tax_override}
                    onManualOverrideChange={(v) => patch((d) => { d.is_manual_tax_override = v; })}
                  />
                </div>
                <p className="flex items-start gap-1.5 text-[10px] text-[--color-muted-foreground]">
                  <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
                  화면의 분리 금액은 미리보기입니다. 실제 공급가액·부가세는 저장 시점의
                  세금 정책(세율)으로 서버가 계산해 박제합니다.
                </p>

                {/* ── 세무·정산 ── */}
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label="과세 유형" htmlFor="e-tax">
                    <select
                      id="e-tax" className={SELECT_CLASS} value={draft.tax_type} disabled={locked}
                      onChange={(e) => patch((d) => { d.tax_type = e.target.value; })}
                    >
                      {optionsOf("tax_type").map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </select>
                  </Field>
                  <Field
                    label="귀속 파트너" htmlFor="e-attr"
                    hint="특정 파트너에게 귀속되는 비용이면 지정합니다"
                  >
                    <select
                      id="e-attr" className={SELECT_CLASS} disabled={locked}
                      value={draft.attributable_partner_id ?? ""}
                      onChange={(e) => patch((d) => {
                        d.attributable_partner_id = e.target.value || null;
                      })}
                    >
                      <option value="">공통 비용</option>
                      {activePartners.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                    </select>
                  </Field>
                  <CheckField
                    id="e-biz" label="업무용" hint="개인 지출이면 해제"
                    checked={draft.is_business_use} disabled={locked}
                    onChange={(v) => patch((d) => { d.is_business_use = v; })}
                  />
                  <CheckField
                    id="e-taxded" label="세무 공제 대상" hint="부가세 매입세액 공제"
                    checked={draft.is_tax_deductible} disabled={locked}
                    onChange={(v) => patch((d) => { d.is_tax_deductible = v; })}
                  />
                  <CheckField
                    id="e-setded" label="정산 차감 대상"
                    hint="해제하면 순이익에서 빠지지 않습니다"
                    checked={draft.is_settlement_deductible} disabled={locked}
                    onChange={(v) => patch((d) => { d.is_settlement_deductible = v; })}
                  />
                </div>

                {/* ── 결제 ── */}
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label="결제수단" required htmlFor="e-pm">
                    <select
                      id="e-pm" className={SELECT_CLASS} value={draft.payment_method} disabled={locked}
                      onChange={(e) => patch((d) => {
                        d.payment_method = e.target.value;
                        // 결제수단과 개인카드 플래그가 어긋나면 상환 집계가 틀어진다.
                        d.is_personal_card = e.target.value === "PERSONAL_CARD";
                      })}
                    >
                      {optionsOf("payment_method").map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </select>
                  </Field>
                  <Field
                    label="결제자" htmlFor="e-holder"
                    required={draft.is_personal_card}
                    hint={draft.is_personal_card ? "개인카드는 결제자가 필요합니다 — 나중에 돌려줘야 할 돈입니다" : undefined}
                  >
                    <select
                      id="e-holder" className={SELECT_CLASS} disabled={locked}
                      value={draft.payment_card_holder_id ?? ""}
                      onChange={(e) => patch((d) => {
                        d.payment_card_holder_id = e.target.value || null;
                      })}
                    >
                      <option value="">미지정</option>
                      {activePartners.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                    </select>
                  </Field>
                </div>

                {/* 개인카드 상환 — 비용 승인과 별개다. 승인=지급이 아니다. */}
                {reimbursement && reimbursement.status !== "NOT_REQUIRED" && (
                  <div className="rounded-md border border-[--color-border] bg-[--color-muted] px-3 py-2 text-[11px]">
                    <p className="font-medium">
                      개인카드 상환 · {REIMBURSEMENT_LABEL[reimbursement.status]}
                    </p>
                    <p className="mt-0.5 text-[--color-muted-foreground]">
                      {partners.find((p) => p.id === reimbursement.payeeId)?.name ?? "결제자 미지정"}
                      에게 {formatKRW(reimbursement.amount ?? 0)} 상환 대상입니다.
                      지급 장부가 아직 없어 <strong>지급 완료 표시는 하지 않습니다</strong>.
                    </p>
                  </div>
                )}

                {/* ── 증빙 ── */}
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label="증빙 유형" htmlFor="e-evt">
                    <select
                      id="e-evt" className={SELECT_CLASS} value={draft.evidence_type} disabled={locked}
                      onChange={(e) => patch((d) => { d.evidence_type = e.target.value; })}
                    >
                      {optionsOf("evidence_type").map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </select>
                  </Field>
                  <Field label="증빙 상태" htmlFor="e-evs">
                    <select
                      id="e-evs" className={SELECT_CLASS} value={draft.evidence_status} disabled={locked}
                      onChange={(e) => patch((d) => {
                        d.evidence_status = e.target.value as EvidenceStatus;
                      })}
                    >
                      {optionsOf("evidence_status").map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </select>
                  </Field>
                </div>
                {/* 실제 파일. **신규 등록 중에는 올릴 수 없다** — 아직 비용 id 가
                    없어서 파일을 붙일 대상이 없다. 저장 후 다시 열어 첨부한다. */}
                {isEdit ? (
                  <EvidencePanel expenseId={expenseId!} locked={locked} />
                ) : (
                  <p className="rounded-md border border-dashed border-[--color-border] p-3 text-center text-[11px] text-[--color-muted-foreground]">
                    증빙 파일은 비용을 먼저 등록한 뒤 첨부할 수 있습니다
                  </p>
                )}

                <Field label="메모" htmlFor="e-memo" hint="메모는 승인 없이 바로 저장됩니다">
                  <textarea
                    id="e-memo" rows={2} value={draft.memo ?? ""} disabled={locked}
                    onChange={(e) => patch((d) => { d.memo = e.target.value || null; })}
                    className="ops-focusable w-full rounded-md border border-[--color-border] bg-[--color-card] p-2 text-xs disabled:bg-[--color-muted]"
                  />
                </Field>

                <Field
                  label="거래처" htmlFor="e-supplier"
                  hint={isEdit ? "변경은 승인을 거칩니다" : undefined}
                >
                  <SupplierSelect
                    id="e-supplier"
                    value={draft.supplier_id}
                    valueName={
                      // 목록 조인으로 이미 아는 이름은 재조회하지 않는다.
                      // 값을 바꾼 뒤에는 새 id 의 이름을 selector 가 직접 찾는다.
                      draft.supplier_id === expense?.supplier_id
                        ? expense?.supplier_name ?? null
                        : null
                    }
                    disabled={locked}
                    onChange={(v) => patch((d) => { d.supplier_id = v; })}
                  />
                </Field>

                {/* 조정 이력 */}
                {isEdit && (adjustments?.length ?? 0) > 0 && (
                  <div>
                    <p className="mb-1.5 text-xs font-medium">이 비용에 걸린 정정 조정</p>
                    <ul className="divide-y divide-[--color-border] overflow-hidden rounded-md border border-[--color-border] text-[11px]">
                      {(adjustments ?? []).map((a) => (
                        <li key={a.id} className="flex items-start justify-between gap-2 px-3 py-2">
                          <span className="min-w-0">
                            <span className="font-medium">
                              {ADJUSTMENT_TYPE_LABEL[a.adjustment_type] ?? a.adjustment_type}
                            </span>
                            <span className="block text-[--color-muted-foreground]">
                              {a.adjustment_reason}
                            </span>
                          </span>
                          <span className="shrink-0 text-right tabular-nums">
                            <span className={a.amount_delta < 0 ? "text-[--color-danger]" : undefined}>
                              {a.amount_delta > 0 ? "+" : ""}{formatKRW(a.amount_delta)}
                            </span>
                            <span className="block text-[10px] text-[--color-muted-foreground]">
                              {a.original_period ?? "-"} → {a.adjustment_period}
                            </span>
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {isEdit && expense && (
                  <p className="text-[10px] text-[--color-muted-foreground]">
                    등록 {formatDate(expense.created_at)}
                    {expense.linked_settlement_id && " · 확정 정산에 반영됨"}
                  </p>
                )}
              </>
            )}
          </div>

          <div className="flex shrink-0 items-center justify-between gap-2 border-t border-[--color-border] bg-[--color-muted] px-4 py-3">
            <p className="text-[11px] text-[--color-muted-foreground]">
              {locked
                ? "잠긴 월이라 수정할 수 없습니다 — 조정으로 처리하세요"
                : isEdit && !ownership
                  ? "승인 정책을 불러오지 못해 저장할 수 없습니다"
                  : !dirty
                    ? "변경사항 없음"
                    : needsHolder
                      ? "개인카드는 결제자를 지정해야 합니다"
                      : classified?.hasApproval && classified.hasDirect
                        ? "일부는 바로 저장되고, 일부는 승인을 거칩니다"
                        : classified?.hasApproval
                          ? "승인을 거쳐야 장부에 반영됩니다"
                          : "저장하지 않으면 변경사항이 사라집니다"}
            </p>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>취소</Button>
              <Button
                size="sm" onClick={onSaveClick}
                disabled={
                  pending || incomplete || needsHolder || savingBlocked || (isEdit && !dirty)
                }
              >
                {pending && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />}
                {classified?.hasApproval ? "승인 요청" : isEdit ? "저장" : "등록"}
              </Button>
            </div>
          </div>
        </SheetContent>
      </Sheet>

      {/* 무엇이 승인으로 가는지 보여주고 사유를 받는다 */}
      <AlertDialog open={confirmApproval} onOpenChange={setConfirmApproval}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>승인 요청을 올릴까요?</AlertDialogTitle>
            <AlertDialogDescription>
              아래 변경은 승인을 거쳐야 장부에 반영됩니다. 승인 전까지 이 비용의 금액은
              그대로이고, 그 달의 순이익·수익배분도 바뀌지 않습니다.
            </AlertDialogDescription>
          </AlertDialogHeader>

          <div className="space-y-3 text-xs">
            {Object.entries(classified?.approval ?? {}).map(([type, fields]) => (
              <div key={type} className="rounded-md border border-[--color-border] px-3 py-2">
                <p className="font-medium">{REQUEST_TYPE_LABELS[type] ?? type}</p>
                <ul className="mt-1 space-y-0.5 text-[--color-muted-foreground]">
                  {Object.entries(fields).map(([f, v]) => (
                    <li key={f}>
                      · {EXPENSE_FIELD_LABELS[f] ?? f}
                      {": "}
                      {typeof v === "boolean"
                        ? (v ? "예" : "아니오")
                        : typeof v === "number"
                          ? formatKRW(v)
                          : EXPENSE_VALUE_LABELS[f]?.[String(v)] ?? String(v ?? "없음")}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
            {classified?.hasDirect && (
              <p className="text-[--color-muted-foreground]">
                나머지 {Object.keys(classified.direct).length}개 항목은 승인 없이 바로 저장됩니다.
              </p>
            )}
            <Field label="변경 사유" htmlFor="e-reason">
              <TextInput
                id="e-reason" value={reason}
                placeholder="예: 카드사 청구금액과 대사 후 정정"
                onChange={(e) => setReason(e.target.value)}
              />
            </Field>
          </div>

          <AlertDialogFooter>
            <AlertDialogCancel>계속 편집</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => { e.preventDefault(); save.mutate(); }}
              disabled={save.isPending}
            >
              {save.isPending && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />}
              승인 요청 올리기
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>이 비용을 삭제할까요?</AlertDialogTitle>
            <AlertDialogDescription>
              {expense?.description} · {formatKRW(expense?.gross_amount ?? 0)}
              <br />
              삭제하면 그 달의 순이익과 세무 리포트 집계에서 빠집니다. 되돌릴 수 없습니다.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>취소</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => { e.preventDefault(); remove.mutate(); }}
              disabled={remove.isPending}
            >
              {remove.isPending && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />}
              삭제
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* 열려 있을 때만 마운트한다 — 매번 새 상태로 시작해야 앞선 입력이 남지 않고,
          "열릴 때 초기화" effect 와 사용자 입력이 경합하지 않는다. */}
      {adjustOpen && (
        <ExpenseAdjustmentDialog
          expense={expense ?? null}
          open
          onOpenChange={setAdjustOpen}
        />
      )}
    </>
  );
}
