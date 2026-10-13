"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, Loader2, Search, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { TextInput } from "@/components/ui/money-input";
import { externalPartnersApi, type ExternalPartner } from "@/lib/api/partners";

/** 최근 고른 거래처. 대부분의 비용은 같은 몇 곳에서 나온다. */
const RECENT_KEY = "pso_recent_suppliers";
const RECENT_MAX = 5;

function loadRecent(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(RECENT_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((v) => typeof v === "string") : [];
  } catch {
    // 저장 형식이 깨졌거나 스토리지가 막혀 있다. 최근 목록은 편의 기능이라 조용히 넘어간다.
    return [];
  }
}

export function rememberSupplier(id: string): void {
  if (typeof window === "undefined") return;
  try {
    const next = [id, ...loadRecent().filter((v) => v !== id)].slice(0, RECENT_MAX);
    window.localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    // 사파리 프라이빗 모드 등에서 쓰기가 막힌다. 실패해도 선택 자체는 유효하다.
  }
}

/**
 * 거래처 선택.
 *
 * **`partner`(내부 파트너)와 다른 목록이다.** 여기 뜨는 것은 돈을 지불하는
 * 상대(매입처·물류사)고, 결제자·귀속 파트너 선택과 섞으면 안 된다.
 *
 * 신규 등록 버튼을 두지 않는다 — 생성 API 가 없다. 동작하지 않는 버튼을 만들면
 * 사용자는 "왜 눌러도 안 되지" 를 확인하는 데 시간을 쓴다. 서버가 `can_create`
 * 로 알려주고, 화면은 그 사실을 문장으로 말한다.
 */
