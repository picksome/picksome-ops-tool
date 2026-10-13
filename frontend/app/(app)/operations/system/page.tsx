"use client";

/**
 * 시스템 상태 (Phase 52).
 *
 * 왜 필요한가
 * ---------
 * worker 가 안 도는 것을 관리자가 알아차릴 방법이 없었다. `/operations/workers`
 * API 는 있었지만 화면이 없어서, 승인이 자동 적용되지 않거나 증빙 삭제가 밀려도
 * DB 를 직접 봐야 알 수 있었다.
 *
 * 이 화면이 지키는 것
 * ----------------
 * · **화면이 임계값을 만들지 않는다.** "3시간 넘으면 빨간색" 같은 규칙을 여기서
 *   정하면 그 숫자의 근거를 아무도 모른다. 상태 판단은 서버가 하고
 *   (`worker_runtime.derive_state`) 화면은 그 코드를 번역만 한다
 * · **stack trace · SQL · 저장 경로 · 비밀값을 표시하지 않는다.** 서버가 애초에
 *   오류 코드와 한 줄 메시지만 저장한다(`worker_runtime.short_error`)
 * · "대기 0건" 이 좋은 소식인지 아닌지 구분한다 — 이슈 스캔은 큐 개념이 없다
 * · 실행된 적 없음과 정상을 섞지 않는다
 */

import { useState } from "react";
import Link from "next/link";
import type { Route } from "next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle, ArrowRight, CircleAlert, CircleCheck, CircleDashed,
  Clock, Loader2, Play, RefreshCw,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PageShell, Section } from "@/components/shell/page-shell";
