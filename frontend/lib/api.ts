/**
 * BE API fetch wrapper.
 *
 * docs/22_api_spec.md §1-3 envelope:
 *   성공: { data: ..., meta: {...} }
 *   실패: { error: { code, message, details? } }
 *
 * 동작:
 *   1. Authorization 헤더 자동 부착 (getAccessToken)
 *   2. 401 응답 → 1회 refresh 시도 → 성공 시 원 요청 재시도, 실패 시 token 비우고 throw
 *   3. envelope 자동 unwrap — 호출자는 data 만 받음
 *   4. error envelope 은 ApiError 로 throw — TanStack Query 의 onError 가 잡음
 *
 * 동시 401 이 여러 개일 때 refresh 가 한 번만 수행되도록 단일 Promise 캐싱.
 */

import { clearTokens, getAccessToken, getRefreshToken, saveTokens } from "./auth";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api/v1";

export class ApiError extends Error {
  code: string;
  status: number;
  details?: unknown;

  constructor(code: string, message: string, status: number, details?: unknown) {
    super(message);
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

let refreshPromise: Promise<boolean> | null = null;

async function refreshAccessToken(): Promise<boolean> {
  if (refreshPromise) return refreshPromise;
  const token = getRefreshToken();
  if (!token) return false;

  refreshPromise = (async () => {
    try {
      const res = await fetch(`${API_BASE}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: token }),
      });
      if (!res.ok) {
        clearTokens();
        return false;
      }
      const body = await res.json();
      if (body?.data?.access_token) {
        saveTokens(body.data);
        return true;
      }
      clearTokens();
      return false;
    } catch {
      clearTokens();
      return false;
    } finally {
      refreshPromise = null;
    }
  })();

  return refreshPromise;
}

/**
 * 목록 API 의 표준 반환 형태.
 *
 * BE 는 `{ data: [...], meta: { total, limit, offset } }` 로 주지만
 * doFetch 가 이 형태로 합쳐서 내려준다(아래 unwrap 참고).
 * `page` / `page_size` 는 서버가 돌려주지 않으므로 여기 없다 — 요청 파라미터일 뿐이다.
 */
export interface ListResponse<T> {
  items: T[];
  total: number;
  limit?: number;
  offset?: number;
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE" | "PUT";
  body?: unknown;
  /**
   * 쿼리 파라미터.
   *
   * 배열을 주면 **반복 파라미터**로 편다(`?status=OPEN&status=WAITING`).
   * FastAPI 의 `list[str] = Query(...)` 가 그 형태를 기대한다 —
   * 쉼표로 이어 붙이면 값 하나짜리 리스트로 파싱된다.
   */
  query?: Record<
    string,
    string | number | boolean | undefined | null | readonly (string | number)[]
  >;
  signal?: AbortSignal;
  /** 인증 없이 호출 (예: /auth/login). 기본 false. */
  skipAuth?: boolean;
  /** 추가 헤더. Idempotency-Key 처럼 요청 단위로 붙이는 값. */
  headers?: Record<string, string>;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const url = path.startsWith("http") ? path : `${API_BASE}${path}`;
  if (!query) return url;
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null) continue;
    if (Array.isArray(v)) {
      for (const item of v) {
        if (item === undefined || item === null) continue;
        usp.append(k, String(item));
      }
      continue;
    }
    usp.append(k, String(v));
  }

  // BE 가 엔드포인트마다 페이지네이션 파라미터를 섞어 쓴다:
  //   limit/offset  — products, expenses, policies, notifications, admin_* 등
  //   page/page_size — sales, sourcing, reports, marketplace_settlements
  // 프론트는 전부 page/page_size 로 보내므로 limit/offset 쪽 엔드포인트에서는
  // 값이 통째로 무시되고 항상 1페이지만 나왔다. 양쪽을 같이 실어 보내면
  // 어느 쪽을 읽는 엔드포인트든 같은 페이지를 준다(FastAPI 는 모르는 쿼리를 무시).
  const page = Number(query.page);
  const pageSize = Number(query.page_size);
  if (Number.isFinite(page) && Number.isFinite(pageSize) && page > 0 && pageSize > 0) {
    if (!usp.has("limit")) usp.append("limit", String(pageSize));
    if (!usp.has("offset")) usp.append("offset", String((page - 1) * pageSize));
  }

  const qs = usp.toString();
  return qs ? `${url}?${qs}` : url;
}

async function doFetch(path: string, opts: RequestOptions, retry: boolean): Promise<unknown> {
  const headers: Record<string, string> = {
    Accept: "application/json",
  };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (!opts.skipAuth) {
    const tk = getAccessToken();
    if (tk) headers["Authorization"] = `Bearer ${tk}`;
  }
  if (opts.headers) Object.assign(headers, opts.headers);

  const res = await fetch(buildUrl(path, opts.query), {
    method: opts.method ?? "GET",
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  });

  if (res.status === 401 && !opts.skipAuth && retry) {
    const ok = await refreshAccessToken();
    if (ok) return doFetch(path, opts, false);
  }

  // 응답이 비어있을 수 있음 (204 등) — text 로 받아 비면 null.
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;

  if (!res.ok) {
    const err = body?.error;
    throw new ApiError(
      err?.code ?? "HTTP_ERROR",
      err?.message ?? `요청 실패 (${res.status})`,
      res.status,
      err?.details,
    );
  }

  // 목록 응답은 envelope 이 `{ data: [...], meta: { total, limit, offset } }` 다.
  // data 만 꺼내고 meta 를 버리면 화면이 기대하는 `{ items, total }` 이 만들어지지
  // 않아 **모든 목록 화면이 빈 상태로 보인다** (data.items === undefined).
  // 실제로 상품 1,065건이 있는데도 "등록된 상품이 없습니다" 가 떴다.
  // 배열 + meta 인 경우에만 합쳐서 내려준다 — 단건 응답은 영향 없음.
  if (body && Array.isArray(body.data) && body.meta) {
    return {
      ...body.meta,
      items: body.data,
      total: body.meta.total ?? body.data.length,
    };
  }

  // `data` 가 **정당하게 null** 인 응답이 있다 — 예: `GET /distributions/{ym}` 는
  // 아직 확정 배분이 없으면 `{"data": null}` 을 준다.
  //
  // 예전 코드는 `body?.data ?? body` 였다. `??` 는 null 에서 오른쪽으로 넘어가므로
  // **envelope 객체 자체**(`{data: null}`)가 반환됐다. 호출자는 그것을 "값이 있다"로
  // 읽고 `run.results.length` 에서 터졌다. 화면 전체가 런타임 오류로 죽는다.
  //
  // 그래서 `data` 키의 **존재 여부**로 판단한다. 키가 있으면 그 값이 null 이어도 그대로 준다.
  if (body && typeof body === "object" && "data" in body) return body.data;
  return body;
}

export function apiRequest<T = unknown>(path: string, opts: RequestOptions = {}): Promise<T> {
  return doFetch(path, opts, true) as Promise<T>;
}

export const api = {
  get: <T = unknown>(path: string, opts?: Omit<RequestOptions, "method" | "body">) =>
    apiRequest<T>(path, { ...opts, method: "GET" }),
  post: <T = unknown>(path: string, body?: unknown, opts?: Omit<RequestOptions, "method">) =>
    apiRequest<T>(path, { ...opts, method: "POST", body }),
  patch: <T = unknown>(path: string, body?: unknown, opts?: Omit<RequestOptions, "method">) =>
    apiRequest<T>(path, { ...opts, method: "PATCH", body }),
  put: <T = unknown>(path: string, body?: unknown, opts?: Omit<RequestOptions, "method">) =>
    apiRequest<T>(path, { ...opts, method: "PUT", body }),
  delete: <T = unknown>(path: string, opts?: Omit<RequestOptions, "method" | "body">) =>
    apiRequest<T>(path, { ...opts, method: "DELETE" }),
};
