#!/usr/bin/env python3
"""네트워크 지연 수집의 정리·복원 확인(2026-10-17, DEVELOPMENT - 동결 전, 클러스터 접근 없음).

순서 1순위로 만든 부분이다: 실험이 끝났을 때 시연 환경이 원래대로 돌아왔는지를 읽기 전용 상태 기록으로 확인한다.
두 층이다:
  - 세션 단위(session_end_problems): 그 세션이 만든 NetworkChaos·표식이 남지 않았고, 수집 중인 환경(network_tolerant)·정책 모드가 유지됐는가.
  - 수집 단위(restore_problems): 전체 수집(정상 4·압박 5·복구)이 끝난 뒤 **시작 전 기록한 원래 상태**와 같은가
    (Rollout 템플릿·probe 설정·유예 시간·정책 모드·정책 context·NetworkChaos·표식). 시연 환경 오염 방지용 최종 관문이다.
상태 기록(capture_state)은 수집 시작 전 읽기 전용 스냅샷에서 만들고 파일로 보존한다(덮어쓰기 금지 - pc.write_json_new)."""
import sys
sys.dont_write_bytecode = True
import copy  # noqa: E402

import n2_common as nc  # noqa: E402

pc = nc.pc
STATE_KEYS = ("probe_profile", "rollout_template_sha256", "scale_down_delay", "policy", "network_chaos_names", "running_markers")
POLICY_KEYS = ("mode", "context", "blocked", "unresolved")


def capture_state(template: dict, scale_down_delay, policy: dict, network_chaos_names, running_markers) -> dict:
    """읽기 전용 스냅샷에서 상태 기록을 만든다. policy = 정책 /admin/state 요약(mode, context, blocked, unresolved).
    rollout_template_sha256 = Rollout spec.template 전체의 결정적 해시(probe 설정뿐 아니라 어떤 필드가 바뀌어도 복원 확인에서 걸린다)."""
    if not isinstance(template, dict) or not template:
        raise ValueError("template 없음 - 상태 기록 불가")
    return {"probe_profile": nc.probe_profile_from_template(template), "rollout_template_sha256": pc.obj_sha256(template),
            "scale_down_delay": scale_down_delay,
            "policy": {k: copy.deepcopy((policy or {}).get(k)) for k in POLICY_KEYS},
            "network_chaos_names": sorted(network_chaos_names or []), "running_markers": sorted(running_markers or [])}


def _missing(state: dict) -> list:
    return [f"state_missing:{k}" for k in STATE_KEYS if not isinstance(state, dict) or k not in state]


def _policy_clean(policy: dict, mode: str) -> list:
    p = policy or {}
    out = []
    if p.get("mode") != mode:
        out.append(f"policy_mode={p.get('mode')} != {mode}")
    if p.get("context"):
        out.append("policy_context_set")
    if p.get("blocked"):
        out.append("policy_blocked")
    if p.get("unresolved"):
        out.append(f"policy_unresolved:{p.get('unresolved')}")
    return out


def session_end_problems(end: dict, planned_cr_names, *, expect_tolerant: bool = True, policy_mode: str = "record_only",
                         run_id_marker: str = None) -> list:
    """세션 하나가 끝난 직후 상태(end)의 문제. 빈 목록 = 깨끗이 정리됨.
    - 이 세션이 만든 NetworkChaos(planned_cr_names)와 그 밖의 NetworkChaos 모두 남지 않아야 한다.
    - 수집 중이므로 probe 설정은 network_tolerant를 유지해야 한다(expect_tolerant=True, 수집 단위 복원은 restore_problems 몫).
    - 정책은 지정 모드(Stage 1 record_only)이고 context·blocked·unresolved가 없어야 한다.
    - 이 세션의 실행 중 표식(running-<run_id>)이 남지 않아야 한다."""
    miss = _missing(end)
    if miss:
        return miss
    probs = []
    left = sorted(set(end["network_chaos_names"]))
    if left:
        probs.append(f"network_chaos_residual:{left}")
    gone = sorted(set(planned_cr_names or []) & set(left))
    if gone:
        probs.append(f"planned_cr_not_deleted:{gone}")
    got = end["probe_profile"]
    want = {"readiness_timeout_sec": nc.PROBE_TIMEOUT_SEC, "liveness_timeout_sec": nc.PROBE_TIMEOUT_SEC}
    if expect_tolerant and got != want:
        probs.append(f"probe_profile_changed_during_collection:{got}")
    probs += _policy_clean(end["policy"], policy_mode)
    markers = [m for m in end["running_markers"] if run_id_marker is None or run_id_marker in m]
    if markers:
        probs.append(f"running_marker_left:{markers}")
    return probs


def restore_problems(start: dict, end: dict) -> list:
    """전체 수집이 끝난 뒤의 상태(end)가 수집 시작 전 기록(start)과 같은가. 빈 목록 = 시연 환경 원복 확인.
    start 자체가 network_tolerant이면(이미 바뀐 상태를 시작으로 기록한 것) 원복 기준으로 쓸 수 없으므로 거부한다."""
    miss = _missing(start) + _missing(end)
    if miss:
        return miss
    probs = []
    tol = {"readiness_timeout_sec": nc.PROBE_TIMEOUT_SEC, "liveness_timeout_sec": nc.PROBE_TIMEOUT_SEC}
    if start["probe_profile"] == tol:
        probs.append("start_state_already_tolerant: 시작 상태가 이미 network_tolerant라 원복 기준이 될 수 없음")
    if end["probe_profile"] != start["probe_profile"]:
        probs.append(f"probe_profile_not_restored:{end['probe_profile']} != {start['probe_profile']}")
    if end["rollout_template_sha256"] != start["rollout_template_sha256"]:
        probs.append("rollout_template_not_restored")
    if end["scale_down_delay"] != start["scale_down_delay"]:
        probs.append(f"scale_down_delay:{end['scale_down_delay']} != {start['scale_down_delay']}")
    if end["policy"].get("mode") != start["policy"].get("mode"):
        probs.append(f"policy_mode:{end['policy'].get('mode')} != {start['policy'].get('mode')}")
    probs += [p for p in _policy_clean(end["policy"], start["policy"].get("mode")) if not p.startswith("policy_mode")]
    left = sorted(set(end["network_chaos_names"]) - set(start["network_chaos_names"]))
    if left:
        probs.append(f"network_chaos_residual:{left}")
    extra_markers = sorted(set(end["running_markers"]) - set(start["running_markers"]))
    if extra_markers:
        probs.append(f"running_marker_left:{extra_markers}")
    return probs


def start_state_problems(start: dict) -> list:
    """수집을 시작해도 되는 깨끗한 시작 상태인가(전환 전 점검): 기본 probe 설정, 정책 record_only·정리됨, NetworkChaos·표식 없음."""
    miss = _missing(start)
    if miss:
        return miss
    probs = []
    tol = {"readiness_timeout_sec": nc.PROBE_TIMEOUT_SEC, "liveness_timeout_sec": nc.PROBE_TIMEOUT_SEC}
    if start["probe_profile"] == tol:
        probs.append("already_tolerant")
    if None in start["probe_profile"].values():
        probs.append("probe_timeout_unreadable")
    probs += _policy_clean(start["policy"], "record_only")
    if start["network_chaos_names"]:
        probs.append(f"network_chaos_present:{start['network_chaos_names']}")
    if start["running_markers"]:
        probs.append(f"running_marker_present:{start['running_markers']}")
    return probs
