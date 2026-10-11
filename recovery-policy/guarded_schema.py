"""guarded 정책의 v2 신호 스키마 - 기존 schemas.py(AnomalySignalRequest)와 분리된 새 파일.

v2는 탐지기 식별·모델 식별·평가 순번·관측 대상(active/preview 해시·Pod UID·재시작 수)·평가 창·판정 시각을
필수로 싣는다. 알 수 없는 필드는 거부한다(extra=forbid). 시각은 시간대가 있어야 하고
window_start < window_end <= decided_at 이어야 한다.

전달 중복 키(delivery_key) = run_id:detector_id:detector_session_id:evaluation_seq - 같은 신호의 재전송을 가린다.
조치 상태(같은 대상에 대한 승격)는 guarded_policy의 target_key로 따로 관리한다(키가 평가마다 달라도 반복 승격 금지).
"""
import hashlib
import json
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SCHEMA_VERSION = "c1-signal-v2"
_ID = r"^[A-Za-z0-9._:\-]{1,200}$"
_HEX64 = r"^[0-9a-f]{64}$"
_HASH = r"^[a-z0-9]{1,63}$"


class ObservedTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_hash: str = Field(pattern=_HASH)
    preview_hash: str = Field(pattern=_HASH)
    active_pod_uid: str = Field(min_length=1, max_length=64)
    preview_pod_uid: str = Field(min_length=1, max_length=64)
    active_restart_count: Optional[int] = Field(default=None, ge=0)
    preview_restart_count: Optional[int] = Field(default=None, ge=0)


class SignalV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["c1-signal-v2"]
    signal_type: Literal["anomaly_risk"]
    detector_id: str = Field(pattern=_ID)
    detector_session_id: str = Field(min_length=1, max_length=100)
    model_version: str = Field(pattern=_ID)
    model_hash: str = Field(pattern=_HEX64)
    artifact_manifest_hash: str = Field(pattern=_HEX64)
    threshold: float
    experiment_run_id: str = Field(pattern=_ID)
    evaluation_seq: int = Field(ge=1)
    window_start_utc: datetime
    window_end_utc: datetime
    decided_at_utc: datetime
    score: float
    consecutive_count: int = Field(ge=1)
    observed: ObservedTarget

    @field_validator("window_start_utc", "window_end_utc", "decided_at_utc")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("시간대 없는 시각")
        return v

    @model_validator(mode="after")
    def _order(self):
        if not (self.window_start_utc < self.window_end_utc <= self.decided_at_utc):
            raise ValueError("시각 순서 위반: window_start < window_end <= decided_at 이어야 함")
        if self.observed.active_hash == self.observed.preview_hash:
            raise ValueError("관측 active와 preview 해시가 같음")
        return self

    @property
    def delivery_key(self) -> str:
        return f"{self.experiment_run_id}:{self.detector_id}:{self.detector_session_id}:{self.evaluation_seq}"


def payload_digest(raw) -> str:
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def parse_signal(raw) -> tuple:
    """(SignalV2 | None, 오류 문자열 | None) - 예외를 올리지 않는다."""
    if not isinstance(raw, dict):
        return None, "payload_not_object"
    try:
        return SignalV2.model_validate(raw), None
    except ValidationError as e:
        return None, "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())[:1000]
