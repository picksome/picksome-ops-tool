"use client";

/**
 * AI · API 접근 설정.
 *
 * 자동화와 나눈 이유
 * ---------------
 * 여기는 **선택 기능**이다. 끄면 외부 AI 접근만 없어지고 자동화·리포트·정산은
 * 그대로 돈다. 한 화면에 합치면 "AI 를 안 켜면 반쪽이구나" 로 읽힌다.
 *
 * 토큰은 한 번만 보여준다
 * --------------------
 * 발급 직후 딱 한 번이다. 서버에는 해시만 남아 다시 조회할 수 없고, 그런
 * 경로를 만들지도 않았다 — 되읽기 경로가 있으면 그게 가장 약한 고리가 된다.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Ban, Bot, Copy, KeyRound, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { PageShell, Section } from "@/components/shell/page-shell";
import { ApiError } from "@/lib/api";
import {
  SCOPE_LABEL, aiAccessApi,
  type IssuedToken, type ServiceAccount,
} from "@/lib/api/automation";
import { formatDateTime } from "@/lib/format";

function errorMessage(err: unknown, action: string): string {
  if (!(err instanceof ApiError)) return `${action} 중 통신에 실패했습니다.`;
  if (err.status === 403) return `${action} 권한이 없습니다. 관리자만 가능합니다.`;
  return err.message || `${action}에 실패했습니다.`;
}

export default function AiApiSettingsPage() {
  const qc = useQueryClient();
  const [createOpen, setCreateOpen] = React.useState(false);

  const { data: accounts, isLoading } = useQuery({
    queryKey: ["ai-access", "accounts"],
    queryFn: () => aiAccessApi.accounts(),
  });
  const { data: mcp } = useQuery({
    queryKey: ["ai-access", "mcp"],
    queryFn: () => aiAccessApi.mcpStatus(),
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["ai-access"] });

  return (
    <PageShell
      title="AI · API"
      description="외부 AI 에이전트가 Seller OS 를 읽을 수 있게 합니다. 선택 기능입니다"
      actions={
        <Button size="sm" onClick={() => setCreateOpen(true)}>
          <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          서비스 계정
        </Button>
      }
      variant="detail"
    >
      {/* 꺼져 있어도 손해가 없다는 사실을 먼저 말한다. */}
      <div className="mb-3 rounded-md border border-[--color-border] bg-[--color-surface-muted] px-3 py-2 text-[12px]">
        <p className="flex items-center gap-1.5">
          <Bot className="h-3.5 w-3.5 text-[--color-muted-foreground]" aria-hidden />
          <span className="font-medium">MCP 접근</span>
          <Badge
            variant={mcp?.enabled ? "success" : "default"}
            className="px-1.5 py-0 text-[10px]"
          >
            {mcp?.enabled ? "켜짐" : "꺼짐"}
          </Badge>
        </p>
        <p className="mt-1 text-[11px] text-[--color-muted-foreground]">
          {mcp?.note ?? "확인 중…"}
        </p>
      </div>

      <Section title={`서비스 계정 ${accounts?.total ?? 0}개`}>
        {isLoading ? (
          <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
        ) : (accounts?.items ?? []).length === 0 ? (
          <p className="py-6 text-center text-sm text-[--color-muted-foreground]">
            등록된 서비스 계정이 없습니다. AI 를 쓰지 않는다면 그대로 두어도 됩니다.
          </p>
        ) : (
          <div className="space-y-2">
            {accounts?.items.map((a) => (
              <AccountCard key={a.id} account={a} onChanged={invalidate} />
            ))}
          </div>
        )}
      </Section>

      <CreateAccountDialog
        open={createOpen} onOpenChange={setCreateOpen} onCreated={invalidate}
      />
    </PageShell>
  );
}

