"use client";

/**
 * 외부 시스템 연동 (셀러툴 등).
 *
 * 판매처 연결과 왜 다른 화면인가
 * ---------------------------
 * `/integrations/marketplaces` 는 쿠팡·스마트스토어 **판매자 계정**이다 —
 * 주문과 정산이 그 계정에 귀속된다. 여기는 셀러툴처럼 우리가 나가서 호출하는
 * 외부 시스템이고, 주문을 소유하지 않는다. 합치면 "주문이 NEXT ENGINE 계정
 * 소속" 같은 상태가 가능해진다(docs/58).
 *
 * 없는 기능을 있는 것처럼 보이지 않는다
 * --------------------------------
 * 카탈로그에 있어도 커넥터가 없으면 동기화 버튼을 누를 수 없고, 그 이유를
 * 화면이 말한다. 눌렀는데 아무 일도 안 나면 사용자는 고장으로 읽는다.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Check, Minus, Plug, Plug2, Plus, RefreshCw, TriangleAlert, X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { PageShell, Section } from "@/components/shell/page-shell";
import {
  CredentialDialog, RemoveCredentialDialog, VaultNoteDialog,
} from "@/components/integrations/credential-dialog";
import { ApiError } from "@/lib/api";
import {
  ACCOUNT_STATUS_LABEL, CREDENTIAL_KIND_LABEL, SYNC_STATUS_LABEL,
  SYSTEM_TYPE_LABEL, TEST_STATUS_LABEL,
  externalApi, supportedCapabilities,
  type ExternalAccount, type ExternalSystem,
} from "@/lib/api/external";
import { formatDateTime } from "@/lib/format";

function errorMessage(err: unknown, action: string): string {
  if (!(err instanceof ApiError)) return `${action} 중 통신에 실패했습니다.`;
  if (err.status === 403) return `${action} 권한이 없습니다.`;
  if (err.status === 501) return err.message || "아직 구현되지 않은 연동입니다.";
  if (err.status === 409) return err.message || "이미 있는 항목입니다.";
  return err.message || `${action}에 실패했습니다.`;
}

export default function ExternalIntegrationsPage() {
  const qc = useQueryClient();
  const [open, setOpen] = React.useState(false);

  const { data: systems } = useQuery({
    queryKey: ["external", "systems"],
    queryFn: () => externalApi.systems(),
  });
  const { data: accounts, isLoading } = useQuery({
    queryKey: ["external", "accounts"],
    queryFn: () => externalApi.accounts(),
  });
  const { data: conflicts } = useQuery({
    queryKey: ["external", "conflicts"],
    queryFn: () => externalApi.conflicts(true),
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["external"] });

  return (
    <PageShell
      title="외부 시스템 연동"
      description="셀러툴 등 외부 시스템 계정을 관리합니다. 쇼핑몰 판매자 계정은 '쇼핑몰 API 연결'에 있습니다"
      actions={
        <Button size="sm" onClick={() => setOpen(true)}>
          <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          계정 추가
        </Button>
      }
      variant="detail"
    >
      {(conflicts?.total ?? 0) > 0 && (
        <div className="mb-3 flex items-start gap-1.5 rounded-md border border-[--color-warning] bg-[--color-surface-muted] px-3 py-2 text-xs">
          <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[--color-warning]" aria-hidden />
          <span>
            외부 값과 내부 값이 다른 항목이 {conflicts?.total}건 있습니다.
            사람이 고친 값을 자동으로 덮지 않았습니다 — 아래 동기화 기록에서
            어떤 값이 달랐는지 확인하세요.
          </span>
        </div>
      )}

      <Section title="사용 가능한 외부 시스템">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {(systems?.items ?? []).map((s) => (
            <SystemCard key={s.id} system={s} />
          ))}
        </div>
      </Section>

      <Section title={`등록된 계정 ${accounts?.total ?? 0}개`}>
        {isLoading ? (
          <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
        ) : (accounts?.items ?? []).length === 0 ? (
          <p className="py-6 text-center text-sm text-[--color-muted-foreground]">
            등록된 외부 계정이 없습니다.
          </p>
        ) : (
          <div className="space-y-2">
            {accounts?.items.map((a) => (
              <AccountRow key={a.id} account={a} onChanged={invalidate} />
            ))}
          </div>
        )}
      </Section>

      <ConflictSection />

      <SyncRunsSection />

      <CreateAccountDialog
        open={open} onOpenChange={setOpen}
        systems={systems?.items ?? []} onCreated={invalidate}
      />
    </PageShell>
  );
}

function SystemCard({ system }: { system: ExternalSystem }) {
  const caps = supportedCapabilities(system.capabilities);
  return (
    <div className="rounded-lg border border-[--color-border] p-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-[13px] font-medium">{system.name}</p>
          <p className="font-mono text-[10px] text-[--color-muted-foreground]">
            {system.code} · {SYSTEM_TYPE_LABEL[system.system_type]}
          </p>
        </div>
        {/* 카탈로그에 있는 것과 연동되는 것은 다르다. */}
        <Badge
          variant={system.connector_available ? "default" : "warning"}
          className="shrink-0 px-1.5 py-0 text-[10px]"
        >
          {system.connector_available ? "연동 가능" : "연동 준비 중"}
        </Badge>
      </div>
      <p className="mt-1.5 text-[11px] text-[--color-muted-foreground]">
        {caps.length > 0
          ? caps.join(" · ")
          : "지원 기능이 아직 없습니다. 계정은 미리 등록해 둘 수 있습니다."}
      </p>
    </div>
  );
}

