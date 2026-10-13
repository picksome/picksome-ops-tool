import { defineConfig, devices } from "@playwright/test";

/**
 * 실제 FastAPI + 전용 PostgreSQL 로 도는 E2E.
 *
 * **mock API 를 쓰지 않는다.** mock 으로 도는 E2E 는 "화면이 그렇게 보인다" 만 증명한다.
 * 우리가 확인해야 하는 건 "DB 에 실제로 그 값이 들어갔다" 이고, 그 둘이 어긋난 채로
 * 통과하던 게 직전까지의 상태였다.
 *
 * 포트
 * ----
 * 고정 포트를 쓰면 개발 서버가 떠 있을 때 충돌한다. `E2E_BACKEND_PORT` /
 * `E2E_FRONTEND_PORT` 로 주입받고, 기본값도 개발 기본 포트(3000/8000)를 피한다.
 *
 * DB
 * --
 * `E2E_DATABASE_URL` 은 반드시 `_e2e` 로 끝나야 한다 — `scripts/e2e_db.py` 가 강제한다.
 * 운영 DB 를 가리키면 즉시 중단된다.
 */

const BACKEND_PORT = Number(process.env.E2E_BACKEND_PORT ?? 8131);
const FRONTEND_PORT = Number(process.env.E2E_FRONTEND_PORT ?? 3131);

const BACKEND_ORIGIN = `http://127.0.0.1:${BACKEND_PORT}`;
const FRONTEND_ORIGIN = `http://127.0.0.1:${FRONTEND_PORT}`;

const E2E_DATABASE_URL =
  process.env.E2E_DATABASE_URL ??
  "postgresql+psycopg://picksome_test:picksome_test@localhost:5432/picksome_e2e";

/**
 * 증빙 파일 저장 위치.
 *
 * 개발용 기본값(`backend/var/attachments`)과 **다른 디렉터리**여야 한다.
 * 같으면 E2E 가 개발 중 올린 파일을 지운다.
 */
export const E2E_ATTACHMENT_DIR =
  process.env.E2E_ATTACHMENT_DIR ?? "./var/e2e-attachments";

export default defineConfig({
  testDir: "./e2e",
  // 승인 워크플로는 같은 상품 행을 놓고 다툰다. 병렬로 돌리면 실패가
  // "코드 결함" 인지 "테스트끼리 충돌" 인지 구분할 수 없다.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  // **재시도하지 않는다.** 재시도로 통과시키면 "가끔 깨지는 테스트" 가 되고,
  // 그 순간부터 아무도 빨간 불을 믿지 않는다. 불안정하면 원인을 고친다.
  retries: 0,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI
    ? [["list"], ["html", { open: "never" }]]
    : [["list"], ["html", { open: "never" }]],

  use: {
    baseURL: FRONTEND_ORIGIN,
    // 실패했을 때 원인을 찾을 수 있어야 한다. trace 가 없으면
    // "가끔 실패함" 을 영원히 못 고친다.
    trace: "retain-on-failure",
    video: "retain-on-failure",
    screenshot: "only-on-failure",
  },

  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],

  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",

  webServer: [
    {
      // 백엔드 — E2E DB 를 가리킨다. 개발 DB 를 절대 건드리지 않는다.
      command:
        `python -m uvicorn app.main:app --host 127.0.0.1 --port ${BACKEND_PORT} --log-level info`,
      cwd: "../backend",
      url: `${BACKEND_ORIGIN}/api/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        DATABASE_URL: E2E_DATABASE_URL,
        E2E_DATABASE_URL,
        // 자격증명 암호화 마스터 키 (M042).
        //
        // **테스트 전용 고정값이다.** 운영 키를 E2E 에 넣지 않는다 — 서버
        // 로그·trace·CI 아티팩트에 환경이 통째로 남을 수 있다. 이 키로
        // 암호화되는 것도 `TEST_SECRET` 같은 가짜 값뿐이다.
        //
        // 고정값을 쓰는 이유: 매번 새로 만들면 앞 실행이 남긴 암호문을 못 읽고,
        // "왜 복호화가 실패하지" 를 테스트 문제인지 코드 문제인지 구분할 수 없다.
        CREDENTIAL_MASTER_KEY:
          process.env.E2E_CREDENTIAL_MASTER_KEY
          ?? "e2e0000000000000000000000000000000000000000=",
        // 증빙 파일은 **실제로 디스크에 쓴다.** mock storage 로 E2E 를 만들면
        // "저장했다고 응답했다" 만 증명하고 파일이 진짜 있는지는 모른다.
        // 테스트가 이 경로를 직접 뒤져 파일 존재를 확인한다.
        ATTACHMENT_STORAGE_DIR: E2E_ATTACHMENT_DIR,
      },
    },
    {
      // 프론트 — dev 서버로 띄운다. production build 는 매 실행마다 재빌드라 느리고,
      // E2E 가 검증하는 건 빌드 산출물이 아니라 화면과 API 의 계약이다.
      command: `npx next dev --port ${FRONTEND_PORT}`,
      url: FRONTEND_ORIGIN,
      reuseExistingServer: false,
      timeout: 180_000,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        BACKEND_ORIGIN,
        // E2E 는 실제 API 만 쓴다. mock fallback 이 켜지면 BE 장애가 가려져
        // 테스트가 초록인데 기능은 죽어 있는 상태가 된다.
        NEXT_PUBLIC_ENABLE_MOCK_FALLBACK: "false",
      },
    },
  ],
});
