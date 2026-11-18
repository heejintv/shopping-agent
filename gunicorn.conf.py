"""gunicorn 설정 (Render 등 배포용).  실행: gunicorn app:app

- 워커 1개 + 스레드 여러 개: 진행 중인 검색·취소 신호를 프로세스 메모리에 두므로 워커는 하나여야 함.
  스레드로 동시 요청과 진행 상황 스트림(/search/stream)을 함께 처리
- gthread 워커는 요청이 길어도 워커를 죽이지 않음. timeout은 워커가 멈췄는지 보는 시간(300초)
- 스트림은 서버가 1초마다 ping을 보내므로 프록시가 연결을 끊지 않음
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "8"))
timeout = 300
graceful_timeout = 30
keepalive = 75
accesslog = "-"
errorlog = "-"
loglevel = "info"
