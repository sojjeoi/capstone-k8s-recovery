#!/usr/bin/env python3
"""§188(2026-09-28) - 후보 IF(-0.04, 3연속·모델·특징 전부 불변)의 정상 운전
장기 안정성 확인. §183 no_injection(600초 x2회, 평가 36개/회, 최대 연속
이상=1회, 발화 0회 - 원자료 재확인 완료)과 완전히 동일한 코드(run_
if_threshold_candidate_smoke.run_no_injection_smoke, §184/§185 정정 포함)를
그대로 재사용하되 관찰시간만 45~60분으로 늘린 별도 실행 버전이다(지시:
"기존 러너가 이 시간을 지원하지 않으면 관찰시간만 별도 실행 버전으로
분리"). run_no_injection_smoke()는 observation_sec을 이미 매개변수로
받는다(하드코딩 아님) - 이 파일이 새로 만드는 것은 그 매개변수를 CLI로
노출하고, prober 수명을 관찰시간에 맞춰 계산하고(§194 정정 - 여유폭은
PROBE_LIFETIME_MARGIN_SEC 참고, 최초 360초에서 실제 요청 전송 여유
재계산 후 600초로 조정됨), 새
plan_id로 결과 파일을 분리하는 것뿐이다. 판정 로직·모델·임계값·정책
연결(capture_sink만, 실제 /signal 없음)·CPU 압박 강도는 전혀 건드리지
않는다(CPU 압박 자체가 없음 - injector.inject()를 호출하지 않는 §183
Phase 1과 동일)."""
import argparse
import json
from datetime import datetime, timezone

import arm_controller
import node_resource_safety_gate
from cpu_pressure_demo_adapter import make_cpu_pressure_injector
import load_ramp_adapter
from load_ramp_adapter import IMAGE as LR_IMAGE, IMAGE_FORCE_CLOSE, make_load_ramp_prober
from run_cpu_demo_trial import DEMO_STAGES
from run_if_threshold_candidate_smoke import (
    CANDIDATE_ARTIFACTS_DIR,
    CANDIDATE_THRESHOLD,
    CONSECUTIVE_THRESHOLD,
    DEFAULT_PROBE_CONFIG,
    DEMO_RESULTS_DIR,
    OFFICIAL_THRESHOLD,
    PROBE_DATA_GAP_THRESHOLD_SEC,
    PROBE_RESULTS_DIR,
    _advance_streak_offline,
    _load_evidence_scores,
    _pick_free_loopback_port,
    _start_candidate_score_server_detector_for_sink,
    _start_gated_score_server_detector_for_sink,
    make_gated_detector_with_consecutive_override,
    run_no_injection_smoke,
)

PLAN_ID = "capstone-if-threshold-candidate-no-injection-long-188-v1"
# §194(2026-09-29) 정정 - 기존 360.0(기존 PROBE_LIFETIME_NO_INJECTION_SEC과
# 동일 여유폭, 더 짧은 무주입 실행용으로 유도된 값)은 이 러너의 "실제 요청
# 전송 여유"를 계산할 때 오해를 낳았다. probe.py --duration-sec에 실제로
# 전달되는 값은 pod 자체의 sleep() 수명이 아니라 duration_sec(observation_sec
# + 이 상수) + load_ramp_adapter.PROBE_DURATION_MARGIN_SEC(30, 공유·동결,
# 여기서 안 건드림)이다. 60분 관찰이 시작되기 전 최악의 경우 소요되는
# 시간(prober.is_alive 확인 ≤30s + 기준선 대기 ≤240s + sink 기동·헬스체크
# ≤16.5s + 탐지기 기동 ≤1.5s = 288초, run_if_threshold_candidate_smoke.py의
# PROBE_READY_TIMEOUT_SEC/BASELINE_TIMEOUT_SEC 및 _start_subprocess/
# _wait_http_ok 인용)까지 감안하면, 기존 360.0으로는 관찰 종료 시점에 실제
# 요청 전송 여유가 최악의 경우 약 102초까지 줄어들 수 있었다(실측
# 확인 - 지시: "60분 관찰 종료 시에도 최소 약 5분의 요청 전송 여유가
# 남도록"). 600.0으로 올리면 같은 최악 조건에서도 여유가 약 342초(5.7분)
# 남는다(600+30-288=342). 1 RPS·관찰 60분(MAX_OBSERVATION_SEC 미변경)·
# 모델·임계값·게이트·정책·공유 probe 어댑터(load_ramp_adapter.py) 설정은
# 전혀 건드리지 않았다 - 이 상수는 이 파일에서만 쓰이며(grep 확인, 다른
# 러너·공유 모듈이 참조하지 않음) 값 하나만 재계산했다.
PROBE_LIFETIME_MARGIN_SEC = 600.0
MIN_OBSERVATION_SEC = 2700.0  # 45분
MAX_OBSERVATION_SEC = 3600.0  # 60분

