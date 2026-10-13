/**
 * E2E 종료 정리.
 *
 * 기본은 **DB 를 남긴다.** 실패했을 때 DB 상태가 가장 중요한 단서인데
 * 자동으로 지워버리면 원인을 볼 수 없다. CI 처럼 매번 깨끗해야 하는 환경은
 * `E2E_DROP_DB=true` 로 켠다.
 *
 * 어차피 다음 실행의 globalSetup 이 seed 를 초기 상태로 되돌리므로,
 * 남겨두는 것이 테스트 반복 실행을 깨뜨리지 않는다.
 */
import { execFileSync } from "node:child_process";
import path from "node:path";
import { E2E_DATABASE_URL } from "./db";

export default function globalTeardown(): void {
  if (process.env.E2E_DROP_DB !== "true") {
    console.log("[e2e] DB 를 남깁니다 (E2E_DROP_DB=true 로 삭제).");
    return;
  }
  execFileSync("python", ["scripts/e2e_db.py", "drop"], {
    cwd: path.resolve(__dirname, "../../backend"),
    env: { ...process.env, E2E_DATABASE_URL, PYTHONIOENCODING: "utf-8" },
    encoding: "utf-8",
  });
}
