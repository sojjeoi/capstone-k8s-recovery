#!/usr/bin/env python3
"""c3 정상 세션 수집 실행기(2026-10-10) - PREREG E·H·K·Q절, runbook C3-COLLECTION-RUNBOOK-DRAFT.md.

수집 경로는 N1과 같다: run_guarded_normal_bc.run_normal(파일 수정 없음)을 그대로 호출하고, 계열 전역(PLAN_ID·CONTRACT_DOC·
RESULTS_DIR·RUNS·check_schedule·freeze 3개)만 호출 동안 c3 값으로 바꿨다가 finally에서 되돌린다(c3_common.patched_n1).
→ probe 1 RPS, record_only, c1_bck_shadow_pair(송신 없음), 관찰 4200 s, canonical 시작·끝 검사, 미승격 preview 정리.
c3가 더하는 것(세션 선택에 탐지기 결과를 쓰지 않음):
  - 순서·역할·블록·시도 상한·중단 규칙(c3 schedule), 예비 ID 대체 기록, 블록 경계 active Pod UID 변경 확인
  - Rollout 스냅샷 기록(generation·Service selector·endpoints·Pod restart 수 - 시작·끝), PC 전원 사건 기록
[USER APPROVAL REQUIRED] --plan·--record-replacement 외 모드(--preflight-only 포함)는 클러스터에 접속한다. 실행(기본 모드)은
preview 생성·probe 부하를 만든다. 사용자 승인 없이 실행하지 않는다."""
import sys
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import copy  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import c3_common as cc  # noqa: E402

FREEZE_VERSION = "c3-collection-v1"
FREEZE_MANIFEST = "experiments/c3_normal_coverage/c3-collection-freeze-manifest.json"
PROVENANCE_REQUIRED = {"contract": cc.PREREG_DOC,
                       "session_manifest": "experiments/c3_normal_coverage/c3-session-manifest.json",
                       "runner": "experiments/c3_normal_coverage/run_c3_collection.py",
                       "common": "experiments/c3_normal_coverage/c3_common.py",
                       "validity": "experiments/c3_normal_coverage/c3_session_validity.py",
                       "n1_runner": "experiments/run_guarded_normal_bc.py",
                       "analyzer": "experiments/guarded_normal_bc_analysis.py",
                       "shadow_detector": "anomaly-detection/c1_bck_shadow_pair.py",
                       "bck_detector": "anomaly-detection/c1_bck_live_detector.py",
                       "rule_spec": "anomaly-detection/bck_rules/rule-spec-v1.json",
                       # 공식 c1 행 선택·5 s 표본기 경로(수정 없이 재사용) - 동결 뒤 바뀌면 실행 거부
                       "identity_sampler": "experiments/run_v1_detector_trial.py",
                       "with_sampler": "experiments/run_detector_switch_ab_trial.py",
                       "timed_switch": "experiments/run_timed_switch_recovery_trial.py",
                       "c1_builder": "experiments/c1_collection.py",
                       "expected_pods": "experiments/input_capture_analysis.py",
                       "input_integrity": "anomaly-detection/input_integrity.py",
                       "c1_shadow_detector": "anomaly-detection/c1_shadow_detector.py",
                       "train_calibrate": "experiments/c3_normal_coverage/train_calibrate_c3.py",
                       # c3 평가·검증 도구와 c1 판정 의미를 정하는 의존 파일(동결 뒤 바뀌면 실행 거부)
                       "eval_validation": "experiments/c3_normal_coverage/eval_c3_validation.py",
                       "pressure": "experiments/c3_normal_coverage/pressure_c3.py",
                       "row_invariant": "experiments/c3_normal_coverage/c3_row_invariant.py",
                       "c1_feature_schema": "anomaly-detection/v3/model_v33_c1/artifacts/feature-schema.json",
                       "c1_model": "anomaly-detection/v3/model_v33_c1/artifacts/model.pkl",
                       "c1_scaler": "anomaly-detection/v3/model_v33_c1/artifacts/scaler.pkl",
                       "c1_threshold": "anomaly-detection/v3/model_v33_c1/artifacts/threshold.json",
                       "c1_trainer": "anomaly-detection/v3/model_v33_c1/train_calibrate_v33c1.py",
                       "c1_data_manifest": "anomaly-detection/v3/model_v33_c1/c1-data-manifest.json",
                       "threshold_calibration": "anomaly-detection/v3/model_v31/replay.py",
                       "feature_selection": "anomaly-detection/v3/model_v31/feature_selection.py",
                       "model_params": "anomaly-detection/v3/model_v32b/train_v32b.py",
                       "streak_semantics": "anomaly-detection/score_server.py",
                       "slo_judge": "experiments/slo_judge.py",
                       "session_wrapper": "experiments/guarded_pilot_session.ps1",
                       "runbook": "experiments/results/c3_normal_coverage_prereg_20261010/C3-COLLECTION-RUNBOOK-DRAFT.md"}
