#!/usr/bin/env python3
"""Phase 2 네트워크 지연 환경 추가 검증 공용 부품 (2026-10-17) - DEVELOPMENT / NOT OFFICIAL / 아직 동결 전.

기존 experiments/phase2_detector_selection(Phase 2 동결, 수정 금지)은 import만 한다(복사하지 않는다). 바뀌는 것만 여기서 새로 정의한다:
  - 후보 3개: CPU 임계값(동결) · net_probe(새, 개발 자료로 보정) · c3-A IF(동결). 동률 순서 CPU > net_probe > IF(사용자 결정 2026-10-17).
  - 환경 프로필: network_tolerant(readiness/liveness timeout 11 s) + 네트워크 지연 압박 정의.
  - 판정: CPU·IF는 동결 pc.judge 그대로, net_probe만 같은 상태기계(score_server.advance_streak)로 새로 판정.
공식 사전등록 문서(PREREG_DOC)는 동결 전이라 PREREG_SHA256=None이다. None이면 프로필 검증이 거부한다(fail-closed) -
동결 때 이 값을 채운다. 테스트는 prereg_sha256 인자로 가짜 값을 넘긴다. 클러스터 접근 없음."""
import sys
sys.dont_write_bytecode = True
import copy  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
ROOT = EXP.parent
P2DIR = EXP / "phase2_detector_selection"
for _p in (P2DIR, EXP):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import p2_common as pc  # noqa: E402  (기존 동결 모듈 - 읽기 전용 사용)

ProfileInvalid, ArtifactMismatch = pc.ProfileInvalid, pc.ArtifactMismatch
CPU_ID, IF_ID = pc.CPU_ID, pc.IF_ID
NET_ID = "net-probe-readiness"
DETECTOR_IDS = (CPU_ID, NET_ID, IF_ID)
SIMPLICITY_ORDER = (CPU_ID, NET_ID, IF_ID)   # 사용자 결정(2026-10-17): 운영 의존성이 적은 순서. 앞 단계가 모두 동등일 때만 쓴다
ACTIVE, SHADOW, NORMAL, ANOMALY, UNKNOWN = pc.ACTIVE, pc.SHADOW, pc.NORMAL, pc.ANOMALY, pc.UNKNOWN

# 기존 Phase 2의 고정 값을 그대로 쓴다(§17 - 결과를 본 뒤 바꾸지 않음). 값을 여기서 다시 쓰지 않는다.
N_NORMAL, N_PRESSURE, N_STAGE2 = pc.N_NORMAL, pc.N_PRESSURE, pc.N_STAGE2
# rev3(2026-10-18): 정상 시도 상한만 6 -> 12로 늘린다(유효 정상 4개가 모일 때까지 추가 수집). 유효성 기준·임계값·Gate·선택 규칙·세션 목표 수·
# 압박/Stage 2 상한은 그대로다. 근거: prereg rev3 §9.2.
MAX_ATTEMPTS = {**pc.MAX_ATTEMPTS, "normal": 12}
GATE2_LIMIT_SEC, GATE2_MIN_PASS = pc.GATE2_LIMIT_SEC, pc.GATE2_MIN_PASS
GATE3_MIN_DIFF, TIMING_MIN_DIFF_SEC, DECISION_SEC = pc.GATE3_MIN_DIFF, pc.TIMING_MIN_DIFF_SEC, pc.DECISION_SEC

PREREG_DOC = "docs/design/PHASE2-NETWORK-ENV-PREREG-v1-rev5.md"
PREREG_SHA256 = None            # rev5 DESIGN FREEZE 때 채운다(LF 정규화 blob 해시). rev1(dd3a74f)·rev2(42a3b46)·rev3(2209451)·rev4(34e7224, 31797ba8…)는 교체됐다.
DEV_DRAFT = "docs/design/NETWORK-DETECTOR-PREREG-DRAFT-20261017.md"
NET_CALIBRATION_REL = "experiments/results/network_detector_dev_20261017/probe-metric/result.json"