function AccountRow({
  account, onChanged,
}: {
  account: ExternalAccount;
  onChanged: () => void;
}) {
  const [credOpen, setCredOpen] = React.useState(false);
  const [removeOpen, setRemoveOpen] = React.useState(false);
  const [noteOpen, setNoteOpen] = React.useState(false);

  const sync = useMutation({
    mutationFn: () => externalApi.syncProducts(account.id),
    onSuccess: (r) => {
      onChanged();
      toast.success(
        `동기화 완료 — 읽음 ${r.read_count}, 반영 ${r.updated_count}, `
        + `건너뜀 ${r.skipped_count}, 충돌 ${r.conflict_count}`,
      );
    },
    onError: (e) => toast.error(errorMessage(e, "동기화")),
  });

  const disable = useMutation({
    mutationFn: () =>
      externalApi.setStatus(
        account.id, account.status === "DISABLED" ? "DISCONNECTED" : "DISABLED",
      ),
    onSuccess: () => {
      onChanged();
      toast.success("계정 상태를 변경했습니다.");
    },
    onError: (e) => toast.error(errorMessage(e, "상태 변경")),
  });

  /**
   * 연결 시험은 실패 사유를 구분해서 알린다.
   *
   * 전부 "연결 실패" 로 묶으면 사용자는 자격증명을 다시 넣어야 하는지, 우리가
   * 아직 만들지 않은 것인지, 상대 서버가 죽은 것인지 알 수 없다 — 셋의 대응이
   * 전혀 다르다.
   */
  const test = useMutation({
    mutationFn: () => externalApi.testConnection(account.id),
    onSuccess: (r) => {
      onChanged();
      const label = TEST_STATUS_LABEL[r.status] ?? r.status;
      if (r.status === "OK") toast.success(label);
      else toast.warning(label, { description: r.message || undefined });
    },
    onError: (e) => toast.error(errorMessage(e, "연결 시험")),
  });

  const caps = supportedCapabilities(account.capabilities);
  const canSync = account.connector_available && account.capabilities.PRODUCT_IMPORT;

  return (
    <div className="rounded-lg border border-[--color-border] p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <Plug className="h-3.5 w-3.5 text-[--color-muted-foreground]" aria-hidden />
            <span className="text-[13px] font-medium">{account.display_name}</span>
            <Badge variant="default" className="px-1.5 py-0 text-[10px]">
              {account.system_name}
            </Badge>
            <Badge
              variant={account.status === "CONNECTED" ? "default" : "warning"}
              className="px-1.5 py-0 text-[10px]"
            >
              {ACCOUNT_STATUS_LABEL[account.status]}
            </Badge>
          </div>
          <p className="mt-1 text-[11px] text-[--color-muted-foreground]">
            {account.account_code}
            {account.external_account_identifier
              ? ` · ${account.external_account_identifier}`
              : ""}
            {" · "}연결된 항목 {account.mapping_count}건
            {account.last_successful_sync_at
              ? ` · 마지막 성공 ${formatDateTime(account.last_successful_sync_at)}`
              : " · 성공한 동기화 없음"}
          </p>
          <ReadinessPanel
            account={account}
            onSetCredential={() => setCredOpen(true)}
            onRemoveCredential={() => setRemoveOpen(true)}
            onLinkNote={() => setNoteOpen(true)}
          />
          {caps.length > 0 && (
            <p className="mt-1 text-[10px] text-[--color-muted-foreground]">
              {caps.join(" · ")}
            </p>
          )}
        </div>

        <div className="flex shrink-0 gap-1.5">
          <Button
            variant="outline" size="sm"
            disabled={test.isPending}
            onClick={() => test.mutate()}
          >
            <Plug2 className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            연결 시험
          </Button>
          <Button
            variant="outline" size="sm"
            disabled={!canSync || sync.isPending}
            onClick={() => sync.mutate()}
            title={canSync ? undefined : "이 시스템의 연동이 아직 구현되지 않았습니다"}
          >
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            상품 동기화
          </Button>
          <Button variant="outline" size="sm" onClick={() => disable.mutate()}>
            {account.status === "DISABLED" ? "다시 사용" : "사용 중지"}
          </Button>
        </div>
      </div>

      {!canSync && (
        <p className="mt-2 text-[11px] text-[--color-muted-foreground]">
          {account.connector_available
            ? "이 시스템은 상품 가져오기를 지원하지 않습니다."
            : "연동이 아직 구현되지 않아 동기화할 수 없습니다. 계정 정보는 미리 등록해 둘 수 있습니다."}
        </p>
      )}

      <CredentialDialog
        account={account} open={credOpen} onOpenChange={setCredOpen}
        onChanged={onChanged}
      />
      <RemoveCredentialDialog
        account={account} open={removeOpen} onOpenChange={setRemoveOpen}
        onChanged={onChanged}
      />
      <VaultNoteDialog
        account={account} open={noteOpen} onOpenChange={setNoteOpen}
        onChanged={onChanged}
      />
    </div>
  );
}

