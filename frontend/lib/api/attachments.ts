/**
 * 증빙 파일 클라이언트 (M027).
 *
 * **브라우저 메모리에 올려두는 가짜 첨부가 아니다.** 실제로 서버에 저장되고,
 * 새로고침해도 남고, 삭제는 승인을 거친다.
 *
 * `storage_key` 는 응답에 없다 — 서버가 내려주지 않는다. 다운로드는 권한 검사를
 * 통과하는 엔드포인트로만 하고, 영구 public URL 은 만들지 않는다.
 */

import { api, ApiError, type ListResponse } from "@/lib/api";
import { getAccessToken } from "@/lib/auth";

export type AttachmentStatus =
  | "PENDING_SCAN" | "AVAILABLE" | "PENDING_DELETE" | "DELETED" | "FAILED";

export type ScanStatus =
  | "SCAN_UNAVAILABLE" | "PENDING" | "CLEAN" | "INFECTED" | "SCAN_FAILED";

export interface ExpenseAttachment {
  id: string;
  expense_id: string;
  filename: string;
  mime_type: string | null;
  size_bytes: number | null;
  file_hash: string | null;
  status: AttachmentStatus;
  scan_status: ScanStatus;
  uploaded_by: string | null;
  uploaded_by_name: string | null;
  uploaded_at: string | null;
  delete_requested_at: string | null;
  delete_approval_request_id: string | null;
  delete_attempt_count: number;
  delete_last_error: string | null;
}

export interface AttachmentListMeta {
  max_attachments: number;
  max_file_bytes: number;
  /** 서버가 판단한다 — 화면이 "삭제" 와 "삭제 승인 요청" 중 무엇을 보여줄지 */
  delete_requires_approval: boolean;
  /** 스캐너 연결 여부. false 면 화면이 "검사됨" 이라고 말하지 않는다 */
  scanner_available: boolean;
}

export const ATTACHMENT_STATUS_LABEL: Record<AttachmentStatus, string> = {
  PENDING_SCAN: "검사 대기",
  AVAILABLE: "보관 중",
  PENDING_DELETE: "삭제 대기",
  DELETED: "삭제됨",
  FAILED: "업로드 실패",
};

/**
 * 스캔 상태 라벨.
 *
 * `SCAN_UNAVAILABLE` 을 "안전" 으로 표시하지 않는다. 검사하지 않은 파일을
 * 안전하다고 말하면 사용자는 검사를 통과했다고 믿는다.
 */
export const SCAN_STATUS_LABEL: Record<ScanStatus, string> = {
  SCAN_UNAVAILABLE: "검사 안 함",
  PENDING: "검사 대기",
  CLEAN: "검사 통과",
  INFECTED: "악성코드 검출",
  SCAN_FAILED: "검사 실패",
};

/** 클라이언트 선검증 — 서버가 다시 검사한다. 여기서 막는 건 왕복 낭비뿐이다. */
export const ALLOWED_EXTENSIONS = ["jpg", "jpeg", "png", "heic", "pdf"] as const;
export const ALLOWED_MIME = [
  "image/jpeg", "image/png", "image/heic", "image/heif", "application/pdf",
] as const;

export interface UploadRejection {
  filename: string;
  reason: string;
}

/**
 * 올리기 전에 거를 수 있는 것만 거른다.
 *
 * 서버 검증을 **대신하지 않는다** — 확장자 위장은 내용을 봐야 알고, 그건 서버가 한다.
 * 여기서 막는 건 10MB 파일을 올려놓고 거절당하는 왕복이다.
 */
export function precheckFile(
  file: File, opts: { maxBytes: number },
): UploadRejection | null {
  const ext = file.name.includes(".")
    ? file.name.split(".").pop()!.toLowerCase()
    : "";
  if (!(ALLOWED_EXTENSIONS as readonly string[]).includes(ext)) {
    return { filename: file.name, reason: "jpg·jpeg·png·heic·pdf 만 첨부할 수 있습니다" };
  }
  if (file.size === 0) {
    return { filename: file.name, reason: "빈 파일입니다" };
  }
  if (file.size > opts.maxBytes) {
    const mb = Math.round((opts.maxBytes / (1024 * 1024)) * 10) / 10;
    return { filename: file.name, reason: `${mb}MB를 넘습니다` };
  }
  return null;
}

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api/v1";

