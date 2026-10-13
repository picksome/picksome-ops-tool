"use client";

/**
 * 외부 계정 자격증명 설정 대화상자 (M042).
 *
 * 여기서 비밀을 받는다 — 그래서 규칙이 있다
 * ------------------------------------
 * 무인 동기화를 하려면 프로그램이 인증정보를 다시 쓸 수 있어야 한다. 그래서
 * 이 화면은 실제 값을 받는다. 대신 값이 남을 수 있는 자리를 하나씩 막았다.
 *
 * ```
 * 저장 후 입력칸        즉시 비운다
 * localStorage/세션     쓰지 않는다
 * 브라우저 자동완성      끈다 (비밀번호 관리자에 저장되면 우리 통제 밖이다)
 * 되읽기 화면           없다 — 잊었으면 새로 넣는다
 * 화면 표시             `type="password"` (어깨너머, screenshot, 화면공유)
 * ```
 *
 * 서버도 값을 돌려주지 않는다. 응답은 "설정됨" 불리언과 시점뿐이고, 검증 오류
 * 응답에서도 제출값이 제거된다(`core/redaction.py`).
 *
 * vendor 별 폼을 만들지 않는다
 * -------------------------
 * 항목 목록은 `credential_kind` 가 정한다. "NEXT ENGINE 용 폼" 을 만들기
 * 시작하면 셀러툴을 하나 붙일 때마다 화면이 늘어난다.
 *
 * 쪽지와 다른 것이다
 * ---------------
 * 보관함 쪽지(`vault-note`)는 **사람이 찾아갈 위치**다. 그것만 있으면 새벽
 * 배치는 아무것도 못 한다. 두 가지를 화면에서도 나눠 놓는다.
 */