DETECTOR_START_FNS = {
    "candidate": _start_candidate_score_server_detector_for_sink,
    "gated": _start_gated_score_server_detector_for_sink,
}


def build_contract(run_id: str, observation_sec: float, detector: str = "candidate",
                    preview_prep_timeout_sec: float = None,
                    no_injection_diagnostic_exception_used: bool = False,
                    probe_settle_sec_used: float = None,
                    consecutive_threshold_override_used: int = None) -> dict:
    return {
        "run_id": run_id, "plan_id": PLAN_ID, "mode": "no_injection_long", "detector": detector,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "official_threshold": OFFICIAL_THRESHOLD, "candidate_threshold": CANDIDATE_THRESHOLD,
        "candidate_artifacts_dir": str(CANDIDATE_ARTIFACTS_DIR),
        "observation_sec_requested": observation_sec,
        # §195(2026-09-29) - 이 실행 호출에만 적용되는 preview 준비 상한(call-site
        # 스코핑, §193에서 오프라인 검증됨). None이면 arm_controller의 공통
        # 기본값(PREVIEW_PREP_TIMEOUT_SEC=480.0)을 그대로 쓴다는 뜻 - 여기 기록하는
        # 것은 감사 추적용일 뿐, 값 자체를 바꾸지 않는다.
        "preview_prep_timeout_sec_used": preview_prep_timeout_sec,
        # §200(2026-09-30) - True면 이 실행이 node_resource_safety_gate의 표준
        # fail-closed 차단(워커 2개 상황)을 의도적으로 우회했다는 뜻 - 감사
        # 추적용, 결과를 볼 때 반드시 같이 봐야 하는 값이라 최상위에 기록한다.
        "no_injection_diagnostic_exception_used": no_injection_diagnostic_exception_used,
        # 2026-10-01 - rep6/rep7 무주입 실패가 probe 기동 직후 안정화 구간에
        # 집중됐음을 원자료로 확인한 뒤의 "1회 진단용 완화"(원인 수정 아님) -
        # None이면 load_ramp_adapter의 공유 기본값(SETTLE_SEC=60)을 그대로
        # 쓴다는 뜻. 감사 추적용으로만 기록.
        "probe_settle_sec_used": probe_settle_sec_used,
        # §204(2026-10-01) - None이면 gated_signal_detector.py가 공식 CONSECUTIVE_THRESHOLD(3)
        # 그대로 사용. 명시된 값이면 "2연속+mem_slope 게이트" 후보를 이 1회 진단 프로세스의
        # 런타임 판정에만 적용한 것 - 공유 threshold.json·score_server.py는 미변경.
        "consecutive_threshold_override_used": consecutive_threshold_override_used,
        "model_features_consecutive_rule_unchanged": consecutive_threshold_override_used is None,
        "cpu_pressure_intensity_unchanged": None,
        "policy_connection": "capture_sink.py(promotion/전달 코드 없음)로만 연결 - 실제 정책·승격 구조적으로 불가능",
        # §190(2026-09-29) - §189 게이트 60분 라이브에서 probe 원자료가 관찰 종료
        # 훨씬 전에 멈췄는데도(네트워크 단절 의심, 미확정) outcome_status가
        # "completed"로 남았던 문제 재발 방지 - run_no_injection_smoke()가 이제
        # probe_raw_csv_path를 받아 결과에 gap 진단·정리 성공 여부를 남기고,
        # 조건 미달 시 "invalid"로 기록한다(판정 로직·모델·임계값 미변경, 결과
        # 유효성 표시만 추가). 계획된 입력값만 남기고 실제 관찰값은 summary에만 남긴다.
        "probe_validity_check": {
            "enabled": True,
            "probe_data_gap_threshold_sec": PROBE_DATA_GAP_THRESHOLD_SEC,
            "checks": ["trailing_gap_to_observation_end", "max_internal_gap", "cleanup_success"],
        },
        "note": ("§183 no_injection(600s x2, run_no_injection_smoke 재사용)과 동일 코드 - "
                 "관찰시간만 연장한 별도 plan_id 실행. 모델/임계값/정책/CPU강도 미변경, "
                 "CPU 압박 주입 자체가 없음(injector.inject() 미호출)."
                 + (" detector=gated - §189 mem_slope>0 게이트 실험용 탐지기(사후선택, 아직 안전장치 아님), "
                    "이 관찰도 sink 전용이라 실제 정책·승격 구조적으로 불가능." if detector == "gated" else "")),
    }