export function SupplierSelect({
  value, valueName, onChange, disabled, id = "e-supplier",
}: {
  value: string | null;
  /** 이미 알고 있는 이름(비용 목록 조인 결과). 삭제된 거래처면 목록에 없다. */
  valueName?: string | null;
  onChange: (id: string | null) => void;
  disabled?: boolean;
  id?: string;
}) {
  const [open, setOpen] = React.useState(false);
  const [q, setQ] = React.useState("");
  const [recent, setRecent] = React.useState<string[]>([]);
  const boxRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => setRecent(loadRecent()), []);

  // 검색어 입력마다 서버를 때리지 않는다.
  const [debouncedQ, setDebouncedQ] = React.useState("");
  React.useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), 250);
    return () => clearTimeout(t);
  }, [q]);

  const { data, isLoading } = useQuery({
    // 선택 목록에는 **활성 거래처만** 올린다. 비활성은 과거 값 표시에만 쓴다 —
    // 거래를 끊은 곳을 새 비용에 새로 붙일 이유가 없다.
    queryKey: ["external-partners", debouncedQ],
    queryFn: () => externalPartnersApi.list({ q: debouncedQ || undefined, active: true, page_size: 30 }),
    enabled: open,
    staleTime: 60_000,
  });

  /** 현재 선택값의 이름. 목록에 없으면(비활성·삭제) 단건으로 확인한다. */
  const { data: selected } = useQuery({
    queryKey: ["external-partner", value],
    queryFn: () => externalPartnersApi.get(value!),
    enabled: !!value && !valueName,
    staleTime: 5 * 60_000,
  });

  // `data?.items ?? []` 를 그대로 쓰면 매 렌더마다 새 배열이라 아래 useMemo 가
  // 항상 다시 돈다. 목록이 30건이라 티는 안 나지만 의존성 경고의 원인이 된다.
  const items = React.useMemo(() => data?.items ?? [], [data]);
  const canCreate = data?.can_create ?? false;

  const recentItems = React.useMemo(
    () => (debouncedQ ? [] : items.filter((p) => recent.includes(p.id))),
    [items, recent, debouncedQ],
  );
  const restItems = React.useMemo(
    () => items.filter((p) => !recentItems.some((r) => r.id === p.id)),
    [items, recentItems],
  );

  React.useEffect(() => {
    if (!open) return;
    function onClickOutside(e: MouseEvent) {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, [open]);

  const displayName = valueName ?? selected?.display_name ?? null;
  const isInactive = selected ? !selected.is_active : false;

  function pick(p: ExternalPartner) {
    onChange(p.id);
    rememberSupplier(p.id);
    setRecent(loadRecent());
    setOpen(false);
    setQ("");
  }

  return (
    <div ref={boxRef} className="relative">
      <div className="flex items-center gap-1">
        <button
          id={id}
          type="button"
          disabled={disabled}
          onClick={() => setOpen((v) => !v)}
          aria-haspopup="listbox"
          aria-expanded={open}
          className={cn(
            "ops-focusable flex h-8 min-w-0 flex-1 items-center gap-1.5 rounded-md border border-[--color-border] bg-[--color-card] px-2 text-left text-xs",
            disabled && "cursor-not-allowed bg-[--color-muted] text-[--color-muted-foreground]",
          )}
        >
          {value ? (
            <span className="flex min-w-0 items-center gap-1.5">
              <span className="truncate">{displayName ?? "거래처"}</span>
              {isInactive && (
                // 이름을 지우지 않는다 — 과거 비용의 거래처가 "없음" 으로 보이면 안 된다.
                <Badge variant="default" className="shrink-0 px-1 py-0 text-[9px]">
                  비활성 거래처
                </Badge>
              )}
            </span>
          ) : (
            <span className="text-[--color-muted-foreground]">거래처 없음</span>
          )}
        </button>
        {value && !disabled && (
          <Button
            variant="ghost" size="icon-sm"
            onClick={() => onChange(null)}
            aria-label="거래처 선택 해제"
          >
            <X className="h-3 w-3" aria-hidden />
          </Button>
        )}
      </div>

      {open && (
        <div className="absolute z-50 mt-1 w-full min-w-[240px] rounded-md border border-[--color-border] bg-[--color-card] p-2 shadow-lg">
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-[--color-muted-foreground]"
              aria-hidden
            />
            <TextInput
              autoFocus value={q} className="pl-6"
              placeholder="거래처명·메모 검색"
              aria-label="거래처 검색"
              onChange={(e) => setQ(e.target.value)}
            />
          </div>

          <ul className="mt-1 max-h-56 overflow-y-auto" role="listbox" aria-label="거래처 목록">
            {isLoading && (
              <li className="flex items-center gap-1.5 px-2 py-3 text-[11px] text-[--color-muted-foreground]">
                <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                불러오는 중…
              </li>
            )}

            {!isLoading && items.length === 0 && (
              <li className="px-2 py-3 text-[11px] text-[--color-muted-foreground]">
                {debouncedQ ? (
                  <>&quot;{debouncedQ}&quot; 와 일치하는 거래처가 없습니다.</>
                ) : (
                  "등록된 거래처를 찾을 수 없습니다."
                )}
                {!canCreate && (
                  <span className="mt-1 block">
                    거래처 등록 기능은 아직 지원하지 않습니다.
                  </span>
                )}
              </li>
            )}

            {recentItems.length > 0 && (
              <li className="px-2 pb-0.5 pt-1 text-[10px] font-medium text-[--color-muted-foreground]">
                최근 사용
              </li>
            )}
            {[...recentItems, ...restItems].map((p, i) => (
              <React.Fragment key={p.id}>
                {i === recentItems.length && recentItems.length > 0 && (
                  <li className="px-2 pb-0.5 pt-1.5 text-[10px] font-medium text-[--color-muted-foreground]">
                    전체
                  </li>
                )}
                <li>
                  <button
                    type="button" role="option" aria-selected={p.id === value}
                    onClick={() => pick(p)}
                    className="ops-focusable flex w-full items-center gap-1.5 rounded px-2 py-1.5 text-left text-xs hover:bg-[--color-muted]"
                  >
                    <Check
                      className={cn("h-3 w-3 shrink-0",
                                    p.id === value ? "opacity-100" : "opacity-0")}
                      aria-hidden
                    />
                    <span className="min-w-0 flex-1 truncate">{p.display_name}</span>
                    {p.type && (
                      <span className="shrink-0 text-[10px] text-[--color-muted-foreground]">
                        {p.type}
                      </span>
                    )}
                  </button>
                </li>
              </React.Fragment>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
