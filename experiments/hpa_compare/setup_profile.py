"""공통 구성 고정: 11초 network_tolerant 프로필 적용(Rollout 템플릿 변경 -> 새 리비전 -> 승격). 적용 전 상태와 적용 후 상태를 저장한다.
되돌리기: `python setup_profile.py --restore` (기본 rollout.yaml 재적용 + experiment-prep-ts 는 적용 전 값으로, 승격)."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hc_common as hc  # noqa: E402


def promote():
    hc.kubectl("exec", "-n", hc.NS, "deploy/guarded-recovery-policy", "--", "kubectl-argo-rollouts", "promote", hc.ROLLOUT, "-n", hc.NS, timeout=60)


def wait_paused(timeout=900):
    def paused():
        ro = hc.rollout_state()
        return ro["phase"] == "Paused" and ro["preview"] != ro["active"] and str(ro["observed"]) == str(ro["generation"]) and \
            all(p["ready"] for p in hc.vllm_pods() if p["hash"] == ro["preview"])
    hc.wait_until(paused, timeout, 5.0, "새 리비전 preview Ready/Paused")


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def template_facts():
    """Rollout 템플릿의 종료 관련 값: vllm 컨테이너 args 의 --shutdown-timeout, terminationGracePeriodSeconds, readiness timeout."""
    spec = hc.kjson("get", "rollout", hc.ROLLOUT, "-n", hc.NS)["spec"]["template"]["spec"]
    c = spec["containers"][0]
    st = [a for a in c.get("args", []) if a.startswith("--shutdown-timeout")]
    return {"shutdown_timeout_arg": st[0] if st else None, "termination_grace_sec": spec.get("terminationGracePeriodSeconds"),
            "readiness_timeout": (c.get("readinessProbe") or {}).get("timeoutSeconds")}


def main():
    """인자(2026-10-23 추가): --overlay DIR(기본 network-tolerant) --tag NAME(기본 apply_tolerant; 기록은 덮어쓰지 않으므로 새 이름을 쓴다)
    --base(overlay 대신 기본 rollout.yaml 을 그대로 적용 = 11초 프로필·드레인 설정 제거, 최종 복원용) --expect tolerant|drain|base(적용 뒤 확인)."""
    out = hc.RESULTS / "setup"
    tag = arg("--tag", "apply_tolerant")
    expect = arg("--expect", "tolerant")
    probs = hc.other_experiment_problems()
    if probs:
        raise SystemExit(f"다른 작업과 충돌: {probs}")
    before = hc.snapshot()
    before["template_facts"] = template_facts()
    hc.jdump(out / f"{tag}-before.json", before)
    hc.log("적용 전 상태 저장")

    def ok(f):
        if expect == "tolerant":
            return f["readiness_timeout"] == 11 and not f["shutdown_timeout_arg"]
        if expect == "drain":
            return f["readiness_timeout"] == 11 and bool(f["shutdown_timeout_arg"]) and bool(f["termination_grace_sec"])
        return f["readiness_timeout"] != 11 and not f["shutdown_timeout_arg"] and not f["termination_grace_sec"]
    if ok(before["template_facts"]):
        hc.log(f"이미 기대 구성({expect})")
        return
    # overlay 는 Service·RBAC·ServiceMonitor 등도 렌더링하므로, 변경 범위를 Rollout 하나로 제한한다
    if "--base" in sys.argv:
        rendered = hc.BASE_ROLLOUT.read_text(encoding="utf-8")
    else:
        rendered = hc.kubectl("kustomize", arg("--overlay", str(hc.TOLERANT_OVERLAY)), "--load-restrictor=LoadRestrictionsNone", timeout=120).stdout
    docs = [d for d in re.split(r"(?m)^---\s*$", rendered) if re.search(r"(?m)^kind: Rollout\s*$", d)]
    assert len(docs) == 1, f"Rollout 문서가 1개가 아님: {len(docs)}"
    hc.jdump(out / f"{tag}-applied-rollout.json", {"yaml": docs[0]})
    hc.kubectl("apply", "-f", "-", input_text=docs[0], timeout=120)
    hc.log("Rollout 만 적용, preview 대기")
    wait_paused()
    promote()
    hc.log("승격")
    hc.wait_until(lambda: hc.settled(1), 900, 5.0, "승격 후 Healthy/Pod 1개")
    after = hc.snapshot()
    after["template_facts"] = template_facts()
    hc.jdump(out / f"{tag}-after.json", after)
    assert ok(after["template_facts"]), after["template_facts"]
    hc.log(f"완료: active={after['rollout']['active']} {after['template_facts']}")


if __name__ == "__main__":
    main()
