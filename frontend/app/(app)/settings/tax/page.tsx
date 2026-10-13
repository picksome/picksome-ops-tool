"use client";

/**
 * 사업자·세금 설정 (실 API).
 *
 * 이 화면이 왜 다시 쓰였나
 * --------------------
 * 직전까지 `settingsAdapter`(메모리 배열)에 저장했다. 세율을 바꿔도 새로고침하면
 * 사라졌고, 그런데도 마진 계산기·판매장부·상품 드로어가 그 값으로 부가세를
 * 계산했다. **세율을 저장하지 못하는 화면은 버그가 아니라 틀린 마진을 만드는 경로다.**
 *
 * 세율을 코드에 넣지 않는다
 * ---------------------
 * 세법은 바뀌고 업종별 비율은 세무 판단이 필요하다. 규칙마다 버전과 유효기간이
 * 붙어 과거 계산을 그대로 재현할 수 있다.
 *
 * 확인되지 않은 값을 정답처럼 두지 않는다
 * --------------------------------
 * 매입세액·수입세액 공제 방식은 M036 에서 컬럼이 생겼지만 기존 규칙은 비어 있다.
 * 비어 있으면 계산은 **공제 없음** 으로 본다(마진을 부풀리지 않는 쪽) 그리고
 * 화면이 "확인 필요" 를 띄운다. 기본값으로 채우면 그게 확인된 값이 된다.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertTriangle, Plus, Save, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageShell, Section, Field } from "@/components/shell/page-shell";
import { PercentInput, TextInput } from "@/components/ui/money-input";
import { ApiError } from "@/lib/api";
import { policiesApi, type Policy } from "@/lib/api/policies";
import { savePolicyWithApproval } from "@/lib/api/policy-approval";
import {
  BUSINESS_TYPES, BUSINESS_TYPE_LABEL, TAX_CALC_METHODS, TAX_CALC_METHOD_LABEL,
  TAX_TYPES, TAX_TYPE_LABEL, VAT_INCLUSIONS, VAT_INCLUSION_LABEL,
  type BusinessType, type TaxCalcMethod, type TaxType, type VatInclusion,
  nextVersion, today,
} from "@/lib/api/policy-forms";
import { formatDate } from "@/lib/format";

const INPUT_VAT = [
  { value: "", label: "미확인 — 공제 없음으로 계산합니다" },
  { value: "FULL_CREDIT", label: "적격증빙 전액 공제 (일반과세)" },
  { value: "FLAT_RATE_CREDIT", label: "매입액 × 고정 공제율 (간이과세)" },
  { value: "NO_CREDIT", label: "공제 없음 (면세)" },
] as const;

const IMPORT_VAT = [
  { value: "", label: "미확인 — 원가 포함으로 계산합니다" },
  { value: "CREDITABLE", label: "공제 가능 — 원가에 넣지 않음" },
  { value: "NOT_CREDITABLE", label: "공제 불가 — 원가에 포함" },
] as const;

const SELECT_CLASS =
  "ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs";

interface Row {
  id: string | null;
  version: number;
  taxType: TaxType;
  businessType: BusinessType | "";
  vatRate: number | null;
  calcMethod: TaxCalcMethod;
  vatInclusion: VatInclusion;
  appliesTo: string;
  inputVat: string;
  importVat: string;
  approvedAt: string | null;
  memo: string;
}

function toRow(p: Policy): Row {
  const f = p.fields as Record<string, unknown>;
  return {
    id: p.id,
    version: p.version,
    taxType: (f.tax_type as TaxType) ?? "GENERAL",
    businessType: (f.business_type as BusinessType) ?? "",
    vatRate: f.vat_rate == null ? null : Number(f.vat_rate),
    calcMethod: (f.calculation_method as TaxCalcMethod) ?? "STANDARD_RATIO",
    vatInclusion: (f.default_vat_inclusion as VatInclusion) ?? "INCLUDED",
    appliesTo: String(f.applies_to ?? "ALL"),
    inputVat: (f.input_vat_credit_policy as string | null) ?? "",
    importVat: (f.import_vat_credit_policy as string | null) ?? "",
    approvedAt: p.approved_at,
    memo: p.memo ?? "",
  };
}

function toFields(r: Row) {
  return {
    tax_type: r.taxType,
    business_type: r.businessType || null,
    vat_rate: r.vatRate ?? 0,
    calculation_method: r.calcMethod,
    default_vat_inclusion: r.vatInclusion,
    applies_to: r.appliesTo.trim() || "ALL",
    // 빈 문자열은 "미확인" 이다. 빈 문자열을 그대로 보내면 CHECK 에 걸린다.
    input_vat_credit_policy: r.inputVat || null,
    import_vat_credit_policy: r.importVat || null,
  };
}

/** 계산에 쓰이는 값이 전부 확인됐는가. 하나라도 비면 화면이 경고한다. */
function isVerified(r: Row): boolean {
  return Boolean(r.inputVat && r.importVat);
}

