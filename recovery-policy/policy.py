"""신호 유형별 조치 결정 - 규칙 기반, LLM 미사용(guideline.md). 재현 가능해야
하므로 순수 함수로 만든다 - 클러스터 상태 조회(preview 준비 여부 등)는
호출자(main.py, 5단계)가 미리 해서 PolicyContext로 넘긴다.

reactive_restart/reactive_redeploy는 뺐다 - 그걸 실행할 클라이언트가 아직
없는데(rollouts_client.py는 promote만 지원) 정책에만 있으면 리뷰에서 지적된
"정직하지 않은" 상태가 된다. 필요해지면 클라이언트부터 만들고 추가할 것.

실제 alert 이름(VLLMTargetDown/VLLMTargetMissing - prometheusrule.yaml)에
맞춰 매핑한다. guideline.md 원래 의사코드의 PodCrashLooping/PodOOMKilled는
실제 배포된 규칙과 달라서(리뷰 지적) 안 쓴다.
"""
from dataclasses import dataclass
from typing import Optional

from schemas import NormalizedSignal

ACTION_PROMOTE_PREVIEW = "promote_preview"
ACTION_OBSERVE_ONLY = "observe_only"


@dataclass
class PolicyContext:
    """정책 결정에 필요한, policy.py 바깥에서 조회해 넘겨주는 상태."""
    preview_ready: bool = False  # rollouts_client.is_paused_pre_promotion() 결과
    has_contradicting_evidence: bool = False  # rule-out 체크 결과(배포 이력 없음 등)


@dataclass
class Decision:
    action: Optional[str]  # None = 조치 없음(정의 안 된 신호 또는 rule-out)
    reasoning: str


def decide(signal: NormalizedSignal, ctx: PolicyContext) -> Decision:
    if signal.signal_type == "anomaly_risk":
        if ctx.has_contradicting_evidence:
            return Decision(None, "반대증거 있음 - rule-out으로 조치 보류")
        if not ctx.preview_ready:
            return Decision(ACTION_OBSERVE_ONLY, "anomaly_risk 감지했으나 preview가 준비 안 됨")
        return Decision(ACTION_PROMOTE_PREVIEW, "anomaly_risk 감지, preview 준비됨 - 선제 전환")

    if signal.signal_type == "VLLMTargetDown":
        if ctx.preview_ready:
            return Decision(ACTION_PROMOTE_PREVIEW, "VLLMTargetDown, preview 준비됨 - 즉시 전환")
        return Decision(ACTION_OBSERVE_ONLY, "VLLMTargetDown이나 preview 없음 - 관찰만, K8s 기본 self-healing에 맡김")

    if signal.signal_type == "VLLMTargetMissing":
        if ctx.preview_ready:
            return Decision(ACTION_PROMOTE_PREVIEW, "VLLMTargetMissing, preview 준비됨 - 즉시 전환")
        return Decision(ACTION_OBSERVE_ONLY, "VLLMTargetMissing이나 preview 없음 - K8s 기본복구 상태 확인 후 관찰")

    return Decision(None, f"정책에 정의되지 않은 신호 타입: {signal.signal_type}")
