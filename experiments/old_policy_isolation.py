#!/usr/bin/env python3
"""기존 recovery-policy 승격 경로 격리(guarded 리허설·pilot용, 2026-10-06 준비 - 아직 실행하지 않음).

왜: 기존 recovery-policy(recovery-policy/main.py process_signal)는 preview가 일시정지 상태이면 /signal 또는
Alertmanager 웹훅(/webhooks/alertmanager) 신호로 실험 context와 무관하게 승격할 수 있다. guarded 앱이 record_only여도
리허설 동안 preview가 준비돼 있으면 전체가 무승격이라고 말할 수 없다.

최소 방안: 기존 Deployment `recovery-policy`를 replicas 0으로 내린다(원래 값을 먼저 기록). 그러면 두 경로의 수신자가
없어져 승격 주체가 사라진다(Alertmanager 웹훅은 실패로만 남음). 확인 조건(assess):
  - Deployment spec.replicas == 0, app=recovery-policy Pod 0개(삭제 중인 Pod도 남아 있으면 격리 아님 - 종료 유예 동안
    요청을 처리할 수 있다), Service endpoints 주소 0개
  - GitOps 자동 되돌림 위험이 **확인된 없음**일 때만 통과:
      * Argo CD 조회 실패(설치 여부 불명)는 미확인 → 거부
      * Flux 표시가 있으면 이 도구는 Flux 자동 동기화 상태를 확인하지 않으므로 미확인 → 거부
      * 그 밖의 관리 표시(argocd·app.kubernetes.io/instance)가 있으면 Argo CD Application이 이 Deployment를 관리하고
        자동 동기화가 꺼져 있음을 확인했을 때만 통과, 아니면 미확인 → 거부
      * 이 Deployment를 자동 동기화(automated)로 관리하는 Argo CD Application이 있으면 거부
  - 조회 실패는 격리 미확인(fail-closed)
시간 제한: 모든 Kubernetes 읽기·변경 호출에 `_request_timeout`(api_timeout_sec)을 준다. 격리·복원 대기의 전체 기한은
단조 시계로 재며, 조회가 **끝난 직후** 다시 읽어 기한 이하(≤)일 때만 그 성공을 인정한다. 기한을 넘겨(>) 도착한 성공은
late_observation으로 보존하고 실패(기한 내 미확인)로 돌려준다.
복원(restore): 기록한 Deployment UID가 같고, 현재 replicas가 우리가 둔 0 그대로이고(다른 주체 변경 없음), Rollout에
일시정지 preview가 남아 있지 않을 때만 원래 replicas로 되돌리고 Ready·endpoints를 확인한다. 하나라도 어긋나면 덮어쓰지
않고 실패로 보고한다(사람 확인).
남는 위험(기록): 사람이 수동으로 다시 올리는 경우는 막지 못한다 - 실행기가 안전 감시 주기(5초)마다 다시 확인해 열리면
중단하지만, 그 사이(≤5초)의 경고는 막지 못한다.

사용(각각 별도 승인 - 라이브 상태 변경):
  python old_policy_isolation.py status
  python old_policy_isolation.py isolate --record <파일>
  python old_policy_isolation.py restore --record <파일>
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

NAMESPACE, DEPLOYMENT, SERVICE, POD_SELECTOR = "vllm-serving", "recovery-policy", "recovery-policy", "app=recovery-policy"
ROLLOUT = ("argoproj.io", "v1alpha1", "rollouts", "vllm-serving")
ARGO_MARKERS = ("argocd.argoproj.io", "app.kubernetes.io/instance")
FLUX_MARKERS = ("fluxcd.io",)  # kustomize.toolkit.fluxcd.io/*, helm.toolkit.fluxcd.io/* 등 - 이 도구는 Flux 상태를 확인하지 않는다
API_TIMEOUT_SEC = 5.0


def _g(o, *path, default=None):
    for k in path:
        if o is None:
            return default
        o = o.get(k) if isinstance(o, dict) else getattr(o, k, None)
    return default if o is None else o


def _now():
    return datetime.now(timezone.utc).isoformat()


def _argocd_apps(custom, api_timeout) -> dict:
    """이 Deployment를 관리하는 Argo CD Application. CRD가 없으면(404) installed=False, 다른 실패는 installed=None(불명)."""
    try:
        apps = custom.list_cluster_custom_object("argoproj.io", "v1alpha1", "applications", _request_timeout=api_timeout)
    except Exception as e:  # noqa: BLE001
        if getattr(e, "status", None) == 404:
            return {"installed": False, "managing": []}
        return {"installed": None, "error": f"{type(e).__name__}: {e}", "managing": []}
    managing = []
    for a in _g(apps, "items", default=[]) or []:
        for r in _g(a, "status", "resources", default=[]) or []:
            if (_g(r, "kind"), _g(r, "name"), _g(r, "namespace")) == ("Deployment", DEPLOYMENT, NAMESPACE):
                auto = _g(a, "spec", "syncPolicy", "automated")
                managing.append({"app": _g(a, "metadata", "name"), "automated": auto is not None,
                                 "self_heal": bool(_g(auto, "selfHeal")) if auto is not None else False})
    return {"installed": True, "managing": managing}


def read_state(*, apps, core, custom, api_timeout: float = API_TIMEOUT_SEC) -> dict:
    """읽기 전용. 모든 호출에 _request_timeout. 어떤 조회든 실패하면 errors에 남긴다(assess가 미확인으로 처리)."""
    st = {"read_at_utc": _now(), "errors": [], "api_timeout_sec": api_timeout}
    try:
        d = apps.read_namespaced_deployment(DEPLOYMENT, NAMESPACE, _request_timeout=api_timeout)
        meta = {**(_g(d, "metadata", "labels", default={}) or {}), **(_g(d, "metadata", "annotations", default={}) or {})}
        st["deployment"] = {"uid": _g(d, "metadata", "uid"), "replicas": _g(d, "spec", "replicas"),
                            "ready_replicas": _g(d, "status", "ready_replicas", default=0),
                            "gitops_markers": sorted(k for k in meta if any(m in k for m in ARGO_MARKERS + FLUX_MARKERS))}
    except Exception as e:  # noqa: BLE001
        st["errors"].append(f"deployment: {type(e).__name__}: {e}")
    try:
        items = _g(core.list_namespaced_pod(NAMESPACE, label_selector=POD_SELECTOR, _request_timeout=api_timeout),
                   "items", default=[]) or []
        st["pods_present"] = len(items)  # 삭제 중인 Pod도 센다(종료 유예 동안 요청 처리 가능)
        st["pods_terminating"] = sum(1 for p in items if _g(p, "metadata", "deletion_timestamp") is not None)
    except Exception as e:  # noqa: BLE001
        st["errors"].append(f"pods: {type(e).__name__}: {e}")
    try:
        ep = core.read_namespaced_endpoints(SERVICE, NAMESPACE, _request_timeout=api_timeout)
        subsets = _g(ep, "subsets", default=[]) or []
        st["endpoints_ready"] = sum(len(_g(s, "addresses", default=[]) or []) for s in subsets)
        st["endpoints_not_ready"] = sum(len(_g(s, "not_ready_addresses", default=[]) or []) for s in subsets)
    except Exception as e:  # noqa: BLE001
        st["errors"].append(f"endpoints: {type(e).__name__}: {e}")
    st["argocd"] = _argocd_apps(custom, api_timeout)
    return st


def gitops_problems(st: dict) -> list:
    """자동 되돌림 위험이 '확인된 없음'이 아니면 사유를 돌려준다(확인 불가 = 거부)."""
    a, out = st.get("argocd") or {}, []
    markers = (st.get("deployment") or {}).get("gitops_markers") or []
    managing = a.get("managing") or []
    for m in managing:
        if m["automated"]:
            out.append(f"gitops_auto_sync:{m['app']}(selfHeal={m['self_heal']})")
    if a.get("installed") is None:
        out.append(f"gitops_unverified: argocd 조회 실패({a.get('error')})")
    flux = [k for k in markers if any(f in k for f in FLUX_MARKERS)]
    if flux:
        out.append(f"gitops_unverified: Flux 표시 {flux} - 자동 동기화 상태 미확인")
    argo = [k for k in markers if k not in flux]
    if argo and not managing and a.get("installed") is not None:
        out.append(f"gitops_unverified: 관리 표시 {argo} 있으나 관리 주체 확인 안 됨(argocd installed={a.get('installed')})")
    return out


def assess(st: dict) -> dict:
    """격리 확인. reasons가 비어야 isolated=True."""
    reasons = list(st.get("errors") or [])
    d = st.get("deployment")
    if d is not None and d.get("replicas") != 0:
        reasons.append(f"deployment_replicas={d.get('replicas')}")
    if st.get("pods_present"):
        reasons.append(f"pods_present={st['pods_present']}(terminating={st.get('pods_terminating')})")
    if st.get("endpoints_ready") or st.get("endpoints_not_ready"):
        reasons.append(f"endpoints={st.get('endpoints_ready')}+{st.get('endpoints_not_ready')}")
    reasons += gitops_problems(st)
    return {"isolated": not reasons, "reasons": reasons, "state": st}


def _wait(check, *, timeout_sec, poll_sec, sleep, monotonic) -> dict:
    """check() -> (ok, detail). 조회가 끝난 직후 단조 시계를 읽어 기한 이하일 때만 성공으로 인정한다.
    기한을 넘겨 끝난 조회의 성공은 late_observation으로만 남긴다(ok=False)."""
    start = monotonic()
    deadline = start + timeout_sec
    while True:
        ok, detail = check()
        done = monotonic()
        if done > deadline:
            return {"ok": False, "detail": detail, "deadline_exceeded": True, "elapsed_sec": done - start,
                    "late_observation": {"ok": ok, "after_deadline_sec": done - deadline} if ok else None}
        if ok:
            return {"ok": True, "detail": detail, "elapsed_sec": done - start}
        if done >= deadline:
            return {"ok": False, "detail": detail, "deadline_exceeded": True, "elapsed_sec": done - start,
                    "late_observation": None}
        sleep(poll_sec)


def isolate(record_path: Path, *, apps, core, custom, timeout_sec=120.0, poll_sec=2.0, sleep=time.sleep,
            monotonic=time.monotonic, api_timeout: float = API_TIMEOUT_SEC) -> dict:
    st = read_state(apps=apps, core=core, custom=custom, api_timeout=api_timeout)
    if st["errors"] or "deployment" not in st:
        return {"isolated": False, "refused": f"상태 조회 실패 - 변경 안 함: {st['errors']}", "state": st}
    gp = gitops_problems(st)
    if gp:
        return {"isolated": False, "refused": f"GitOps 되돌림 위험 미확인 - 변경 안 함: {gp}", "state": st}
    record = {"deployment": DEPLOYMENT, "namespace": NAMESPACE, "deployment_uid": st["deployment"]["uid"],
              "original_replicas": st["deployment"]["replicas"], "isolated_at_utc": _now(), "state_before": st}
    with open(record_path, "x", encoding="utf-8") as fh:  # 바꾸기 전에 원래 값을 먼저 남긴다(덮어쓰지 않음)
        json.dump(record, fh, ensure_ascii=False, indent=1, default=str)
    apps.patch_namespaced_deployment_scale(DEPLOYMENT, NAMESPACE, {"spec": {"replicas": 0}}, _request_timeout=api_timeout)

    def check():
        a = assess(read_state(apps=apps, core=core, custom=custom, api_timeout=api_timeout))
        return a["isolated"], a
    w = _wait(check, timeout_sec=timeout_sec, poll_sec=poll_sec, sleep=sleep, monotonic=monotonic)
    return {"isolated": w["ok"], "record": str(record_path), "wait": w}


def _paused_preview(custom, api_timeout) -> bool:
    ro = custom.get_namespaced_custom_object(ROLLOUT[0], ROLLOUT[1], NAMESPACE, ROLLOUT[2], ROLLOUT[3],
                                             _request_timeout=api_timeout)
    return any(_g(c, "reason") == "BlueGreenPause" for c in (_g(ro, "status", "pauseConditions", default=[]) or []))


def restore(record: dict, *, apps, core, custom, timeout_sec=300.0, poll_sec=5.0, sleep=time.sleep,
            monotonic=time.monotonic, api_timeout: float = API_TIMEOUT_SEC) -> dict:
    """예외를 던지지 않는다. restored=False면 사람이 확인한다(다음 실행 차단 근거)."""
    out = {"restored": False, "error": None, "restored_at_utc": None}
    try:
        st = read_state(apps=apps, core=core, custom=custom, api_timeout=api_timeout)
        d = st.get("deployment") or {}
        if st["errors"]:
            out["error"] = f"상태 조회 실패 - 복원 안 함: {st['errors']}"
        elif d.get("uid") != record["deployment_uid"]:
            out["error"] = f"Deployment UID가 바뀜 - 덮어쓰지 않음: {d.get('uid')}"
        elif d.get("replicas") != 0:
            out["error"] = f"현재 replicas={d.get('replicas')} - 다른 주체가 바꿨을 수 있음, 덮어쓰지 않음"
        elif _paused_preview(custom, api_timeout):
            out["error"] = "일시정지 preview가 남아 있음 - 복원하면 기존 정책이 승격할 수 있어 복원 안 함"
        if out["error"]:
            return out
        n = record["original_replicas"]
        apps.patch_namespaced_deployment_scale(DEPLOYMENT, NAMESPACE, {"spec": {"replicas": n}}, _request_timeout=api_timeout)

        def ready():
            s = read_state(apps=apps, core=core, custom=custom, api_timeout=api_timeout)
            dd = s.get("deployment") or {}
            ok = bool(not s["errors"] and dd.get("replicas") == n
                      and (n == 0 or (dd.get("ready_replicas") == n and s.get("endpoints_ready"))))
            return ok, s
        w = _wait(ready, timeout_sec=timeout_sec, poll_sec=poll_sec, sleep=sleep, monotonic=monotonic)
        out.update(restored=w["ok"], wait=w, restored_at_utc=_now() if w["ok"] else None,
                   error=None if w["ok"] else (f"{timeout_sec:.0f}초 기한 안에 Ready·endpoints 확인 안 됨"
                                               + (" (기한 뒤 성공 관측 - 기한 내 성공 아님)" if w.get("late_observation") else "")))
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _clients():  # pragma: no cover - 라이브 전용
    from kubernetes import client, config
    config.load_kube_config()
    return {"apps": client.AppsV1Api(), "core": client.CoreV1Api(), "custom": client.CustomObjectsApi()}


def main(argv=None):  # pragma: no cover - 라이브 전용(각 동작 별도 승인)
    p = argparse.ArgumentParser(description="기존 recovery-policy 승격 경로 격리·복원(라이브 변경 - 승인 필요)")
    p.add_argument("action", choices=["status", "isolate", "restore"])
    p.add_argument("--record", type=Path)
    a = p.parse_args(argv)
    c = _clients()
    if a.action == "status":
        r = assess(read_state(**c))
    elif a.action == "isolate":
        r = isolate(a.record, **c)
    else:
        r = restore(json.loads(a.record.read_text(encoding="utf-8")), **c)
    print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
    ok = r.get("isolated") if a.action != "restore" else r.get("restored")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
