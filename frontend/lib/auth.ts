/**
 * JWT 토큰 저장 / 회수 / 갱신 layer.
 *
 * 보안 메모 (docs/31_tech_frontend.md §7):
 *   장기적으로는 access token = 메모리, refresh token = httpOnly cookie 로 옮길 예정.
 *   MVP 는 localStorage 단순 저장으로 시작 — XSS 위험은 인지하나, 인증 후 노출되는
 *   민감 정보가 정책/정산이라 우선순위가 낮음. M00X 단계에서 cookie 전환.
 *
 * - 토큰 키 prefix `pso_` 로 다른 앱과 충돌 방지.
 * - Server Component 에서는 사용 금지 (브라우저 storage 의존) — `"use client"` 보호자에서만.
 */

const ACCESS_KEY = "pso_access";
const REFRESH_KEY = "pso_refresh";

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export function saveTokens(tokens: TokenPair): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(ACCESS_KEY, tokens.access_token);
  window.localStorage.setItem(REFRESH_KEY, tokens.refresh_token);
}

export function clearTokens(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(ACCESS_KEY);
  window.localStorage.removeItem(REFRESH_KEY);
}

export function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(ACCESS_KEY);
}

export function getRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(REFRESH_KEY);
}

export function isAuthenticated(): boolean {
  return getAccessToken() !== null;
}
