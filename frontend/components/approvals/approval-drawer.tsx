"use client";

import * as React from "react";
import Link from "next/link";
import type { Route } from "next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle, ArrowRight, CheckCircle2, Clock, ExternalLink, Info, Loader2, X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { PageTabs, Field } from "@/components/shell/page-shell";
import { TextInput } from "@/components/ui/money-input";
import {
  approvalsApi, buildDiffRows, describeApprovalError,
  REQUEST_TYPE_LABELS, TARGET_TYPE_LABELS,
  type ApprovalStatus, type DiffRow,
} from "@/lib/api/approvals";
import { formatDate, formatDateTime, formatKRW } from "@/lib/format";
import { useExpenseRefs } from "@/hooks/useExpenseRefs";

export const STATUS_LABEL: Record<ApprovalStatus, string> = {
  DRAFT: "작성중", PENDING: "승인 대기", APPROVED: "승인됨",
  REJECTED: "반려", APPLIED: "적용 완료", CANCELLED: "취소됨",
};

const STATUS_TONE: Record<ApprovalStatus, "default" | "info" | "success" | "warning" | "danger"> = {
  DRAFT: "default", PENDING: "warning", APPROVED: "info",
  REJECTED: "danger", APPLIED: "success", CANCELLED: "default",
};

export function ApprovalStatusBadge({ status }: { status: ApprovalStatus }) {
  return (
    <Badge variant={STATUS_TONE[status]} className="px-1.5 py-0 text-[10px]">
      {STATUS_LABEL[status]}
    </Badge>
  );
}

/** 값 표시 — null 과 빈 문자열을 구분한다. 둘은 의미가 다르다. */
function DiffValue({
  value, kind, valueLabels,
}: { value: unknown; kind: DiffRow["kind"]; valueLabels?: Record<string, string> }) {
  if (value === null || value === undefined) {
    return <span className="text-[--color-muted-foreground]">없음</span>;
  }
  if (value === "") {
    return <span className="text-[--color-muted-foreground]">(빈 값)</span>;
  }
  // 승인자에게 `true` / `INCLUDED` 를 보여주면 무엇이 바뀌는지 읽을 수 없다.
  if (typeof value === "boolean") {
    return <span>{value ? "예" : "아니오"}</span>;
  }
  if (valueLabels && typeof value === "string" && valueLabels[value]) {
    return <span>{valueLabels[value]}</span>;
  }
  if (kind === "money" && typeof value === "number") {
    return <span className="tabular-nums">{formatKRW(value)}</span>;
  }
  if (kind === "date") {
    return <span className="tabular-nums">{formatDate(String(value))}</span>;
  }
  if (kind === "url") {
    const url = String(value);
    return (
      <a
        href={url} target="_blank" rel="noopener noreferrer"
        className="ops-focusable inline-flex max-w-[280px] items-center gap-1 truncate text-[--color-accent] hover:underline"
        title={url}
      >
        <span className="truncate">{url}</span>
        <ExternalLink className="h-3 w-3 shrink-0" aria-hidden />
      </a>
    );
  }
  return <span className="break-words">{String(value)}</span>;
}

