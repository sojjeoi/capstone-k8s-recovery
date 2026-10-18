"""guarded_pilot_session.ps1 -> n2_run.py stage2 변환 어댑터(Stage 2 세션 실행 연결 파일, 동결 코드 무수정).
래퍼는 `--run R --mode act --policy-url U --session-file F --act-approval A [--preflight-only]`를 넘기고,
n2_run은 `--policy-url U stage2 --run R --active-profile … --selection-evidence … --artifact-root … --act-approval A …` 형태를 받는다.
active profile·선택 증거는 Stage 1 선택 분석 결과 폴더의 고정 위치만 쓴다(다른 경로로 바꿀 수 없다)."""
import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NET = HERE.parent
ROOT = NET.parents[1]
SELECTION_DIR = ROOT / "experiments" / "results" / "phase2_network_env_selection"
ACTIVE_PROFILE = SELECTION_DIR / "active-profile.json"
SELECTION_EVIDENCE = SELECTION_DIR / "phase2-network-selection-evidence.json"


def translate(argv) -> list:
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True)
    p.add_argument("--mode", required=True)
    p.add_argument("--policy-url", required=True)
    p.add_argument("--session-file", required=True)
    p.add_argument("--act-approval", required=True)
    p.add_argument("--preflight-only", action="store_true")
    a = p.parse_args(argv)
    if a.mode != "act":
        raise SystemExit("Stage 2는 act 모드만 허용")
    root = os.environ.get("PHASE2_ARTIFACT_ROOT")
    if not root:
        raise SystemExit("PHASE2_ARTIFACT_ROOT 환경변수 필요(c3-A artifact 폴더)")
    out = ["--policy-url", a.policy_url, "stage2", "--run", a.run, "--active-profile", str(ACTIVE_PROFILE), "--selection-evidence", str(SELECTION_EVIDENCE),
           "--artifact-root", root, "--act-approval", a.act_approval, "--session-file", a.session_file]
    return out + (["--preflight-only"] if a.preflight_only else [])


def main() -> int:
    sys.path.insert(0, str(NET))
    import n2_run
    return n2_run.main(translate(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
