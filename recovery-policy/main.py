"""신호 수신 엔드포인트 — 지금은 배포 구조(1단계)를 테스트할 수 있게
/healthz만 있는 최소 스텁이다. 실제 /signals/anomaly, /webhooks/alertmanager는
5단계(입력 어댑터 구현)에서 policy.py/safety.py/decision_log.py와 함께 붙인다.
"""
from fastapi import FastAPI

app = FastAPI()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
