/**
 * 옵션 순차 저장 — 행별 결과.
 *
 * 백엔드에 variant 일괄 저장 API 가 없어서 바뀐 행만 하나씩 보낸다.
 * 그때 **한 건이 실패했다고 나머지를 포기하면 안 되고, 일부만 성공했는데
 * 전체 성공으로 표시해도 안 된다.**
 *
 * 이전 구현은 첫 실패에서 루프가 통째로 중단되고 일반 오류 토스트만 떴다.
 * 사용자는 어느 행이 저장됐는지 알 수 없었고, 다시 누르면 성공한 행까지 또 보냈다.
 *
 * 이 파일은 렌더링과 무관한 순수 로직이라 단위 테스트가 가능하다.
 */

import { ApiError } from "@/lib/api";
import type { ProductOption } from "@/lib/adapters/types";

export type RowState =
  | "PENDING"    // 대기
  | "SAVING"     // 저장 중
  | "SAVED"      // 성공
  | "FAILED"     // 실패
  | "CONFLICT"   // 다른 사람이 먼저 고침 — 최신값 확인 필요
  | "RETRY";     // 재시도 대기

export interface RowOutcome {
  optionId: string;
  /** 화면 표시용 이름 */
  label: string;
  state: RowState;
  errorCode: string | null;
  errorMessage: string | null;
  /** 최신 데이터를 다시 불러와 비교해야 하는가 (409 계열) */
  needsRefresh: boolean;
}

export interface SequentialSaveResult {
  outcomes: RowOutcome[];
  attempted: number;
  succeeded: number;
  failed: number;
  /** 바뀐 게 없어 건너뛴 행 수 */
  skipped: number;
  /** 전부 성공했는가. **일부 성공은 false** */
  allSucceeded: boolean;
}

export interface SaveHandlers {
  create: (option: ProductOption) => Promise<ProductOption>;
  update: (option: ProductOption) => Promise<ProductOption>;
}

/** 저장 대상 필드만 비교한다. 표시용 파생값은 무시. */
export function isSameOption(a: ProductOption, b: ProductOption): boolean {
  return (
    a.optionSku === b.optionSku &&
    a.optionCost === b.optionCost &&
    a.extraPrice === b.extraPrice &&
    a.stock === b.stock &&
    a.isActive === b.isActive
  );
}

function classifyError(err: unknown): Pick<RowOutcome, "state" | "errorCode" | "errorMessage" | "needsRefresh"> {
  if (err instanceof ApiError) {
    // 409 는 "값이 잘못됐다" 가 아니라 "화면이 낡았다" 다.
    // 같은 값으로 다시 보내면 또 실패하므로 최신값을 받아야 한다.
    const isConflict = err.status === 409;
    return {
      state: isConflict ? "CONFLICT" : "FAILED",
      errorCode: err.code,
      errorMessage: err.message,
      needsRefresh: isConflict,
    };
  }
  return {
    state: "FAILED",
    errorCode: null,
    errorMessage: "네트워크 상태를 확인한 뒤 다시 시도해 주세요.",
    needsRefresh: false,
  };
}

function labelOf(option: ProductOption): string {
  return option.customName ?? option.autoName ?? Object.values(option.values).join(" / ");
}

/**
 * 변경된 행만 순차 저장한다.
 *
 * **한 행이 실패해도 나머지를 계속 시도한다.** 중단하면 사용자가 다시 눌렀을 때
 * 이미 성공한 행까지 또 보내게 되고, 어디까지 됐는지도 알 수 없다.
 *
 * `onProgress` 는 각 행의 상태가 바뀔 때마다 불린다 — 화면이 진행률을 그린다.
 */
export async function runSequentialSave(
  rows: ProductOption[],
  original: Map<string, ProductOption>,
  handlers: SaveHandlers,
  onProgress?: (outcome: RowOutcome) => void,
): Promise<SequentialSaveResult> {
  const outcomes: RowOutcome[] = [];
  let skipped = 0;

  for (const row of rows) {
    const before = original.get(row.id);
    // 안 바뀐 행은 보내지 않는다. 전부 보내면 안 바꾼 행까지 updated_at 이
    // 갱신되어 변경 이력이 의미를 잃는다.
    if (before && isSameOption(before, row)) {
      skipped += 1;
      continue;
    }

    const base: RowOutcome = {
      optionId: row.id, label: labelOf(row), state: "SAVING",
      errorCode: null, errorMessage: null, needsRefresh: false,
    };
    onProgress?.(base);

    try {
      if (before) await handlers.update(row);
      else await handlers.create(row);
      const ok = { ...base, state: "SAVED" as const };
      outcomes.push(ok);
      onProgress?.(ok);
    } catch (err) {
      const failed = { ...base, ...classifyError(err) };
      outcomes.push(failed);
      onProgress?.(failed);
    }
  }

  const succeeded = outcomes.filter((o) => o.state === "SAVED").length;
  const failed = outcomes.length - succeeded;

  return {
    outcomes,
    attempted: outcomes.length,
    succeeded,
    failed,
    skipped,
    // 시도한 게 없으면 "전부 성공" 이 아니라 "변경 없음" 이다.
    // 여기서 true 를 돌려주면 화면이 성공 토스트를 띄운다.
    allSucceeded: outcomes.length > 0 && failed === 0,
  };
}

/** 실패·충돌 행만 골라 재시도 대상으로 만든다. */
export function failedRowIds(result: SequentialSaveResult): Set<string> {
  return new Set(
    result.outcomes.filter((o) => o.state !== "SAVED").map((o) => o.optionId),
  );
}

/** 성공한 행만 골라낸다 — 이 행들만 dirty 상태를 푼다. */
export function savedRowIds(result: SequentialSaveResult): Set<string> {
  return new Set(
    result.outcomes.filter((o) => o.state === "SAVED").map((o) => o.optionId),
  );
}

/** 결과 요약 문장. 일부 성공을 전체 성공처럼 말하지 않는다. */
export function summarize(result: SequentialSaveResult): {
  tone: "success" | "warning" | "info";
  title: string;
  description: string;
} {
  if (result.attempted === 0) {
    return { tone: "info", title: "변경된 옵션이 없습니다", description: "" };
  }
  if (result.allSucceeded) {
    return {
      tone: "success",
      title: `옵션 ${result.succeeded}건을 저장했습니다`,
      description: "",
    };
  }
  return {
    tone: "warning",
    title: `옵션 ${result.attempted}건 중 ${result.succeeded}건만 저장됐습니다`,
    description: `실패 ${result.failed}건은 입력값이 유지됩니다. 실패 건만 다시 시도할 수 있습니다.`,
  };
}
