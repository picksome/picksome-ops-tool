"use client";

/**
 * 비용 카테고리 (실 API).
 *
 * 이 화면이 왜 다시 쓰였나
 * --------------------
 * 직전까지 `settingsAdapter` 라는 메모리 배열을 썼다. "저장했습니다" 토스트는
 * 떴지만 **DB 에는 아무 일도 일어나지 않았고**, 새로고침하면 사라졌다.
 * 비용 카테고리는 순이익 계산의 입력이므로, 저장되지 않는 화면은 단순한 버그가
 * 아니라 **틀린 숫자를 만드는 경로**다.
 *
 * 없던 기능을 지어내지 않는다
 * -----------------------
 * 옛 화면에는 "월 예산", "사용 건수", "상품 원가 포함" 이 있었다. 백엔드
 * `expense_category_policy` 에는 그런 컬럼이 없다. 저장되지 않는 입력칸을 두면
 * 사용자는 예산을 설정했다고 믿는다. 그래서 **화면에서 없앴다.**
 *
 * 실제 컬럼은 다음뿐이다:
 *   name, kind, default_tax_deductible, default_settlement_deductible, sort_order
 *
 * 저장하면 즉시 적용된다
 * ------------------
 * 이 프로젝트의 정책은 생성과 동시에 활성이다(`policy_crud` 주석 참고).
 * "승인해야 적용됩니다" 라고 안내하면 거짓말이 된다.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Save } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageShell, Section } from "@/components/shell/page-shell";
import { TextInput } from "@/components/ui/money-input";
import { ApiError } from "@/lib/api";
import { policiesApi, type Policy } from "@/lib/api/policies";
import {
  EXPENSE_CATEGORY_KINDS,
  EXPENSE_CATEGORY_KIND_LABEL,
  type ExpenseCategoryKind,
  nextVersion,
  today,
} from "@/lib/api/policy-forms";

interface Row {
  /** 서버에 이미 있는 행이면 정책 id, 새 행이면 null. */
  id: string | null;
  version: number;
  name: string;
  kind: ExpenseCategoryKind;
  taxDeductible: boolean;
  settlementDeductible: boolean;
  sortOrder: number;
}

function toRow(p: Policy): Row {
  const f = p.fields as Record<string, unknown>;
  return {
    id: p.id,
    version: p.version,
    name: String(f.name ?? ""),
    kind: (f.kind as ExpenseCategoryKind) ?? "OTHER",
    taxDeductible: Boolean(f.default_tax_deductible),
    settlementDeductible: Boolean(f.default_settlement_deductible),
    sortOrder: Number(f.sort_order ?? 0),
  };
}

function toFields(r: Row) {
  return {
    name: r.name.trim(),
    kind: r.kind,
    default_tax_deductible: r.taxDeductible,
    default_settlement_deductible: r.settlementDeductible,
    sort_order: r.sortOrder,
  };
}

function errorMessage(err: unknown): string {
  if (!(err instanceof ApiError)) return "저장 중 통신에 실패했습니다.";
  if (err.status === 403) return "비용 카테고리를 수정할 권한이 없습니다.";
  return err.message || "저장에 실패했습니다.";
}