function DiffTable({ rows, showAll }: { rows: DiffRow[]; showAll: boolean }) {
  const visible = showAll ? rows : rows.filter((r) => r.changed);
  if (visible.length === 0) {
    return (
      <p className="rounded-md border border-dashed border-[--color-border] p-4 text-center text-xs text-[--color-muted-foreground]">
        변경된 필드가 없습니다
      </p>
    );
  }
  return (
    <ul className="divide-y divide-[--color-border] overflow-hidden rounded-lg border border-[--color-border]">
      {visible.map((r) => (
        <li key={r.field} className={cn("px-3 py-2", r.changed && "bg-[--color-accent-bg]/30")}>
          <p className="mb-1 flex items-center gap-1.5 text-[11px] font-medium">
            {r.label}
            {r.changed && (
              <Badge variant="info" className="px-1 py-0 text-[9px]">변경</Badge>
            )}
          </p>
          <div className="grid gap-1 text-[12px] sm:grid-cols-[1fr_auto_1fr] sm:items-center">
            <span className={cn(r.changed && "text-[--color-muted-foreground] line-through")}>
              <DiffValue value={r.before} kind={r.kind} valueLabels={r.valueLabels} />
            </span>
            <ArrowRight
              className={cn("hidden h-3 w-3 text-[--color-muted-foreground] sm:block",
                            !r.changed && "invisible")}
              aria-hidden
            />
            <span className={cn(r.changed && "font-medium")}>
              <DiffValue value={r.after} kind={r.kind} valueLabels={r.valueLabels} />
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}

type DialogKind = "approve" | "reject" | "cancel" | "apply" | null;

export function ApprovalDrawer({
  requestId,
  open,
  onOpenChange,
  currentUserId,
  canReview,
}: {
  requestId: string | null;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  currentUserId?: string;
  canReview: boolean;
}) {
  const qc = useQueryClient();
  const [tab, setTab] = React.useState("changes");
  const [showAll, setShowAll] = React.useState(false);
  const [dialog, setDialog] = React.useState<DialogKind>(null);
  const [comment, setComment] = React.useState("");

  const { data, isLoading, refetch } = useQuery({
    queryKey: ["approval", requestId],
    queryFn: () => approvalsApi.get(requestId!),
    enabled: !!requestId && open,
  });

  React.useEffect(() => {
    if (open) { setTab("changes"); setComment(""); setShowAll(false); }
  }, [open, requestId]);

  function handleError(err: unknown) {
    const guide = describeApprovalError(err);
    toast.error(guide.title, { description: guide.description });
    if (guide.shouldRefetch) {
      refetch();
      qc.invalidateQueries({ queryKey: ["approvals"] });
    }
  }

  function onDone(message: string) {
    toast.success(message);
    setDialog(null);
    setComment("");
    refetch();
    qc.invalidateQueries({ queryKey: ["approvals"] });
    qc.invalidateQueries({ queryKey: ["products"] });
  }

  const submitM = useMutation({
    mutationFn: () => approvalsApi.submit(requestId!),
    onSuccess: () => onDone("승인 요청을 제출했습니다"), onError: handleError,
  });
  const approveM = useMutation({
    mutationFn: () => approvalsApi.approve(requestId!, comment || undefined),
    onSuccess: () => onDone("승인했습니다"), onError: handleError,
  });
  const rejectM = useMutation({
    mutationFn: () => approvalsApi.reject(requestId!, comment),
    onSuccess: () => onDone("반려했습니다"), onError: handleError,
  });
  const cancelM = useMutation({
    mutationFn: () => approvalsApi.cancel(requestId!, comment || undefined),
    onSuccess: () => onDone("요청을 취소했습니다"), onError: handleError,
  });
  const applyM = useMutation({
    mutationFn: () => approvalsApi.apply(requestId!),
    onSuccess: () => onDone("변경 내용을 적용했습니다"), onError: handleError,
  });

  // 비용 승인 diff 는 카테고리·결제자를 UUID 로 들고 있다. 이름을 붙이지 않으면
  // 승인자가 보는 것은 `9d3f…` 다 — 그걸로는 승인 여부를 판단할 수 없다.
  const { resolve } = useExpenseRefs();

  const rows = React.useMemo(
    () => (data
      ? buildDiffRows(
        data.target_type, data.before_snapshot_json, data.proposed_changes_json, resolve,
      )
      : []),
    [data, resolve],
  );

  const isOwner = !!data && !!currentUserId && data.requested_by === currentUserId;
  const selfApproveBlocked =
    isOwner && !(data?.approval_policy_snapshot_json?.self_approve_allowed ?? false);
  const isFuture = !!data?.effective_from && data.effective_from > new Date().toISOString().slice(0, 10);

  return (
    <>
      <Sheet open={open} onOpenChange={onOpenChange}>
        <SheetContent
          side="right" showCloseButton={false}
          className="[--sheet-w:620px] flex w-full flex-col gap-0 p-0"
        >
          <SheetHeader className="shrink-0 gap-1 border-b border-[--color-border] px-4 py-3">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <SheetTitle className="truncate text-[15px]">
                  {isLoading
                    ? "불러오는 중…"
                    : `${data ? REQUEST_TYPE_LABELS[data.request_type] ?? data.request_type : ""} 승인 요청`}
                </SheetTitle>
                <SheetDescription className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px]">
                  {data && (
                    <>
                      <span className="font-mono">{data.business_key}</span>
                      <ApprovalStatusBadge status={data.status} />
                      {data.effective_from && (
                        <span className="text-[--color-muted-foreground]">
                          적용 예정 {formatDate(data.effective_from)}
                        </span>
                      )}
                      {data.applied_by_type === "SYSTEM" && (
                        <Badge variant="default" className="px-1 py-0 text-[9px]">자동 적용</Badge>
                      )}
                    </>
                  )}
                </SheetDescription>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                {data?.target_type === "product" && (
                  <Button variant="ghost" size="icon-sm" asChild title="대상 상품 보기">
                    <Link href={`/products?productId=${data.target_id}` as Route}>
                      <ExternalLink className="h-3.5 w-3.5" aria-hidden />
                      <span className="sr-only">대상 상품 보기</span>
                    </Link>
                  </Button>
                )}
                {data?.target_type === "expense" && (
                  <Button variant="ghost" size="icon-sm" asChild title="대상 비용 보기">
                    <Link href={`/expenses?expenseId=${data.target_id}` as Route}>
                      <ExternalLink className="h-3.5 w-3.5" aria-hidden />
                      <span className="sr-only">대상 비용 보기</span>
                    </Link>
                  </Button>
                )}
                <Button variant="ghost" size="icon-sm" onClick={() => onOpenChange(false)} aria-label="닫기">
                  <X className="h-4 w-4" aria-hidden />
                </Button>
              </div>
            </div>
            <div className="-mb-3 mt-1">
              <PageTabs
                tabs={[
                  { key: "changes", label: "변경 내용" },
                  { key: "request", label: "요청 정보" },
                  { key: "history", label: "처리 이력" },
                ]}
                value={tab}
                onChange={setTab}
              />
            </div>
          </SheetHeader>

          <div className="flex-1 space-y-3 overflow-y-auto p-4">
            {isLoading || !data ? (
              <p className="py-8 text-center text-xs text-[--color-muted-foreground]">불러오는 중…</p>
            ) : (
              <>
                {/* 상태별 안내 */}
                {data.status === "APPROVED" && isFuture && (
                  <p className="flex items-start gap-1.5 rounded-md bg-[--color-info-bg] p-2 text-[11px] text-[--color-info]">
                    <Clock className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                    <span>
                      <strong>{formatDate(data.effective_from)}</strong> 에 자동으로 적용됩니다.
                      지금 바로 반영하려면 아래 &quot;지금 적용&quot; 을 누르세요.
                    </span>
                  </p>
                )}
                {data.auto_apply_status === "HELD_BY_LOCK" && (
                  <p className="flex items-start gap-1.5 rounded-md bg-[--color-warning-bg] p-2 text-[11px] text-[--color-warning]">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                    <span>
                      적용일이 속한 월이 <strong>잠겨 있어 보류</strong>되었습니다.
                      관리자가 월 잠금을 해제하면 다시 적용됩니다. 승인 자체는 유효합니다.
                    </span>
                  </p>
                )}
                {data.auto_apply_status === "PERMANENT_FAILURE" && data.last_error_message && (
                  <p className="flex items-start gap-1.5 rounded-md bg-[--color-danger-bg] p-2 text-[11px] text-[--color-danger]">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                    <span>
                      자동 적용에 실패했습니다 ({data.attempt_count}회 시도). {data.last_error_message}
                    </span>
                  </p>
                )}
                {data.status === "REJECTED" && (
                  <div className="rounded-md border border-[--color-danger-border] bg-[--color-danger-bg] p-3">
                    <p className="text-[11px] font-medium text-[--color-danger]">반려 사유</p>
                    <p className="mt-1 text-xs">{data.review_comment || "사유가 기록되지 않았습니다."}</p>
                  </div>
                )}

                {tab === "changes" && (
                  <>
                    <div className="flex items-center justify-between">
                      <p className="text-xs font-semibold">변경 전후</p>
                      <label className="flex items-center gap-1.5 text-[11px] text-[--color-muted-foreground]">
                        <input
                          type="checkbox" className="ops-focusable"
                          checked={showAll} onChange={(e) => setShowAll(e.target.checked)}
                        />
                        전체 필드 보기
                      </label>
                    </div>
                    <DiffTable rows={rows} showAll={showAll} />
                  </>
                )}

                {tab === "request" && (
                  <dl className="divide-y divide-[--color-border] overflow-hidden rounded-lg border border-[--color-border] text-[12px]">
                    {[
                      ["요청 유형", REQUEST_TYPE_LABELS[data.request_type] ?? data.request_type],
                      ["대상 유형", TARGET_TYPE_LABELS[data.target_type] ?? data.target_type],
                      ["요청 사유", data.reason || "-"],
                      ["적용 예정일", data.effective_from ? formatDate(data.effective_from) : "승인 즉시"],
                      ["요청일", formatDateTime(data.requested_at)],
                      ["제출일", data.submitted_at ? formatDateTime(data.submitted_at) : "-"],
                    ].map(([k, v]) => (
                      <div key={k} className="flex items-start justify-between gap-3 px-3 py-2">
                        <dt className="shrink-0 text-[--color-muted-foreground]">{k}</dt>
                        <dd className="text-right">{v}</dd>
                      </div>
                    ))}
                  </dl>
                )}

                {tab === "history" && (
                  <ol className="space-y-1.5">
                    {[
                      { at: data.requested_at, label: "요청 생성", note: data.reason },
                      { at: data.submitted_at, label: "승인 요청 제출", note: null },
                      {
                        at: data.reviewed_at,
                        label: data.status === "REJECTED" ? "반려" : "승인",
                        note: data.review_comment,
                      },
                      {
                        at: data.applied_at,
                        label: `적용 완료${data.applied_by_type === "SYSTEM" ? " (자동)" : ""}`,
                        note: null,
                      },
                      { at: data.cancelled_at, label: "취소", note: null },
                    ]
                      .filter((e) => e.at)
                      .map((e, i) => (
                        <li key={i} className="rounded-md border border-[--color-border] px-3 py-2 text-xs">
                          <div className="flex items-center justify-between">
                            <span className="font-medium">{e.label}</span>
                            <span className="text-[10px] text-[--color-muted-foreground]">
                              {formatDateTime(e.at as string)}
                            </span>
                          </div>
                          {e.note && (
                            <p className="mt-1 text-[--color-muted-foreground]">{e.note}</p>
                          )}
                        </li>
                      ))}
                    {data.attempt_count > 0 && (
                      <li className="rounded-md border border-[--color-border] px-3 py-2 text-xs">
                        <span className="font-medium">자동 적용 시도 {data.attempt_count}회</span>
                        {data.last_attempt_at && (
                          <p className="mt-1 text-[10px] text-[--color-muted-foreground]">
                            마지막 {formatDateTime(data.last_attempt_at)}
                            {data.next_retry_at && ` · 다음 재시도 ${formatDateTime(data.next_retry_at)}`}
                          </p>
                        )}
                      </li>
                    )}
                  </ol>
                )}
              </>
            )}
          </div>

          {/* 상태·권한별 액션 */}
          {data && (
            <div className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-[--color-border] bg-[--color-muted] px-4 py-3">
              {data.status === "DRAFT" && isOwner && (
                <>
                  <Button variant="outline" size="sm" onClick={() => setDialog("cancel")}>취소</Button>
                  <Button size="sm" onClick={() => submitM.mutate()} disabled={submitM.isPending}>
                    {submitM.isPending && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />}
                    승인 요청 제출
                  </Button>
                </>
              )}
              {data.status === "PENDING" && (
                <>
                  {isOwner && (
                    <Button variant="outline" size="sm" onClick={() => setDialog("cancel")}>취소</Button>
                  )}
                  {canReview && (
                    <>
                      <Button variant="outline" size="sm" onClick={() => setDialog("reject")}>반려</Button>
                      <Button
                        size="sm" onClick={() => setDialog("approve")}
                        disabled={selfApproveBlocked}
                        title={selfApproveBlocked ? "본인 요청은 승인할 수 없습니다" : undefined}
                      >
                        승인
                      </Button>
                    </>
                  )}
                </>
              )}
              {data.status === "APPROVED" && (
                <>
                  <Button variant="outline" size="sm" onClick={() => setDialog("cancel")}>취소</Button>
                  {canReview && (
                    <Button size="sm" onClick={() => setDialog("apply")}>지금 적용</Button>
                  )}
                </>
              )}
              {data.status === "APPLIED" && data.target_type === "product" && (
                <Button variant="outline" size="sm" asChild>
                  <Link href={`/products?q=${data.target_id}` as Route}>대상 상품 보기</Link>
                </Button>
              )}
              {data.status === "REJECTED" && (
                <span className="text-[11px] text-[--color-muted-foreground]">
                  수정하려면 새 승인 요청을 만드세요
                </span>
              )}
              {selfApproveBlocked && data.status === "PENDING" && (
                <p className="w-full text-right text-[10px] text-[--color-muted-foreground]">
                  본인이 요청한 건은 승인할 수 없습니다. 다른 승인자에게 검토를 요청하세요.
                </p>
              )}
            </div>
          )}
        </SheetContent>
      </Sheet>

      {/* 확인 dialog */}
      <Dialog open={dialog !== null} onOpenChange={(v) => !v && setDialog(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>
              {dialog === "approve" && "이 변경을 승인할까요?"}
              {dialog === "reject" && "이 요청을 반려할까요?"}
              {dialog === "cancel" && "요청을 취소할까요?"}
              {dialog === "apply" && "지금 바로 적용할까요?"}
            </DialogTitle>
            <DialogDescription className="text-[12px]">
              {dialog === "approve" && data?.effective_from && (
                <>적용 예정일: <strong>{formatDate(data.effective_from)}</strong>. 그날 자동으로 반영됩니다.</>
              )}
              {dialog === "approve" && !data?.effective_from &&
                "승인 후 별도로 적용해야 대상 데이터가 바뀝니다."}
              {dialog === "reject" && "반려 사유는 요청자에게 그대로 표시됩니다."}
              {dialog === "cancel" && isFuture &&
                "적용 예정이던 승인이 취소됩니다. 예정된 자동 적용도 실행되지 않습니다."}
              {dialog === "cancel" && !isFuture && "진행 중인 요청을 철회합니다."}
              {dialog === "apply" &&
                "대상 데이터가 즉시 변경됩니다. 적용일이 속한 월이 잠겨 있으면 적용되지 않습니다."}
            </DialogDescription>
          </DialogHeader>

          {(dialog === "approve" || dialog === "reject" || dialog === "cancel") && (
            <Field
              label={dialog === "reject" ? "반려 사유 (필수)" : "메모"}
              htmlFor="ap-comment"
              hint={dialog === "reject" ? "2자 이상 입력하세요" : undefined}
            >
              <TextInput
                id="ap-comment" value={comment}
                onChange={(e) => setComment(e.target.value)}
                placeholder={dialog === "reject" ? "예: 원가 근거 자료가 필요합니다" : "선택 사항"}
              />
            </Field>
          )}

          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setDialog(null)}>닫기</Button>
            {dialog === "approve" && (
              <Button size="sm" onClick={() => approveM.mutate()} disabled={approveM.isPending}>
                <CheckCircle2 className="mr-1.5 h-3.5 w-3.5" aria-hidden /> 승인
              </Button>
            )}
            {dialog === "reject" && (
              <Button
                size="sm" onClick={() => rejectM.mutate()}
                disabled={comment.trim().length < 2 || rejectM.isPending}
              >
                반려
              </Button>
            )}
            {dialog === "cancel" && (
              <Button size="sm" onClick={() => cancelM.mutate()} disabled={cancelM.isPending}>
                요청 취소
              </Button>
            )}
            {dialog === "apply" && (
              <Button size="sm" onClick={() => applyM.mutate()} disabled={applyM.isPending}>
                {applyM.isPending && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />}
                지금 적용
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

/** 임계값 미확정 안내 — 자동 판정이 없다는 사실을 숨기지 않는다. */
export function ThresholdNotice({ resolved }: { resolved: boolean }) {
  if (resolved) return null;
  return (
    <p className="flex items-start gap-1.5 rounded-md bg-[--color-info-bg] p-2 text-[11px] text-[--color-info]">
      <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
      <span>
        자동 승인 판정 기준이 아직 설정되지 않았습니다. 현재는 승인 요청 여부를 직접 선택합니다.
      </span>
    </p>
  );
}