# ---------------- net_probe 정의(개정 2 개발 자료로 보정, 20세션) ----------------
NET_EXPR = ('(sum by (namespace,pod)(rate(prober_probe_duration_seconds_sum{namespace="vllm-serving",container="vllm",probe_type="Readiness"}[60s]))'
            '/sum by (namespace,pod)(rate(prober_probe_duration_seconds_count{namespace="vllm-serving",container="vllm",probe_type="Readiness"}[60s])))'
            ' and on(namespace,pod) scrape_duration_seconds{namespace="vllm-serving",job="vllm-active"}')
NET_THRESHOLD = 0.006401369666666786         # 표시값 0.0064 s. value > T(엄격), 보정 함수 calibrate_threshold(score=-value)의 결과
NET_CALIBRATION_SHA256 = "c6ece5885bb97000f5a4df87ef135ad9a21e358e99b621e90f4b875e48c7af96"   # NET_CALIBRATION_REL(LF 정규화)
NET_DEFINITION = {"metric": "kubelet readiness probe 소요 시간(60초 rate 비율), active Pod만", "promql": NET_EXPR, "window_sec": 60,
                  "eval_interval_sec": 15, "consecutive": 3, "cooldown_sec": 60, "anomalous_if": "value > threshold",
                  "calibration": "replay.calibrate_threshold(score=-value), 정상 세션 20개(c1 10 + c3 normal_valid 10), 압박·SLO 미사용"}
NET_DEFINITION_SHA256 = pc.obj_sha256(NET_DEFINITION)
FROZEN_NET = {"detector_id": NET_ID, "detector_version": f"net-probe-gt-{NET_THRESHOLD:.7f}", "threshold": NET_THRESHOLD,
              "artifact": {"artifact_id": "network-detector-dev-20261017/net-probe-readiness",
                           "definition_sha256": NET_DEFINITION_SHA256, "calibration_result_sha256": NET_CALIBRATION_SHA256}}
FROZEN_DETECTORS = {CPU_ID: pc.FROZEN_DETECTORS[CPU_ID], NET_ID: FROZEN_NET, IF_ID: pc.FROZEN_DETECTORS[IF_ID]}

# ---------------- 환경: network_tolerant + 네트워크 지연 압박 ----------------
PROBE_TIMEOUT_SEC = 11            # readiness/liveness timeoutSeconds(파일럿 calibration으로 확정, 기본 1초에서는 재시작 연쇄 장애)
NETWORK_STAGES = ({"name": "stage-1-500ms", "latency": "500ms", "jitter": "50ms", "duration_sec": 90},
                  {"name": "stage-2-1000ms", "latency": "1000ms", "jitter": "100ms", "duration_sec": 90},
                  {"name": "stage-3-2000ms", "latency": "2000ms", "jitter": "200ms", "duration_sec": 90},
                  {"name": "stage-4-4000ms", "latency": "4000ms", "jitter": "400ms", "duration_sec": 90})
PRESSURE = {"type": "network_delay", "action": "delay", "target": "active Pod 1개(이름 지정, preview·새 Pod 제외)", "mode": "one",
            "stages": [dict(s) for s in NETWORK_STAGES]}
PROBE_PROFILE = {"name": "network_tolerant", "readiness_timeout_sec": PROBE_TIMEOUT_SEC, "liveness_timeout_sec": PROBE_TIMEOUT_SEC}
_ABS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


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


def verify_net_calibration(root: Path = ROOT) -> dict:
    """보정 근거 파일이 동결 해시와 같고 그 안의 임계값이 NET_THRESHOLD와 같은지(§5.1 검사의 net_probe판)."""
    p = Path(root) / NET_CALIBRATION_REL
    if not p.is_file():
        raise ArtifactMismatch(f"PROFILE_INVALID: net_probe 보정 근거 없음 {NET_CALIBRATION_REL}")
    if pc.text_sha256(p) != NET_CALIBRATION_SHA256:
        raise ArtifactMismatch("PROFILE_INVALID: net_probe 보정 근거 해시 불일치")
    d = json.loads(p.read_text(encoding="utf-8"))
    if d.get("threshold_seconds") != NET_THRESHOLD or d.get("calibration_failed") or d.get("expr") != NET_EXPR:
        raise ArtifactMismatch("PROFILE_INVALID: net_probe 임계값·질의가 보정 근거와 다름")
    return {"calibration_sha256": NET_CALIBRATION_SHA256, "definition_sha256": NET_DEFINITION_SHA256}


