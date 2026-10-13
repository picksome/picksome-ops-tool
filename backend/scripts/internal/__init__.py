"""내부 전용 스크립트 (SaaS 배포 미포함).

이 디렉토리의 모든 파일은 픽썸 내부 운영을 위한 일회성 적재/유틸리티이며,
SaaS 빌드/이미지 생성 시 제외해야 한다.

제외 방법:
  - Docker 이미지: COPY 시 scripts/internal/ 제외
  - Python 패키지: pyproject.toml 의 [tool.hatch.build] 에서 exclude
"""
