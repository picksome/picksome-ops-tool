# 33. 기획 / 문서 / 프로세스 기술 스택 (Planner)

> PICKSOME OPS TOOL 기획·문서·의사결정·역할 분리에 사용한 도구와 채택 이유.
> 작성일 2026-06-22 / 작성자 Planner Role.

---

## 1. 문서 시스템

| 분류 | 도구 / 방식 | 채택 이유 |
|------|-------------|----------|
| 문서 포맷 | **Markdown (GFM)** | 코드 친화, 버전 관리(git) 자연스러움, AI 친화 |
| 도식 | Mermaid (md 내장) | 텍스트로 ERD/플로우 작성 → 코드 리뷰 가능 |
| HTML preview | `docs/*.html` (수동 export) | 비기술자 공유용 |
| 위치 | `docs/<번호>_<역할/주제>.md` | 번호 prefix 로 정렬 + 의존 순서 시각화 |

---

## 2. 8역할 분리 원칙 (CLAUDE.md Working Rule)

| # | 역할 | 산출물 위치 |
|---|------|------------|
| 0 | PM / Architect | docs/01_pm_architect_role.md + 10_decision_log.md |
| 1 | Planner | docs/02 + 20_prd.md |
| 2 | Designer | docs/03 + 23_design.md |
| 3 | Frontend | docs/04 + 24_frontend.md + 31_tech_frontend.md |
| 4 | Backend | docs/05 + 22_api_spec.md + 30_tech_backend.md |
| 5 | DevOps / SE | docs/06 + 25_devops.md |
| 6 | DBA | docs/07 + 21_erd.md + 32_tech_database.md + db_schema/*.yaml |
| 7 | User / 세무 | docs/08 + 26_user_verification.md |

**원칙**: 한 번에 한 역할만. 산출물은 해당 역할 책임 범위 안에서만 작성. 다음 역할로 인수인계는 docs/09_handoff_rule.md 템플릿 사용.

---

## 3. 의사결정 추적

| 분류 | 도구 |
|------|------|
| 의사결정 로그 | **docs/10_decision_log.md** — 모든 D-XXX 결정 누적 |
| Open Items | 동일 문서 내 "Open Items" 섹션 — Confirmed/Pending/Rejected |
| 확정된 결정 (D-280~D-286) | 2026-06-18 일괄 Confirmed |
| 확정 라벨 | `[Confirmed]` / `[Inferred]` / `[Uncertain]` (structured-thinking skill) |

---

## 4. structured-thinking 스킬 (CLAUDE.md Skill Rule)

모든 작업에 적용:

1. **Reframed request** — 요청을 명확한 형태로 재정리
2. **정보 라벨링** — `[Confirmed]` (확정) / `[Inferred]` (추론) / `[Uncertain]` (불확실)
3. **구체적 산출물** — 분석만으로 끝내지 않음
4. **Action checklist** — 마지막에 다음 행동 정리

위치: `.claude/skills/structured-thinking`

---

## 5. 핵심 설계 원칙

| 원칙 | 출처 | 적용 |
|------|------|------|
| **Generalization** | CLAUDE.md | 비즈니스 값 0 하드코딩, 정책 테이블 + version/effective_from |
| **박제 (Snapshot)** | CLAUDE.md Calculation Rules | 모든 계산 결과에 정책 버전 snapshot 부착 → 과거 재현 가능 |
| **순이익 기준 배분** | CLAUDE.md Settlement Model | 매출이 아닌 (매출 - 직접비 - 공통비) 기준 |
| **UUID 노출 금지** | D-283 | 사용자에게 보이는 ID 는 business_key |
| **세무 자동신고 X** | CLAUDE.md Tax Report | "세무사 검토용 자료 제공" 범위로 한정 |
| **CLAUDE.md Security** | 보안 | 평문 비밀번호 금지, vault 는 보관 위치만 |
| **8역할 분리** | CLAUDE.md Working Rule | 한 번에 한 역할, 사용자 승인 필수 |

---

## 6. 인수인계 (Handoff)

`docs/09_handoff_rule.md` 표준 템플릿:
1. 산출물 위치
2. 적용된 결정 (D-XXX)
3. 다음 역할의 책임 범위
4. 알려진 미확정 사항

---

## 7. 엑셀 기반 분석

| 단계 | 산출물 |
|------|--------|
| 원본 엑셀 자동 분석 | `docs/_excel_analysis.md` |
| 기획 세션 메모 | `docs/_planning_session.md` |
| 스키마 설계 메모 | `docs/_schema_design.md` |

**Core Principle**: 엑셀 화면을 복사하지 않는다. 엑셀의 **업무 프로세스 + 계산 로직** 만 재설계한다 (CLAUDE.md).

---

## 8. AI 협업 인터페이스

| 도구 | 용도 |
|------|------|
| **Claude Code** (CLI) | 코드 생성 / 문서 작성 / 8역할 시뮬레이션 |
| 메모리 (`.claude/projects/...`) | 사용자 선호 + 진행상황 + 의사결정 영속화 |
| 슬래시 명령 (`/loop`, `/fast` 등) | 반복 작업 자동화 |
| Skill (`structured-thinking`) | 분석 품질 표준화 |

**AI 친화 산출물**:
- 모든 문서 markdown — context 입력 효율
- YAML 테이블 명세 (`docs/db_schema/*.yaml`) — 모델 코드 자동 생성/검증 가능
- 결정 로그 D-XXX 라벨 — 과거 의사결정 추적

---

## 9. 버전 관리 / 산출물 추적

| 분류 | 도구 |
|------|------|
| 코드 + 문서 | git (현재 비-repo 상태, init 예정) |
| 결정 변경 이력 | docs/10_decision_log.md 누적 (절대 삭제 금지) |
| 마이그레이션 버전 | Alembic M001/M002/M003... revision chain |
| 사용자 노출 ID 체계 | business_key (D-283) |

---

## 10. 작업 진행 추적

| 도구 | 용도 |
|------|------|
| `docs/_planning_session.md` | 기획 단계 회의록 |
| Claude memory `project_work_order.md` | 13단계 진행 현황 |
| Claude memory `project_open_items.md` | Open Items 7건 Confirmed 추적 |
| CLAUDE.md "Work Order" 섹션 | 1~13 단계 체크리스트 |

---

## 11. 채택하지 않은 도구 (의도적)

| 선택지 | 채택 안 함 이유 |
|--------|---------------|
| Notion / Confluence | git 외부 도구 → 버전 관리 분기 |
| Jira / Linear | 1~2인 운영 규모. CLAUDE.md + memory 로 충분 |
| Figma 디자인 스펙 | 운영툴이라 디자인 의존도 낮음. shadcn 표준 컴포넌트로 진행 |
| ADR (Architecture Decision Records) 별도 폴더 | docs/10_decision_log.md 단일 파일로 압축 관리 |
| Swagger UI 외부 운영 | FastAPI 가 `/api/docs` 자동 제공 — 별도 도구 불필요 |

---

## 12. 다음 단계 (Planner 관점)

- [ ] 사용자 검증 회차 2 (실제 픽썸 운영 데이터 1개월 입력 후 정확성 검증)
- [ ] SaaS 출시 전 외부 셀러 1~2명 베타 테스트
- [ ] CLAUDE.md "Resume Point" 갱신 — 13-6 Frontend 진입 시점
- [ ] 결정 로그 D-287 이후 (예: 외부 API 인증 방식, 다국가 통화) Open Items 작성
