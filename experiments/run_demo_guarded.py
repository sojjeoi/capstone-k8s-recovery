#!/usr/bin/env python3
"""교수 시연 전용(demo-only) guarded 실행기 - 얇은 wrapper (2026-10-10, counted=false).

연구용 frozen 실행기 run_guarded_pilot.py를 **수정하지 않고** 그 run_guarded()를 그대로 호출한다.
바꾸는 것은 이 프로세스 안의 실행 ID 표·plan ID·결과 폴더뿐이며, 호출이 끝나면 원래 값으로 되돌린다.
- 실행 ID: demo01–demo09 (공식 gap/ab/bc/nb ID와 겹치지 않음, 연구 집계 대상 아님)
- 결과 폴더: experiments/results/demo_only_guarded/ (공식 분석기가 읽는 폴더와 분리)
- run_id: guarded_pressure_act-demoNN-capstone-demo-only-20261010 (공식 분석기의 ID 패턴과 겹치지 않음)
- 같은 탐지기·정책·승격·서비스 확인(검증된 트래픽 확인 재사용), 같은 사전검사·정리·원복.
- act 승인 기록에 demo_only=true·counted=false·runs=[실행 ID]가 있어야 시작한다. 실행 ID 재사용은 원 실행기 규칙대로 거부.
세션 래퍼: guarded_pilot_session.ps1 -RunnerScript experiments\\run_demo_guarded.py -SessionRoot <결과 폴더> 로 호출."""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import run_guarded_pilot as gp
import run_timed_switch_recovery_trial as rt

DEMO_PLAN_ID = "capstone-demo-only-20261010"
DEMO_CONTRACT_DOC = "docs/demo/FINAL-DEMO-RUNBOOK-20261010.md"
DEMO_DIR = Path(__file__).parent / "results" / "demo_only_guarded"
DEMO_RUNS = {f"demo{n:02d}": ("pressure", gp.MODE_ACT, 900 + n) for n in range(1, 10)}


def check_demo_approval(path, run: str) -> None:
    if not path:
        raise SystemExit("demo act는 승인 기록(--act-approval)이 있어야 시작한다")
    a = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if a.get("demo_only") is not True or a.get("counted") is not False or run not in (a.get("runs") or []):
        raise SystemExit(f"demo 승인 기록 아님 - demo_only=true, counted=false, runs에 {run} 필요")


def run_demo(run: str, *, deps, policy_url: str, approval_path=None, preflight_only=False, results_dir: Path = DEMO_DIR) -> dict:
    if run not in DEMO_RUNS:
        raise SystemExit(f"demo 실행 ID가 아님: {run} (허용 {sorted(DEMO_RUNS)})")
    check_demo_approval(approval_path, run)
    saved = (dict(gp.RUNS), gp.PLAN_ID, gp.CONTRACT_DOC)
    base = deps.check_unresolved
    deps.check_unresolved = lambda: (base(), rt.check_no_unresolved_failures(results_dir))  # demo 정리 실패도 다음 실행을 막는다
    try:
        gp.RUNS.update(DEMO_RUNS)
        gp.PLAN_ID, gp.CONTRACT_DOC = DEMO_PLAN_ID, DEMO_CONTRACT_DOC
        run_id = gp.run_id_for(run)
        results_dir.mkdir(parents=True, exist_ok=True)
        tag = results_dir / f"demo-tag-{run_id}{'-preflight-only' if preflight_only else ''}.json"
        if not tag.exists():   # 실행 전에 남긴다 - 중단돼도 이 폴더 자료가 demo 전용임이 남도록
            tag.write_text(json.dumps({"run": run, "run_id": run_id, "demo_only": True, "counted": False,
                                       "official_evidence": False, "written_at_utc": datetime.now(timezone.utc).isoformat()},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
        return gp.run_guarded(run, mode=gp.MODE_ACT, deps=deps, policy_url=policy_url, approval_path=approval_path,
                              preflight_only=preflight_only, results_dir=results_dir)
    finally:
        gp.RUNS.clear()
        gp.RUNS.update(saved[0])
        gp.PLAN_ID, gp.CONTRACT_DOC = saved[1], saved[2]


def main(argv=None) -> int:  # pragma: no cover - 라이브 진입점(세션 래퍼가 부른다)
    p = argparse.ArgumentParser(description="demo-only guarded 실행 1회(act). 관리자 토큰은 환경변수 GUARDED_ADMIN_TOKEN")
    p.add_argument("--run", required=True, choices=sorted(DEMO_RUNS))
    p.add_argument("--mode", required=True, choices=[gp.MODE_ACT])
    p.add_argument("--policy-url", default="http://127.0.0.1:18090")
    p.add_argument("--session-file", default=None)
    p.add_argument("--act-approval", required=True)
    p.add_argument("--preflight-only", action="store_true")
    a = p.parse_args(argv)
    token = os.environ.get("GUARDED_ADMIN_TOKEN")
    if not token:
        raise SystemExit("GUARDED_ADMIN_TOKEN 환경변수 필요")
    s = run_demo(a.run, deps=gp.real_deps(a.policy_url, token, a.session_file), policy_url=a.policy_url,
                 approval_path=a.act_approval, preflight_only=a.preflight_only)
    if s.get("cleanup_errors"):
        print(f"[중단] 정리·원복 문제 - 다음 실행 자동 거부, 수동 확인 필요: {s['cleanup_errors']}")
        return 2
    print(f"결과: {s.get('outcome_status') or s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
