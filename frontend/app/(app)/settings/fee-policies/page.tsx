"use client";

/**
 * 판매처 수수료 정책 (실 API).
 *
 * 이 화면이 왜 다시 쓰였나
 * --------------------
 * 직전까지 `settingsAdapter`(메모리 배열)에 저장했다. 새로고침하면 사라졌고,
 * 그런데도 마진 계산기·판매장부·상품 드로어가 그 값으로 수수료를 계산했다.
 * 즉 **화면에 뜨던 마진이 지어낸 수수료로 만들어진 숫자**였다.
 *
 * 부가세 포함 여부가 왜 따로 있나
 * ---------------------------
 * 마켓마다 수수료율 표기가 다르다. 틀리면 **수수료가 통째로 10% 어긋난다.**
 * 확인하지 않은 값을 기본값으로 채우지 않고 "미확인" 으로 두며(M036 은 이 컬럼을
 * nullable 로 만들었다), 계산은 그동안 부가세 별도로 본다 — 마진을 부풀리지
 * 않는 쪽이다.
 *
 * 저장하면 즉시 적용된다
 * ------------------
 * 이 프로젝트의 정책은 생성과 동시에 활성이다. "승인해야 적용됩니다" 는 거짓말이다.
 * 이미 확정한 달은 그때 버전이 박제돼 있어 변하지 않는다.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertTriangle, Plus, Save } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageShell, Section } from "@/components/shell/page-shell";
import { PercentInput, TextInput } from "@/components/ui/money-input";
import { ApiError } from "@/lib/api";
import { policiesApi, type Policy } from "@/lib/api/policies";
import { savePolicyWithApproval } from "@/lib/api/policy-approval";
import { TierEditor, type Tier } from "@/components/settings/tier-editor";
import { FEE_TYPES, FEE_TYPE_LABEL, type FeeType, nextVersion, today } from "@/lib/api/policy-forms";
import { useSaleChannels } from "@/hooks/useSaleChannels";

interface Row {
  id: string | null;
  version: number;
  marketplace: string;
  categoryPath: string;
  feeType: FeeType;
  rate: number | null;
  fixedAmount: number | null;
  /** null = 아직 확인하지 않음. 기본값으로 채우지 않는다. */
  feeVatIncluded: boolean | null;
  tiers: Tier[] | null;
  sourceUrl: string;
}

function toRow(p: Policy): Row {
  const f = p.fields as Record<string, unknown>;
  return {
    id: p.id,
    version: p.version,
    marketplace: String(f.marketplace ?? ""),
    categoryPath: String(f.category_path ?? ""),
    feeType: (f.fee_type as FeeType) ?? "RATE",
    rate: f.rate == null ? null : Number(f.rate),
    fixedAmount: f.fixed_amount == null ? null : Number(f.fixed_amount),
    feeVatIncluded: f.fee_vat_included == null ? null : Boolean(f.fee_vat_included),
    tiers: (f.tier_json as Tier[] | null) ?? null,
    sourceUrl: p.source_url ?? "",
  };
}

function toFields(r: Row) {
  return {
    marketplace: r.marketplace.trim(),
    category_path: r.categoryPath.trim() || null,
    fee_type: r.feeType,
    // 정률이면 rate, 정액이면 fixed_amount 만 채운다 — 둘 다 넣으면
    // 어느 쪽으로 계산했는지 나중에 알 수 없다.
    rate: r.feeType === "RATE" ? r.rate : null,
    fixed_amount: r.feeType === "FIXED" ? r.fixedAmount : null,
    tier_json: r.feeType === "TIERED" ? r.tiers : null,
    fee_vat_included: r.feeVatIncluded,
  };
}

function errorMessage(err: unknown): string {
  if (!(err instanceof ApiError)) return "저장 중 통신에 실패했습니다.";
  if (err.status === 403) return "수수료 정책을 수정할 권한이 없습니다.";
  return err.message || "저장에 실패했습니다.";
}

