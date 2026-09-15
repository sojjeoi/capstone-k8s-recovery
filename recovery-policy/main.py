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
POST /admin/experiment-run으로 "지금 진행 중인 실험"을 등록하면, 그 사이에
들어온 Alertmanager 신호는 이 값을 experiment_run_id로 쓴다.

이 ambient 방식의 한계(문서화): 이벤트 자체에 correlation ID가 없어서
프로세스 메모리의 "현재 실험 1개" 상태에 의존한다 - 여러 실험이 동시에
도는 운영 환경이었다면 이벤트에 직접 correlation ID를 실어야 맞다. Phase 8은
실험을 순차(한 번에 1개) 실행하므로 이 정도로 충분하다고 판단했다. 대신
아래 안전장치를 둔다:
- 동시에 활성 실험은 1개만(등록 중 다른 run_id로 재등록 시도 시 409)
- 같은 run_id 재등록은 idempotent(그냥 200)
- clear는 run_id를 받아 현재 값과 일치할 때만 지움(늦게 도착한 이전 trial의
  clear가 다음 trial의 등록을 실수로 지우는 걸 방지)
- recovery-policy 재시작으로 컨텍스트가 사라지면 그 trial은 오케스트레이터가
  invalid_run으로 처리(재시작 = 메모리 상태 소실은 설계상 당연한 결과)
- Alert의 startsAt이 현재 등록된 실험의 started_at보다 이전이면 태깅 안 함
  (이전 trial에서 새어든 stale alert 방지)
- 이 엔드포인트는 ClusterIP로만 노출돼 클러스터 외부에서 접근 불가(service.yaml)
  - 별도 토큰 인증은 안 둠(단일 신뢰된 오케스트레이터, 외부 노출 없음)
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from datetime import datetime
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

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
ALERTMANAGER_URL = "http://kube-prom-kube-prometheus-alertmanager.monitoring.svc.cluster.local:9093"


class ExperimentContext(BaseModel):
    run_id: str
    scenario: str
    arm: str
    rep: int
    started_at: datetime


_current_experiment: Optional[ExperimentContext] = None


@app.on_event("startup")
def _on_startup():
    git_client.start_worker()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/admin/quiescent")
def check_quiescent():
    """run_once()가 trial 시작 전/정리 후 호출 - vllm-serving 관련 critical
    Alert가 하나도 없어야 True. 이전 trial의 알림이 다음 trial로 새는 걸
    막는 quiescence 확인용(계약서 §6).

    실측(2026-09-16)으로 발견: 필터 없이 전체 active alert를 보면 이
    클러스터는 처음부터(2026-09-04) etcdInsufficientMembers/KubeProxy
    InstanceUnreachable/Watchdog 등 control-plane 기본 알림이 계속 떠있어서
    quiescent가 영원히 False가 됨 - 단일 노드 kubeadm 클러스터의 흔한
    노이즈고 recovery-policy와 무관함. alertmanagerconfig.yaml이 실제로
    라우팅하는 조건(severity=critical, namespace=vllm-serving)과 동일한
    필터를 걸어서 우리 실험과 무관한 알림은 무시한다.

    Alertmanager API 자체가 응답 안 하면 안전 쪽으로 quiescent=False를
    돌려준다(연결 문제를 "조용하다"로 오판하면 안 됨)."""
    try:
        resp = requests.get(
            f"{ALERTMANAGER_URL}/api/v2/alerts",
            params={"active": "true", "filter": ["severity=critical", "namespace=vllm-serving"]},
            timeout=10,
        )
        resp.raise_for_status()
        active_alerts = resp.json()
        return {"quiescent": len(active_alerts) == 0, "active_count": len(active_alerts)}
    except Exception as e:
        return {"quiescent": False, "active_count": None, "error": str(e)}


@app.get("/admin/experiment-run")
def get_experiment_run():
    """run_once()가 trial 시작 전 "활성 context 없음"을 명시적으로 확인하는
    용도(계약서 §6 순서: quiescence -> 활성 context 없음 확인 -> cooldown
    초기화). current가 null이 아니면 다른 trial이 아직 안 끝났다는 뜻."""
    return {"current": _current_experiment}


@app.post("/admin/reset-cooldown")
def reset_action_cooldown():
    """run_once()가 trial 시작 전(주입 전) 호출 - action cooldown만
    초기화한다(idempotency 기록은 안 건드림, safety.reset_cooldown() 참고).
    각 trial은 독립 표본이어야 하므로, 이전 trial(다른 arm일 수 있음)의
    promotion이 남긴 cooldown이 이번 trial의 조치를 막아 비교를 왜곡하면
    안 된다.

    안전을 위해 quiescent하고 활성 experiment context가 없을 때만 허용한다
    - 클라이언트(run_once())가 순서를 지켰다고 믿지 않고 서버에서도
    재확인한다(다른 admin 엔드포인트들과 같은 방어적 패턴)."""
    if _current_experiment is not None:
        raise HTTPException(
            status_code=409,
            detail=f"활성 실험 있음: {_current_experiment.run_id} - cooldown 초기화 거부",
        )
    quiescence = check_quiescent()
    if not quiescence["quiescent"]:
        raise HTTPException(
            status_code=409,
            detail=f"quiescent 아님(active_count={quiescence['active_count']}) - cooldown 초기화 거부",
        )
    safety.reset_cooldown()
    return {"status": "cooldown_reset"}


@app.post("/admin/experiment-run")
def start_experiment_run(ctx: ExperimentContext):
    """run_once()가 chaos 주입 직전에 호출. 이미 다른 run_id가 활성 중이면
    409(오케스트레이터가 quiescence를 안 지켰다는 뜻 - 버그로 취급해야 함)."""
    global _current_experiment
    if _current_experiment is not None and _current_experiment.run_id != ctx.run_id:
        raise HTTPException(
            status_code=409,
            detail=f"다른 실험이 이미 활성 중: {_current_experiment.run_id} (요청: {ctx.run_id})",
        )
    _current_experiment = ctx
    return {"status": "active", "current": _current_experiment}


@app.post("/admin/experiment-run/clear")
def clear_experiment_run(run_id: str):
    """run_once()가 trial 정리 단계에서 호출. run_id가 현재 활성값과 일치할
    때만 지운다 - 안 그러면 늦게 도착한 이전 trial의 clear가 다음 trial을
    실수로 지울 수 있다."""
    global _current_experiment
    if _current_experiment is None:
        return {"status": "already_clear"}
    if _current_experiment.run_id != run_id:
        raise HTTPException(
            status_code=409,
            detail=f"현재 활성 run_id({_current_experiment.run_id})와 불일치(요청: {run_id})",
        )
    _current_experiment = None
    return {"status": "cleared"}


def process_signal(signal: NormalizedSignal) -> DecisionRecord:
    if not signal.raw.get("experiment_run_id") and _current_experiment is not None:
        if signal.received_at >= _current_experiment.started_at:
            signal.raw["experiment_run_id"] = _current_experiment.run_id

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
