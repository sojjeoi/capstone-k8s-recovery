"""old_policy_isolation 오프라인 테스트 - 가짜 Kubernetes API·시계. 클러스터 접속 없음."""
import json
from types import SimpleNamespace as NS

import pytest

import old_policy_isolation as opi


class ApiErr(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class Clock:
    def __init__(self):
        self.t = 0.0

    def mono(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeK8s:
    """모든 호출에 _request_timeout이 있어야 한다. read_cost: Deployment 조회 1회에 걸리는 시간(가짜 시계)."""

    def __init__(self, replicas=1, argocd=None, labels=None, paused=False, uid="d1", fail=None, terminating=0,
                 clock=None, read_cost=0.0, ready_after_reads=0):
        self.replicas, self.ready, self.uid, self.paused, self.fail = replicas, replicas, uid, paused, fail
        self.argocd, self.labels, self.calls, self.terminating = argocd, labels or {}, [], terminating
        self.clock, self.read_cost, self.ready_after_reads, self.reads = clock, read_cost, ready_after_reads, 0
        self.timeouts = []

    def _t(self, kw):
        assert kw.get("_request_timeout"), "호출 제한 없음"
        self.timeouts.append(kw["_request_timeout"])

    def read_namespaced_deployment(self, name, ns, **kw):
        self._t(kw)
        self.reads += 1
        if self.clock:
            self.clock.t += self.read_cost
        if self.fail == "deployment" or (self.fail == "after1" and self.reads > 1):
            raise TimeoutError("read timed out")
        return NS(metadata=NS(uid=self.uid, labels=self.labels, annotations={}),
                  spec=NS(replicas=self.replicas), status=NS(ready_replicas=self._ready()))

    def _ready(self):
        return self.ready if self.reads > self.ready_after_reads else 0

    def patch_namespaced_deployment_scale(self, name, ns, body, **kw):
        self._t(kw)
        self.calls.append(("patch", body["spec"]["replicas"]))
        self.replicas = self.ready = body["spec"]["replicas"]
        self.terminating = 0

    def list_namespaced_pod(self, ns, label_selector=None, **kw):
        self._t(kw)
        live = [NS(metadata=NS(deletion_timestamp=None)) for _ in range(self._ready())]
        return NS(items=live + [NS(metadata=NS(deletion_timestamp="2026-10-06T00:00:00Z")) for _ in range(self.terminating)])

    def read_namespaced_endpoints(self, name, ns, **kw):
        self._t(kw)
        r = self._ready()
        return NS(subsets=[NS(addresses=[NS(ip="10.0.0.9")] * r, not_ready_addresses=None)] if r else None)

    def list_cluster_custom_object(self, g, v, plural, **kw):
        self._t(kw)
        if self.argocd is None:
            raise ApiErr(404)
        if self.argocd == "error":
            raise ApiErr(403)
        return {"items": self.argocd}

    def get_namespaced_custom_object(self, g, v, ns, plural, name, **kw):
        self._t(kw)
        return {"status": {"pauseConditions": [{"reason": "BlueGreenPause"}] if self.paused else []}}


def _apis(k):
    return {"apps": k, "core": k, "custom": k}


def _app(automated):
    return {"metadata": {"name": "rp"}, "spec": {"syncPolicy": {"automated": {"selfHeal": True}} if automated else {}},
            "status": {"resources": [{"kind": "Deployment", "name": "recovery-policy", "namespace": "vllm-serving"}]}}


def _assess(k):
    return opi.assess(opi.read_state(**_apis(k)))


def test_running_policy_is_not_isolated():
    a = _assess(FakeK8s(replicas=1))
    assert a["isolated"] is False and "deployment_replicas=1" in a["reasons"] and any("endpoints" in r for r in a["reasons"])


def test_scaled_to_zero_without_gitops_is_isolated_and_all_calls_have_timeouts():
    k = FakeK8s(replicas=0)
    assert _assess(k)["isolated"] is True and len(k.timeouts) == 4 and set(k.timeouts) == {opi.API_TIMEOUT_SEC}


# ---------- 1. 삭제 중인 Pod ----------
def test_terminating_pod_blocks_isolation_even_with_zero_replicas_and_endpoints():
    a = _assess(FakeK8s(replicas=0, terminating=1))
    assert a["isolated"] is False and "pods_present=1(terminating=1)" in a["reasons"]
    assert not any("endpoints" in r or "replicas" in r for r in a["reasons"])  # 다른 조건은 통과한 상태


# ---------- 2. GitOps 확인 불가 ----------
@pytest.mark.parametrize("k,why", [
    (FakeK8s(replicas=0, argocd=[_app(True)]), "gitops_auto_sync"),
    (FakeK8s(replicas=0, argocd="error"), "argocd 조회 실패"),  # 표시가 없어도 Argo CD 상태 불명이면 거부
    (FakeK8s(replicas=0, labels={"kustomize.toolkit.fluxcd.io/name": "apps"}), "Flux"),  # Argo CD 없음(404)이어도 거부
    (FakeK8s(replicas=0, argocd=[], labels={"kustomize.toolkit.fluxcd.io/name": "apps"}), "Flux"),
    (FakeK8s(replicas=0, labels={"app.kubernetes.io/instance": "rp"}), "관리 주체 확인 안 됨"),  # Argo CD 없음 + 표시
    (FakeK8s(replicas=0, argocd=[], labels={"argocd.argoproj.io/instance": "rp"}), "관리 주체 확인 안 됨"),
    (FakeK8s(replicas=0, fail="deployment"), "deployment:")])
def test_unverifiable_gitops_or_read_error_is_not_isolated(k, why):
    a = _assess(k)
    assert a["isolated"] is False and any(why in r for r in a["reasons"])


def test_argocd_managed_without_automated_sync_is_allowed():
    assert _assess(FakeK8s(replicas=0, argocd=[_app(False)], labels={"app.kubernetes.io/instance": "rp"}))["isolated"]


def test_isolate_refuses_under_unverifiable_gitops_without_change(tmp_path):
    k = FakeK8s(replicas=1, labels={"helm.toolkit.fluxcd.io/name": "rp"})
    r = opi.isolate(tmp_path / "iso.json", **_apis(k), sleep=lambda s: None, monotonic=lambda: 0.0)
    assert r["isolated"] is False and "GitOps" in r["refused"] and k.calls == [] and not (tmp_path / "iso.json").exists()


# ---------- 격리·기록 ----------
def test_isolate_records_original_before_patch_and_verifies(tmp_path):
    k = FakeK8s(replicas=1)
    rec = tmp_path / "iso.json"
    r = opi.isolate(rec, **_apis(k), sleep=lambda s: None, monotonic=lambda: 0.0)
    assert r["isolated"] is True and k.calls == [("patch", 0)]
    saved = json.loads(rec.read_text(encoding="utf-8"))
    assert saved["original_replicas"] == 1 and saved["deployment_uid"] == "d1"


def test_isolate_waits_while_pod_still_terminating(tmp_path):
    c = Clock()
    k = FakeK8s(replicas=1, clock=c)
    orig = k.patch_namespaced_deployment_scale

    def patch_leaves_terminating(name, ns, body, **kw):
        orig(name, ns, body, **kw)
        k.terminating = 1  # 내린 뒤에도 종료 유예 중인 Pod가 남음
    k.patch_namespaced_deployment_scale = patch_leaves_terminating
    r = opi.isolate(tmp_path / "iso.json", **_apis(k), timeout_sec=10, poll_sec=2, sleep=c.sleep, monotonic=c.mono)
    assert r["isolated"] is False and r["wait"]["deadline_exceeded"] is True
    assert any("pods_present" in x for x in r["wait"]["detail"]["reasons"])


def test_isolate_never_overwrites_existing_record(tmp_path):
    rec = tmp_path / "iso.json"
    rec.write_text("{}", encoding="utf-8")
    k = FakeK8s(replicas=1)
    with pytest.raises(FileExistsError):
        opi.isolate(rec, **_apis(k), sleep=lambda s: None, monotonic=lambda: 0.0)
    assert k.calls == []


# ---------- 3. 시간 제한 ----------
def test_isolation_success_arriving_after_deadline_is_not_success(tmp_path):
    c = Clock()
    k = FakeK8s(replicas=1, clock=c, read_cost=15.0)  # 첫 확인 조회가 15초 걸려 '격리됨'을 돌려줌, 기한 10초
    r = opi.isolate(tmp_path / "iso.json", **_apis(k), timeout_sec=10, sleep=c.sleep, monotonic=c.mono)
    assert r["isolated"] is False and r["wait"]["deadline_exceeded"] is True
    assert r["wait"]["late_observation"] == {"ok": True, "after_deadline_sec": 5.0}


def test_isolation_success_exactly_at_deadline_is_accepted(tmp_path):
    c = Clock()
    k = FakeK8s(replicas=1, clock=c, read_cost=5.0)  # 사전 조회 5초 + 확인 조회 5초 → 대기 시작 뒤 정확히 5초 = 기한
    r = opi.isolate(tmp_path / "iso.json", **_apis(k), timeout_sec=5, sleep=c.sleep, monotonic=c.mono)
    assert r["isolated"] is True and r["wait"]["elapsed_sec"] == 5.0


def test_isolation_read_timeout_is_not_isolated(tmp_path):
    c = Clock()
    k = FakeK8s(replicas=1, clock=c, fail="after1")  # 사전 조회는 되고, 변경 뒤 확인 조회가 계속 시간 초과
    r = opi.isolate(tmp_path / "iso.json", **_apis(k), timeout_sec=6, poll_sec=2, sleep=c.sleep, monotonic=c.mono)
    assert r["isolated"] is False and any("TimeoutError" in x for x in r["wait"]["detail"]["reasons"])


def _record(uid="d1", n=1):
    return {"deployment_uid": uid, "original_replicas": n}


def test_restore_scales_back_and_waits_ready():
    k = FakeK8s(replicas=0)
    r = opi.restore(_record(), **_apis(k), sleep=lambda s: None, monotonic=lambda: 0.0)
    assert r["restored"] is True and k.calls == [("patch", 1)]


def test_restore_ready_after_deadline_is_not_success():
    c = Clock()
    k = FakeK8s(replicas=0, clock=c, read_cost=20.0)  # 복원 뒤 첫 확인이 20초 걸려 Ready를 돌려줌, 기한 10초
    r = opi.restore(_record(), **_apis(k), timeout_sec=10, sleep=c.sleep, monotonic=c.mono)
    assert r["restored"] is False and "기한 뒤 성공 관측" in r["error"] and r["wait"]["late_observation"]["ok"] is True


@pytest.mark.parametrize("k,why", [(FakeK8s(replicas=0, uid="d2"), "UID"), (FakeK8s(replicas=2), "다른 주체"),
                                   (FakeK8s(replicas=0, paused=True), "일시정지 preview"),
                                   (FakeK8s(replicas=0, fail="deployment"), "조회 실패")])
def test_restore_refuses_without_overwrite(k, why):
    r = opi.restore(_record(), **_apis(k), sleep=lambda s: None, monotonic=lambda: 0.0)
    assert r["restored"] is False and why in r["error"] and k.calls == []
