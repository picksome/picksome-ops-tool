/**
 * 증빙 파일 클라이언트 단위 테스트.
 *
 * 여기서 막는 사고
 *   · 10MB 초과·허용 안 되는 형식을 서버까지 보내고 나서 거절당하는 것
 *   · 스캐너가 없는데 화면이 "안전" 이라고 말하는 것
 *   · 삭제 승인 요청이 잘못된 target_type 으로 나가는 것
 */

import { describe, expect, it } from "vitest";
import {
  ALLOWED_EXTENSIONS, ATTACHMENT_STATUS_LABEL, SCAN_STATUS_LABEL,
  precheckFile, type AttachmentStatus, type ScanStatus,
} from "@/lib/api/attachments";
import { REQUEST_TYPE_LABELS, TARGET_TYPE_LABELS } from "@/lib/api/approvals";

const MAX = 10 * 1024 * 1024;

function file(name: string, size: number): File {
  // jsdom 의 File 은 size 를 blob 내용에서 계산한다. 큰 파일을 실제로 만들면
  // 테스트가 느려지므로 size 만 덮어쓴다.
  const f = new File(["x"], name);
  Object.defineProperty(f, "size", { value: size });
  return f;
}

describe("업로드 선검증 — 왕복을 아끼되 서버 검증을 대신하지 않는다", () => {
  it("허용 확장자는 통과한다", () => {
    for (const ext of ALLOWED_EXTENSIONS) {
      expect(precheckFile(file(`a.${ext}`, 1024), { maxBytes: MAX })).toBeNull();
    }
  });

  it("대문자 확장자도 통과한다", () => {
    // 아이폰에서 온 파일은 `.HEIC` 인 경우가 많다.
    expect(precheckFile(file("IMG_0001.HEIC", 1024), { maxBytes: MAX })).toBeNull();
    expect(precheckFile(file("scan.PDF", 1024), { maxBytes: MAX })).toBeNull();
  });

  it("허용하지 않는 확장자는 거른다", () => {
    const r = precheckFile(file("payload.exe", 1024), { maxBytes: MAX });
    expect(r?.reason).toContain("jpg");
  });

  it("확장자가 없으면 거른다", () => {
    expect(precheckFile(file("noext", 1024), { maxBytes: MAX })).not.toBeNull();
  });

  it("10MB 초과는 올리기 전에 막는다", () => {
    const r = precheckFile(file("big.pdf", MAX + 1), { maxBytes: MAX });
    expect(r?.reason).toContain("10MB");
  });

  it("정확히 10MB 는 허용한다", () => {
    // 경계에서 막으면 "10MB 이하" 라는 안내가 거짓말이 된다.
    expect(precheckFile(file("edge.pdf", MAX), { maxBytes: MAX })).toBeNull();
  });

  it("빈 파일은 막는다", () => {
    expect(precheckFile(file("empty.png", 0), { maxBytes: MAX })?.reason)
      .toContain("빈 파일");
  });

  it("확장자 위장은 여기서 잡지 않는다 — 내용 검사는 서버 몫이다", () => {
    // 이름만 보면 통과한다. 그래서 서버가 magic byte 를 본다.
    expect(precheckFile(file("evil.pdf", 1024), { maxBytes: MAX })).toBeNull();
  });
});

describe("상태 라벨 — 하지 않은 일을 했다고 말하지 않는다", () => {
  it("모든 상태에 라벨이 있다", () => {
    const statuses: AttachmentStatus[] = [
      "PENDING_SCAN", "AVAILABLE", "PENDING_DELETE", "DELETED", "FAILED",
    ];
    for (const s of statuses) expect(ATTACHMENT_STATUS_LABEL[s]).toBeTruthy();

    const scans: ScanStatus[] = [
      "SCAN_UNAVAILABLE", "PENDING", "CLEAN", "INFECTED", "SCAN_FAILED",
    ];
    for (const s of scans) expect(SCAN_STATUS_LABEL[s]).toBeTruthy();
  });

  it("검사하지 않은 파일을 안전하다고 하지 않는다", () => {
    // 스캐너가 없는데 "안전"·"통과" 라고 쓰면 사용자는 검사를 통과했다고 믿는다.
    expect(SCAN_STATUS_LABEL.SCAN_UNAVAILABLE).toBe("검사 안 함");
    expect(SCAN_STATUS_LABEL.SCAN_UNAVAILABLE).not.toContain("안전");
    expect(SCAN_STATUS_LABEL.SCAN_UNAVAILABLE).not.toContain("통과");
  });

  it("삭제 대기와 삭제 완료를 구분한다", () => {
    // 파일이 아직 있는데 "삭제됨" 으로 보이면 사용자는 지워진 줄 안다.
    expect(ATTACHMENT_STATUS_LABEL.PENDING_DELETE)
      .not.toBe(ATTACHMENT_STATUS_LABEL.DELETED);
  });
});

describe("증빙 삭제 승인 — 승인함이 무엇인지 읽을 수 있어야 한다", () => {
  it("첨부 대상에 한글 라벨이 있다", () => {
    expect(TARGET_TYPE_LABELS.expense_attachment).toBeTruthy();
    expect(TARGET_TYPE_LABELS.expense_attachment).not.toContain("_");
  });

  it("증빙 변경 요청 유형에 라벨이 있다", () => {
    expect(REQUEST_TYPE_LABELS.EXPENSE_EVIDENCE_CHANGE).toBe("증빙 변경");
  });
});
