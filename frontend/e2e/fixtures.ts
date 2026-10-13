import { test as base, expect, request, type APIRequestContext, type Page } from "@playwright/test";
import { SEED } from "./db";

/**
 * 로그인 헬퍼.
 *
 * 로그인 화면을 클릭으로 통과하지 않고 **실제 로그인 API** 를 호출해 토큰을 얻는다.
 * 화면 로그인은 그 자체로 별도 테스트 대상이고, 모든 시나리오가 매번 그걸 거치면
 * 로그인 UI 가 바뀔 때마다 관련 없는 테스트 8개가 같이 깨진다.
 *
 * mock 이 아니다 — 진짜 FastAPI 가 진짜 비밀번호를 검증하고 진짜 JWT 를 준다.
 *
 * 브라우저와 API 는 인증 경로가 다르다
 * ----------------------------------
 * 앱은 토큰을 `localStorage` 에 두고 fetch 에 붙인다. 그런데 `page.request` 는
 * **브라우저 밖**에서 도는 별도 클라이언트라 localStorage 를 보지 않는다.
 * 그래서 두 경로를 각각 준비한다 — 이걸 놓치면 화면은 로그인돼 있는데
 * API 호출만 401 이 나서 원인을 찾느라 헤맨다.
 */

export type Role = keyof typeof SEED.users;

/**
 * 프론트 origin.
 *
 * `page.url()` 에서 뽑으면 안 된다 — 아직 아무 데도 가지 않은 페이지는 `about:blank`
 * 이고, 거기서 origin 을 꺼내면 `"null"` 이 나와 요청 URL 이 통째로 깨진다.
 * playwright.config.ts 와 **같은 규칙**으로 계산한다.
 */
export const BASE_URL = `http://127.0.0.1:${process.env.E2E_FRONTEND_PORT ?? 3131}`;

export interface Session {
  accessToken: string;
  /** Authorization 헤더가 붙은 API 클라이언트. `page.request` 대신 이걸 쓴다. */
  api: APIRequestContext;
}

async function fetchTokens(baseURL: string, role: Role) {
  const anon = await request.newContext({ baseURL });
  try {
    const res = await anon.post("/api/v1/auth/login", {
      data: { email: SEED.users[role].email, password: SEED.password },
    });
    if (!res.ok()) {
      throw new Error(`로그인 실패 (${role}): ${res.status()} ${await res.text()}`);
    }
    const body = await res.json();
    return body.data ?? body;
  } finally {
    await anon.dispose();
  }
}

/** 브라우저 세션 + 인증된 API 클라이언트를 함께 만든다. */
export async function loginAs(page: Page, role: Role): Promise<Session> {
  const tokens = await fetchTokens(BASE_URL, role);

  // 앱이 읽는 키와 **정확히** 같아야 한다 (lib/auth.ts).
  await page.addInitScript(
    ([access, refresh]) => {
      window.localStorage.setItem("pso_access", access);
      window.localStorage.setItem("pso_refresh", refresh);
    },
    [tokens.access_token, tokens.refresh_token] as const,
  );

  const api = await request.newContext({
    baseURL: BASE_URL,
    extraHTTPHeaders: { Authorization: `Bearer ${tokens.access_token}` },
  });
  return { accessToken: tokens.access_token, api };
}

/** 화면 없이 API 만 필요할 때 (다른 역할로 승인 등). */
export async function apiAs(role: Role, baseURL = BASE_URL): Promise<APIRequestContext> {
  const tokens = await fetchTokens(baseURL, role);
  return request.newContext({
    baseURL,
    extraHTTPHeaders: { Authorization: `Bearer ${tokens.access_token}` },
  });
}

/**
 * 상품 drawer 를 연다.
 *
 * 목록에서 행을 찾아 클릭하는 대신 딥링크를 쓴다 — 목록 정렬·페이징에 의존하면
 * seed 상품이 늘어날 때마다 테스트가 깨진다.
 */
export async function openProductDrawer(page: Page, productId: string): Promise<void> {
  await page.goto(`/products?productId=${productId}`);
  await expect(page.getByRole("dialog")).toBeVisible({ timeout: 20_000 });
}

export const test = base;
export { expect };