import * as React from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { KeyRound, ShieldOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { ApiError } from "@/lib/api";
import {
  CREDENTIAL_FIELDS, CREDENTIAL_KIND_LABEL, externalApi, type ExternalAccount,
} from "@/lib/api/external";
import { vaultApi, vaultEntryLabel } from "@/lib/api/vault";

const KINDS = [
  "API_KEY", "ACCESS_SECRET", "CLIENT_CREDENTIALS", "OAUTH_REFRESH",
  "BASIC", "CUSTOM",
] as const;

function message(err: unknown, action: string): string {
  if (!(err instanceof ApiError)) return `${action} 중 통신에 실패했습니다.`;
  if (err.status === 403) return "자격증명은 관리자만 변경할 수 있습니다.";
  if (err.status === 503) {
    // 코드 버그가 아니라 배포 설정이 덜 된 상태다. 할 일이 다르다.
    return "서버에 자격증명 암호화 키가 설정되지 않았습니다. 운영자에게 문의하세요.";
  }
  if (err.status === 422) return err.message || "입력값을 확인하세요.";
  return err.message || `${action}에 실패했습니다.`;
}

export function CredentialDialog({
  account, open, onOpenChange, onChanged,
}: {
  account: ExternalAccount;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onChanged: () => void;
}) {
  const [kind, setKind] = React.useState<string>(account.credential_kind ?? "API_KEY");
  const [values, setValues] = React.useState<Record<string, string>>({});
  const [customKey, setCustomKey] = React.useState("");

  const fields = CREDENTIAL_FIELDS[kind] ?? [];

  /** 입력값을 지운다. 저장 성공·취소·닫기 어디서든 부른다. */
  const clearInputs = React.useCallback(() => {
    setValues({});
    setCustomKey("");
  }, []);

  // 대화상자를 닫으면 입력한 값이 컴포넌트 상태에 남지 않게 한다.
  // 남겨 두면 다시 열었을 때 이전 사람이 넣던 값이 보인다.
  React.useEffect(() => {
    if (!open) clearInputs();
  }, [open, clearInputs]);

  // 형태를 바꾸면 이전 형태의 값은 의미가 없다.
  React.useEffect(() => clearInputs(), [kind, clearInputs]);

  const save = useMutation({
    mutationFn: () =>
      externalApi.setCredential(account.id, { credential_kind: kind, secret: values }),
    onSuccess: () => {
      // 성공하자마자 비운다 — 화면에 남겨 둘 이유가 없다.
      clearInputs();
      onChanged();
      onOpenChange(false);
      toast.success(
        account.credential_configured
          ? "자격증명을 교체했습니다."
          : "자격증명을 설정했습니다.",
      );
    },
    onError: (e) => toast.error(message(e, "자격증명 설정")),
  });

  const filled = fields.length > 0
    ? fields.every((f) => (values[f.key] ?? "").trim().length > 0)
    : Object.keys(values).length > 0;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {account.credential_configured ? "자격증명 교체" : "자격증명 설정"}
            {" — "}{account.display_name}
          </DialogTitle>
        </DialogHeader>

        {/* 폼으로 감싸지 않는다 — 브라우저가 비밀번호 저장을 제안하면
            값이 우리 통제 밖으로 나간다. */}
        <div className="space-y-3 py-2 text-sm">
          <p className="rounded-md border border-[--color-border] bg-[--color-surface-muted] px-3 py-2 text-[11px] text-[--color-muted-foreground]">
            입력한 값은 서버에서 <strong>암호화되어</strong> 저장되고, 저장 뒤에는
            어떤 화면에서도 다시 볼 수 없습니다. 잊으면 새로 넣어야 합니다.
          </p>

          {account.credential_configured && (
            <p className="text-[11px] text-[--color-muted-foreground]">
              지금 설정된 종류: {CREDENTIAL_KIND_LABEL[account.credential_kind ?? ""]
                ?? account.credential_kind ?? "알 수 없음"}
              {" · "}저장에 실패해도 기존 자격증명은 그대로 남습니다.
            </p>
          )}

          <div className="space-y-1">
            <Label htmlFor="cred-kind" className="text-xs">인증 방식 *</Label>
            <select
              id="cred-kind" value={kind} onChange={(e) => setKind(e.target.value)}
              className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
            >
              {KINDS.map((k) => (
                <option key={k} value={k}>{CREDENTIAL_KIND_LABEL[k]}</option>
              ))}
            </select>
          </div>

          {fields.map((f) => (
            <div key={f.key} className="space-y-1">
              <Label htmlFor={`cred-${f.key}`} className="text-xs">
                {f.label} *
              </Label>
              <Input
                id={`cred-${f.key}`}
                // 비밀 항목은 가린다 — 어깨너머·화면공유·screenshot.
                type={f.secret ? "password" : "text"}
                // 브라우저 비밀번호 관리자에 저장되면 우리 통제 밖이다.
                autoComplete="off"
                spellCheck={false}
                value={values[f.key] ?? ""}
                onChange={(e) =>
                  setValues((v) => ({ ...v, [f.key]: e.target.value }))}
                className="h-8 font-mono text-sm"
              />
            </div>
          ))}

          {kind === "CUSTOM" && (
            <div className="space-y-1.5 rounded-md border border-dashed border-[--color-border] p-2">
              <p className="text-[11px] text-[--color-muted-foreground]">
                항목 이름을 직접 정합니다. 형태가 정해진 방식을 쓸 수 있으면
                그쪽이 낫습니다 — 필수 항목 검증이 붙습니다.
              </p>
              <div className="flex gap-1.5">
                <Input
                  value={customKey} onChange={(e) => setCustomKey(e.target.value)}
                  placeholder="항목 이름 (예: shop_token)"
                  className="h-7 text-xs"
                />
                <Button
                  variant="outline" size="sm" className="h-7"
                  disabled={!customKey.trim()}
                  onClick={() => {
                    setValues((v) => ({ ...v, [customKey.trim()]: "" }));
                    setCustomKey("");
                  }}
                >
                  추가
                </Button>
              </div>
              {Object.keys(values).map((k) => (
                <div key={k} className="space-y-1">
                  <Label htmlFor={`cred-c-${k}`} className="text-xs">{k}</Label>
                  <Input
                    id={`cred-c-${k}`} type="password" autoComplete="off"
                    value={values[k]}
                    onChange={(e) =>
                      setValues((v) => ({ ...v, [k]: e.target.value }))}
                    className="h-8 font-mono text-sm"
                  />
                </div>
              ))}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button
            variant="outline" size="sm"
            onClick={() => { clearInputs(); onOpenChange(false); }}
          >
            취소
          </Button>
          <Button
            size="sm" disabled={!filled || save.isPending}
            onClick={() => save.mutate()}
          >
            <KeyRound className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            {account.credential_configured ? "교체" : "저장"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * 자격증명 삭제 확인.
 *
 * 확인을 받는 이유는 이 계정의 동기화가 즉시 멈추기 때문이다 — 삭제된 계정은
 * `CREDENTIAL_NOT_CONFIGURED` 로 거부되고, 그건 정상 동작이다.
 */
export function RemoveCredentialDialog({
  account, open, onOpenChange, onChanged,
}: {
  account: ExternalAccount;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onChanged: () => void;
}) {
  const remove = useMutation({
    mutationFn: () => externalApi.removeCredential(account.id),
    onSuccess: () => {
      onChanged();
      onOpenChange(false);
      toast.success("자격증명을 삭제했습니다.");
    },
    onError: (e) => toast.error(message(e, "자격증명 삭제")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>자격증명 삭제</DialogTitle></DialogHeader>
        <div className="space-y-2 py-2 text-sm">
          <p>
            <strong>{account.display_name}</strong> 의 자격증명을 삭제합니다.
          </p>
          <p className="text-[11px] text-[--color-muted-foreground]">
            이 계정의 동기화가 즉시 중단됩니다. 저장된 값은 되돌릴 수 없습니다 —
            다시 쓰려면 새로 입력해야 합니다.
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            취소
          </Button>
          <Button
            variant="destructive" size="sm" disabled={remove.isPending}
            onClick={() => remove.mutate()}
          >
            <ShieldOff className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            삭제
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * 보관함 쪽지 연결 — **사람용**이다.
 *
 * 값이 없으므로 이걸로 동기화가 되지는 않는다. 담당자가 바뀔 때 "이 계정
 * 비밀번호 어디 있죠" 의 단서로 남긴다.
 */
export function VaultNoteDialog({
  account, open, onOpenChange, onChanged,
}: {
  account: ExternalAccount;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onChanged: () => void;
}) {
  const [entryId, setEntryId] = React.useState("");

  const { data: entries, isLoading } = useQuery({
    queryKey: ["vault", "entries"],
    queryFn: () => vaultApi.list({ limit: 200 }),
    enabled: open,
  });

  const link = useMutation({
    mutationFn: () => externalApi.linkVaultNote(account.id, entryId),
    onSuccess: () => {
      onChanged();
      onOpenChange(false);
      setEntryId("");
      toast.success("보관함 위치를 연결했습니다.");
    },
    onError: (e) => toast.error(message(e, "보관 위치 연결")),
  });

  const items = entries?.items ?? [];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>보관함 위치 연결 — {account.display_name}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 py-2 text-sm">
          <p className="rounded-md border border-[--color-border] bg-[--color-surface-muted] px-3 py-2 text-[11px] text-[--color-muted-foreground]">
            사람이 찾아갈 <strong>위치</strong>만 기록합니다. 값이 없으므로
            이것만으로는 자동 동기화가 되지 않습니다 — 그건 위의
            &lsquo;자격증명&rsquo;에서 설정합니다.
          </p>
          {isLoading ? (
            <p className="text-[11px] text-[--color-muted-foreground]">불러오는 중…</p>
          ) : items.length === 0 ? (
            <p className="text-[11px] text-[--color-warning]">
              등록된 보관 위치가 없습니다. 먼저 비밀번호 보관함에 항목을 만들어 주세요.
            </p>
          ) : (
            <div className="space-y-1">
              <Label htmlFor="vault-entry" className="text-xs">보관함 항목 *</Label>
              <select
                id="vault-entry" value={entryId}
                onChange={(e) => setEntryId(e.target.value)}
                className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
              >
                <option value="">선택하세요</option>
                {items.map((e) => (
                  <option key={e.id} value={e.id}>{vaultEntryLabel(e)}</option>
                ))}
              </select>
            </div>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            취소
          </Button>
          <Button size="sm" disabled={!entryId || link.isPending}
                  onClick={() => link.mutate()}>
            연결
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