def summarize(run_id: str, observation_sec: float, outcome: dict, evidence_log_path, detector: str = "candidate",
              no_injection_diagnostic_exception_used: bool = False, probe_settle_sec_used: float = None,
              consecutive_threshold_override_used: int = None) -> dict:
    evaluations = _load_evidence_scores(evidence_log_path)
    scores = [e["score"] for e in evaluations]
    consecutive_values = [e.get("consecutive_anomalous", 0) for e in evaluations]
    summary = {
        "run_id": run_id, "mode": "no_injection_long", "detector": detector,
        # §200(2026-09-30) - 이 실행이 표준 안전 차단을 의도적으로 우회했는지
        # 결과 파일 최상위에도 남긴다(감사용, contract와 동일 값 중복 기록).
        "no_injection_diagnostic_exception_used": no_injection_diagnostic_exception_used,
        # 2026-10-01 - "1회 진단용 완화"(원인 수정 아님) 사용 여부, contract와
        # 동일 값 중복 기록.
        "probe_settle_sec_used": probe_settle_sec_used,
        # §204(2026-10-01) - contract와 동일 값 중복 기록(감사용). "2연속 조건 횟수"·
        # "게이트 통과 횟수"는 아래 기존 original_fire_condition_count/gate_pass_count
        # 필드(이미 evidence에서 집계)로 그대로 확인 가능 - 새 필드 추가 없음.
        "consecutive_threshold_override_used": consecutive_threshold_override_used,
        "outcome_status": outcome["status"], "outcome_reason": outcome["reason"],
        "elapsed_sec": outcome["elapsed_sec"], "observation_sec_requested": observation_sec,
        "prober_stop_failed": outcome["prober_stop_failed"],
        "injector_cleanup_failed": outcome["injector_cleanup_failed"],
        "cleanup_status": outcome.get("cleanup_status"),
        "invalidity_reasons": outcome.get("invalidity_reasons"),
        "observation_ended_at_utc": outcome.get("observation_ended_at_utc"),
        "probe_last_request_utc": outcome.get("probe_last_request_utc"),
        "probe_data_gap_sec": outcome.get("probe_data_gap_sec"),
        "probe_data_gap_detected": outcome.get("probe_data_gap_detected"),
        "probe_max_internal_gap_sec": outcome.get("probe_max_internal_gap_sec"),
        "n_evaluations": len(evaluations),
        "max_consecutive_anomalous_observed": max(consecutive_values) if consecutive_values else None,
        "score_min": min(scores) if scores else None,
        "score_max": max(scores) if scores else None,
        "official_threshold_offline_replay": _advance_streak_offline(scores, OFFICIAL_THRESHOLD, CONSECUTIVE_THRESHOLD),
        "candidate_threshold_offline_replay": _advance_streak_offline(scores, CANDIDATE_THRESHOLD, CONSECUTIVE_THRESHOLD),
    }
    if detector == "gated":
        # §189 - 게이트 실행에서만 있는 필드(원래 발화조건/게이트통과/전송예정)를
        # 원자료에서 그대로 세어 남긴다 - 새 판정 로직을 여기서 만들지 않는다.
        summary["original_fire_condition_count"] = sum(1 for e in evaluations if e.get("original_fire_condition"))
        summary["gate_pass_count"] = sum(1 for e in evaluations if e.get("gate_pass"))
        summary["signal_send_intended_count"] = sum(1 for e in evaluations if e.get("signal_send_intended"))
    return summary


