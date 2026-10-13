"""공급처 Excel 반영 CLI (개발용).

```
python -m scripts.import_supplier_sites <파일> --org <UUID> --actor <UUID>
       --mode analyze     파일만 읽는다. DB 를 보지도 않는다
       --mode dry-run     DB 와 대조해 무엇이 바뀔지 계산한다 (기본)
       --mode apply       실제로 반영한다
```

세 단계를 나눈 이유
----------------
불리언 하나(`--apply`)로 운영 데이터가 바뀌는 구조는 실수를 부른다. 그리고
`analyze` 는 DB 없이도 돌아야 한다 — 파일을 처음 받았을 때 "이 시트를 우리가
읽을 수는 있나" 부터 확인하게 된다.

**운영에서는 `apply` 가 막힌다.** CLI 한 줄로 운영 공급처 마스터가 바뀌는 경로를
남기지 않는다(`assert_import_allowed`). 미리보기는 운영에서도 된다 — 못 하게
하면 사람이 결국 우회한다.

⚠️ 출력에 비밀이 없다
------------------
Excel 의 `ID`/`PW` 열은 읽어서 버린다. 화면에도 로그에도 값이 나오지 않고,
"자격증명 열 2개를 버렸다" 는 사실만 나온다.
"""

from __future__ import annotations

import argparse
import sys
from uuid import UUID

import openpyxl

from app.db.session import SessionLocal
from app.services.supplier_import import (
    ImportNotAllowedError,
    import_suppliers,
    parse_supplier_sheet,
    record_import_run,
    sha256_of,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="공급처 Excel → supplier_site")
    ap.add_argument("path")
    ap.add_argument("--sheet", default=None, help="시트 이름 (기본: 첫 시트)")
    ap.add_argument("--org", required=True)
    ap.add_argument("--actor", required=True)
    ap.add_argument(
        "--mode", default="dry-run", choices=["analyze", "dry-run", "apply"],
        help="analyze=파일만 / dry-run=미리보기(기본) / apply=실제 반영",
    )
    args = ap.parse_args()
    mode = {"analyze": "ANALYZE", "dry-run": "DRY_RUN", "apply": "APPLY"}[args.mode]

    wb = openpyxl.load_workbook(args.path, data_only=True, read_only=True)
    ws = wb[args.sheet] if args.sheet else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    digest = sha256_of(args.path)

    report = parse_supplier_sheet(rows)
    print(f"시트 '{ws.title}' — 공급처 {len(report.rows)}건  (sha256 {digest[:12]}…)")
    if report.dropped_credential_headers:
        print("  자격증명 열은 읽지 않고 버렸습니다: "
              + ", ".join(report.dropped_credential_headers))
    if report.unmapped_headers:
        # 추측해서 넣지 않는다. 사람이 매핑을 정해야 한다.
        print("  매핑되지 않은 열(무시됨): " + ", ".join(report.unmapped_headers))
    if report.skipped_rows:
        print("  ⚠️ 다른 값은 있는데 이름이 없어 버린 행: "
              + ", ".join(str(r) for r in report.skipped_rows))
    if report.blank_rows:
        print(f"  빈 행 {report.blank_rows}개는 무시했습니다(시트 여백).")
    for row_no, raw in report.unparsable_urls:
        print(f"  웹사이트 칸이 주소가 아닙니다 (행 {row_no}): {raw!r} — 그대로 뒀습니다")

    unknown_rating = sum(1 for r in report.rows if r.rating is None)
    with_login = sum(1 for r in report.rows if r.credential_present)
    print(f"  평점 미기입(모름): {unknown_rating}건 · 로그인 계정 있음: {with_login}건")

    if mode == "ANALYZE":
        # DB 를 보지 않는다. 파일을 읽을 수 있는지까지가 이 단계다.
        print("[분석] DB 를 조회하지 않았습니다. 변경 예정을 보려면 --mode dry-run.")
        return 0

    org_id, actor_id = UUID(args.org), UUID(args.actor)
    db = SessionLocal()
    try:
        result = import_suppliers(
            db, org_id=org_id, actor_user_id=actor_id,
            rows=report.rows, mode=mode,
        )
        record_import_run(
            db, org_id=org_id, actor_user_id=actor_id, mode=mode,
            source_name=args.path, source_sha256=digest, sheet_name=ws.title,
            report=report, result=result,
        )
        # 실행 기록은 미리보기에서도 남긴다 — 누가 무엇을 들여다봤는지도 기록이다.
        db.commit()
    except ImportNotAllowedError as exc:
        db.rollback()
        print(f"[거부] {exc}")
        return 2
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    label = "반영" if mode == "APPLY" else "미리보기"
    print(f"[{label}] 신규 {result.created} · 빈칸 채움 {result.filled} · "
          f"변화 없음 {result.unchanged} · 값이 달라 확인 필요 {result.conflicted}행 "
          f"({len(result.differences)}건)")
    for d in result.differences[:20]:
        print(f"    - {d['name']} / {d['field']}: 우리={d['current']!r} "
              f"Excel={d['excel']!r} (덮지 않음)")
    if len(result.differences) > 20:
        print(f"    … 외 {len(result.differences) - 20}건")
    if mode != "APPLY":
        print("  실제로 반영하려면 --mode apply 를 쓰세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