# ---------------- profile ----------------
def validate_profile(p: dict, *, stage: str, prereg_sha256=None) -> dict:
    """형식·동결값 검사. stage1 = ACTIVE 0개, stage2 = 정확히 1개. prereg_sha256이 None이면 모듈 상수를 쓰고, 그것도 None이면 거부."""
    if stage not in ("stage1", "stage2"):
        raise ValueError(f"stage는 stage1/stage2: {stage}")
    want_rule = prereg_sha256 if prereg_sha256 is not None else PREREG_SHA256
    if want_rule is None:
        raise ProfileInvalid("PROFILE_INVALID: 사전등록 문서가 동결되지 않음(PREREG_SHA256=None) - 공식 실행 불가")
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
        if mine.get("threshold") is None:
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
    env = p["environment"] if isinstance(p["environment"], dict) else {}
    if env.get("probe_profile") != PROBE_PROFILE:
        probs.append("environment.probe_profile != network_tolerant(11 s)")
    if env.get("pressure") != PRESSURE:
        probs.append("environment.pressure != 네트워크 지연 사전등록 정의")
    if (p.get("selection") or {}).get("rule") != want_rule:
        probs.append("selection_rule_not_design_freeze")
    if probs:
        cls = ArtifactMismatch if any(x.startswith("detector_differs_from_frozen") for x in probs) else ProfileInvalid
        raise cls(f"PROFILE_INVALID: {probs}")
    return p


def load_profile(path, *, stage: str, prereg_sha256=None) -> dict:
    raw = Path(path).read_bytes()
    try:
        p = json.loads(raw)
    except ValueError as e:
        raise ProfileInvalid(f"profile JSON 아님: {e}") from e
    validate_profile(p, stage=stage, prereg_sha256=prereg_sha256)
    act = [d["detector_id"] for d in p["detectors"] if d["role"] == ACTIVE]
    return {"profile": p, "sha256": pc.text_sha256(path), "active": act[0] if act else None}


def build_selection_profile(environment: dict, recovery_policy_sha256: str, prereg_sha256: str,
                            profile_id: str = "network-tolerant-vllm-qwen05b-v1") -> dict:
    """Stage 1 selection profile: 세 후보 모두 SHADOW(ACTIVE 0개). environment는 관측값(serving 계약 + 계약 해시)에 probe_profile·pressure를 더한다."""
    env = {**environment, "probe_profile": dict(PROBE_PROFILE), "pressure": copy.deepcopy(PRESSURE)}
    p = {"profile_id": profile_id, "environment": env,
         "detectors": [{**copy.deepcopy(FROZEN_DETECTORS[d]), "role": SHADOW} for d in DETECTOR_IDS],
         "selection": {"rule": prereg_sha256, "validation_evidence_id": None, "selected_at": None, "outcome": None,
                       "gate_results": None, "reason": "selection profile - Stage 1(ACTIVE 0, 송신 없음)"},
         "recovery_policy_sha256": recovery_policy_sha256}
    return validate_profile(p, stage="stage1", prereg_sha256=prereg_sha256)


def derive_active_profile(selection_profile: dict, selection_profile_sha256: str, evidence: dict, evidence_sha256: str,
                          prereg_sha256=None) -> dict:
    """SELECTED 결과에서만. selection profile을 복사해 선택 detector의 role만 ACTIVE로, selection 블록만 채운다."""
    validate_profile(selection_profile, stage="stage1", prereg_sha256=prereg_sha256)
    out = evidence.get("outcome") or ""
    if not out.startswith("SELECTED:") or out.split(":", 1)[1] not in DETECTOR_IDS:
        raise ProfileInvalid(f"SELECTED가 아님 - active profile 생성 거부: {out}")
    if evidence.get("selection_profile_sha256") != selection_profile_sha256:
        raise ProfileInvalid("선택 증거가 이 selection profile로 만든 것이 아님")
    sel = out.split(":", 1)[1]
    p = copy.deepcopy(selection_profile)
    for d in p["detectors"]:
        d["role"] = ACTIVE if d["detector_id"] == sel else SHADOW
    p["selection"] = {"rule": p["selection"]["rule"], "validation_evidence_id": evidence["evidence_id"],
                      "selection_evidence_sha256": evidence_sha256, "source_selection_profile_sha256": selection_profile_sha256,
                      "selected_at": evidence["decided_at_utc"], "outcome": out, "gate_results": evidence["gate_results"],
                      "reason": evidence["reason"]}
    return validate_profile(p, stage="stage2", prereg_sha256=prereg_sha256)


