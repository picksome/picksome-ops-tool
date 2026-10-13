import { execFileSync } from "node:child_process";
import path from "node:path";
import { E2E_DATABASE_URL } from "./db";

/**
 * E2E DB 준비 — 생성 · migration head · 결정적 seed.
 *
 * webServer 가 뜨기 **전에** 돌아야 한다. 백엔드가 먼저 뜨면 migration 이 안 된
 * DB 에 붙어 첫 요청부터 터진다.
 */
export default function globalSetup(): void {
  const backendDir = path.resolve(__dirname, "../../backend");
  const out = execFileSync("python", ["scripts/e2e_db.py", "prepare"], {
    cwd: backendDir,
    env: { ...process.env, E2E_DATABASE_URL, PYTHONIOENCODING: "utf-8" },
    encoding: "utf-8",
  });
  console.log(`[e2e] DB 준비 완료: ${out.trim()}`);
}
