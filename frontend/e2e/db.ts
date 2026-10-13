import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

/**
 * E2E 가 **DB 실제 값**을 확인하는 통로.
 *
 * 화면 문구로만 검증하면 "저장했습니다" 토스트는 뜨는데 DB 는 옛날 값인 상태를
 * 그대로 통과시킨다. 그게 이번에 고친 mock 사고의 본질이라, E2E 는 반드시
 * DB 를 직접 읽어서 확인한다.
 */

const BACKEND_DIR = path.resolve(__dirname, "../../backend");

export const E2E_DATABASE_URL =
  process.env.E2E_DATABASE_URL ??
  "postgresql+psycopg://picksome_test:picksome_test@localhost:5432/picksome_e2e";

/** seed 가 심는 고정 ID. `backend/scripts/e2e_db.py` 와 **반드시** 같아야 한다. */
export const SEED = {
  orgId: "e2e0e2e0-0000-0000-0000-000000000001",
  password: "e2e-Passw0rd!",
  users: {
    operator: { email: "operator@e2e-test.com", id: "e2e0e2e0-0000-0000-0000-0000000000a1" },
    approver: { email: "approver@e2e-test.com", id: "e2e0e2e0-0000-0000-0000-0000000000a2" },
    readonly: { email: "readonly@e2e-test.com", id: "e2e0e2e0-0000-0000-0000-0000000000a3" },
    // 비용 승인 주체는 ADMIN·ACCOUNTING 이다.
    accountant: { email: "accountant@e2e-test.com", id: "e2e0e2e0-0000-0000-0000-0000000000a4" },
    // 6개 역할을 전부 둔다 — 네 개만 있으면 이 둘의 차단은 아무도 검증하지 않는다.
    sourcing: { email: "sourcing@e2e-test.com", id: "e2e0e2e0-0000-0000-0000-0000000000a5" },
    extAccountant: {
      email: "extaccountant@e2e-test.com", id: "e2e0e2e0-0000-0000-0000-0000000000a6",
    },
  },
  products: {
    memo: "e2e0e2e0-0000-0000-0000-0000000000b1",
    name: "e2e0e2e0-0000-0000-0000-0000000000b2",
    cost: "e2e0e2e0-0000-0000-0000-0000000000b3",
    lock: "e2e0e2e0-0000-0000-0000-0000000000b4",
    race: "e2e0e2e0-0000-0000-0000-0000000000b5",
    options: "e2e0e2e0-0000-0000-0000-0000000000b6",
    price: "e2e0e2e0-0000-0000-0000-0000000000b7",
  },
  /** 사업비용 — 열린 달 1건, 잠긴 달(2026-07) 1건 */
  expenses: {
    open: "e2e0e2e0-0000-0000-0000-0000000000e1",
    locked: "e2e0e2e0-0000-0000-0000-0000000000e2",
    category: "e2e0e2e0-0000-0000-0000-0000000000f1",
  },
  /** 결제자·귀속 파트너. app_user 와 별개다. */
  partner: "e2e0e2e0-0000-0000-0000-0000000000f2",
  /** 월 마감 — 열린 달(8월)과 잠긴 달(7월). 잠긴 달은 조정·재오픈 검증용. */
  months: {
    open: "2026-06",
    locked: "2026-07",
    openId: "e2e0e2e0-0000-0000-0000-0000000000c1",
    /**
     * 대시보드 전용 달. 마감 흐름 E2E 가 쓰는 `open` 과 분리한다 —
     * 같은 달을 쓰면 한쪽이 확정하는 순간 다른 쪽 기대값이 바뀐다.
     */
    dashboard: "2026-05",
  },
  /** 대시보드 검증용 판매 2건 (2026-05). 판매처가 서로 다르다. */
  sales: {
    coupang: {
      id: "e2e0e2e0-0000-0000-0000-0000000000a5",
      marketplace: "COUPANG",
      salePrice: 300_000,
      cost: 180_000,
      marketplaceFee: 30_000,
      /** 본점 계정에 귀속돼 있다 (M032). */
      integrationAccountId: "e2e0e2e0-0000-0000-0000-0000000000d1",
    },
    smartstore: {
      id: "e2e0e2e0-0000-0000-0000-0000000000a6",
      marketplace: "SMARTSTORE",
      salePrice: 100_000,
      cost: 60_000,
      marketplaceFee: 5_000,
      /** 계정 미지정 — legacy NULL 주문이 집계·조회에서 빠지지 않는지 확인용. */
      integrationAccountId: null,
    },
  },
  /** 거래처(external_partner). partner 와 다른 테이블이다 — 돈을 지불하는 상대. */
  suppliers: {
    active: "e2e0e2e0-0000-0000-0000-0000000000f3",
    inactive: "e2e0e2e0-0000-0000-0000-0000000000f4",
  },
  /** 같은 판매처(쿠팡)의 서로 다른 판매자 계정 — 복수 스토어 검증용 */
  accounts: {
    main: "e2e0e2e0-0000-0000-0000-0000000000d1",
    second: "e2e0e2e0-0000-0000-0000-0000000000d2",
  },
} as const;

function run(cmd: string, arg?: string): string {
  const args = ["scripts/e2e_db.py", cmd];
  if (arg !== undefined) args.push(arg);
  return execFileSync("python", args, {
    cwd: BACKEND_DIR,
    env: { ...process.env, E2E_DATABASE_URL, PYTHONIOENCODING: "utf-8" },
    encoding: "utf-8",
  });
}

/** SELECT 결과를 그대로 돌려준다. */
export function dbQuery<T = Record<string, unknown>>(sql: string): T[] {
  return JSON.parse(run("query", sql)) as T[];
}