export default function ExpenseCategoriesPage() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["policies", "expense_category"],
    queryFn: () => policiesApi.list("expense_category", { limit: 200 }),
  });

  const [draft, setDraft] = React.useState<Row[] | null>(null);
  const [dirty, setDirty] = React.useState(false);

  React.useEffect(() => {
    if (data) {
      setDraft(data.items.map(toRow));
      setDirty(false);
    }
  }, [data]);

  const save = useMutation({
    mutationFn: async () => {
      const rows = draft ?? [];
      // 이름 없는 행은 저장하지 않는다 — 빈 카테고리는 비용 분류를 망가뜨린다.
      const invalid = rows.filter((r) => !r.name.trim());
      if (invalid.length > 0) throw new ApiError("EMPTY_NAME", "이름이 비어 있는 카테고리가 있습니다.", 422);

      let version = nextVersion(data?.items ?? []);
      for (const r of rows) {
        if (r.id) {
          await policiesApi.update("expense_category", r.id, { fields: toFields(r) });
        } else {
          await policiesApi.create("expense_category", {
            version: version++,
            effective_from: today(),
            fields: toFields(r),
          });
        }
      }
    },
    onSuccess: () => {
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["policies", "expense_category"] });
      toast.success("비용 카테고리를 저장했습니다. 이후 계산에 바로 반영됩니다.");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  function patch(i: number, updater: (r: Row) => void) {
    setDraft((prev) => {
      if (!prev) return prev;
      const next = structuredClone(prev);
      updater(next[i]);
      return next;
    });
    setDirty(true);
  }

  function addRow() {
    setDraft((prev) => [
      ...(prev ?? []),
      {
        id: null,
        version: 0,
        name: "",
        kind: "OTHER",
        taxDeductible: true,
        settlementDeductible: true,
        sortOrder: (prev?.length ?? 0) + 1,
      },
    ]);
    setDirty(true);
  }

  if (isLoading || !draft) {
    return (
      <PageShell title="비용 카테고리" variant="detail">
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
      </PageShell>
    );
  }

  return (
    <PageShell
      title="비용 카테고리"
      description="카테고리 설정이 세무 리포트와 순이익 계산에 그대로 반영됩니다"
      actions={
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={addRow}>
            <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            카테고리 추가
          </Button>
          <Button size="sm" onClick={() => save.mutate()} disabled={!dirty || save.isPending}>
            <Save className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            저장
          </Button>
        </div>
      }
      variant="detail"
    >
      <Section title={`카테고리 ${draft.length}개`}>
        {draft.length === 0 ? (
          <p className="text-sm text-[--color-muted-foreground]">
            등록된 카테고리가 없습니다. &quot;카테고리 추가&quot;로 만드세요.
          </p>
        ) : (
          <div className="ops-scroll-x">
            <table className="w-full text-[12px]">
              <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
                <tr>
                  <th scope="col" className="px-2 py-1.5 text-left font-semibold">이름</th>
                  <th scope="col" className="px-2 py-1.5 text-left font-semibold">분류</th>
                  <th scope="col" className="px-2 py-1.5 text-center font-semibold">부가세 공제</th>
                  <th scope="col" className="px-2 py-1.5 text-center font-semibold">순이익 차감</th>
                  <th scope="col" className="px-2 py-1.5 text-right font-semibold">정렬</th>
                  <th scope="col" className="px-2 py-1.5 text-center font-semibold">버전</th>
                </tr>
              </thead>
              <tbody>
                {draft.map((c, i) => (
                  <tr key={c.id ?? `new-${i}`} className="border-t border-[--color-border]">
                    <td className="px-2 py-1.5">
                      <TextInput
                        value={c.name}
                        aria-label={`카테고리 ${i + 1} 이름`}
                        className="w-40"
                        onChange={(e) => patch(i, (x) => { x.name = e.target.value; })}
                      />
                    </td>
                    <td className="px-2 py-1.5">
                      <select
                        value={c.kind}
                        aria-label={`${c.name || `카테고리 ${i + 1}`} 분류`}
                        onChange={(e) =>
                          patch(i, (x) => { x.kind = e.target.value as ExpenseCategoryKind; })
                        }
                        className="ops-focusable h-8 rounded-md border border-[--color-border] bg-[--color-card] px-1.5 text-xs"
                      >
                        {EXPENSE_CATEGORY_KINDS.map((k) => (
                          <option key={k} value={k}>{EXPENSE_CATEGORY_KIND_LABEL[k]}</option>
                        ))}
                      </select>
                    </td>
                    <td className="px-2 py-1.5 text-center">
                      <input
                        type="checkbox"
                        className="ops-focusable"
                        checked={c.taxDeductible}
                        aria-label={`${c.name || `카테고리 ${i + 1}`} 부가세 공제`}
                        onChange={(e) => patch(i, (x) => { x.taxDeductible = e.target.checked; })}
                      />
                    </td>
                    <td className="px-2 py-1.5 text-center">
                      <input
                        type="checkbox"
                        className="ops-focusable"
                        checked={c.settlementDeductible}
                        aria-label={`${c.name || `카테고리 ${i + 1}`} 순이익 차감`}
                        onChange={(e) =>
                          patch(i, (x) => { x.settlementDeductible = e.target.checked; })
                        }
                      />
                    </td>
                    <td className="px-2 py-1.5 text-right">
                      <input
                        type="number"
                        value={c.sortOrder}
                        aria-label={`${c.name || `카테고리 ${i + 1}`} 정렬 순서`}
                        onChange={(e) =>
                          patch(i, (x) => { x.sortOrder = Number(e.target.value) || 0; })
                        }
                        className="ops-focusable h-8 w-16 rounded-md border border-[--color-border] bg-[--color-card] px-1.5 text-right text-xs tabular-nums"
                      />
                    </td>
                    <td className="px-2 py-1.5 text-center text-[--color-muted-foreground] tabular-nums">
                      {c.id ? `v${c.version}` : "신규"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="mt-3 space-y-1.5 text-[11px] text-[--color-muted-foreground]">
          <p>
            <strong className="text-[--color-foreground]">부가세 공제</strong> — 매입세액 공제 대상인지
            여부입니다. 부가세 참고 리포트에 반영됩니다.
          </p>
          <p>
            <strong className="text-[--color-foreground]">순이익 차감</strong> — 사업 순이익에서 차감할지
            여부입니다. 개인 지출을 잘못 등록한 경우 여기를 꺼서 제외합니다.
          </p>
          <p>
            저장하면 <strong className="text-[--color-foreground]">이후 계산에 바로 반영됩니다.</strong>
            이미 확정한 달의 숫자는 그때 버전이 박제돼 있어 변하지 않습니다.
          </p>
        </div>
      </Section>
    </PageShell>
  );
}
