"use client";

/**
 * 쿠팡 정산서 가져오기 (M056).
 *
 * 이 화면이 예전에 죽어 있었다
 * -------------------------
 * `/marketplace-settlements/import/preview` 와 `/commit` 을 부르고 있었는데
 * **그런 endpoint 는 없었다.** 버튼을 누르면 404 가 났다. CSV 만 받는다고
 * 적혀 있었지만 쿠팡 WING 이 주는 파일은 `.xlsx` 다.
 *
 * 실제 파일을 받아 보고 전부 다시 배선했다.
 *
 * 왜 판매계정을 사용자가 고르나
 * --------------------------
 * 파일 안에는 어느 스토어의 정산서인지 적혀 있지 않다. 서버가 "쿠팡 계정이
 * 하나뿐이니 그거겠지" 로 고르면, 두 번째 스토어가 생기는 날 조용히 틀린
 * 계정에 붙는다.
 *
 * 왜 미리보기가 먼저인가
 * -------------------
 * 정산 자료는 손익 귀속의 입력이다. 파일 하나가 수십~수백 행이고, 잘못
 * 넣으면 되돌리는 데 승인이 필요하다. 무엇이 들어갈지 먼저 보여준다.
 */

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/shell/page-header";
import { getAccessToken } from "@/lib/auth";
import {
  MATCHING_HELP,
  MATCHING_LABEL,
  settlementImportApi,
  type SettlementImportSummary,
} from "@/lib/api/settlement-import";
import { toast } from "sonner";
import { AlertTriangle, CheckCircle2, Info, Upload } from "lucide-react";

interface AccountOption {
  id: string;
  account_label: string;
  sale_channel_code: string | null;
}

function StepIndicator({ step }: { step: 1 | 2 | 3 }) {
  const steps = [
    { n: 1, label: "파일 · 계정" },
    { n: 2, label: "미리보기" },
    { n: 3, label: "완료" },
  ];
  return (
    <div className="mb-6 flex items-center gap-0">
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
          {i < steps.length - 1 && <div className="mx-4 h-px w-10 bg-[--color-border]" />}
        </div>
      ))}
    </div>
  );
}

function Stat({
  label,
  value,
  tone = "plain",
  help,
}: {
  label: string;
  value: number;
  tone?: "plain" | "good" | "warn";
  help?: string;
}) {
  const cls =
    tone === "good"
      ? "border-[--color-success-border] bg-[--color-success-bg] text-[--color-success]"
      : tone === "warn"
        ? "border-[--color-warning-border] bg-[--color-warning-bg] text-[--color-warning]"
        : "border-[--color-border] bg-[--color-card]";
  return (
    <div className={`rounded-lg border px-4 py-3 ${cls}`} title={help}>
      <p className="text-xs opacity-80">{label}</p>
      <p className="mt-0.5 text-xl font-bold tabular-nums">
        {value.toLocaleString("ko-KR")}
      </p>
    </div>
  );
}

