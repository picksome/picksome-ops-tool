"use client";

/**
 * 자동화 설정.
 *
 * AI 설정과 나눈 이유
 * ----------------
 * 여기 있는 것은 **AI 없이 도는 일**이다. 한 화면에 합치면 "AI 를 안 켜면
 * 자동화도 안 되나" 로 읽힌다. AI 접근은 `설정 → AI · API` 에 따로 있다.
 *
 * 모드를 숨기지 않는다
 * -----------------
 * 켜기/끄기 토글 하나로 만들면 사용자는 무엇이 저절로 바뀌는지 모른 채 켠다.
 * 관찰만 할지, 승인 요청을 만들지, 직접 실행할지를 매번 고르게 한다.
 *
 * 한도 없는 자동 실행을 만들지 않는다
 * -----------------------------
 * `자동 실행` 을 골라도 한도를 비워 두면 서버가 제안으로 떨어뜨린다. 화면도
 * 그 사실을 미리 말한다 — 켜 놓고 "왜 실행이 안 되지" 를 묻지 않도록.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Ban, Play, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PageShell, Section } from "@/components/shell/page-shell";
import { ApiError } from "@/lib/api";
import {
  GUARDRAIL_HELP, GUARDRAIL_LABEL, MODE_HELP, MODE_LABEL, OUTCOME_LABEL,
  RUN_STATUS_LABEL,
  automationApi, isRate,
  type AutomationMode, type AutomationPolicy,
} from "@/lib/api/automation";
import { ORDER_SYNC_REASON_LABEL, externalApi } from "@/lib/api/external";
import { formatDateTime } from "@/lib/format";

function errorMessage(err: unknown, action: string): string {
  if (!(err instanceof ApiError)) return `${action} 중 통신에 실패했습니다.`;
  if (err.status === 403) return `${action} 권한이 없습니다.`;
  return err.message || `${action}에 실패했습니다.`;
}

export default function AutomationSettingsPage() {
  const qc = useQueryClient();
  const { data: policies, isLoading } = useQuery({
    queryKey: ["automation", "policies"],
    queryFn: () => automationApi.policies(),
  });
  const { data: types } = useQuery({
    queryKey: ["automation", "types"],
    queryFn: () => automationApi.types(),
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["automation"] });

  return (
    <PageShell
      title="자동화"
      description="Seller OS 가 스스로 확인하는 일을 정합니다. AI 없이 동작합니다"
      variant="detail"
    >
      <Section title="자동화 항목">
        {isLoading ? (
          <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
        ) : (
          <div className="space-y-2">
            {(policies?.items ?? []).map((p) => (
              <PolicyCard key={p.automation_type} policy={p} onSaved={invalidate} />
            ))}
          </div>
        )}
      </Section>

      {/* "왜 지급 자동화가 없지" 를 묻지 않도록 제외 목록을 보여준다. */}
      <Section title="자동으로 하지 않는 일">
        <div className="rounded-lg border border-[--color-border] p-3">
          <p className="mb-2 flex items-center gap-1.5 text-[12px]">
            <ShieldCheck className="h-3.5 w-3.5 text-[--color-muted-foreground]" aria-hidden />
            아래는 <strong>사람이 화면에서</strong> 처리합니다. 자동화 경로가 아예 없습니다.
          </p>
          <ul className="space-y-1">
            {(types?.never_automated ?? []).map((n) => (
              <li key={n.action} className="flex items-start gap-1.5 text-[11px] text-[--color-muted-foreground]">
                <Ban className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
                <span><span className="font-mono">{n.action}</span> — {n.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      </Section>

      <OrderSyncStatus />

      <RunHistory />
    </PageShell>
  );
}

function PolicyCard({
  policy, onSaved,
}: {
  policy: AutomationPolicy;
  onSaved: () => void;
}) {
  const [enabled, setEnabled] = React.useState(policy.enabled);
  const [mode, setMode] = React.useState<AutomationMode>(policy.mode);
  const [interval, setInterval] = React.useState<string>(
    policy.interval_minutes != null ? String(policy.interval_minutes) : "",
  );
  const [guardrails, setGuardrails] = React.useState<Record<string, string>>(
    policy.guardrail_json ?? {},
  );

  const save = useMutation({
    mutationFn: () =>
      automationApi.savePolicy({
        automation_type: policy.automation_type,
        enabled, mode,
        interval_minutes: interval.trim() ? Number(interval) : null,
        guardrails: Object.fromEntries(
          Object.entries(guardrails).filter(([, v]) => String(v).trim() !== ""),
        ),
      }),
    onSuccess: () => {
      onSaved();
      toast.success(`${policy.label} 설정을 저장했습니다.`);
    },
    onError: (e) => toast.error(errorMessage(e, "자동화 설정")),
  });

  const runNow = useMutation({
    mutationFn: () => automationApi.run(policy.id as string),
    onSuccess: (r) => {
      onSaved();
      const parts = Object.entries(r.outcomes ?? {})
        .map(([k, v]) => `${OUTCOME_LABEL[k as never] ?? k} ${v}`);
      toast.success(parts.length ? parts.join(" · ") : "처리할 대상이 없었습니다.");
    },
    onError: (e) => toast.error(errorMessage(e, "지금 실행")),
  });

  // 자동 실행인데 한도가 비었으면 서버가 제안으로 떨어뜨린다.
  const guardrailsEmpty = Object.values(guardrails)
    .every((v) => String(v ?? "").trim() === "");
  const willDowngrade = mode === "AUTO_EXECUTE" && guardrailsEmpty;

  return (
    <div className="rounded-lg border border-[--color-border] p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="text-[13px] font-medium">{policy.label}</span>
            <Badge
              variant={policy.enabled ? "success" : "default"}
              className="px-1.5 py-0 text-[10px]"
            >
              {policy.enabled ? "켜짐" : "꺼짐"}
            </Badge>
            {policy.enabled && (
              <Badge variant="info" className="px-1.5 py-0 text-[10px]">
                {MODE_LABEL[policy.mode]}
              </Badge>
            )}
          </div>
          <p className="mt-1 text-[11px] text-[--color-muted-foreground]">
            {policy.description}
          </p>
          <p className="mt-0.5 text-[11px] text-[--color-muted-foreground]">
            {policy.last_run_at
              ? `마지막 실행 ${formatDateTime(policy.last_run_at)}`
              : "아직 실행한 적 없음"}
            {policy.interval_minutes
              ? ` · ${policy.interval_minutes}분마다`
              : " · 자동 주기 없음(수동 실행만)"}
          </p>
        </div>

        {policy.id && (
          <Button
            variant="outline" size="sm" disabled={runNow.isPending}
            onClick={() => runNow.mutate()}
          >
            <Play className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            지금 실행
          </Button>
        )}
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-3">
        <div className="space-y-1">
          <Label htmlFor={`en-${policy.automation_type}`} className="text-xs">
            사용
          </Label>
          <select
            id={`en-${policy.automation_type}`}
            value={enabled ? "on" : "off"}
            onChange={(e) => setEnabled(e.target.value === "on")}
            className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
          >
            <option value="off">끔</option>
            <option value="on">켬</option>
          </select>
        </div>

        <div className="space-y-1">
          <Label htmlFor={`md-${policy.automation_type}`} className="text-xs">
            무엇까지 할까요
          </Label>
          <select
            id={`md-${policy.automation_type}`} value={mode}
            onChange={(e) => setMode(e.target.value as AutomationMode)}
            className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
          >
            {policy.supported_modes.map((m) => (
              <option key={m} value={m}>{MODE_LABEL[m]}</option>
            ))}
          </select>
        </div>

        <div className="space-y-1">
          <Label htmlFor={`iv-${policy.automation_type}`} className="text-xs">
            실행 주기(분)
          </Label>
          <Input
            id={`iv-${policy.automation_type}`} value={interval}
            onChange={(e) => setInterval(e.target.value)}
            placeholder="비우면 수동만" inputMode="numeric"
            className="h-8 text-sm"
          />
        </div>
      </div>

      <p className="mt-1.5 text-[11px] text-[--color-muted-foreground]">
        {MODE_HELP[mode]}
      </p>

      {policy.available_guardrails.length > 0 && (
        <div className="mt-3">
          <p className="mb-1.5 text-[11px] font-medium">허용 범위</p>
          <div className="grid gap-2 sm:grid-cols-2">
            {policy.available_guardrails.map((key) => (
              <div key={key} className="space-y-1">
                <Label htmlFor={`g-${policy.automation_type}-${key}`} className="text-xs">
                  {GUARDRAIL_LABEL[key] ?? key}
                  {isRate(key) && (
                    <span className="ml-1 text-[10px] text-[--color-muted-foreground]">
                      0~1 (0.2 = 20%)
                    </span>
                  )}
                </Label>
                <Input
                  id={`g-${policy.automation_type}-${key}`}
                  value={guardrails[key] ?? ""}
                  onChange={(e) =>
                    setGuardrails((g) => ({ ...g, [key]: e.target.value }))}
                  placeholder="비우면 판단하지 않음"
                  inputMode="decimal" className="h-8 text-sm"
                />
                {GUARDRAIL_HELP[key] && (
                  <p className="text-[10px] text-[--color-muted-foreground]">
                    {GUARDRAIL_HELP[key]}
                  </p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {willDowngrade && (
        // 켜 놓고 "왜 실행이 안 되지" 를 묻지 않도록 미리 말한다.
        <p className="mt-2 rounded-md border border-[--color-warning] bg-[--color-surface-muted] px-2.5 py-1.5 text-[11px]">
          허용 범위를 정하지 않으면 자동 실행하지 않고 <strong>승인 요청까지만</strong>
          {" "}만듭니다. 범위 없는 자동 실행은 허용하지 않습니다.
        </p>
      )}

      <div className="mt-3 flex justify-end">
        <Button size="sm" disabled={save.isPending} onClick={() => save.mutate()}>
          저장
        </Button>
      </div>
    </div>
  );
}

function OrderSyncStatus() {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["order-sync", "status"],
    queryFn: () => externalApi.orderSyncStatus(),
  });

  const run = useMutation({
    mutationFn: (accountId: string) => externalApi.runOrderSync(accountId),
    onSuccess: (res) => {
      const out = res.items?.[0];
      // 이미 돌고 있으면 성공했다고 말하지 않는다.
      if (out?.status === "SKIPPED") {
        toast.warning(
          `동기화하지 않았습니다 — ${ORDER_SYNC_REASON_LABEL[out.reason ?? ""] ?? out.reason}`,
        );
      } else if (out?.status === "SUCCESS") {
        toast.success(
          `주문 ${out.fetched}건 확인, 신규 ${out.created}건, 되살림 ${out.replayed}건`,
        );
      } else {
        toast.error(
          `동기화가 ${out?.status} 로 끝났습니다 — ` +
          `${ORDER_SYNC_REASON_LABEL[out?.reason ?? ""] ?? out?.reason ?? "원인 미상"}`,
        );
      }
      qc.invalidateQueries({ queryKey: ["order-sync"] });
      qc.invalidateQueries({ queryKey: ["automation"] });
    },
    onError: (err) => toast.error(errorMessage(err, "주문 동기화")),
  });

  if (!data) return null;

  return (
    <Section title="주문 동기화 상태">
      <div className="space-y-2">
        {/* 주문이 자동으로 들어와도 취소·반품은 아직 반영되지 않는다.
            한 덩어리로 보이면 사용자는 정산이 맞는다고 믿는다. */}
        {!data.claim_ingest_implemented && (
          <p className="rounded-md border border-[--color-border] bg-[--color-surface-muted] px-2.5 py-1.5 text-[11px]">
            주문만 자동으로 들어옵니다. <strong>취소·반품·교환은 아직 반영되지
            않습니다</strong> — 판매처에서 확인해 주세요.
          </p>
        )}

        {data.accounts.length === 0 && (
          <p className="text-sm text-[--color-muted-foreground]">
            주문을 가져올 외부 계정이 없습니다.
          </p>
        )}

        {data.accounts.map((a) => (
          <div key={a.external_account_id}
               className="rounded-lg border border-[--color-border] p-3">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-sm font-medium">{a.display_name}</p>
                <p className="text-[11px] text-[--color-muted-foreground]">
                  {a.configured
                    ? `${a.initial_sync_from} 이후 · 최근 ${a.overlap_days}일 겹쳐 읽기`
                    : "자동 수집 대상으로 설정되지 않았습니다"}
                </p>
              </div>
              <div className="flex items-center gap-2">
                {a.blocked_by && (
                  <Badge variant="warning" className="text-[10px]">
                    {ORDER_SYNC_REASON_LABEL[a.blocked_by] ?? a.blocked_by}
                  </Badge>
                )}
                <Button size="sm" variant="outline"
                        disabled={!a.configured || run.isPending}
                        onClick={() => run.mutate(a.external_account_id)}>
                  <Play className="mr-1 h-3 w-3" aria-hidden />
                  지금 동기화
                </Button>
              </div>
            </div>

            {/* 마지막 시도와 마지막 성공을 **따로** 보여준다. 합치면
                몇 시간째 못 읽고 있는 상태가 성공 시각 뒤에 숨는다. */}
            <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] sm:grid-cols-4">
              <div>
                <dt className="text-[--color-muted-foreground]">마지막 시도</dt>
                <dd>{a.last_attempt_at ? formatDateTime(a.last_attempt_at) : "없음"}</dd>
              </div>
              <div>
                <dt className="text-[--color-muted-foreground]">마지막 성공</dt>
                <dd>{a.last_success_at ? formatDateTime(a.last_success_at) : "없음"}</dd>
              </div>
              <div>
                <dt className="text-[--color-muted-foreground]">결과</dt>
                <dd>
                  {a.last_status
                    ? (RUN_STATUS_LABEL[a.last_status] ?? a.last_status)
                    : "기록 없음"}
                  {a.last_error_code && (
                    <span className="ml-1 text-[--color-danger]">
                      ({ORDER_SYNC_REASON_LABEL[a.last_error_code] ?? a.last_error_code})
                    </span>
                  )}
                </dd>
              </div>
              <div>
                <dt className="text-[--color-muted-foreground]">최근 건수</dt>
                <dd>
                  {a.last_counts
                    ? `조회 ${a.last_counts.fetched ?? 0} · 신규 ${a.last_counts.created ?? 0}`
                    : "—"}
                </dd>
              </div>
            </dl>
          </div>
        ))}
      </div>
    </Section>
  );
}

function RunHistory() {
  const { data } = useQuery({
    queryKey: ["automation", "runs"],
    queryFn: () => automationApi.runs({ limit: 20 }),
  });
  const runs = data?.items ?? [];
  if (runs.length === 0) return null;

  return (
    <Section title="최근 실행">
      <div className="ops-scroll-x">
        <table className="w-full text-[12px]">
          <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
            <tr>
              <th scope="col" className="px-2 py-1.5 text-left font-semibold">시각</th>
              <th scope="col" className="px-2 py-1.5 text-left font-semibold">항목</th>
              <th scope="col" className="px-2 py-1.5 text-center font-semibold">모드</th>
              <th scope="col" className="px-2 py-1.5 text-center font-semibold">상태</th>
              <th scope="col" className="px-2 py-1.5 text-right font-semibold">검토</th>
              <th scope="col" className="px-2 py-1.5 text-left font-semibold">결과</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[--color-border]">
            {runs.map((r) => (
              <tr key={r.id}>
                <td className="whitespace-nowrap px-2 py-1.5 tabular-nums">
                  {formatDateTime(r.started_at)}
                </td>
                <td className="px-2 py-1.5">{r.name}</td>
                <td className="px-2 py-1.5 text-center text-[11px]">
                  {MODE_LABEL[r.mode]}
                </td>
                <td className="px-2 py-1.5 text-center">
                  <Badge
                    variant={
                      r.status === "SUCCESS" ? "success"
                        : r.status === "FAILED" ? "danger"
                          : r.status === "SKIPPED" ? "default" : "warning"
                    }
                    className="px-1.5 py-0 text-[10px]"
                  >
                    {RUN_STATUS_LABEL[r.status] ?? r.status}
                  </Badge>
                </td>
                {/* null 은 아직 안 끝난 것이다 — 0 과 다르다. */}
                <td className="px-2 py-1.5 text-right tabular-nums">
                  {r.evaluated_count ?? "—"}
                </td>
                <td className="px-2 py-1.5 text-[11px]">
                  {Object.keys(r.outcomes ?? {}).length === 0 ? (
                    <span className="text-[--color-muted-foreground]">해당 없음</span>
                  ) : (
                    Object.entries(r.outcomes).map(([k, v]) => (
                      <span
                        key={k}
                        className={k === "BLOCKED" ? "text-[--color-warning]" : ""}
                      >
                        {OUTCOME_LABEL[k as never] ?? k} {v}{" "}
                      </span>
                    ))
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}
