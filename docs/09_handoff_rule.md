# Handoff Rule (공통)

각 역할은 작업이 끝날 때 **반드시 아래 형식으로** 다음 역할에게 인수인계한다.

이 규칙은 모든 역할(PM, Planner, Designer, Frontend, Backend, DevOps, DBA, User/세무)에 동일하게 적용된다.

---

## Handoff Template

```markdown
# Handoff: {From Role} → {To Role}

## Summary
이번 역할에서 결정한 핵심 내용을 3~5줄로 요약한다.

## Decisions
- 확정된 결정 1
- 확정된 결정 2
- ...

## Open Questions
- 아직 결정되지 않은 질문 1
- 아직 결정되지 않은 질문 2
- ...

## Risks
- 다음 단계에서 주의해야 할 리스크 1
- 다음 단계에서 주의해야 할 리스크 2
- ...

## Required Inputs for Next Role
다음 역할이 작업하기 위해 필요한 입력값.
- 산출물 A (어디서 가져오는지)
- 데이터 B (형식 명시)
- ...

## Handoff Target
다음 역할명: {예: Backend}
```

---

## 작성 규칙

- **Summary**는 3~5줄. 한 문장당 한 결정.
- **Decisions**는 합의·확정된 항목만 적는다 (가설 금지).
- **Open Questions**는 PM/Architect에게 에스컬레이션할 항목이다.
- **Risks**는 다음 역할이 모르면 손해보는 것을 적는다.
- **Required Inputs**는 "다음 역할이 무엇을 들고 시작해야 하는가"이다.
- **Handoff Target**은 단일 역할명을 적는다. 복수면 줄을 나눠 여러 Handoff를 작성한다.

## 누락 시 PM의 행동

PM/Architect는 Handoff가 누락되거나 부실하면
다음 역할로의 진행을 **중단**하고 이전 역할에게 보완을 요구한다.

## 라벨링 (structured-thinking 스킬 연동)

Decisions / Open Questions 항목에는 필요 시 라벨을 붙인다.

- `[Confirmed]` 사용자/엑셀에서 명확히 확인된 사실
- `[Inferred]` 합리적 추론
- `[Uncertain]` 모름 — 검증 필요