active_profile_diff = pc.active_profile_diff        # role·selection 외 차이(일반 함수 - 후보 수와 무관)
check_stale = pc.check_stale


# ---------------- Rollout probe 프로필 검사(읽기 전용 스냅샷) ----------------
def probe_profile_from_template(template: dict) -> dict:
    """Rollout spec.template(읽기 전용 스냅샷)에서 vllm 컨테이너의 readiness/liveness timeoutSeconds를 읽는다."""
    cs = [c for c in ((template or {}).get("spec") or {}).get("containers") or [] if c.get("name") == "vllm"]
    if len(cs) != 1:
        raise ProfileInvalid(f"vllm 컨테이너가 정확히 1개가 아님: {len(cs)}")
    c = cs[0]
    return {"readiness_timeout_sec": (c.get("readinessProbe") or {}).get("timeoutSeconds"),
            "liveness_timeout_sec": (c.get("livenessProbe") or {}).get("timeoutSeconds")}


def probe_profile_problems(template: dict, *, expect_tolerant: bool, canonical: dict = None) -> list:
    """expect_tolerant=True: 실험 중 11 s여야 함. False: 실험 뒤 복원 검증 - canonical(실험 전 기록한 값)과 같아야 함."""
    got = probe_profile_from_template(template)
    if expect_tolerant:
        want = {"readiness_timeout_sec": PROBE_TIMEOUT_SEC, "liveness_timeout_sec": PROBE_TIMEOUT_SEC}
        return [] if got == want else [f"probe_profile {got} != network_tolerant {want}"]
    if canonical is None:
        return ["canonical probe profile 기록 없음 - 복원 검증 불가"]
    return [] if got == canonical else [f"probe_profile 복원 안 됨: {got} != canonical {canonical}"]


# ---------------- 환경 관측(읽기 전용 스냅샷의 template → profile environment와 같은 모양) ----------------
def contract_hashes() -> dict:
    """pc.contract_hashes와 같은 방식. 달라지는 것: workload 계약(CPU 스트레스 단계 대신 네트워크 지연 단계)과 telemetry 계약(net_probe 질의 추가).
    SLO 계약·정책 소스 해시는 Phase 2와 같은 파일에서 계산하므로 같은 값이다(복구 경로를 바꾸지 않았다는 증거)."""
    import features
    import input_integrity as ii
    import run_if_threshold_candidate_smoke as smoke
    from load_ramp_adapter import IMAGE_FORCE_CLOSE, IMAGE_FORCE_CLOSE_ID
    from score_server import EVAL_INTERVAL_SEC, FRESHNESS_PROBE_QUERIES, WINDOW_SEC
    workload = {"probe_config_sha256": pc.text_sha256(smoke.DEFAULT_PROBE_CONFIG), "probe_image": IMAGE_FORCE_CLOSE,
                "probe_image_id": IMAGE_FORCE_CLOSE_ID, "network_stages": [dict(s) for s in NETWORK_STAGES]}
    telemetry = {"cpu": ii.cpu_rate_query(), "metrics": {k: features.METRICS[k] for k in ("memory", "queue", "cache")},
                 "freshness": FRESHNESS_PROBE_QUERIES, "window_sec": WINDOW_SEC, "eval_interval_sec": EVAL_INTERVAL_SEC,
                 "net_probe": NET_EXPR}
    return {"workload_contract": pc.obj_sha256(workload),
            "slo_contract": pc.obj_sha256({p: pc.text_sha256(ROOT / p) for p in pc.SLO_SOURCES}),
            "telemetry_contract": pc.obj_sha256(telemetry),
            "recovery_policy_sha256": pc.obj_sha256({p: pc.text_sha256(ROOT / p) for p in pc.POLICY_SOURCES})}


