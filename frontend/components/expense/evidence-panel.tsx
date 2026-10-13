"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle, Download, FileText, Image as ImageIcon, Loader2,
  Lock, Paperclip, ShieldCheck, Trash2, Upload, X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api";
import { approvalsApi, describeApprovalError } from "@/lib/api/approvals";
import {
  ALLOWED_MIME, ATTACHMENT_STATUS_LABEL, SCAN_STATUS_LABEL,
  attachmentsApi, precheckFile, type ExpenseAttachment,
} from "@/lib/api/attachments";
import { formatDateTime, formatFileSize } from "@/lib/format";

/** 업로드 진행 중인 파일. 서버 행이 생기기 전까지만 존재한다. */
interface InFlight {
  key: string;
  filename: string;
  size: number;
  percent: number;
  error?: string;
}

/**
 * 증빙 파일 패널.
 *
 * **브라우저 메모리에 파일을 올려두지 않는다.** 업로드는 곧바로 서버로 가고,
 * 목록은 DB 를 읽는다. 새로고침하면 사라지는 첨부는 증빙이 아니다.
 *
 * 삭제가 세 갈래인 이유
 * -------------------
 *   · 잠긴 달       → 아예 막는다. 확정된 달의 근거가 사라지면 안 된다
 *   · 승인된 비용   → 삭제 **승인 요청**을 만든다
 *   · 그 외         → 바로 삭제 요청(그래도 파일은 worker 가 지운다)
 * 무엇이 적용될지는 **서버가 알려준다**(`delete_requires_approval`) —
 * 화면이 추측하면 눌러 보고 403 을 맞는다.
 */