export const attachmentsApi = {
  list: (expenseId: string) =>
    api.get<ListResponse<ExpenseAttachment> & AttachmentListMeta>(
      `/expenses/${expenseId}/attachments`,
    ),

  /**
   * 업로드 1건.
   *
   * `fetch` 대신 XHR 을 쓴다 — **진행률이 필요하다.** fetch 는 업로드 진행률을
   * 주지 않고, 증빙은 10MB 까지 허용해서 느린 회선에서 몇 초씩 걸린다.
   * 그 사이 아무 표시가 없으면 사용자는 버튼을 다시 누른다.
   */
  upload: (
    expenseId: string, file: File, onProgress?: (percent: number) => void,
  ): Promise<ExpenseAttachment> =>
    new Promise((resolve, reject) => {
      const form = new FormData();
      form.append("file", file);

      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${BASE}/expenses/${expenseId}/attachments`);
      const token = getAccessToken();
      if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);

      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) {
          onProgress(Math.round((e.loaded / e.total) * 100));
        }
      };
      xhr.onload = () => {
        let body: { data?: ExpenseAttachment; error?: { code: string; message: string; details?: unknown } } = {};
        try {
          body = JSON.parse(xhr.responseText || "{}");
        } catch {
          // 서버가 JSON 이 아닌 응답을 냈다(프록시 오류 등). 아래에서 상태로 판단한다.
        }
        if (xhr.status >= 200 && xhr.status < 300 && body.data) {
          resolve(body.data);
          return;
        }
        reject(new ApiError(
          body.error?.code ?? "UPLOAD_FAILED",
          body.error?.message ?? `업로드 실패 (${xhr.status})`,
          xhr.status,
          body.error?.details,
        ));
      };
      xhr.onerror = () => reject(
        new ApiError("NETWORK_ERROR", "네트워크 오류로 업로드하지 못했습니다.", 0));
      xhr.onabort = () => reject(
        new ApiError("ABORTED", "업로드를 취소했습니다.", 0));
      xhr.send(form);
    }),

  /**
   * 다운로드.
   *
   * `<a href>` 로 걸지 않는다 — 토큰이 Authorization 헤더에 있어서 브라우저의
   * 평범한 네비게이션으로는 인증이 붙지 않는다. 받아서 blob URL 로 저장한다.
   * blob URL 은 **곧바로 폐기**해서 링크가 남지 않게 한다.
   */
  download: async (attachmentId: string, filename: string): Promise<void> => {
    const token = getAccessToken();
    const res = await fetch(`${BASE}/expense-attachments/${attachmentId}/download`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) {
      let code = "DOWNLOAD_FAILED";
      let message = `다운로드 실패 (${res.status})`;
      try {
        const body = await res.json();
        code = body?.error?.code ?? code;
        message = body?.error?.message ?? message;
      } catch {
        // 본문이 JSON 이 아니면 상태 코드 기반 메시지를 쓴다.
      }
      throw new ApiError(code, message, res.status);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  },

  /** 직접 삭제. 승인이 필요한 비용이면 서버가 403(`APPROVAL_REQUIRED`)을 준다. */
  remove: (attachmentId: string) =>
    api.delete<{ id: string; status: AttachmentStatus }>(
      `/expense-attachments/${attachmentId}`),

  /** 삭제 worker 1회 실행 (ADMIN). cron 이 없어도 사람이 밀 수 있어야 한다. */
  runDelete: () =>
    api.post<{ scanned: number; deleted: number; retry_pending: number; failed: number }>(
      "/expense-attachments/run-delete"),
};
