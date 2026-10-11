"""run_guarded_bck 실제 CLI 진입점 테스트(v1.1, 2026-10-07) - guarded_pilot_session.ps1(수정 없음)이 runner를 띄우는 인자 형식
그대로 subprocess로 실행한다. 클러스터·네트워크 접속 전 단계(argparse·승인 기록 확인·계약 dry-run)까지만 간다.
거부 경로는 실제 스크립트 파일을 그대로 실행하고, 진입 경로는 결과 폴더만 tmp로 바꾼 같은 main()을 실행한다(실제 결과 폴더를 만들지 않음)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import run_guarded_bck as gb

EXP = Path(__file__).resolve().parent
SCRIPT = EXP / "run_guarded_bck.py"
# guarded_pilot_session.ps1 83-86행과 같은 순서·형식: --run <Run> --mode <Mode> --policy-url ... --session-file ... [--act-approval ...]
DRIVER = ("import sys, pathlib; sys.path.insert(0, sys.argv.pop(1)); import run_guarded_bck as gb; "
          "gb.BCK_RESULTS_DIR = pathlib.Path(sys.argv.pop(1)); sys.exit(gb.main(sys.argv[1:]))")


def _argv(tmp, run="bc90b", mode="act", approval=True, drop=()):
    a = {"--run": run, "--mode": mode, "--policy-url": "http://127.0.0.1:18090",
         "--session-file": str(tmp / "session.json"), "--act-approval": str(tmp / "approval.json") if approval else None}
    out = []
    for k, v in a.items():
        if k not in drop and v is not None:
            out += [k, v]
    return out


def _env(token=True):
    e = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}  # ps1과 같은 인코딩 설정
    e.pop("GUARDED_ADMIN_TOKEN", None)
    if token:
        e["GUARDED_ADMIN_TOKEN"] = "test-token-not-real"
    return e


def _script(args, token=True):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=EXP, env=_env(token), capture_output=True,
                          text=True, encoding="utf-8", timeout=120)


def _driver(results, args, token=True):
    return subprocess.run([sys.executable, "-c", DRIVER, str(EXP), str(results), *args], cwd=EXP, env=_env(token),
                          capture_output=True, text=True, encoding="utf-8", timeout=120)


@pytest.fixture(autouse=True)
def _real_results_untouched():
    before = gb.BCK_RESULTS_DIR.exists()
    yield
    assert gb.BCK_RESULTS_DIR.exists() == before  # 실제 B/C/K 결과 폴더를 만들거나 지우지 않는다


@pytest.mark.parametrize("mode", ["record_only", "ACT", "", "act "])
def test_non_act_mode_refused_by_real_script(tmp_path, mode):
    r = _script(_argv(tmp_path, mode=mode))
    assert r.returncode == 2 and "--mode" in r.stderr and "invalid choice" in r.stderr, r.stderr


@pytest.mark.parametrize("drop", ["--mode", "--run", "--act-approval"])
def test_missing_required_argument_refused_by_real_script(tmp_path, drop):
    r = _script(_argv(tmp_path, drop=(drop,)))
    assert r.returncode == 2 and "required" in r.stderr and drop in r.stderr, r.stderr


@pytest.mark.parametrize("run", ["bc01a", "ab01b", "bc19b", "bc90"])
def test_unknown_run_id_refused_by_real_script(tmp_path, run):
    r = _script(_argv(tmp_path, run=run))
    assert r.returncode == 2 and "--run" in r.stderr, r.stderr


def test_missing_token_refused_before_cluster(tmp_path):
    r = _script(_argv(tmp_path), token=False)
    assert r.returncode == 1 and "GUARDED_ADMIN_TOKEN" in r.stderr, r.stderr


def test_wrapper_format_with_mode_act_passes_argparse_and_enters_run(tmp_path):
    """ps1 형식 그대로 --mode act → argparse 통과 → run_bck 진입 → 승인 기록 확인 단계에서 fail-closed(파일 없음).
    클러스터 접속(사전검사)은 승인 확인 뒤라 일어나지 않는다."""
    res = tmp_path / "results"
    r = _driver(res, _argv(tmp_path))
    assert r.returncode == 1 and "act 승인 기록을 읽을 수 없음" in r.stderr, (r.returncode, r.stderr[-800:])
    assert "unrecognized arguments" not in r.stderr and res.is_dir() and list(res.iterdir()) == []


def test_wrapper_format_dry_run_contract_records_act_and_arm(tmp_path):
    (tmp_path / "clock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "approval.json").write_text(json.dumps({
        "status": "final", "timing_source": "clock-measurement-test", "clock_measurement_file": "clock.json",
        "timing": {"max_signal_age_sec": 30.0, "max_future_skew_sec": 5.0, "max_window_to_decision_sec": 30.0},
        "service_wait_deadline_sec": 80.0, "approved_by": "test", "approved_at_utc": "2026-10-07T00:00:00+00:00",
        "runs": ["bc90b"]}), encoding="utf-8")
    res = tmp_path / "results"
    r = _driver(res, _argv(tmp_path) + ["--dry-run-contract-only"], token=False)
    assert r.returncode == 0, r.stderr[-800:]
    c = json.loads((res / f"contract-{gb.run_id_for('bc90b')}.json").read_text(encoding="utf-8"))
    assert c["mode"] == "act" and c["arm"] == "B" and c["comparison"]["counted_in_comparison"] is False
    assert c["conditions"]["rules"]["C"]["threshold"] == 1.6842544293089357 and c["conditions"]["rules"]["K"]["threshold"] == 2.7
    assert sorted(p.name for p in res.iterdir()) == [f"contract-{gb.run_id_for('bc90b')}.json"]


def test_session_wrapper_still_passes_mode_and_is_unchanged():
    ps1 = (EXP / "guarded_pilot_session.ps1").read_text(encoding="utf-8")
    assert "'--run', $Run, '--mode', $Mode" in ps1 and "[ValidateSet('record_only', 'act')]" in ps1
