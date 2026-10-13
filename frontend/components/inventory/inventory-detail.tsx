"use client";

/**
 * 재고 상세 — 원장과 조정.
 *
 * 잔량을 덮어쓰는 입력을 만들지 않는다
 * -------------------------------
 * "현재 재고 = 12" 를 입력받으면, 두 사람이 동시에 세었을 때 한쪽 결과가 조용히
 * 사라진다. 그리고 나중에 "왜 12 가 됐나" 에 답할 수 없다.
 *
 * 그래서 입력은 항상 **차이**다.
 *
 * ```
 * 실사 결과 9개인데 장부가 12개
 * → 입력: 감소 3, 사유 "실사 차이"
 * → 원장: ADJUSTMENT -3
 * ```
 *
 * 사유가 필수인 이유
 * ---------------
 * 조정의 근거는 원장 밖에 있다(사람이 세어 봤다). 사유가 없으면 몇 달 뒤에
 * 아무도 왜 숫자가 바뀌었는지 모른다. 서버가 거부하고, 화면도 막는다.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  TXN_LABEL,
  inventoryApi,
  type InventoryItem,
} from "@/lib/api/inventory";
import { formatKRW } from "@/lib/format";

export function InventoryDetail({ item }: { item: InventoryItem }) {
  const qc = useQueryClient();
  const [mode, setMode] = React.useState<"increase" | "decrease">("decrease");
  const [amount, setAmount] = React.useState("");
  const [reason, setReason] = React.useState("");

  const { data: ledger } = useQuery({
    queryKey: ["inventory", "txn", item.product_id, item.product_variant_id],
    queryFn: () =>
      inventoryApi.transactions(item.product_id, item.product_variant_id),
  });

  const adjust = useMutation({
    mutationFn: () =>
      inventoryApi.adjust({
        product_id: item.product_id,
        product_variant_id: item.product_variant_id,
        // 화면은 방향과 크기를 따로 받고, 여기서 부호를 만든다. 사용자가
        // 마이너스를 직접 치게 하면 부호를 빠뜨려 반대로 조정하게 된다.
        quantity_delta:
          mode === "decrease" ? -Math.abs(Number(amount)) : Math.abs(Number(amount)),
        reason: reason.trim(),
      }),
    onSuccess: () => {
      setAmount("");
      setReason("");
      void qc.invalidateQueries({ queryKey: ["inventory"] });
    },
  });

  const canSubmit = Number(amount) > 0 && reason.trim().length > 0;

  return (
    <div className="mt-1 space-y-4 rounded-md border border-[--color-border] bg-[--color-muted]/30 p-3">
      {/* ── 현재 상태 ─────────────────────────────────────────── */}
      <div className="flex flex-wrap gap-4 text-sm">
        <Stat label="현재 보유" value={String(item.on_hand)} />
        {/* 예약은 셀 수 없다. 0 이라고 쓰지 않는다. */}
        <Stat label="예약" value="주문 연동 안 됨" muted />
        <Stat label="판매가능" value="계산 불가" muted />
        <Stat
          label="입고예정"
          value={item.inbound > 0 ? String(item.inbound) : "없음"}
          muted={item.inbound === 0}
        />
      </div>

      {/* ── 조정 ─────────────────────────────────────────────── */}
      <div>
        <p className="mb-2 text-sm font-medium">재고 조정</p>
        <p className="mb-2 text-xs text-[--color-muted-foreground]">
          목표 수량이 아니라 <strong>차이</strong>를 입력합니다. 실사 결과가 장부보다
          3개 적으면 «감소 3».
        </p>
        <div className="flex flex-wrap items-end gap-2">
          <div className="flex overflow-hidden rounded-md border border-[--color-border]">
            {(["decrease", "increase"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                className={`px-3 py-2 text-sm ${
                  mode === m ? "bg-[--color-muted] font-medium" : ""
                }`}
              >
                {m === "decrease" ? "감소" : "증가"}
              </button>
            ))}
          </div>
          <Input
            type="number"
            min={1}
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            placeholder="수량"
            className="w-24"
          />
          <Input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="사유 (필수)"
            className="w-64"
          />
          <Button
            size="sm"
            disabled={!canSubmit || adjust.isPending}
            onClick={() => adjust.mutate()}
          >
            조정
          </Button>
        </div>
        {!canSubmit && (amount || reason) && (
          <p className="mt-1 text-xs text-[--color-muted-foreground]">
            수량과 사유가 모두 필요합니다.
          </p>
        )}
        {adjust.isError && (
          <p className="mt-1 text-sm text-[--color-destructive]">
            조정에 실패했습니다.
          </p>
        )}
        {adjust.data?.negative && (
          <p className="mt-1 text-sm text-[--color-destructive]">
            조정 후 재고가 {adjust.data.on_hand_after}개(음수)입니다. 0 으로
            맞추지 않고 그대로 기록했습니다 — 원인을 확인하세요.
          </p>
        )}
      </div>

      {/* ── 원장 ─────────────────────────────────────────────── */}
      <div>
        <p className="mb-2 text-sm font-medium">원장</p>
        {!ledger || ledger.length === 0 ? (
          <p className="text-sm text-[--color-muted-foreground]">
            기록이 없습니다.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-[--color-muted-foreground]">
                <tr className="border-b border-[--color-border]">
                  <th className="py-1 pr-3 font-medium">일시</th>
                  <th className="py-1 pr-3 font-medium">유형</th>
                  <th className="py-1 pr-3 text-right font-medium">증감</th>
                  <th className="py-1 pr-3 text-right font-medium">취득단가</th>
                  <th className="py-1 pr-3 font-medium">사유</th>
                  <th className="py-1 font-medium">기록자</th>
                </tr>
              </thead>
              <tbody>
                {ledger.map((t) => (
                  <tr
                    key={t.id}
                    className="border-b border-[--color-border] last:border-0"
                  >
                    <td className="py-1 pr-3 text-xs">
                      {t.occurred_at.slice(0, 16).replace("T", " ")}
                    </td>
                    <td className="py-1 pr-3">
                      <Badge variant="info">{TXN_LABEL[t.transaction_type]}</Badge>
                    </td>
                    <td
                      className={`py-1 pr-3 text-right tabular-nums ${
                        t.quantity_delta < 0 ? "" : "font-medium"
                      }`}
                    >
                      {t.quantity_delta > 0 ? "+" : ""}
                      {t.quantity_delta}
                    </td>
                    <td className="py-1 pr-3 text-right tabular-nums text-[--color-muted-foreground]">
                      {/* 취득이 아닌 움직임은 단가가 없다. 0 이 아니라 «—» */}
                      {t.unit_cost === null ? "—" : formatKRW(t.unit_cost)}
                    </td>
                    <td className="max-w-0 truncate py-1 pr-3 text-xs text-[--color-muted-foreground]">
                      {t.reason ?? "—"}
                    </td>
                    <td className="py-1 text-xs text-[--color-muted-foreground]">
                      {t.actor_name}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  muted,
}: {
  label: string;
  value: string;
  muted?: boolean;
}) {
  return (
    <div>
      <span className="block text-xs text-[--color-muted-foreground]">
        {label}
      </span>
      <span
        className={
          muted
            ? "text-sm text-[--color-muted-foreground]"
            : "text-sm font-medium tabular-nums"
        }
      >
        {value}
      </span>
    </div>
  );
}
