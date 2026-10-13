/**
 * 예외 센터 표시 규칙.
 *
 * 이 파일이 고정하는 것
 * ------------------
 * `count = 0` 이 다섯 가지 뜻을 갖는다. 화면이 그 다섯을 같은 문구로 그리면,
 * "클레임 API 미연결" 이 "취소·반품 없음" 으로 보인다. 실제로 반품이 쌓이고
 * 있어도 사용자는 아무것도 모른다.
 *
 * 그래서 문구는 **status 에서** 나와야 하고 건수에서 나오면 안 된다.
 */

import { describe, expect, it } from "vitest";
import { categoryText, type ExceptionCategorySummary } from "./exceptions";

function summary(
  status: ExceptionCategorySummary["status"],
  count = 0,
): ExceptionCategorySummary {
  return { category: "ORDER", label: "주문·클레임", status, count, note: null };
}

describe("카테고리 문구", () => {
  it("건수 0 을 다섯 가지로 구분한다", () => {
    // 전부 count=0 인데 사용자가 할 일이 완전히 다르다.
    expect(categoryText(summary("OK"))).toBe("이상 없음");
    expect(categoryText(summary("NOT_CONNECTED"))).toBe("미연결");
    expect(categoryText(summary("NOT_CONFIGURED"))).toBe("설정 필요");
    expect(categoryText(summary("ERROR"))).toBe("불러오지 못함");
    expect(categoryText(summary("ATTENTION"))).toBe("0건");
  });

  it("미연결을 '이상 없음' 으로 표현하지 않는다", () => {
    // 이게 이 파일의 이유다. 둘이 같아지면 거짓말이 된다.
    expect(categoryText(summary("NOT_CONNECTED"))).not.toBe(
      categoryText(summary("OK")),
    );
    expect(categoryText(summary("NOT_CONFIGURED"))).not.toBe(
      categoryText(summary("OK")),
    );
  });

  it("건수가 있으면 건수를 쓴다", () => {
    expect(categoryText(summary("ATTENTION", 3))).toBe("3건");
  });

  it("미연결이면 건수가 있어도 건수를 쓰지 않는다", () => {
    // 셀 수 없다고 말한 뒤 숫자를 보여주면 앞말이 무의미해진다.
    expect(categoryText(summary("NOT_CONNECTED", 5))).toBe("미연결");
  });
});