export default function FeePoliciesPage() {
  const qc = useQueryClient();
  const { channels } = useSaleChannels();
  const { data, isLoading } = useQuery({
    queryKey: ["policies", "marketplace_fee"],
    queryFn: () => policiesApi.list("marketplace_fee", { limit: 200 }),
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
      if (rows.some((r) => !r.marketplace.trim())) {
        throw new ApiError("EMPTY_MARKETPLACE", "판매처가 비어 있는 정책이 있습니다.", 422);
      }
      let version = nextVersion(data?.items ?? []);
      const byId = new Map((data?.items ?? []).map((p) => [p.id, p]));
      let approvals = 0;

      for (const r of rows) {
        if (r.id) {
          // 수수료율은 마진의 입력값이라 직접 못 바꾼다. 승인 요청으로 나간다.
          const result = await savePolicyWithApproval({
            kind: "marketplace_fee",
            policyId: r.id,
            current: (byId.get(r.id)?.fields ?? {}) as Record<string, unknown>,
            next: toFields(r) as Record<string, unknown>,
            sourceUrl: r.sourceUrl.trim() || null,
            reason: "수수료 정책 변경",
          });
          if (result.approvalRequestId) approvals += 1;
        } else {
          await policiesApi.create("marketplace_fee", {
            version: version++,
            effective_from: today(),
            source_url: r.sourceUrl.trim() || null,
            fields: toFields(r),
          });
        }
      }
      return approvals;
    },
    onSuccess: (approvals) => {
      setDirty(false);
      // 계산 화면이 같은 queryKey 를 읽는다.
      qc.invalidateQueries({ queryKey: ["policies", "marketplace_fee"] });
      qc.invalidateQueries({ queryKey: ["approvals"] });
      toast.success(
        approvals > 0
          ? `승인 요청 ${approvals}건을 만들었습니다. 승인되면 새 버전으로 적용됩니다 — `
            + "기존 버전은 그대로 남아 과거 달의 계산이 바뀌지 않습니다."
          : "수수료 정책을 저장했습니다.",
      );
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
        id: null, version: 0, marketplace: channels[0]?.code ?? "", categoryPath: "",
        feeType: "RATE", rate: null, fixedAmount: null, tiers: null,
        feeVatIncluded: null, sourceUrl: "",
      },
    ]);
    setDirty(true);
  }

  if (isLoading || !draft) {
    return (
      <PageShell title="수수료 정책" variant="detail">
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
      </PageShell>
    );
  }

  const unverified = draft.filter((r) => r.feeVatIncluded === null).length;

  return (
    <PageShell
      title="수수료 정책"
      description="판매처 수수료가 마진 계산에 그대로 반영됩니다"
      actions={
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={addRow}>
            <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            정책 추가
          </Button>
          <Button size="sm" onClick={() => save.mutate()} disabled={!dirty || save.isPending}>
            <Save className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            저장
          </Button>
        </div>
      }
      variant="detail"
    >
      {unverified > 0 && (
        <div className="mb-3 flex items-start gap-1.5 rounded-md border border-[--color-warning] bg-[--color-surface-muted] px-3 py-2 text-xs">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[--color-warning]" aria-hidden />
          <span>
            부가세 포함 여부가 확인되지 않은 정책이 {unverified}건 있습니다. 잘못 잡으면
            수수료가 10% 어긋납니다. 확인 전까지는 <strong>부가세 별도</strong>로 계산합니다.
          </span>
        </div>
      )}

      <Section title={`정책 ${draft.length}개`}>
        {draft.length === 0 ? (
          <p className="text-sm text-[--color-muted-foreground]">
            등록된 수수료 정책이 없습니다. 정책이 없으면 마진 계산에 수수료가 반영되지 않습니다.
          </p>
        ) : (
          <div className="ops-scroll-x">
            <table className="w-full text-[12px]">
              <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
                <tr>
                  <th scope="col" className="px-2 py-1.5 text-left font-semibold">판매처</th>
                  <th scope="col" className="px-2 py-1.5 text-left font-semibold">카테고리</th>
                  <th scope="col" className="px-2 py-1.5 text-left font-semibold">방식</th>
                  <th scope="col" className="px-2 py-1.5 text-right font-semibold">값</th>
                  <th scope="col" className="px-2 py-1.5 text-left font-semibold">부가세</th>
                  <th scope="col" className="px-2 py-1.5 text-center font-semibold">버전</th>
                </tr>
              </thead>
              <tbody>
                {draft.map((r, i) => (
                  <tr key={r.id ?? `new-${i}`} className="border-t border-[--color-border]">
                    <td className="px-2 py-1.5">
                      <select
                        value={r.marketplace}
                        aria-label={`정책 ${i + 1} 판매처`}
                        onChange={(e) => patch(i, (x) => { x.marketplace = e.target.value; })}
                        className="ops-focusable h-8 rounded-md border border-[--color-border] bg-[--color-card] px-1.5 text-xs"
                      >
                        <option value="">선택</option>
                        {channels.map((c) => (
                          <option key={c.code} value={c.code}>{c.display_name}</option>
                        ))}
                      </select>
                    </td>
                    <td className="px-2 py-1.5">
                      <TextInput
                        value={r.categoryPath}
                        placeholder="전체"
                        aria-label={`정책 ${i + 1} 카테고리`}
                        className="w-32"
                        onChange={(e) => patch(i, (x) => { x.categoryPath = e.target.value; })}
                      />
                    </td>
                    <td className="px-2 py-1.5">
                      <select
                        value={r.feeType}
                        aria-label={`정책 ${i + 1} 방식`}
                        onChange={(e) => patch(i, (x) => { x.feeType = e.target.value as FeeType; })}
                        className="ops-focusable h-8 rounded-md border border-[--color-border] bg-[--color-card] px-1.5 text-xs"
                      >
                        {FEE_TYPES.map((t) => (
                          <option key={t} value={t}>{FEE_TYPE_LABEL[t]}</option>
                        ))}
                      </select>
                    </td>
                    <td className="px-2 py-1.5 text-right">
                      {r.feeType === "RATE" ? (
                        <PercentInput
                          className="w-24"
                          value={r.rate}
                          onChange={(v) => patch(i, (x) => { x.rate = v; })}
                        />
                      ) : r.feeType === "FIXED" ? (
                        <input
                          type="number"
                          value={r.fixedAmount ?? ""}
                          aria-label={`정책 ${i + 1} 정액 수수료`}
                          onChange={(e) =>
                            patch(i, (x) => {
                              x.fixedAmount = e.target.value === "" ? null : Number(e.target.value);
                            })
                          }
                          className="ops-focusable h-8 w-24 rounded-md border border-[--color-border] bg-[--color-card] px-1.5 text-right text-xs tabular-nums"
                        />
                      ) : (
                        <span className="text-[11px] text-[--color-muted-foreground]">
                          {r.tiers?.length
                            ? `${r.tiers.length}개 구간`
                            : "구간 미입력"}
                        </span>
                      )}
                    </td>
                    <td className="px-2 py-1.5">
                      <select
                        value={r.feeVatIncluded === null ? "" : String(r.feeVatIncluded)}
                        aria-label={`정책 ${i + 1} 부가세 포함 여부`}
                        onChange={(e) =>
                          patch(i, (x) => {
                            x.feeVatIncluded = e.target.value === "" ? null : e.target.value === "true";
                          })
                        }
                        className="ops-focusable h-8 rounded-md border border-[--color-border] bg-[--color-card] px-1.5 text-xs"
                      >
                        <option value="">미확인</option>
                        <option value="true">포함</option>
                        <option value="false">별도</option>
                      </select>
                    </td>
                    <td className="px-2 py-1.5 text-center tabular-nums text-[--color-muted-foreground]">
                      {r.id ? `v${r.version}` : "신규"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {draft.map((r, i) =>
          r.feeType !== "TIERED" ? null : (
            <div key={`tiers-${r.id ?? i}`} className="mt-4">
              <p className="mb-1.5 text-[12px] font-medium">
                {r.marketplace || `정책 ${i + 1}`} 구간별 수수료
              </p>
              <TierEditor
                tiers={r.tiers ?? []}
                onChange={(t) => patch(i, (x) => { x.tiers = t; })}
              />
            </div>
          ),
        )}

        <p className="mt-3 text-[11px] text-[--color-muted-foreground]">
          수수료율·구간 변경은 <strong className="text-[--color-foreground]">승인이 필요합니다.</strong>
          승인되면 새 버전으로 적용되고, 기존 버전은 그대로 남아 이미 확정한 달의
          계산이 바뀌지 않습니다.
        </p>
      </Section>
    </PageShell>
  );
}