function errorMessage(err: unknown): string {
  if (!(err instanceof ApiError)) return "저장 중 통신에 실패했습니다.";
  if (err.status === 403) return "세금 설정을 수정할 권한이 없습니다.";
  return err.message || "저장에 실패했습니다.";
}

export default function TaxSettingsPage() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["policies", "tax"],
    queryFn: () => policiesApi.list("tax", { limit: 100 }),
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
      let version = nextVersion(data?.items ?? []);
      const byId = new Map((data?.items ?? []).map((p) => [p.id, p]));
      let approvals = 0;

      for (const r of rows) {
        if (r.id) {
          // 세율은 마진·순이익의 입력값이라 직접 못 바꾼다. 승인 요청으로 나간다.
          const result = await savePolicyWithApproval({
            kind: "tax",
            policyId: r.id,
            current: (byId.get(r.id)?.fields ?? {}) as Record<string, unknown>,
            next: toFields(r) as Record<string, unknown>,
            memo: r.memo || null,
            reason: "세금 정책 변경",
          });
          if (result.approvalRequestId) approvals += 1;
        } else {
          await policiesApi.create("tax", {
            version: version++,
            effective_from: today(),
            memo: r.memo || null,
            fields: toFields(r),
          });
        }
      }
      return approvals;
    },
    onSuccess: (approvals) => {
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["policies", "tax"] });
      qc.invalidateQueries({ queryKey: ["approvals"] });
      toast.success(
        approvals > 0
          ? `승인 요청 ${approvals}건을 만들었습니다. 승인되면 새 버전으로 적용됩니다 — `
            + "기존 버전은 그대로 남아 과거 달의 계산이 바뀌지 않습니다."
          : "세금 규칙을 저장했습니다.",
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

  function addRule() {
    setDraft((prev) => [
      ...(prev ?? []),
      {
        id: null, version: 0, taxType: "GENERAL", businessType: "",
        vatRate: 0.1, calcMethod: "STANDARD_RATIO", vatInclusion: "INCLUDED",
        appliesTo: "ALL", inputVat: "", importVat: "", approvedAt: null, memo: "",
      },
    ]);
    setDirty(true);
  }

  if (isLoading || !draft) {
    return (
      <PageShell title="사업자·세금 설정" variant="detail">
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
      </PageShell>
    );
  }

  const unverified = draft.filter((r) => !isVerified(r)).length;

  return (
    <PageShell
      title="사업자·세금 설정"
      description="세율과 공제 방식이 마진·순이익 계산에 그대로 반영됩니다"
      actions={
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={addRule}>
            <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            규칙 추가
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
            공제 방식이 확인되지 않은 규칙이 {unverified}건 있습니다. 확인 전까지는
            <strong> 공제 없음</strong>으로 계산합니다 — 실제보다 마진이 낮게 보일 수 있습니다.
            세무사 확인 후 지정하세요.
          </span>
        </div>
      )}

      {draft.length === 0 && (
        <p className="text-sm text-[--color-muted-foreground]">
          등록된 세금 규칙이 없습니다. 규칙이 없으면 부가세가 계산에 반영되지 않습니다.
        </p>
      )}

      {draft.map((r, i) => (
        <Section
          key={r.id ?? `new-${i}`}
          title={`${TAX_TYPE_LABEL[r.taxType]} ${r.id ? `v${r.version}` : "(신규)"}`}
          description={
            isVerified(r)
              ? r.approvedAt
                ? `확인됨 · ${formatDate(r.approvedAt)}`
                : "공제 방식 지정됨"
              : "공제 방식 미확인"
          }
        >
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Field label="과세 유형" htmlFor={`tt-${i}`}>
              <select
                id={`tt-${i}`} className={SELECT_CLASS} value={r.taxType}
                onChange={(e) => patch(i, (x) => { x.taxType = e.target.value as TaxType; })}
              >
                {TAX_TYPES.map((t) => (
                  <option key={t} value={t}>{TAX_TYPE_LABEL[t]}</option>
                ))}
              </select>
            </Field>

            <Field label="사업자 형태" htmlFor={`bt-${i}`}>
              <select
                id={`bt-${i}`} className={SELECT_CLASS} value={r.businessType}
                onChange={(e) =>
                  patch(i, (x) => { x.businessType = e.target.value as BusinessType | ""; })
                }
              >
                <option value="">지정 안 함</option>
                {BUSINESS_TYPES.map((t) => (
                  <option key={t} value={t}>{BUSINESS_TYPE_LABEL[t]}</option>
                ))}
              </select>
            </Field>

            <Field label="부가세율" htmlFor={`vr-${i}`}>
              <PercentInput
                value={r.vatRate}
                onChange={(v) => patch(i, (x) => { x.vatRate = v; })}
              />
            </Field>

            <Field label="매출세액 계산" htmlFor={`cm-${i}`}>
              <select
                id={`cm-${i}`} className={SELECT_CLASS} value={r.calcMethod}
                onChange={(e) =>
                  patch(i, (x) => { x.calcMethod = e.target.value as TaxCalcMethod; })
                }
              >
                {TAX_CALC_METHODS.map((m) => (
                  <option key={m} value={m}>{TAX_CALC_METHOD_LABEL[m]}</option>
                ))}
              </select>
            </Field>

            <Field label="판매가 표기 기본값" htmlFor={`vi-${i}`}>
              <select
                id={`vi-${i}`} className={SELECT_CLASS} value={r.vatInclusion}
                onChange={(e) =>
                  patch(i, (x) => { x.vatInclusion = e.target.value as VatInclusion; })
                }
              >
                {VAT_INCLUSIONS.map((v) => (
                  <option key={v} value={v}>{VAT_INCLUSION_LABEL[v]}</option>
                ))}
              </select>
            </Field>

            <Field label="적용 대상" htmlFor={`at-${i}`} hint="ALL 이면 전체에 적용됩니다">
              <TextInput
                id={`at-${i}`} value={r.appliesTo}
                onChange={(e) => patch(i, (x) => { x.appliesTo = e.target.value; })}
              />
            </Field>

            <Field label="매입세액 공제" htmlFor={`iv-${i}`} className="sm:col-span-2">
              <select
                id={`iv-${i}`} className={SELECT_CLASS} value={r.inputVat}
                onChange={(e) => patch(i, (x) => { x.inputVat = e.target.value; })}
              >
                {INPUT_VAT.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </Field>

            <Field label="수입 부가세" htmlFor={`mv-${i}`}>
              <select
                id={`mv-${i}`} className={SELECT_CLASS} value={r.importVat}
                onChange={(e) => patch(i, (x) => { x.importVat = e.target.value; })}
              >
                {IMPORT_VAT.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </Field>

            <Field label="메모" htmlFor={`mm-${i}`} className="sm:col-span-2 lg:col-span-3">
              <TextInput
                id={`mm-${i}`} value={r.memo} placeholder="근거, 세무사 확인 내용 등"
                onChange={(e) => patch(i, (x) => { x.memo = e.target.value; })}
              />
            </Field>
          </div>

          {isVerified(r) && (
            <p className="mt-2 flex items-center gap-1 text-[11px] text-[--color-success]">
              <ShieldCheck className="h-3 w-3" aria-hidden />
              계산에 필요한 값이 모두 지정되어 있습니다.
            </p>
          )}
        </Section>
      ))}

      <p className="mt-3 text-[11px] text-[--color-muted-foreground]">
        세율·공제 방식 변경은 <strong className="text-[--color-foreground]">승인이 필요합니다.</strong>
        승인되면 새 버전으로 적용되고, 기존 버전은 그대로 남아 이미 확정한 달의
        계산이 바뀌지 않습니다. 이 도구는 세무 신고를 대체하지 않습니다.
      </p>
    </PageShell>
  );
}
