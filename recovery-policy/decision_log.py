"""판단 근거 레코드를 "생성"만 한다 - 파일에 쓰거나 git에 커밋하는 건
git_client.py 몫(guideline.md: "탐지 시점의 주요 표준화 편차 지표·시간창
변화량, 반대증거(rule-out 체크), 선택한 조치와 실행 결과를 남겨 '무엇을
바꿨는지'뿐 아니라 '왜 그렇게 판단했는지'까지 감사 가능하게 함").

Isolation Forest 자체가 개별 판단의 feature contribution을 직접 제공한다고
주장하지 않는다(guideline.md) - evidence는 policy.py가 계산한 표준화 편차 등을
그대로 담을 뿐, 이 모듈이 "왜 이상인지"를 스스로 설명해내지 않는다.

허용된 조치(EXECUTED_*)만이 아니라 no_action/cooldown 차단/중복 차단/promotion
실패도 전부 기록 대상이다 - 리뷰 지적: "allowed()에서 바로 반환하면 중요한
판단이 기록되지 않는다"는 걸 막기 위해 Outcome에 그 경우들을 전부 넣어둔다.
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel

from schemas import NormalizedSignal


class Outcome(str, Enum):
    EXECUTED_VERIFIED = "executed_verified"  # 조치 실행 + 실제 반영 확인됨
    EXECUTED_UNVERIFIED = "executed_unverified"  # 조치는 시도했으나 검증 실패(rollouts_client verified=False)
    SKIPPED_RULE_OUT = "skipped_rule_out"  # 반대증거 있어서 조치 안 함
    SKIPPED_COOLDOWN = "skipped_cooldown"  # safety.py 쿨다운으로 차단
    SKIPPED_DUPLICATE = "skipped_duplicate"  # idempotency로 중복 차단
    SKIPPED_UNKNOWN_SIGNAL = "skipped_unknown_signal"  # 정책에 정의 안 된 신호 타입
    NO_ACTION = "no_action"  # 그 외 조치 불필요 판단


class DecisionRecord(BaseModel):
    record_id: str
    decided_at: datetime
    signal_source: str
    signal_type: str
    idempotency_key: str
    evidence: dict  # 표준화 편차 지표, rule-out 체크 결과 등 - policy.py가 채움
    action: Optional[str]  # 예: "promote_preview", None(조치 없음)
    outcome: Outcome
    result: Optional[dict] = None  # rollouts_client.promote()의 반환값(실행한 경우만)
    reasoning: str = ""  # 사람이 읽을 짧은 설명


def build(
    signal: NormalizedSignal,
    action: Optional[str],
    outcome: Outcome,
    evidence: Optional[dict] = None,
    result: Optional[dict] = None,
    reasoning: str = "",
) -> DecisionRecord:
    return DecisionRecord(
        record_id=str(uuid.uuid4()),
        decided_at=datetime.now(timezone.utc),
        signal_source=signal.source,
        signal_type=signal.signal_type,
        idempotency_key=signal.idempotency_key,
        evidence=evidence or {},
        action=action,
        outcome=outcome,
        result=result,
        reasoning=reasoning,
    )
