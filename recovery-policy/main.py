"""Phase 7 recovery-policy 서비스. 예측 경로(/signal — score_server.py가
실제로 이 경로로 POST함)와 반응 경로(/webhooks/alertmanager)를 공통
NormalizedSignal로 바꾼 뒤 policy.decide()로 조치를 정하고 safety.py로
중복/쿨다운을 거른다. promote는 rollouts_client를 통해 실행하고, 모든
판단은 decision_log로 남긴 뒤 git_client.enqueue()로 PVC에 기록 + 비동기
Git 커밋·푸시 큐에 올린다(8단계, guideline.md 9-6절: PVC 기반 단순 감사
outbox). git 지연·실패가 이 함수의 반환(=HTTP 응답)을 막지 않는다.

rule-out(반대증거) 체크는 아직 실제 근거 수집 로직이 없어 항상 False다 -
policy.PolicyContext.has_contradicting_evidence를 세팅하는 지점을 남겨는
뒀지만, 무엇을 반대증거로 볼지(예: 최근 배포 이력)는 이번 5~6단계 범위 밖.

Phase 8 experiment_run_id 전파(/signal 경로): score_server.py/fixed_threshold.py가
JSON payload에 직접 실어 보내므로 schemas.py가 이미 처리한다.

Phase 8 experiment_run_id 전파(/webhooks/alertmanager 경로): Alertmanager
alert에는 실험 메타데이터를 실을 자리가 없다(PrometheusRule 라벨은 정적이라
trial마다 동적으로 못 바꿈). 대신 오케스트레이터가 chaos 주입 직전에
POST /admin/experiment-run으로 "지금 진행 중인 실험"을 알려주면, 그 사이에
들어온 Alertmanager 신호는 이 값을 experiment_run_id로 쓴다(trial 종료 시
오케스트레이터가 다시 null로 지움 - 다음 trial로 새는 것 방지).
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from fastapi import FastAPI

import git_client
import policy
import safety
from decision_log import DecisionRecord, Outcome, build
from rollouts_client import is_paused_pre_promotion, promote
from schemas import (
    AlertmanagerWebhookRequest,
    AnomalySignalRequest,
    NormalizedSignal,
    normalize_alertmanager_webhook,
    normalize_anomaly_signal,
)

app = FastAPI()

ROLLOUT_NAME = "vllm-serving"
NAMESPACE = "vllm-serving"
KNOWN_SIGNAL_TYPES = {"anomaly_risk", "VLLMTargetDown", "VLLMTargetMissing"}

_current_experiment_run_id: str = None


@app.on_event("startup")
def _on_startup():
    git_client.start_worker()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/admin/experiment-run")
def set_experiment_run(run_id: str = None):
    """Phase 8 오케스트레이터 전용 - chaos 주입 직전에 run_id로, trial 종료 후엔
    인자 없이(null로) 호출한다. Alertmanager 경로의 run_id 태깅에만 쓰인다."""
    global _current_experiment_run_id
    _current_experiment_run_id = run_id
    return {"current_experiment_run_id": _current_experiment_run_id}


def process_signal(signal: NormalizedSignal) -> DecisionRecord:
    if not signal.raw.get("experiment_run_id") and _current_experiment_run_id:
        signal.raw["experiment_run_id"] = _current_experiment_run_id

    if not safety.check_and_reserve(signal.idempotency_key):
        record = build(signal, action=None, outcome=Outcome.SKIPPED_DUPLICATE,
                        reasoning="idempotency_key 중복 - 이미 처리된 신호")
        git_client.enqueue(signal, record)
        return record

    preview_ready = is_paused_pre_promotion(ROLLOUT_NAME, NAMESPACE)
    ctx = policy.PolicyContext(preview_ready=preview_ready)
    decision = policy.decide(signal, ctx)

    if decision.action is None:
        outcome = (
            Outcome.SKIPPED_RULE_OUT
            if signal.signal_type in KNOWN_SIGNAL_TYPES
            else Outcome.SKIPPED_UNKNOWN_SIGNAL
        )
        record = build(signal, action=None, outcome=outcome, reasoning=decision.reasoning)
        git_client.enqueue(signal, record)
        return record

    if decision.action == policy.ACTION_OBSERVE_ONLY:
        record = build(signal, action=decision.action, outcome=Outcome.NO_ACTION, reasoning=decision.reasoning)
        git_client.enqueue(signal, record)
        return record

    # 여기부터 ACTION_PROMOTE_PREVIEW
    if safety.in_action_cooldown():
        record = build(signal, action=decision.action, outcome=Outcome.SKIPPED_COOLDOWN, reasoning=decision.reasoning)
        git_client.enqueue(signal, record)
        return record

    result = promote(ROLLOUT_NAME, NAMESPACE)
    safety.mark_action_taken()
    outcome = Outcome.EXECUTED_VERIFIED if result.get("verified") else Outcome.EXECUTED_UNVERIFIED
    record = build(signal, action=decision.action, outcome=outcome, result=result, reasoning=decision.reasoning)
    git_client.enqueue(signal, record)
    return record


@app.post("/signal")
def receive_anomaly_signal(req: AnomalySignalRequest):
    return process_signal(normalize_anomaly_signal(req))


@app.post("/webhooks/alertmanager")
def receive_alertmanager_webhook(req: AlertmanagerWebhookRequest):
    return {"processed": [process_signal(s) for s in normalize_alertmanager_webhook(req)]}
