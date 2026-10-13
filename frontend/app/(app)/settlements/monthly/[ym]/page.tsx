"use client";

/**
 * 월 마감 상세 — 검증 → 검토 → 확정 → 잠금 → 재오픈 요청.
 *
 * 화면이 지키는 것
 * -------------
 * 1. **확정된 달의 숫자는 snapshot 에서 읽는다.** 실시간 재집계로 다른 값을
 *    보여주면 "7월 확정 순이익" 이 매일 달라진다.
 * 2. **경고를 사용자가 직접 확인해야 확정된다.** 체크박스로 하나씩 본다 —
 *    "전부 무시" 버튼 하나면 아무도 읽지 않는다.
 * 3. **정산 미연결을 "이상 없음" 으로 보이게 하지 않는다.**
 * 4. **재오픈 버튼이 달을 바로 열지 않는다.** 승인 요청을 만들 뿐이다.
 * 5. 실패 응답을 "오류가 발생했습니다" 로 뭉개지 않는다 — 차단 항목·경고·
 *    잠금·충돌을 각각 다르게 안내한다.
 */

import { useMemo, useState } from "react";
import { use } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle,
  CircleAlert,
  Eye,
  History,
  Info,
  Lock,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { MoneyText, ProfitText } from "@/components/ui/finance";
import { ApiError } from "@/lib/api";
import {
  monthlyCloseApi,
  type CheckGrade,
  type CloseSnapshot,
  type ValidationItem,
} from "@/lib/api/settlements";

function fmtYm(ym: string) {
  const [y, m] = ym.split("-");
  return `${y}년 ${parseInt(m)}월`;
}

const GRADE_ICON: Record<CheckGrade, typeof Info> = {
  BLOCKING: CircleAlert,
  WARNING: AlertTriangle,
  INFO: Info,
};

const GRADE_LABEL: Record<CheckGrade, string> = {
  BLOCKING: "차단",
  WARNING: "경고",
  INFO: "참고",
};

/**
 * 실패 응답을 사용자가 다음에 무엇을 해야 할지 알 수 있게 번역한다.
 * "오류가 발생했습니다" 만 띄우면 사용자는 같은 버튼을 계속 누른다.
 */
function closeErrorMessage(err: unknown, action: string): string {
  if (!(err instanceof ApiError)) {
    return `${action} 중 통신에 실패했습니다. 네트워크를 확인하고 다시 시도하세요.`;
  }
  const d = err.details as Record<string, unknown> | undefined;
  if (Array.isArray(d?.blocking) && d.blocking.length) {
    return `차단 항목 ${d.blocking.length}건을 먼저 처리해야 ${action}할 수 있습니다.`;
  }
  if (Array.isArray(d?.unacknowledged) && d.unacknowledged.length) {
    return `확인하지 않은 경고 ${d.unacknowledged.length}건이 있습니다. 목록에서 체크한 뒤 다시 시도하세요.`;
  }
  if (Array.isArray(d?.changed_fields) && d.changed_fields.length) {
    return `확정 이후 데이터가 변경되었습니다(${d.changed_fields.join(", ")}). 재오픈 후 다시 확정하세요.`;
  }
  switch (err.status) {
    case 403:
      return `${action} 권한이 없습니다. 관리자에게 문의하세요.`;
    case 404:
      return "이 달의 정산이 아직 생성되지 않았습니다. 월별 정산에서 먼저 계산하세요.";
    case 409:
      return err.message || `다른 요청이 상태를 먼저 바꿨습니다. 새로고침 후 다시 시도하세요.`;
    default:
      return err.message || `${action}에 실패했습니다.`;
  }
}

