"use client";

/**
 * 상품 소싱 패널 — Product Drawer 의 "소싱" 탭 (M037 live).
 *
 * 이 패널이 왜 다시 쓰였나
 * --------------------
 * 예전 소싱 탭은 공급처를 **문자열 한 칸**으로 받고, 관세·수입VAT·포장비 같은
 * 항목을 입력받아 **프론트에서 총원가를 계산**해 보여줬다. 그런데 그 값들은
 * 저장되지 않았다 — 백엔드로 나가는 것은 `cost_amount`/`cost_currency`/
 * `cost_fx_rate`/`supply_url` 넷뿐이다. 사용자는 관세를 입력하고 총원가를 본 뒤
 * 저장했지만 새로 열면 사라져 있었다.
 *
 * 지금은 실제 모델을 쓴다:
 *
 * ```
 * supplier_site                  공급처 (코드로 식별, 이름 바꿔도 안 깨진다)
 * product_source                 그 공급처의 이 상품 (URL 단위)
 * product_source_cost_version    시점별 매입가 (덮어쓰지 않는다)
 * fx_rate_snapshot               시점별 환율
 * ```
 *
 * 계산은 서버가 한다
 * ---------------
 * landed cost·권장가·민감도를 여기서 다시 계산하지 않는다. 두 곳의 산식이
 * 갈라지는 순간 어느 쪽이 맞는지 사용자가 알 수 없다.
 *
 * 원가는 자동으로 반영되지 않는다
 * ---------------------------
 * 소싱 원가는 **관측한 사실**이고 `product.cost_amount` 는 **채택한 값**이다.
 * 채택하려면 `COST_CHANGE` 승인을 거친다 — 여기서 바로 바꾸면 승인이 장식이 된다.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle, ExternalLink, Plus, Star, TrendingUp, TriangleAlert,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { ApiError } from "@/lib/api";
import {
  FX_RATE_TYPE_LABEL, missingLabel, requestCostAdoption, sourcingApi,
  type CostComparison, type ProductSource, type Recommendation,
  type Sensitivity, type SourceComparison, type SourceComparisonRow,
} from "@/lib/api/sourcing";
import { formatDate, formatKRW } from "@/lib/format";

const PRICE_LABEL: Record<string, string> = {
  break_even: "손익분기",
  minimum_recommended: "최소 권장가",
  recommended: "권장가",
};

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function errorMessage(err: unknown, action: string): string {
  if (!(err instanceof ApiError)) return `${action} 중 통신에 실패했습니다.`;
  if (err.status === 403) return `${action} 권한이 없습니다.`;
  if (err.status === 409) return err.message || "이미 있는 항목입니다.";
  return err.message || `${action}에 실패했습니다.`;
}

/** 계산 불가 사유. **0 으로 그리지 않는다** — 0원은 "원가가 0" 으로 읽힌다. */
function Unavailable({ missing }: { missing: string[] }) {
  return (
    <div className="flex items-start gap-1.5 rounded-md border border-dashed border-[--color-border] px-3 py-2 text-[11px] text-[--color-muted-foreground]">
      <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
      <span>
        계산할 수 없습니다 —{" "}
        {missing.map((m) => missingLabel(m)).join(", ")}
      </span>
    </div>
  );
}

export function ProductSourcingPanel({
  productId,
  marketplace,
  salePrice,
}: {
  productId: string;
  /** 권장가·민감도는 판매처마다 수수료가 달라 판매처를 알아야 계산된다. */
  marketplace: string;
  /** 민감도는 "이 판매가에서" 마진이 얼마나 줄어드는지를 본다. */
  salePrice: number;
}) {
  const qc = useQueryClient();
  const [addOpen, setAddOpen] = React.useState(false);
  const [costOpen, setCostOpen] = React.useState(false);
  const [fxOpen, setFxOpen] = React.useState(false);

  const { data: sources, isLoading } = useQuery({
    queryKey: ["product-sources", productId],
    queryFn: () => sourcingApi.listSources(productId),
  });

  const list = sources?.items ?? [];
  const primary = list.find((s) => s.is_primary) ?? null;

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["product-sources", productId] });
    qc.invalidateQueries({ queryKey: ["landed-cost"] });
    qc.invalidateQueries({ queryKey: ["recommendation"] });
    qc.invalidateQueries({ queryKey: ["sensitivity"] });
    qc.invalidateQueries({ queryKey: ["cost-versions"] });
  };

  if (isLoading) {
    return <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-[11px] text-[--color-muted-foreground]">
          소싱 원가는 <strong className="text-[--color-foreground]">관측한 사실</strong>입니다.
          상품 계산에 반영하려면 아래에서 승인 요청을 만드세요.
        </p>
        <Button variant="outline" size="sm" onClick={() => setAddOpen(true)}>
          <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          소싱처 추가
        </Button>
      </div>

      {list.length === 0 ? (
        <div className="rounded-lg border border-dashed border-[--color-border] px-4 py-8 text-center text-sm text-[--color-muted-foreground]">
          <p>등록된 소싱처가 없습니다.</p>
          <p className="mt-1 text-xs">
            소싱처를 등록하면 예상 원가와 권장 판매가를 계산할 수 있습니다.
          </p>
        </div>
      ) : (
        <>
          <SourceList
            sources={list}
            onPrimaryChanged={invalidate}
          />

          {list.length > 1 && (
            <SourceComparisonSection
              productId={productId} onPrimaryChanged={invalidate}
            />
          )}

          {primary && (
            <>
              <CostSection
                source={primary}
                onAddCost={() => setCostOpen(true)}
                onAddFx={() => setFxOpen(true)}
              />
              <LandedCostSection
                sourceId={primary.id}
                productId={productId}
                supplierName={primary.supplier_name}
                onAddFx={() => setFxOpen(true)}
              />
              <RecommendationSection sourceId={primary.id} marketplace={marketplace} />
              <SensitivitySection
                sourceId={primary.id}
                marketplace={marketplace}
                salePrice={salePrice}
              />
              <CostHistorySection sourceId={primary.id} />
            </>
          )}
        </>
      )}

      <AddSourceDialog
        open={addOpen} onOpenChange={setAddOpen} productId={productId}
        onAdded={invalidate}
      />
      {primary && (
        <>
          <AddCostDialog
            open={costOpen} onOpenChange={setCostOpen} source={primary}
            onAdded={invalidate}
          />
          <AddFxDialog
            open={fxOpen} onOpenChange={setFxOpen}
            currency={primary.currency} onAdded={invalidate}
          />
        </>
      )}
    </div>
  );
}