function AccountCard({
  account, onChanged,
}: {
  account: ServiceAccount;
  onChanged: () => void;
}) {
  const [issueOpen, setIssueOpen] = React.useState(false);

  const toggle = useMutation({
    mutationFn: () =>
      aiAccessApi.setStatus(
        account.id, account.status === "ACTIVE" ? "DISABLED" : "ACTIVE",
      ),
    onSuccess: () => {
      onChanged();
      toast.success("계정 상태를 변경했습니다.");
    },
    onError: (e) => toast.error(errorMessage(e, "상태 변경")),
  });

  const revoke = useMutation({
    mutationFn: (tokenId: string) => aiAccessApi.revokeToken(tokenId),
    onSuccess: () => {
      onChanged();
      toast.success("토큰을 폐기했습니다. 즉시 사용할 수 없습니다.");
    },
    onError: (e) => toast.error(errorMessage(e, "토큰 폐기")),
  });

  return (
    <div className="rounded-lg border border-[--color-border] p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="text-[13px] font-medium">{account.name}</span>
            <Badge
              variant={account.status === "ACTIVE" ? "success" : "default"}
              className="px-1.5 py-0 text-[10px]"
            >
              {account.status === "ACTIVE" ? "사용" : "정지"}
            </Badge>
          </div>
          <p className="mt-1 text-[11px] text-[--color-muted-foreground]">
            {account.description || "설명 없음"}
            {" · "}
            {account.last_used_at
              ? `마지막 사용 ${formatDateTime(account.last_used_at)}`
              : "사용 기록 없음"}
          </p>
          {account.active_token_count === 0 && (
            // 토큰이 없으면 계정이 있어도 아무것도 못 한다.
            <p className="mt-1 text-[11px] text-[--color-warning]">
              사용 가능한 토큰이 없습니다. 토큰을 발급해야 접근할 수 있습니다.
            </p>
          )}
        </div>
        <div className="flex shrink-0 gap-1.5">
          <Button variant="outline" size="sm" onClick={() => setIssueOpen(true)}>
            <KeyRound className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            토큰 발급
          </Button>
          <Button variant="outline" size="sm" onClick={() => toggle.mutate()}>
            {account.status === "ACTIVE" ? "정지" : "다시 사용"}
          </Button>
        </div>
      </div>

      {account.tokens.length > 0 && (
        <div className="ops-scroll-x mt-3">
          <table className="w-full text-[12px]">
            <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
              <tr>
                <th scope="col" className="px-2 py-1.5 text-left font-semibold">토큰</th>
                <th scope="col" className="px-2 py-1.5 text-left font-semibold">권한</th>
                <th scope="col" className="px-2 py-1.5 text-left font-semibold">마지막 사용</th>
                <th scope="col" className="px-2 py-1.5 text-center font-semibold">상태</th>
                <th scope="col" className="px-2 py-1.5" />
              </tr>
            </thead>
            <tbody className="divide-y divide-[--color-border]">
              {account.tokens.map((t) => (
                <tr key={t.id}>
                  <td className="px-2 py-1.5">
                    <span className="font-mono text-[11px]">{t.token_prefix}…</span>
                    {t.label && (
                      <span className="ml-1 text-[10px] text-[--color-muted-foreground]">
                        {t.label}
                      </span>
                    )}
                  </td>
                  <td className="px-2 py-1.5 text-[11px]">
                    {t.scopes.map((s) => SCOPE_LABEL[s] ?? s).join(", ")}
                  </td>
                  <td className="px-2 py-1.5 text-[11px]">
                    {t.last_used_at ? formatDateTime(t.last_used_at) : "없음"}
                  </td>
                  <td className="px-2 py-1.5 text-center">
                    {/* 폐기와 만료를 구분해서 보여준다 — 대응이 다르다. */}
                    <Badge
                      variant={t.active ? "success" : "default"}
                      className="px-1.5 py-0 text-[10px]"
                    >
                      {t.revoked ? "폐기됨" : t.expired ? "만료됨" : "사용 가능"}
                    </Badge>
                  </td>
                  <td className="px-2 py-1.5 text-right">
                    {t.active && (
                      <Button
                        variant="ghost" size="sm" className="h-6 px-1.5 text-[11px]"
                        onClick={() => revoke.mutate(t.id)}
                      >
                        폐기
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <IssueTokenDialog
        account={account} open={issueOpen} onOpenChange={setIssueOpen}
        onIssued={onChanged}
      />
    </div>
  );
}

function CreateAccountDialog({
  open, onOpenChange, onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated: () => void;
}) {
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");

  const create = useMutation({
    mutationFn: () =>
      aiAccessApi.createAccount({
        name: name.trim(), description: description.trim() || null,
      }),
    onSuccess: () => {
      onCreated();
      onOpenChange(false);
      setName(""); setDescription("");
      toast.success("서비스 계정을 만들었습니다. 이제 토큰을 발급하세요.");
    },
    onError: (e) => toast.error(errorMessage(e, "서비스 계정 생성")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>서비스 계정 만들기</DialogTitle></DialogHeader>
        <div className="space-y-3 py-2 text-sm">
          <p className="rounded-md border border-[--color-border] bg-[--color-surface-muted] px-3 py-2 text-[11px] text-[--color-muted-foreground]">
            사람 계정과 다릅니다. 로그인하지 않고, 역할(권한 등급)도 없습니다.
            할 수 있는 일은 <strong>토큰마다</strong> 따로 정합니다.
          </p>
          <div className="space-y-1">
            <Label htmlFor="sa-name" className="text-xs">이름 *</Label>
            <Input
              id="sa-name" value={name} onChange={(e) => setName(e.target.value)}
              placeholder="예: OpenClaw 셀러 에이전트" className="h-8 text-sm"
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="sa-desc" className="text-xs">설명</Label>
            <Input
              id="sa-desc" value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="무엇에 쓰는 계정인지" className="h-8 text-sm"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            취소
          </Button>
          <Button size="sm" disabled={!name.trim() || create.isPending}
                  onClick={() => create.mutate()}>
            만들기
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function IssueTokenDialog({
  account, open, onOpenChange, onIssued,
}: {
  account: ServiceAccount;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onIssued: () => void;
}) {
  const [selected, setSelected] = React.useState<string[]>([]);
  const [label, setLabel] = React.useState("");
  const [issued, setIssued] = React.useState<IssuedToken | null>(null);

  const { data: scopes } = useQuery({
    queryKey: ["ai-access", "scopes"],
    queryFn: () => aiAccessApi.scopes(),
    enabled: open,
  });

  const issue = useMutation({
    mutationFn: () =>
      aiAccessApi.issueToken(account.id, {
        scopes: selected, label: label.trim() || null,
      }),
    onSuccess: (r) => {
      onIssued();
      // 대화상자를 닫지 않는다 — 지금 복사하지 않으면 다시 볼 수 없다.
      setIssued(r);
      setSelected([]); setLabel("");
    },
    onError: (e) => toast.error(errorMessage(e, "토큰 발급")),
  });

  const close = () => {
    setIssued(null);
    onOpenChange(false);
  };

  const toggle = (scope: string) =>
    setSelected((s) =>
      s.includes(scope) ? s.filter((x) => x !== scope) : [...s, scope]);

  return (
    <Dialog open={open} onOpenChange={(v) => (v ? onOpenChange(v) : close())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {issued ? "토큰이 발급되었습니다" : `토큰 발급 — ${account.name}`}
          </DialogTitle>
        </DialogHeader>

        {issued ? (
          <div className="space-y-3 py-2 text-sm">
            {/* 한 번만 보여준다. 닫으면 끝이다. */}
            <p className="rounded-md border border-[--color-warning] bg-[--color-surface-muted] px-3 py-2 text-[11px]">
              <strong>{issued.warning}</strong>
            </p>
            <div className="flex gap-1.5">
              <Input
                readOnly value={issued.token}
                className="h-8 font-mono text-xs"
                onFocus={(e) => e.currentTarget.select()}
              />
              <Button
                variant="outline" size="sm" className="h-8"
                onClick={() => {
                  navigator.clipboard?.writeText(issued.token);
                  toast.success("복사했습니다.");
                }}
              >
                <Copy className="h-3.5 w-3.5" aria-hidden />
              </Button>
            </div>
            <p className="text-[11px] text-[--color-muted-foreground]">
              권한: {issued.scopes.map((s) => SCOPE_LABEL[s] ?? s).join(", ")}
            </p>
          </div>
        ) : (
          <div className="space-y-3 py-2 text-sm">
            <div className="space-y-1">
              <Label htmlFor="tk-label" className="text-xs">이름표</Label>
              <Input
                id="tk-label" value={label} onChange={(e) => setLabel(e.target.value)}
                placeholder="예: 읽기 전용" className="h-8 text-sm"
              />
            </div>

            <div>
              <p className="mb-1 text-xs font-medium">권한 *</p>
              <div className="space-y-1">
                {(scopes?.read ?? []).concat(scopes?.propose ?? []).map((s) => (
                  <label key={s} className="flex items-center gap-1.5 text-[12px]">
                    <input
                      type="checkbox" checked={selected.includes(s)}
                      onChange={() => toggle(s)} className="ops-focusable"
                    />
                    <span>{SCOPE_LABEL[s] ?? s}</span>
                    <span className="font-mono text-[10px] text-[--color-muted-foreground]">
                      {s}
                    </span>
                  </label>
                ))}
              </div>
            </div>

            {/* 발급 불가 목록을 숨기면 "왜 지급 권한이 없지" 를 계속 묻는다. */}
            {(scopes?.forbidden ?? []).length > 0 && (
              <div className="rounded-md border border-dashed border-[--color-border] px-2.5 py-2">
                <p className="mb-1 flex items-center gap-1 text-[11px] font-medium">
                  <Ban className="h-3 w-3" aria-hidden />
                  발급할 수 없는 권한
                </p>
                <ul className="space-y-0.5">
                  {scopes?.forbidden.map((f) => (
                    <li key={f.scope} className="text-[10px] text-[--color-muted-foreground]">
                      <span className="font-mono">{f.scope}</span> — {f.reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        <DialogFooter>
          {issued ? (
            <Button size="sm" onClick={close}>복사했습니다. 닫기</Button>
          ) : (
            <>
              <Button variant="outline" size="sm" onClick={close}>취소</Button>
              <Button
                size="sm" disabled={selected.length === 0 || issue.isPending}
                onClick={() => issue.mutate()}
              >
                발급
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
