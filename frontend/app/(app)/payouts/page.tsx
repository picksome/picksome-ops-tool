"use client";

/**
 * 지급 장부 (Phase 32~34).
 *
 * 화면이 지키는 것
 * -------------
 * · **회계 효과를 항목마다 표시한다.** 동업자 배분을 비용처럼 보여주면
 *   사용자가 손익을 잘못 이해한다
 * · **지급 완료는 지급 기록이 있어야만 된다.** "완료 처리" 버튼을 두지 않고,
 *   금액을 입력받아 기록한다
 * · 부분 지급을 지원하고 남은 금액을 항상 보여준다
 * · 개인카드 상환이 새 비용을 만들지 않는다는 사실을 안내에 명시한다
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Banknote, CreditCard, Plus, Wallet } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { MoneyText } from "@/components/ui/finance";
import { ApiError } from "@/lib/api";
import {
  ACCOUNTING_EFFECT_LABEL,
  BATCH_STATUS_LABEL,
  ITEM_STATUS_LABEL,
  ITEM_TYPE_LABEL,
  payoutsApi,
  type PayoutBatch,
  type PayoutItem,
} from "@/lib/api/payouts";

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function errorMessage(err: unknown, action: string): string {
  if (!(err instanceof ApiError)) return `${action} 중 통신에 실패했습니다.`;
  const d = err.details as Record<string, unknown> | undefined;
  if (typeof d?.outstanding === "number") {
    return `남은 지급액은 ${d.outstanding.toLocaleString("ko-KR")}원입니다. 그 이하로 입력하세요.`;
  }
  if (err.status === 403) return `${action} 권한이 없습니다.`;
  if (err.status === 409) return err.message || "상태가 이미 바뀌었습니다.";
  return err.message || `${action}에 실패했습니다.`;
}

export default function PayoutsPage() {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [payTarget, setPayTarget] = useState<PayoutItem | null>(null);
  const [payAmount, setPayAmount] = useState("");
  const [payDate, setPayDate] = useState(today());
  const [payRef, setPayRef] = useState("");
  const [reimbOpen, setReimbOpen] = useState(false);
  const [selectedExpenses, setSelectedExpenses] = useState<Set<string>>(new Set());

  const { data: list, isLoading } = useQuery({
    queryKey: ["payouts", "list"],
    queryFn: () => payoutsApi.list({ limit: 50 }),
  });

  const { data: batch } = useQuery({
    queryKey: ["payouts", selectedId],
    queryFn: () => payoutsApi.get(selectedId!),
    enabled: !!selectedId,
  });

  const { data: candidates } = useQuery({
    queryKey: ["payouts", "reimbursement-candidates"],
    queryFn: () => payoutsApi.reimbursementCandidates(),
    enabled: reimbOpen,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["payouts"] });

  const approve = useMutation({
    mutationFn: (id: string) => payoutsApi.approve(id),
    onSuccess: () => {
      invalidate();
      toast.success("지급을 승인했습니다. 이제 지급 기록을 남길 수 있습니다.");
    },
    onError: (e) => toast.error(errorMessage(e, "지급 승인")),
  });

  const pay = useMutation({
    mutationFn: (args: { itemId: string; amount: number }) =>
      payoutsApi.recordPayment(args.itemId, {
        payment_date: payDate,
        amount: args.amount,
        payment_reference: payRef.trim() || undefined,
      }),
    onSuccess: (r) => {
      invalidate();
      setPayTarget(null);
      setPayAmount("");
      setPayRef("");
      toast.success(
        r.item.outstanding_amount > 0
          ? `지급 기록 완료 — 잔액 ${r.item.outstanding_amount.toLocaleString("ko-KR")}원`
          : "지급 완료되었습니다.",
      );
    },
    onError: (e) => toast.error(errorMessage(e, "지급 기록")),
  });

  const createReimbursement = useMutation({
    mutationFn: () =>
      payoutsApi.createReimbursement({
        expense_ids: [...selectedExpenses],
        title: `개인카드 상환 (${today()})`,
      }),
    onSuccess: (b) => {
      invalidate();
      setReimbOpen(false);
      setSelectedExpenses(new Set());
      setSelectedId(b.id);
      toast.success("상환 배치를 만들었습니다. 새 비용은 생성되지 않았습니다.");
    },
    onError: (e) => toast.error(errorMessage(e, "상환 배치 생성")),
  });

  const batches = list?.items ?? [];

  return (
    <div className="flex h-full flex-col gap-4 p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">지급 장부</h1>
          <p className="text-sm text-[--color-muted-foreground]">
            계산된 배분액과 실제로 보낸 돈은 다릅니다. 지급 완료는 지급 기록이
            있어야만 됩니다.
          </p>
        </div>
        <Button size="sm" variant="outline" onClick={() => setReimbOpen(true)}>
          <CreditCard className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          개인카드 상환 만들기
        </Button>
      </div>

      <div className="grid flex-1 grid-cols-1 gap-4 overflow-hidden lg:grid-cols-[380px_1fr]">
        {/* 배치 목록 */}
        <div className="overflow-y-auto rounded-lg border border-[--color-border]">
          {isLoading ? (
            <p className="p-4 text-sm text-[--color-muted-foreground]">불러오는 중...</p>
          ) : batches.length === 0 ? (
            <div className="p-4 text-sm text-[--color-muted-foreground]">
              <p>지급 배치가 없습니다.</p>
              <p className="mt-1 text-xs">
                수익배분 화면에서 배분을 확정·승인하면 지급 배치를 만들 수 있습니다.
              </p>
            </div>
          ) : (
            <ul className="divide-y divide-[--color-border]">
              {batches.map((b) => (
                <li key={b.id}>
                  <button
                    type="button"
                    onClick={() => setSelectedId(b.id)}
                    aria-current={selectedId === b.id}
                    className={`w-full px-3 py-2.5 text-left hover:bg-[--color-surface-muted] ${
                      selectedId === b.id ? "bg-[--color-surface-muted]" : ""
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-medium">{b.title}</span>
                      <span className="text-xs text-[--color-muted-foreground]">
                        {BATCH_STATUS_LABEL[b.status]}
                      </span>
                    </div>
                    <p className="mt-0.5 text-xs text-[--color-muted-foreground]">
                      {b.business_key} · {b.item_count}건 ·{" "}
                      <MoneyText value={b.paid_total} /> /{" "}
                      <MoneyText value={b.due_total} />
                    </p>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* 배치 상세 */}
        <div className="overflow-y-auto rounded-lg border border-[--color-border] p-4">
          {!batch ? (
            <p className="text-sm text-[--color-muted-foreground]">
              왼쪽에서 지급 배치를 선택하세요.
            </p>
          ) : (
            <BatchDetail
              batch={batch}
              onApprove={() => approve.mutate(batch.id)}
              onPay={(item) => {
                setPayTarget(item);
                setPayAmount(String(item.outstanding_amount));
                setPayDate(today());
              }}
            />
          )}
        </div>
      </div>

      {/* 지급 기록 */}
      <Dialog open={!!payTarget} onOpenChange={(v) => !v && setPayTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>지급 기록</DialogTitle>
          </DialogHeader>
          {payTarget ? (
            <div className="space-y-3 py-2 text-sm">
              <div className="rounded-md border border-[--color-border] bg-[--color-surface-muted] px-3 py-2">
                <p className="font-medium">{payTarget.payee_name_snapshot}</p>
                <p className="mt-0.5 text-xs text-[--color-muted-foreground]">
                  지급 예정 <MoneyText value={payTarget.due_amount} /> · 이미 지급{" "}
                  <MoneyText value={payTarget.paid_amount} /> · 남은 금액{" "}
                  <MoneyText value={payTarget.outstanding_amount} />
                </p>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1">
                  <Label htmlFor="pay-date" className="text-xs">
                    지급일 *
                  </Label>
                  <Input
                    id="pay-date"
                    type="date"
                    value={payDate}
                    onChange={(e) => setPayDate(e.target.value)}
                    className="h-8 text-sm"
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="pay-amount" className="text-xs">
                    금액 * (남은 금액 이하)
                  </Label>
                  <Input
                    id="pay-amount"
                    type="number"
                    inputMode="numeric"
                    value={payAmount}
                    onChange={(e) => setPayAmount(e.target.value)}
                    max={payTarget.outstanding_amount}
                    className="h-8 text-sm tabular-nums"
                  />
                </div>
              </div>
              <div className="space-y-1">
                <Label htmlFor="pay-ref" className="text-xs">
                  이체 참조번호 (선택)
                </Label>
                <Input
                  id="pay-ref"
                  value={payRef}
                  onChange={(e) => setPayRef(e.target.value)}
                  placeholder="예: 은행 거래번호"
                  className="h-8 text-sm"
                />
                <p className="text-[11px] text-[--color-muted-foreground]">
                  계좌번호는 저장하지 않습니다.
                </p>
              </div>
            </div>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setPayTarget(null)}>
              취소
            </Button>
            <Button
              disabled={
                pay.isPending ||
                !payTarget ||
                !payDate ||
                Number(payAmount) <= 0 ||
                Number(payAmount) > (payTarget?.outstanding_amount ?? 0)
              }
              onClick={() =>
                payTarget &&
                pay.mutate({ itemId: payTarget.id, amount: Number(payAmount) })
              }
            >
              <Banknote className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              기록
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 개인카드 상환 */}
      <Dialog open={reimbOpen} onOpenChange={setReimbOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>개인카드 상환 배치</DialogTitle>
          </DialogHeader>
          <div className="space-y-3 py-2 text-sm">
            <p className="rounded-md border border-[--color-border] bg-[--color-surface-muted] px-3 py-2 text-xs text-[--color-muted-foreground]">
              원본 비용은 이미 손익에 반영되어 있습니다.{" "}
              <strong>상환은 새 비용을 만들지 않습니다</strong> — 카드 소유자에게
              돌려줄 돈을 기록할 뿐입니다.
            </p>
            <div className="max-h-72 overflow-y-auto">
              {!candidates?.items.length ? (
                <p className="text-sm text-[--color-muted-foreground]">
                  상환 대상 비용이 없습니다.
                </p>
              ) : (
                <ul className="space-y-1">
                  {candidates.items.map((c) => (
                    <li key={c.expense_id}>
                      <label className="flex items-start gap-2 rounded-md border border-[--color-border] px-2 py-1.5">
                        <input
                          type="checkbox"
                          className="mt-1"
                          checked={selectedExpenses.has(c.expense_id)}
                          onChange={() =>
                            setSelectedExpenses((prev) => {
                              const next = new Set(prev);
                              if (next.has(c.expense_id)) next.delete(c.expense_id);
                              else next.add(c.expense_id);
                              return next;
                            })
                          }
                        />
                        <span className="min-w-0 flex-1">
                          <span className="block text-sm">{c.description}</span>
                          <span className="block text-xs text-[--color-muted-foreground]">
                            {c.expense_date} · {c.card_holder_name ?? "소유자 미지정"} ·{" "}
                            <MoneyText value={c.gross_amount} />
                          </span>
                        </span>
                      </label>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setReimbOpen(false)}>
              취소
            </Button>
            <Button
              disabled={!selectedExpenses.size || createReimbursement.isPending}
              onClick={() => createReimbursement.mutate()}
            >
              <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              {selectedExpenses.size}건으로 배치 만들기
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function BatchDetail({
  batch,
  onApprove,
  onPay,
}: {
  batch: PayoutBatch;
  onApprove: () => void;
  onPay: (item: PayoutItem) => void;
}) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold">{batch.title}</h2>
          <p className="text-xs text-[--color-muted-foreground]">
            {batch.business_key} · {BATCH_STATUS_LABEL[batch.status]}
            {batch.period_year_month ? ` · ${batch.period_year_month}` : ""}
          </p>
        </div>
        {batch.status === "DRAFT" ? (
          <Button size="sm" onClick={onApprove}>
            <Wallet className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            지급 승인
          </Button>
        ) : null}
      </div>

      <div className="grid grid-cols-3 gap-3">
        <Tile label="지급 예정" value={batch.due_total} />
        <Tile label="지급 완료" value={batch.paid_total} />
        <Tile label="남은 금액" value={batch.outstanding_total} />
      </div>

      {batch.memo ? (
        <p className="rounded-md border border-[--color-border] bg-[--color-surface-muted] px-3 py-2 text-xs text-[--color-muted-foreground]">
          {batch.memo}
        </p>
      ) : null}

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-[--color-border] text-left text-xs text-[--color-muted-foreground]">
              <th scope="col" className="py-2">받는 사람</th>
              <th scope="col">유형</th>
              <th scope="col">회계 효과</th>
              <th scope="col" className="text-right">지급 예정</th>
              <th scope="col" className="text-right">지급됨</th>
              <th scope="col" className="text-right">남은 금액</th>
              <th scope="col">상태</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {batch.items.map((item) => (
              <tr key={item.id} className="border-b border-[--color-border]">
                <td className="py-2">{item.payee_name_snapshot}</td>
                <td className="text-xs">{ITEM_TYPE_LABEL[item.item_type]}</td>
                <td className="text-xs text-[--color-muted-foreground]">
                  {ACCOUNTING_EFFECT_LABEL[item.accounting_effect]}
                </td>
                <td className="text-right tabular-nums">
                  <MoneyText value={item.due_amount} />
                </td>
                <td className="text-right tabular-nums">
                  <MoneyText value={item.paid_amount} />
                </td>
                <td className="text-right tabular-nums">
                  <MoneyText value={item.outstanding_amount} />
                </td>
                <td className="text-xs">{ITEM_STATUS_LABEL[item.status]}</td>
                <td className="text-right">
                  {batch.status !== "DRAFT" &&
                  item.outstanding_amount > 0 &&
                  item.status !== "CANCELLED" ? (
                    <Button size="sm" variant="outline" onClick={() => onPay(item)}>
                      지급 기록
                    </Button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {batch.status === "DRAFT" ? (
        <p className="text-xs text-[--color-muted-foreground]">
          승인 전에는 지급을 기록할 수 없습니다.
        </p>
      ) : null}
    </div>
  );
}

function Tile({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border border-[--color-border] px-3 py-2">
      <p className="text-xs text-[--color-muted-foreground]">{label}</p>
      <p className="text-sm font-semibold tabular-nums">
        <MoneyText value={value} />
      </p>
    </div>
  );
}