POWER_EVENT_IDS = {"Microsoft-Windows-Kernel-Power": (41, 42, 109, 506, 507),
                   "Microsoft-Windows-Power-Troubleshooter": (1,)}


def _rec_path(d, sid):
    return Path(d) / f"c3-session-{sid}.json"


def _read(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def attempts(m, d) -> list:
    """이미 끝난 시도(c3 세션 기록 있음)를 시도 순서대로."""
    ids = [x["id"] for x in m["sessions"]] + list(m["spares"])
    return sorted((_read(_rec_path(d, s)) for s in ids if _rec_path(d, s).exists()), key=lambda r: r["attempt_number"])


def replacement_for(d, sid):
    p = Path(d) / f"c3-replacement-{sid}.json"
    return _read(p)["replaces"] if p.exists() else None


def schedule(m, run, d) -> dict:
    """c3 순서 규칙(수집 결과·탐지기 점수는 보지 않음 - 시도 여부와 운영 유효성 판정 파일만)."""
    plan = cc.planned(m)
    if run not in plan and run not in m["spares"]:
        raise SystemExit(f"{run} 거부: 매니페스트에 없는 ID")
    if _rec_path(d, run).exists():
        raise SystemExit(f"{run} 거부: 이미 시도한 ID(재사용 금지)")
    done = attempts(m, d)
    missing_validity = [r["run"] for r in done if not (Path(d) / f"{r['run']}-validity.json").exists()]
    if missing_validity:
        raise SystemExit(f"{run} 거부: 이전 시도의 유효성 판정이 없음 {missing_validity}")
    status = [_read(Path(d) / f"{r['run']}-validity.json")["status"] for r in done]
    if len(status) >= 2 and status[-1] == status[-2] == "invalid":
        raise SystemExit("중단 규칙: invalid 2회 연속 - 사용자 확인 필요")
    if status.count("unexpected_degradation") >= 2:
        raise SystemExit("중단 규칙: unexpected_degradation 2회 - 사용자 확인 필요")
    if any(r.get("cleanup_errors") for r in done):
        raise SystemExit("중단 규칙: 정리 오류 1회 - 사용자 확인 필요")
    if len(done) + 1 > m["max_attempts"]:
        raise SystemExit(f"중단 규칙: 시도 상한 {m['max_attempts']} 도달")
    replaces = None
    if run in plan:
        before = [s["id"] for s in m["sessions"] if s["order"] < plan[run]["order"]]
        not_tried = [s for s in before if not _rec_path(d, s).exists()]
        if not_tried:
            raise SystemExit(f"{run} 거부: 순서 밖 - 먼저 시도할 계획 세션 {not_tried}")
        role, block = plan[run]["role"], plan[run]["block"]
    else:
        replaces = replacement_for(d, run)
        if replaces is None:
            raise SystemExit(f"{run} 거부: 예비 ID 대체 기록(c3-replacement-{run}.json) 없음")
        used = [s for s in m["spares"] if _rec_path(d, s).exists()]
        if m["spares"][len(used)] != run:
            raise SystemExit(f"{run} 거부: 예비 ID는 순서대로({m['spares'][len(used)]} 먼저)")
        slot = cc.slot_of(m, run, cc.load_replacements(m, d))      # 예비→예비 사슬은 처음 계획 자리로
        role, block = plan[slot]["role"], plan[slot]["block"]
    return {"session": run, "role": role, "block": block, "replaces": replaces, "attempt_number": len(done) + 1,
            "required_before": [], "counted": True}


def record_replacement(m, spare, target, d) -> Path:
    """예비 ID 대체 기록(오프라인). 대상 = 계획 세션 또는 이미 시도한 예비(그 예비가 채우던 같은 계획 자리를 다시 채움).
    대상은 유효성 판정이 normal_valid가 아니어야 하며, 같은 대상을 두 번 대체하지 않는다(자리마다 현재 차지한 세션만 대체)."""
    if spare not in m["spares"] or (target not in cc.planned(m) and not (target in m["spares"] and _rec_path(d, target).exists())):
        raise SystemExit("예비·대상 ID가 매니페스트와 맞지 않음(대상은 계획 세션 또는 이미 시도한 예비)")
    vp = Path(d) / f"{target}-validity.json"
    if not vp.exists() or _read(vp)["status"] == "normal_valid":
        raise SystemExit(f"{target}: 유효성 판정이 없거나 normal_valid - 대체 대상 아님")
    if any(replacement_for(d, s) == target for s in m["spares"]):
        raise SystemExit(f"{target}: 이미 대체 기록 있음")
    return cc.write_new_json(Path(d) / f"c3-replacement-{spare}.json",
                             {"spare": spare, "replaces": target, "recorded_at_utc": datetime.now(timezone.utc).isoformat()})


def block_boundary_check(m, run, sched, d, snapshot_fn) -> dict:
    """블록 첫 계획 세션이면 지금 active Pod UID가 이전 시도들의 시작 active UID와 달라야 한다(PREREG H-5, 읽기만)."""
    plan = cc.planned(m)
    first_of_block = run in plan and sched["block"] > 1 and \
        min(s["order"] for s in m["sessions"] if s["block"] == sched["block"]) == plan[run]["order"]
    if not first_of_block:
        return {"checked": False}
    import guarded_cleanup_gate as gcg
    p0 = gcg.active_pod(snapshot_fn() or {})
    uid = p0["uid"] if p0 else None
    prev = sorted({r.get("start_active_uid") for r in attempts(m, d)} - {None})
    if uid is None or uid in prev:
        raise SystemExit(f"블록 경계 거부: active Pod UID {uid}가 이전 블록과 같거나 확인 불가 - 사용자 결정 필요(PREREG H-5)")
    return {"checked": True, "active_uid": uid, "previous_uids": prev}


def logging_snapshot(fn, path):
    """deps.canonical_snapshot(읽기만)을 감싸 매 호출 결과를 JSONL로 남긴다(template 제외)."""
    def wrapped():
        s = fn()
        with open(cc.check_write_path(path), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({k: v for k, v in (s or {}).items() if k != "template"}, ensure_ascii=False, default=str) + "\n")
        return s
    return wrapped


def windows_power_events(start_utc: str, end_utc: str) -> list:  # pragma: no cover - 로컬 PC 이벤트 로그 읽기(클러스터 아님)
    flt = ", ".join(f"@{{ProviderName='{p}'; Id={','.join(map(str, ids))}; StartTime=[datetime]'{start_utc}'; "
                    f"EndTime=[datetime]'{end_utc}'}}" for p, ids in POWER_EVENT_IDS.items())
    ps = (f"$r=@(); foreach($f in @({flt})){{ try {{ $r += Get-WinEvent -FilterHashtable $f -ErrorAction Stop }} catch {{}} }}; "
          "$r | Select-Object TimeCreated,ProviderName,Id | ConvertTo-Json -Compress")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"전원 이벤트 조회 실패: {out.stderr[-300:]}")
    txt = out.stdout.strip()
    rows = json.loads(txt) if txt else []
    return rows if isinstance(rows, list) else [rows]


