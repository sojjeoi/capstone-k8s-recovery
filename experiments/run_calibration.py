#!/usr/bin/env python3
"""무개입 대조군(calibration) 실행 - 복구 정책 서비스가 개입하지 않는 상태로
시나리오를 그대로 흘려보내고 t_SLO_counterfactual을 측정한다(9-3절 반사실
문제 해결책). blue_green_prep으로 실제 실험과 동일하게 active+preview를
동시 구동해 리소스 조건을 맞춘 뒤(9-7절 공정한 비교), 부하 자체를 합성
프로브로 써서 측정하고 slo_judge.py로 판정한다.

지금은 부하형 시나리오(고정 도착률 부하 증가 등 - ramp.py 실행 자체가 곧
관찰 대상)만 지원한다. Chaos Mesh 기반 시나리오(메모리 압박/네트워크 열화/
Pod kill)는 chaos YAML을 적용해두고 별도 프로브를 병행 실행해야 하는데
그 모드는 아직 구현/실측 전이다 - 필요해지면 추가할 것.
"""
import argparse
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import slo_judge
from blue_green_prep import prepare_preview
from run_ramp_in_cluster import run_in_cluster

RESULTS_DIR = Path(__file__).parent / "results"


def run_load_based(config_path: str, run_id: str, method: str = "calibration") -> Path:
    """부하형 시나리오 - ramp.py 실행 자체가 곧 관찰 대상. kubectl port-forward
    터널은 동시 연결이 몰리면 앞쪽 요청들이 타임아웃되는 걸 실측으로 확인해서
    (서버 문제 아님 - in-cluster 직접 호출은 100% 성공), 반드시 in-cluster
    실행으로 측정한다."""
    _summary, raw = run_in_cluster(config_path, run_id, method, "1", Path("../chaos/loadgen/results"))
    return raw


def main():
    parser = argparse.ArgumentParser(description="무개입 대조군 실행 - t_SLO_counterfactual 측정")
    parser.add_argument("--config", required=True, help="ramp.py용 부하 시나리오 config")
    parser.add_argument("--rollout", default="vllm-serving")
    parser.add_argument("--namespace", default="vllm-serving")
    parser.add_argument("--skip-prep", action="store_true", help="preview가 이미 준비돼있으면 건너뜀(디버깅용)")
    args = parser.parse_args()

    run_id = datetime.now(timezone.utc).strftime("calib-%Y%m%dT%H%M%SZ")

    if not args.skip_prep:
        print("blue_green_prep: preview 준비 중 (active+preview 동시 구동으로 리소스 조건 맞춤)...")
        if not prepare_preview(args.rollout, args.namespace):
            print("preview가 시간 내에 Ready되지 않음 - 중단")
            return
        print("preview 준비 완료, 무개입 상태로 부하 시작")

    print(f"run_id={run_id}")
    raw_csv = run_load_based(args.config, run_id)

    rows = slo_judge.load_raw(raw_csv)
    points = slo_judge.evaluate(rows)
    t_slo = slo_judge.find_t_slo(points)
    t_recovery = slo_judge.find_t_recovery(points, t_slo)

    result = {
        "run_id": run_id,
        "config": args.config,
        "raw_csv": str(raw_csv),
        "t_slo_counterfactual": t_slo.isoformat() if t_slo else None,
        "t_recovery": t_recovery.isoformat() if t_recovery else None,
    }
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{run_id}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"결과 저장: {out}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
