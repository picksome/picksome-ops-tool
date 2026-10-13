"use client";

/**
 * 쇼핑몰 연동 (Phase 61 mock 감사).
 *
 * ⚠️ 이전 화면은 **외부 API 성공 mock 이었다.**
 *
 * ```
 * connect(mk, {apiKey})  → 메모리 플래그를 켜고 status: "OK"
 * sync(mk, "products")   → productCount: 42     ← 지어낸 숫자
 * sync(mk, "orders")     → orderCount: 118      ← 지어낸 숫자
 * ```
 *
 * 쿠팡·네이버 API 자격증명도 커넥터 구현도 **하나도 없다.** 그런데 화면은
 * "연결됨 · 상품 42건 동기화 완료" 를 보여줬다. 사용자는 주문이 자동으로
 * 들어오고 있다고 믿게 되고, 실제로는 아무것도 들어오지 않는다.
 *
 * 지금 이 화면이 보여주는 것은 **실제로 존재하는 두 가지**뿐이다.
 *   ① 판매처별 가격 전송 커넥터 지원 여부 — 서버 레지스트리가 답한다 (전부 미지원)
 *   ② 봇 연동 계정 — 실제로 발급되고 실제로 주문을 받는 경로다
 */

import { useState } from "react";
import Link from "next/link";
import type { Route } from "next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CircleAlert, Info, KeyRound, Plug, Plus } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PageShell, Section, Field } from "@/components/shell/page-shell";
import { TextInput } from "@/components/ui/money-input";
import { ApiError } from "@/lib/api";
import { channelPricesApi } from "@/lib/api/channel-prices";
import { integrationsApi, type IntegrationAccountListItem } from "@/lib/api/integrations";
import { useSaleChannels } from "@/hooks/useSaleChannels";
import { formatDateTime } from "@/lib/format";

function errorMessage(err: unknown, action: string): string {
  if (!(err instanceof ApiError)) return `${action} 중 통신에 실패했습니다.`;
  if (err.status === 403) return `${action} 권한이 없습니다.`;
  return err.message || `${action}에 실패했습니다.`;
}

export default function MarketplacesPage() {
  return (
    <PageShell
      title="쇼핑몰 연동"
      description="지금 실제로 연결돼 있는 것과 아직 안 되는 것을 구분해서 보여줍니다"
      variant="detail"
    >
      <div className="space-y-4">
        <ConnectorSupportSection />
        <BotAccountsSection />
      </div>
    </PageShell>
  );
}

/* ────────────────────────────────────────────────────────────
 * ① 가격 전송 커넥터 — 서버가 지원 여부를 답한다
 * ──────────────────────────────────────────────────────────── */

function ConnectorSupportSection() {
  const { labelOf } = useSaleChannels();
  const { data, isLoading, error } = useQuery({
    queryKey: ["connector-support"],
    queryFn: () => channelPricesApi.connectorSupport(),
  });

  const supported = (data ?? []).filter((s) => s.price_publish_supported);

  return (
    <Section
      title="판매처 API 커넥터"
      description="확정한 판매가를 판매처에 자동 반영하는 기능입니다"
    >
      {error ? (
        <p role="alert" className="rounded-md border border-[--color-danger] bg-[--color-danger-bg] p-3 text-sm">
          {errorMessage(error, "커넥터 상태 조회")}
        </p>
      ) : isLoading ? (
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
      ) : (
        <>
          {supported.length === 0 && (
            <p className="mb-3 flex items-start gap-1.5 rounded-md bg-[--color-warning-bg] p-2 text-[11px] text-[--color-warning]">
              <CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              <span>
                <strong>연결된 판매처 API 가 하나도 없습니다.</strong> 자격증명과 커넥터 구현이
                모두 없는 상태라, 가격 반영·주문 자동 수집·정산서 자동 수신이 동작하지 않습니다.
                판매·정산 데이터는 현재 <strong>수동 입력과 봇 ingest</strong> 로만 들어옵니다.
              </span>
            </p>
          )}

          <ul className="divide-y divide-[--color-border] overflow-hidden rounded-lg border border-[--color-border]">
            {(data ?? []).map((s) => (
              <li key={s.marketplace} className="flex items-start gap-3 px-3 py-2.5">
                <Plug className="mt-0.5 h-4 w-4 shrink-0 text-[--color-muted-foreground]" aria-hidden />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-[13px] font-medium">{labelOf(s.marketplace)}</span>
                    <Badge
                      variant={s.price_publish_supported ? "success" : "warning"}
                      className="px-1.5 py-0 text-[10px]"
                    >
                      {s.price_publish_supported ? "가격 반영 가능" : "미연결"}
                    </Badge>
                  </div>
                  {s.reason && (
                    <p className="mt-0.5 text-[11px] text-[--color-muted-foreground]">{s.reason}</p>
                  )}
                </div>
              </li>
            ))}
          </ul>

          <p className="mt-2 flex items-start gap-1.5 rounded-md bg-[--color-muted] p-2 text-[10px] leading-relaxed text-[--color-muted-foreground]">
            <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
            <span>
              연결 버튼을 만들지 않았습니다. API 키 입력란만 두고 &quot;연결됨&quot; 을 표시하면,
              실제로는 아무 데도 연결되지 않았는데 사용자는 주문이 자동으로 들어온다고
              믿게 됩니다. 커넥터가 구현되면 이 표가 먼저 바뀌고, 그때 화면 버튼이 살아납니다.
            </span>
          </p>
        </>
      )}
    </Section>
  );
}

