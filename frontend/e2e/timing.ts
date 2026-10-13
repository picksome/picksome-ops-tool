/**
 * E2E 구간 시간 측정.
 *
 * 왜 만들었나
 * ---------
 * `expense-screen.spec.ts` 의 "잠긴 달 정정" 이 한 번 46.8초에서 시간 초과로
 * 죽었다(제한 45초). 단독 실행은 17.7초, 이후 전체 실행에서도 재현되지 않았다.
 * reseed 는 0.26초로 측정돼 원인이 아니다.
 *
 * 이때 할 수 있는 나쁜 선택이 둘 있다.
 *
 * ```
 * timeout 45 → 90     증상만 밀어낸다. 90초에서 또 죽으면 그때는 무엇을 볼까
 * retries 0 → 1       빨간 불을 초록으로 덮는다. 그 순간부터 아무도 안 믿는다
 * ```
 *
 * 둘 다 하지 않았다. 대신 **다음에 느려질 때 어디서 느렸는지 알 수 있게** 한다.
 * 한 번의 실패에서 나온 총 시간 하나로는 원인을 좁힐 수 없다 — 로그인인지,
 * 첫 화면 컴파일인지, drawer 열기인지, 저장 왕복인지 구분이 안 된다.
 *
 * 쓰는 법
 * ------
 * ```ts
 * await phase("로그인", () => loginAs(page, "accountant"));
 * await phase("drawer 열기", () => openExpense(page, id));
 * ```
 *
 * `test.step` 으로 감싸므로 HTML 리포트와 trace 에 구간이 그대로 보이고, 느린
 * 구간은 콘솔에도 남는다. 테스트가 끝나면 전 구간 표가 첨부된다.
 *
 * 측정이 결과를 바꾸지 않는다
 * ------------------------
 * 대기를 넣지도, 조건을 바꾸지도 않는다. 시계만 읽는다.
 */

import { test } from "@playwright/test";

/**
 * 이 시간을 넘긴 구간은 콘솔에 바로 남긴다. 나중에 로그를 뒤지지 않게.
 *
 * `E2E_TIMING_VERBOSE=1` 이면 전부 남긴다 — 정상일 때의 기준값을 뜨려고 할 때
 * 쓴다. 기준값이 있어야 "이번엔 어디가 늘었다" 를 말할 수 있다.
 */
const VERBOSE = !!process.env.E2E_TIMING_VERBOSE;
const NOTABLE_MS = VERBOSE ? 0 : 5_000;

interface Mark {
  name: string;
  ms: number;
}

const marks = new WeakMap<object, Mark[]>();

function bucket(): Mark[] {
  const key = test.info();
  let list = marks.get(key);
  if (!list) {
    list = [];
    marks.set(key, list);
  }
  return list;
}

/** 한 구간을 재고 결과를 그대로 돌려준다. */
export async function phase<T>(name: string, fn: () => Promise<T>): Promise<T> {
  const started = Date.now();
  try {
    return await test.step(name, fn);
  } finally {
    const ms = Date.now() - started;
    bucket().push({ name, ms });
    if (ms >= NOTABLE_MS) {
      console.log(`[timing] ${test.info().title} / ${name}: ${ms}ms`);
    }
  }
}

/** 동기 구간(DB 확인 등)도 같은 표에 넣는다. */
export function phaseSync<T>(name: string, fn: () => T): T {
  const started = Date.now();
  try {
    return fn();
  } finally {
    bucket().push({ name, ms: Date.now() - started });
  }
}

/**
 * 측정한 구간을 리포트에 붙인다. `test.afterEach` 에서 부른다.
 *
 * 실패했을 때만 붙이지 않는다 — **정상일 때의 값이 있어야** 46.8초가 어디서
 * 늘어난 것인지 비교할 수 있다.
 */
export async function attachTimings(): Promise<void> {
  const info = test.info();
  const list = marks.get(info) ?? [];
  if (list.length === 0) return;

  const total = list.reduce((sum, m) => sum + m.ms, 0);
  const body = [
    `총 측정 구간 ${total}ms (테스트 제한 ${info.timeout}ms)`,
    ...list.map((m) => `${String(m.ms).padStart(7)}ms  ${m.name}`),
  ].join("\n");

  await info.attach("구간 시간", { body, contentType: "text/plain" });

  // 제한의 절반을 넘겼으면 아직 통과했어도 남긴다 — 다음에 죽을 후보다.
  if (VERBOSE || total > info.timeout / 2) {
    console.log(`[timing] 느림: ${info.title}\n${body}`);
  }
}
