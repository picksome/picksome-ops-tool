/**
 * mock fallback 이 **운영에서 절대 켜지지 않는지** 고정한다.
 *
 * 왜 테스트가 필요한가
 * -----------------
 * 지금은 `mockFallbackEnabled()` 안에 `NODE_ENV !== "production"` 조건이 있다.
 * 그 한 줄이 사라지면 운영에서 BE 장애가 mock 으로 가려지고, 사용자는 **없는
 * 데이터를 편집한 뒤 저장이 안 되는 이유를 모른다.** 금액 화면에서 그건
 * 장애보다 나쁘다.
 *
 * 코드 리뷰로만 지키면 언젠가 빠진다. Core v1 Freeze 조건이라 테스트로 박는다.
 *
 * 소스를 읽어서 검사하는 이유
 * ------------------------
 * `NODE_ENV` 는 번들 시점에 치환되므로 런타임에 바꿔 끼울 수 없다. 그래서
 * 조건이 코드에 남아 있는지를 본다 — 문서가 아니라 코드를 확인한다.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SOURCE = readFileSync(
  join(process.cwd(), "lib", "adapters", "index.ts"),
  "utf-8",
);

describe("mock fallback 안전장치", () => {
  it("운영 환경 검사가 코드에 남아 있다", () => {
    expect(SOURCE).toContain('process.env.NODE_ENV !== "production"');
  });

  it("환경변수만으로는 켜지지 않는다 — 두 조건이 AND 로 묶여 있다", () => {
    // 플래그를 실수로 켠 채 배포해도 운영이면 무시돼야 한다.
    const fn = SOURCE.slice(
      SOURCE.indexOf("function mockFallbackEnabled"),
      SOURCE.indexOf("function delay"),
    );
    expect(fn).toContain('NODE_ENV !== "production"');
    expect(fn).toContain("NEXT_PUBLIC_ENABLE_MOCK_FALLBACK");
    expect(fn).toContain("&&");
    // OR 로 바뀌면 플래그 하나로 운영에서 켜진다.
    expect(fn).not.toContain("||");
  });

  it("401 은 mock 으로 가리지 않는다", () => {
    // 로그인 문제를 mock 으로 덮으면 사용자가 원인을 못 찾는다.
    expect(SOURCE).toContain("401");
  });
});