def c3_starter(base_start, results_dir, box, lifecycle_errors, *, sampler_cls=None):
    """탐지기 시작 = N1 shadow 탐지기 + 공식 c1 5 s 표본기(run_v1_detector_trial.V1NoInjectionSampler)를
    공식 run_detector_switch_ab_trial.with_sampler로 함께 켜고 끈다 - c1 수집(run_observation)과 같은 조립.
    표본기는 탐지기 입력이 아니다: 공식 기대 Pod 판정(행 선택)과 무결성 증거용.
    표본기 수명 오류(시작 실패 = HarnessCorrupted 중단, 정지 확인 실패 = 정리 오류)는 c1 수집과 같은 공식 처리다. 표본 공백
    자체는 세션 유효성 판정에 쓰지 않고, 그 창만 공식 기대 Pod 판정에서 제외된다."""
    def start(run_id, evidence_log_path=None, sink_url=None):
        import run_detector_switch_ab_trial as ab
        import run_v1_detector_trial as v1
        sampler = (sampler_cls or v1.V1NoInjectionSampler)(
            injector=box["injector"], send_signal=False, run_id=run_id,
            evidence_path=Path(results_dir) / f"samples-{run_id}.jsonl", switch_at_sec=None)
        return ab.with_sampler(base_start(run_id, evidence_log_path, sink_url), sampler, lifecycle_errors)
    return start