/* ────────────────────────────────────────────────────────────
 * 소싱처 목록
 * ──────────────────────────────────────────────────────────── */

function SourceList({
  sources, onPrimaryChanged,
}: {
  sources: ProductSource[];
  onPrimaryChanged: () => void;
}) {
  const setPrimary = useMutation({
    mutationFn: (id: string) => sourcingApi.setPrimary(id),
    onSuccess: () => {
      // 주 소싱처가 바뀌면 예상 원가·권장가가 전부 달라진다 — 함께 다시 읽는다.
      onPrimaryChanged();
      toast.success("주 소싱처를 변경했습니다.");
    },
    onError: (e) => toast.error(errorMessage(e, "주 소싱처 변경")),
  });

  return (
    <section>
      <h4 className="mb-1.5 text-[12px] font-medium">소싱처 {sources.length}곳</h4>
      <div className="ops-scroll-x">
        <table className="w-full text-[12px]">
          <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
            <tr>
              <th scope="col" className="px-2 py-1.5 text-left font-semibold">공급처</th>
              <th scope="col" className="px-2 py-1.5 text-left font-semibold">통화</th>
              <th scope="col" className="px-2 py-1.5 text-right font-semibold">MOQ</th>
              <th scope="col" className="px-2 py-1.5 text-right font-semibold">리드타임</th>
              <th scope="col" className="px-2 py-1.5 text-left font-semibold">원본</th>
              <th scope="col" className="px-2 py-1.5 text-center font-semibold">주 소싱처</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[--color-border]">
            {sources.map((s) => (
              <tr key={s.id}>
                <td className="px-2 py-1.5">
                  <span className="font-medium">{s.supplier_name}</span>
                  <span className="ml-1 font-mono text-[10px] text-[--color-muted-foreground]">
                    {s.supplier_code}
                  </span>
                </td>
                <td className="px-2 py-1.5 tabular-nums">{s.currency}</td>
                <td className="px-2 py-1.5 text-right tabular-nums">{s.moq ?? "—"}</td>
                <td className="px-2 py-1.5 text-right tabular-nums">
                  {s.lead_time_days == null ? "—" : `${s.lead_time_days}일`}
                </td>
                <td className="px-2 py-1.5">
                  {/* http/https 만 저장된다(백엔드가 막는다). 그래도 새 창은 격리한다. */}
                  {s.source_url ? (
                    <a
                      href={s.source_url}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="ops-focusable inline-flex items-center gap-1 underline underline-offset-2"
                    >
                      열기
                      <ExternalLink className="h-3 w-3" aria-hidden />
                    </a>
                  ) : (
                    <span className="text-[--color-muted-foreground]">—</span>
                  )}
                </td>
                <td className="px-2 py-1.5 text-center">
                  {s.is_primary ? (
                    <Badge variant="info" className="px-1.5 py-0 text-[10px]">
                      <Star className="mr-0.5 inline h-2.5 w-2.5" aria-hidden />
                      주 소싱처
                    </Badge>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setPrimary.mutate(s.id)}
                      disabled={setPrimary.isPending}
                      className="ops-focusable text-[11px] underline underline-offset-2"
                    >
                      주 소싱처로
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/* ────────────────────────────────────────────────────────────
 * 대안 소싱처 비교
 * ──────────────────────────────────────────────────────────── */

type SortKey = "landed" | "moq" | "lead_time" | "checked";

const SORT_LABEL: Record<SortKey, string> = {
  landed: "예상 원가",
  moq: "MOQ",
  lead_time: "리드타임",
  checked: "최근 확인",
};

/** 값이 없는 줄은 **항상 아래로**. 위로 올라오면 "가장 유리한 곳" 처럼 보인다. */
export function sortRows(rows: SourceComparisonRow[], key: SortKey): SourceComparisonRow[] {
  const value = (r: SourceComparisonRow): number | null => {
    switch (key) {
      case "landed": return r.landed_cost.is_complete ? r.landed_cost.total_krw : null;
      case "moq": return r.moq;
      case "lead_time": return r.lead_time_days;
      case "checked":
        return r.last_checked_at ? -new Date(r.last_checked_at).getTime() : null;
    }
  };
  return [...rows].sort((a, b) => {
    const av = value(a);
    const bv = value(b);
    if (av === null && bv === null) return 0;
    if (av === null) return 1;
    if (bv === null) return -1;
    return av - bv;
  });
}

/**
 * 소싱처를 나란히 놓는다.
 *
 * 추천을 하지 않는다
 * ---------------
 * 최저 예상 원가에 별을 붙이지 않는다. 실제 선택에는 여기 없는 것들이 들어간다 —
 * MOQ 를 감당할 현금, 시즌과 리드타임, 클레임 이력, 카드 결제 가능 여부.
 * 하나로 줄을 세우는 순간 그것들이 사라진다.
 *
 * 정렬은 사용자가 고른다. 정렬은 "무엇을 먼저 보고 싶은가" 지 "무엇이 정답인가"
 * 가 아니다.
 *
 * 계산 못 하는 줄은 비워 둔다
 * ------------------------
 * 환율이나 원가 version 이 없으면 "계산 불가" 라고 쓴다. 0 으로 채우면 정확히
 * 반대로 — 가장 싼 곳으로 — 보인다.
 */
function SourceComparisonSection({
  productId, onPrimaryChanged,
}: {
  productId: string;
  onPrimaryChanged: () => void;
}) {
  const [sortKey, setSortKey] = React.useState<SortKey>("landed");

  const { data, isLoading } = useQuery<SourceComparison>({
    queryKey: ["source-comparison", productId],
    queryFn: () => sourcingApi.sourceComparison(productId),
  });

  const setPrimary = useMutation({
    mutationFn: (id: string) => sourcingApi.setPrimary(id),
    onSuccess: () => {
      onPrimaryChanged();
      toast.success("주 소싱처를 변경했습니다.");
    },
    onError: (e) => toast.error(errorMessage(e, "주 소싱처 변경")),
  });

  if (isLoading || !data) return null;
  const rows = sortRows(data.sources, sortKey);
  const applied = data.current_cost_amount;

  return (
    <section>
      <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-[12px] font-medium">
          소싱처 비교
          <span className="ml-1.5 font-normal text-[11px] text-[--color-muted-foreground]">
            {data.comparable_count}/{data.sources.length}곳 계산됨
          </span>
        </h4>
        <div className="flex items-center gap-1.5">
          <label htmlFor="src-sort" className="text-[11px] text-[--color-muted-foreground]">
            정렬
          </label>
          <select
            id="src-sort" value={sortKey}
            onChange={(e) => setSortKey(e.target.value as SortKey)}
            className="ops-focusable h-7 rounded-md border border-[--color-border] bg-[--color-card] px-1.5 text-[11px]"
          >
            {(Object.keys(SORT_LABEL) as SortKey[]).map((k) => (
              <option key={k} value={k}>{SORT_LABEL[k]}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="ops-scroll-x">
        <table className="w-full text-[12px]">
          <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
            <tr>
              <th scope="col" className="px-2 py-1.5 text-left font-semibold">공급처</th>
              <th scope="col" className="px-2 py-1.5 text-right font-semibold">예상 원가</th>
              <th scope="col" className="px-2 py-1.5 text-right font-semibold">적용 원가 대비</th>
              <th scope="col" className="px-2 py-1.5 text-right font-semibold">MOQ</th>
              <th scope="col" className="px-2 py-1.5 text-right font-semibold">리드타임</th>
              <th scope="col" className="px-2 py-1.5 text-left font-semibold">최근 확인</th>
              <th scope="col" className="px-2 py-1.5 text-center font-semibold">주 소싱처</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[--color-border]">
            {rows.map((r) => (
              <tr key={r.product_source_id}>
                <td className="px-2 py-1.5">
                  <span className="font-medium">{r.supplier_name}</span>
                  <span className="ml-1 text-[10px] text-[--color-muted-foreground]">
                    {r.currency}
                  </span>
                </td>

                {r.landed_cost.is_complete ? (
                  <td className="px-2 py-1.5 text-right tabular-nums">
                    {formatKRW(r.landed_cost.total_krw ?? 0)}
                  </td>
                ) : (
                  // 0 으로 그리면 가장 싼 곳처럼 보인다.
                  <td className="px-2 py-1.5 text-right text-[11px] text-[--color-muted-foreground]">
                    계산 불가
                    <span className="block text-[10px]">
                      {r.landed_cost.missing.map((m) => missingLabel(m)).join(", ")}
                    </span>
                  </td>
                )}

                <td className="px-2 py-1.5 text-right tabular-nums">
                  {r.difference === null ? (
                    <span className="text-[11px] text-[--color-muted-foreground]">—</span>
                  ) : (
                    <span
                      className={
                        r.difference > 0 ? "text-[--color-danger]" : "text-[--color-success]"
                      }
                    >
                      {r.difference > 0 ? "+" : ""}{formatKRW(r.difference)}
                      {r.difference_rate !== null && (
                        <span className="ml-1 text-[10px]">
                          ({(r.difference_rate * 100).toFixed(1)}%)
                        </span>
                      )}
                    </span>
                  )}
                </td>

                <td className="px-2 py-1.5 text-right tabular-nums">{r.moq ?? "—"}</td>
                <td className="px-2 py-1.5 text-right tabular-nums">
                  {r.lead_time_days == null ? "—" : `${r.lead_time_days}일`}
                </td>
                <td className="px-2 py-1.5 text-[11px]">
                  {r.last_checked_at ? formatDate(r.last_checked_at) : (
                    <span className="text-[--color-muted-foreground]">확인 기록 없음</span>
                  )}
                </td>
                <td className="px-2 py-1.5 text-center">
                  {r.is_primary ? (
                    <Badge variant="info" className="px-1.5 py-0 text-[10px]">
                      <Star className="mr-0.5 inline h-2.5 w-2.5" aria-hidden />
                      주 소싱처
                    </Badge>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setPrimary.mutate(r.product_source_id)}
                      disabled={setPrimary.isPending}
                      className="ops-focusable text-[11px] underline underline-offset-2"
                    >
                      주 소싱처로
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="mt-1.5 text-[11px] text-[--color-muted-foreground]">
        {applied === null
          ? "이 상품에 적용된 원가가 없어 차이를 계산하지 않았습니다."
          : `적용 원가 ${formatKRW(applied)} 기준입니다.`}
        {" "}가장 싼 곳이 곧 정답은 아닙니다 — MOQ·리드타임·품질을 함께 보세요.
        소싱처를 바꿔도 상품 원가는 <strong className="text-[--color-foreground]">
        승인</strong>을 거쳐야 바뀝니다.
      </p>
    </section>
  );
}

/* ────────────────────────────────────────────────────────────
 * 현재 원가 · FX
 * ──────────────────────────────────────────────────────────── */

function CostSection({
  source, onAddCost, onAddFx,
}: {
  source: ProductSource;
  onAddCost: () => void;
  onAddFx: () => void;
}) {
  const { data } = useQuery({
    queryKey: ["cost-versions", source.id],
    queryFn: () => sourcingApi.costVersions(source.id),
  });

  const current = data?.items?.find((v) => v.valid_to === null) ?? null;

  return (
    <section className="rounded-lg border border-[--color-border] p-3">
      <div className="mb-2 flex items-center justify-between">
        <h4 className="text-[12px] font-medium">현재 매입가</h4>
        <div className="flex gap-1.5">
          {source.currency !== "KRW" && (
            <Button variant="outline" size="sm" onClick={onAddFx}>
              환율 등록
            </Button>
          )}
          <Button variant="outline" size="sm" onClick={onAddCost}>
            새 원가 등록
          </Button>
        </div>
      </div>

      {!current ? (
        <p className="text-[11px] text-[--color-muted-foreground]">
          매입가가 등록되지 않았습니다. 예상 원가를 계산할 수 없습니다.
        </p>
      ) : current.redacted ? (
        <p className="text-[11px] text-[--color-muted-foreground]">
          매입가를 볼 권한이 없습니다.
        </p>
      ) : (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] sm:grid-cols-3">
          <Money label="매입가" value={current.purchase_price} unit={current.currency} />
          <Money label="현지 배송비" value={current.domestic_shipping} unit={current.currency} />
          <Money label="국제 배송비" value={current.international_shipping} unit="KRW" />
          <Money label="관세" value={current.customs_duty} unit="KRW" />
          <Money label="수입 부가세" value={current.import_vat} unit="KRW" />
          <Money label="포장비" value={current.packaging_cost} unit="KRW" />
          <div className="col-span-full mt-1 text-[--color-muted-foreground]">
            {current.valid_from} 부터 적용
          </div>
        </dl>
      )}
    </section>
  );
}

/** 입력하지 않은 항목(`null`)과 실제 0을 구분해서 보여준다. */
function Money({ label, value, unit }: { label: string; value: number | null; unit: string }) {
  return (
    <>
      <dt className="text-[--color-muted-foreground]">{label}</dt>
      <dd className="tabular-nums">
        {value == null ? (
          <span className="text-[--color-muted-foreground]">미입력</span>
        ) : (
          `${value.toLocaleString("ko-KR")} ${unit}`
        )}
      </dd>
    </>
  );
}

/* ────────────────────────────────────────────────────────────
 * 예상 원가 + 현재 적용 원가 비교
 * ──────────────────────────────────────────────────────────── */

function LandedCostSection({
  sourceId, productId, supplierName, onAddFx,
}: {
  sourceId: string;
  productId: string;
  supplierName: string;
  onAddFx: () => void;
}) {
  const qc = useQueryClient();
  const { data, isError, error } = useQuery<CostComparison>({
    queryKey: ["landed-cost", sourceId],
    queryFn: () => sourcingApi.landedCost(sourceId),
    retry: false,
  });

  const adopt = useMutation({
    mutationFn: () =>
      requestCostAdoption({
        productId,
        landedCostKrw: data!.landed_cost.total_krw!,
        supplierName,
        costVersionId: null,
        fxRate: data?.landed_cost.fx?.rate ?? null,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["approvals"] });
      toast.success(
        "원가 변경 승인을 요청했습니다. 승인되어야 상품 원가에 반영됩니다.",
      );
    },
    onError: (e) => toast.error(errorMessage(e, "원가 반영 요청")),
  });

  if (isError) {
    // 권한이 없으면 계산 결과를 아예 주지 않는다(0 으로 주면 거짓이 된다).
    return (
      <section className="rounded-lg border border-[--color-border] p-3">
        <h4 className="mb-1.5 text-[12px] font-medium">예상 원가</h4>
        <p className="text-[11px] text-[--color-muted-foreground]">
          {error instanceof ApiError && error.status === 403
            ? "예상 원가를 볼 권한이 없습니다."
            : "예상 원가를 불러오지 못했습니다."}
        </p>
      </section>
    );
  }
  if (!data) return null;

  const lc = data.landed_cost;

  return (
    <section className="rounded-lg border border-[--color-border] p-3">
      <h4 className="mb-2 text-[12px] font-medium">예상 원가 (landed cost)</h4>

      {!lc.is_complete ? (
        <div className="space-y-2">
          <Unavailable missing={lc.missing} />
          {lc.missing.some((m) => m.startsWith("FX_RATE:")) && (
            <Button variant="outline" size="sm" onClick={onAddFx}>
              환율 등록
            </Button>
          )}
        </div>
      ) : (
        <>
          <table className="w-full text-[11px]">
            <tbody className="divide-y divide-[--color-border]">
              {lc.breakdown.map((b) => (
                <tr key={b.key}>
                  <td className="py-1 text-[--color-muted-foreground]">{b.label}</td>
                  <td className="py-1 text-right tabular-nums">
                    {b.provided ? (
                      formatKRW(b.amount_krw)
                    ) : (
                      // 입력하지 않아 0 인 것과 실제 0 을 구분한다.
                      <span className="text-[--color-muted-foreground]">미입력</span>
                    )}
                  </td>
                </tr>
              ))}
              <tr className="border-t-2 border-[--color-border]">
                <td className="py-1 font-medium">예상 원가</td>
                <td className="py-1 text-right font-semibold tabular-nums">
                  {formatKRW(lc.total_krw)}
                </td>
              </tr>
            </tbody>
          </table>

          {lc.fx && (
            <p className="mt-1.5 text-[10px] text-[--color-muted-foreground]">
              환율 {lc.fx.rate} · {lc.fx.source_type}
              {lc.fx.observed_at ? ` · ${lc.fx.observed_at.slice(0, 10)}` : ""}
            </p>
          )}

          <div className="mt-3 rounded-md bg-[--color-surface-muted] px-3 py-2">
            <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
              <dt className="text-[--color-muted-foreground]">현재 적용 원가</dt>
              <dd className="text-right tabular-nums">
                {data.current_cost_amount == null
                  ? "미설정"
                  : formatKRW(data.current_cost_amount)}
              </dd>
              <dt className="text-[--color-muted-foreground]">차이</dt>
              <dd className="text-right tabular-nums">
                {data.difference == null ? (
                  <span className="text-[--color-muted-foreground]">비교 불가</span>
                ) : (
                  <>
                    {data.difference > 0 ? "+" : ""}
                    {formatKRW(data.difference)}
                    {data.difference_rate != null && (
                      <span className="ml-1 text-[--color-muted-foreground]">
                        ({(data.difference_rate * 100).toFixed(2)}%)
                      </span>
                    )}
                  </>
                )}
              </dd>
            </dl>

            <Button
              size="sm" className="mt-2 w-full"
              disabled={adopt.isPending || data.difference === 0}
              onClick={() => adopt.mutate()}
            >
              <TrendingUp className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              현재 상품 원가로 적용 요청
            </Button>
            <p className="mt-1 text-[10px] text-[--color-muted-foreground]">
              바로 반영되지 않습니다. 승인되어야 상품 원가가 바뀌고, 이미 확정한
              달의 숫자는 변하지 않습니다.
            </p>
          </div>
        </>
      )}
    </section>
  );
}

/* ────────────────────────────────────────────────────────────
 * 권장가 · 민감도 · 이력
 * ──────────────────────────────────────────────────────────── */

function RecommendationSection({
  sourceId, marketplace,
}: {
  sourceId: string;
  marketplace: string;
}) {
  const { data } = useQuery<Recommendation>({
    queryKey: ["recommendation", sourceId, marketplace],
    queryFn: () => sourcingApi.recommendation(sourceId, marketplace),
    retry: false,
  });
  if (!data) return null;

  return (
    <section className="rounded-lg border border-[--color-border] p-3">
      <h4 className="mb-2 text-[12px] font-medium">권장 판매가 ({marketplace})</h4>

      {!data.available ? (
        <Unavailable missing={data.missing ?? []} />
      ) : (
        <>
          <table className="w-full text-[11px]">
            <thead className="text-[10px] text-[--color-muted-foreground]">
              <tr>
                <th scope="col" className="py-1 text-left font-medium">기준</th>
                <th scope="col" className="py-1 text-right font-medium">목표 마진</th>
                <th scope="col" className="py-1 text-right font-medium">판매가</th>
                <th scope="col" className="py-1 text-right font-medium">예상 마진</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[--color-border]">
              {data.prices?.map((p) => (
                <tr key={p.key}>
                  <td className="py-1">{PRICE_LABEL[p.key] ?? p.key}</td>
                  <td className="py-1 text-right tabular-nums">
                    {(Number(p.target_rate) * 100).toFixed(1)}%
                  </td>
                  <td className="py-1 text-right font-medium tabular-nums">
                    {formatKRW(p.recommended_price)}
                  </td>
                  <td className="py-1 text-right tabular-nums">
                    {formatKRW(p.expected_margin_amount)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {/* 나중에 "왜 권장가가 달라졌나" 에 답할 수 있어야 한다. */}
          {data.basis && (
            <details className="mt-2">
              <summary className="cursor-pointer text-[10px] text-[--color-muted-foreground]">
                계산 근거
              </summary>
              <ul className="mt-1 space-y-0.5 text-[10px] text-[--color-muted-foreground]">
                <li>기준일 {data.basis.as_of}</li>
                {data.basis.fx && <li>환율 {data.basis.fx.rate}</li>}
                {data.basis.source_cost_version_id && (
                  <li>원가 version {data.basis.source_cost_version_id.slice(0, 8)}</li>
                )}
                {data.basis.policy_versions.map((p) => (
                  <li key={p.policy_row_id}>
                    {p.policy_table} v{p.version}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </>
      )}
    </section>
  );
}

function SensitivitySection({
  sourceId, marketplace, salePrice,
}: {
  sourceId: string;
  marketplace: string;
  salePrice: number;
}) {
  const { data } = useQuery<Sensitivity>({
    queryKey: ["sensitivity", sourceId, marketplace, salePrice],
    queryFn: () => sourcingApi.sensitivity(sourceId, marketplace, salePrice),
    enabled: salePrice > 0,
    retry: false,
  });
  if (!data) return null;

  return (
    <section className="rounded-lg border border-[--color-border] p-3">
      <h4 className="mb-2 text-[12px] font-medium">
        민감도 <span className="text-[10px] text-[--color-muted-foreground]">
          판매가 {formatKRW(salePrice)} 기준
        </span>
      </h4>
      <p className="mb-2 text-[10px] text-[--color-muted-foreground]">
        가정해 보는 것일 뿐 저장되지 않습니다. 실제 환율·원가는 바뀌지 않습니다.
      </p>

      <div className="grid gap-3 sm:grid-cols-2">
        <ScenarioTable
          title="환율이 오르면"
          unavailable={!data.fx.available ? (data.fx.missing ?? []) : null}
          rows={(data.fx.scenarios ?? []).map((s) => ({
            key: s.step,
            left: `+${(Number(s.step) * 100).toFixed(0)}% (${s.rate})`,
            rate: s.margin_rate,
          }))}
        />
        <ScenarioTable
          title="원가가 오르면"
          unavailable={!data.cost.available ? (data.cost.missing ?? []) : null}
          rows={(data.cost.scenarios ?? []).map((s) => ({
            key: s.step,
            left: `+${(Number(s.step) * 100).toFixed(0)}% (${formatKRW(s.cost_krw ?? 0)})`,
            rate: s.margin_rate,
          }))}
        />
      </div>
    </section>
  );
}

function ScenarioTable({
  title, rows, unavailable,
}: {
  title: string;
  rows: { key: string; left: string; rate?: string }[];
  unavailable: string[] | null;
}) {
  return (
    <div>
      <p className="mb-1 text-[11px] font-medium">{title}</p>
      {unavailable ? (
        <Unavailable missing={unavailable} />
      ) : (
        <table className="w-full text-[11px]">
          <tbody className="divide-y divide-[--color-border]">
            {rows.map((r) => (
              <tr key={r.key}>
                <td className="py-1 text-[--color-muted-foreground]">{r.left}</td>
                <td className="py-1 text-right tabular-nums">
                  {r.rate == null
                    ? "—"
                    : `${(Number(r.rate) * 100).toFixed(1)}%`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function CostHistorySection({ sourceId }: { sourceId: string }) {
  const { data } = useQuery({
    queryKey: ["cost-versions", sourceId],
    queryFn: () => sourcingApi.costVersions(sourceId),
  });
  const rows = data?.items ?? [];
  if (rows.length <= 1) return null;

  return (
    <section className="rounded-lg border border-[--color-border] p-3">
      <h4 className="mb-1.5 text-[12px] font-medium">원가 이력</h4>
      {/* 과거 version 을 고치는 버튼은 없다 — 그때 판단의 근거이므로 남아야 한다. */}
      <table className="w-full text-[11px]">
        <tbody className="divide-y divide-[--color-border]">
          {rows.map((v) => (
            <tr key={v.id}>
              <td className="py-1 tabular-nums text-[--color-muted-foreground]">
                {v.valid_from} ~ {v.valid_to ?? "현재"}
              </td>
              <td className="py-1 text-right tabular-nums">
                {v.purchase_price == null
                  ? "가려짐"
                  : `${v.purchase_price.toLocaleString("ko-KR")} ${v.currency}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/* ────────────────────────────────────────────────────────────
 * 입력 다이얼로그
 * ──────────────────────────────────────────────────────────── */

function AddSourceDialog({
  open, onOpenChange, productId, onAdded,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  productId: string;
  onAdded: () => void;
}) {
  const [supplierId, setSupplierId] = React.useState("");
  const [url, setUrl] = React.useState("");
  const [currency, setCurrency] = React.useState("KRW");
  const [moq, setMoq] = React.useState("");
  const [lead, setLead] = React.useState("");

  const { data: suppliers } = useQuery({
    queryKey: ["supplier-sites", ""],
    queryFn: () => sourcingApi.listSuppliers({ limit: 200 }),
    enabled: open,
  });

  const add = useMutation({
    mutationFn: () =>
      sourcingApi.addSource(productId, {
        supplier_site_id: supplierId,
        source_url: url.trim() || null,
        currency,
        moq: moq ? Number(moq) : null,
        lead_time_days: lead ? Number(lead) : null,
      }),
    onSuccess: () => {
      onAdded();
      onOpenChange(false);
      setSupplierId(""); setUrl(""); setMoq(""); setLead("");
      toast.success("소싱처를 연결했습니다.");
    },
    onError: (e) => toast.error(errorMessage(e, "소싱처 연결")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>소싱처 추가</DialogTitle></DialogHeader>
        <div className="space-y-3 py-2 text-sm">
          <div className="space-y-1">
            <Label htmlFor="ps-sup" className="text-xs">공급처 *</Label>
            <select
              id="ps-sup" value={supplierId}
              onChange={(e) => setSupplierId(e.target.value)}
              className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
            >
              <option value="">선택하세요</option>
              {(suppliers?.items ?? []).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.supplier_code})
                </option>
              ))}
            </select>
            {/* 없는 공급처를 여기서 즉석으로 만들지 않는다 — 근거 없는 거래처가 생긴다. */}
            <p className="text-[11px] text-[--color-muted-foreground]">
              공급처가 없으면 먼저 공급처 화면에서 등록하세요.
            </p>
          </div>
          <div className="space-y-1">
            <Label htmlFor="ps-url" className="text-xs">원본 상품 URL</Label>
            <Input
              id="ps-url" value={url} onChange={(e) => setUrl(e.target.value)}
              className="h-8 text-sm" placeholder="https://detail.1688.com/offer/…"
            />
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div className="space-y-1">
              <Label htmlFor="ps-cur" className="text-xs">매입 통화</Label>
              <select
                id="ps-cur" value={currency} onChange={(e) => setCurrency(e.target.value)}
                className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
              >
                {["KRW", "CNY", "USD", "JPY", "EUR"].map((c) => <option key={c}>{c}</option>)}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="ps-moq" className="text-xs">MOQ</Label>
              <Input
                id="ps-moq" type="number" value={moq}
                onChange={(e) => setMoq(e.target.value)} className="h-8 text-sm"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="ps-lead" className="text-xs">리드타임(일)</Label>
              <Input
                id="ps-lead" type="number" value={lead}
                onChange={(e) => setLead(e.target.value)} className="h-8 text-sm"
              />
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>취소</Button>
          <Button size="sm" disabled={!supplierId || add.isPending} onClick={() => add.mutate()}>
            연결
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AddCostDialog({
  open, onOpenChange, source, onAdded,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  source: ProductSource;
  onAdded: () => void;
}) {
  const [price, setPrice] = React.useState("");
  const [from, setFrom] = React.useState(today());
  const [fields, setFields] = React.useState<Record<string, string>>({});

  const set = (k: string, v: string) => setFields((p) => ({ ...p, [k]: v }));
  const num = (k: string) => (fields[k] ? Number(fields[k]) : null);

  const add = useMutation({
    mutationFn: () =>
      sourcingApi.addCostVersion(source.id, {
        purchase_price: Number(price),
        currency: source.currency,
        valid_from: from,
        domestic_shipping: num("domestic_shipping"),
        international_shipping: num("international_shipping"),
        customs_duty: num("customs_duty"),
        import_vat: num("import_vat"),
        packaging_cost: num("packaging_cost"),
        other_cost: num("other_cost"),
      }),
    onSuccess: () => {
      onAdded();
      onOpenChange(false);
      setPrice(""); setFields({});
      toast.success("새 원가를 등록했습니다. 이전 원가는 이력으로 남습니다.");
    },
    onError: (e) => toast.error(errorMessage(e, "원가 등록")),
  });

  const EXTRA: [string, string, string][] = [
    ["domestic_shipping", "현지 배송비", source.currency],
    ["international_shipping", "국제 배송비", "KRW"],
    ["customs_duty", "관세", "KRW"],
    ["import_vat", "수입 부가세", "KRW"],
    ["packaging_cost", "포장비", "KRW"],
    ["other_cost", "기타", "KRW"],
  ];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>새 원가 등록</DialogTitle></DialogHeader>
        <div className="space-y-3 py-2 text-sm">
          <p className="rounded-md border border-[--color-border] bg-[--color-surface-muted] px-3 py-2 text-[11px] text-[--color-muted-foreground]">
            기존 원가를 고치지 않고 새 버전을 만듭니다. 이전 값은 그때 판단의
            근거로 남습니다.
          </p>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label htmlFor="cv-price" className="text-xs">
                매입가 ({source.currency}) *
              </Label>
              <Input
                id="cv-price" type="number" value={price}
                onChange={(e) => setPrice(e.target.value)} className="h-8 text-sm"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="cv-from" className="text-xs">적용일 *</Label>
              <Input
                id="cv-from" type="date" value={from}
                onChange={(e) => setFrom(e.target.value)} className="h-8 text-sm"
              />
            </div>
            {EXTRA.map(([k, label, unit]) => (
              <div key={k} className="space-y-1">
                <Label htmlFor={`cv-${k}`} className="text-xs">{label} ({unit})</Label>
                <Input
                  id={`cv-${k}`} type="number" value={fields[k] ?? ""}
                  onChange={(e) => set(k, e.target.value)} className="h-8 text-sm"
                  placeholder="미입력"
                />
              </div>
            ))}
          </div>
          <p className="text-[10px] text-[--color-muted-foreground]">
            비워 두면 &quot;미입력&quot;으로 남습니다 — 0원과 구분됩니다.
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>취소</Button>
          <Button size="sm" disabled={!price || add.isPending} onClick={() => add.mutate()}>
            등록
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AddFxDialog({
  open, onOpenChange, currency, onAdded,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  currency: string;
  onAdded: () => void;
}) {
  const [rate, setRate] = React.useState("");
  const [type, setType] = React.useState("SOURCING");

  const add = useMutation({
    mutationFn: () =>
      sourcingApi.createFxRate({
        base_currency: currency,
        rate,
        rate_type: type as never,
      }),
    onSuccess: () => {
      onAdded();
      onOpenChange(false);
      setRate("");
      toast.success("환율을 등록했습니다.");
    },
    onError: (e) => toast.error(errorMessage(e, "환율 등록")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>환율 등록</DialogTitle></DialogHeader>
        <div className="space-y-3 py-2 text-sm">
          <p className="flex items-start gap-1.5 rounded-md border border-[--color-border] bg-[--color-surface-muted] px-3 py-2 text-[11px] text-[--color-muted-foreground]">
            <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
            <span>
              직접 입력하는 값입니다. 외부 환율 API 연동이 아니므로 자동으로
              갱신되지 않습니다.
            </span>
          </p>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label htmlFor="fx-rate" className="text-xs">
                1 {currency} = ? KRW *
              </Label>
              <Input
                id="fx-rate" type="number" step="0.0001" value={rate}
                onChange={(e) => setRate(e.target.value)} className="h-8 text-sm"
                placeholder="205.34"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="fx-type" className="text-xs">시점 구분</Label>
              <select
                id="fx-type" value={type} onChange={(e) => setType(e.target.value)}
                className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
              >
                {Object.entries(FX_RATE_TYPE_LABEL).map(([v, l]) => (
                  <option key={v} value={v}>{l}</option>
                ))}
              </select>
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>취소</Button>
          <Button size="sm" disabled={!rate || add.isPending} onClick={() => add.mutate()}>
            등록
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
