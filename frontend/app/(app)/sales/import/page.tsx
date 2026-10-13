"use client";

import { useState, useRef } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { PageHeader } from "@/components/shell/page-header";
import { MoneyText } from "@/components/ui/finance";
import { type SaleChannel } from "@/lib/api/sales";
import { useSaleChannels } from "@/hooks/useSaleChannels";
import { getAccessToken } from "@/lib/auth";
import { toast } from "sonner";
import { Upload, CheckCircle2, AlertTriangle, FileText } from "lucide-react";

// ─── Types ────────────────────────────────────────────────────────────────────

interface PreviewRow {
  sale_date: string;
  marketplace: string;
  quantity: number;
  sale_price: number;
  [key: string]: unknown;
}

interface PreviewData {
  batch_id: string;
  total_rows: number;
  error_rows: number;
  preview_rows: PreviewRow[];
}

interface CommitResult {
  imported_count: number;
  [key: string]: unknown;
}

// ─── Step indicator ──────────────────────────────────────────────────────────

function StepIndicator({ step }: { step: 1 | 2 | 3 }) {
  const steps = [
    { n: 1, label: "파일 선택" },
    { n: 2, label: "미리보기" },
    { n: 3, label: "완료" },
  ];
  return (
    <div className="flex items-center gap-0 mb-6">
      {steps.map((s, i) => (
        <div key={s.n} className="flex items-center">
          <div
            className={[
              "flex h-7 w-7 items-center justify-center rounded-full text-xs font-semibold",
              step === s.n
                ? "bg-[--color-primary] text-white"
                : step > s.n
                  ? "bg-[--color-success] text-white"
                  : "bg-[--color-muted] text-[--color-muted-foreground]",
            ].join(" ")}
          >
            {step > s.n ? "✓" : s.n}
          </div>
          <span
            className={[
              "ml-2 text-sm",
              step === s.n ? "font-semibold" : "text-[--color-muted-foreground]",
            ].join(" ")}
          >
            {s.label}
          </span>
          {i < steps.length - 1 && (
            <div className="mx-4 h-px w-10 bg-[--color-border]" />
          )}
        </div>
      ))}
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

// CSV import 템플릿(GET /sales/import/templates/{marketplace})이 정의된 마켓만 — 전체
// sale_channel 카탈로그가 아니라 의도적으로 고정된 부분집합(오늘의집 같은 커스텀 채널은
// 아직 CSV 템플릿이 없으므로 여기 포함하지 않음).
const IMPORT_CHANNELS: SaleChannel[] = ["COUPANG", "SMARTSTORE", "ELEVENST", "OTHER"];

export default function SalesImportPage() {
  const router = useRouter();
  const fileRef = useRef<HTMLInputElement>(null);
  const { labelOf } = useSaleChannels();

  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [marketplace, setMarketplace] = useState<SaleChannel | "">("");
  const [file, setFile] = useState<File | null>(null);
  const [previewData, setPreviewData] = useState<PreviewData | null>(null);
  const [importedCount, setImportedCount] = useState(0);
  const [loading, setLoading] = useState(false);

  // ─── Step 1: Preview ────────────────────────────────────────────────────────

  async function handlePreview() {
    if (!marketplace) {
      toast.error("마켓을 선택하세요.");
      return;
    }
    if (!file) {
      toast.error("CSV 파일을 선택하세요.");
      return;
    }

    setLoading(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("marketplace", marketplace);

      const token = getAccessToken();
      const res = await fetch("/api/v1/sales/import/preview", {
        method: "POST",
        body: formData,
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });

      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.error?.message ?? `미리보기 실패 (${res.status})`);
      }

      const body = await res.json();
      const data: PreviewData = body?.data ?? body;
      setPreviewData(data);
      setStep(2);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "미리보기 중 오류가 발생했습니다.");
    } finally {
      setLoading(false);
    }
  }

  // ─── Step 2: Commit ─────────────────────────────────────────────────────────

  async function handleCommit() {
    if (!previewData) return;
    setLoading(true);
    try {
      const token = getAccessToken();
      const res = await fetch("/api/v1/sales/import/commit", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ batch_id: previewData.batch_id, marketplace }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.error?.message ?? `임포트 실패 (${res.status})`);
      }

      const body = await res.json();
      const result: CommitResult = body?.data ?? body;
      setImportedCount(result.imported_count ?? 0);
      setStep(3);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "임포트 중 오류가 발생했습니다.");
    } finally {
      setLoading(false);
    }
  }

  // ─── Render ──────────────────────────────────────────────────────────────────

  return (
    <div className="flex h-full flex-col p-6">
      <PageHeader title="판매 CSV 임포트" description="마켓 판매 데이터를 CSV로 일괄 등록합니다." />
      <StepIndicator step={step} />

      {/* ── Step 1 ──────────────────────────────────────────────────────────── */}
      {step === 1 && (
        <div className="flex flex-col gap-5 max-w-lg">
          {/* 마켓 선택 */}
          <div className="flex flex-col gap-1.5">
            <label className="text-sm font-medium">
              마켓 선택 <span className="text-[--color-danger]">*</span>
            </label>
            <Select
              value={marketplace}
              onValueChange={(v) => setMarketplace(v as SaleChannel)}
            >
              <SelectTrigger className="w-60">
                <SelectValue placeholder="마켓을 선택하세요" />
              </SelectTrigger>
              <SelectContent>
                {IMPORT_CHANNELS.map((ch) => (
                  <SelectItem key={ch} value={ch}>
                    {labelOf(ch)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* 템플릿 다운로드 */}
          {marketplace && (
            <div>
              <a
                href={`/api/v1/sales/import/templates/${marketplace}`}
                download
                className="inline-flex items-center gap-1.5 text-sm text-[--color-primary] underline"
              >
                <FileText className="h-4 w-4" />
                템플릿 다운로드
              </a>
              <p className="mt-1 text-xs text-[--color-muted-foreground]">
                템플릿 양식에 맞춰 데이터를 입력 후 업로드하세요.
              </p>
            </div>
          )}

          {/* 파일 선택 */}
          <div className="flex flex-col gap-1.5">
            <label className="text-sm font-medium">
              CSV 파일 <span className="text-[--color-danger]">*</span>
            </label>
            <div
              className="flex cursor-pointer items-center gap-3 rounded-lg border-2 border-dashed border-[--color-border] p-5 hover:border-[--color-primary] transition-colors"
              onClick={() => fileRef.current?.click()}
            >
              <Upload className="h-5 w-5 text-[--color-muted-foreground]" />
              <div>
                <p className="text-sm font-medium">
                  {file ? file.name : "파일을 클릭하여 선택하세요"}
                </p>
                {file && (
                  <p className="text-xs text-[--color-muted-foreground]">
                    {(file.size / 1024).toFixed(1)} KB
                  </p>
                )}
                {!file && (
                  <p className="text-xs text-[--color-muted-foreground]">.csv 형식만 허용</p>
                )}
              </div>
            </div>
            <input
              ref={fileRef}
              type="file"
              accept=".csv"
              className="hidden"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </div>

          <div className="flex gap-2 pt-2">
            <Button
              onClick={handlePreview}
              disabled={loading || !marketplace || !file}
            >
              {loading ? "미리보기 생성 중…" : "다음 단계"}
            </Button>
          </div>
        </div>
      )}

      {/* ── Step 2 ──────────────────────────────────────────────────────────── */}
      {step === 2 && previewData && (
        <div className="flex flex-col gap-5">
          {/* 통계 요약 */}
          <div className="flex gap-4">
            <div className="rounded-lg border border-[--color-border] bg-[--color-card] px-4 py-3">
              <p className="text-xs text-[--color-muted-foreground]">전체 행</p>
              <p className="mt-0.5 text-xl font-bold tabular-nums">
                {previewData.total_rows.toLocaleString("ko-KR")}
              </p>
            </div>
            <div
              className={[
                "rounded-lg border px-4 py-3",
                previewData.error_rows > 0
                  ? "border-[--color-warning-border] bg-[--color-warning-bg]"
                  : "border-[--color-border] bg-[--color-card]",
              ].join(" ")}
            >
              <p className="text-xs text-[--color-muted-foreground] flex items-center gap-1">
                {previewData.error_rows > 0 && (
                  <AlertTriangle className="h-3 w-3 text-[--color-warning]" />
                )}
                오류 행
              </p>
              <p className="mt-0.5 text-xl font-bold tabular-nums">
                {previewData.error_rows.toLocaleString("ko-KR")}
              </p>
            </div>
          </div>

          {/* 미리보기 테이블 */}
          <div>
            <p className="mb-2 text-sm font-medium">
              미리보기 (최대 20행)
            </p>
            <div className="overflow-auto rounded-lg border border-[--color-border]">
              <table className="w-full border-collapse text-[13px]">
                <thead className="bg-[--color-muted]">
                  <tr>
                    <th className="border-b border-[--color-border] px-3 py-2 text-left text-[12px] font-semibold uppercase tracking-wide text-[--color-muted-foreground]">
                      판매일
                    </th>
                    <th className="border-b border-[--color-border] px-3 py-2 text-left text-[12px] font-semibold uppercase tracking-wide text-[--color-muted-foreground]">
                      마켓
                    </th>
                    <th className="border-b border-[--color-border] px-3 py-2 text-left text-[12px] font-semibold uppercase tracking-wide text-[--color-muted-foreground]">
                      수량
                    </th>
                    <th className="border-b border-[--color-border] px-3 py-2 text-left text-[12px] font-semibold uppercase tracking-wide text-[--color-muted-foreground]">
                      판매가+배송비
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {previewData.preview_rows.slice(0, 20).map((row, i) => (
                    <tr
                      key={i}
                      className="border-b border-[--color-border] last:border-0 hover:bg-[--color-muted]/50"
                    >
                      <td className="px-3 py-2.5 tabular-nums text-xs">
                        {String(row.sale_date ?? "").slice(0, 10)}
                      </td>
                      <td className="px-3 py-2.5">
                        <Badge variant="info" className="text-[11px]">
                          {labelOf(row.marketplace)}
                        </Badge>
                      </td>
                      <td className="px-3 py-2.5 tabular-nums">
                        {row.quantity}
                      </td>
                      <td className="px-3 py-2.5">
                        <MoneyText value={row.sale_price} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="flex gap-2 pt-2">
            <Button
              variant="outline"
              onClick={() => {
                setStep(1);
                setPreviewData(null);
              }}
            >
              이전
            </Button>
            <Button onClick={handleCommit} disabled={loading}>
              {loading ? "임포트 중…" : "확인 및 임포트"}
            </Button>
          </div>
        </div>
      )}

      {/* ── Step 3 ──────────────────────────────────────────────────────────── */}
      {step === 3 && (
        <div className="flex flex-col items-center justify-center gap-6 py-16">
          <div className="flex h-16 w-16 items-center justify-center rounded-full bg-[--color-success-bg]">
            <CheckCircle2 className="h-8 w-8 text-[--color-success]" />
          </div>
          <div className="text-center">
            <h2 className="text-lg font-semibold">임포트 완료</h2>
            <p className="mt-1 text-sm text-[--color-muted-foreground]">
              총{" "}
              <span className="font-semibold tabular-nums text-[--color-foreground]">
                {importedCount.toLocaleString("ko-KR")}건
              </span>
              의 판매 데이터가 등록되었습니다.
            </p>
          </div>
          <Button onClick={() => router.push("/sales")}>판매 장부로 이동</Button>
        </div>
      )}
    </div>
  );
}