def identity_metadata(injector, samples_path) -> dict:
    """무결성 메타데이터(세션 무효 기준 아님): 공식 c1 요약과 같은 active_target·preview_prep + 표본 통계."""
    out = {}
    try:
        out["active_target"] = (injector.get_stress_delivery_record() or {}).get("target")
    except Exception as e:  # noqa: BLE001
        out["active_target"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        import run_detector_switch_ab_trial as ab
        out["preview_prep"] = ab._preview_prep_info(injector)
    except Exception as e:  # noqa: BLE001
        out["preview_prep"] = {"fetch_error": f"{type(e).__name__}: {e}"}
    ss = [json.loads(x) for x in Path(samples_path).read_text(encoding="utf-8").splitlines() if x.strip()] \
        if Path(samples_path).exists() else []
    ts_ = [cc.ts(s["t"]) for s in ss]
    out["identity_recorder"] = {
        "samples_file": Path(samples_path).name, "count": len(ss), "first_t": ss[0]["t"] if ss else None,
        "last_t": ss[-1]["t"] if ss else None,
        "max_gap_sec": max(((b - a).total_seconds() for a, b in zip(ts_, ts_[1:])), default=None),
        "records_with_query_error": sum(any(k.endswith("_error") for k in s) for s in ss),
        "note": "기록·보고 전용 - 누락은 행 선택(공식 기대 Pod 판정)에서만 반영, 세션 무효 기준이 아님"}
    return out


def run_c3(run, *, deps, rn, manifest=None, results_dir=None, preflight_only=False, code_provenance=None,
           power_events_fn=windows_power_events, run_normal_fn=None) -> dict:
    """[USER APPROVAL REQUIRED] 실제 deps면 클러스터 접속·workload. 테스트는 가짜 deps·run_normal_fn."""
    m = cc.load_manifest(manifest)
    d = cc.check_write_path(results_dir or cc.COLLECTION_DIR)
    sched = schedule(m, run, d)
    started = datetime.now(timezone.utc).isoformat()
    deps = copy.copy(deps)                      # 호출자 객체는 바꾸지 않음
    deps.canonical_snapshot = logging_snapshot(deps.canonical_snapshot, d / f"c3-k8s-snapshots-{run}.jsonl")
    box, lifecycle_errors = {"injector": None}, []
    base_make_injector = getattr(deps, "make_injector", None)
    if base_make_injector is not None:
        def make_injector(*a, **k):             # 표본기가 공식 c1처럼 injector의 대상(active target)을 읽도록 보관
            box["injector"] = base_make_injector(*a, **k)
            return box["injector"]
        deps.make_injector = make_injector
    boundary = block_boundary_check(m, run, sched, d, deps.canonical_snapshot)
    run_normal_fn = run_normal_fn or rn.run_normal
    with cc.patched_n1(rn, runs=cc.runs_table(m), schedule_fn=lambda r, _d: sched, results_dir=d,
                       freeze=(FREEZE_VERSION, FREEZE_MANIFEST, PROVENANCE_REQUIRED)):
        run_id = rn.run_id_for(run)
        summary = run_normal_fn(run, deps=deps, preflight_only=preflight_only, results_dir=d,
                                code_provenance=code_provenance,
                                starter=lambda rd: c3_starter(rn.shadow_starter(rd), rd, box, lifecycle_errors))
    if preflight_only:
        return {"preflight_only": True, "schedule": sched, "block_boundary": boundary, "run_id": run_id}
    ended = datetime.now(timezone.utc).isoformat()
    ident = identity_metadata(box["injector"], d / f"samples-{run_id}.jsonl")
    try:
        power, power_err = power_events_fn(started, ended), None
    except Exception as e:  # noqa: BLE001
        power, power_err = None, f"{type(e).__name__}: {e}"
    pf = d / str(summary.get("preflight_file") or "")
    start_pod = (_read(pf).get("canonical_start") or {}).get("start_pod") if pf.is_file() else None
    rec = {"run": run, "run_id": run_id, "plan_id": cc.PLAN_ID,
           **{k: sched[k] for k in ("role", "block", "replaces", "attempt_number")},
           "manifest_sha256": cc.sha256(manifest or cc.MANIFEST), "runner_started_at_utc": started,
           "runner_ended_at_utc": ended, "start_active_pod": start_pod,
           "start_active_uid": start_pod[1] if start_pod else None, "block_boundary": boundary,
           "workload": {"probe_rps": 1, "probe_config": str(rn.smoke.DEFAULT_PROBE_CONFIG),
                        "observation_sec": m["observation_sec"]},
           "power_events": power, "power_events_error": power_err,
           "k8s_snapshots_file": f"c3-k8s-snapshots-{run}.jsonl", "pod_events_file": f"pod-events-{run_id}.json",
           **ident, "sampler_lifecycle_errors": lifecycle_errors, "outcome_status": summary.get("outcome_status"),
           "cleanup_errors": list(summary.get("cleanup_errors") or []) + lifecycle_errors}
    cc.write_new_json(_rec_path(d, run), rec)
    return {"summary": summary, "c3_record": rec}


def write_freeze_manifest(root=cc.ROOT) -> Path:
    """수집 직전 동결(PREREG T-3, 사용자 승인 뒤 1회). 필수 파일 해시를 고정 - 실행 때 rn.verify_provenance가 다시 확인."""
    files = {rel: cc.sha256(Path(root) / rel) for rel in sorted(set(PROVENANCE_REQUIRED.values()))}
    p = cc.check_write_path(Path(root) / FREEZE_MANIFEST)
    with open(p, "x", encoding="utf-8") as fh:
        fh.write(json.dumps({"status": "frozen", "version": FREEZE_VERSION, "plan_id": cc.PLAN_ID,
                             "created_at_utc": datetime.now(timezone.utc).isoformat(), "files": files},
                            ensure_ascii=False, indent=2))
    return p


def plan_text(m, d) -> str:
    done = {r["run"]: r for r in attempts(m, d)} if Path(d).exists() else {}
    lines = [f"{s['order']:>2} {s['id']} {s['role']:<11} block {s['block']}  {'시도함' if s['id'] in done else '-'}"
             for s in m["sessions"]]
    return "\n".join(lines + [f"예비: {m['spares']}  시도 {len(done)}/{m['max_attempts']}"])


def main(argv=None) -> int:  # pragma: no cover - 라이브 진입점
    p = argparse.ArgumentParser(description="c3 정상 세션 수집 - [USER APPROVAL REQUIRED] --plan·--record-replacement 외 모드는 클러스터 접속")
    p.add_argument("--run")
    p.add_argument("--mode", choices=["record_only"], default="record_only")  # 세션 실행기 호환
    p.add_argument("--policy-url", default="http://127.0.0.1:18090")
    p.add_argument("--session-file", default=None)
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument("--plan", action="store_true", help="계획·진행만 출력(클러스터 접속 없음)")
    p.add_argument("--record-replacement", nargs=2, metavar=("SPARE", "TARGET"), help="예비 ID 대체 기록(오프라인)")
    p.add_argument("--write-freeze-manifest", action="store_true", help="수집 직전 1회(사용자 승인 뒤)")
    a = p.parse_args(argv)
    m = cc.load_manifest()
    if a.plan:
        print(plan_text(m, cc.COLLECTION_DIR))
        return 0
    if a.record_replacement:
        print(record_replacement(m, *a.record_replacement, cc.COLLECTION_DIR))
        return 0
    if a.write_freeze_manifest:
        print(write_freeze_manifest())
        return 0
    if not a.run:
        raise SystemExit("--run 필요")
    import run_guarded_normal_bc as rn
    import run_timed_switch_recovery_trial as rt
    n1_dir = rn.RESULTS_DIR
    token = os.environ.get("GUARDED_ADMIN_TOKEN")
    if not token:
        raise SystemExit("GUARDED_ADMIN_TOKEN 환경변수 필요")
    with cc.patched_n1(rn, runs=cc.runs_table(m), results_dir=cc.COLLECTION_DIR,
                       freeze=(FREEZE_VERSION, FREEZE_MANIFEST, PROVENANCE_REQUIRED)):
        prov = rn.verify_provenance()
    deps = rn.real_deps(a.policy_url, token, a.session_file)
    base = deps.check_unresolved

    def check_unresolved():  # run_c3 안(전역 c3)에서 호출되면 c3 폴더 + 기존 폴더, 그리고 N1 폴더도 확인
        base()
        rt.check_no_unresolved_failures(n1_dir)
    deps.check_unresolved = check_unresolved
    r = run_c3(a.run, deps=deps, rn=rn, preflight_only=a.preflight_only, code_provenance=prov)
    errs = (r.get("summary") or {}).get("cleanup_errors")
    print(json.dumps({"run": a.run, "outcome": (r.get("summary") or {}).get("outcome_status"), "cleanup_errors": errs,
                      "preflight_only": r.get("preflight_only", False)}, ensure_ascii=False, default=str))
    return 2 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