/** 한 행만 기대할 때. 없으면 null. */
export function dbOne<T = Record<string, unknown>>(sql: string): T | null {
  const rows = dbQuery<T>(sql);
  return rows.length > 0 ? rows[0] : null;
}

/** 사전 조건 조작 (월 잠금 등). **테스트 전용**이다. */
export function dbExec(sql: string): number {
  return (JSON.parse(run("exec", sql)) as { rowcount: number }).rowcount;
}

/**
 * seed 를 초기 상태로 되돌린다. 테스트 사이 격리.
 *
 * **증빙 저장 디렉터리도 함께 비운다.** DB 행만 지우면 앞 테스트가 올린 파일이
 * 디스크에 남아, "이 파일이 저장되지 않았는가" 를 확인하는 단언이 남의 파일을 보고
 * 실패한다. 지우는 대상은 `_e2e` 전용 디렉터리다(playwright.config.ts).
 */
export function reseed(): void {
  run("prepare");
  wipeAttachmentStorage();
}

export function product(id: string) {
  return dbOne<{
    id: string; name_ko: string; memo: string | null; cost_amount: number;
    status: string; updated_at: string;
  }>(`SELECT id, name_ko, memo, cost_amount, status, updated_at
        FROM product WHERE id = '${id}'`);
}

export function approvalsFor(productId: string) {
  return dbQuery<{
    id: string; business_key: string; request_type: string; status: string;
    decision_outcome: string; auto_apply_status: string | null; effective_from: string | null;
  }>(`SELECT id, business_key, request_type, status, decision_outcome,
             auto_apply_status, effective_from
        FROM approval_request
       WHERE target_id = '${productId}' AND deleted_at IS NULL
       ORDER BY requested_at DESC`);
}

export function auditFor(productId: string) {
  return dbQuery<{ action: string; domain_table: string }>(
    `SELECT action, domain_table FROM audit_log
      WHERE domain_row_id = '${productId}' ORDER BY created_at DESC`,
  );
}

/**
 * executor advisory lock 을 붙잡은 자식 프로세스를 띄운다.
 *
 * 반환된 `release()` 를 부르면 프로세스를 끝내 잠금을 푼다.
 * 잠금이 **확실히 잡힌 뒤에** resolve 하므로, 테스트가 경합에 기대지 않는다.
 */
export async function holdExecutorLock(seconds = 20): Promise<() => void> {
  const { spawn } = await import("node:child_process");
  const child = spawn("python", ["scripts/e2e_db.py", "hold-lock", String(seconds)], {
    cwd: BACKEND_DIR,
    env: { ...process.env, E2E_DATABASE_URL, PYTHONIOENCODING: "utf-8" },
  });

  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("잠금 획득 시간 초과")), 20_000);
    child.stdout.on("data", (buf: Buffer) => {
      if (buf.toString().includes("LOCKED")) {
        clearTimeout(timer);
        resolve();
      }
    });
    child.on("error", reject);
    child.on("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`잠금 프로세스가 먼저 종료됨 (code ${code})`));
    });
  });

  return () => child.kill();
}

/**
 * 증빙 파일이 **실제로 디스크에 있는지** 확인한다.
 *
 * DB 상태만 보면 "DELETED 로 바뀌었다" 까지만 알 수 있다. 파일이 남아 있는데
 * 지웠다고 표시된 상태를 그대로 통과시키지 않으려면 디스크를 봐야 한다.
 */
function attachmentRoot(): string {
  const root = path.resolve(
    __dirname, "../../backend",
    process.env.E2E_ATTACHMENT_DIR ?? "./var/e2e-attachments",
  );
  // 안전장치 — 운영·개발 첨부 디렉터리를 지우는 일이 없어야 한다.
  if (!root.includes("e2e")) {
    throw new Error(`E2E 첨부 디렉터리 이름에 'e2e' 가 없습니다: ${root}`);
  }
  return root;
}

export function wipeAttachmentStorage(): void {
  fs.rmSync(attachmentRoot(), { recursive: true, force: true });
}

export function attachmentFiles(): string[] {
  const root = attachmentRoot();
  if (!fs.existsSync(root)) return [];
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else out.push(path.relative(root, full));
    }
  };
  walk(root);
  return out;
}

export function attachmentsOf(expenseId: string) {
  return dbQuery<{
    id: string; original_filename: string; status: string; scan_status: string;
    file_hash: string; size_bytes: number; storage_key: string;
  }>(`SELECT id::text, original_filename, status, scan_status, file_hash,
             size_bytes, storage_key
        FROM expense_attachment
       WHERE expense_id = '${expenseId}' AND deleted_at IS NULL
       ORDER BY uploaded_at`);
}

/**
 * 서버 기준 오늘 (Asia/Seoul).
 *
 * `new Date().toISOString()` 은 **UTC 날짜**를 준다. 백엔드는 `date.today()` 를
 * Asia/Seoul 로 쓰므로 00:00~09:00 KST 사이에는 두 날짜가 하루 어긋난다.
 *
 * 그 시간대에 "오늘 날짜로" 만든 데이터는 서버가 보기엔 **어제**라서,
 * 오늘부터 앞을 보는 조회(현금흐름 30일 등)에서 빠진다. 테스트가 새벽에만
 * 깨지는 이유가 이것이다 — 재시도로 덮으면 원인을 영영 못 찾는다.
 */
export function todayKst(): string {
  return new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 10);
}

/** 서버 기준 오늘로부터 n일 뒤. */
export function daysFromTodayKst(n: number): string {
  return new Date(Date.now() + (9 * 3600 + n * 86400) * 1000)
    .toISOString().slice(0, 10);
}
