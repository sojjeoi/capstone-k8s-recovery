#!/usr/bin/env python3
"""Phase 2 detector selection 공용 부품(2026-10-11) - 설계 동결 문서(수정 금지):
docs/design/PHASE2-DETECTOR-SELECTION-PREREG-v1-rev6.md rev 6 (design freeze 교체 docs/design/phase2-detector-selection-design-freeze-rev6.json,
rev 5와 그 동결 기록은 supersede된 이력 - 수정 금지)

이 모듈이 정하는 것(prereg 절):
  - 후보 2개의 동결 식별·임계값·artifact 기대 해시(§5·§5.1) - profile은 이 값과 정확히 같아야 한다(다르면 PROFILE_INVALID)
  - Environment Profile 읽기·형식 검사(§4.2: ACTIVE 0/1개, threshold 필수, 절대 경로 금지) → ProfileInvalid
  - 외부 artifact 검사(§5.1, 기본 경로 추측 없음) → ProfileInvalid
  - 환경 관측값과 profile 비교(§4.4 PROFILE_STALE)
  - 판정 정규화(§4.1 NORMAL/ANOMALY/UNKNOWN), 정책 신호 식별(c1-signal-v2 identity·context)
  - active profile 기계적 생성과 diff 검사(§8.3), implementation freeze 검증(§18-B)
판정 상태기계(3연속·쿨다운 60 s·건너뜀 리셋)는 동결 score_server.advance_streak를 그대로 쓴다. 클러스터 접근 없음."""
import sys
sys.dont_write_bytecode = True
import copy  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
ROOT = EXP.parent
AD = ROOT / "anomaly-detection"
for _p in (EXP, AD, AD / "v3" / "model_v31", EXP / "c3_normal_coverage", ROOT / "recovery-policy"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

PLAN_ID = "capstone-phase2-detector-selection-v1"
PREREG_DOC = "docs/design/PHASE2-DETECTOR-SELECTION-PREREG-v1-rev6.md"
PREREG_SHA256 = "a6512ff03f33fb96b1d65031ff7c03f84c26fdfd74dd833e1516336110bad7d3"   # rev 6 design freeze(LF blob 기준)
DESIGN_FREEZE = "docs/design/phase2-detector-selection-design-freeze-rev6.json"
# supersede된 rev 5 - 이력 증명용으로 implementation freeze에 해시만 고정(실행 기준 아님)
SUPERSEDED = ("docs/design/PHASE2-DETECTOR-SELECTION-PREREG-v1.md", "docs/design/phase2-detector-selection-design-freeze.json")
SELECTION_PROFILE = HERE / "profiles" / "selection-profile-cpu-vllm-qwen05b-v1.json"
IMPLEMENTATION_FREEZE = "experiments/phase2_detector_selection/phase2-detector-selection-implementation-freeze.json"

CPU_ID, IF_ID = "cpu-threshold-c", "c3-a-if"
DETECTOR_IDS = (CPU_ID, IF_ID)
SIMPLICITY_ORDER = (CPU_ID, IF_ID)          # §7.3 Tie-break 2(사전 고정) - 앞의 모든 단계가 동등일 때만 쓴다
ACTIVE, SHADOW = "ACTIVE", "SHADOW"
NORMAL, ANOMALY, UNKNOWN = "NORMAL", "ANOMALY", "UNKNOWN"
CPU_THRESHOLD = 1.6842544293089357           # §5 A(B/C/K arm C) - cpu_mean > T(엄격)
IF_THRESHOLD = -0.1948341270493127           # §5 B(c3-A) - decision_function < T(엄격)
RULE_SPEC_REL = "anomaly-detection/bck_rules/rule-spec-v1.json"
SKLEARN_VERSION = "1.9.0"
C3A_SERIES, C3A_PLAN = "c3-normal-coverage", "capstone-c3-normal-coverage-20261010"
C3A_FILES = {"model.pkl": "model_sha256", "scaler.pkl": "scaler_sha256", "feature-schema.json": "feature_schema_sha256",
             "threshold.json": "threshold_sha256", "freeze.json": "freeze_sha256"}

# §7 고정 값(§17 - 결과를 본 뒤 바꾸지 않음)
N_NORMAL, N_PRESSURE, N_STAGE2 = 4, 5, 3
MAX_ATTEMPTS = {"normal": 6, "pressure": 7, "stage2": 5}
GATE2_LIMIT_SEC, GATE2_MIN_PASS = 363.0, 4
GATE3_MIN_DIFF = 2          # 합계 차이 ≥ 2 → 적은 쪽, ≤ 1 → 동등
TIMING_MIN_DIFF_SEC = 30.0  # 중앙값 차이 ≥ 30 s → 빠른 쪽
DECISION_SEC = 3600.0

# §5·§5.1 동결 후보 정의(role 제외). profile의 detectors 블록은 role만 빼고 이것과 정확히 같아야 한다.
FROZEN_DETECTORS = {
    CPU_ID: {"detector_id": CPU_ID, "detector_version": "fixed-cpu-gt-1.6842544293089357", "threshold": CPU_THRESHOLD,
             "artifact": {"artifact_id": "bck-rule-spec-v1/arm-C",
                          "rule_spec_sha256": "b0113f174893173cbf208d4f587b541f5f4cfa21c6a362cc535ffd5d06ce4630"}},
    IF_ID: {"detector_id": IF_ID, "detector_version": C3A_PLAN, "threshold": IF_THRESHOLD,
            "artifact": {"artifact_id": f"{C3A_SERIES}/{C3A_PLAN}",
                         "model_sha256": "24cec78e658833dd24957ff49a8c39499403db2a7e8a0c7e752aff61ba3668cf",
                         "scaler_sha256": "7970aa165606bfca1d632af448ddbf502e1596a6fdcbb3d8eaa5393f7da1a67e",
                         "feature_schema_sha256": "9d8ddb6eb45448361015f58c42c7189af59b649b5f52781c677b8f33a45fbc51",
                         "threshold_id": "-0.1948341270493127",
                         "threshold_sha256": "47eb6ff2c9618586e788507f45b10c1bd5d78ac7a32ad3dc4d551b2abf2266dd",
                         "freeze_sha256": "3fa6c1696ab3dd2f0c1b1e1db8fd4398c9c7b2a5c8794a4e3e5431c828700e3c",
                         "sklearn_version": SKLEARN_VERSION}},
}
# profile environment의 계약 해시 원천(LF 정규화 텍스트 해시 - checkout 줄바꿈과 무관)
SLO_SOURCES = ("experiments/slo_judge.py", "experiments/slo_episodes.py")
POLICY_SOURCES = ("recovery-policy/guarded_policy.py", "recovery-policy/guarded_schema.py",
                  "recovery-policy/guarded_state.py", "recovery-policy/guarded_ledger.py",
                  "recovery-policy/guarded_k8s.py", "recovery-policy/main_guarded.py")
_ABS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")
# implementation freeze에 반드시 들어가야 하는 파일(git add 누락 방지 - 작성·검증 양쪽에서 확인)
FREEZE_REQUIRED = tuple(f"experiments/phase2_detector_selection/{f}" for f in (
    "p2_common.py", "p2_executor.py", "selection.py", "run_stage1.py", "run_stage2.py", "write_implementation_freeze.py",
    "test_phase2_detector_selection.py", "PHASE2-STAGE1-RUNBOOK.md", "KNOWN-BASELINE-FAILURES.txt",
    "profiles/selection-profile-cpu-vllm-qwen05b-v1.json")) + (
    PREREG_DOC, DESIGN_FREEZE, *SUPERSEDED, RULE_SPEC_REL, "experiments/run_guarded_normal_bc.py", "experiments/run_guarded_bck.py",
    "experiments/run_guarded_pilot.py", "experiments/guarded_ab_analysis.py", "experiments/guarded_normal_bc_analysis.py",
    "experiments/guarded_cleanup_gate.py", "experiments/old_policy_isolation.py", "experiments/guarded_pilot_session.ps1",
    "experiments/c3_normal_coverage/c3_common.py", "experiments/c3_normal_coverage/c3_session_validity.py",
    "experiments/c3_normal_coverage/eval_c3_validation.py", "experiments/c3_normal_coverage/run_c3_collection.py",
    "anomaly-detection/c1_shadow_detector.py", "anomaly-detection/c1_bck_live_detector.py",
    "anomaly-detection/c1_policy_link.py", "anomaly-detection/score_server.py", "recovery-policy/guarded_policy.py")


class ProfileInvalid(RuntimeError):
    """PROFILE_INVALID(§4.4·§5.1) - non-actionable, 실행기 시작 거부."""


class ArtifactMismatch(ProfileInvalid):
    """detector artifact/버전/임계값 해시 불일치(§4.4: 재개 못 하면 INTEGRITY_FAILURE) - 메시지가 아니라 형식으로 구분한다."""


# ---------------- 해시 ----------------
def sha256_bytes(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def expected_if_hashes() -> dict:
    """c3-A 동결 기대 해시(SHA256SUMS 형식) - 기록값끼리가 아니라 동결값과 비교할 때 쓴다."""
    a = FROZEN_DETECTORS[IF_ID]["artifact"]
    return {f: a[k] for f, k in sorted(C3A_FILES.items())}


def text_sha256(p) -> str:
    """CRLF→LF 정규화 내용 해시(Phase 1 줄바꿈 provenance 문제 회피 - 커밋 blob과 같은 값)."""
    return hashlib.sha256(Path(p).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def canonical(obj) -> bytes:
    """결정적 직렬화(키 정렬·고정 구분자·UTF-8) - 같은 의미면 같은 바이트."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def obj_sha256(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def write_json_new(p, obj) -> Path:
    """새 파일로만 쓴다(덮어쓰기 금지 - 판정·증거 파일을 결과를 본 뒤 바꾸지 못하게)."""
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "xb") as fh:
        fh.write(canonical(obj) + b"\n")
    return p


# ---------------- 계약 해시(§4.2 environment) ----------------
def contract_hashes() -> dict:
    """workload·SLO·telemetry 계약 해시와 정책 소스 해시. 코드 상수·저장소 파일에서만 계산(클러스터 접근 없음)."""
    import features
    import input_integrity as ii
    import run_if_threshold_candidate_smoke as smoke
    import run_timed_switch_recovery_trial as rt
    from load_ramp_adapter import IMAGE_FORCE_CLOSE, IMAGE_FORCE_CLOSE_ID
    from score_server import EVAL_INTERVAL_SEC, FRESHNESS_PROBE_QUERIES, WINDOW_SEC
    workload = {"probe_config_sha256": text_sha256(smoke.DEFAULT_PROBE_CONFIG), "probe_image": IMAGE_FORCE_CLOSE,
                "probe_image_id": IMAGE_FORCE_CLOSE_ID, "stress_stages": rt.DEMO_STAGES}
    telemetry = {"cpu": ii.cpu_rate_query(), "metrics": {k: features.METRICS[k] for k in ("memory", "queue", "cache")},
                 "freshness": FRESHNESS_PROBE_QUERIES, "window_sec": WINDOW_SEC, "eval_interval_sec": EVAL_INTERVAL_SEC}
    return {"workload_contract": obj_sha256(workload),
            "slo_contract": obj_sha256({p: text_sha256(ROOT / p) for p in SLO_SOURCES}),
            "telemetry_contract": obj_sha256(telemetry),
            "recovery_policy_sha256": obj_sha256({p: text_sha256(ROOT / p) for p in POLICY_SOURCES})}


def env_from_template(template: dict) -> dict:
    """Rollout spec.template(읽기 전용 스냅샷 gcg.read_snapshot()["template"] 또는 gitops 매니페스트) → serving 계약."""
    cs = [c for c in ((template or {}).get("spec") or {}).get("containers") or [] if c.get("name") == "vllm"]
    if len(cs) != 1:
        raise ProfileInvalid(f"vllm 컨테이너가 정확히 1개가 아님: {len(cs)}")
    c = cs[0]
    args = list(c.get("args") or [])
    models = [a.split("=", 1)[1] for a in args if a.startswith("--model=")]
    res = c.get("resources") or {}
    gpu = any("gpu" in k for part in res.values() for k in (part or {}))
    return {"serving_backend": {"image": c.get("image"), "image_pull_policy": c.get("imagePullPolicy")},
            "model": models[0] if len(models) == 1 else None, "compute_type": "GPU" if gpu else "CPU",
            "resources": res, "serving_args": args}


def observe_environment(template: dict) -> dict:
    return {**env_from_template(template), **{k: v for k, v in contract_hashes().items() if k != "recovery_policy_sha256"}}


# ---------------- profile ----------------
def _strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


def without_role(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "role"}


def validate_profile(p: dict, *, stage: str) -> dict:
    """형식·동결값 검사(§4.2·§5.1). stage: "stage1"(ACTIVE 0개만) / "stage2"(ACTIVE 정확히 1개). 위반은 ProfileInvalid."""
    if stage not in ("stage1", "stage2"):
        raise ValueError(f"stage는 stage1/stage2: {stage}")
    if not isinstance(p, dict) or set(p) != {"profile_id", "environment", "detectors", "selection", "recovery_policy_sha256"}:
        raise ProfileInvalid(f"profile 최상위 블록 불일치: {sorted(p) if isinstance(p, dict) else type(p).__name__}")
    dets = p["detectors"]
    if not isinstance(dets, list) or len(dets) != len(DETECTOR_IDS) or not all(isinstance(d, dict) for d in dets) \
            or sorted(d.get("detector_id") or "" for d in dets) != sorted(DETECTOR_IDS):
        raise ProfileInvalid(f"detectors는 후보 {DETECTOR_IDS} 각 1개여야 함")
    probs = []
    abs_paths = [s for s in _strings(p) if _ABS_PATH.match(s)]
    if abs_paths:
        probs.append(f"absolute_path_in_profile: {abs_paths[:3]}")
    for d in dets:
        if d.get("threshold") is None:
            probs.append(f"threshold_none:{d['detector_id']}")
        if d.get("role") not in (ACTIVE, SHADOW):
            probs.append(f"role_unknown:{d['detector_id']}={d.get('role')}")
        mine, frozen = without_role(d), FROZEN_DETECTORS[d["detector_id"]]
        if mine.get("threshold") is None:          # threshold 누락은 형식 오류(위) - artifact 불일치로 세지 않는다
            mine, frozen = ({k: v for k, v in x.items() if k != "threshold"} for x in (mine, frozen))
        if mine != frozen:
            probs.append(f"detector_differs_from_frozen:{d['detector_id']}")
    n_active = sum(d.get("role") == ACTIVE for d in dets)
    if n_active > 1:
        probs.append(f"active_count={n_active}>1")
    elif stage == "stage1" and n_active != 0:
        probs.append(f"stage1_requires_active_0: {n_active}")
    elif stage == "stage2" and n_active != 1:
        probs.append(f"stage2_requires_active_1: {n_active}")
    if (p.get("selection") or {}).get("rule") != PREREG_SHA256:
        probs.append("selection_rule_not_design_freeze")
    if probs:
        cls = ArtifactMismatch if any(x.startswith("detector_differs_from_frozen") for x in probs) else ProfileInvalid
        raise cls(f"PROFILE_INVALID: {probs}")
    return p


def load_profile(path, *, stage: str) -> dict:
    """{"profile", "sha256"(LF 정규화 파일 해시), "active"(id 또는 None)}."""
    raw = Path(path).read_bytes()
    try:
        p = json.loads(raw)
    except ValueError as e:
        raise ProfileInvalid(f"profile JSON 아님: {e}") from e
    validate_profile(p, stage=stage)
    act = [d["detector_id"] for d in p["detectors"] if d["role"] == ACTIVE]
    return {"profile": p, "sha256": text_sha256(path), "active": act[0] if act else None}


def build_selection_profile(template: dict, profile_id: str = "cpu-vllm-qwen05b-v1") -> dict:
    """Stage 1 selection profile(§4.2): 두 후보 모두 SHADOW(ACTIVE 0개), selection 블록은 규칙(design freeze 해시)만."""
    return {"profile_id": profile_id, "environment": observe_environment(template),
            "detectors": [{**FROZEN_DETECTORS[d], "role": SHADOW} for d in DETECTOR_IDS],
            "selection": {"rule": PREREG_SHA256, "validation_evidence_id": None, "selected_at": None, "outcome": None,
                          "gate_results": None, "reason": "selection profile - Stage 1(ACTIVE 0, 송신 없음)"},
            "recovery_policy_sha256": contract_hashes()["recovery_policy_sha256"]}


def check_stale(profile: dict, observed_env: dict, policy_sha256: str) -> list:
    """§4.4 - 다른 항목 목록(빈 목록 = 최신). detector artifact/버전은 validate_profile·verify_artifacts가 따로 막는다."""
    want = profile["environment"]
    out = [f"environment.{k}" for k in sorted(set(want) | set(observed_env)) if want.get(k) != observed_env.get(k)]
    if profile["recovery_policy_sha256"] != policy_sha256:
        out.append("recovery_policy_sha256")
    return out


# ---------------- 외부 artifact(§5.1) ----------------
def verify_artifacts(artifact_root, *, sklearn_version=None) -> dict:
    """c3-A 외부 artifact 검사(§5.1 1-5·7단계; 6단계 feature 키 집합은 check_features). 기본 경로 없음 -
    artifact_root를 명시해야 한다. 실패는 ProfileInvalid. 반환: model·scaler·schema·threshold·hashes(SHA256SUMS 형식)."""
    import pickle
    if artifact_root is None or str(artifact_root).strip() == "":
        raise ProfileInvalid("PROFILE_INVALID: artifact_root 미지정(--artifact-root) - 기본 경로를 추측하지 않음")
    root = Path(artifact_root)
    want = FROZEN_DETECTORS[IF_ID]["artifact"]
    missing = [f for f in C3A_FILES if not (root / f).is_file()]
    if missing:
        raise ArtifactMismatch(f"PROFILE_INVALID: artifact 없음 {missing} (root={root.name})")
    hashes = {f: sha256_bytes(root / f) for f in C3A_FILES}
    bad = [f for f, k in C3A_FILES.items() if hashes[f] != want[k]]
    if bad:
        raise ArtifactMismatch(f"PROFILE_INVALID: artifact 해시 불일치 {bad}")
    fz = json.loads((root / "freeze.json").read_text(encoding="utf-8"))
    if (fz.get("status"), fz.get("series"), fz.get("plan_id")) != ("frozen", C3A_SERIES, C3A_PLAN) \
            or (fz.get("versions") or {}).get("sklearn") != SKLEARN_VERSION:
        raise ArtifactMismatch(f"PROFILE_INVALID: artifact 버전 불일치 {fz.get('series')}/{fz.get('plan_id')}/"
                             f"{(fz.get('versions') or {}).get('sklearn')}")
    if sklearn_version is None:
        import sklearn
        sklearn_version = sklearn.__version__
    if sklearn_version != SKLEARN_VERSION:
        raise ArtifactMismatch(f"PROFILE_INVALID: 실행 환경 sklearn {sklearn_version} != {SKLEARN_VERSION}")
    thr = json.loads((root / "threshold.json").read_text(encoding="utf-8"))
    if thr.get("threshold") != IF_THRESHOLD or thr.get("calibration_failed"):
        raise ArtifactMismatch(f"PROFILE_INVALID: threshold 불일치 {thr.get('threshold')}")
    schema = json.loads((root / "feature-schema.json").read_text(encoding="utf-8"))
    return {"model": pickle.loads((root / "model.pkl").read_bytes()), "scaler": pickle.loads((root / "scaler.pkl").read_bytes()),
            "schema": schema, "threshold": IF_THRESHOLD, "hashes": dict(sorted(hashes.items()))}


def check_features(features: dict, schema: dict) -> None:
    """§5.1 6단계 - 평가 기록 features 키 집합 = 원본 특징 8개(FEATURE_NAMES), schema의 사용 특징은 그 부분집합."""
    from feature_selection import FEATURE_NAMES
    if set(features) != set(FEATURE_NAMES) or not set(schema["kept_feature_names"]) <= set(FEATURE_NAMES):
        raise ProfileInvalid(f"PROFILE_INVALID: feature schema 불일치 {sorted(features)}")


def verify_rule_spec() -> dict:
    """CPU 후보 규칙 명세: 동결 load_spec(코드 상수 대조) + profile 기대 해시(LF 정규화)."""
    import c1_bck_live_detector as bd
    spec = bd.load_spec()
    if bd.THRESHOLDS["C"] != CPU_THRESHOLD:
        raise ArtifactMismatch("PROFILE_INVALID: arm C 임계값이 동결값과 다름")
    if text_sha256(ROOT / RULE_SPEC_REL) != FROZEN_DETECTORS[CPU_ID]["artifact"]["rule_spec_sha256"]:
        raise ArtifactMismatch("PROFILE_INVALID: rule-spec 해시 불일치")
    return spec


# ---------------- 판정 정규화(§4.1) ----------------
def detector_value(det: str, rec: dict, if_score):
    """정규화 입력: CPU = cpu_mean, IF = c3-A decision_function. 자기 입력이 유한하지 않으면 None(→ UNKNOWN)."""
    if rec.get("status") != "scored":
        return None
    v = (rec.get("features") or {}).get("cpu_mean") if det == CPU_ID else if_score
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def streak_args(det: str, v: float):
    """동결 advance_streak(value < threshold = 이상) 입력 - CPU는 judge_all과 같은 부호 변환."""
    return (-v, -CPU_THRESHOLD) if det == CPU_ID else (v, IF_THRESHOLD)


def judge(det: str, rec: dict, if_score, state: dict, now: float) -> dict:
    """한 detector의 tick 판정. state = {"consecutive", "last_signal"}(detector별 독립). UNKNOWN → 연속 0."""
    from score_server import COOLDOWN_SEC, CONSECUTIVE_THRESHOLD, advance_streak
    v = detector_value(det, rec, if_score)
    thr = FROZEN_DETECTORS[det]["threshold"]
    if v is None:
        state["consecutive"] = 0
        return {"value": None, "threshold": thr, "state": UNKNOWN, "is_anomalous": None, "consecutive": 0,
                "condition_met": False, "condition_met_at_utc": None}
    s = advance_streak(*streak_args(det, v), state["consecutive"], state["last_signal"], now,
                       cooldown_sec=COOLDOWN_SEC, consecutive_threshold=CONSECUTIVE_THRESHOLD)
    state["consecutive"], state["last_signal"] = s["consecutive_anomalous"], s["last_signal_at"]
    return {"value": v, "threshold": thr, "state": ANOMALY if s["is_anomalous"] else NORMAL,
            "is_anomalous": s["is_anomalous"], "consecutive": s["consecutive_anomalous"], "condition_met": s["should_signal"],
            "condition_met_at_utc": rec["score_completed_at_utc"] if s["should_signal"] else None}


# ---------------- 정책 식별(§4 2차 방어) ----------------
def signal_identity(det: str, if_hashes: dict) -> dict:
    """c1-signal-v2 identity·guarded context 공통. threshold는 항상 숫자(None 금지 - §4 2)."""
    import c1_policy_link as link
    f = FROZEN_DETECTORS[det]
    if det == CPU_ID:
        h = f["artifact"]["rule_spec_sha256"]
        ident = {"model_hash": h, "artifact_manifest_hash": link.artifact_manifest_hash({Path(RULE_SPEC_REL).name: h})}
    else:
        ident = {"model_hash": if_hashes["model.pkl"], "artifact_manifest_hash": link.artifact_manifest_hash(if_hashes)}
    return {"detector_id": det, "model_version": f["detector_version"], **ident, "threshold": f["threshold"]}


def build_context(run_id: str, rollout: dict, loaded: dict, if_hashes: dict) -> dict:
    """act context는 ACTIVE가 정확히 1개인 유효 profile에서만(§4.2·§4.3). 그 밖은 ProfileInvalid."""
    validate_profile(loaded["profile"], stage="stage2")
    if loaded["active"] is None:
        raise ProfileInvalid("ACTIVE 없음 - act context를 만들지 않음")
    ctx = {"run_id": run_id, **signal_identity(loaded["active"], if_hashes), "rollout": rollout, "actions_allowed": True}
    if ctx["threshold"] is None:
        raise ProfileInvalid("context threshold None")
    return ctx


# ---------------- active profile(§8.3) ----------------
def derive_active_profile(selection_profile: dict, selection_profile_sha256: str, evidence: dict, evidence_sha256: str) -> dict:
    """SELECTED 결과에서만. selection profile을 복사해 선택 detector role만 ACTIVE로, selection 블록만 채운다."""
    validate_profile(selection_profile, stage="stage1")
    out = evidence.get("outcome") or ""
    if not out.startswith("SELECTED:") or out.split(":", 1)[1] not in DETECTOR_IDS:
        raise ProfileInvalid(f"SELECTED가 아님 - active profile 생성 거부: {out}")
    if evidence.get("selection_profile_sha256") != selection_profile_sha256:
        raise ProfileInvalid("선택 증거가 이 selection profile로 만든 것이 아님")
    sel = out.split(":", 1)[1]
    p = copy.deepcopy(selection_profile)
    for d in p["detectors"]:
        d["role"] = ACTIVE if d["detector_id"] == sel else SHADOW
    p["selection"] = {"rule": PREREG_SHA256, "validation_evidence_id": evidence["evidence_id"],
                      "selection_evidence_sha256": evidence_sha256, "source_selection_profile_sha256": selection_profile_sha256,
                      "selected_at": evidence["decided_at_utc"], "outcome": out, "gate_results": evidence["gate_results"],
                      "reason": evidence["reason"]}
    return validate_profile(p, stage="stage2")


def active_profile_diff(selection_profile: dict, active_profile: dict) -> list:
    """role·selection 외 차이 목록(빈 목록 = 기계적 파생과 일치)."""
    a, s = copy.deepcopy(active_profile), copy.deepcopy(selection_profile)
    for x in (a, s):
        x.pop("selection", None)
        x["detectors"] = sorted((without_role(d) for d in x.get("detectors") or []), key=lambda d: d.get("detector_id") or "")
    return sorted(k for k in set(a) | set(s) if a.get(k) != s.get(k))


# ---------------- implementation freeze(§18-B) ----------------
def verify_implementation_freeze(manifest_rel: str = IMPLEMENTATION_FREEZE, root: Path = ROOT,
                                 required=FREEZE_REQUIRED) -> dict:
    """실행 전 fail-closed: manifest 형식·design freeze 참조·모든 파일 LF 정규화 해시. 클러스터 접근 전에 부른다."""
    root = Path(root)
    try:
        raw = (root / manifest_rel).read_bytes()
        m = json.loads(raw)
        files = m["files"]
        if m.get("status") != "implementation_frozen" or not isinstance(files, dict) or not files:
            raise ValueError(f"status={m.get('status')}")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"implementation freeze fail-closed: manifest 읽기·형식 오류: {type(e).__name__}: {e}") from e
    df = m.get("design_freeze") or {}
    probs = []
    if df.get("prereg_sha256") != PREREG_SHA256 or df.get("manifest_path") != DESIGN_FREEZE \
            or not (root / DESIGN_FREEZE).is_file() or df.get("manifest_sha256") != text_sha256(root / DESIGN_FREEZE):
        probs.append("design_freeze_reference_mismatch")
    if not (root / PREREG_DOC).is_file() or text_sha256(root / PREREG_DOC) != PREREG_SHA256:
        probs.append("prereg_changed")
    if (m.get("tests") or {}).get("phase2_failed") != 0 or (m.get("tests") or {}).get("new_regressions") != 0:
        probs.append("tests_not_100_percent")
    bad = sorted(rel for rel, want in files.items() if not (root / rel).is_file() or text_sha256(root / rel) != want)
    if manifest_rel in files:
        probs.append("self_reference")
    missing = sorted(set(required) - set(files))
    if missing:
        probs.append(f"required_files_not_frozen: {missing}")
    if bad or probs:
        raise SystemExit(f"implementation freeze fail-closed: 해시 불일치 {bad}, {probs}")
    return {"manifest": manifest_rel, "manifest_sha256": hashlib.sha256(raw).hexdigest(), "verified_files": len(files),
            "selection_profile_sha256": m.get("selection_profile_sha256"), "status": "verified"}
