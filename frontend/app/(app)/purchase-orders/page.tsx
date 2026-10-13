"use client";

/**
 * 발주.
 *
 * 초안은 주문이 아니다
 * -----------------
 * 자동화가 만든 것이든 사람이 만든 것이든 `DRAFT` 는 아직 아무 데도 안 나갔다.
 * "발주함" 으로 옮기는 것은 **돈을 쓰기로 결정하는 일**이라 화면에서 분명히
 * 구분한다 — 자동 제안에는 배지를 붙이고, 확정 버튼은 따로 둔다.
 *
 * 입고 수량을 사용자 입력으로 두는 이유
 * -------------------------------
 * "전량 입고" 버튼만 두면 실제로 덜 온 것을 다 왔다고 기록하게 된다. 실물과
 * 다른 장부는 쓸모가 없으므로 **온 만큼** 적게 한다.
 *
 * 입력 유실을 만들지 않는다
 * ---------------------
 * 입고 수량은 서버 응답으로 덮어쓰지 않는다. 다른 창에서 발주가 바뀌어도
 * 사용자가 치던 숫자를 지우지 않는다 — 이 저장소에서 이미 한 번 겪은 결함이다.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, PackageCheck, Plus } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PageShell, Section } from "@/components/shell/page-shell";
import {
  PO_STATUS_LABEL,
  purchaseOrderApi,
  type PurchaseOrderStatus,
} from "@/lib/api/inventory";
import { PurchaseOrderCreate } from "@/components/purchasing/purchase-order-create";
import { formatKRW } from "@/lib/format";

const OPEN: PurchaseOrderStatus[] = [
  "DRAFT",
  "ORDERED",
  "PARTIALLY_RECEIVED",
];

export default function PurchaseOrdersPage() {
  const [openId, setOpenId] = React.useState<string | null>(null);
  const [creating, setCreating] = React.useState(false);

  const list = useQuery({
    queryKey: ["purchase-orders"],
    queryFn: () => purchaseOrderApi.list(),
  });

  const orders = list.data?.items ?? [];
  const open = orders.filter((o) => OPEN.includes(o.status));
  const closed = orders.filter((o) => !OPEN.includes(o.status));

  return (
    <PageShell
      title="발주"
      variant="detail"
      actions={
        <Button size="sm" onClick={() => setCreating(true)} className="gap-1">
          <Plus className="size-4" />
          발주 초안 만들기
        </Button>
      }
    >
      <PurchaseOrderCreate open={creating} onOpenChange={setCreating} />

      <Section title={`진행 중 ${open.length}건`}>
        {open.length === 0 ? (
          <p className="text-sm text-[--color-muted-foreground]">
            진행 중인 발주가 없습니다.
          </p>
        ) : (
          <ul className="space-y-2">
            {open.map((o) => (
              <li key={o.id}>
                <button
                  type="button"
                  onClick={() => setOpenId(openId === o.id ? null : o.id)}
                  className="flex w-full items-center justify-between gap-3 rounded-md border border-[--color-border] px-3 py-2 text-left text-sm hover:bg-[--color-muted]"
                >
                  <span className="flex min-w-0 items-center gap-2">
                    <span className="font-mono text-xs">{o.business_key}</span>
                    <span className="truncate">{o.supplier_name ?? "—"}</span>
                    {o.created_by_automation && (
                      <Badge variant="info" className="gap-1">
                        <Bot className="size-3" />
                        자동 제안
                      </Badge>
                    )}
                  </span>
                  <span className="flex shrink-0 items-center gap-3">
                    <span className="tabular-nums text-[--color-muted-foreground]">
                      {o.line_count}품목 · {formatKRW(o.total_amount)}
                    </span>
                    <Badge
                      variant={o.status === "DRAFT" ? "info" : "default"}
                    >
                      {PO_STATUS_LABEL[o.status]}
                    </Badge>
                  </span>
                </button>
                {openId === o.id && <OrderDetail orderId={o.id} />}
              </li>
            ))}
          </ul>
        )}
      </Section>

      {closed.length > 0 && (
        <Section title={`완료 ${closed.length}건`}>
          <ul className="space-y-1">
            {closed.map((o) => (
              <li
                key={o.id}
                className="flex items-center justify-between rounded-md px-3 py-1.5 text-sm text-[--color-muted-foreground]"
              >
                <span className="truncate">
                  <span className="font-mono text-xs">{o.business_key}</span>{" "}
                  {o.supplier_name ?? "—"}
                </span>
                <span className="shrink-0">
                  {formatKRW(o.total_amount)} · {PO_STATUS_LABEL[o.status]}
                </span>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </PageShell>
  );
}

function OrderDetail({ orderId }: { orderId: string }) {
  const qc = useQueryClient();
  const { data: order } = useQuery({
    queryKey: ["purchase-orders", orderId],
    queryFn: () => purchaseOrderApi.get(orderId),
  });

  // 입고 수량은 **사용자 입력**이라 서버 응답으로 덮지 않는다.
  // 발주가 바뀌었을 때만 비운다 — 그 판단 기준은 orderId 뿐이다.
  const [qty, setQty] = React.useState<Record<string, string>>({});
  React.useEffect(() => {
    setQty({});
  }, [orderId]);

  // 재전송 방지 키. 이 폼이 열려 있는 동안 하나로 고정한다 — 버튼을 두 번
  // 눌러도 재고가 두 번 늘지 않는다.
  const receiptKey = React.useMemo(
    () => `${orderId}:${Date.now()}`,
    [orderId],
  );

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["purchase-orders"] });
    void qc.invalidateQueries({ queryKey: ["inventory"] });
  };

  const confirm = useMutation({
    mutationFn: () => purchaseOrderApi.setStatus(orderId, "ORDERED"),
    onSuccess: invalidate,
  });

  const receive = useMutation({
    mutationFn: () =>
      purchaseOrderApi.receive(
        orderId,
        Object.entries(qty)
          .filter(([, v]) => Number(v) > 0)
          .map(([line_id, v]) => ({ line_id, quantity: Number(v) })),
        receiptKey,
      ),
    onSuccess: () => {
      setQty({});
      invalidate();
    },
  });

  if (!order) {
    return (
      <p className="px-3 py-2 text-sm text-[--color-muted-foreground]">
        불러오는 중…
      </p>
    );
  }

  const receivable = order.status === "ORDERED" || order.status === "PARTIALLY_RECEIVED";
  const hasQty = Object.values(qty).some((v) => Number(v) > 0);

  return (
    <div className="mt-1 rounded-md border border-[--color-border] bg-[--color-muted]/30 p-3">
      {order.memo && (
        <p className="mb-3 text-xs text-[--color-muted-foreground]">{order.memo}</p>
      )}
      <table className="w-full text-sm">
        <thead className="text-left text-xs text-[--color-muted-foreground]">
          <tr>
            <th className="pb-1 font-medium">품목</th>
            <th className="pb-1 text-right font-medium">발주</th>
            <th className="pb-1 text-right font-medium">입고</th>
            <th className="pb-1 text-right font-medium">단가</th>
            {receivable && <th className="pb-1 text-right font-medium">이번 입고</th>}
          </tr>
        </thead>
        <tbody>
          {order.lines.map((ln) => (
            <tr key={ln.id} className="border-t border-[--color-border]">
              <td className="max-w-0 truncate py-1.5 pr-2">{ln.product_name}</td>
              <td className="py-1.5 text-right tabular-nums">{ln.quantity}</td>
              <td className="py-1.5 text-right tabular-nums">
                {ln.received_quantity}
              </td>
              <td className="py-1.5 text-right tabular-nums">
                {formatKRW(ln.unit_price)}
              </td>
              {receivable && (
                <td className="py-1.5 pl-2 text-right">
                  <Input
                    type="number"
                    min={0}
                    value={qty[ln.id] ?? ""}
                    onChange={(e) =>
                      setQty((prev) => ({ ...prev, [ln.id]: e.target.value }))
                    }
                    className="ml-auto h-8 w-20 text-right"
                    placeholder="0"
                  />
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>

      <div className="mt-3 flex items-center justify-between gap-3">
        <span className="text-sm tabular-nums">
          합계 {formatKRW(order.total_amount)}
        </span>
        <div className="flex gap-2">
          {order.status === "DRAFT" && (
            <Button
              size="sm"
              onClick={() => confirm.mutate()}
              disabled={confirm.isPending}
            >
              {/* 여기서 돈을 쓰기로 결정한다. 문구를 부드럽게 쓰지 않는다. */}
              발주 확정
            </Button>
          )}
          {receivable && (
            <Button
              size="sm"
              variant="secondary"
              onClick={() => receive.mutate()}
              disabled={!hasQty || receive.isPending}
              className="gap-1"
            >
              <PackageCheck className="size-4" />
              입고 처리
            </Button>
          )}
        </div>
      </div>

      {receive.isError && (
        <p className="mt-2 text-sm text-[--color-destructive]">
          입고에 실패했습니다. 이미 처리된 입고일 수 있으니 목록을 새로고침해
          확인하세요.
        </p>
      )}
    </div>
  );
}
