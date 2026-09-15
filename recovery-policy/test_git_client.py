#!/usr/bin/env python3
"""git_client.py를 로컬 bare 저장소로 검증 - 실제 GitHub를 안 써서 오프라인·
결정적으로 확인 가능. enqueue -> 백그라운드 워커가 commit·push까지 하는
정상 경로와, non-fast-forward(동시에 다른 커밋이 먼저 push된 상황) 재조정을
확인한다. env var를 git_client import 전에 세팅해야 모듈 상수에 반영된다."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

TMP = Path(tempfile.mkdtemp(prefix="git_client_test_"))
REMOTE = TMP / "remote.git"
DATA_DIR = TMP / "data"


def _git(*args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", check=True)


def _setup_remote():
    _git("init", "--bare", str(REMOTE), cwd=TMP)
    seed = TMP / "seed"
    _git("clone", str(REMOTE), str(seed), cwd=TMP)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=seed)
    _git("-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-m", "seed", cwd=seed)
    _git("push", cwd=seed)
    return seed


seed_clone = _setup_remote()

os.environ["RECOVERY_POLICY_DATA_DIR"] = str(DATA_DIR)
os.environ["GIT_REMOTE_URL"] = str(REMOTE)
os.environ["GIT_TOKEN"] = ""  # file:// 원격이라 실제로 안 쓰임(askpass 자체가 호출 안 됨)
os.environ["GIT_AUTHOR_NAME"] = "recovery-policy-test"
os.environ["GIT_AUTHOR_EMAIL"] = "recovery-policy-test@localhost"

import git_client  # noqa: E402  (env var 세팅 이후 import해야 모듈 상수에 반영됨)
from decision_log import Outcome, build  # noqa: E402
from schemas import NormalizedSignal, SignalSource  # noqa: E402


def _signal(key: str) -> NormalizedSignal:
    return NormalizedSignal(
        source=SignalSource.ANOMALY, signal_type="anomaly_risk", idempotency_key=key,
        received_at="2026-09-15T00:00:00+00:00", raw={},
    )


def _wait_for_status(record_id: str, status: str, timeout=10) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        entry = git_client._load_outbox().get(record_id)
        if entry and entry.get("status") == status:
            return entry
        time.sleep(0.2)
    raise AssertionError(f"{record_id}가 {timeout}초 안에 status={status}에 도달 못 함: {git_client._load_outbox().get(record_id)}")


def test_enqueue_commits_and_pushes():
    git_client.start_worker()
    signal = _signal("k1")
    record = build(signal, action="promote_preview", outcome=Outcome.EXECUTED_VERIFIED,
                    result={"verified": True}, reasoning="test")
    git_client.enqueue(signal, record)

    entry = _wait_for_status(record.record_id, "pushed")
    assert entry["commit_sha"], "commit_sha가 비어있음"
    assert entry["t_audit_push"]

    log_out = subprocess.run(["git", "--git-dir", str(REMOTE), "log", "--oneline"],
                              capture_output=True, text=True, encoding="utf-8", check=True).stdout
    assert entry["commit_sha"][:7] in log_out, "push된 커밋이 실제 원격 로그에 없음"
    print("OK - enqueue -> 백그라운드 워커가 commit+push, SHA 검증:", entry["commit_sha"][:7])


def test_non_fast_forward_recovers_via_rebase():
    # seed_clone은 테스트1의 push 이후로 뒤처져 있으니 먼저 따라잡고,
    # 그 다음 "다른 writer가 먼저 원격에 커밋을 push한 상황"을 재현
    _git("pull", cwd=seed_clone)
    (seed_clone / "other.txt").write_text("external change\n", encoding="utf-8")
    _git("add", "other.txt", cwd=seed_clone)
    _git("-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-m", "external", cwd=seed_clone)
    _git("push", cwd=seed_clone)

    signal = _signal("k2")
    record = build(signal, action="observe_only", outcome=Outcome.NO_ACTION, reasoning="test2")
    git_client.enqueue(signal, record)

    entry = _wait_for_status(record.record_id, "pushed", timeout=15)
    log_out = subprocess.run(["git", "--git-dir", str(REMOTE), "log", "--oneline"],
                              capture_output=True, text=True, encoding="utf-8", check=True).stdout
    assert "external" in log_out, "외부 커밋이 원격에 없음(테스트 준비 오류)"
    assert entry["commit_sha"][:7] in log_out, "non-fast-forward 이후 재시도한 push가 실제로 반영 안 됨"
    print("OK - non-fast-forward -> pull --rebase 후 재시도, push 성공:", entry["commit_sha"][:7])


def test_restart_requeues_pending():
    # "쓰기 직후 죽어서 큐에는 못 넣었지만 outbox엔 pending으로 남은" 상황 재현
    fake_id = "fake-crashed-record"
    git_client._update_outbox_entry(fake_id, status="pending", run_id="adhoc",
                                     t_audit_write="2026-09-15T00:00:00+00:00", attempts=0)
    path = git_client.AUDIT_LOG_DIR / "adhoc.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write('{"record_id": "%s", "fake": true}\n' % fake_id)

    git_client._worker_started = False  # 재기동 시뮬레이션
    git_client.start_worker()

    entry = _wait_for_status(fake_id, "pushed", timeout=10)
    print("OK - 재기동 시 pending 레코드 재처리:", entry["commit_sha"][:7])


if __name__ == "__main__":
    try:
        test_enqueue_commits_and_pushes()
        test_non_fast_forward_recovers_via_rebase()
        test_restart_requeues_pending()
        print("모두 통과")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