def observed_probe_profile(template: dict) -> dict:
    """관측한 probe 설정을 profile의 probe_profile과 비교할 수 있는 모양으로. 11 s/11 s면 network_tolerant, 아니면 실제 값을 그대로(→ STALE)."""
    got = probe_profile_from_template(template)
    if got == {"readiness_timeout_sec": PROBE_TIMEOUT_SEC, "liveness_timeout_sec": PROBE_TIMEOUT_SEC}:
        return dict(PROBE_PROFILE)
    return {"name": "not_network_tolerant", **got}


def observe_environment(template: dict) -> dict:
    """profile.environment와 같은 키 집합(check_stale 비교용). serving 계약 + 계약 해시 + probe_profile + pressure."""
    ch = {k: v for k, v in contract_hashes().items() if k != "recovery_policy_sha256"}
    return {**pc.env_from_template(template), **ch, "probe_profile": observed_probe_profile(template),
            "pressure": copy.deepcopy(PRESSURE)}


# ---------------- 판정 ----------------
def net_value(rec: dict):
    """net_probe 값 = 평가 기록의 net_probe.value. scored가 아니거나 유한하지 않으면 None(→ UNKNOWN, 연속 0)."""
    if rec.get("status") != "scored":
        return None
    v = (rec.get("net_probe") or {}).get("value")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def judge(det: str, rec: dict, if_score, state: dict, now: float) -> dict:
    """한 detector의 tick 판정. CPU·IF는 동결 pc.judge 그대로, net_probe만 같은 상태기계를 새로 쓴다."""
    if det != NET_ID:
        return pc.judge(det, rec, if_score, state, now)
    from score_server import COOLDOWN_SEC, CONSECUTIVE_THRESHOLD, advance_streak
    v = net_value(rec)
    if v is None:
        state["consecutive"] = 0
        return {"value": None, "threshold": NET_THRESHOLD, "state": UNKNOWN, "is_anomalous": None, "consecutive": 0,
                "condition_met": False, "condition_met_at_utc": None}
    s = advance_streak(-v, -NET_THRESHOLD, state["consecutive"], state["last_signal"], now,
                       cooldown_sec=COOLDOWN_SEC, consecutive_threshold=CONSECUTIVE_THRESHOLD)
    state["consecutive"], state["last_signal"] = s["consecutive_anomalous"], s["last_signal_at"]
    return {"value": v, "threshold": NET_THRESHOLD, "state": ANOMALY if s["is_anomalous"] else NORMAL,
            "is_anomalous": s["is_anomalous"], "consecutive": s["consecutive_anomalous"], "condition_met": s["should_signal"],
            "condition_met_at_utc": rec["score_completed_at_utc"] if s["should_signal"] else None}


def build_context(run_id: str, rollout: dict, loaded: dict, if_hashes: dict, prereg_sha256=None) -> dict:
    """act context는 ACTIVE가 정확히 1개인 유효 profile에서만(§4.2·§4.3). 그 밖은 ProfileInvalid."""
    validate_profile(loaded["profile"], stage="stage2", prereg_sha256=prereg_sha256)
    if loaded["active"] is None:
        raise ProfileInvalid("ACTIVE 없음 - act context를 만들지 않음")
    ctx = {"run_id": run_id, **signal_identity(loaded["active"], if_hashes), "rollout": rollout, "actions_allowed": True}
    if ctx["threshold"] is None:
        raise ProfileInvalid("context threshold None")
    return ctx


def signal_identity(det: str, if_hashes: dict) -> dict:
    """c1-signal-v2 identity·guarded context 공통. net_probe는 정의 해시를 model_hash로 쓴다. threshold는 항상 숫자."""
    if det != NET_ID:
        return pc.signal_identity(det, if_hashes)
    import c1_policy_link as link
    h = FROZEN_NET["artifact"]["definition_sha256"]
    return {"detector_id": NET_ID, "model_version": FROZEN_NET["detector_version"], "model_hash": h,
            "artifact_manifest_hash": link.artifact_manifest_hash({"net-probe-definition.json": h}),
            "threshold": FROZEN_NET["threshold"]}
