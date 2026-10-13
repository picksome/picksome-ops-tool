"use client";

/**
 * 운영 이슈 센터 (Phase 36).
 *
 * 대시보드 경고가 "떠 있기만 하는 것" 에서 "누가 언제까지 처리하는 일" 이 된다.
 *
 * 화면이 지키는 것
 * -------------
 * · 상태를 **색만으로** 구분하지 않는다. 아이콘 + 글자를 함께 쓴다
 * · 무시(IGNORED)에는 사유를 반드시 받는다 — 서버도 거부하지만 화면이 먼저 막는다
 * · 스캔 결과를 사람이 닫은 이슈가 덮이지 않는다는 사실을 안내에 명시한다
 * · 대시보드에서 심각도별로 필터된 상태로 진입할 수 있다 (?severity=BLOCKING)
 */

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { toast } from "sonner";
import {
  AlertTriangle,
  CalendarClock,
  CircleAlert,
  CircleCheck,
  CircleDashed,
  EyeOff,
  Info,
  Loader2,
  RefreshCw,
  Timer,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ApiError } from "@/lib/api";
import {
  ISSUE_SEVERITY_LABEL,
  ISSUE_STATUS_LABEL,
  operationsApi,
  type IssueSeverity,
  type IssueStatus,
  type OperationIssue,
} from "@/lib/api/operations";

const SEVERITY_ICON: Record<IssueSeverity, typeof Info> = {
  BLOCKING: CircleAlert,
  WARNING: AlertTriangle,
  INFO: Info,
};

const STATUS_ICON: Record<IssueStatus, typeof Info> = {
  OPEN: CircleDashed,
  IN_PROGRESS: Loader2,
  WAITING: Timer,
  RESOLVED: CircleCheck,
  IGNORED: EyeOff,
};

const ALL_SEVERITIES: IssueSeverity[] = ["BLOCKING", "WARNING", "INFO"];
const ALL_STATUSES: IssueStatus[] = [
  "OPEN",
  "IN_PROGRESS",
  "WAITING",
  "RESOLVED",
  "IGNORED",
];

function errorMessage(err: unknown, action: string): string {
  if (!(err instanceof ApiError)) {
    return `${action} 중 통신에 실패했습니다.`;
  }
  if (err.status === 403) return `${action} 권한이 없습니다.`;
  if (err.status === 409) return err.message || "상태가 이미 변경되었습니다. 새로고침하세요.";
  return err.message || `${action}에 실패했습니다.`;
}

