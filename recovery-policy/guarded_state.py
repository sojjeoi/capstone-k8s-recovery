"""guarded 정책(별도 버전)의 영속 상태 - 기존 safety.py와 분리된 새 파일.

전제(문서화): 단일 프로세스·단일 인스턴스·전용 상태 경로. 동시성은 GuardedPolicy의 프로세스 잠금 하나로
직렬화한다(이 모듈은 잠금을 갖지 않는다 - 상태 전이 전체가 한 잠금 안에서 일어나야 하기 때문).

저장 규칙:
- 같은 디렉터리의 임시 파일에 쓰고 flush+fsync 뒤 os.replace로 교체한다(부분 기록 파일이 남지 않음).
  전원 장애 시 디렉터리 항목까지의 내구성은 대상 파일시스템에서 따로 확인할 사항이다.
- 저장 실패는 예외(StateSaveError)로 올린다 - 호출자는 승격하지 않는다.

불러오기 규칙(fail-closed, 자동 복구 없음):
- 파일 없음: initialize=True로 명시 초기화할 때만 새로 만든다. 아니면 StateUnavailable("state_missing").
- JSON 손상·버전 불일치: StateUnavailable - 빈 상태로 덮어쓰지 않는다.
재시작 규칙(load_for_restart):
- 남아 있는 in_progress 조치는 unknown_after_restart로 바꾼다(승격 결과 불명 - 자동 재승격 금지, 전역 차단).
- context의 actions_allowed는 False로 내린다(재시작 뒤 조치는 사람이 다시 허용해야 한다).
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

STATE_VERSION = 1
UNRESOLVED = ("in_progress", "unknown", "unverified", "unknown_after_restart", "verified_service_failed",
              "verified_service_unknown", "verified_service_pending")


class StateUnavailable(Exception):
    """상태를 믿을 수 없음 - 정책은 조치 불가 상태로 동작해야 한다."""


class StateSaveError(Exception):
    """상태 저장 실패 - 호출자는 승격하지 않는다."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def empty_state() -> dict:
    return {"version": STATE_VERSION, "initialized_at": _now(), "context": None, "deliveries": {},
            "last_seq": {}, "sessions": {}, "actions": {}, "blocked": None, "history": []}


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self, *, initialize: bool = False) -> dict:
        if not self.path.exists():
            if not initialize:
                raise StateUnavailable("state_missing")
            st = empty_state()
            self.save(st)
            return st
        try:
            st = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise StateUnavailable(f"state_unreadable: {type(e).__name__}") from e
        if not isinstance(st, dict) or st.get("version") != STATE_VERSION:
            raise StateUnavailable(f"state_version_mismatch: {st.get('version') if isinstance(st, dict) else None}")
        return st

    def load_for_restart(self, *, initialize: bool = False) -> dict:
        st = self.load(initialize=initialize)
        changed = False
        for act in st["actions"].values():
            if act.get("status") == "in_progress":
                act["previous_status"], act["status"] = "in_progress", "unknown_after_restart"
                act["restart_marked_at"] = _now()
                changed = True
        if st.get("context") and st["context"].get("actions_allowed"):
            st["context"]["actions_allowed"] = False
            st["context"]["actions_disabled_reason"] = "process_restart"
            changed = True
        if changed:
            st["history"].append({"at": _now(), "event": "restart_rules_applied"})
            self.save(st)
        return st

    def save(self, st: dict) -> None:
        st["history"] = st.get("history", [])[-500:]
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(st, ensure_ascii=False, indent=1, default=str))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            raise StateSaveError(f"{type(e).__name__}: {e}") from e


def unresolved_actions(st: dict) -> dict:
    return {k: a for k, a in st["actions"].items() if a.get("status") in UNRESOLVED}
