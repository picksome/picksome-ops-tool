"use client";

/**
 * Golden 검증 한 화면 — 사용자가 엑셀·DB·계산기를 열지 않게 한다.
 *
 * 이 화면이 지키는 것
 * ----------------
 * · **모르는 값을 0 으로 그리지 않는다.** `value === null` 은 "확인 불가" 다
 * · **차이가 0이라고 자동 승인하지 않는다.** 버튼은 사람이 누른다
 * · **추정을 실측처럼 보여주지 않는다.** 신뢰도를 항목마다 붙인다
 * · **보류는 실패가 아니다.** 빨간색으로 칠하지 않는다
 *
 * 승인의 뜻
 * --------
 * "여기 보이는 원천과 독립 검산을 확인했고, 이 값을 앞으로의 기준으로
 * 삼는다." 판매·정산·마진 데이터를 고치는 것이 아니다.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CheckCircle2, CircleAlert, CircleHelp, TriangleAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api";
import {
  COMPONENT_LABEL, CONFIDENCE_LABEL, DEFER_REASON_LABEL, SOURCE_LABEL,
  verificationApi,
  type Confidence, type EvidenceFact, type GoldenEvidence,
} from "@/lib/api/verification";
import { cn } from "@/lib/utils";

const CONFIDENCE_VARIANT: Record<Confidence, "success" | "info" | "warning"> = {
  ACTUAL: "success",
  ESTIMATED: "info",
  UNKNOWN: "warning",
};

/** 화면에 보여줄 성분 순서. 매출 → 비용 → 결과. */
const FACT_ORDER = [
  "product_revenue",
  "delivery_revenue",
  "seller_coupon",
  "marketplace_fee",
  "cost",
  "shipping_cost",
  "ad_cost",
] as const;

const FACT_LABEL: Record<string, string> = {
  ...COMPONENT_LABEL,
  product_revenue: "상품매출",
  delivery_revenue: "배송비매출",
  seller_coupon: "판매자 쿠폰",
};

function money(n: number): string {
  return `${n.toLocaleString("ko-KR")}원`;
}

/** 모르는 값은 금액처럼 그리지 않는다. */
function FactValue({ fact }: { fact: EvidenceFact }) {
  if (fact.value === null) {
    return <span className="text-[--color-muted-foreground]">확인 불가</span>;
  }
  return <span className="tabular-nums">{money(fact.value)}</span>;
}

