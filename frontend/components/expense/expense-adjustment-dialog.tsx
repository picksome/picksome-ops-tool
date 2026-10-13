"use client";

import * as React from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Loader2 } from "lucide-react";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Field } from "@/components/shell/page-shell";
import { MoneyInput, TextInput } from "@/components/ui/money-input";
import { describeApprovalError } from "@/lib/api/approvals";
import {
  ADJUSTMENT_TYPE_LABEL, expenseApprovalApi, type AdjustmentType,
} from "@/lib/api/expenses-approval";
import type { ExpenseOut } from "@/lib/api/expenses";
import { formatKRW } from "@/lib/format";
import { todayKst } from "@/lib/date-range";

const SELECT_CLASS =
  "ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs";

/**
 * 잠긴 달 비용의 정정 조정.
 *
 * **원본은 건드리지 않는다.** 지금 열린 달에 차액만큼의 비용 행을 만들고 원본을 가리킨다.
 * 그래서 확정된 달의 순이익·수익배분은 그대로 남고, 차액은 이번 달 손익에 반영된다.
 *
 * 금액은 **차액**이다 — 원본 금액을 그대로 넣으면 두 배가 된다. 화면이 계산 결과를
 * 미리 보여줘서 그 실수를 막는다.
 */
export function ExpenseAdjustmentDialog({
  expense, open, onOpenChange,
}: {
  expense: ExpenseOut | null;
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const qc = useQueryClient();

  /**
   * 초기화는 **마운트 시점 한 번**이다. 호출부가 열릴 때만 이 컴포넌트를 렌더하므로
   * 열 때마다 새 상태로 시작한다.
   *
   * `open` 이 바뀔 때 effect 로 비우면, 열리자마자 입력한 값이 뒤늦게 도는 초기화에
   * 지워진다 — E2E 가 이 경합을 잡았다(입력은 다 했는데 버튼이 계속 비활성).
   */
  const [type, setType] = React.useState<AdjustmentType>("CORRECTION");
  const [reason, setReason] = React.useState("");
  const [date, setDate] = React.useState(todayKst);
  const [delta, setDelta] = React.useState<number | null>(null);

  const create = useMutation({
    mutationFn: () =>
      expenseApprovalApi.createAdjustment(expense!.id, {
        adjustment_type: type,
        adjustment_reason: reason.trim(),
        adjustment_date: date,
        amount_delta: delta ?? 0,
      }),
    onSuccess: () => {
      toast.success("정정 조정을 만들었습니다", {
        description: "원본 비용은 그대로이고, 차액이 조정한 달의 손익에 반영됩니다.",
      });
      qc.invalidateQueries({ queryKey: ["expenses"] });
      qc.invalidateQueries({ queryKey: ["expense-adjustments", expense?.id] });
      onOpenChange(false);
    },
    onError: (err) => {
      const g = describeApprovalError(err);
      toast.error(g.title, { description: g.description });
    },
  });

  // 0 원 조정은 서버가 거부한다. 사유 없는 조정은 나중에 아무도 설명하지 못한다.
  const invalid = !reason.trim() || delta === null || delta === 0 || !date;

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>정정 조정을 만들까요?</AlertDialogTitle>
          <AlertDialogDescription>
            원본 비용은 바뀌지 않습니다. 조정한 달에 차액만큼의 비용이 새로 생기고,
            원본과 연결됩니다.
          </AlertDialogDescription>
        </AlertDialogHeader>

        <div className="space-y-3 text-xs">
          {expense && (
            <div className="rounded-md border border-[--color-border] px-3 py-2">
              <p className="font-medium">{expense.description}</p>
              <p className="mt-0.5 text-[--color-muted-foreground]">
                원본 {expense.expense_date} · {formatKRW(expense.gross_amount)}
              </p>
            </div>
          )}

          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="조정 유형" required htmlFor="adj-type">
              <select
                id="adj-type" className={SELECT_CLASS} value={type}
                onChange={(e) => setType(e.target.value as AdjustmentType)}
              >
                {(Object.keys(ADJUSTMENT_TYPE_LABEL) as AdjustmentType[]).map((t) => (
                  <option key={t} value={t}>{ADJUSTMENT_TYPE_LABEL[t]}</option>
                ))}
              </select>
            </Field>
            <Field
              label="조정 귀속일" required htmlFor="adj-date"
              hint="열린 달이어야 합니다"
            >
              <TextInput
                id="adj-date" type="date" value={date}
                onChange={(e) => setDate(e.target.value)}
              />
            </Field>
          </div>

          <Field
            label="차액" required htmlFor="adj-delta"
            hint={
              delta === null || delta === 0
                ? "원본에 더할 금액입니다. 되돌리려면 음수를 넣습니다"
                : `이 조정으로 ${formatKRW(Math.abs(delta))}이 ${delta > 0 ? "늘어납니다" : "줄어듭니다"}` +
                  ` (실질 합계 ${formatKRW((expense?.gross_amount ?? 0) + delta)})`
            }
          >
            <MoneyInput id="adj-delta" value={delta} onChange={setDelta} allowNegative />
          </Field>

          <Field
            label="조정 사유" required htmlFor="adj-reason"
            hint="나중에 이 숫자를 설명할 사람은 이 문장만 봅니다"
          >
            <TextInput
              id="adj-reason" value={reason}
              placeholder="예: 7월 광고비 세금계산서 누락분 반영"
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
        </div>

        <AlertDialogFooter>
          <AlertDialogCancel>취소</AlertDialogCancel>
          <AlertDialogAction
            onClick={(e) => { e.preventDefault(); create.mutate(); }}
            disabled={invalid || create.isPending}
          >
            {create.isPending && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />}
            조정 만들기
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
