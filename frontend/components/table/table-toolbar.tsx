"use client";

import * as React from "react";
import { Columns3, Download, Bookmark, BookmarkPlus, Trash2, Rows3, GripVertical } from "lucide-react";
import type { VisibilityState, ColumnOrderState } from "@tanstack/react-table";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Checkbox } from "@/components/ui/checkbox";
import type { TableDensity } from "@/components/table/data-table";

/* ────────────────────────────────────────────────────────────────
 * 표시 열 선택 + 열 순서 변경
 * ──────────────────────────────────────────────────────────────── */

export interface ColumnSpec {
  id: string;
  label: string;
  /** 끌 수 없는 열 (상품명처럼 없으면 행을 식별할 수 없는 것) */
  locked?: boolean;
}

/**
 * 열 설정 팝오버.
 *
 * 순서 변경은 드래그 대신 ↑↓ 버튼을 쓴다. 드래그는 터치·키보드 접근성을 직접
 * 구현해야 하고, 12개 남짓 목록에서는 버튼이 오히려 정확하고 빠르다.
 */
export function ColumnSettings({
  specs,
  visibility,
  onVisibilityChange,
  order,
  onOrderChange,
  density,
  onDensityChange,
}: {
  specs: ColumnSpec[];
  visibility: VisibilityState;
  onVisibilityChange: (next: VisibilityState) => void;
  order: ColumnOrderState;
  onOrderChange: (next: ColumnOrderState) => void;
  density?: TableDensity;
  onDensityChange?: (next: TableDensity) => void;
}) {
  const ordered = React.useMemo(() => {
    const byId = new Map(specs.map((s) => [s.id, s]));
    const seen = new Set<string>();
    const out: ColumnSpec[] = [];
    for (const id of order) {
      const spec = byId.get(id);
      if (spec) { out.push(spec); seen.add(id); }
    }
    for (const s of specs) if (!seen.has(s.id)) out.push(s);
    return out;
  }, [specs, order]);

  const visibleCount = ordered.filter((s) => visibility[s.id] !== false).length;

  function move(id: string, delta: number) {
    const ids = ordered.map((s) => s.id);
    const idx = ids.indexOf(id);
    const next = idx + delta;
    if (idx < 0 || next < 0 || next >= ids.length) return;
    [ids[idx], ids[next]] = [ids[next], ids[idx]];
    onOrderChange(ids);
  }

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" title="표시 열 설정">
          <Columns3 className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          열 {visibleCount}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 p-0">
        <div className="border-b border-[--color-border] px-3 py-2">
          <p className="text-xs font-semibold">표시 열</p>
          <p className="mt-0.5 text-[11px] text-[--color-muted-foreground]">
            체크로 표시 여부, 화살표로 순서를 바꿉니다
          </p>
        </div>
        <div className="max-h-72 overflow-y-auto py-1">
          {ordered.map((spec, i) => {
            const checked = visibility[spec.id] !== false;
            return (
              <div
                key={spec.id}
                className="flex items-center gap-2 px-3 py-1.5 hover:bg-[--color-muted]"
              >
                <GripVertical className="h-3 w-3 text-[--color-muted-foreground]" aria-hidden />
                <Checkbox
                  id={`col-${spec.id}`}
                  checked={checked}
                  disabled={spec.locked}
                  onCheckedChange={(v) =>
                    onVisibilityChange({ ...visibility, [spec.id]: !!v })
                  }
                />
                <label
                  htmlFor={`col-${spec.id}`}
                  className={cn(
                    "flex-1 cursor-pointer text-xs",
                    spec.locked && "cursor-default text-[--color-muted-foreground]",
                  )}
                >
                  {spec.label}
                  {spec.locked && <span className="ml-1 text-[10px]">(고정)</span>}
                </label>
                <div className="flex">
                  <Button
                    variant="ghost" size="icon-sm"
                    onClick={() => move(spec.id, -1)}
                    disabled={i === 0}
                    aria-label={`${spec.label} 위로`}
                  >
                    <span aria-hidden>↑</span>
                  </Button>
                  <Button
                    variant="ghost" size="icon-sm"
                    onClick={() => move(spec.id, 1)}
                    disabled={i === ordered.length - 1}
                    aria-label={`${spec.label} 아래로`}
                  >
                    <span aria-hidden>↓</span>
                  </Button>
                </div>
              </div>
            );
          })}
        </div>

        {onDensityChange && (
          <div className="border-t border-[--color-border] px-3 py-2">
            <p className="mb-1.5 flex items-center gap-1 text-xs font-semibold">
              <Rows3 className="h-3 w-3" aria-hidden /> 행 높이
            </p>
            <div className="flex gap-1">
              {([
                { key: "compact", label: "좁게" },
                { key: "normal", label: "보통" },
                { key: "comfortable", label: "넓게" },
              ] as const).map((d) => (
                <button
                  key={d.key}
                  type="button"
                  aria-pressed={density === d.key}
                  onClick={() => onDensityChange(d.key)}
                  className={cn(
                    "ops-focusable flex-1 rounded-md border px-2 py-1 text-[11px]",
                    density === d.key
                      ? "border-[--color-accent] bg-[--color-accent-bg] text-[--color-accent]"
                      : "border-[--color-border] hover:bg-[--color-muted]",
                  )}
                >
                  {d.label}
                </button>
              ))}
            </div>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}

/* ────────────────────────────────────────────────────────────────
 * 저장된 보기 (saved views)
 * ──────────────────────────────────────────────────────────────── */

export interface SavedView {
  id: string;
  name: string;
  /** URL query string — 필터·정렬·열 설정이 전부 여기 담긴다 */
  query: string;
}

const VIEW_STORAGE_PREFIX = "ops.savedViews.";

export function loadSavedViews(scope: string): SavedView[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(VIEW_STORAGE_PREFIX + scope);
    return raw ? (JSON.parse(raw) as SavedView[]) : [];
  } catch {
    return [];
  }
}

export function persistSavedViews(scope: string, views: SavedView[]) {
  try {
    window.localStorage.setItem(VIEW_STORAGE_PREFIX + scope, JSON.stringify(views));
  } catch {
    // 저장 실패(용량/프라이빗 모드)는 조용히 무시 — 보기 저장은 부가 기능이다
  }
}

/**
 * 저장된 보기.
 *
 * 서버에 저장하지 않고 localStorage 를 쓴다 — 아직 BE 엔드포인트가 없고,
 * "내가 자주 보는 필터"는 개인 설정 성격이라 로컬로도 충분하다.
 * BE 가 생기면 `loadSavedViews`/`persistSavedViews` 두 함수만 교체하면 된다.
 */
export function SavedViews({
  scope,
  currentQuery,
  onApply,
}: {
  scope: string;
  currentQuery: string;
  onApply: (query: string) => void;
}) {
  const [views, setViews] = React.useState<SavedView[]>([]);
  const [name, setName] = React.useState("");

  React.useEffect(() => setViews(loadSavedViews(scope)), [scope]);

  function save() {
    const trimmed = name.trim();
    if (!trimmed) return;
    const next = [
      ...views.filter((v) => v.name !== trimmed),
      { id: `${Date.now()}`, name: trimmed, query: currentQuery },
    ];
    setViews(next);
    persistSavedViews(scope, next);
    setName("");
  }

  function remove(id: string) {
    const next = views.filter((v) => v.id !== id);
    setViews(next);
    persistSavedViews(scope, next);
  }

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" title="저장된 보기">
          <Bookmark className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          보기{views.length > 0 && ` ${views.length}`}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 p-0">
        <div className="border-b border-[--color-border] px-3 py-2">
          <p className="text-xs font-semibold">저장된 보기</p>
        </div>
        <div className="max-h-56 overflow-y-auto py-1">
          {views.length === 0 ? (
            <p className="px-3 py-4 text-center text-[11px] text-[--color-muted-foreground]">
              현재 필터를 이름을 붙여 저장하면
              <br />
              다음에 한 번에 불러올 수 있습니다
            </p>
          ) : (
            views.map((v) => (
              <div key={v.id} className="flex items-center gap-1 px-2 py-1 hover:bg-[--color-muted]">
                <button
                  type="button"
                  onClick={() => onApply(v.query)}
                  className="ops-focusable flex-1 truncate rounded px-1 py-1 text-left text-xs"
                  title={v.query || "(필터 없음)"}
                >
                  {v.name}
                </button>
                <Button
                  variant="ghost" size="icon-sm"
                  onClick={() => remove(v.id)}
                  aria-label={`${v.name} 삭제`}
                >
                  <Trash2 className="h-3 w-3 text-[--color-danger]" aria-hidden />
                </Button>
              </div>
            ))
          )}
        </div>
        <div className="flex items-center gap-1 border-t border-[--color-border] p-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && save()}
            placeholder="현재 필터 이름"
            aria-label="저장할 보기 이름"
            className="ops-focusable h-7 flex-1 rounded-md border border-[--color-border] px-2 text-xs"
          />
          <Button size="sm" onClick={save} disabled={!name.trim()}>
            <BookmarkPlus className="h-3.5 w-3.5" aria-hidden />
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

/* ────────────────────────────────────────────────────────────────
 * CSV / Excel 내보내기
 * ──────────────────────────────────────────────────────────────── */

/**
 * 현재 화면의 행을 CSV 로 내보낸다.
 *
 * - **UTF-8 BOM 을 붙인다.** 없으면 엑셀에서 한글이 깨진다. 운영툴에서 제일 자주 나오는 민원.
 * - 값에 쉼표·따옴표·줄바꿈이 있으면 큰따옴표로 감싸고 내부 따옴표는 두 번 쓴다.
 * - 숫자로 보이는 문자열(주문번호 `00123`)은 앞에 탭을 붙여 엑셀이 숫자로 바꾸지 않게 한다.
 */
export function toCsv<T>(
  rows: T[],
  columns: Array<{ header: string; value: (row: T) => string | number | null | undefined }>,
): string {
  const escape = (raw: string | number | null | undefined): string => {
    if (raw === null || raw === undefined) return "";
    let s = String(raw);
    if (/^0\d+$/.test(s)) s = `\t${s}`;
    if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
    return s;
  };
  const head = columns.map((c) => escape(c.header)).join(",");
  const body = rows.map((r) => columns.map((c) => escape(c.value(r))).join(",")).join("\r\n");
  return `﻿${head}\r\n${body}`;
}

export function downloadCsv(filename: string, content: string) {
  const blob = new Blob([content], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename.endsWith(".csv") ? filename : `${filename}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export function ExportButton({
  onExport,
  disabled,
  label = "내보내기",
}: {
  onExport: () => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <Button variant="outline" size="sm" onClick={onExport} disabled={disabled} title={`${label} (CSV)`}>
      <Download className="mr-1.5 h-3.5 w-3.5" aria-hidden />
      {label}
    </Button>
  );
}

/* ────────────────────────────────────────────────────────────────
 * 열 설정 영속화 훅
 * ──────────────────────────────────────────────────────────────── */

const COLUMN_PREF_PREFIX = "ops.columns.";

export function useColumnPreferences(scope: string, specs: ColumnSpec[], defaultHidden: string[] = []) {
  const [visibility, setVisibility] = React.useState<VisibilityState>({});
  const [order, setOrder] = React.useState<ColumnOrderState>([]);
  const [density, setDensity] = React.useState<TableDensity>("normal");
  const [loaded, setLoaded] = React.useState(false);

  React.useEffect(() => {
    try {
      const raw = window.localStorage.getItem(COLUMN_PREF_PREFIX + scope);
      if (raw) {
        const parsed = JSON.parse(raw) as {
          visibility?: VisibilityState;
          order?: ColumnOrderState;
          density?: TableDensity;
        };
        setVisibility(parsed.visibility ?? {});
        setOrder(parsed.order ?? specs.map((s) => s.id));
        setDensity(parsed.density ?? "normal");
      } else {
        setVisibility(Object.fromEntries(defaultHidden.map((id) => [id, false])));
        setOrder(specs.map((s) => s.id));
      }
    } catch {
      setOrder(specs.map((s) => s.id));
    }
    setLoaded(true);
    // scope 가 바뀔 때만 다시 읽는다 — specs 는 매 렌더 새 배열이라 의존성에 넣으면 무한 루프
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope]);

  React.useEffect(() => {
    if (!loaded) return;
    try {
      window.localStorage.setItem(
        COLUMN_PREF_PREFIX + scope,
        JSON.stringify({ visibility, order, density }),
      );
    } catch {
      // 저장 실패는 무시 — 표시 설정은 부가 기능
    }
  }, [scope, visibility, order, density, loaded]);

  return { visibility, setVisibility, order, setOrder, density, setDensity };
}