export default function OperationIssuesPage() {
  const qc = useQueryClient();
  const searchParams = useSearchParams();

  const [severity, setSeverity] = useState<IssueSeverity[]>([]);
  const [status, setStatus] = useState<IssueStatus[]>([]);
  const [keyword, setKeyword] = useState("");
  const [issueType, setIssueType] = useState("");
  const [overdueOnly, setOverdueOnly] = useState(false);
  const [openOnly, setOpenOnly] = useState(true);
  const [target, setTarget] = useState<OperationIssue | null>(null);
  const [note, setNote] = useState("");
  const [nextStatus, setNextStatus] = useState<IssueStatus>("RESOLVED");

  // 대시보드 배지가 ?severity=BLOCKING / ?issue_type=... 으로 진입시킨다.
  // 유형별 배지를 눌렀는데 전체 목록이 뜨면 사용자가 그 건을 다시 찾아야 한다.
  useEffect(() => {
    const s = searchParams.get("severity");
    if (s && ALL_SEVERITIES.includes(s as IssueSeverity)) {
      setSeverity([s as IssueSeverity]);
    }
    setIssueType(searchParams.get("issue_type") ?? "");
    if (searchParams.get("overdue") === "1") setOverdueOnly(true);
  }, [searchParams]);

  const filters = useMemo(
    () => ({
      severity: severity.length ? severity : undefined,
      status: !openOnly && status.length ? status : undefined,
      issue_type: issueType || undefined,
      keyword: keyword.trim() || undefined,
      overdue_only: overdueOnly || undefined,
      open_only: openOnly || undefined,
      limit: 100,
    }),
    [severity, status, issueType, keyword, overdueOnly, openOnly],
  );

  const { data, isLoading, error } = useQuery({
    queryKey: ["operation-issues", filters],
    queryFn: () => operationsApi.listIssues(filters),
  });

  const { data: summary } = useQuery({
    queryKey: ["operation-issues", "summary"],
    queryFn: () => operationsApi.issueSummary(),
  });

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["operation-issues"] });

  const scan = useMutation({
    mutationFn: () => operationsApi.scan(),
    onSuccess: (r) => {
      invalidate();
      toast.success(
        `스캔 완료 — 후보 ${r.processed}건. 이미 처리·무시한 이슈는 다시 열지 않습니다.`,
      );
    },
    onError: (e) => toast.error(errorMessage(e, "스캔")),
  });

  const update = useMutation({
    mutationFn: (args: {
      id: string;
      status?: IssueStatus;
      resolution_note?: string;
    }) => operationsApi.updateIssue(args.id, args),
    onSuccess: () => {
      invalidate();
      setTarget(null);
      setNote("");
      toast.success("이슈를 갱신했습니다.");
    },
    onError: (e) => toast.error(errorMessage(e, "이슈 갱신")),
  });

  const issues = data?.items ?? [];

  return (
    <div className="flex h-full flex-col gap-4 p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">운영 이슈</h1>
          <p className="text-sm text-[--color-muted-foreground]">
            마감 검증과 상시 점검에서 발견한 것들. 처리하거나 사유와 함께 무시할 수
            있고, 스캔은 이미 닫은 이슈를 다시 열지 않습니다.
          </p>
        </div>
        <Button
          size="sm"
          variant="outline"
          onClick={() => scan.mutate()}
          disabled={scan.isPending}
        >
          <RefreshCw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          지금 스캔
        </Button>
      </div>

      {/* 요약 */}
      {summary ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          <SummaryTile label="미처리" value={summary.open_total} />
          <SummaryTile
            label="차단"
            value={summary.blocking}
            onClick={() => setSeverity(["BLOCKING"])}
          />
          <SummaryTile
            label="경고"
            value={summary.warning}
            onClick={() => setSeverity(["WARNING"])}
          />
          <SummaryTile
            label="기한 초과"
            value={summary.overdue}
            onClick={() => setOverdueOnly(true)}
          />
          <SummaryTile label="담당자 미지정" value={summary.unassigned} />
        </div>
      ) : null}

      {summary?.last_scan ? (
        <p className="text-xs text-[--color-muted-foreground]">
          마지막 스캔 {summary.last_scan.started_at.slice(0, 16).replace("T", " ")} ·{" "}
          {summary.last_scan.status}
        </p>
      ) : (
        <p className="text-xs text-[--color-warning]">
          아직 한 번도 스캔하지 않았습니다. 이 목록은 비어 있을 수 있습니다.
        </p>
      )}

      {/* 필터 */}
      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-[--color-border] p-3">
        <fieldset className="flex flex-col gap-1">
          <legend className="text-xs text-[--color-muted-foreground]">심각도</legend>
          <div className="flex gap-1">
            {ALL_SEVERITIES.map((s) => (
              <FilterChip
                key={s}
                active={severity.includes(s)}
                label={ISSUE_SEVERITY_LABEL[s]}
                onClick={() =>
                  setSeverity((prev) =>
                    prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s],
                  )
                }
              />
            ))}
          </div>
        </fieldset>

        <fieldset className="flex flex-col gap-1">
          <legend className="text-xs text-[--color-muted-foreground]">상태</legend>
          <div className="flex flex-wrap gap-1">
            <FilterChip
              active={openOnly}
              label="열린 것만"
              onClick={() => setOpenOnly((v) => !v)}
            />
            {!openOnly
              ? ALL_STATUSES.map((s) => (
                  <FilterChip
                    key={s}
                    active={status.includes(s)}
                    label={ISSUE_STATUS_LABEL[s]}
                    onClick={() =>
                      setStatus((prev) =>
                        prev.includes(s)
                          ? prev.filter((x) => x !== s)
                          : [...prev, s],
                      )
                    }
                  />
                ))
              : null}
          </div>
        </fieldset>

        <div className="flex flex-col gap-1">
          <Label htmlFor="issue-keyword" className="text-xs">
            검색
          </Label>
          <Input
            id="issue-keyword"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="제목·설명"
            className="h-8 w-48 text-sm"
          />
        </div>

        <FilterChip
          active={overdueOnly}
          label="기한 초과만"
          onClick={() => setOverdueOnly((v) => !v)}
        />

        {/* 대시보드에서 유형을 눌러 들어온 경우. 필터가 걸린 사실을 숨기면
            "왜 3건밖에 없지" 를 사용자가 혼자 추측하게 된다. */}
        {issueType ? (
          <FilterChip
            active
            label={`유형: ${issueType} ✕`}
            onClick={() => setIssueType("")}
          />
        ) : null}
      </div>

      {error ? (
        <div
          role="alert"
          className="rounded-md border border-[--color-danger] bg-[--color-danger-bg] p-3 text-sm"
        >
          이슈 목록을 불러오지 못했습니다.
        </div>
      ) : null}

      {/* 목록 */}
      <div className="flex-1 overflow-y-auto">
        {isLoading ? (
          <p className="p-4 text-sm text-[--color-muted-foreground]">불러오는 중...</p>
        ) : issues.length === 0 ? (
          <p className="p-4 text-sm text-[--color-muted-foreground]">
            조건에 맞는 이슈가 없습니다.
          </p>
        ) : (
          <ul className="space-y-2">
            {issues.map((issue) => {
              const SevIcon = SEVERITY_ICON[issue.severity] ?? Info;
              const StIcon = STATUS_ICON[issue.status] ?? CircleDashed;
              return (
                <li
                  key={issue.id}
                  className="rounded-lg border border-[--color-border] bg-[--color-card] p-3"
                >
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="inline-flex items-center gap-1 text-xs">
                          <SevIcon className="h-3.5 w-3.5" aria-hidden />
                          {ISSUE_SEVERITY_LABEL[issue.severity]}
                        </span>
                        <span className="inline-flex items-center gap-1 text-xs text-[--color-muted-foreground]">
                          <StIcon className="h-3.5 w-3.5" aria-hidden />
                          {ISSUE_STATUS_LABEL[issue.status]}
                        </span>
                        <code className="rounded bg-[--color-surface-muted] px-1 text-[11px]">
                          {issue.issue_type}
                        </code>
                        {issue.is_overdue ? (
                          <span className="inline-flex items-center gap-1 text-xs text-[--color-danger]">
                            <CalendarClock className="h-3.5 w-3.5" aria-hidden />
                            기한 초과
                          </span>
                        ) : null}
                      </div>
                      <p className="mt-1 text-sm font-medium">{issue.title}</p>
                      {issue.description ? (
                        <p className="mt-0.5 text-xs text-[--color-muted-foreground]">
                          {issue.description}
                        </p>
                      ) : null}
                      <p className="mt-1 text-[11px] text-[--color-muted-foreground]">
                        {issue.affected_count}건 · 최초 발견{" "}
                        {issue.first_detected_at.slice(0, 10)} · 마지막 확인{" "}
                        {issue.last_detected_at.slice(0, 10)}
                        {issue.assigned_to_name ? ` · 담당 ${issue.assigned_to_name}` : ""}
                      </p>
                      {issue.resolution_note ? (
                        <p className="mt-1 rounded bg-[--color-surface-muted] px-2 py-1 text-xs">
                          메모: {issue.resolution_note}
                        </p>
                      ) : null}
                    </div>

                    <div className="flex shrink-0 gap-1">
                      {issue.status === "OPEN" ? (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            update.mutate({ id: issue.id, status: "IN_PROGRESS" })
                          }
                        >
                          처리 시작
                        </Button>
                      ) : null}
                      {issue.status !== "RESOLVED" && issue.status !== "IGNORED" ? (
                        <>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => {
                              setTarget(issue);
                              setNextStatus("RESOLVED");
                              setNote("");
                            }}
                          >
                            해결
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => {
                              setTarget(issue);
                              setNextStatus("IGNORED");
                              setNote("");
                            }}
                          >
                            무시
                          </Button>
                        </>
                      ) : (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => update.mutate({ id: issue.id, status: "OPEN" })}
                        >
                          다시 열기
                        </Button>
                      )}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {/* 해결 / 무시 */}
      <Dialog open={!!target} onOpenChange={(v) => !v && setTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {nextStatus === "IGNORED" ? "이슈 무시" : "이슈 해결"}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3 py-2 text-sm">
            <p className="text-[--color-muted-foreground]">{target?.title}</p>
            {nextStatus === "IGNORED" ? (
              <p className="rounded-md border border-[--color-border] bg-[--color-surface-muted] px-3 py-2 text-xs text-[--color-muted-foreground]">
                무시한 이슈는 다음 스캔에서 다시 열리지 않습니다. 왜 처리하지 않아도
                되는지 남겨야 나중에 설명할 수 있습니다.
              </p>
            ) : null}
            <div className="space-y-1">
              <Label htmlFor="issue-note" className="text-xs">
                {nextStatus === "IGNORED" ? "무시 사유 *" : "처리 메모"}
              </Label>
              <Textarea
                id="issue-note"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder={
                  nextStatus === "IGNORED"
                    ? "예: 증정 건이라 원가 0원이 정상입니다"
                    : "예: 상품 연결 후 재계산 완료"
                }
                className="h-24 resize-none text-sm"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setTarget(null)}>
              취소
            </Button>
            <Button
              disabled={
                update.isPending ||
                (nextStatus === "IGNORED" && !note.trim())
              }
              onClick={() =>
                target &&
                update.mutate({
                  id: target.id,
                  status: nextStatus,
                  resolution_note: note.trim() || undefined,
                })
              }
            >
              {nextStatus === "IGNORED" ? "무시" : "해결"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function SummaryTile({
  label,
  value,
  onClick,
}: {
  label: string;
  value: number;
  onClick?: () => void;
}) {
  const content = (
    <>
      <p className="text-xs text-[--color-muted-foreground]">{label}</p>
      <p className="text-lg font-semibold tabular-nums">{value}</p>
    </>
  );
  if (!onClick) {
    return (
      <div className="rounded-lg border border-[--color-border] px-3 py-2">
        {content}
      </div>
    );
  }
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-lg border border-[--color-border] px-3 py-2 text-left hover:bg-[--color-surface-muted]"
    >
      {content}
    </button>
  );
}

function FilterChip({
  active,
  label,
  onClick,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-full border px-2.5 py-1 text-xs ${
        active
          ? "border-[--color-primary] bg-[--color-primary] text-[--color-primary-foreground]"
          : "border-[--color-border] text-[--color-muted-foreground]"
      }`}
    >
      {label}
    </button>
  );
}
