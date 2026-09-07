"""'해도 되는지' 게이트키핑 - idempotency(중복 차단)와 조치 쿨다운(실제 조치
빈도 제한)만 담당한다.

preview Ready 확인은 policy.py의 PolicyContext로 넘어가는 값이고
(rollouts_client.is_paused_pre_promotion()), promotion 실행 후 실제로
반영됐는지 검증은 rollouts_client.promote()의 verified 필드가 이미 한다 -
여기서 다시 하지 않는다(중복 방지).

score_server.py에도 쿨다운이 있는데 그건 "신호를 다시 보낼지"(발생 억제)를
막는 것이고, 여기 쿨다운은 "실제 조치를 다시 실행할지"(조치 억제)를 막는
것이다 - 예측 경로와 반응 경로가 동시에 같은 사고에 대해 서로 다른 신호를
보내는 경우, 신호 자체는 각자 정상적으로 들어와도 실제 promote는 한 번만
일어나야 하므로 조치 쪽에도 별도 쿨다운이 필요하다(리뷰가 지적한 역할 분리).

메모리 set()만 쓰면 서비스 재시작 시 중복 조치가 가능하다는 지적을 반영해,
처리한 idempotency_key를 로컬 JSON 파일에 남긴다(이 규모에 SQLite는 과함).
"""
import json
import time
from pathlib import Path

STATE_FILE = Path(__file__).parent / "state" / "safety_state.json"
ACTION_COOLDOWN_SEC = 60
MAX_KEPT_KEYS = 1000


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"processed_keys": [], "last_action_at": None}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def check_and_reserve(idempotency_key: str) -> bool:
    """True면 새 신호(처리해도 됨, 이 호출로 즉시 processed 표시됨).
    False면 이미 처리된 신호(중복, 조치하면 안 됨).

    # ponytail: read-modify-write 사이 race condition 있음(동시 요청 두 개가
    # 같은 key를 동시에 통과할 수 있음) - 이 프로젝트는 요청 동시성이 낮아
    # 무시. 문제되면 파일 락이나 SQLite로 바꿀 것.
    """
    state = _load_state()
    if idempotency_key in state["processed_keys"]:
        return False
    state["processed_keys"] = (state["processed_keys"] + [idempotency_key])[-MAX_KEPT_KEYS:]
    _save_state(state)
    return True


def in_action_cooldown() -> bool:
    state = _load_state()
    last = state.get("last_action_at")
    return last is not None and (time.time() - last) < ACTION_COOLDOWN_SEC


def mark_action_taken() -> None:
    state = _load_state()
    state["last_action_at"] = time.time()
    _save_state(state)