/**
 * 동기화가 되기까지 무엇이 남았는가.
 *
 * 세 가지를 **합치지 않는다**
 * ------------------------
 * ```
 * 연동 구현   우리가 이 시스템의 커넥터를 만들었는가
 * 자격증명    프로그램이 인증할 수 있는가
 * 연결 시험   실제로 붙어 봤는가
 * ```
 *
 * 하나로 뭉쳐 "연결됨/안 됨" 으로 보여주면, 사용자는 자기 자격증명을 몇 번씩
 * 다시 넣는다 — 실제로는 우리가 아직 안 만든 경우가 가장 흔한데도.
 *
 * 마지막 줄은 **결론**이다. 위 셋을 보고 지금 동기화를 할 수 있는지 한 줄로
 * 말해 준다.
 */
function ReadinessPanel({
  account, onSetCredential, onRemoveCredential, onLinkNote,
}: {
  account: ExternalAccount;
  onSetCredential: () => void;
  onRemoveCredential: () => void;
  onLinkNote: () => void;
}) {
  const canSync = account.connector_available && account.credential_configured
    && account.capabilities.PRODUCT_IMPORT;

  return (
    <div className="mt-2 space-y-1 rounded-md border border-[--color-border] bg-[--color-surface-muted] px-2.5 py-2">
      <ReadyRow
        label="연동 구현"
        ok={account.connector_available}
        text={account.connector_available ? "사용 가능" : "미구현"}
      />

      <ReadyRow
        label="자격증명"
        ok={account.credential_configured}
        text={
          account.credential_configured
            ? [
                "설정됨",
                account.credential_kind
                  ? CREDENTIAL_KIND_LABEL[account.credential_kind]
                    ?? account.credential_kind
                  : null,
                account.credential_updated_at
                  ? `${formatDateTime(account.credential_updated_at)} 변경`
                  : null,
              ].filter(Boolean).join(" · ")
            : "미설정"
        }
        actions={
          <>
            <Button
              variant="ghost" size="sm" className="h-5 px-1.5 text-[11px]"
              onClick={onSetCredential}
            >
              {account.credential_configured ? "교체" : "설정"}
            </Button>
            {account.credential_configured && (
              <Button
                variant="ghost" size="sm" className="h-5 px-1.5 text-[11px]"
                onClick={onRemoveCredential}
              >
                삭제
              </Button>
            )}
          </>
        }
      />

      <ReadyRow
        label="연결 시험"
        ok={account.last_test_status === "OK"}
        // 아직 안 해 본 것과 실패한 것은 다르다.
        pending={!account.last_test_status}
        text={
          account.last_test_status
            ? `${TEST_STATUS_LABEL[account.last_test_status]
                ?? account.last_test_status}`
              + (account.last_test_at
                  ? ` · ${formatDateTime(account.last_test_at)}` : "")
            : "아직 해 보지 않음"
        }
      />

      {/* 보관함 쪽지는 사람용이다 — 위의 자격증명과 다른 것이라 따로 둔다. */}
      <ReadyRow
        label="보관함 쪽지"
        ok={account.vault_note_linked}
        pending={!account.vault_note_linked}
        text={account.vault_note_linked
          ? "연결됨 (사람이 찾아갈 위치)"
          : "없음 — 자동 동기화와는 무관합니다"}
        actions={
          <Button
            variant="ghost" size="sm" className="h-5 px-1.5 text-[11px]"
            onClick={onLinkNote}
          >
            {account.vault_note_linked ? "변경" : "연결"}
          </Button>
        }
      />

      <p className="border-t border-[--color-border] pt-1 text-[11px]">
        <span className="font-medium">동기화</span>{" — "}
        {canSync ? (
          <span className="text-[--color-success]">사용 가능</span>
        ) : (
          <span className="text-[--color-muted-foreground]">
            사용 불가 ({!account.connector_available
              ? "연동 미구현"
              : !account.credential_configured
                ? "자격증명 미설정"
                : "이 시스템은 상품 가져오기를 지원하지 않습니다"})
          </span>
        )}
      </p>
    </div>
  );
}