def main():
    parser = argparse.ArgumentParser(description="§188 - 후보 IF 무주입 장기 관찰(45~60분)")
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--observation-sec", type=float, default=MIN_OBSERVATION_SEC,
                         help=f"관찰 시간(초), {MIN_OBSERVATION_SEC:.0f}~{MAX_OBSERVATION_SEC:.0f} 범위")
    parser.add_argument("--probe-config", default=str(DEFAULT_PROBE_CONFIG))
    parser.add_argument("--probe-image", choices=[LR_IMAGE, IMAGE_FORCE_CLOSE], default=LR_IMAGE,
                        help="2026-10-02 - 기본값은 과거 실행과 같은 이미지. V1 새 실행은 force_close 이미지를 명시")
    parser.add_argument("--detector", choices=list(DETECTOR_START_FNS), default="candidate",
                         help="candidate=공식 score_server.py(기존 기본값, §183/§188과 동일) / "
                              "gated=§189 mem_slope>0 게이트 실험용(사후선택, 둘 다 sink 전용)")
    parser.add_argument("--consecutive-threshold-override", type=int, default=None,
                         help="§204 - --detector gated 전용. 생략 시 공식 3연속 그대로. 명시하면 "
                              "gated_signal_detector.py의 이번 1회 프로세스 런타임 판정만 분리 완화함"
                              "(공유 threshold.json·score_server.py의 CONSECUTIVE_THRESHOLD는 미변경, "
                              "sink 전용 연결 유지) - --detector candidate와 함께 주면 실행 거부.")
    parser.add_argument("--dry-run-contract-only", action="store_true")
    parser.add_argument("--preview-prep-timeout-sec", type=float,
                         default=arm_controller.PREVIEW_PREP_TIMEOUT_SEC,
                         help="이 실행 호출에만 적용되는 preview 준비 상한(초). 기본값은 "
                              "arm_controller의 공통 기본값과 동일(변경 없음) - §193에서 "
                              "call-site 스코핑이 다른 러너에 영향 없음을 오프라인 검증함.")
    parser.add_argument("--allow-no-injection-diagnostic-exception", action="store_true",
                         help="§200(2026-09-30) - 기본 off. 켜면 워커 2개 상황의 기존 안전 차단"
                              "(preview_target_node_ambiguous, ALWAYS_BLOCK_NON_NATIVE_ARM)을 "
                              "이 실행에 한해 의도적으로 우회한다 - docs/design/phase8-no-"
                              "injection-gate-options-20260930.md 선택지 B. 무주입 전용 - CPU "
                              "압박 러너는 이 플래그가 없고 이 예외 함수를 import하지 않는다. "
                              "매 실행마다 명시적으로 켜야 하며 영구 설정이 아니다.")
    parser.add_argument("--probe-settle-sec", type=float, default=load_ramp_adapter.SETTLE_SEC,
                         help="2026-10-01 - 기본값은 load_ramp_adapter의 공유 기본값(60초)과 "
                              "동일(변경 없음). rep6/rep7 무주입 시도에서 probe 기동 직후 "
                              "안정화 구간에 실패가 집중된 것을 확인해, 이 실행 호출에만 더 "
                              "긴 값을 시험적으로 줄 수 있게 열어둔 것 - 원인을 고친 것이 "
                              "아니라 1회 진단용 완화(mitigation)다. 공유 SETTLE_SEC 상수 "
                              "자체는 건드리지 않는다.")
    args = parser.parse_args()

    if not (MIN_OBSERVATION_SEC <= args.observation_sec <= MAX_OBSERVATION_SEC):
        raise SystemExit(
            f"--observation-sec은 {MIN_OBSERVATION_SEC:.0f}~{MAX_OBSERVATION_SEC:.0f}s(45~60분) 범위여야 함: "
            f"{args.observation_sec}")
    if args.consecutive_threshold_override is not None:
        # 무시되는 값이 contract에 "사용됨"으로 남는 것을 막는다(감사 기록 오염 방지).
        if args.detector != "gated":
            raise SystemExit("--consecutive-threshold-override는 --detector gated에서만 허용됨")
        if args.consecutive_threshold_override < 1:
            raise SystemExit(f"--consecutive-threshold-override는 1 이상이어야 함: {args.consecutive_threshold_override}")

    scenario = "if_threshold_candidate_no_injection_long"
    detector_suffix = "" if args.detector == "candidate" else f"-{args.detector}"
    # §204 - override 실행은 run_id부터 공식 3연속 결과와 분리(파일명 glob 집계 혼입 방지).
    if args.consecutive_threshold_override is not None:
        detector_suffix += f"-c{args.consecutive_threshold_override}"
    run_id = f"{scenario}{detector_suffix}-proposed-{args.rep:02d}-{PLAN_ID}"
    evidence_log_path = DEMO_RESULTS_DIR / f"detector-evidence-{run_id}.jsonl"
    summary_path = DEMO_RESULTS_DIR / f"summary-{run_id}.json"

    DEMO_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if summary_path.exists():
        raise SystemExit(f"run_id({run_id})의 결과가 이미 존재함 - 중단: {summary_path}")

    contract = build_contract(run_id, args.observation_sec, detector=args.detector,
                               preview_prep_timeout_sec=args.preview_prep_timeout_sec,
                               no_injection_diagnostic_exception_used=args.allow_no_injection_diagnostic_exception,
                               probe_settle_sec_used=args.probe_settle_sec,
                               consecutive_threshold_override_used=args.consecutive_threshold_override)
    contract["probe_image"] = args.probe_image  # 2026-10-02 - 사용한 probe 이미지 태그 기록
    contract_path = DEMO_RESULTS_DIR / f"contract-{run_id}.json"
    contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"계약 기록: {contract_path}")
    if args.dry_run_contract_only:
        print("dry-run - 클러스터 미접속, 여기서 종료")
        return

    injector = make_cpu_pressure_injector(run_id, "proposed", args.rep, stages=DEMO_STAGES)
    injector = arm_controller.wrap_injector_with_preview_prep(
        injector, "proposed", preview_prep_timeout_sec=args.preview_prep_timeout_sec)
    if args.allow_no_injection_diagnostic_exception:
        # §200(2026-09-30) - 명시적으로 켰을 때만: 기존 node_resource_safety_gate의
        # fail-closed 차단(워커 2개일 때 preview_target_node_ambiguous,
        # ALWAYS_BLOCK_NON_NATIVE_ARM)을 이번 1회 실행에 한해 의도적으로 우회한다.
        # 두 기존 게이트 함수 자체는 변경하지 않았고, 이 분기를 안 타면(기본값)
        # 아래 §198 표준 게이트가 그대로 적용된다.
        print("[§200] 무주입 진단 예외 활성화 - 워커 2개 상황의 표준 안전 차단을 "
              "이번 1회 실행에 한해 의도적으로 우회함(docs/design/phase8-no-injection-"
              "gate-options-20260930.md 선택지 B)")
        injector = node_resource_safety_gate.wrap_injector_with_no_injection_diagnostic_exception(
            injector, "proposed", "no_injection_long")
    else:
        # §198 - rep5(NodeNotReady) 이후 preview 생성 전 워커 노드 headroom 게이트가
        # 이 60분 무주입 러너에는 배선돼 있지 않았음을 재확인(코드 감사) - 다른 CPU
        # 데모 러너 5개와 동일한 방식으로 최소 배선한다. node_resource_safety_gate.py의
        # ALWAYS_BLOCK_NON_NATIVE_ARM(§197)이 그대로 적용되므로 새 임계값을 만들지
        # 않는다 - 방어 가능한 통과 기준이 생기기 전까지 이 러너도 fail-closed다.
        injector = node_resource_safety_gate.wrap_injector_with_node_resource_gate(injector, "proposed")
    if args.probe_settle_sec != load_ramp_adapter.SETTLE_SEC:
        print(f"[진단용 완화] probe settle_sec={args.probe_settle_sec:.0f}s(공유 기본값 "
              f"{load_ramp_adapter.SETTLE_SEC:.0f}s에서 이 실행 1회만 연장) - 원인 수정 아님, "
              f"probe 기동 직후 안정화 구간 실패에 대한 1회 진단용 완화")
    prober = make_load_ramp_prober(args.probe_config, run_id, scenario, "proposed", args.rep,
                                    args.observation_sec + PROBE_LIFETIME_MARGIN_SEC,
                                    settle_sec=args.probe_settle_sec, image=args.probe_image)
    probe_raw_csv_path = PROBE_RESULTS_DIR / f"probe-{run_id}-proposed-{args.rep}-raw.csv"
    sink_port = _pick_free_loopback_port()
    sink_out_path = DEMO_RESULTS_DIR / f"sink-captured-{run_id}.jsonl"

    detector_start_fn = DETECTOR_START_FNS[args.detector]
    if args.detector == "gated" and args.consecutive_threshold_override is not None:
        print(f"[§204] 연속판정 1회 진단용 완화 - consecutive_threshold="
              f"{args.consecutive_threshold_override}(공유 threshold.json·공식 CONSECUTIVE_THRESHOLD="
              f"{CONSECUTIVE_THRESHOLD}은 미변경, sink 전용 연결 유지, 실제 승격에 연결되지 않음)")
        detector_start_fn = make_gated_detector_with_consecutive_override(args.consecutive_threshold_override)

    print(f"run_id: {run_id} / 관찰: {args.observation_sec:.0f}s / sink_port={sink_port} / detector={args.detector}")

    outcome = run_no_injection_smoke(run_id, injector, prober, sink_port, evidence_log_path, sink_out_path,
                                      probe_raw_csv_path=probe_raw_csv_path,
                                      observation_sec=args.observation_sec,
                                      detector_start_fn=detector_start_fn)

    summary = summarize(run_id, args.observation_sec, outcome, evidence_log_path, detector=args.detector,
                         no_injection_diagnostic_exception_used=args.allow_no_injection_diagnostic_exception,
                         probe_settle_sec_used=args.probe_settle_sec,
                         consecutive_threshold_override_used=args.consecutive_threshold_override)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"요약 기록: {summary_path}")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
