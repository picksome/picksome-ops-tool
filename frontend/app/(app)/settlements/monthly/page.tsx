"use client";

/**
 * 월 마감 목록.
 *
 * 확정월과 진행중인 달을 같은 컬럼에 섞지 않는다
 * ------------------------------------------
 * 확정된 달은 snapshot 의 숫자를, 진행중인 달은 실시간 집계를 보여준다.
 * 둘을 한 컬럼에 넣으면 "7월 순이익" 이 매일 달라 보인다.
 * 진행중인 달에는 "잠정" 을 명시한다 — 색만으로 구분하지 않는다.
 */

import { useQuery } from "@tanstack/react-query";
import { ColumnDef } from "@tanstack/react-table";
import { ChevronRight, Lock, CheckCircle2, CircleDashed, Eye } from "lucide-react";
import { useRouter } from "next/navigation";
import { DataTable } from "@/components/table/data-table";
import { ProfitText } from "@/components/ui/finance";
import {
  monthlyCloseApi,
  type CloseState,
  type MonthCloseSummary,
} from "@/lib/api/settlements";

function fmtYm(ym: string) {
  const [y, m] = ym.split("-");
  return `${y}년 ${parseInt(m)}월`;
}

const STATE_LABEL: Record<CloseState, string> = {
  OPEN: "진행중",
  REVIEW: "검토중",
  CONFIRMED: "확정",
  LOCKED: "잠금",
};

const STATE_ICON: Record<CloseState, typeof Lock> = {
  OPEN: CircleDashed,
  REVIEW: Eye,
  CONFIRMED: CheckCircle2,
  LOCKED: Lock,
};

/**
 * 상태 배지. **색만으로 상태를 구분하지 않는다** — 아이콘과 글자를 함께 쓴다.
 */
function CloseStateBadge({ state }: { state: CloseState }) {
  const Icon = STATE_ICON[state] ?? CircleDashed;
  const tone =
    state === "LOCKED"
      ? "text-[--color-muted-foreground]"
      : state === "CONFIRMED"
        ? "text-[--color-success]"
        : state === "REVIEW"
          ? "text-[--color-warning]"
          : "text-[--color-foreground]";
  return (
    <span className={`inline-flex items-center gap-1 text-xs ${tone}`}>
      <Icon className="h-3.5 w-3.5" aria-hidden />
      {STATE_LABEL[state] ?? state}
    </span>
  );
}

const COLUMNS: ColumnDef<MonthCloseSummary>[] = [
  {
    accessorKey: "year_month",
    header: "정산월",
    cell: ({ getValue }) => (
      <span className="font-medium">{fmtYm(getValue<string>())}</span>
    ),
    size: 110,
  },
  {
    accessorKey: "close_state",
    header: "마감 상태",
    cell: ({ getValue }) => <CloseStateBadge state={getValue<CloseState>()} />,
    size: 100,
  },
  {
    id: "profit",
    header: "순이익",
    cell: ({ row }) => {
      const { snapshot_net_profit, net_profit } = row.original;
      const confirmed = snapshot_net_profit !== null;
      return (
        <div className="flex flex-col">
          <ProfitText value={confirmed ? snapshot_net_profit! : net_profit} />
          <span className="text-[11px] text-[--color-muted-foreground]">
            {confirmed ? "확정" : "잠정"}
          </span>
        </div>
      );
    },
    size: 140,
  },
  {
    id: "distributable",
    header: "분배 가능",
    cell: ({ row }) => {
      const { snapshot_distributable_profit, distributable_amount } = row.original;
      const v = snapshot_distributable_profit ?? distributable_amount;
      return <ProfitText value={v} />;
    },
    size: 130,
  },
  {
    accessorKey: "snapshot_profit_basis",
    header: "정산 대사",
    cell: ({ getValue }) => {
      const v = getValue<string | null>();
      if (v === null) {
        return <span className="text-xs text-[--color-muted-foreground]">-</span>;
      }
      // 정산이 연결되지 않은 것을 "이상 없음" 으로 보이게 하지 않는다.
      return v === "SETTLEMENT_RECONCILED" ? (
        <span className="text-xs text-[--color-success]">대사 완료</span>
      ) : (
        <span className="text-xs text-[--color-warning]">정산 미연결</span>
      );
    },
    size: 110,
  },
  {
    accessorKey: "close_generation",
    header: "확정 세대",
    cell: ({ row }) => {
      const { close_generation, reopen_count } = row.original;
      if (!close_generation) {
        return <span className="text-xs text-[--color-muted-foreground]">-</span>;
      }
      return (
        <span className="text-xs text-[--color-muted-foreground]">
          {close_generation}세대
          {reopen_count > 0 ? ` · 재개 ${reopen_count}회` : ""}
        </span>
      );
    },
    size: 120,
  },
  {
    accessorKey: "locked_at",
    header: "잠금일",
    cell: ({ row }) => {
      const v = row.original.locked_at ?? row.original.confirmed_at;
      return (
        <span className="text-xs text-[--color-muted-foreground]">
          {v ? v.slice(0, 10) : "-"}
        </span>
      );
    },
    size: 100,
  },
  {
    id: "action",
    header: "",
    cell: () => (
      <ChevronRight className="h-4 w-4 text-[--color-muted-foreground]" aria-hidden />
    ),
    size: 36,
  },
];

export default function MonthlyClosePage() {
  const router = useRouter();

  const { data, isLoading, error } = useQuery({
    queryKey: ["monthly-close", "list"],
    queryFn: () => monthlyCloseApi.list(24),
  });

  const items = data?.items ?? [];

  return (
    <div className="flex h-full flex-col gap-4 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">월 마감</h1>
          <p className="text-sm text-[--color-muted-foreground]">
            검증 → 검토 → 확정 → 잠금. 확정하면 그 시점 숫자가 박제되고,
            잠근 뒤에는 재오픈 승인 없이 바꿀 수 없습니다.
          </p>
        </div>
      </div>

      {error ? (
        <div
          role="alert"
          className="rounded-md border border-[--color-danger] bg-[--color-danger-bg] p-3 text-sm"
        >
          월 마감 목록을 불러오지 못했습니다. 잠시 후 다시 시도하세요.
        </div>
      ) : null}

      <div className="flex-1 overflow-hidden">
        <DataTable
          columns={COLUMNS}
          data={items}
          isLoading={isLoading}
          onRowClick={(row) => router.push(`/settlements/monthly/${row.year_month}`)}
          emptyText="마감 이력이 없습니다. 월별 정산에서 먼저 계산하세요."
        />
      </div>
    </div>
  );
}