function ReadyRow({
  label, ok, pending = false, text, actions,
}: {
  label: string;
  ok: boolean;
  pending?: boolean;
  text: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px]">
      <span className="w-[62px] shrink-0 text-[--color-muted-foreground]">{label}</span>
      {ok ? (
        <Check className="h-3 w-3 shrink-0 text-[--color-success]" aria-hidden />
      ) : pending ? (
        <Minus className="h-3 w-3 shrink-0 text-[--color-muted-foreground]" aria-hidden />
      ) : (
        <X className="h-3 w-3 shrink-0 text-[--color-warning]" aria-hidden />
      )}
      <span className={ok ? "" : "text-[--color-muted-foreground]"}>{text}</span>
      {actions}
    </div>
  );
}

/**
 * 자동으로 정하지 않고 남긴 충돌.
 *
 * 왜 목록으로 보여주나
 * -----------------
 * 배너의 "3건 있습니다" 만으로는 아무것도 못 한다. 어떤 상품의 어떤 값이
 * 달랐는지 봐야 외부를 고칠지 우리를 고칠지 정할 수 있다.
 *
 * 여기서 해결 버튼을 두지 않은 이유
 * ---------------------------
 * "외부 값 채택" 은 원가라면 `COST_CHANGE` 승인을 거쳐야 한다. 이 화면에 버튼을
 * 달면 승인을 우회하는 통로가 생긴다. 지금은 **무엇이 달랐는지 보여주는 데까지**만
 * 한다 — 값 반영은 각 도메인 화면의 정상 경로로 한다.
 */
