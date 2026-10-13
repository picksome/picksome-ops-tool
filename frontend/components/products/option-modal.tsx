"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Copy, Loader2, Plus, Trash2, Wand2, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { MoneyInput, TextInput } from "@/components/ui/money-input";
import { buildOptionMatrix, productAdapter } from "@/lib/adapters";
import type { ProductOption } from "@/lib/adapters/types";
import { describeApprovalError } from "@/lib/api/approvals";
import {
  failedRowIds, runSequentialSave, savedRowIds, summarize,
  type RowOutcome, type SequentialSaveResult,
} from "@/lib/adapters/option-save";

/**
 * DB 에 컬럼이 없어 저장되지 않는 옵션 필드의 안내 문구.
 *
 * 입력은 받아놓고 저장이 안 되면 사용자는 입력한 걸 잃는다. 그럴 바에는
 * 처음부터 비활성으로 두고 이유를 보여주는 게 낫다.
 */
const UNSUPPORTED_OPTION_REASON =
  "아직 저장되지 않는 항목입니다. 옵션별 상세 스키마가 확정되면 활성화됩니다.";

import { formatKRW } from "@/lib/format";

/**
 * 옵션 편집 모달.
 *
 * 상품 목록의 "옵션 수" 를 누르면 열린다. 페이지 이동을 하지 않는 이유:
 * 옵션 편집은 목록을 훑으며 여러 상품을 연달아 고치는 작업이라,
 * 페이지를 옮기면 스크롤·필터·페이지 위치를 매번 잃는다.
 *
 * 핵심 설계:
 *   - **옵션명은 기본 상품명과 분리해서 관리한다.** 자동 생성명을 항상 보여주되
 *     사용자가 덮어쓸 수 있고, 덮어쓴 값이 우선한다.
 *   - 그룹/값을 바꾸면 조합을 다시 만들되 **기존 행의 가격·재고는 유지**한다.
 *     조합이 사라진 행도 지우지 않고 비활성으로 남긴다 — 판매 이력이 붙어 있을 수 있다.
 */