import { ApiError } from "@/lib/api";
import { externalApi } from "@/lib/api/external";
import {
  WORKER_STATE_LABEL, operationsApi, workerLabel,
  type WorkerHealth, type WorkerState,
} from "@/lib/api/operations";
import { useCurrentUser } from "@/hooks/useCurrentUser";
import { formatDateTime, formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";

/** 상태별 표시. 색만으로 구분하지 않으려고 아이콘과 글자를 함께 쓴다. */
const STATE_STYLE: Record<
  WorkerState,
  { icon: typeof CircleCheck; tone: string; variant: "success" | "warning" | "danger" | "info" }
> = {
  IDLE: { icon: CircleCheck, tone: "text-[--color-success]", variant: "success" },
  RUNNING: { icon: Loader2, tone: "text-[--color-info]", variant: "info" },
  PENDING_QUEUE: { icon: Clock, tone: "text-[--color-warning]", variant: "warning" },
  NEVER_RAN: { icon: CircleDashed, tone: "text-[--color-muted-foreground]", variant: "warning" },
  LAST_RUN_FAILED: { icon: CircleAlert, tone: "text-[--color-danger]", variant: "danger" },
  STALE_RUNNING: { icon: AlertTriangle, tone: "text-[--color-danger]", variant: "danger" },
};

/** 각 worker 가 무슨 일을 하는지. 이름만으로는 알 수 없다. */
const WORKER_PURPOSE: Record<string, string> = {
  due_approvals: "승인된 요청을 실제 데이터에 적용합니다. 멈추면 승인해도 반영되지 않습니다",
  attachment_delete: "삭제 표시된 증빙 파일을 디스크에서 지웁니다",
  channel_publish: "확정된 판매가를 판매처에 반영합니다 (커넥터 연결 시)",
  period_recalc: "잠긴 달 조정 등으로 더러워진 기간을 다시 집계합니다. 멈추면 월 마감이 막힙니다",
  operation_issue_scan: "운영 이슈를 찾아냅니다. 멈추면 문제가 있어도 이슈가 생기지 않습니다",
};

function errorMessage(err: unknown, action: string): string {
  if (!(err instanceof ApiError)) return `${action} 중 통신에 실패했습니다.`;
  if (err.status === 403) return `${action} 권한이 없습니다. 관리자만 실행할 수 있습니다.`;
  return err.message || `${action}에 실패했습니다.`;
}

function formatAge(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}초`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}분`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}시간`;
  return `${Math.round(seconds / 86400)}일`;
}

export default function SystemStatusPage() {
  const qc = useQueryClient();
  const { user } = useCurrentUser();
  const isAdmin = user?.role === "ADMIN";
  const [selected, setSelected] = useState<string | null>(null);

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["worker-health"],
    queryFn: () => operationsApi.workers(),
    // worker 는 초 단위로 바뀌지 않는다. 자동 갱신은 하지 않고 사람이 누르게 한다 —
    // 관리 화면이 계속 요청을 던지면 그것 자체가 부하다.
    staleTime: 30_000,
  });

  const { data: runs } = useQuery({
    queryKey: ["worker-runs", selected],
    queryFn: () => operationsApi.workerRuns({ worker_name: selected ?? undefined, limit: 30 }),
    enabled: isAdmin,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["worker-health"] });
    qc.invalidateQueries({ queryKey: ["worker-runs"] });
  };

  const runOne = useMutation({
    mutationFn: (name: string) => operationsApi.runWorker(name),
    onSuccess: (r) => {
      invalidate();
      if (r.status === "FAILED") {
        // 실패를 성공처럼 보여주지 않는다. 서버는 200 으로 응답하지만 결과는 실패다.
        toast.error(
          `${workerLabel(r.worker)} 실행 실패 — ${r.error_code ?? "알 수 없는 오류"}`,
        );
        return;
      }
      toast.success(
        `${workerLabel(r.worker)} 실행 완료 — 처리 ${r.processed ?? 0}건, 성공 ${r.success ?? 0}건`
        + (r.failure ? `, 실패 ${r.failure}건` : ""),
      );
    },
    onError: (e) => toast.error(errorMessage(e, "worker 실행")),
  });

  const reclaim = useMutation({
    mutationFn: () => operationsApi.reclaimStale(),
    onSuccess: (r) => {
      invalidate();
      toast.success(
        r.reclaimed > 0
          ? `중단된 실행 기록 ${r.reclaimed}건을 정리했습니다`
          : "정리할 중단 기록이 없습니다",
      );
    },
    onError: (e) => toast.error(errorMessage(e, "중단 기록 정리")),
  });

  const workers = data?.workers ?? [];
  const problems = workers.filter(
    (w) => w.state === "LAST_RUN_FAILED" || w.state === "STALE_RUNNING" || w.state === "NEVER_RAN",
  );

  return (
    <PageShell
      title="시스템 상태"
      description="백그라운드 worker 가 실제로 돌고 있는지 확인합니다"
      variant="detail"
      actions={
        <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => refetch()}>
            <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", isFetching && "animate-spin")} aria-hidden />
            새로고침
          </Button>
          {isAdmin && (
            <Button size="sm" variant="outline" onClick={() => reclaim.mutate()} disabled={reclaim.isPending}>
              중단 기록 정리
            </Button>
          )}
        </div>
      }
    >
      {error instanceof ApiError && error.status === 403 ? (
        // 권한 없음을 일반 오류로 보여주면 "고장났나?" 를 먼저 의심하게 된다.
        // 무엇이 부족한지 명시한다.
        <Section title="worker 상태">
          <div className="flex flex-col items-center gap-2 py-10 text-center">
            <CircleAlert className="h-7 w-7 text-[--color-muted-foreground]" aria-hidden />
            <p className="text-sm">worker 실행과 실행 이력 조회는 관리자만 할 수 있습니다.</p>
            <p className="max-w-md text-xs text-[--color-muted-foreground]">
              백그라운드 작업 상태는 시스템 운영 정보라 관리자 권한이 필요합니다.
              처리해야 할 업무는{" "}
              <Link href={"/operations/issues" as Route} className="underline">운영 이슈</Link>{" "}
              화면에서 볼 수 있습니다.
            </p>
          </div>
        </Section>
      ) : error ? (
        <Section title="worker 상태">
          <p role="alert" className="rounded-md border border-[--color-danger] bg-[--color-danger-bg] p-3 text-sm">
            {errorMessage(error, "상태 조회")}
          </p>
        </Section>
      ) : isLoading ? (
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
      ) : (
        <div className="space-y-4">
          {problems.length > 0 && (
            <section
              aria-label="확인이 필요한 worker"
              className="rounded-lg border border-[--color-warning] bg-[--color-warning-bg] p-3"
            >
              <p className="text-[13px] font-semibold text-[--color-warning]">
                확인이 필요한 worker {problems.length}개
              </p>
              <ul className="mt-1 space-y-0.5">
                {problems.map((w) => (
                  <li key={w.worker_name} className="text-[11px] text-[--color-warning]">
                    {workerLabel(w.worker_name)} — {WORKER_STATE_LABEL[w.state]}
                    {w.state === "NEVER_RAN" && " (스케줄러가 연결되지 않았을 수 있습니다)"}
                  </li>
                ))}
              </ul>
            </section>
          )}

          <Section
            title="백그라운드 worker"
            description={
              `실행이 멈춰 있으면 승인 적용·가격 반영·이슈 탐지가 함께 멈춥니다`
              + ` · 중단 판정 기준 ${data?.stale_run_threshold_minutes ?? "—"}분`
            }
          >
            <ul className="space-y-2">
              {workers.map((w) => (
                <WorkerCard
                  key={w.worker_name}
                  worker={w}
                  isAdmin={isAdmin}
                  onRun={() => runOne.mutate(w.worker_name)}
                  running={runOne.isPending && runOne.variables === w.worker_name}
                  selected={selected === w.worker_name}
                  onSelect={() =>
                    setSelected((prev) => (prev === w.worker_name ? null : w.worker_name))
                  }
                />
              ))}
            </ul>
          </Section>

          <DataFreshness />

          {!isAdmin && (
            <p className="rounded-md bg-[--color-muted] p-2 text-[11px] text-[--color-muted-foreground]">
              worker 실행과 실행 이력 조회는 관리자만 할 수 있습니다.
            </p>
          )}

          {isAdmin && (
            <Section
              title={selected ? `${workerLabel(selected)} 실행 이력` : "최근 실행 이력"}
              description={
                selected
                  ? "다른 worker 를 보려면 위에서 카드를 다시 누르세요"
                  : "worker 카드를 누르면 그 worker 만 볼 수 있습니다"
              }
            >
              <RunTable rows={runs?.items ?? []} showWorker={!selected} />
            </Section>
          )}

          <p className="flex items-start gap-1.5 rounded-md bg-[--color-muted] p-2 text-[10px] leading-relaxed text-[--color-muted-foreground]">
            <ArrowRight className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
            <span>
              worker 는 외부 스케줄러(cron/systemd)가 <code>python -m app.scripts.run_workers --once</code>
              를 주기적으로 부르는 구조입니다. 여기서 누르는 실행 버튼은{" "}
              <strong>같은 코드를 한 번 돌립니다</strong> — 스케줄러를 대신하지는 않습니다.
              절차는 <Link href={"/operations/issues" as Route} className="underline">운영 이슈</Link>{" "}
              화면과 운영 런북(docs/49)을 참고하세요.
            </span>
          </p>
        </div>
      )}
    </PageShell>
  );
}

/**
 * 데이터 최신성 — **서비스가 떠 있는 것과 다른 사실이다.**
 *
 * 개인 PC 로 운영하면 앱은 방금 켰지만 데이터는 며칠 전 것일 수 있다.
 * worker 상태만 보여주면 "worker 는 도는데 왜 숫자가 안 맞지" 를 답할 수 없다.
 *
 * 여기서 지키는 것
 * --------------
 * · **마지막 시도와 마지막 성공을 따로 보여준다.** 합치면 몇 시간째 못 읽고
 *   있는 상태가 성공 시각 뒤에 숨는다
 * · **주문과 클레임을 한 덩어리로 묶지 않는다.** 다른 endpoint 이고 따로
 *   켠다 — 주문이 들어온다고 취소·반품까지 들어오는 것은 아니다(M057)
 * · 여기서 임계값을 만들지 않는다. "몇 시간 넘으면 빨간색" 같은 판단은
 *   화면이 정할 일이 아니다. 사실(시각·상태 코드)만 옮긴다
 *
 * 백업 상태는 여기 없다
 * -------------------
 * 백업은 파일 시스템에 있고 이 API 는 그것을 모른다. 백업 경로를 읽는
 * endpoint 를 새로 열지 않았다 — 로컬 운영에서는
 * `scripts\status_seller_os.ps1` 이 그 답을 준다(docs/85).
 */
function DataFreshness() {
  const orders = useQuery({
    queryKey: ["order-sync-status"],
    queryFn: () => externalApi.orderSyncStatus(),
    staleTime: 30_000,
  });
  const claims = useQuery({
    queryKey: ["claim-sync-status"],
    queryFn: () => externalApi.claimSyncStatus(),
    staleTime: 30_000,
  });

  if (orders.isError || claims.isError) {
    // 권한이 없거나 통신이 안 된다. **최신이라고 말하지 않는다.**
    return (
      <Section title="데이터 최신성">
        <p className="text-xs text-[--color-muted-foreground]">
          동기화 상태를 확인하지 못했습니다. 앱이 열렸다고 데이터가 최신인 것은 아닙니다.
        </p>
      </Section>
    );
  }

  const rows = [
    ...(orders.data?.accounts ?? []).map((a) => ({
      key: `order-${a.external_account_id}`,
      kind: "주문 동기화",
      account: a.display_name,
      configured: a.configured,
      blockedBy: a.blocked_by,
      lastSuccessAt: a.last_success_at,
      lastAttemptAt: a.last_attempt_at,
      lastStatus: a.last_status,
      lastErrorCode: a.last_error_code,
    })),
    ...(claims.data?.accounts ?? []).map((a) => ({
      key: `claim-${a.external_account_id}`,
      kind: "취소·반품 수집",
      account: a.display_name,
      configured: a.configured,
      blockedBy: a.blocked_by,
      lastSuccessAt: a.last_success_at,
      lastAttemptAt: a.last_attempt_at,
      lastStatus: a.last_status,
      lastErrorCode: a.last_error_code,
    })),
  ];

  return (
    <Section
      title="데이터 최신성"
      description="앱이 떠 있는 것과 데이터가 최신인 것은 다릅니다 · PC 를 꺼 둔 동안은 수집되지 않습니다"
    >
      {orders.isLoading || claims.isLoading ? (
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-[--color-muted-foreground]">
          연결된 외부 계정이 없습니다.
        </p>
      ) : (
        <ul className="space-y-2">
          {rows.map((r) => {
            const ok = r.lastStatus === "SUCCESS";
            return (
              <li
                key={r.key}
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-[--color-border] p-2.5"
              >
                <div className="min-w-0">
                  <p className="text-[13px] font-medium">{r.kind}</p>
                  <p className="text-[11px] text-[--color-muted-foreground]">{r.account}</p>
                </div>
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
                  <span className="text-[--color-muted-foreground]">
                    마지막 성공{" "}
                    <strong className="font-medium text-[--color-foreground]">
                      {r.lastSuccessAt ? formatRelative(r.lastSuccessAt) : "없음"}
                    </strong>
                  </span>
                  <span className="text-[--color-muted-foreground]">
                    마지막 시도{" "}
                    <strong className="font-medium text-[--color-foreground]">
                      {r.lastAttemptAt ? formatRelative(r.lastAttemptAt) : "없음"}
                    </strong>
                  </span>
                  {!r.configured ? (
                    <Badge variant="warning">설정 필요</Badge>
                  ) : r.blockedBy ? (
                    <Badge variant="warning">{r.blockedBy}</Badge>
                  ) : ok ? (
                    <Badge variant="success">정상</Badge>
                  ) : r.lastStatus ? (
                    <Badge variant="danger">{r.lastErrorCode ?? r.lastStatus}</Badge>
                  ) : (
                    <Badge variant="warning">실행된 적 없음</Badge>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
      <p className="mt-2 text-[10px] leading-relaxed text-[--color-muted-foreground]">
        PC 를 켠 뒤 <code>Seller OS 시작</code> 을 실행하면 꺼져 있던 동안의 주문·클레임을
        자동으로 따라잡습니다. 마지막 성공이 오래됐다면{" "}
        <Link href={"/operations/exceptions" as Route} className="underline">예외 센터</Link>
        에서 원인을 확인하세요.
      </p>
    </Section>
  );
}

function WorkerCard({
  worker, isAdmin, onRun, running, selected, onSelect,
}: {
  worker: WorkerHealth;
  isAdmin: boolean;
  onRun: () => void;
  running: boolean;
  selected: boolean;
  onSelect: () => void;
}) {
  const style = STATE_STYLE[worker.state];
  const Icon = style.icon;

  return (
    <li
      className={cn(
        "rounded-lg border border-[--color-border] p-3",
        selected && "border-[--color-accent] bg-[--color-muted]/40",
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <button
          type="button"
          onClick={onSelect}
          className="ops-focusable min-w-0 flex-1 text-left"
          aria-pressed={selected}
        >
          <div className="flex items-center gap-2">
            <Icon
              className={cn("h-4 w-4 shrink-0", style.tone, worker.state === "RUNNING" && "animate-spin")}
              aria-hidden
            />
            <span className="text-[13px] font-medium">{workerLabel(worker.worker_name)}</span>
            <Badge variant={style.variant} className="px-1.5 py-0 text-[10px]">
              {WORKER_STATE_LABEL[worker.state]}
            </Badge>
          </div>
          <p className="mt-0.5 text-[11px] text-[--color-muted-foreground]">
            {WORKER_PURPOSE[worker.worker_name] ?? worker.worker_name}
          </p>
        </button>

        {isAdmin && (
          <Button size="sm" variant="outline" onClick={onRun} disabled={running}>
            <Play className="mr-1.5 h-3 w-3" aria-hidden />
            지금 실행
          </Button>
        )}
      </div>

      <dl className="mt-2.5 grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] sm:grid-cols-4">
        <Fact
          label="마지막 실행"
          value={worker.last_started_at ? formatRelative(worker.last_started_at) : "없음"}
          title={worker.last_started_at ? formatDateTime(worker.last_started_at) : undefined}
        />
        <Fact
          label="마지막 성공"
          value={worker.last_success_at ? formatRelative(worker.last_success_at) : "없음"}
          title={worker.last_success_at ? formatDateTime(worker.last_success_at) : undefined}
        />
        <Fact
          label="대기 건수"
          // 큐 개념이 없는 worker 의 "0건" 은 좋은 소식도 나쁜 소식도 아니다.
          value={worker.has_queue ? `${worker.queue_depth}건` : "해당 없음"}
        />
        <Fact
          label="가장 오래된 대기"
          value={worker.has_queue ? formatAge(worker.oldest_pending_age_seconds) : "해당 없음"}
        />
      </dl>

      {worker.last_error_code && (
        <p className="mt-2 rounded-md bg-[--color-danger-bg] p-2 text-[11px] text-[--color-danger]">
          <span className="font-semibold">{worker.last_error_code}</span>
          {worker.last_error_message && ` — ${worker.last_error_message}`}
          {worker.last_failure_at && (
            <span className="ml-1 opacity-80">({formatDateTime(worker.last_failure_at)})</span>
          )}
        </p>
      )}
    </li>
  );
}

function Fact({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div>
      <dt className="text-[--color-muted-foreground]">{label}</dt>
      <dd className="tabular-nums" title={title}>{value}</dd>
    </div>
  );
}

function RunTable({
  rows, showWorker,
}: {
  rows: Array<{
    id: string; worker_name: string; trigger: string; status: string;
    started_at: string; duration_ms: number | null;
    processed_count: number | null; success_count: number | null;
    failure_count: number | null; skipped_count: number | null;
    error_code: string | null; error_message: string | null;
  }>;
  showWorker: boolean;
}) {
  if (rows.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-[--color-border] p-6 text-center text-xs text-[--color-muted-foreground]">
        실행 기록이 없습니다. 스케줄러가 아직 한 번도 돌지 않았습니다.
      </p>
    );
  }

  return (
    <div className="ops-scroll-x">
      <table className="w-full text-[12px]">
        <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
          <tr>
            <th scope="col" className="px-3 py-1.5 text-left font-semibold">시작</th>
            {showWorker && (
              <th scope="col" className="px-3 py-1.5 text-left font-semibold">worker</th>
            )}
            <th scope="col" className="px-3 py-1.5 text-left font-semibold">실행 주체</th>
            <th scope="col" className="px-3 py-1.5 text-left font-semibold">결과</th>
            <th scope="col" className="px-3 py-1.5 text-right font-semibold">처리</th>
            <th scope="col" className="px-3 py-1.5 text-right font-semibold">소요</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-t border-[--color-border]">
              <td className="px-3 py-1.5 whitespace-nowrap">{formatDateTime(r.started_at)}</td>
              {showWorker && <td className="px-3 py-1.5">{workerLabel(r.worker_name)}</td>}
              <td className="px-3 py-1.5 text-[--color-muted-foreground]">{r.trigger}</td>
              <td className="px-3 py-1.5">
                <Badge
                  variant={
                    r.status === "SUCCESS" ? "success" : r.status === "RUNNING" ? "info" : "danger"
                  }
                  className="px-1.5 py-0 text-[10px]"
                >
                  {r.status === "SUCCESS" ? "성공" : r.status === "RUNNING" ? "실행 중" : "실패"}
                </Badge>
                {r.error_code && (
                  <span className="ml-1.5 text-[10px] text-[--color-danger]">{r.error_code}</span>
                )}
              </td>
              <td className="ops-num px-3 py-1.5">
                {r.processed_count ?? 0}
                {(r.failure_count ?? 0) > 0 && (
                  <span className="text-[--color-danger]"> (실패 {r.failure_count})</span>
                )}
              </td>
              <td className="ops-num px-3 py-1.5">
                {r.duration_ms === null ? "—" : `${(r.duration_ms / 1000).toFixed(1)}초`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
