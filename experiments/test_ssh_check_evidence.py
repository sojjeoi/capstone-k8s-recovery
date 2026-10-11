"""ssh_check_evidence 테스트 - 가짜 ssh(로컬 python 스크립트)로 정상·시간 초과·연결 실패·근거 저장 실패를 재현한다.
판정은 동결 evaluate_node_disk_health 그대로임을 '같은 stdout을 기존 fetch가 받았을 때의 판정'과 비교해 확인한다. 네트워크 없음."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import node_disk_health_gate as g
import ssh_check_evidence as sce

FAKE = r'''
import json, pathlib, sys, time
cfg = pathlib.Path(sys.argv[1]); cnt = cfg.with_suffix(".n")
n = int(cnt.read_text()) if cnt.exists() else 0
cnt.write_text(str(n + 1))
plan = json.loads(cfg.read_text(encoding="utf-8"))
steps = plan[min(n, len(plan) - 1)]
assert sys.argv[2] in ("capstone-worker", "capstone-worker2", "capstone-control"), sys.argv
assert sys.argv[3].startswith("echo \"@@GUARDED_STAGE T0"), sys.argv[3][:40]
for kind, val in steps:
    if kind == "sleep":
        time.sleep(val)
    elif kind == "holder":  # 출력 파이프를 물려받은 자식을 남기고 먼저 끝남(프로세스 종료 ≠ 출력 끝)
        import subprocess
        subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({val})"], stdout=sys.stdout, stderr=sys.stderr)
    elif kind == "exit":
        sys.exit(val)
    else:
        s = sys.stdout.buffer if kind == "out" else sys.stderr.buffer
        s.write(val.encode("utf-8")); s.flush()
sys.exit(0)
'''
HEAD = "2026-10-06T10:00:00+00:00\n---MOUNT\nrw,relatime\n"
OLD = "2026-10-06T09:00:00,000000+00:00 Linux version 6.8\n"
MID = "2026-10-06T09:50:00,000000+00:00 eth0: link up\n"
DISK_ERR = "2026-10-06T09:59:00,000000+00:00 blk_update_request: I/O error, dev vda, sector 1\n"
LATE_START = "2026-10-06T09:45:00,000000+00:00 Linux version 6.8\n"


def _t(stage, extra=""):
    return ("err", f"@@GUARDED_STAGE {stage} {extra}1790000000.{stage[-1]}00\n")


def staged(dmesg, rc=0, warn="", tail=()):
    return [_t("T0"), ("out", HEAD), _t("T1"), ("out", "---DMESG\n" + dmesg), *([("err", warn)] if warn else []),
            _t("T2", f"rc={rc} "), *tail, ("exit", rc)]


PLANS = {
    "normal": staged(OLD + MID, warn="Warning: Permanently added host\n"),
    "disk_error": staged(OLD + DISK_ERR),
    "short_coverage": staged(LATE_START + MID),
    "sudo_fail": staged("", rc=1, warn="sudo: a password is required\n"),
    "partial_timeout": [_t("T0"), ("out", HEAD), _t("T1"), ("out", "---DMESG\n" + OLD), ("sleep", 30)],
    "done_no_exit": staged(OLD + MID, tail=[("sleep", 30)]),
    "no_start_timeout": [("sleep", 30)],
    "exit_output_open": [("holder", 5), *staged(OLD + MID)],
    "connect_fail": [("err", "ssh: connect to host capstone-worker2 port 22: Connection timed out\r\n"), ("exit", 255)],
}


def expected(plan):
    """같은 원격 출력을 기존 fetch_node_kernel_log가 받았을 때의 입력(단계 표시 없는 stdout·stderr)."""
    out = "".join(v for k, v in plan if k == "out")
    err = "".join(v for k, v in plan if k == "err" and not v.startswith(sce.MARK)).replace("\r\n", "\n")
    rc = next((v for k, v in plan if k == "exit"), 0)
    return {"returncode": rc, "stdout": out, "stderr": err}


def collector(tmp_path, modes, timeout=2.0, evidence_dir=None):
    (tmp_path / "fake_ssh.py").write_text(FAKE, encoding="utf-8")
    cfg = tmp_path / "plan.json"
    cfg.write_text(json.dumps([PLANS[m] for m in modes]), encoding="utf-8")
    return sce.SshCheckEvidence(evidence_dir or tmp_path / "ev",
                                ssh_prefix=[sys.executable, str(tmp_path / "fake_ssh.py"), str(cfg)], timeout_sec=timeout)


def one(tmp_path, mode, **kw):
    c = collector(tmp_path, [mode], **kw)
    f = c.fetch("capstone-worker2")
    return c, f, c.records[-1], g.evaluate_node_disk_health("sj-worker2", f)


def test_instrumented_command_keeps_stdout_sequence_and_puts_stages_on_stderr():
    cmd = sce.INSTRUMENTED_CMD
    assert cmd.count(sce.MARK) == 3 and cmd.count(">&2") == 3
    stripped = cmd
    for frag in ('echo "@@GUARDED_STAGE T0 $(date +%s.%N)" >&2; ', 'echo "@@GUARDED_STAGE T1 $(date +%s.%N)" >&2; '):
        stripped = stripped.replace(frag, "")
    assert stripped == g.REMOTE_CMD + '; rc=$?; echo "@@GUARDED_STAGE T2 rc=$rc $(date +%s.%N)" >&2; exit $rc'
    assert sce.SSH_PREFIX == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    assert sce.SshCheckEvidence(Path(".")).timeout == g.SSH_TIMEOUT_SEC == 40.0


def test_normal_same_judgement_as_existing_and_raw_saved(tmp_path):
    c, f, rec, res = one(tmp_path, "normal")
    exp = expected(PLANS["normal"])
    assert f == exp  # 단계 표시는 stdout에 없고 stderr에서도 빠짐 - 기존 파서 입력과 같다
    assert res == g.evaluate_node_disk_health("sj-worker2", exp) and res["ok"] is True
    assert rec["observed"] == "exited_after_completion_marker" and rec["last_stage"] == "T2" and rec["returncode"] == 0
    assert set(rec["stages_remote"]) == {"T0", "T1", "T2"} and rec["stages_remote"]["T2"].startswith("rc=0 ")
    assert rec["stdout_bytes"] == len(exp["stdout"].encode()) and rec["elapsed_sec"] >= 0
    raw_err = (c.dir / rec["stderr_file"]).read_bytes()
    assert (c.dir / rec["stdout_file"]).read_text(encoding="utf-8") == exp["stdout"]
    assert b"@@GUARDED_STAGE T0" in raw_err and len(raw_err) == rec["stderr_bytes"]  # 원문 stderr는 표시 포함 보존
    idx = [json.loads(x) for x in (c.dir / sce.INDEX).read_text(encoding="utf-8").splitlines()]
    assert idx[0]["observed"] == "exited_after_completion_marker" and c.summary()["failed_checks"] == []


def test_stage_markers_do_not_hide_disk_errors_or_coverage_gap(tmp_path):
    for mode, reason in (("disk_error", "disk_errors_in_last_1800s=1"), ("short_coverage", "dmesg_does_not_cover_lookback")):
        d = tmp_path / mode
        d.mkdir()
        _, f, rec, res = one(d, mode)
        assert res == g.evaluate_node_disk_health("sj-worker2", expected(PLANS[mode]))
        assert res["ok"] is False and any(r.startswith(reason) for r in res["reasons"]), res
        assert rec["observed"] == "exited_after_completion_marker"


def test_nonzero_exit_after_completion_marker_is_access_failure_like_before(tmp_path):
    _, f, rec, res = one(tmp_path, "sudo_fail")
    assert f["returncode"] == 1 and res == g.evaluate_node_disk_health("sj-worker2", expected(PLANS["sudo_fail"]))
    assert res["ok"] is False and res["reasons"][0].startswith("log_access_failed(rc=1): sudo: a password is required")


def test_timeout_with_partial_output_fails_and_preserves_partial(tmp_path):
    c, f, rec, res = one(tmp_path, "partial_timeout")
    assert f["returncode"] is None and f["stdout"] == ""  # 부분 출력(---MOUNT·---DMESG 포함)이 있어도 통과 안 함
    assert res["ok"] is False and res["reasons"][0].startswith("log_access_failed(rc=None): TimeoutExpired")
    assert rec["observed"] == "timeout_after_start_no_completion_marker" and rec["last_stage"] == "T1"
    assert rec["timed_out"] is True and rec["elapsed_sec"] >= 2.0 and rec["returncode"] is None
    assert rec["returncode_after_kill"] is not None and rec["judged_returncode"] is None  # 실제 종료 상태는 근거로만
    part = (c.dir / rec["stdout_file"]).read_text(encoding="utf-8")
    assert part == HEAD + "---DMESG\n" + OLD and rec["stdout_bytes"] == len(part.encode())
    assert c.summary()["failed_checks"][0]["observed"] == "timeout_after_start_no_completion_marker"


def test_completion_marker_but_no_exit_still_fails(tmp_path):
    c, f, rec, res = one(tmp_path, "done_no_exit")
    assert f["returncode"] is None and res["ok"] is False  # 완료 표시·전체 출력이 있어도 시간 초과면 실패
    assert rec["observed"] == "timeout_after_completion_marker" and rec["last_stage"] == "T2"
    assert (c.dir / rec["stdout_file"]).read_text(encoding="utf-8") == expected(PLANS["done_no_exit"])["stdout"]


def test_timeout_before_any_output(tmp_path):
    _, f, rec, res = one(tmp_path, "no_start_timeout")
    assert f["returncode"] is None and res["ok"] is False
    assert rec["observed"] == "timeout_no_start_marker" and rec["last_stage"] is None and rec["stdout_bytes"] == 0


def test_connect_failure(tmp_path):
    c, f, rec, res = one(tmp_path, "connect_fail")
    assert f["returncode"] == 255 and res["reasons"][0].startswith("log_access_failed(rc=255): ssh: connect to host")
    assert rec["observed"] == "exited_without_start_marker" and rec["last_stage"] is None  # 원인 단정 없는 관측 이름
    assert (c.dir / rec["stderr_file"]).read_bytes().startswith(b"ssh: connect")


def test_launch_error_never_raises(tmp_path):
    c = sce.SshCheckEvidence(tmp_path / "ev", ssh_prefix=[str(tmp_path / "no-such-ssh.exe")], timeout_sec=2.0)
    f = c.fetch("capstone-worker")
    assert f["returncode"] is None and c.records[0]["observed"] == "launch_error"
    assert g.evaluate_node_disk_health("sj-worker", f)["ok"] is False


def test_evidence_write_failure_fails_the_check(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")  # 근거 폴더 자리에 파일 - 저장 불가
    c, f, rec, res = one(tmp_path, "normal", evidence_dir=blocker)
    assert f["returncode"] is None and f["stdout"] == "" and f["stderr"].startswith("evidence_write_failed")
    assert res["ok"] is False and c.summary()["write_errors"][0]["node"] == "sj-worker2"
    assert [x["seq"] for x in c.summary()["failed_checks"]] == [1] and rec["judged_returncode"] is None
    assert rec["observed"] == "exited_after_completion_marker"  # 관측은 정상이어도 필수 기록 실패로 통과 안 함


def test_check_all_nodes_through_disk_fn_sequential_and_files_not_overwritten(tmp_path):
    c = collector(tmp_path, ["normal", "partial_timeout", "normal"])
    r = c.disk_fn(lambda: ["sj-worker", "sj-worker2"])()
    assert r["ok"] is False and [n["ok"] for n in r["nodes"]] == [True, False]
    assert "sj-worker2" in r["reasons"][0] and "TimeoutExpired" in r["reasons"][0]
    assert [x["node"] for x in c.records] == ["sj-worker", "sj-worker2"]
    assert c.records[1]["started_utc"] >= c.records[0]["ended_utc"]  # 순차(병렬 SSH 없음)
    s = c.summary()
    assert s["checks"] == 2 and s["by_observed"] == {"exited_after_completion_marker": 1,
                                                     "timeout_after_start_no_completion_marker": 1}
    c2 = sce.SshCheckEvidence(c.dir, ssh_prefix=c.ssh_prefix, timeout_sec=2.0)  # 같은 폴더 두 번째 세션
    c2.fetch("capstone-worker")
    assert len(list(c.dir.glob("*.json"))) == 3 and len((c.dir / sce.INDEX).read_text(encoding="utf-8").splitlines()) == 3


def test_process_exit_with_output_still_open_fails_like_run_timeout(tmp_path):
    c, f, rec, res = one(tmp_path, "exit_output_open")
    assert rec["returncode"] == 0 and f["returncode"] is None and res["ok"] is False  # 출력이 다 닫히기 전엔 통과 안 함
    assert rec["observed"] == "exited_output_not_closed_within_limit" and "TimeoutExpired" in f["stderr"]
    assert rec["elapsed_sec"] < 2.0 + 6  # 같은 제한(+정리 대기) 안에서 끝남


BASH = Path(r"C:\Program Files\Git\bin\bash.exe")


@pytest.mark.skipif(not BASH.exists(), reason="POSIX 셸(Git Bash) 없음")
@pytest.mark.parametrize("findmnt_rc,dmesg_rc", [(0, 0), (1, 0), (0, 1)])
def test_instrumented_command_in_real_shell_same_stdout_and_exit_code(findmnt_rc, dmesg_rc):
    """원격 셸 대신 로컬 POSIX 셸에서 실행(findmnt·sudo만 함수로 대체). stdout(날짜 줄 제외)·종료 코드가 기존 명령과 같아야 한다."""
    stub = (f"findmnt() {{ echo rw,relatime; return {findmnt_rc}; }}; "
            f"sudo() {{ echo '2026-10-06T09:00:00,000000+00:00 line'; echo 'sudo-err' >&2; return {dmesg_rc}; }}; ")

    def run(cmd):
        r = subprocess.run([str(BASH), "-c", stub + cmd], capture_output=True, timeout=20)
        return r.returncode, r.stdout.decode().splitlines()[1:], r.stderr.decode()
    rc_old, out_old, err_old = run(g.REMOTE_CMD)
    rc_new, out_new, err_new = run(sce.INSTRUMENTED_CMD)
    assert (rc_new, out_new) == (rc_old, out_old) and rc_new == dmesg_rc
    assert [ln.split()[1] for ln in err_new.splitlines() if ln.startswith(sce.MARK)] == ["T0", "T1", "T2"]
    assert f"T2 rc={dmesg_rc} " in err_new
    assert [ln for ln in err_new.splitlines() if not ln.startswith(sce.MARK)] == err_old.splitlines()