export function OptionModal({
  productId,
  productName,
  open,
  onOpenChange,
}: {
  productId: string | null;
  productName: string;
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const qc = useQueryClient();
  const [groups, setGroups] = React.useState<Array<{ name: string; values: string[] }>>([]);
  const [rows, setRows] = React.useState<ProductOption[]>([]);
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [bulkExtra, setBulkExtra] = React.useState<number | null>(null);
  const [dirty, setDirty] = React.useState(false);

  const { data: loadedGroups } = useQuery({
    queryKey: ["optionGroups", productId],
    queryFn: () => productAdapter.optionGroups(productId!),
    enabled: !!productId && open,
  });
  const { data: loadedOptions, isLoading } = useQuery({
    queryKey: ["options", productId],
    queryFn: () => productAdapter.options(productId!),
    enabled: !!productId && open,
  });

  React.useEffect(() => {
    if (loadedGroups) setGroups(structuredClone(loadedGroups));
  }, [loadedGroups]);
  React.useEffect(() => {
    if (loadedOptions) {
      setRows(structuredClone(loadedOptions));
      setDirty(false);
      setSelected(new Set());
    }
  }, [loadedOptions]);

  /** 행별 저장 결과. **일부 성공을 전체 성공으로 표시하지 않기 위해** 유지한다. */
  const [outcomes, setOutcomes] = React.useState<Map<string, RowOutcome>>(new Map());
  const [lastResult, setLastResult] = React.useState<SequentialSaveResult | null>(null);
  const [onlyFailed, setOnlyFailed] = React.useState(false);

  const save = useMutation({
    mutationFn: async (retryOnly: boolean) => {
      const original = new Map((loadedOptions ?? []).map((o) => [o.id, o]));
      // 재시도는 실패·충돌 행만 보낸다. 전부 다시 보내면 이미 성공한 행이
      // 또 저장되어 updated_at 이 갱신되고 변경 이력이 지저분해진다.
      const target = retryOnly && lastResult
        ? rows.filter((r) => failedRowIds(lastResult).has(r.id))
        : rows;

      setOutcomes(new Map());
      return runSequentialSave(
        target, original,
        {
          create: (o) => productAdapter.createOption(productId!, o),
          update: (o) => productAdapter.saveOption(o),
        },
        (o) => setOutcomes((prev) => new Map(prev).set(o.optionId, o)),
      );
    },
    onSuccess: (result) => {
      setLastResult(result);
      const s = summarize(result);
      if (s.tone === "success") toast.success(s.title);
      else if (s.tone === "warning") toast.warning(s.title, { description: s.description });
      else toast.info(s.title);

      // **성공한 행만** dirty 를 푼다. 실패 행은 입력값을 그대로 두어야
      // 사용자가 다시 입력하지 않는다.
      const saved = savedRowIds(result);
      if (saved.size > 0) {
        qc.invalidateQueries({ queryKey: ["products"] });
        qc.invalidateQueries({ queryKey: ["options", productId] });
      }
      if (result.allSucceeded) {
        setDirty(false);
        setOnlyFailed(false);
        onOpenChange(false);
      } else {
        // 실패가 있으면 창을 닫지 않는다 — 닫으면 실패 행 입력이 사라진다.
        setOnlyFailed(true);
      }
    },
    onError: (err) => {
      // 여기 오는 건 개별 행 오류가 아니라 루프 자체가 터진 경우다.
      const guide = describeApprovalError(err);
      toast.error(guide.title, { description: guide.description });
    },
  });

  /**
   * 닫기 전 확인. 미저장·실패 행이 있으면 경고한다 —
   * 그냥 닫으면 실패 행에 다시 입력한 값이 사라진다.
   */
  function requestClose() {
    const hasUnsaved = dirty || (lastResult ? lastResult.failed > 0 : false);
    if (hasUnsaved && !window.confirm(
      "저장되지 않았거나 실패한 옵션이 있습니다. 닫으면 입력한 내용이 사라집니다. 닫을까요?",
    )) {
      return;
    }
    onOpenChange(false);
  }

  /** 그룹 조합에서 매트릭스를 다시 만든다. 기존 값은 유지. */
  function regenerate() {
    const next = buildOptionMatrix(productId ?? "p", groups, rows, productName);
    // 새 조합에 없는 기존 행은 삭제하지 않고 비활성으로 보존한다
    const nextKeys = new Set(next.map((o) => JSON.stringify(o.values)));
    const orphans = rows
      .filter((o) => !nextKeys.has(JSON.stringify(o.values)))
      .map((o) => ({ ...o, isActive: false }));
    setRows([...next, ...orphans]);
    setDirty(true);
    if (orphans.length > 0) {
      toast.info(`조합에서 빠진 옵션 ${orphans.length}개는 비활성으로 남겼습니다`);
    }
  }

  function updateRow(id: string, updater: (o: ProductOption) => void) {
    setRows((prev) =>
      prev.map((o) => {
        if (o.id !== id) return o;
        const next = { ...o };
        updater(next);
        return next;
      }),
    );
    setDirty(true);
  }

  function duplicateRow(id: string) {
    const src = rows.find((o) => o.id === id);
    if (!src) return;
    setRows((prev) => {
      const idx = prev.findIndex((o) => o.id === id);
      const copy = { ...structuredClone(src), id: `${id}-copy-${Date.now()}`, optionSku: null };
      return [...prev.slice(0, idx + 1), copy, ...prev.slice(idx + 1)];
    });
    setDirty(true);
  }

  function removeRows(ids: string[]) {
    setRows((prev) => prev.filter((o) => !ids.includes(o.id)));
    setSelected(new Set());
    setDirty(true);
  }

  function applyBulkExtra() {
    if (bulkExtra === null || selected.size === 0) return;
    setRows((prev) =>
      prev.map((o) => (selected.has(o.id) ? { ...o, extraPrice: bulkExtra } : o)),
    );
    setDirty(true);
    toast.success(`${selected.size}개 옵션의 추가 판매가를 ${formatKRW(bulkExtra)}로 변경했습니다`);
  }

  const allSelected = rows.length > 0 && selected.size === rows.length;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        showCloseButton={false}
        className="flex h-[88vh] max-h-[88vh] w-[96vw] max-w-6xl flex-col gap-0 p-0 sm:max-w-6xl"
      >
        <DialogHeader className="shrink-0 gap-1 border-b border-[--color-border] px-4 py-3">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <DialogTitle className="truncate">옵션 편집 — {productName}</DialogTitle>
              <DialogDescription className="mt-0.5 text-[11px]">
                옵션명은 기본 상품명과 별도로 관리됩니다. 자동 생성된 이름을 그대로 쓰거나 직접 고칠 수 있습니다.
              </DialogDescription>
            </div>
            <Button variant="ghost" size="icon-sm" onClick={() => onOpenChange(false)} aria-label="닫기">
              <X className="h-4 w-4" aria-hidden />
            </Button>
          </div>
        </DialogHeader>

        <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
          {/* ── 좌: 옵션 그룹 ── */}
          <aside className="shrink-0 overflow-y-auto border-b border-[--color-border] p-3 lg:w-64 lg:border-b-0 lg:border-r">
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-xs font-semibold">옵션 그룹</h3>
              <Button
                variant="ghost" size="xs"
                onClick={() => {
                  setGroups((g) => [...g, { name: "", values: [] }]);
                  setDirty(true);
                }}
              >
                <Plus className="mr-1 h-3 w-3" aria-hidden />
                추가
              </Button>
            </div>

            <div className="space-y-3">
              {groups.length === 0 && (
                <p className="rounded-md border border-dashed border-[--color-border] p-3 text-center text-[11px] text-[--color-muted-foreground]">
                  그룹을 추가하면 조합이 자동으로 만들어집니다
                </p>
              )}
              {groups.map((g, gi) => (
                <div key={gi} className="rounded-md border border-[--color-border] p-2">
                  <div className="mb-1.5 flex items-center gap-1">
                    <TextInput
                      value={g.name}
                      placeholder="그룹명 (예: 색상)"
                      aria-label={`옵션 그룹 ${gi + 1} 이름`}
                      onChange={(e) => {
                        setGroups((prev) =>
                          prev.map((x, i) => (i === gi ? { ...x, name: e.target.value } : x)),
                        );
                        setDirty(true);
                      }}
                    />
                    <Button
                      variant="ghost" size="icon-sm"
                      aria-label={`${g.name || `그룹 ${gi + 1}`} 삭제`}
                      onClick={() => {
                        setGroups((prev) => prev.filter((_, i) => i !== gi));
                        setDirty(true);
                      }}
                    >
                      <Trash2 className="h-3 w-3 text-[--color-danger]" aria-hidden />
                    </Button>
                  </div>
                  <TextInput
                    value={g.values.join(", ")}
                    placeholder="값을 쉼표로 구분 (네이비, 그레이)"
                    aria-label={`${g.name || `그룹 ${gi + 1}`} 값 목록`}
                    onChange={(e) => {
                      const values = e.target.value.split(",").map((v) => v.trim()).filter(Boolean);
                      setGroups((prev) => prev.map((x, i) => (i === gi ? { ...x, values } : x)));
                      setDirty(true);
                    }}
                  />
                  <p className="mt-1 text-[10px] text-[--color-muted-foreground]">
                    {g.values.length}개 값
                  </p>
                </div>
              ))}
            </div>

            <Button
              className="mt-3 w-full" size="sm" variant="outline"
              onClick={regenerate}
              disabled={groups.length === 0 || groups.some((g) => !g.name || g.values.length === 0)}
            >
              <Wand2 className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              조합 생성 ({groups.reduce((a, g) => a * Math.max(1, g.values.length), 1)}개)
            </Button>

            {/* 자동 생성 옵션명 미리보기 */}
            {groups.length > 0 && groups.every((g) => g.values.length > 0) && (
              <div className="mt-3 rounded-md bg-[--color-muted] p-2">
                <p className="mb-1 text-[10px] font-semibold text-[--color-muted-foreground]">
                  자동 생성 옵션명 미리보기
                </p>
                <p className="break-words text-[11px]">
                  {[productName, ...groups.map((g) => g.values[0])].filter(Boolean).join(" ")}
                </p>
              </div>
            )}
          </aside>

          {/* ── 우: Variant 테이블 ── */}
          <div className="flex min-h-0 flex-1 flex-col">
            {/* 일괄 수정 바 */}
            {selected.size > 0 && (
              <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-[--color-border] bg-[--color-info-bg] px-3 py-2">
                <span className="text-xs font-medium text-[--color-info]">
                  {selected.size}개 선택
                </span>
                <div className="flex items-center gap-1">
                  <label htmlFor="bulk-extra" className="text-[11px]">추가 판매가</label>
                  <MoneyInput
                    id="bulk-extra" className="w-28" value={bulkExtra}
                    onChange={setBulkExtra} allowNegative
                  />
                  <Button size="xs" onClick={applyBulkExtra} disabled={bulkExtra === null}>
                    적용
                  </Button>
                </div>
                <Button
                  size="xs" variant="ghost"
                  onClick={() => removeRows([...selected])}
                  className="text-[--color-danger]"
                >
                  <Trash2 className="mr-1 h-3 w-3" aria-hidden />
                  선택 삭제
                </Button>
                <Button size="xs" variant="ghost" onClick={() => setSelected(new Set())}>
                  선택 해제
                </Button>
              </div>
            )}

            <div className="ops-scroll-x min-h-0 flex-1 overflow-y-auto">
              {isLoading ? (
                <p className="p-6 text-center text-xs text-[--color-muted-foreground]">불러오는 중…</p>
              ) : rows.length === 0 ? (
                <div className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center">
                  <p className="text-sm font-medium">옵션이 없습니다</p>
                  <p className="max-w-xs text-xs text-[--color-muted-foreground]">
                    왼쪽에서 옵션 그룹과 값을 입력한 뒤 &quot;조합 생성&quot;을 누르세요
                  </p>
                </div>
              ) : (
                <table className="w-full border-collapse text-[12px]">
                  <thead className="sticky top-0 z-10 bg-[--color-muted]">
                    <tr className="text-[11px] text-[--color-muted-foreground]">
                      <th scope="col" className="w-9 px-2 py-1.5">
                        <Checkbox
                          checked={allSelected ? true : selected.size > 0 ? "indeterminate" : false}
                          onCheckedChange={(v) =>
                            setSelected(v ? new Set(rows.map((o) => o.id)) : new Set())
                          }
                          aria-label="전체 선택"
                        />
                      </th>
                      <th scope="col" className="min-w-[180px] px-2 py-1.5 text-left font-semibold">옵션명</th>
                      <th scope="col" className="px-2 py-1.5 text-left font-semibold">옵션 SKU</th>
                      <th scope="col" className="px-2 py-1.5 text-right font-semibold">옵션 원가</th>
                      <th scope="col" className="px-2 py-1.5 text-right font-semibold">추가 판매가</th>
                      <th scope="col" className="px-2 py-1.5 text-right font-semibold">재고</th>
                      <th scope="col" className="px-2 py-1.5 text-center font-semibold">판매</th>
                      <th scope="col" className="px-2 py-1.5 text-left font-semibold">소스 옵션 URL</th>
                      <th scope="col" className="px-2 py-1.5 text-left font-semibold">판매처 옵션번호</th>
                      <th scope="col" className="w-16 px-2 py-1.5" />
                    </tr>
                  </thead>
                  <tbody>
                    {rows
                      .filter((o) => !onlyFailed || !lastResult
                        || failedRowIds(lastResult).has(o.id))
                      .map((o) => {
                        const outcome = outcomes.get(o.id);
                        return (
                      <tr
                        key={o.id}
                        className={cn(
                          "border-t border-[--color-border]",
                          !o.isActive && "bg-[--color-muted]/40 text-[--color-muted-foreground]",
                          selected.has(o.id) && "bg-[--color-info-bg]",
                          // 실패 행을 시각적으로 구분한다 — 목록이 길면 못 찾는다.
                          outcome && outcome.state !== "SAVED" && outcome.state !== "SAVING"
                            && "bg-[--color-danger]/10",
                        )}
                      >
                        {outcome && (
                          <td colSpan={0} className="hidden">
                            {/* 상태는 아래 마지막 칸에 표시한다 */}
                          </td>
                        )}
                        <td className="px-2 py-1">
                          <Checkbox
                            checked={selected.has(o.id)}
                            onCheckedChange={(v) =>
                              setSelected((prev) => {
                                const next = new Set(prev);
                                if (v) next.add(o.id);
                                else next.delete(o.id);
                                return next;
                              })
                            }
                            aria-label={`${o.customName ?? o.autoName} 선택`}
                          />
                        </td>
                        <td className="px-2 py-1">
                          <TextInput
                            disabled
                            title={UNSUPPORTED_OPTION_REASON}
                            value={o.customName ?? ""}
                            placeholder={o.autoName}
                            aria-label="옵션명"
                            onChange={(e) =>
                              updateRow(o.id, (x) => { x.customName = e.target.value || null; })
                            }
                          />
                          {!o.customName && (
                            <p className="mt-0.5 truncate text-[10px] text-[--color-muted-foreground]">
                              자동: {o.autoName}
                            </p>
                          )}
                        </td>
                        <td className="px-2 py-1">
                          <TextInput
                            value={o.optionSku ?? ""} aria-label="옵션 SKU" className="w-28"
                            onChange={(e) => updateRow(o.id, (x) => { x.optionSku = e.target.value || null; })}
                          />
                        </td>
                        <td className="px-2 py-1">
                          <MoneyInput
                            className="w-24" value={o.optionCost}
                            onChange={(v) => updateRow(o.id, (x) => { x.optionCost = v; })}
                          />
                        </td>
                        <td className="px-2 py-1">
                          <MoneyInput
                            className="w-24" value={o.extraPrice} allowNegative
                            onChange={(v) => updateRow(o.id, (x) => { x.extraPrice = v ?? 0; })}
                          />
                        </td>
                        <td className="px-2 py-1">
                          <TextInput
                            type="number" className="w-20 text-right"
                            value={o.stock ?? ""} aria-label="재고"
                            onChange={(e) =>
                              updateRow(o.id, (x) => {
                                x.stock = e.target.value === "" ? null : Number(e.target.value);
                              })
                            }
                          />
                        </td>
                        <td className="px-2 py-1 text-center">
                          <Checkbox
                            checked={o.isActive}
                            onCheckedChange={(v) => updateRow(o.id, (x) => { x.isActive = !!v; })}
                            aria-label="판매 상태"
                          />
                        </td>
                        <td className="px-2 py-1">
                          <TextInput
                            disabled
                            title={UNSUPPORTED_OPTION_REASON}
                            value={o.sourceOptionUrl ?? ""} placeholder="아직 저장되지 않음"
                            aria-label="소스 옵션 URL" className="w-40"
                            onChange={(e) => updateRow(o.id, (x) => { x.sourceOptionUrl = e.target.value || null; })}
                          />
                        </td>
                        <td className="px-2 py-1">
                          <TextInput
                            disabled
                            title={UNSUPPORTED_OPTION_REASON}
                            value={o.marketplaceOptionNo ?? ""} aria-label="판매처 옵션번호" className="w-28"
                            onChange={(e) => updateRow(o.id, (x) => { x.marketplaceOptionNo = e.target.value || null; })}
                          />
                        </td>
                        <td className="px-2 py-1">
                          <div className="flex">
                            <Button
                              variant="ghost" size="icon-sm"
                              onClick={() => duplicateRow(o.id)}
                              aria-label="옵션 복제" title="복제"
                            >
                              <Copy className="h-3 w-3" aria-hidden />
                            </Button>
                            <Button
                              variant="ghost" size="icon-sm"
                              onClick={() => removeRows([o.id])}
                              aria-label="옵션 삭제" title="삭제"
                            >
                              <Trash2 className="h-3 w-3 text-[--color-danger]" aria-hidden />
                            </Button>
                          </div>
                        </td>
                        <td className="px-2 py-1 text-[10px]">
                          {outcome?.state === "SAVING" && (
                            <span className="text-[--color-muted-foreground]">저장 중…</span>
                          )}
                          {outcome?.state === "SAVED" && (
                            <span className="text-[--color-success]">저장됨</span>
                          )}
                          {(outcome?.state === "FAILED" || outcome?.state === "CONFLICT") && (
                            <span className="text-[--color-danger]">
                              {outcome.state === "CONFLICT" ? "충돌" : "실패"}
                              {outcome.errorCode && (
                                <span className="block font-mono">{outcome.errorCode}</span>
                              )}
                              <span className="block">{outcome.errorMessage}</span>
                              {outcome.needsRefresh && (
                                <span className="block">
                                  다른 사용자가 먼저 수정했습니다. 목록을 다시 불러오세요.
                                </span>
                              )}
                            </span>
                          )}
                        </td>
                      </tr>
                        );
                      })}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </div>

        <div className="flex shrink-0 items-center justify-between gap-2 border-t border-[--color-border] bg-[--color-muted] px-4 py-3">
          <div className="min-w-0 text-[11px] text-[--color-muted-foreground]">
            <p>
              옵션 {rows.length}개 · 판매중 {rows.filter((o) => o.isActive).length}개
              {dirty && <span className="ml-2 text-[--color-warning]">저장되지 않음</span>}
            </p>
            {lastResult && !lastResult.allSucceeded && (
              // 일부 성공을 전체 성공처럼 말하지 않는다.
              <p className="mt-0.5 text-[--color-warning]">
                총 {lastResult.attempted}건 · 성공 {lastResult.succeeded}건 ·
                실패 {lastResult.failed}건
              </p>
            )}
          </div>
          <div className="flex gap-2">
            {lastResult && lastResult.failed > 0 && (
              <>
                <Button
                  variant="ghost" size="sm"
                  onClick={() => setOnlyFailed((v) => !v)}
                >
                  {onlyFailed ? "전체 보기" : "실패한 옵션만 보기"}
                </Button>
                <Button
                  variant="outline" size="sm"
                  disabled={save.isPending}
                  onClick={() => save.mutate(true)}
                >
                  실패 건 다시 시도
                </Button>
              </>
            )}
            <Button variant="outline" size="sm" onClick={() => requestClose()}>취소</Button>
            <Button size="sm" onClick={() => save.mutate(false)}
                    disabled={!dirty || save.isPending}>
              {save.isPending && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />}
              저장
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
