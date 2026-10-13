"""운영 스크립트 (CLI 진입점).

외부 스케줄러(cron / systemd timer / 컨테이너 스케줄러)가 호출한다.
애플리케이션 프로세스 안에 스케줄러를 두지 않는 이유는
`app/services/scheduler.py` 와 `docs/25_devops.md` 를 참고.
"""