export default function MonthCloseDetailPage({
  params,
}: {
  params: Promise<{ ym: string }>;
}) {
  const { ym } = use(params);
  const router = useRouter();
  const qc = useQueryClient();

  const [acked, setAcked] = useState<Set<string>>(new Set());
  const [reopenOpen, setReopenOpen] = useState(false);
  const [reopenReason, setReopenReason] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);

  const { data: month, isLoading } = useQuery({
    queryKey: ["monthly-close", ym],
    queryFn: () => monthlyCloseApi.get(ym),
  });

  const { data: quota } = useQuery({
    queryKey: ["monthly-close", ym, "quota"],
    queryFn: () => monthlyCloseApi.reopenQuota(ym),
    enabled: month?.close_state === "LOCKED",
  });

  const { data: drift } = useQuery({
    queryKey: ["monthly-close", ym, "drift"],
    queryFn: () => monthlyCloseApi.drift(ym),
    enabled: month?.close_state === "CONFIRMED",
  });

  const { data: generations } = useQuery({
    queryKey: ["monthly-close", ym, "snapshots"],
    queryFn: () => monthlyCloseApi.snapshots(ym),
    enabled: historyOpen,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["monthly-close"] });
  };

  const validate = useMutation({
    mutationFn: () => monthlyCloseApi.validate(ym),
    onSuccess: (res) => {
      invalidate();
      const blocking = res.items.filter((i) => i.grade === "BLOCKING").length;
      toast.success(
        blocking
          ? `검증 완료 — 차단 ${blocking}건을 먼저 처리하세요.`
          : `검증 완료 — 차단 항목이 없습니다.`,
      );
    },
    onError: (e) => toast.error(closeErrorMessage(e, "검증")),
  });

  const requestReview = useMutation({
    mutationFn: () => monthlyCloseApi.requestReview(ym),
    onSuccess: () => {
      invalidate();
      toast.success("검토 요청했습니다.");
    },
    onError: (e) => toast.error(closeErrorMessage(e, "검토 요청")),
  });

  const confirm = useMutation({
    mutationFn: () => monthlyCloseApi.confirm(ym, [...acked]),
    onSuccess: () => {
      invalidate();
      toast.success("월을 확정했습니다. 이 시점 숫자가 박제되었습니다.");
    },
    onError: (e) => toast.error(closeErrorMessage(e, "확정")),
  });

  const lock = useMutation({
    mutationFn: () => monthlyCloseApi.lock(ym),
    onSuccess: () => {
      invalidate();
      toast.success("월을 잠갔습니다. 이후 판매·비용을 바꿀 수 없습니다.");
    },
    onError: (e) => toast.error(closeErrorMessage(e, "잠금")),
  });

  const reopen = useMutation({
    mutationFn: (reason: string) => monthlyCloseApi.requestReopen(ym, reason),
    onSuccess: (req) => {
      invalidate();
      setReopenOpen(false);
      setReopenReason("");
      // 버튼을 눌렀다고 달이 열리지 않았다는 사실을 분명히 말한다.
      toast.success(
        `재오픈 승인 요청(${req.business_key})을 만들었습니다. 관리자 승인 후 열립니다.`,
      );
    },
    onError: (e) => toast.error(closeErrorMessage(e, "재오픈 요청")),
  });

  const items: ValidationItem[] = useMemo(
    () => month?.validation_result ?? [],
    [month],
  );
  const blocking = items.filter((i) => i.grade === "BLOCKING");
  const warnings = items.filter((i) => i.grade === "WARNING");
  const infos = items.filter((i) => i.grade === "INFO");
  const allWarningsAcked = warnings.every((w) => acked.has(w.code));

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중...</p>
      </div>
    );
  }
  if (!month) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-sm text-[--color-muted-foreground]">
          이 달의 마감 정보를 찾을 수 없습니다.
        </p>
      </div>
    );
  }

  const state = month.close_state;
  const snap = month.snapshot;
  const canValidate = state === "OPEN" || state === "REVIEW";
  const canConfirm = (state === "OPEN" || state === "REVIEW") && !blocking.length;
  const canLock = state === "CONFIRMED";
  const reopenExhausted = quota ? quota.reopen_count >= quota.limit : false;

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-6">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="ghost" size="sm" onClick={() => router.back()}>
          <ArrowLeft className="mr-1 h-4 w-4" aria-hidden />
          목록
        </Button>
        <h1 className="text-xl font-semibold">{fmtYm(ym)} 마감</h1>
        <span className="rounded-full border border-[--color-border] px-2.5 py-0.5 text-xs">
          {state}
          {month.close_generation ? ` · ${month.close_generation}세대` : ""}
        </span>
      </div>

      {/* 확정 이후 데이터가 바뀌었으면 잠글 수 없다. 먼저 알려준다. */}
      {drift?.drifted ? (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-lg border border-[--color-danger] bg-[--color-danger-bg] px-4 py-3 text-sm"
        >
          <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <div>
            <p className="font-medium">확정 이후 이 달의 데이터가 변경되었습니다.</p>
            <p className="mt-0.5 text-[--color-muted-foreground]">
              변경된 항목: {drift.changed_fields.join(", ") || "알 수 없음"}. 이 상태로는
              잠글 수 없습니다. 재오픈 후 다시 확정하세요.
            </p>
          </div>
        </div>
      ) : null}

      {/* 확정 숫자 — snapshot 이 있으면 그것이 진실이다 */}
      <section className="rounded-xl border border-[--color-border] bg-[--color-card] p-5">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-[--color-muted-foreground]">
            {month.profit_label}
          </h2>
          <button
            type="button"
            onClick={() => setHistoryOpen(true)}
            className="inline-flex items-center gap-1 text-xs text-[--color-muted-foreground] underline"
          >
            <History className="h-3.5 w-3.5" aria-hidden />
            확정 이력
          </button>
        </div>

        {snap ? <SnapshotFigures snap={snap} /> : <LiveFigures month={month} />}
      </section>

      {/* 마감 검증 */}
      <section className="rounded-xl border border-[--color-border] bg-[--color-card] p-5">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-[--color-muted-foreground]">
            마감 검증
          </h2>
          <div className="flex items-center gap-2">
            {month.validated_at ? (
              <span className="text-xs text-[--color-muted-foreground]">
                마지막 검증 {month.validated_at.slice(0, 16).replace("T", " ")}
              </span>
            ) : null}
            <Button
              size="sm"
              variant="outline"
              onClick={() => validate.mutate()}
              disabled={validate.isPending || !canValidate}
            >
              <RefreshCw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              검증 실행
            </Button>
          </div>
        </div>

        {items.length === 0 ? (
          <p className="text-sm text-[--color-muted-foreground]">
            아직 검증하지 않았습니다. 확정 전에 실행하세요.
          </p>
        ) : (
          <div className="space-y-4">
            {blocking.length > 0 ? (
              <div role="alert" aria-label="차단 항목 요약">
                <p className="mb-2 text-xs font-semibold text-[--color-danger]">
                  차단 {blocking.length}건 — 처리해야 확정할 수 있습니다
                </p>
                <ul className="space-y-2">
                  {blocking.map((i) => (
                    <CheckRow key={i.code} item={i} />
                  ))}
                </ul>
              </div>
            ) : null}

            {warnings.length > 0 ? (
              <div>
                <p className="mb-2 text-xs font-semibold text-[--color-warning]">
                  경고 {warnings.length}건 — 확인해야 확정할 수 있습니다
                  {" "}
                  ({acked.size}/{warnings.length} 확인함)
                </p>
                <ul className="space-y-2">
                  {warnings.map((i) => (
                    <CheckRow
                      key={i.code}
                      item={i}
                      checked={acked.has(i.code)}
                      onToggle={() =>
                        setAcked((prev) => {
                          const next = new Set(prev);
                          if (next.has(i.code)) next.delete(i.code);
                          else next.add(i.code);
                          return next;
                        })
                      }
                    />
                  ))}
                </ul>
              </div>
            ) : null}

            {infos.length > 0 ? (
              <div>
                <p className="mb-2 text-xs font-semibold text-[--color-muted-foreground]">
                  참고 {infos.length}건
                </p>
                <ul className="space-y-2">
                  {infos.map((i) => (
                    <CheckRow key={i.code} item={i} />
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        )}
      </section>

      {/* 액션 */}
      <div className="flex flex-wrap items-center gap-2 border-t border-[--color-border] pt-4">
        {canValidate ? (
          <>
            {state === "OPEN" ? (
              <Button
                size="sm"
                variant="outline"
                onClick={() => requestReview.mutate()}
                disabled={requestReview.isPending}
              >
                <Eye className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                검토 요청
              </Button>
            ) : null}
            <Button
              size="sm"
              onClick={() => confirm.mutate()}
              disabled={
                confirm.isPending ||
                !canConfirm ||
                items.length === 0 ||
                !allWarningsAcked
              }
            >
              <CheckCircle className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              확정
            </Button>
            {items.length === 0 ? (
              <span className="text-xs text-[--color-muted-foreground]">
                검증을 먼저 실행하세요.
              </span>
            ) : !canConfirm ? (
              <span className="text-xs text-[--color-danger]">
                차단 {blocking.length}건을 먼저 처리하세요.
              </span>
            ) : !allWarningsAcked ? (
              <span className="text-xs text-[--color-warning]">
                경고 {warnings.length - acked.size}건을 확인하세요.
              </span>
            ) : null}
          </>
        ) : null}

        {canLock ? (
          <Button
            size="sm"
            onClick={() => lock.mutate()}
            disabled={lock.isPending || drift?.drifted}
          >
            <Lock className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            잠금
          </Button>
        ) : null}

        {state === "LOCKED" ? (
          <>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setReopenOpen(true)}
              disabled={reopenExhausted}
            >
              <RotateCcw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              재오픈 승인 요청
            </Button>
            <span className="text-xs text-[--color-muted-foreground]">
              {quota
                ? reopenExhausted
                  ? `재개 한도(${quota.limit}회)를 모두 사용했습니다. 관리자에게 문의하세요.`
                  : `재개 ${quota.reopen_count}/${quota.limit}회 사용`
                : ""}
            </span>
          </>
        ) : null}
      </div>

      {/* 재오픈 요청 */}
      <Dialog open={reopenOpen} onOpenChange={setReopenOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>재오픈 승인 요청</DialogTitle>
          </DialogHeader>
          <div className="space-y-3 py-2 text-sm">
            <div className="flex items-start gap-2 rounded-md border border-[--color-border] bg-[--color-surface-muted] px-3 py-2">
              <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
              <p className="text-[--color-muted-foreground]">
                이 버튼은 달을 바로 열지 않습니다. <strong>관리자 승인</strong>이
                있어야 열립니다. 재오픈하면 확정 숫자가 무효화되고 확정 배분도 함께
                무효화됩니다(기록은 남습니다).
              </p>
            </div>
            <div className="space-y-1">
              <Label htmlFor="reopen-reason" className="text-xs">
                재오픈 사유 * (5자 이상)
              </Label>
              <Textarea
                id="reopen-reason"
                value={reopenReason}
                onChange={(e) => setReopenReason(e.target.value)}
                placeholder="예: 쿠팡 정산서가 마감 후 도착해 반품 차감을 반영해야 함"
                className="h-24 resize-none text-sm"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setReopenOpen(false)}>
              취소
            </Button>
            <Button
              disabled={reopenReason.trim().length < 5 || reopen.isPending}
              onClick={() => reopen.mutate(reopenReason.trim())}
            >
              승인 요청
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 확정 이력 */}
      <Dialog open={historyOpen} onOpenChange={setHistoryOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{fmtYm(ym)} 확정 이력</DialogTitle>
          </DialogHeader>
          <div className="max-h-96 space-y-3 overflow-y-auto py-2">
            {!generations?.items.length ? (
              <p className="text-sm text-[--color-muted-foreground]">
                아직 확정한 적이 없습니다.
              </p>
            ) : (
              generations.items.map((g) => (
                <div
                  key={g.id}
                  className="rounded-md border border-[--color-border] px-3 py-2 text-sm"
                >
                  <div className="flex items-center justify-between">
                    <span className="font-medium">
                      {g.generation}세대 · {g.taken_at_state}
                      {g.superseded_at ? " (대체됨)" : " (현재)"}
                    </span>
                    <ProfitText value={g.net_profit} />
                  </div>
                  <p className="mt-1 text-xs text-[--color-muted-foreground]">
                    {g.taken_at.slice(0, 16).replace("T", " ")}
                    {g.reason ? ` · ${g.reason}` : ""}
                  </p>
                  <p className="mt-0.5 break-all font-mono text-[10px] text-[--color-muted-foreground]">
                    {g.content_hash.slice(0, 32)}…
                  </p>
                </div>
              ))
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/** 검증 항목 한 줄. 색만으로 등급을 구분하지 않고 아이콘 + 글자를 함께 쓴다. */
function CheckRow({
  item,
  checked,
  onToggle,
}: {
  item: ValidationItem;
  checked?: boolean;
  onToggle?: () => void;
}) {
  const Icon = GRADE_ICON[item.grade] ?? Info;
  return (
    <li className="flex items-start gap-2 rounded-md border border-[--color-border] px-3 py-2">
      {onToggle ? (
        <input
          type="checkbox"
          className="mt-1"
          checked={!!checked}
          onChange={onToggle}
          aria-label={`${item.message} 확인함`}
        />
      ) : (
        <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      )}
      <div className="min-w-0 flex-1">
        <p className="text-sm">
          <span className="mr-1.5 text-xs text-[--color-muted-foreground]">
            [{GRADE_LABEL[item.grade]}]
          </span>
          {item.message}
        </p>
        {item.hint ? (
          <p className="mt-0.5 text-xs text-[--color-muted-foreground]">{item.hint}</p>
        ) : null}
      </div>
    </li>
  );
}

function Figure({
  label,
  value,
  note,
}: {
  label: string;
  value: React.ReactNode;
  note?: string;
}) {
  return (
    <div>
      <p className="text-xs text-[--color-muted-foreground]">{label}</p>
      <div className="text-sm font-semibold tabular-nums">{value}</div>
      {note ? (
        <p className="text-[11px] text-[--color-muted-foreground]">{note}</p>
      ) : null}
    </div>
  );
}

function SnapshotFigures({ snap }: { snap: CloseSnapshot }) {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Figure label="총매출" value={<MoneyText value={snap.gross_sales} />}
          note={`주문 ${snap.order_count.toLocaleString("ko-KR")}건`} />
        <Figure label="상품 직접비" value={<MoneyText value={snap.total_direct_cost} />} />
        <Figure label="사업 공통비" value={<MoneyText value={snap.business_expenses} />} />
        <Figure label="순이익" value={<ProfitText value={snap.net_profit} />} />
      </div>

      <div className="grid grid-cols-2 gap-4 border-t border-[--color-border] pt-3 sm:grid-cols-4">
        <Figure label="원가" value={<MoneyText value={snap.cogs} />} />
        <Figure label="마켓 수수료" value={<MoneyText value={snap.marketplace_fees} />} />
        <Figure label="광고비" value={<MoneyText value={snap.advertising_cost} />} />
        <Figure label="반품비" value={<MoneyText value={snap.returns_cost} />} />
        <Figure label="급여(비용)" value={<MoneyText value={snap.labor_expense} />} />
        <Figure label="개인카드 사용분" value={<MoneyText value={snap.personal_card_expense} />}
          note="상환 대상" />
        <Figure label="이월 차감" value={<MoneyText value={snap.carryover_offset} />} />
        <Figure label="분배 가능" value={<ProfitText value={snap.distributable_profit} />} />
      </div>

      {/* 정산 미연결을 0원과 구분해서 보여준다 */}
      <div className="rounded-md border border-[--color-border] bg-[--color-surface-muted] px-3 py-2 text-xs">
        {snap.settlement_connected ? (
          <span>
            정산 {snap.settlement_record_count}건 연결 · 미매칭{" "}
            {snap.settlement_unmatched_count}건 · 확정 입금{" "}
            <MoneyText value={snap.confirmed_settlement_amount} />
          </span>
        ) : (
          <span className="text-[--color-warning]">
            쇼핑몰 정산 자료가 연결되지 않았습니다 — 판매·비용 기준 잠정 순이익입니다.
          </span>
        )}
      </div>

      <p className="text-[11px] text-[--color-muted-foreground]">
        {snap.source_cutoff_at.slice(0, 16).replace("T", " ")} 기준으로 박제됨 ·
        산식 {snap.calculation_version} · {snap.generation}세대
      </p>
    </div>
  );
}

function LiveFigures({
  month,
}: {
  month: { total_revenue: number; total_direct_cost: number; total_common_expense: number; net_profit: number; distributable_amount: number };
}) {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Figure label="총매출" value={<MoneyText value={month.total_revenue} />} />
        <Figure label="상품 직접비" value={<MoneyText value={month.total_direct_cost} />} />
        <Figure label="사업 공통비" value={<MoneyText value={month.total_common_expense} />} />
        <Figure label="순이익" value={<ProfitText value={month.net_profit} />} />
      </div>
      <p className="text-xs text-[--color-muted-foreground]">
        아직 확정하지 않은 달입니다. 이 숫자는 지금 시점의 집계이며 데이터가
        추가되면 달라집니다.
      </p>
    </div>
  );
}
