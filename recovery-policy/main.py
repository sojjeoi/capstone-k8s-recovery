"""Phase 7 recovery-policy 서비스. 예측 경로(/signal — score_server.py가
실제로 이 경로로 POST함)와 반응 경로(/webhooks/alertmanager)를 공통
NormalizedSignal로 바꾼 뒤 policy.decide()로 조치를 정하고 safety.py로
중복/쿨다운을 거른다. promote는 rollouts_client를 통해 실행하고, 모든
판단은 decision_log로 남겨 audit-log/에 기록한다.

git_client.py(8단계)가 아직 없어서 audit-log/ 기록은 여기서 직접 append만
한다 - git add/commit/push는 8단계에서 git_client.py가 맡는다(guideline.md
저장소 구조: "git_client.py: decision_log 레코드를 audit-log/에 쓰고
git add·commit·push"). 파일명 규칙(`{experiment_run_id}.jsonl`)은 그대로
따르되, 지금 들어오는 신호는 experiment_run_id가 없으므로(Phase 8
오케스트레이터가 나중에 채움) "adhoc"으로 묶는다.

rule-out(반대증거) 체크는 아직 실제 근거 수집 로직이 없어 항상 False다 -
policy.PolicyContext.has_contradicting_evidence를 세팅하는 지점을 남겨는
뒀지만, 무엇을 반대증거로 볼지(예: 최근 배포 이력)는 이번 5~6단계 범위 밖.
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from fastapi import FastAPI

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
AUDIT_LOG_DIR = Path(__file__).parent.parent / "audit-log"


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


def _write_audit_log(signal: NormalizedSignal, record: DecisionRecord) -> None:
    AUDIT_LOG_DIR.mkdir(exist_ok=True)
    run_id = signal.raw.get("experiment_run_id") or "adhoc"
    path = AUDIT_LOG_DIR / f"{run_id}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(record.model_dump_json() + "\n")


def process_signal(signal: NormalizedSignal) -> DecisionRecord:
    if not safety.check_and_reserve(signal.idempotency_key):
        record = build(signal, action=None, outcome=Outcome.SKIPPED_DUPLICATE,
                        reasoning="idempotency_key 중복 - 이미 처리된 신호")
        _write_audit_log(signal, record)
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
        _write_audit_log(signal, record)
        return record

    if decision.action == policy.ACTION_OBSERVE_ONLY:
        record = build(signal, action=decision.action, outcome=Outcome.NO_ACTION, reasoning=decision.reasoning)
        _write_audit_log(signal, record)
        return record

    # 여기부터 ACTION_PROMOTE_PREVIEW
    if safety.in_action_cooldown():
        record = build(signal, action=decision.action, outcome=Outcome.SKIPPED_COOLDOWN, reasoning=decision.reasoning)
        _write_audit_log(signal, record)
        return record

    result = promote(ROLLOUT_NAME, NAMESPACE)
    safety.mark_action_taken()
    outcome = Outcome.EXECUTED_VERIFIED if result.get("verified") else Outcome.EXECUTED_UNVERIFIED
    record = build(signal, action=decision.action, outcome=outcome, result=result, reasoning=decision.reasoning)
    _write_audit_log(signal, record)
    return record


@app.post("/signal")
def receive_anomaly_signal(req: AnomalySignalRequest):
    return process_signal(normalize_anomaly_signal(req))


@app.post("/webhooks/alertmanager")
def receive_alertmanager_webhook(req: AlertmanagerWebhookRequest):
    return {"processed": [process_signal(s) for s in normalize_alertmanager_webhook(req)]}
