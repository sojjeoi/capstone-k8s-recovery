"""감사기록을 PVC에 동기 기록하고, 별도 백그라운드 작업자가 Git에 순차
커밋·푸시한다(guideline.md 9-6절: "PVC 기반의 단순 감사 outbox"). recovery-policy는
반드시 replicas:1이어야 한다 - 이 모듈은 단일 프로세스·단일 writer를 전제로
파일 락 하나로 직렬화한다(여러 pod가 동시에 같은 PVC에 쓰면 outbox.json이
깨질 수 있음).

핵심 분리: decision_log.DecisionRecord는 "판단 근거·조치 결과"만 담는다
(t_audit_write는 outbox 엔트리 쪽에 기록). commit하기 전엔 SHA를 알 수 없으므로
"전송 상태"(t_audit_push, commit SHA, 재시도 횟수, 실패 사유)는 별도의 outbox.json에
기록한다 - 감사 레코드 자체를 나중에 고쳐쓰지 않는다.

시작 시퀀스(start_worker, main.py가 FastAPI startup에서 호출):
(1) PVC에 clone이 없으면 git clone, 있으면 git pull --rebase로 최신화
(2) outbox에서 pending/failed 레코드를 다시 큐에 넣음
(3) 단일 백그라운드 워커 스레드 시작 - 요청마다 스레드를 새로 만들지 않음
enqueue()는 파일을 동기로 쓰고 큐에 넣기만 하고 즉시 반환한다 - git 지연·실패가
복구 API 응답을 막지 않는다.
"""
import json
import logging
import os
import queue
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from decision_log import DecisionRecord
from schemas import NormalizedSignal

logger = logging.getLogger("git_client")

DATA_DIR = Path(os.environ.get("RECOVERY_POLICY_DATA_DIR", "/data"))
REPO_DIR = DATA_DIR / "repo"
AUDIT_LOG_DIR = REPO_DIR / "audit-log"
OUTBOX_PATH = DATA_DIR / "outbox.json"
ASKPASS_SCRIPT = Path(__file__).parent / "git_askpass.sh"

GIT_REMOTE_URL = os.environ.get("GIT_REMOTE_URL", "")
GIT_TOKEN = os.environ.get("GIT_TOKEN", "")
GIT_AUTHOR_NAME = os.environ.get("GIT_AUTHOR_NAME", "recovery-policy")
GIT_AUTHOR_EMAIL = os.environ.get("GIT_AUTHOR_EMAIL", "recovery-policy@localhost")
GIT_TIMEOUT_SEC = 30
CLONE_TIMEOUT_SEC = 120
PUSH_MAX_RETRIES = 5
PUSH_BACKOFF_BASE_SEC = 2  # 2, 4, 8, 16, 32초

_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
_worker_started = False
_outbox_lock = threading.Lock()


# ---- outbox 상태 관리 (PVC의 outbox.json: record_id -> 전송상태) ----

def _load_outbox() -> dict:
    if not OUTBOX_PATH.exists():
        return {}
    return json.loads(OUTBOX_PATH.read_text(encoding="utf-8"))


def _save_outbox(state: dict) -> None:
    OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTBOX_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OUTBOX_PATH)  # 원자적 교체 - 쓰다 죽어도 outbox.json 자체는 안 깨짐


def _update_outbox_entry(record_id: str, **fields) -> None:
    with _outbox_lock:
        state = _load_outbox()
        entry = state.get(record_id, {"status": "pending", "attempts": 0})
        entry.update(fields)
        state[record_id] = entry
        _save_outbox(state)


# ---- git 실행 ----

def _git_env() -> dict:
    env = os.environ.copy()
    env["GIT_ASKPASS"] = str(ASKPASS_SCRIPT)
    env["GIT_TOKEN"] = GIT_TOKEN
    env["GIT_TERMINAL_PROMPT"] = "0"  # 자격증명 없을 때 대화형으로 안 멈추고 바로 실패
    return env


def _run_git(*args, timeout=GIT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO_DIR, env=_git_env(),
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )


def _ensure_repo() -> None:
    if (REPO_DIR / ".git").exists():
        result = _run_git("pull", "--rebase")
        if result.returncode != 0:
            logger.warning("git pull --rebase 실패(다음 push 재시도 때 다시 조정): %s", result.stderr)
        AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        return

    REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", GIT_REMOTE_URL, str(REPO_DIR)],
        env=_git_env(), capture_output=True, text=True, encoding="utf-8", timeout=CLONE_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone 실패: {result.stderr}")
    _run_git("config", "user.name", GIT_AUTHOR_NAME)
    _run_git("config", "user.email", GIT_AUTHOR_EMAIL)
    AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)


# ---- 기록 큐잉 (main.py가 조치 처리 직후 호출) ----

def enqueue(signal: NormalizedSignal, record: DecisionRecord) -> None:
    run_id = signal.raw.get("experiment_run_id") or "adhoc"
    AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIT_LOG_DIR / f"{run_id}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(record.model_dump_json() + "\n")
    _update_outbox_entry(
        record.record_id, status="pending", run_id=run_id,
        t_audit_write=datetime.now(timezone.utc).isoformat(), attempts=0,
    )
    _queue.put((record.record_id, run_id))


# ---- 백그라운드 워커 ----

def _process_batch(items: list[tuple[str, str]]) -> None:
    for record_id, _ in items:
        _update_outbox_entry(record_id, status="pushing")

    add_result = _run_git("add", "-A", "audit-log/")
    if add_result.returncode != 0:
        _fail_batch(items, f"git add 실패: {add_result.stderr}")
        return

    run_ids = sorted({run_id for _, run_id in items})
    commit_msg = f"audit: {len(items)}건 감사기록 ({', '.join(run_ids)})"
    commit_result = _run_git("commit", "-m", commit_msg)
    # 이미 이전 배치에 묶여 커밋된 상태일 수 있음(재시도 재진입) - 실패로 안 침
    if commit_result.returncode != 0 and "nothing to commit" not in commit_result.stdout:
        _fail_batch(items, f"git commit 실패: {commit_result.stderr}")
        return

    push_result = _run_git("push")
    if push_result.returncode != 0:
        rebase_result = _run_git("pull", "--rebase")
        if rebase_result.returncode != 0:
            _fail_batch(items, f"non-fast-forward 재조정 실패: {rebase_result.stderr}")
            return
        push_result = _run_git("push")
        if push_result.returncode != 0:
            _fail_batch(items, f"git push 실패: {push_result.stderr}")
            return

    sha = _run_git("rev-parse", "HEAD").stdout.strip()
    pushed_at = datetime.now(timezone.utc).isoformat()
    for record_id, _ in items:
        _update_outbox_entry(record_id, status="pushed", commit_sha=sha,
                              t_audit_push=pushed_at, last_error=None)


def _fail_batch(items: list[tuple[str, str]], error: str) -> None:
    logger.warning("배치 push 실패: %s", error)
    for record_id, run_id in items:
        entry = _load_outbox().get(record_id, {"attempts": 0})
        attempts = entry.get("attempts", 0) + 1
        _update_outbox_entry(record_id, status="failed", attempts=attempts, last_error=error)
        if attempts <= PUSH_MAX_RETRIES:
            delay = PUSH_BACKOFF_BASE_SEC * (2 ** (attempts - 1))
            threading.Timer(delay, lambda rid=record_id, rn=run_id: _queue.put((rid, rn))).start()
        else:
            logger.error("record %s: push 재시도 %d회 소진, 포기", record_id, PUSH_MAX_RETRIES)


def _worker_loop() -> None:
    while True:
        batch = [_queue.get()]  # 최소 1개는 블로킹으로 기다림
        try:
            while True:
                batch.append(_queue.get_nowait())
        except queue.Empty:
            pass
        try:
            _process_batch(batch)
        except Exception:
            logger.exception("배치 처리 중 예외 - outbox는 failed로 안 남았을 수 있어 수동 확인 필요")


def _requeue_unsent() -> None:
    state = _load_outbox()
    for record_id, entry in state.items():
        if entry.get("status") in ("pending", "pushing", "failed"):
            _queue.put((record_id, entry.get("run_id", "adhoc")))


def start_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    _ensure_repo()
    _requeue_unsent()
    threading.Thread(target=_worker_loop, daemon=True, name="git-audit-worker").start()
    _worker_started = True