/* ────────────────────────────────────────────────────────────
 * ② 봇 연동 계정 — 실제로 동작하는 경로
 * ──────────────────────────────────────────────────────────── */

function BotAccountsSection() {
  const qc = useQueryClient();
  const { channels, labelOf } = useSaleChannels();
  const [label, setLabel] = useState("");
  const [provider, setProvider] = useState("COUPANG");
  const [issuedKey, setIssuedKey] = useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["integration-accounts"],
    queryFn: () => integrationsApi.list(),
  });

  const create = useMutation({
    mutationFn: () =>
      integrationsApi.create({
        provider,
        account_label: label.trim(),
        sale_channel_code: provider,
      }),
    onSuccess: (created) => {
      qc.invalidateQueries({ queryKey: ["integration-accounts"] });
      // 발급 키는 **이 응답에서만** 볼 수 있다. 서버가 해시만 보관한다.
      setIssuedKey(created.api_key);
      setLabel("");
      toast.success("연동 계정을 발급했습니다");
    },
    onError: (e) => toast.error(errorMessage(e, "계정 발급")),
  });

  return (
    <Section
      title="봇 연동 계정"
      description="봇·외부 배치가 주문 데이터를 넣을 때 쓰는 계정입니다. 실제로 동작합니다."
    >
      {error ? (
        <p role="alert" className="rounded-md border border-[--color-danger] bg-[--color-danger-bg] p-3 text-sm">
          {errorMessage(error, "연동 계정 조회")}
        </p>
      ) : isLoading ? (
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
      ) : (
        <>
          {(data ?? []).length === 0 ? (
            <p className="rounded-lg border border-dashed border-[--color-border] p-6 text-center text-xs text-[--color-muted-foreground]">
              발급된 연동 계정이 없습니다.
            </p>
          ) : (
            <ul className="divide-y divide-[--color-border] overflow-hidden rounded-lg border border-[--color-border]">
              {(data ?? []).map((a: IntegrationAccountListItem) => (
                <li key={a.id} className="flex items-start gap-3 px-3 py-2.5">
                  <KeyRound className="mt-0.5 h-4 w-4 shrink-0 text-[--color-muted-foreground]" aria-hidden />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-[13px] font-medium">{a.account_label}</span>
                      <Badge variant="info" className="px-1.5 py-0 text-[10px]">
                        {labelOf(a.sale_channel_code ?? a.provider)}
                      </Badge>
                      <Badge
                        variant={a.status === "ACTIVE" ? "success" : "warning"}
                        className="px-1.5 py-0 text-[10px]"
                      >
                        {a.status}
                      </Badge>
                    </div>
                    <p className="mt-0.5 text-[11px] text-[--color-muted-foreground]">
                      인증 방식 {a.auth_kind}
                      {" · "}
                      {/* 없는 시각을 "방금" 으로 보여주지 않는다. */}
                      {a.last_synced_at
                        ? `마지막 수신 ${formatDateTime(a.last_synced_at)}`
                        : "아직 수신 기록 없음"}
                    </p>
                  </div>
                </li>
              ))}
            </ul>
          )}

          {issuedKey && (
            <div className="mt-3 rounded-md border border-[--color-warning] bg-[--color-warning-bg] p-3">
              <p className="text-[12px] font-semibold text-[--color-warning]">
                발급된 키 — 지금만 볼 수 있습니다
              </p>
              <code className="mt-1 block break-all rounded bg-[--color-card] px-2 py-1 text-[11px]">
                {issuedKey}
              </code>
              <p className="mt-1 text-[10px] text-[--color-warning]">
                서버는 해시만 보관하므로 다시 조회할 수 없습니다. 안전한 곳에 옮겨 두세요.
              </p>
              <Button
                size="sm" variant="outline" className="mt-2"
                onClick={() => setIssuedKey(null)}
              >
                확인했습니다
              </Button>
            </div>
          )}

          <div className="mt-4 rounded-lg border border-[--color-border] p-3">
            <div className="grid gap-2.5 sm:grid-cols-3">
              <Field label="판매처" htmlFor="acct-provider">
                <select
                  id="acct-provider"
                  value={provider}
                  onChange={(e) => setProvider(e.target.value)}
                  className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
                >
                  {channels.map((c) => (
                    <option key={c.code} value={c.code}>{c.display_name}</option>
                  ))}
                </select>
              </Field>
              <Field label="계정 이름" htmlFor="acct-label" hint="예: 쿠팡 본점">
                <TextInput
                  id="acct-label" value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  placeholder="스토어를 구분할 이름"
                />
              </Field>
              <div className="flex items-end">
                <Button
                  size="sm"
                  disabled={!label.trim() || create.isPending}
                  onClick={() => create.mutate()}
                >
                  <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                  계정 발급
                </Button>
              </div>
            </div>
          </div>

          <p className="mt-2 flex items-start gap-1.5 rounded-md bg-[--color-muted] p-2 text-[10px] leading-relaxed text-[--color-muted-foreground]">
            <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
            <span>
              봇 계정은 <strong>주문 데이터를 받는 통로</strong>입니다. 판매처 API 를 대신
              호출하지는 않습니다. 들어온 주문은{" "}
              <Link href={"/sales" as Route} className="underline">판매장부</Link> 에서 확인합니다.
            </span>
          </p>
        </>
      )}
    </Section>
  );
}
