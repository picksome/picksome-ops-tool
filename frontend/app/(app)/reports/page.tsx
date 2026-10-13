"use client";

import { useState } from "react";
import { Download, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { reportsApi, REPORT_LABELS, type ReportType } from "@/lib/api/reports";
import { getAccessToken } from "@/lib/auth";

const REPORT_GROUPS: { title: string; types: ReportType[] }[] = [
  {
    title: "매출·매입",
    types: ["MONTHLY_SALES", "MONTHLY_PURCHASE", "SALES_BY_CHANNEL"],
  },
  {
    title: "손익·배분",
    types: ["PRODUCT_PNL", "PARTNER_DISTRIBUTION"],
  },
  {
    title: "세무·증빙",
    types: ["EXPENSE", "VAT_REFERENCE", "MISSING_EVIDENCE"],
  },
  {
    title: "정산",
    types: ["SETTLEMENT_RECONCILIATION"],
  },
  {
    // 세무 제출용이 아니라 내부 운영 검토용이다. 그룹을 나눠 두지 않으면
    // 세무사에게 보낼 묶음에 운영 이슈까지 섞어 보내게 된다.
    title: "운영 (내부 검토용)",
    types: ["PAYOUT_LEDGER", "CASHFLOW", "OPERATION_ISSUES"],
  },
];

function currentYm() {
  return new Date().toISOString().slice(0, 7);
}

export default function ReportsPage() {
  const [ym, setYm] = useState(currentYm());
  const [downloading, setDownloading] = useState<ReportType | null>(null);

  const [zipStartDate, setZipStartDate] = useState("");
  const [zipEndDate, setZipEndDate] = useState("");
  const [zipFormat, setZipFormat] = useState<"xlsx" | "csv">("xlsx");
  const [zipLoading, setZipLoading] = useState(false);

  async function handleZipDownload() {
    if (!zipStartDate || !zipEndDate) {
      toast.error("기간을 선택하세요.");
      return;
    }
    setZipLoading(true);
    try {
      const token = getAccessToken();
      const res = await fetch("/api/v1/reports/export-zip", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ period_start: zipStartDate, period_end: zipEndDate, format: zipFormat }),
      });
      if (!res.ok) { toast.error("다운로드 실패"); return; }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${zipStartDate}_전체리포트.zip`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success("다운로드 완료");
    } catch {
      toast.error("다운로드 중 오류가 발생했습니다.");
    } finally {
      setZipLoading(false);
    }
  }

  async function handleDownload(type: ReportType) {
    setDownloading(type);
    try {
      await reportsApi.download(type, ym);
      toast.success(`${REPORT_LABELS[type]} 다운로드 완료`);
    } catch {
      toast.error("다운로드에 실패했습니다");
    } finally {
      setDownloading(null);
    }
  }

  const [displayY, displayM] = ym.split("-");

  return (
    <div className="flex flex-col gap-6 p-6">
      <PageHeader
        title="세무 리포트"
        description="월별 Excel 리포트를 다운로드합니다. 세무사 제출용 자료입니다."
      />

      {/* 월 선택 */}
      <div className="flex items-center gap-3">
        <label className="text-sm font-medium" htmlFor="ym-picker">
          기준 월
        </label>
        <input
          id="ym-picker"
          type="month"
          value={ym}
          onChange={(e) => setYm(e.target.value)}
          className="h-9 rounded-lg border border-[--color-border] bg-transparent px-3 text-sm focus:outline-none focus:ring-2 focus:ring-[--color-accent]"
        />
        <span className="text-sm text-[--color-muted-foreground]">
          {displayY}년 {parseInt(displayM)}월 기준
        </span>
      </div>

      {/* 리포트 그룹 */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-2">
        {REPORT_GROUPS.map((group) => (
          <Card key={group.title}>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-semibold text-[--color-muted-foreground]">
                {group.title}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {group.types.map((type) => (
                <div
                  key={type}
                  className="flex items-center justify-between rounded-md border border-[--color-border] px-3 py-2"
                >
                  <span className="text-sm">{REPORT_LABELS[type]}</span>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleDownload(type)}
                    disabled={downloading !== null}
                    className="h-7 gap-1 text-xs"
                  >
                    {downloading === type ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    ) : (
                      <Download className="h-3 w-3" />
                    )}
                    다운로드
                  </Button>
                </div>
              ))}
            </CardContent>
          </Card>
        ))}
      </div>

      {/* ZIP 번들 다운로드 */}
      <div className="rounded-lg border border-[--color-border] bg-[--color-card] p-5">
        <h3 className="mb-1 font-semibold">전체 리포트 ZIP 다운로드</h3>
        <p className="mb-4 text-sm text-[--color-muted-foreground]">
          선택한 기간의 모든 리포트를 ZIP 파일로 일괄 다운로드합니다.
        </p>
        <div className="flex flex-wrap gap-3 items-end">
          <div>
            <label className="mb-1 block text-xs text-[--color-muted-foreground]">시작 월</label>
            <input
              type="month"
              className="h-9 rounded-md border border-[--color-border] bg-[--color-background] px-3 text-sm"
              value={zipStartDate}
              onChange={(e) => setZipStartDate(e.target.value)}
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-[--color-muted-foreground]">종료 월</label>
            <input
              type="month"
              className="h-9 rounded-md border border-[--color-border] bg-[--color-background] px-3 text-sm"
              value={zipEndDate}
              onChange={(e) => setZipEndDate(e.target.value)}
            />
          </div>
          <div className="flex gap-2">
            {(["xlsx", "csv"] as const).map((f) => (
              <button
                key={f}
                type="button"
                onClick={() => setZipFormat(f)}
                className={`rounded-md border px-3 py-1.5 text-sm ${zipFormat === f ? "border-[--color-primary] bg-[--color-primary] text-white" : "border-[--color-border] text-[--color-foreground]"}`}
              >
                {f.toUpperCase()}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={handleZipDownload}
            disabled={zipLoading}
            className="h-9 rounded-md bg-[--color-primary] px-4 text-sm font-medium text-white disabled:opacity-50"
          >
            {zipLoading ? "다운로드 중..." : "ZIP 다운로드"}
          </button>
        </div>
      </div>

      <p className="text-xs text-[--color-muted-foreground]">
        * 파일명 형식: {ym}_리포트명.xlsx / 세무사 제출 전 내용을 반드시 검토하세요.
      </p>
    </div>
  );
}