function ConflictSection() {
  const { data } = useQuery({
    queryKey: ["external", "conflicts"],
    queryFn: () => externalApi.conflicts(true),
  });
  const rows = data?.items ?? [];
  if (rows.length === 0) return null;

  return (
    <Section title={`미해결 충돌 ${rows.length}건`}>
      <div className="ops-scroll-x">
        <table className="w-full text-[12px]">
          <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
            <tr>
              <th scope="col" className="px-2 py-1.5 text-left font-semibold">발생</th>
              <th scope="col" className="px-2 py-1.5 text-left font-semibold">대상</th>
              <th scope="col" className="px-2 py-1.5 text-left font-semibold">항목</th>
              <th scope="col" className="px-2 py-1.5 text-left font-semibold">외부 값</th>
              <th scope="col" className="px-2 py-1.5 text-left font-semibold">우리 값 (유지됨)</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[--color-border]">
            {rows.map((c) => (
              <tr key={c.id}>
                <td className="whitespace-nowrap px-2 py-1.5 tabular-nums">
                  {formatDateTime(c.created_at)}
                </td>
                <td className="px-2 py-1.5">
                  {c.internal_entity_name ?? "(연결 정보 없음)"}
                  {c.account_name && (
                    <span className="text-[10px] text-[--color-muted-foreground]">
                      {" · "}{c.account_name}
                    </span>
                  )}
                </td>
                <td className="px-2 py-1.5 font-mono text-[11px]">{c.field_name}</td>
                {c.values_masked ? (
                  // 값이 없는 것과 가려진 것을 같게 보이면 안 된다.
                  <td colSpan={2} className="px-2 py-1.5 text-[11px] text-[--color-muted-foreground]">
                    원가 항목이라 표시하지 않습니다 — 조회 권한이 필요합니다.
                  </td>
                ) : (
                  <>
                    <td className="px-2 py-1.5">{c.external_value ?? "—"}</td>
                    <td className="px-2 py-1.5">{c.internal_value ?? "—"}</td>
                  </>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-[11px] text-[--color-muted-foreground]">
        우리 값을 유지했습니다. 외부 값을 채택하려면 해당 상품 화면에서
        정상 경로로 변경하세요 — 원가는 승인이 필요합니다.
      </p>
    </Section>
  );
}

function SyncRunsSection() {
  const { data } = useQuery({
    queryKey: ["external", "runs"],
    queryFn: () => externalApi.runs({ limit: 20 }),
  });
  const runs = data?.items ?? [];
  if (runs.length === 0) return null;

  return (
    <Section title="최근 동기화">
      <div className="ops-scroll-x">
        <table className="w-full text-[12px]">
          <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
            <tr>
              <th scope="col" className="px-2 py-1.5 text-left font-semibold">시각</th>
              <th scope="col" className="px-2 py-1.5 text-left font-semibold">유형</th>
              <th scope="col" className="px-2 py-1.5 text-center font-semibold">상태</th>
              <th scope="col" className="px-2 py-1.5 text-right font-semibold">읽음</th>
              <th scope="col" className="px-2 py-1.5 text-right font-semibold">반영</th>
              <th scope="col" className="px-2 py-1.5 text-right font-semibold">건너뜀</th>
              <th scope="col" className="px-2 py-1.5 text-right font-semibold">충돌</th>
              <th scope="col" className="px-2 py-1.5 text-left font-semibold">비고</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[--color-border]">
            {runs.map((r) => (
              <tr key={r.id}>
                <td className="whitespace-nowrap px-2 py-1.5 tabular-nums">
                  {formatDateTime(r.started_at)}
                </td>
                <td className="px-2 py-1.5">{r.sync_type}</td>
                <td className="px-2 py-1.5 text-center">
                  {/* 미지원은 실패가 아니다 — 색까지 같게 하면 고장으로 읽힌다.
                      일부 실패는 성공과 뭉치지 않는다. */}
                  <Badge
                    variant={
                      r.status === "SUCCESS" ? "success"
                        : r.status === "FAILED" ? "danger"
                          : r.status === "UNSUPPORTED" ? "info"
                            : r.status === "RUNNING" ? "default" : "warning"
                    }
                    className="px-1.5 py-0 text-[10px]"
                  >
                    {SYNC_STATUS_LABEL[r.status]}
                  </Badge>
                </td>
                {/* null 은 아직 안 끝난 것이다 — 0 과 다르다. */}
                <td className="px-2 py-1.5 text-right tabular-nums">{r.read_count ?? "—"}</td>
                <td className="px-2 py-1.5 text-right tabular-nums">{r.updated_count ?? "—"}</td>
                <td className="px-2 py-1.5 text-right tabular-nums">{r.skipped_count ?? "—"}</td>
                <td className="px-2 py-1.5 text-right tabular-nums">{r.conflict_count ?? "—"}</td>
                <td className="px-2 py-1.5 text-[11px] text-[--color-muted-foreground]">
                  {r.error_message ?? ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function CreateAccountDialog({
  open, onOpenChange, systems, onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  systems: ExternalSystem[];
  onCreated: () => void;
}) {
  const [systemId, setSystemId] = React.useState("");
  const [name, setName] = React.useState("");
  const [code, setCode] = React.useState("");
  const [identifier, setIdentifier] = React.useState("");

  const create = useMutation({
    mutationFn: () =>
      externalApi.createAccount({
        external_system_id: systemId,
        display_name: name.trim(),
        account_code: code.trim(),
        external_account_identifier: identifier.trim() || null,
      }),
    onSuccess: () => {
      onCreated();
      onOpenChange(false);
      setName(""); setCode(""); setIdentifier("");
      toast.success("외부 계정을 등록했습니다.");
    },
    onError: (e) => toast.error(errorMessage(e, "계정 등록")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>외부 계정 추가</DialogTitle></DialogHeader>
        <div className="space-y-3 py-2 text-sm">
          {/* 등록 폼에 비밀번호 칸을 두면 그 값이 요청 로그·브라우저 히스토리에 남는다. */}
          <p className="rounded-md border border-[--color-border] bg-[--color-surface-muted] px-3 py-2 text-[11px] text-[--color-muted-foreground]">
            자격증명은 여기서 입력하지 않습니다. 계정을 먼저 등록한 뒤 별도로
            설정합니다 — 비밀번호가 요청 기록에 남지 않게 하기 위해서입니다.
          </p>
          <div className="space-y-1">
            <Label htmlFor="ea-sys" className="text-xs">외부 시스템 *</Label>
            <select
              id="ea-sys" value={systemId} onChange={(e) => setSystemId(e.target.value)}
              className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
            >
              <option value="">선택하세요</option>
              {systems.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}{s.connector_available ? "" : " (연동 준비 중)"}
                </option>
              ))}
            </select>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label htmlFor="ea-name" className="text-xs">계정 이름 *</Label>
              <Input
                id="ea-name" value={name} onChange={(e) => setName(e.target.value)}
                className="h-8 text-sm" placeholder="본사 계정"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="ea-code" className="text-xs">계정 코드 *</Label>
              <Input
                id="ea-code" value={code} onChange={(e) => setCode(e.target.value)}
                className="h-8 text-sm" placeholder="MAIN"
              />
            </div>
          </div>
          <div className="space-y-1">
            <Label htmlFor="ea-ident" className="text-xs">외부 계정 식별자</Label>
            <Input
              id="ea-ident" value={identifier}
              onChange={(e) => setIdentifier(e.target.value)}
              className="h-8 text-sm" placeholder="회사코드 등 (비밀 아님)"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>취소</Button>
          <Button
            size="sm" disabled={!systemId || !name.trim() || !code.trim() || create.isPending}
            onClick={() => create.mutate()}
          >
            등록
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