export function EvidencePanel({
  expenseId, locked, canWrite = true,
}: {
  expenseId: string;
  /** 잠긴 달이면 추가만 되고 삭제는 막힌다 */
  locked: boolean;
  canWrite?: boolean;
}) {
  const qc = useQueryClient();
  const fileRef = React.useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = React.useState(false);
  const [inFlight, setInFlight] = React.useState<InFlight[]>([]);
  const [deleting, setDeleting] = React.useState<ExpenseAttachment | null>(null);
  const [reason, setReason] = React.useState("");

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["expense-attachments", expenseId],
    queryFn: () => attachmentsApi.list(expenseId),
    enabled: !!expenseId,
  });

  const items = React.useMemo(() => data?.items ?? [], [data]);
  const maxCount = data?.max_attachments ?? 5;
  const maxBytes = data?.max_file_bytes ?? 10 * 1024 * 1024;
  const needsApproval = data?.delete_requires_approval ?? false;
  const scannerAvailable = data?.scanner_available ?? false;

  const remaining = Math.max(0, maxCount - items.length - inFlight.length);

  /**
   * 파일을 한 건씩 올린다. **한 요청에 묶지 않는다** — 3번째가 실패했을 때
   * 앞의 2건을 어떻게 할지 HTTP 상태 하나로 표현할 수 없다.
   * 파일별로 성공·실패를 보여주고, 실패한 것만 다시 고르면 된다.
   */
  async function uploadFiles(files: File[]) {
    if (!canWrite || files.length === 0) return;

    const accepted: File[] = [];
    for (const file of files) {
      if (accepted.length >= remaining) {
        toast.error(`증빙은 최대 ${maxCount}개까지 첨부할 수 있습니다`, {
          description: `${file.name} 외 나머지는 올리지 않았습니다.`,
        });
        break;
      }
      const rejected = precheckFile(file, { maxBytes });
      if (rejected) {
        toast.error(`${rejected.filename} — ${rejected.reason}`);
        continue;
      }
      accepted.push(file);
    }

    for (const file of accepted) {
      const key = `${file.name}-${file.size}-${Date.now()}-${Math.random()}`;
      setInFlight((p) => [...p, { key, filename: file.name, size: file.size, percent: 0 }]);
      try {
        await attachmentsApi.upload(expenseId, file, (percent) => {
          setInFlight((p) => p.map((f) => (f.key === key ? { ...f, percent } : f)));
        });
        setInFlight((p) => p.filter((f) => f.key !== key));
        toast.success(`${file.name} 첨부 완료`);
      } catch (err) {
        // 실패한 항목은 목록에 남겨 **무엇이 왜 실패했는지** 보여준다.
        // 토스트만 띄우면 여러 개 올릴 때 어느 파일이 실패했는지 사라진다.
        const message = err instanceof ApiError ? err.message : "업로드하지 못했습니다";
        setInFlight((p) => p.map((f) => (f.key === key ? { ...f, error: message } : f)));
      }
    }
    qc.invalidateQueries({ queryKey: ["expense-attachments", expenseId] });
    qc.invalidateQueries({ queryKey: ["expenses"] });
    qc.invalidateQueries({ queryKey: ["expense", expenseId] });
  }

  const download = useMutation({
    mutationFn: (a: ExpenseAttachment) => attachmentsApi.download(a.id, a.filename),
    onError: (err) => {
      if (err instanceof ApiError && err.status === 403) {
        toast.error("증빙 원본을 열 권한이 없습니다", {
          description: "관리자에게 권한을 요청하세요.",
        });
        return;
      }
      toast.error(err instanceof ApiError ? err.message : "다운로드하지 못했습니다");
    },
  });

  const removeDirect = useMutation({
    mutationFn: (a: ExpenseAttachment) => attachmentsApi.remove(a.id),
    onSuccess: () => {
      toast.success("삭제 요청했습니다", {
        description: "파일은 잠시 뒤 저장소에서 제거됩니다.",
      });
      setDeleting(null);
      qc.invalidateQueries({ queryKey: ["expense-attachments", expenseId] });
    },
    onError: (err) => {
      const g = describeApprovalError(err);
      toast.error(g.title, { description: g.description });
      if (g.shouldRefetch) refetch();
    },
  });

  const requestDeleteApproval = useMutation({
    mutationFn: (a: ExpenseAttachment) =>
      approvalsApi.create({
        target_type: "expense_attachment",
        target_id: a.id,
        request_type: "EXPENSE_EVIDENCE_CHANGE",
        proposed_changes: { status: "PENDING_DELETE" },
        reason: reason.trim() || undefined,
        submit: true,
      }),
    onSuccess: () => {
      toast.success("증빙 삭제 승인을 요청했습니다", {
        description: "승인이 적용되면 파일이 제거됩니다. 그전까지는 그대로 유지됩니다.",
      });
      setDeleting(null);
      setReason("");
      qc.invalidateQueries({ queryKey: ["expense-attachments", expenseId] });
      qc.invalidateQueries({ queryKey: ["approvals"] });
    },
    onError: (err) => {
      const g = describeApprovalError(err);
      toast.error(g.title, { description: g.description });
    },
  });

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    if (locked && !canWrite) return;
    uploadFiles(Array.from(e.dataTransfer.files));
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <p className="text-xs font-medium">
          증빙 파일
          <span className="ml-1.5 text-[10px] font-normal text-[--color-muted-foreground]">
            {items.length}/{maxCount} · jpg·png·heic·pdf · {formatFileSize(maxBytes)} 이하
          </span>
        </p>
        {canWrite && (
          <Button
            variant="outline" size="xs"
            onClick={() => fileRef.current?.click()}
            disabled={remaining === 0}
          >
            <Upload className="mr-1 h-3 w-3" aria-hidden />
            파일 선택
          </Button>
        )}
        <input
          ref={fileRef} type="file" multiple className="hidden"
          accept={ALLOWED_MIME.join(",")}
          onChange={(e) => {
            uploadFiles(Array.from(e.target.files ?? []));
            e.target.value = "";   // 같은 파일을 다시 고를 수 있게
          }}
        />
      </div>

      {/* 드롭 영역 겸 목록 */}
      <div
        onDragOver={(e) => { e.preventDefault(); if (canWrite) setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={cn(
          "rounded-md border border-dashed border-[--color-border] transition-colors",
          dragging && "border-[--color-accent] bg-[--color-accent-bg]/40",
          items.length === 0 && inFlight.length === 0 ? "p-4" : "p-2",
        )}
      >
        {isLoading ? (
          <p className="py-2 text-center text-[11px] text-[--color-muted-foreground]">
            불러오는 중…
          </p>
        ) : isError ? (
          <div className="py-2 text-center text-[11px]">
            <p className="text-[--color-danger]">증빙 목록을 불러오지 못했습니다</p>
            <Button variant="outline" size="xs" className="mt-1.5" onClick={() => refetch()}>
              다시 시도
            </Button>
          </div>
        ) : items.length === 0 && inFlight.length === 0 ? (
          <p className="text-center text-[11px] text-[--color-muted-foreground]">
            {canWrite
              ? "여기에 파일을 끌어다 놓거나 파일 선택을 누르세요"
              : "첨부된 증빙이 없습니다"}
            <br />
            증빙을 첨부하면 세무 리포트의 &quot;증빙 누락&quot; 집계에서 빠집니다
          </p>
        ) : (
          <ul className="space-y-1">
            {items.map((a) => (
              <li
                key={a.id}
                className="flex items-center gap-2 rounded-md border border-[--color-border] bg-[--color-card] p-2"
              >
                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded bg-[--color-muted]">
                  {a.mime_type?.startsWith("image/") ? (
                    <ImageIcon className="h-4 w-4 text-[--color-muted-foreground]" aria-hidden />
                  ) : (
                    <FileText className="h-4 w-4 text-[--color-muted-foreground]" aria-hidden />
                  )}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="flex items-center gap-1.5 truncate text-[11px] font-medium">
                    <span className="truncate">{a.filename}</span>
                    {a.status === "PENDING_DELETE" && (
                      <Badge variant="warning" className="shrink-0 px-1 py-0 text-[9px]">
                        {ATTACHMENT_STATUS_LABEL[a.status]}
                      </Badge>
                    )}
                  </p>
                  <p className="truncate text-[10px] text-[--color-muted-foreground]">
                    {formatFileSize(a.size_bytes ?? 0)}
                    {a.uploaded_by_name && ` · ${a.uploaded_by_name}`}
                    {a.uploaded_at && ` · ${formatDateTime(a.uploaded_at)}`}
                    {!scannerAvailable && ` · ${SCAN_STATUS_LABEL[a.scan_status]}`}
                  </p>
                  {a.delete_last_error && (
                    <p className="text-[10px] text-[--color-danger]">
                      삭제 재시도 {a.delete_attempt_count}회 — {a.delete_last_error}
                    </p>
                  )}
                </div>

                <Button
                  variant="ghost" size="icon-sm" title="다운로드"
                  onClick={() => download.mutate(a)}
                  disabled={download.isPending}
                >
                  <Download className="h-3 w-3" aria-hidden />
                  <span className="sr-only">{a.filename} 다운로드</span>
                </Button>
                {canWrite && a.status !== "PENDING_DELETE" && (
                  <Button
                    variant="ghost" size="icon-sm"
                    onClick={() => { setDeleting(a); setReason(""); }}
                    aria-label={`${a.filename} 삭제`}
                  >
                    <Trash2 className="h-3 w-3 text-[--color-danger]" aria-hidden />
                  </Button>
                )}
              </li>
            ))}

            {/* 업로드 중 · 실패 */}
            {inFlight.map((f) => (
              <li
                key={f.key}
                className="flex items-center gap-2 rounded-md border border-[--color-border] bg-[--color-card] p-2"
              >
                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded bg-[--color-muted]">
                  {f.error
                    ? <AlertTriangle className="h-4 w-4 text-[--color-danger]" aria-hidden />
                    : <Loader2 className="h-4 w-4 animate-spin text-[--color-muted-foreground]" aria-hidden />}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[11px] font-medium">{f.filename}</p>
                  {f.error ? (
                    <p className="text-[10px] text-[--color-danger]">{f.error}</p>
                  ) : (
                    <div
                      className="mt-1 h-1 w-full overflow-hidden rounded-full bg-[--color-muted]"
                      role="progressbar" aria-valuenow={f.percent}
                      aria-valuemin={0} aria-valuemax={100}
                      aria-label={`${f.filename} 업로드`}
                    >
                      <div
                        className="h-full bg-[--color-accent] transition-[width]"
                        style={{ width: `${f.percent}%` }}
                      />
                    </div>
                  )}
                </div>
                {f.error && (
                  <Button
                    variant="ghost" size="icon-sm"
                    onClick={() => setInFlight((p) => p.filter((x) => x.key !== f.key))}
                    aria-label={`${f.filename} 목록에서 지우기`}
                  >
                    <X className="h-3 w-3" aria-hidden />
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      {locked && (
        <p className="flex items-start gap-1.5 text-[10px] text-[--color-muted-foreground]">
          <Lock className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
          잠긴 월이지만 <strong className="mx-0.5">증빙 추가는 가능합니다</strong> —
          증빙은 금액을 바꾸지 않습니다. 기존 증빙 제거만 막힙니다.
        </p>
      )}

      {/* 삭제 — 상황에 따라 다른 것을 보여준다 */}
      {deleting && (
        <div className="rounded-md border border-[--color-border] bg-[--color-muted] p-3 text-[11px]">
          <p className="flex items-center gap-1.5 font-medium">
            <Paperclip className="h-3.5 w-3.5 shrink-0" aria-hidden />
            {deleting.filename}
          </p>

          {locked ? (
            <>
              <p className="mt-1.5">
                잠긴 월의 증빙은 직접 제거할 수 없습니다.
                필요한 경우 월 재오픈 절차가 필요합니다.
              </p>
              {/* 월 재오픈 승인은 아직 없다. 동작하지 않는 버튼을 두지 않는다. */}
              <p className="mt-1 text-[--color-muted-foreground]">
                월 재오픈 승인 기능은 아직 제공되지 않습니다. 관리자에게 문의하세요.
              </p>
              <div className="mt-2 flex justify-end">
                <Button variant="outline" size="xs" onClick={() => setDeleting(null)}>
                  닫기
                </Button>
              </div>
            </>
          ) : needsApproval ? (
            <>
              <p className="mt-1.5 flex items-center gap-1.5">
                <ShieldCheck className="h-3.5 w-3.5 shrink-0" aria-hidden />
                이 증빙을 삭제하려면 승인이 필요합니다.
              </p>
              <p className="mt-1 text-[--color-muted-foreground]">
                이미 승인을 거친 비용입니다. 승인이 적용되기 전까지 파일은 그대로 유지됩니다.
              </p>
              <label className="mt-2 block" htmlFor="att-reason">
                <span className="mb-1 block font-medium">삭제 사유</span>
                <input
                  id="att-reason" value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="예: 잘못 첨부한 다른 건의 영수증"
                  className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
                />
              </label>
              <div className="mt-2 flex justify-end gap-2">
                <Button variant="outline" size="xs" onClick={() => setDeleting(null)}>
                  취소
                </Button>
                <Button
                  size="xs"
                  onClick={() => requestDeleteApproval.mutate(deleting)}
                  disabled={requestDeleteApproval.isPending}
                >
                  {requestDeleteApproval.isPending && (
                    <Loader2 className="mr-1 h-3 w-3 animate-spin" aria-hidden />
                  )}
                  삭제 승인 요청
                </Button>
              </div>
            </>
          ) : (
            <>
              <p className="mt-1.5">
                이 증빙을 삭제할까요? 파일은 저장소에서 제거되며 되돌릴 수 없습니다.
              </p>
              <div className="mt-2 flex justify-end gap-2">
                <Button variant="outline" size="xs" onClick={() => setDeleting(null)}>
                  취소
                </Button>
                <Button
                  size="xs" onClick={() => removeDirect.mutate(deleting)}
                  disabled={removeDirect.isPending}
                >
                  {removeDirect.isPending && (
                    <Loader2 className="mr-1 h-3 w-3 animate-spin" aria-hidden />
                  )}
                  삭제
                </Button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