export default function CoupangSettlementImportPage() {
  const router = useRouter();
  const fileRef = useRef<HTMLInputElement>(null);

  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [file, setFile] = useState<File | null>(null);
  const [accounts, setAccounts] = useState<AccountOption[]>([]);
  const [accountId, setAccountId] = useState("");
  const [preview, setPreview] = useState<SettlementImportSummary | null>(null);
  const [committed, setCommitted] = useState<SettlementImportSummary | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const token = getAccessToken();
    fetch(`${process.env.NEXT_PUBLIC_API_BASE ?? "/api/v1"}/integrations`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
      .then((r) => r.json())
      .then((b) => {
        const all: AccountOption[] = b?.data ?? [];
        // 쿠팡 계정만 고를 수 있다. 서버도 같은 검사를 한다 — 화면에서만
        // 막으면 API 를 직접 부르는 경로가 뚫린다.
        setAccounts(all.filter((a) => (a.sale_channel_code ?? "").toUpperCase() === "COUPANG"));
      })
      .catch(() => setAccounts([]));
  }, []);

  async function run(mode: "DRY_RUN" | "COMMIT") {
    if (!file) {
      toast.error("정산서 파일을 선택하세요.");
      return;
    }
    if (!accountId) {
      toast.error("어느 스토어의 정산서인지 선택하세요.");
      return;
    }
    setLoading(true);
    try {
      const result = await settlementImportApi.run(file, accountId, mode);
      if (mode === "DRY_RUN") {
        setPreview(result);
        setStep(2);
      } else {
        setCommitted(result);
        setStep(3);
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "정산서를 읽지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }

  const s = preview;

  return (
    <div className="flex h-full flex-col p-6">
      <PageHeader
        title="쿠팡 정산서 가져오기"
        description="쿠팡 WING에서 받은 매출·정산 상세 파일을 읽어 주문과 대사합니다."
      />
      <StepIndicator step={step} />

      {step === 1 && (
        <div className="flex max-w-lg flex-col gap-5">
          <div className="flex items-start gap-3 rounded-lg border border-[--color-info-border] bg-[--color-info-bg] p-4">
            <Info className="mt-0.5 h-4 w-4 shrink-0 text-[--color-info]" />
            <div className="text-sm text-[--color-info]">
              <p>
                쿠팡 WING <span className="font-semibold">정산</span> 메뉴에서 받은{" "}
                <span className="font-semibold">매출·정산 상세</span> 엑셀을 올리세요.
              </p>
              <p className="mt-1 opacity-90">
                같은 파일을 여러 번 올려도 정산 행이 두 번 생기지 않습니다.
              </p>
            </div>
          </div>

          <div className="flex flex-col gap-1.5">
            <label className="text-sm font-medium" htmlFor="settlement-account">
              판매 스토어 <span className="text-[--color-danger]">*</span>
            </label>
            <select
              id="settlement-account"
              className="rounded-lg border border-[--color-border] bg-[--color-card] px-3 py-2 text-sm"
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
            >
              <option value="">선택하세요</option>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.account_label}
                </option>
              ))}
            </select>
            {accounts.length === 0 && (
              <p className="text-xs text-[--color-warning]">
                쿠팡 판매계정이 없습니다. 설정 &gt; 연동에서 먼저 등록하세요.
              </p>
            )}
            <p className="text-xs text-[--color-muted-foreground]">
              파일에는 어느 스토어인지 적혀 있지 않습니다. 직접 골라야 합니다.
            </p>
          </div>

          <div className="flex flex-col gap-1.5">
            <label className="text-sm font-medium">
              정산서 파일 <span className="text-[--color-danger]">*</span>
            </label>
            <div
              className="flex cursor-pointer items-center gap-3 rounded-lg border-2 border-dashed border-[--color-border] p-5 transition-colors hover:border-[--color-primary]"
              onClick={() => fileRef.current?.click()}
            >
              <Upload className="h-5 w-5 text-[--color-muted-foreground]" />
              <div>
                <p className="text-sm font-medium">
                  {file ? file.name : "파일을 클릭하여 선택하세요"}
                </p>
                <p className="text-xs text-[--color-muted-foreground]">
                  {file ? `${(file.size / 1024).toFixed(1)} KB` : ".xlsx 형식"}
                </p>
              </div>
            </div>
            <input
              ref={fileRef}
              type="file"
              accept=".xlsx,.xlsm"
              className="hidden"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </div>

          <div className="flex gap-2 pt-2">
            <Button onClick={() => run("DRY_RUN")} disabled={loading || !file || !accountId}>
              {loading ? "읽는 중…" : "미리보기"}
            </Button>
          </div>
        </div>
      )}

      {step === 2 && s && (
        <div className="flex flex-col gap-5">
          <div className="flex flex-wrap gap-3">
            <Stat label="원본 행" value={s.source_rows} />
            <Stat label="새로 들어갈 행" value={s.inserted} tone="good" />
            {s.unchanged > 0 && (
              <Stat
                label="이미 있는 행"
                value={s.unchanged}
                help="같은 파일을 다시 올렸습니다. 두 번 들어가지 않습니다."
              />
            )}
            {s.rejected > 0 && (
              <Stat label="읽지 못한 행" value={s.rejected} tone="warn" />
            )}
          </div>

          <div>
            <p className="mb-2 text-sm font-medium">주문 매칭 결과</p>
            <div className="flex flex-wrap gap-3">
              <Stat
                label={MATCHING_LABEL.matched_exact}
                value={s.matched_exact}
                tone="good"
                help={MATCHING_HELP.matched_exact}
              />
              <Stat
                label={MATCHING_LABEL.ambiguous}
                value={s.ambiguous}
                tone={s.ambiguous > 0 ? "warn" : "plain"}
                help={MATCHING_HELP.ambiguous}
              />
              <Stat
                label={MATCHING_LABEL.unmatched}
                value={s.unmatched}
                tone={s.unmatched > 0 ? "warn" : "plain"}
                help={MATCHING_HELP.unmatched}
              />
            </div>
          </div>

          <dl className="grid max-w-xl grid-cols-[10rem_1fr] gap-y-1 text-sm">
            <dt className="text-[--color-muted-foreground]">정산 기간</dt>
            <dd className="tabular-nums">
              {s.period_start} ~ {s.period_end}
            </dd>
            <dt className="text-[--color-muted-foreground]">정산 예정일</dt>
            <dd className="tabular-nums">{s.due_dates.join(", ") || "—"}</dd>
            <dt className="text-[--color-muted-foreground]">시트</dt>
            <dd>{s.sheet_name}</dd>
          </dl>

          {!s.reconciles && (
            <div className="flex items-start gap-2 rounded-lg border border-[--color-danger-border] bg-[--color-danger-bg] p-3 text-sm text-[--color-danger]">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                원본 행 수와 처리된 행 수가 맞지 않습니다. 반영하지 마세요.
              </span>
            </div>
          )}

          {s.changed_not_applied > 0 && (
            <div className="flex items-start gap-2 rounded-lg border border-[--color-warning-border] bg-[--color-warning-bg] p-3 text-sm text-[--color-warning]">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                이미 등록된 행 {s.changed_not_applied}건의 금액이 달라졌습니다.{" "}
                <span className="font-semibold">덮어쓰지 않습니다</span> — 재발행된
                정산서라면 정정으로 처리해야 합니다.
              </span>
            </div>
          )}

          {s.unknown_headers.length > 0 && (
            <div className="flex items-start gap-2 rounded-lg border border-[--color-warning-border] bg-[--color-warning-bg] p-3 text-sm text-[--color-warning]">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                처음 보는 컬럼이 있습니다: {s.unknown_headers.join(", ")}. 쿠팡이 정산서
                형식을 바꿨을 수 있습니다.
              </span>
            </div>
          )}

          {s.rejects.length > 0 && (
            <div>
              <p className="mb-2 text-sm font-medium">읽지 못한 행</p>
              <div className="overflow-auto rounded-lg border border-[--color-border]">
                <table className="w-full border-collapse text-[13px]">
                  <thead className="bg-[--color-muted]">
                    <tr>
                      {["행", "사유", "내용"].map((h) => (
                        <th
                          key={h}
                          className="border-b border-[--color-border] px-3 py-2 text-left text-[12px] font-semibold uppercase tracking-wide text-[--color-muted-foreground]"
                        >
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {s.rejects.map((r) => (
                      <tr key={r.row} className="border-b border-[--color-border] last:border-0">
                        <td className="px-3 py-2 tabular-nums">{r.row}</td>
                        <td className="px-3 py-2">{r.reason}</td>
                        <td className="px-3 py-2 text-[--color-muted-foreground]">
                          {r.detail}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <div className="flex gap-2 pt-2">
            <Button
              variant="outline"
              onClick={() => {
                setStep(1);
                setPreview(null);
              }}
            >
              이전
            </Button>
            <Button onClick={() => run("COMMIT")} disabled={loading || !s.reconciles}>
              {loading ? "반영 중…" : "확인하고 반영"}
            </Button>
          </div>
        </div>
      )}

      {step === 3 && committed && (
        <div className="flex flex-col items-center justify-center gap-6 py-16">
          <div className="flex h-16 w-16 items-center justify-center rounded-full bg-[--color-success-bg]">
            <CheckCircle2 className="h-8 w-8 text-[--color-success]" />
          </div>
          <div className="text-center">
            <h2 className="text-lg font-semibold">가져오기 완료</h2>
            <p className="mt-1 text-sm text-[--color-muted-foreground]">
              새로 등록{" "}
              <span className="font-semibold tabular-nums text-[--color-foreground]">
                {committed.inserted.toLocaleString("ko-KR")}건
              </span>
              , 이미 있던 행{" "}
              <span className="tabular-nums">
                {committed.unchanged.toLocaleString("ko-KR")}건
              </span>
            </p>
            {(committed.ambiguous > 0 || committed.unmatched > 0) && (
              <p className="mt-2 text-sm text-[--color-warning]">
                직접 확인할 행이 {committed.ambiguous + committed.unmatched}건 있습니다.
              </p>
            )}
          </div>
          <Button onClick={() => router.push("/settlements/marketplace")}>
            정산 내역으로 이동
          </Button>
        </div>
      )}
    </div>
  );
}