export function GoldenReview({
  salesOrderId,
  caseId,
  caseType,
  onClose,
  canDecide,
}: {
  salesOrderId: string;
  /** 등록된 사례일 때만 승인/보류할 수 있다. 후보 단계면 없다. */
  caseId: string | null;
  caseType: string;
  onClose: () => void;
  canDecide: boolean;
}) {
  const qc = useQueryClient();
  const [deferReason, setDeferReason] = React.useState("SOURCE_MISSING");
  const [showApprove, setShowApprove] = React.useState(false);

  const { data, isLoading, error } = useQuery({
    queryKey: ["golden-evidence", salesOrderId],
    queryFn: () => verificationApi.goldenEvidence(salesOrderId),
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["verification-golden"] });
    qc.invalidateQueries({ queryKey: ["verification-golden-candidates"] });
    qc.invalidateQueries({ queryKey: ["verification-golden-recommended"] });
  };

  const approve = useMutation({
    mutationFn: (expected: Record<string, number | null>) =>
      verificationApi.approveGolden(caseId!, {
        expected,
        source_evidence: data?.settlement?.where ?? "판매 장부 · 정책",
      }),
    onSuccess: () => {
      invalidate();
      toast.success("기준값으로 승인했습니다. 앞으로 이 값이 회귀 기준입니다.");
      onClose();
    },
    onError: (e) =>
      toast.error(e instanceof ApiError ? e.message : "승인에 실패했습니다."),
  });

  const defer = useMutation({
    mutationFn: () => verificationApi.deferGolden(caseId!, { reason: deferReason }),
    onSuccess: () => {
      invalidate();
      toast.success("보류했습니다. 계산 오류로 세지 않습니다.");
      onClose();
    },
    onError: (e) =>
      toast.error(e instanceof ApiError ? e.message : "보류에 실패했습니다."),
  });

  return (
    <div
      role="dialog"
      aria-label="Golden 검증"
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/40 p-2 sm:items-center"
      onClick={onClose}
    >
      <div
        className="max-h-[88vh] w-full max-w-2xl overflow-auto rounded-lg bg-[--color-background] p-4 shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        {isLoading || !data ? (
          error ? (
            <p className="text-sm">원천을 불러오지 못했습니다.</p>
          ) : (
            <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
          )
        ) : (
          <>
            <Header data={data} caseType={caseType} onClose={onClose} />
            <Facts data={data} />
            <Independent data={data} />
            <Comparison data={data} />
            <Checklist data={data} />

            {canDecide && caseId && (
              <Decide
                data={data}
                showApprove={showApprove}
                setShowApprove={setShowApprove}
                deferReason={deferReason}
                setDeferReason={setDeferReason}
                approving={approve.isPending}
                deferring={defer.isPending}
                onApprove={(expected) => approve.mutate(expected)}
                onDefer={() => defer.mutate()}
              />
            )}
            {!caseId && (
              <p className="mt-3 rounded-md bg-[--color-muted] p-2 text-[11px] text-[--color-muted-foreground]">
                이 주문은 아직 기준 사례로 등록되지 않았습니다. 목록에서 등록한 뒤
                승인할 수 있습니다.
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function Header({
  data, caseType, onClose,
}: { data: GoldenEvidence; caseType: string; onClose: () => void }) {
  return (
    <>
      <div className="mb-3 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold">
            {data.order.business_key}{" "}
            <span className="font-normal text-[--color-muted-foreground]">
              · {data.order.marketplace} · {data.order.sale_date}
            </span>
          </h2>
          <p className="text-[11px] text-[--color-muted-foreground]">
            {data.order.product_name ?? "상품명 없음"} · 수량 {data.order.quantity}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <Badge variant={CONFIDENCE_VARIANT[data.golden_confidence]}>
            {CONFIDENCE_LABEL[data.golden_confidence]} 기준
          </Badge>
          <Button size="sm" variant="outline" onClick={onClose}>닫기</Button>
        </div>
      </div>

      {data.settlement && (
        <p className="mb-2 rounded-md bg-[--color-muted] p-2 text-[11px]">
          원천: {data.settlement.where}
          <span className="text-[--color-muted-foreground]">
            {" "}· 파일을 열지 않아도 아래 값이 그 행의 숫자입니다
          </span>
        </p>
      )}

      {data.warnings.map((w) => (
        <p
          key={w}
          className="mb-2 flex items-start gap-1.5 rounded-md bg-[--color-warning-bg] p-2 text-[11px] text-[--color-warning]"
        >
          <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
          <span>{w}</span>
        </p>
      ))}

      {data.evidence_state === "SOURCE_EVIDENCE_INCOMPLETE" && (
        <p className="mb-2 rounded-md bg-[--color-warning-bg] p-2 text-[11px] text-[--color-warning]">
          원천이 부족해 독립 검산을 할 수 없습니다: {data.blockers.join(" · ")}
        </p>
      )}

      <p className="sr-only">사례 유형 {caseType}</p>
    </>
  );
}

function Facts({ data }: { data: GoldenEvidence }) {
  return (
    <section className="mb-3">
      <h3 className="mb-1 text-[12px] font-medium">원천값</h3>
      <table className="w-full border-collapse text-[12px]">
        <caption className="sr-only">성분별 원천값과 출처</caption>
        <thead>
          <tr className="border-b border-[--color-border] text-left text-[11px] text-[--color-muted-foreground]">
            <th scope="col" className="py-1">항목</th>
            <th scope="col" className="py-1 text-right">금액</th>
            <th scope="col" className="py-1">출처</th>
            <th scope="col" className="py-1">신뢰도</th>
          </tr>
        </thead>
        <tbody>
          {FACT_ORDER.map((key) => {
            const fact = data.facts[key];
            if (!fact) return null;
            return (
              <tr key={key} className="border-b border-[--color-border] last:border-0">
                <td className="py-1">{FACT_LABEL[key] ?? key}</td>
                <td className="py-1 text-right"><FactValue fact={fact} /></td>
                <td className="py-1 text-[11px] text-[--color-muted-foreground]">
                  {SOURCE_LABEL[fact.source] ?? fact.source}
                  {fact.source_ref && (
                    <span className="block opacity-80">{fact.source_ref}</span>
                  )}
                </td>
                <td className="py-1">
                  <Badge variant={CONFIDENCE_VARIANT[fact.confidence]}>
                    {CONFIDENCE_LABEL[fact.confidence]}
                  </Badge>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

function Independent({ data }: { data: GoldenEvidence }) {
  const ind = data.independent;
  return (
    <section className="mb-3 rounded-md border border-[--color-border] p-2.5">
      <h3 className="mb-1 text-[12px] font-medium">
        독립 검산{" "}
        <span className="font-normal text-[--color-muted-foreground]">
          — Seller OS 계산기를 쓰지 않고 위 값으로 다시 셈
        </span>
      </h3>
      <pre className="mb-1.5 whitespace-pre-wrap font-mono text-[10px] text-[--color-muted-foreground]">
        {data.formula}
      </pre>
      {!ind.calculable ? (
        <p className="text-[11px] text-[--color-warning]">{ind.reason}</p>
      ) : (
        <>
          <dl className="grid grid-cols-2 gap-1 text-[12px]">
            <dt className="text-[--color-muted-foreground]">순매출</dt>
            <dd className="text-right tabular-nums">{money(ind.net_revenue!)}</dd>
            <dt className="text-[--color-muted-foreground]">마진</dt>
            <dd className="text-right font-medium tabular-nums">{money(ind.margin!)}</dd>
          </dl>
          {ind.vendor_check && (
            <p
              className={cn(
                "mt-1.5 rounded-md p-1.5 text-[11px]",
                ind.vendor_check.difference === 0
                  ? "bg-[--color-success-bg] text-[--color-success]"
                  : "bg-[--color-danger-bg] text-[--color-danger]",
              )}
            >
              정산서 자체 검산 · 재구성 {money(ind.vendor_check.reconstructed)} vs
              벤더 정산금액 {money(ind.vendor_check.vendor_net_settlement)} · 차이{" "}
              {money(ind.vendor_check.difference)}
            </p>
          )}
          {ind.assumptions.length > 0 && (
            <ul className="mt-1.5 space-y-0.5">
              {ind.assumptions.map((a) => (
                <li key={a} className="text-[10px] text-[--color-muted-foreground]">
                  · {a}
                </li>
              ))}
            </ul>
          )}
          <p className="mt-1.5 text-[10px] text-[--color-muted-foreground]">
            반품비·풀필먼트비·결제수수료·부가세는 <strong>원천이 없어</strong> 계산에
            넣지 않았습니다 — 0원이라는 뜻이 아닙니다.
          </p>
        </>
      )}
    </section>
  );
}

function Comparison({ data }: { data: GoldenEvidence }) {
  const comp = data.comparison;
  if (!comp.comparable) {
    return (
      <p className="mb-3 rounded-md bg-[--color-warning-bg] p-2 text-[11px] text-[--color-warning]">
        {comp.reason}
      </p>
    );
  }
  return (
    <section className="mb-3">
      <h3 className="mb-1 text-[12px] font-medium">독립 검산 vs Seller OS</h3>
      <table className="w-full border-collapse text-[12px]">
        <caption className="sr-only">항목별 차이</caption>
        <thead>
          <tr className="border-b border-[--color-border] text-left text-[11px] text-[--color-muted-foreground]">
            <th scope="col" className="py-1">항목</th>
            <th scope="col" className="py-1 text-right">독립 검산</th>
            <th scope="col" className="py-1 text-right">Seller OS</th>
            <th scope="col" className="py-1 text-right">차이</th>
          </tr>
        </thead>
        <tbody>
          {comp.items.map((i) => (
            <tr key={i.field} className="border-b border-[--color-border] last:border-0">
              <td className="py-1">{i.label}</td>
              <td className="py-1 text-right tabular-nums">{money(i.independent)}</td>
              <td className="py-1 text-right tabular-nums">{money(i.seller_os)}</td>
              <td
                className={cn(
                  "py-1 text-right tabular-nums",
                  i.match ? "text-[--color-success]" : "text-[--color-danger] font-medium",
                )}
              >
                {i.match ? "0" : `${i.difference > 0 ? "+" : ""}${i.difference.toLocaleString("ko-KR")}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p
        className={cn(
          "mt-1.5 flex items-center gap-1.5 rounded-md p-2 text-[11px]",
          comp.all_match
            ? "bg-[--color-success-bg] text-[--color-success]"
            : "bg-[--color-danger-bg] text-[--color-danger]",
        )}
      >
        {comp.all_match ? (
          <CheckCircle2 className="h-3.5 w-3.5 shrink-0" aria-hidden />
        ) : (
          <CircleAlert className="h-3.5 w-3.5 shrink-0" aria-hidden />
        )}
        {comp.all_match
          ? "모든 자동 대사 항목이 일치합니다 — 그래도 승인은 사람이 합니다."
          : `${comp.mismatched}개 항목이 다릅니다. 원인을 확인한 뒤 판단하세요.`}
      </p>
    </section>
  );
}

function Checklist({ data }: { data: GoldenEvidence }) {
  return (
    <section className="mb-3">
      <h3 className="mb-1 text-[12px] font-medium">확인할 것</h3>
      <ul className="space-y-1">
        {data.checklist.map((item) => (
          <li
            key={item.label}
            className="flex items-start gap-1.5 rounded-md border border-[--color-border] p-2 text-[11px]"
          >
            {item.auto_matched === true ? (
              <CheckCircle2 className="mt-0.5 h-3 w-3 shrink-0 text-[--color-success]" aria-hidden />
            ) : item.auto_matched === false ? (
              <CircleAlert className="mt-0.5 h-3 w-3 shrink-0 text-[--color-danger]" aria-hidden />
            ) : (
              <CircleHelp className="mt-0.5 h-3 w-3 shrink-0 text-[--color-muted-foreground]" aria-hidden />
            )}
            <span>
              <strong className="font-medium">{item.label}</strong>
              <span className="block text-[--color-muted-foreground]">{item.detail}</span>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Decide({
  data, showApprove, setShowApprove, deferReason, setDeferReason,
  approving, deferring, onApprove, onDefer,
}: {
  data: GoldenEvidence;
  showApprove: boolean;
  setShowApprove: (v: boolean) => void;
  deferReason: string;
  setDeferReason: (v: string) => void;
  approving: boolean;
  deferring: boolean;
  onApprove: (expected: Record<string, number | null>) => void;
  onDefer: () => void;
}) {
  const ind = data.independent;
  const canApprove = ind.calculable;

  // 승인할 기준값 — **독립 검산 결과**다. Seller OS 계산 결과를 베끼지 않는다.
  const expected: Record<string, number | null> = {
    net_revenue: ind.net_revenue,
    marketplace_fee: data.facts.marketplace_fee?.value ?? null,
    cost: data.facts.cost?.value ?? null,
    margin_amount: ind.margin,
  };

  if (!showApprove) {
    return (
      <div className="flex flex-wrap items-center gap-2 border-t border-[--color-border] pt-3">
        <Button
          size="sm"
          disabled={!canApprove}
          onClick={() => setShowApprove(true)}
        >
          Golden 기준으로 승인
        </Button>
        <select
          aria-label="보류 사유"
          className="rounded-md border border-[--color-border] bg-[--color-background] px-2 py-1 text-[12px]"
          value={deferReason}
          onChange={(e) => setDeferReason(e.target.value)}
        >
          {Object.entries(DEFER_REASON_LABEL).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
        <Button size="sm" variant="outline" disabled={deferring} onClick={onDefer}>
          보류
        </Button>
        <p className="w-full text-[10px] text-[--color-muted-foreground]">
          보류는 계산 오류가 아닙니다. 나중에 다시 볼 것으로만 표시됩니다.
        </p>
      </div>
    );
  }

  return (
    <div className="border-t border-[--color-border] pt-3">
      <p className="mb-1.5 text-[12px] font-medium">
        아래 값을 이 주문의 기준으로 고정합니다
      </p>
      <dl className="mb-2 grid grid-cols-2 gap-1 rounded-md bg-[--color-muted] p-2 text-[12px]">
        {Object.entries(expected).map(([field, value]) => (
          <React.Fragment key={field}>
            <dt className="text-[--color-muted-foreground]">
              {FACT_LABEL[field] ?? COMPONENT_LABEL[field] ?? field}
            </dt>
            <dd className="text-right tabular-nums">
              {value === null ? "확인 불가 (기준에서 제외)" : money(value)}
            </dd>
          </React.Fragment>
        ))}
      </dl>
      <p className="mb-2 text-[10px] text-[--color-muted-foreground]">
        이 값은 <strong>독립 검산 결과</strong>입니다. Seller OS 계산 결과를 그대로
        복사한 것이 아닙니다 — 앞으로 계산이 바뀌면 이 값과 비교해 잡아냅니다.
        승인해도 판매·정산·마진 데이터는 바뀌지 않습니다.
      </p>
      <div className="flex gap-2">
        <Button size="sm" disabled={approving} onClick={() => onApprove(expected)}>
          승인
        </Button>
        <Button size="sm" variant="outline" onClick={() => setShowApprove(false)}>
          취소
        </Button>
      </div>
    </div>
  );
}
