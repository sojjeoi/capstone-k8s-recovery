"""run_guarded_pilot 연결 테스트 - 새 실행기 진입점(run_guarded)부터 끝까지 돈다.
실제로 쓰는 것: 기존 시행 함수(run_no_switch_control_trial·run_no_injection_smoke), compose_prepare, 실제 guarded 정책
(GuardedPolicy + HTTP 앱, TestClient), c1_policy_link.send·c1_live_detector.classify_send_result.
가짜로 바꾼 것: 클러스터(대상 조회·승격·서비스 확인·유예 설정), 주입기·probe·capture sink 프로세스, 탐지기 프로세스, 시계.
네트워크·클러스터 접속 없음."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import guarded_live_detector_runner as gr
import run_guarded_pilot as gp
import run_if_threshold_candidate_smoke as smoke
import run_timed_switch_recovery_trial as rt
from run_if_threshold_candidate_smoke import DetectorHandle
from run_once import HarnessCorrupted

sys.path.append(str(Path(__file__).resolve().parent.parent / "recovery-policy"))
import guarded_ledger as gl  # noqa: E402
import guarded_state as gs  # noqa: E402
from guarded_policy import AuditLog, GuardConfig, GuardedPolicy  # noqa: E402
from main_guarded import create_app  # noqa: E402

TIMING = {"max_signal_age_sec": 30.0, "max_future_skew_sec": 5.0, "max_window_to_decision_sec": 30.0}  # 테스트 값(채택 아님)
CANON_SNAP = {"generation": 1, "observed": "1", "phase": "Healthy", "abort": None, "active": "aaa111", "preview": "aaa111",
              "stable": "aaa111", "current": "aaa111", "delay": 30,
              "rs": {"aaa111": {"uid": "r1", "desired": 1, "current": 1, "ready": 1}},
              "pods": [{"name": "vllm-serving-aaa111-x", "uid": "up", "ip": "10.0.0.1", "hash": "aaa111", "ready": True,
                        "deleting": False, "restarts": 0}],
              "svc_selectors": {"vllm-active": {"rollouts-pod-template-hash": "aaa111"},
                                "vllm-preview": {"rollouts-pod-template-hash": "aaa111"}},
              "endpoints": {"vllm-active": ["10.0.0.1"], "vllm-preview": ["10.0.0.1"]}, "health_rc": 0, "chaos": [], "probe_pods": []}
ROLL = {"namespace": "vllm-serving", "name": "vllm-serving", "uid": "ru1"}
PODS = [{"name": "vllm-serving-aaa111-x", "uid": "ua", "hash": "aaa111", "restart_count": 0, "ip": "10.0.0.1"},
        {"name": "vllm-serving-bbb222-y", "uid": "up", "hash": "bbb222", "restart_count": 0, "ip": "10.0.0.2"}]


def view():
    return {"rollout": {**ROLL, "active_selector": "aaa111", "preview_selector": "bbb222", "paused_bluegreen": True},
            "pods": [{**p, "ready": True, "terminating": False} for p in PODS]}


def _now():
    return datetime.now(timezone.utc)


class Clock:
    def __init__(self):
        self.t, self.listeners = 0.0, []

    def mono(self):
        return self.t

    def sleep(self, s):
        self.t += s
        for f in list(self.listeners):
            f(self.t)


class Http:
    """requests 모양 호출을 TestClient로 넘긴다."""
    def __init__(self, c):
        self.c = c

    @staticmethod
    def _path(url):
        return "/" + url.split("/", 3)[3]

    def get(self, url, headers=None, params=None, **_):
        return self.c.get(self._path(url), headers=headers, params=params)

    def post(self, url, headers=None, params=None, json=None, **_):
        return self.c.post(self._path(url), headers=headers, params=params, json=json)


class FakeDetector:
    """c1_live_detector의 기록·송신·분류·종료 규칙(link.send + ld.classify_send_result)을 그대로 따른다."""

    def __init__(self, world, send_at, ready=True, resend=False, lose_response=False):
        self.world, self.send_at, self.ready, self.resend = world, list(send_at), ready, resend
        self.lose_response = lose_response
        self.alive, self.post_action, self.seq, self.stops, self.posts = False, False, 0, 0, 0
        self.last_payload = None

    def _w(self, rec):
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def starter_factory(self, results_dir, policy_url, version, mode):
        self.factory_args = (policy_url, version, mode)

        def start(rid, path, sink_url):
            self.rid, self.path, self.alive = rid, gr.live_evidence_path(results_dir, rid), True
            art = gp.sd.load_artifacts(gp.sd.DEFAULT_ARTIFACTS)
            self.start_rec = {"record_type": "c1_live_start", "run_id": rid, "detector": gp.ld.DETECTOR_ID,
                              "artifact_hashes": art["hashes"], "threshold": art["threshold"],
                              "started_at_utc": _now().isoformat()}
            self._w(self.start_rec)
            self._w({"record_type": "c1_evaluation", "run_id": rid, "evaluation_seq": 0,
                     "status": "scored" if self.ready else "skipped", "score": 0.1 if self.ready else None,
                     "skip_reason": None if self.ready else "preview_missing", "condition_met": False})
            self.t0 = self.world.clock.t
            self.world.clock.listeners.append(self.tick)
            return DetectorHandle(is_alive=lambda: self.alive, stop=self.stop, label="fake-c1-live")
        return start

    def _post(self, url, json=None, **_):
        self.posts += 1
        return self.world.client.post("/signal/v2", json=json)

    def _send(self, payload):
        self._w({"record_type": "c1_policy_send_attempt", "run_id": self.rid, "evaluation_seq": self.seq})
        sr = gp.link.send("http://p/signal/v2", payload, post=self._post)
        if self.lose_response:  # 정책은 처리했지만 탐지기는 결과를 기록하지 못함
            self.post_action = True
            return
        dec = gp.ld.classify_send_result(sr)
        self._w({"record_type": "c1_policy_send_result", "run_id": self.rid, "evaluation_seq": self.seq,
                 "decision": dec, **sr})
        if dec.startswith("stop_"):
            self.alive = False  # 실제 탐지기는 종료 코드 5/6으로 끝난다
        elif dec == "post_action":
            self.post_action = True

    def tick(self, t):
        if not self.alive:
            return
        if getattr(self, "die_at", None) is not None and t - self.t0 >= self.die_at:
            self.alive = False  # 필수 기록 실패(EvidenceWriteFailed, 종료 코드 2) 등 탐지기 자체 종료
            return
        if self.post_action:
            if self.resend and self.last_payload is not None:  # 같은 신호 재전송(응답 유실 흉내) - 1회
                self.resend = False
                self._send(self.last_payload)
            return
        if self.send_at and t - self.t0 >= self.send_at[0]:
            self.send_at.pop(0)
            self.seq += 1
            d = _now()
            rec = {"record_type": "c1_evaluation", "run_id": self.rid, "evaluation_seq": self.seq, "status": "scored",
                   "score": -0.2, "condition_met": True, "condition_met_at_utc": d.isoformat(),
                   "window_start_utc": (d - timedelta(seconds=62)).isoformat(),
                   "window_end_utc": (d - timedelta(seconds=2)).isoformat(), "consecutive_anomalous": 3,
                   "expected_pods": {p["name"]: p["uid"] for p in PODS},
                   "pod_snapshot": {"active_selector": "aaa111", "preview_selector": "bbb222", "pods": PODS}}
            self._w(rec)
            self.last_payload = gp.link.build_v2_payload(self.start_rec, rec)
            self._send(self.last_payload)

    def stop(self):
        self.alive = False
        self.stops += 1
        return {"returncode": 0, "exited_cleanly": True}


class FakeInjector:
    get_actual_injection_time = None

    def __init__(self):
        self.calls = []

    def prepare(self):
        self.calls.append("prepare")

    def get_real_promotion_diagnostic_exception_info(self):
        return {"prediction": {"active_node": "sj-worker"}}

    def inject(self):
        self.calls.append("inject")

    def is_started(self):
        return True

    def is_effective(self):
        return True

    def is_done(self):
        return False

    def cleanup(self):
        self.calls.append("cleanup")

    def get_stress_delivery_record(self):
        return {"planned_cr_names": []}


class FakeProber:
    def __init__(self, clock=None):
        self.calls, self.clock, self.refresh_times, self.stop_time = [], clock, [], None

    def start(self):
        self.calls.append("start")

    def is_alive(self):
        return True

    def get_baseline_status(self):
        return {"ready": True}

    def check_slo_violation(self):
        if self.clock:
            self.refresh_times.append(self.clock.t)
        return None

    def stop(self):
        self.calls.append("stop")
        if self.clock:
            self.stop_time = self.clock.t


class FakeThread:
    """guard를 시계로 delay초 뒤에 실행한다(그 전까지 진행 중)."""

    def __init__(self, clock, delay, fn, args):
        self.clock, self.until, self.fn, self.args, self.done = clock, clock.t + delay, fn, args, False

    def _run(self):
        self.done = True
        self.fn(*self.args)

    def is_alive(self):
        if not self.done and self.clock.t >= self.until:
            self._run()
        return not self.done

    def join(self, timeout=None):
        if not self.done:
            self._run()


class FakeProc:
    def poll(self):
        return None


class FakeCluster:
    """실행기가 읽는 실제 Rollout·Service·RS 흉내(정책의 static view와 별개). 승격되면 selector가 바뀌고, 구 RS는
    전환 시각 + 그때의 유예 뒤에야 0이 된다(pilot04 B 실측: 구 Pod 종료 = 전환 + 유예)."""

    def __init__(self, world):
        self.w, self.active, self.service, self.switched_at, self.zero_delay = world, "aaa111", "aaa111", None, None
        self.uid, self.current, self.fail_view = "ru1", "bbb222", False

    def switch(self, rollout=True, service=True):
        self.active = "bbb222" if rollout else self.active
        self.service = "bbb222" if service else self.service
        self.switched_at, self.zero_delay = self.w.clock.t, self.w.delay

    def view(self):
        if self.fail_view:
            raise TimeoutError("rollout read timeout")
        return {"uid": self.uid, "active_selector": self.active, "preview_selector": "bbb222",
                "current_pod_hash": self.current, "delay": self.w.delay, "service_selector": self.service}

    def old_zero(self):
        return self.switched_at is not None and self.w.clock.t >= self.switched_at + self.zero_delay

    def list_rs(self):
        z = self.old_zero()
        return {"aaa111": {"desired": 0 if z else 1, "current": 0 if z else 1}, "bbb222": {"desired": 1, "current": 1}}


class World:
    def __init__(self, tmp_path, monkeypatch, *, mode, service=None, defer_service=False, send_at=(50.0,), ready=True,
                 resend=False, restore_ok=True, ledger_seed=None, promote_verified=True, die_at=None,
                 guard_delay=None, lose_response=False, old_policy=None, service_delay=None, wrap_preview=False,
                 switch_on_unknown=False):
        monkeypatch.setattr(smoke, "_start_subprocess", lambda *a, **k: {"proc": FakeProc(), "alive_after_start": True})
        monkeypatch.setattr(smoke, "_stop_subprocess", lambda *a, **k: {"returncode": 0, "exited_cleanly": True})
        monkeypatch.setattr(smoke, "_wait_http_ok", lambda *a, **k: True)
        self.tmp, self.mode, self.clock = tmp_path, mode, Clock()
        self.promotes, self.overrides, self.pending, self.delay, self.restore_ok = [], [], [], 30, restore_ok
        self.promote_verified = promote_verified
        data = tmp_path / "policy"
        audit = AuditLog(data / "audit.jsonl")
        if mode == gp.MODE_ACT:
            led = gl.Ledger(data / "ledger.jsonl", create=True)
            for rec in ledger_seed or []:
                led.append(rec)
            runner = (lambda fn: self.pending.append(fn)) if defer_service else (
                (lambda fn: self._later(fn, service_delay)) if service_delay else (lambda fn: fn()))
            self.policy = GuardedPolicy(GuardConfig(mode="act", **TIMING), gs.StateStore(data / "state.json"), audit=audit,
                                        cluster_reader=view, promote_fn=self._promote,
                                        service_check_fn=service or (lambda job: {"ok": True}), ledger=led,
                                        service_runner=runner, initialize=True)
        else:
            self.policy = GuardedPolicy(GuardConfig(mode="record_only", **TIMING), gs.StateStore(data / "state.json"),
                                        audit=audit, cluster_reader=view, promote_fn=self._promote, initialize=True)
        self.client = TestClient(create_app(self.policy, admin_token="tok"))
        self.policy_client = gp.PolicyClient("http://p", "tok", http=Http(self.client))
        self.detector = FakeDetector(self, send_at, ready=ready, resend=resend, lose_response=lose_response)
        self.guard_delay, self.threads = guard_delay, []
        self.old_policy = old_policy or (lambda: {"isolated": True, "reasons": []})
        self.detector.die_at = die_at
        self.injector, self.prober = FakeInjector(), FakeProber(self.clock)
        self.results = tmp_path / "results"
        self.cluster, self.restores, self.preview_cleanups = FakeCluster(self), [], []
        self.wrap_preview, self.switch_on_unknown, self.ledger_path = wrap_preview, switch_on_unknown, data / "ledger.jsonl"
        self.before_patch = None
        # 계약 r2 canonical 상태(가짜 읽기) - 기본 canonical. canonical_restore는 호출만 기록하고 결과를 돌려준다
        self.canon, self.restore_calls, self.restore_result = json.loads(json.dumps(CANON_SNAP)), [], {"ok": True, "problems": []}

    def _canonical_restore(self, prep, pod):
        self.restore_calls.append((prep.get("created_pod_hash"), pod))
        return dict(self.restore_result)

    def make_injector(self, rid, rep, kind, cleanup_preview_fn=None):
        if not self.wrap_preview:
            return self.injector
        import arm_controller  # 실제 preview wrapper(동결)에 관문을 주입 - 원래 cleanup(압박 제거) 뒤 관문 호출
        prep = {"ready": True, "pre_prepare_active_selector": "aaa111", "created_pod_hash": "bbb222",
                "external_interference": False}
        return arm_controller.wrap_injector_with_preview_prep(self.injector, gp.rt.ARM_LABEL,
                                                              prepare_preview_fn=lambda *a: dict(prep),
                                                              cleanup_preview_fn=cleanup_preview_fn)

    def real_preview_cleanup(self, prep, name, ns):  # blue_green_prep.cleanup_unpromoted_preview 자리(abort 흉내)
        self.preview_cleanups.append(prep)
        return True

    def restore_override(self, record, *, promoted, old_pod_hash, wait_timeout_sec, patch_fn):
        """scale_down_delay_override.restore_override 자리 - 소유·구 RS 확인 뒤 patch_fn(30)을 부른다(실제와 같은 순서)."""
        self.restores.append({"t": self.clock.t, "promoted": promoted, "old_zero": self.cluster.old_zero()})
        if not self.restore_ok:
            return {"restored": False, "error": "구 RS가 0이 안 됨"}
        if promoted and not self.cluster.old_zero():
            return {"restored": False, "error": "구 RS 아직 남음"}
        if self.before_patch:
            self.before_patch()
        try:
            patch_fn(30)
        except Exception as e:  # noqa: BLE001 - 실제 restore_override처럼 예외를 결과로 돌려준다
            return {"restored": False, "error": f"{type(e).__name__}: {e}"}
        return {"restored": True, "final": {"delay": self.delay}}

    def ledger_read(self):
        return [json.loads(x) for x in self.ledger_path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def _later(self, fn, delay):  # 정책의 비동기 서비스 확인이 delay초 뒤 끝나는 상황
        due, box = self.clock.t + delay, {"done": False}

        def tick(t):
            if not box["done"] and t >= due:
                box["done"] = True
                fn()
        self.clock.listeners.append(tick)

    def _thread_runner(self, fn, *args):
        if self.guard_delay is None:
            return fn(*args)
        t = FakeThread(self.clock, self.guard_delay, fn, args)
        self.threads.append(t)
        return t

    def _promote(self, target):
        self.promotes.append(target)
        v = self.promote_verified
        if v or (v is None and self.switch_on_unknown):  # 시간 초과여도 실제 selector는 바뀐 경우
            self.cluster.switch()
        return {"called": True, "verified": v, "verified_at": _now().isoformat() if v else None}

    def apply_override(self, sec, path):
        self.overrides.append(sec)
        self.delay = sec
        return {"verified": True, "applied_value": sec, "original_value": 30, "original_present": True,
                "rollout_uid": "ru1", "before": {"active_selector": "aaa111", "uid": "ru1"}}

    def deps(self, **over):
        d = gp.Deps(
            policy=self.policy_client, check_unresolved=lambda: rt.check_no_unresolved_failures(self.results),
            preflight=lambda pol, **k: {"ok": True, "reasons": [], "guarded_healthz": pol.healthz()},
            session_check=lambda: {"ok": True, "problems": []}, rollout_identity=lambda: dict(ROLL),
            read_delay=lambda: self.delay, make_injector=self.make_injector,
            make_prober=lambda *a: self.prober, base_safety=lambda inj, kind: (lambda b, p: None),
            run_guard_check=lambda *a, **k: None, probe_image_check=lambda rec: rec.update(verified=True),
            trial_pressure=smoke.run_no_switch_control_trial, trial_no_injection=smoke.run_no_injection_smoke,
            apply_override=self.apply_override, patch_delay=lambda v: setattr(self, "delay", v),
            wait_chaos_gone=lambda names: [], read_probe_pods=lambda: [], capture_events=lambda since: [],
            starter_factory=self.detector.starter_factory, thread_runner=self._thread_runner,
            old_policy_check=lambda: self.old_policy(),
            sleep_fn=self.clock.sleep, monotonic_fn=self.clock.mono, pick_port=lambda: 1,
            probe_csv_path=lambda rid, rep: None, rollout_view=self.cluster.view, list_rs=self.cluster.list_rs,
            ledger_read=self.ledger_read, restore_override=self.restore_override,
            cleanup_unpromoted_preview=self.real_preview_cleanup,
            cpu_raw_counter=lambda pod, t0: [(t0.isoformat(), 1.0)],
            canonical_snapshot=lambda: json.loads(json.dumps(self.canon)), canonical_restore=self._canonical_restore)
        for k, v in over.items():
            setattr(d, k, v)
        return d

    def approval(self, **over):
        (self.tmp / "clock.json").write_text("{}", encoding="utf-8")
        a = {"status": "final", "timing_source": "clock-measurement-2026xxxx", "clock_measurement_file": "clock.json",
             "timing": dict(TIMING), "service_wait_deadline_sec": 80.0, "approved_by": "user",
             "approved_at_utc": "2026-10-06T00:00:00+00:00", **over}
        p = self.tmp / "approval.json"
        p.write_text(json.dumps(a), encoding="utf-8")
        return p

    def run(self, run, **kw):
        return gp.run_guarded(run, mode=self.mode, deps=kw.pop("deps", None) or self.deps(), policy_url="http://p",
                              results_dir=self.results, **kw)


# ---------------- record_only ----------------
@pytest.mark.parametrize("run", ["grn01", "grp01"])
def test_record_only_entry_zero_promotions_and_no_delay_change(tmp_path, monkeypatch, run):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO)
    s = w.run(run)
    assert w.promotes == [] and w.overrides == [] and w.delay == 30
    assert s["outcome_status"] == "completed" and s["cleanup_errors"] == []
    assert s["evidence_counts"]["send_results"] == {"would_pass_record_only": 1}
    assert s["rehearsal"]["connection_evaluation_recording_ok"] is True and s["rehearsal"]["signal_observed"] is True
    assert ("inject" in w.injector.calls) is (run == "grp01") and "cleanup" in w.injector.calls
    assert w.detector.factory_args == ("http://p", "guarded-v1", "record_only")
    assert w.policy.snapshot()["context"] is None  # 해제됨
    assert not list(w.results.glob("running-*.json"))


def test_record_only_without_signal_reports_connection_only(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO, send_at=())
    s = w.run("grn02")
    assert s["rehearsal"]["connection_evaluation_recording_ok"] is True and s["rehearsal"]["signal_observed"] is False
    assert "확인되지 않은" in s["rehearsal"]["note"]


@pytest.mark.parametrize("run", ["grn01", "grp01"])
def test_readiness_failure_never_starts_pressure_or_observation(tmp_path, monkeypatch, run):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO, ready=False, send_at=())
    s = w.run(run)
    assert s["outcome_status"] == "aborted" and "첫 정상 평가 대기" in s["outcome_reason"]
    assert "inject" not in w.injector.calls and "cleanup" in w.injector.calls
    assert s["detector_readiness"]["ready_at_utc"] is None
    assert s["rehearsal"]["connection_evaluation_recording_ok"] is False


# ---------------- act ----------------
def test_act_verified_promotion_links_service_check_for_same_target_key(tmp_path, monkeypatch):
    jobs = []
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, service=lambda job: jobs.append(job) or {"ok": True})
    s = w.run("gap01", approval_path=w.approval())
    wt = s["watcher"]
    assert len(w.promotes) == 1 and wt["guard_starts"] == 1 and wt["guard_result"]["result"] == "verified"
    tk = wt["action"]["target_key"]
    assert jobs[0]["target_key"] == tk and w.policy.snapshot()["actions"][tk]["status"] == "verified"
    assert w.overrides == [420] and s["scale_down_delay_after"] == 30 and s["scale_down_delay_restore"]["restored"]
    assert s["outcome_status"] == "completed" and s["cleanup_errors"] == []
    assert w.injector.calls.index("prepare") < w.injector.calls.index("inject")


@pytest.mark.parametrize("kind", ["service_failed", "deadline"])
def test_act_service_failure_or_deadline_stops_detector_cleans_up_and_blocks(tmp_path, monkeypatch, kind):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, service=lambda job: {"ok": False},
              defer_service=(kind == "deadline"))
    s = w.run("gap01", approval_path=w.approval())
    gres = s["watcher"]["guard_result"]
    assert gres["runner_action"] == "stopped" and w.detector.stops >= 1
    assert gres["result"] == ("verified_service_failed" if kind == "service_failed" else "timeout_pending")
    assert s["outcome_status"] == "aborted" and "서비스 확인이 정상 아님" in s["outcome_reason"]
    assert "cleanup" in w.injector.calls and len(w.promotes) == 1
    assert w.delay == 420 and w.restores == []  # D6: 서비스 실패·기한 초과면 자동 원복하지 않는다
    assert any(e.startswith("scale_down_delay_restore: restore_held") for e in s["cleanup_errors"])
    assert any(e.startswith("policy_unresolved_actions") for e in s["cleanup_errors"])
    assert list(w.results.glob("running-*.json"))  # 실행 중 표시 보존 → 다음 실행 차단
    with pytest.raises(SystemExit, match="실행 중 표시"):
        w.run("gap02", approval_path=w.approval())  # 새 실행 ID로도 우회 불가


def test_duplicate_response_does_not_repeat_wait_or_promotion(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, resend=True)
    s = w.run("gap01", approval_path=w.approval())
    assert w.detector.posts == 2 and len(w.promotes) == 1
    assert s["watcher"]["guard_starts"] == 1 and s["watcher"]["duplicates"] == 1
    assert s["evidence_counts"]["send_results"] == {"promoted_verified": 1, "duplicate_delivery": 1}


def test_context_clear_failure_is_unresolved_block(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO)

    def boom(run_id):
        raise RuntimeError("HTTP 503")
    w.policy_client.clear_context = boom
    s = w.run("grn01")
    assert any(e.startswith("policy_context_clear") for e in s["cleanup_errors"])
    assert list(w.results.glob("running-*.json"))


def test_delay_restore_failure_is_unresolved_block(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, restore_ok=False)
    s = w.run("gap01", approval_path=w.approval())
    assert any(e.startswith("scale_down_delay_restore") for e in s["cleanup_errors"])
    assert any(e.startswith("scale_down_delay_after=420") for e in s["cleanup_errors"])
    assert list(w.results.glob("running-*.json"))


@pytest.mark.parametrize("over,msg", [({"status": "draft"}, "status"), ({"timing_source": "rehearsal"}, "timing_source"),
                                      ({"service_wait_deadline_sec": 30}, "service_wait"),
                                      ({"timing": {**TIMING, "max_signal_age_sec": float("nan")}}, "max_signal_age"),
                                      ({"clock_measurement_file": "missing.json"}, "clock_measurement")])
def test_act_refuses_without_final_approval(tmp_path, monkeypatch, over, msg):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT)
    with pytest.raises(SystemExit, match=msg):
        w.run("gap01", approval_path=w.approval(**over))
    assert w.injector.calls == [] and w.policy.snapshot()["context"] is None


def test_act_refuses_without_approval_or_with_policy_timing_mismatch(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT)
    with pytest.raises(SystemExit, match="act-approval"):
        w.run("gap01")
    with pytest.raises(SystemExit, match="policy_timing"):
        w.run("gap01", approval_path=w.approval(timing={**TIMING, "max_signal_age_sec": 20.0}))
    assert w.injector.calls == [] and w.policy.snapshot()["context"] is None


def test_mode_must_match_run_id(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO)
    with pytest.raises(SystemExit, match="모드"):
        gp.run_guarded("gap01", mode=gp.MODE_RO, deps=w.deps(), policy_url="http://p", results_dir=w.results)


def test_record_only_run_against_act_policy_is_refused(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT)
    with pytest.raises(SystemExit, match="policy_mode=act"):
        gp.run_guarded("grn01", mode=gp.MODE_RO, deps=w.deps(), policy_url="http://p", results_dir=w.results)


def test_previous_unresolved_policy_action_blocks_new_run_and_new_state(tmp_path, monkeypatch):
    seed = [{"event": "intent", "target_key": "old-target", "run_id": "old-run"}]  # 결과 없이 끝난 이전 조치(원장)
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, ledger_seed=seed)
    with pytest.raises(SystemExit, match="policy_unresolved_actions"):
        w.run("gap01", approval_path=w.approval())
    assert w.injector.calls == [] and w.promotes == []


def test_delay_not_base_value_refuses_start(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO)
    w.delay = 420
    with pytest.raises(SystemExit, match="scaleDownDelaySeconds=420"):
        w.run("grn01")
    assert w.injector.calls == []


# ---------------- 조치 감지 단위 ----------------
def _watcher(tmp_path, mode, recs, state=None):
    p = tmp_path / "ev.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return gp.ActionWatcher(p, mode=mode, run_id="r1", get_state=lambda: state or {}, deadline_sec=80,
                            thread_runner=lambda fn, *a: fn(*a), guard_fn=lambda *a, **k: {"runner_action": "continue"})


def _sr(outcome, tk="t1", **kw):
    return {"record_type": "c1_policy_send_result", "policy_outcome": outcome, "target_key": tk, **kw}


def test_watcher_record_only_promotion_is_harness_error(tmp_path):
    with pytest.raises(HarnessCorrupted):
        _watcher(tmp_path, gp.MODE_RO, [_sr("promoted_verified")]).check()


def test_watcher_second_target_violates_single_action(tmp_path):
    w = _watcher(tmp_path, gp.MODE_ACT, [_sr("promoted_verified"), _sr("promoted_verified", tk="t2")],
                 state={"actions": {"t1": {"run_id": "r1"}}})
    with pytest.raises(HarnessCorrupted, match="두 번째"):
        w.check()


@pytest.mark.parametrize("outcome", ["promotion_unknown", "promoted_unverified"])
def test_watcher_unknown_or_unverified_stops_without_wait(tmp_path, outcome):
    w = _watcher(tmp_path, gp.MODE_ACT, [_sr(outcome)])
    with pytest.raises(Exception, match="검증 성공이 아님"):
        w.check()
    assert w.guard_starts == 0


def test_watcher_target_key_must_match_policy_action_run(tmp_path):
    w = _watcher(tmp_path, gp.MODE_ACT, [_sr("promoted_verified")], state={"actions": {"t1": {"run_id": "other"}}})
    with pytest.raises(HarnessCorrupted, match="불일치"):
        w.check()
    assert w.guard_starts == 0


def test_session_check_requires_live_helpers_and_endpoints(tmp_path):
    f = tmp_path / "s.json"
    f.write_text("﻿" + json.dumps({"keep_awake": 1, "pf_prometheus": 2, "pf_policy": 3,
                                        "prometheus_url": "http://127.0.0.1:9090", "policy_url": "http://127.0.0.1:18090"}),
                 encoding="utf-8")  # PowerShell Out-File BOM
    ok = gp.check_session(f, pid_alive=lambda p: True, http_ok=lambda u: True)
    assert ok["ok"] is True
    bad = gp.check_session(f, pid_alive=lambda p: p != 2, http_ok=lambda u: "9090" not in u)
    assert bad["ok"] is False and "pf_prometheus_dead(2)" in bad["problems"]
    assert any("prometheus_unreachable" in x for x in bad["problems"])
    assert gp.check_session(None)["ok"] is False


def test_promotion_unknown_stops_without_wait_or_repromotion_and_blocks(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, promote_verified=None)
    s = w.run("gap01", approval_path=w.approval())
    assert len(w.promotes) == 1 and s["watcher"]["guard_starts"] == 0
    assert s["outcome_status"] == "aborted" and "검증 성공이 아님(promotion_unknown)" in s["outcome_reason"]
    assert "cleanup" in w.injector.calls and w.delay == 420 and w.restores == []  # D6: 결과 불명이면 원복 보류
    assert any(e.startswith("policy_unresolved_actions") for e in s["cleanup_errors"])
    assert w.detector.alive is False  # 실제 탐지기 규칙: 결과 불명 → 종료 코드 6


def test_detector_exit_mid_run_aborts_and_cleans_up(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO, send_at=(), die_at=100.0)
    s = w.run("grp01")
    assert s["outcome_status"] == "aborted" and "예상 밖 종료" in s["outcome_reason"]
    assert "cleanup" in w.injector.calls and "stop" in w.prober.calls
    assert s["rehearsal"]["connection_evaluation_recording_ok"] is False and s["cleanup_errors"] == []



# ---------------- 기존 recovery-policy 격리 ----------------
def test_rehearsal_refused_while_old_policy_can_promote(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO,
              old_policy=lambda: {"isolated": False, "reasons": ["deployment_replicas=1", "endpoints=1+0"]})
    with pytest.raises(SystemExit, match="old_recovery_policy_not_isolated"):
        w.run("grn01")
    assert w.injector.calls == [] and w.policy.snapshot()["context"] is None  # preview 준비 전 거부


def test_isolation_check_error_is_not_isolated(tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("API timeout")
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO, old_policy=boom)
    with pytest.raises(SystemExit, match="check_error"):
        w.run("grn01")
    assert w.injector.calls == []


def test_isolation_rechecked_right_before_preview_prep(tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky():  # 사전검사는 통과, preview 준비 직전 확인에서 열림(예: GitOps가 되돌림)
        calls["n"] += 1
        return {"isolated": calls["n"] == 1, "reasons": [] if calls["n"] == 1 else ["deployment_replicas=1"]}
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO, old_policy=flaky)
    s = w.run("grp01")
    assert "prepare" not in w.injector.calls and "inject" not in w.injector.calls
    assert s["outcome_status"] in ("aborted", "harness_error")
    assert any("not_isolated_at_end" in e for e in s["cleanup_errors"])


def test_isolation_lost_mid_run_aborts_and_blocks(tmp_path, monkeypatch):
    calls = {"n": 0}

    def reopened():
        calls["n"] += 1
        ok = calls["n"] < 20
        return {"isolated": ok, "reasons": [] if ok else ["pods_running=1"]}
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO, old_policy=reopened)
    s = w.run("grp01")
    assert s["outcome_status"] == "aborted" and "승격 경로가 열림" in s["outcome_reason"]
    assert "cleanup" in w.injector.calls and w.promotes == []
    assert any("not_isolated_at_end" in e for e in s["cleanup_errors"])
    assert list(w.results.glob("running-*.json"))


# ---------------- 관찰 종료와 서비스 확인 수명 분리 ----------------
def _ext_checks(w, s):
    ext = s["service_extensions"]
    assert ext and all(e["counted_in_main_metrics"] is False for e in ext)
    assert ext[0]["started_at_utc"] >= s["observation_ended_at_utc"]  # 관찰 종료 시각은 그대로, 연장은 그 뒤
    assert w.prober.stop_time >= ext[-1]["ended_mono"]  # 요청 부하는 연장이 끝난 뒤에야 멈춤
    assert not [t for t in w.prober.refresh_times if ext[0]["started_mono"] < t <= ext[-1]["ended_mono"]]  # 원자료 미갱신
    assert all(ext[0]["probe_alive"]) and ext[0]["safety_checks"] >= 10
    return ext


def test_promotion_just_before_observation_end_extends_until_service_result(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, send_at=(599.0,), guard_delay=70.0)
    s = w.run("gap01", approval_path=w.approval())
    ext = _ext_checks(w, s)
    assert ext[0]["phase"] == "before_detector_stop" and ext[0]["ended_mono"] - ext[0]["started_mono"] >= 60
    ev = s["watcher"]["promotion_events"][0]
    assert ev["source"] == "after_last_safety_check"  # 마지막 안전검사 이후 도착한 응답도 잡음
    assert s["watcher"]["guard_result"]["result"] == "verified" and s["watcher"]["guard_starts"] == 1
    assert s["outcome_status"] == "completed" and s["cleanup_errors"] == [] and len(w.promotes) == 1
    assert s["scale_down_delay_after"] == 30


def test_response_lost_after_last_check_is_adopted_from_policy_state(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, send_at=(599.0,), guard_delay=70.0, lose_response=True,
              service_delay=70.0)
    s = w.run("gap01", approval_path=w.approval())
    ext = _ext_checks(w, s)
    assert ext[0]["phase"] == "after_detector_stop"
    assert s["watcher"]["action"]["source"] == "policy_state_after_detector_stop"
    assert s["watcher"]["guard_result"]["result"] == "verified" and s["cleanup_errors"] == []


@pytest.mark.parametrize("kind", ["service_failed", "deadline"])
def test_service_failure_or_timeout_during_extension_blocks(tmp_path, monkeypatch, kind):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, send_at=(599.0,), guard_delay=70.0,
              service=lambda job: {"ok": False}, defer_service=(kind == "deadline"))
    s = w.run("gap01", approval_path=w.approval())
    _ext_checks(w, s)
    g = s["watcher"]["guard_result"]
    assert g["runner_action"] == "stopped"
    assert g["result"] == ("verified_service_failed" if kind == "service_failed" else "timeout_pending")
    assert any(e.startswith("policy_unresolved_actions") for e in s["cleanup_errors"])
    assert s["scale_down_delay_after"] == 420 and w.restores == []  # D6: 서비스 실패·기한 초과면 원복 보류
    assert w.policy.snapshot()["context"] is None  # context 해제는 그대로
    assert list(w.results.glob("running-*.json"))


def test_record_only_late_promotion_in_policy_state_is_flagged(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO, send_at=())
    w.policy.state["actions"]["x"] = {"status": "verified", "run_id": gp.run_id_for("grn01")}  # 비정상 상태 흉내
    s = w.run("grn01")
    assert any("record_only인데 정책 상태에 조치" in e for e in s["cleanup_errors"])


# ---------------- 실제 격리 판정(opi.assess)으로 연결 확인 ----------------
def _real_isolation_check(k):
    import old_policy_isolation as opi
    return lambda: opi.assess(opi.read_state(apps=k, core=k, custom=k))


def test_terminating_old_policy_pod_refuses_before_preview(tmp_path, monkeypatch):
    from test_old_policy_isolation import FakeK8s
    k = FakeK8s(replicas=0, terminating=1)  # replicas 0·endpoints 0이어도 삭제 중 Pod가 남아 있음
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO, old_policy=_real_isolation_check(k))
    with pytest.raises(SystemExit, match="pods_present=1"):
        w.run("grn01")
    assert w.injector.calls == [] and w.policy.snapshot()["context"] is None


def test_unverifiable_gitops_refuses_before_preview(tmp_path, monkeypatch):
    from test_old_policy_isolation import FakeK8s
    k = FakeK8s(replicas=0, labels={"kustomize.toolkit.fluxcd.io/name": "apps"})
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO, old_policy=_real_isolation_check(k))
    with pytest.raises(SystemExit, match="Flux"):
        w.run("grn01")
    assert w.injector.calls == []


def test_isolation_read_timeout_mid_run_aborts_cleans_up_and_blocks(tmp_path, monkeypatch):
    from test_old_policy_isolation import FakeK8s
    k = FakeK8s(replicas=0)
    real = _real_isolation_check(k)
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        if calls["n"] == 20:
            k.fail = "deployment"  # 관찰 중 조회 시간 초과
        return real()
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO, old_policy=check)
    s = w.run("grp01")
    assert s["outcome_status"] == "aborted" and "TimeoutError" in s["outcome_reason"]
    assert "cleanup" in w.injector.calls and w.promotes == []
    assert any("not_isolated_at_end" in e for e in s["cleanup_errors"])
    assert list(w.results.glob("running-*.json"))  # 다음 실행 차단


def test_summary_preserves_trial_outcome_and_stress_delivery_record(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO)
    s = w.run("grp01")
    assert s["trial_outcome"]["status"] == "completed" and "t_injection_observed_utc" in s["trial_outcome"]
    assert s["stress_delivery_record"] == {"planned_cr_names": []}


# ---------------- SSH 디스크 점검 근거 보존(guarded 경로 전용, 판정 불변) ----------------
def _ssh_evidence_deps(w, tmp_path, modes):
    import run_detector_switch_ab_trial as ab
    import test_ssh_check_evidence as tse
    (tmp_path / "fake").mkdir()
    probe = tse.collector(tmp_path / "fake", modes)  # 가짜 ssh 스크립트·호출 순서별 응답 계획만 빌린다
    healthy = {"Ready": "True", "MemoryPressure": "False", "DiskPressure": "False", "PIDPressure": "False"}

    def run_guard_check(record, injector, state, **kw):  # 실제 ab.check_run_guard(배치·압박 전 검사는 통과 상태로 채움)
        record.update(preview_verified=True, preview_actual_node="sj-worker2", pre_stress={"ok": True})
        return ab.check_run_guard(record, injector, state, mem_fn=lambda: {n: 8 * 2 ** 30 for n in ab.WATCH_NODES},
                                  cond_fn=lambda n: healthy, monotonic_fn=w.clock.mono, **kw)

    def preflight(pol, disk_check_fn):  # 실제 rt.run_preflight(디스크 외 항목은 가짜)
        return rt.run_preflight(disk_check_fn=disk_check_fn, active_pods_fn=lambda: [{"name": "p"}],
                                metrics_text_fn=lambda n: f"{rt.REQUEST_COUNTER_METRIC} 5", policy_healthy_fn=pol.healthz)
    return w.deps(make_ssh_evidence=lambda d: gp.sce.SshCheckEvidence(d, ssh_prefix=probe.ssh_prefix, timeout_sec=2.0),
                  run_guard_check=run_guard_check, preflight=preflight,
                  preflight_nodes=lambda: ["sj-control", "sj-worker", "sj-worker2"])


def test_ssh_check_normal_runs_and_links_evidence(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO)
    s = w.run("grp01", deps=_ssh_evidence_deps(w, tmp_path, ["normal"]))
    assert s["outcome_status"] == "completed" and s["cleanup_errors"] == []
    ev = s["ssh_check_evidence"]
    assert ev["checks"] >= 3 + 2 * 5 and set(ev["by_observed"]) == {"exited_after_completion_marker"}
    assert ev["failed_checks"] == [] and s["run_guard"]["disk_checks"] >= 5
    pre = json.loads((w.results / s["preflight_file"]).read_text(encoding="utf-8"))
    assert pre["disk"]["ok"] is True and pre["ssh_check_evidence"]["checks"] == 3
    assert Path(ev["index"]).exists() and Path(ev["dir"]).name == f"ssh-evidence-{s['run_id']}"


def test_ssh_check_preflight_timeout_refuses_start_and_keeps_evidence(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO)
    with pytest.raises(SystemExit, match="disk_health"):
        w.run("grp01", deps=_ssh_evidence_deps(w, tmp_path, ["normal", "normal", "done_no_exit"]))
    assert "prepare" not in w.injector.calls and w.promotes == []
    pre = json.loads(next(w.results.glob("preflight-*.json")).read_text(encoding="utf-8"))
    fc = pre["ssh_check_evidence"]["failed_checks"]
    assert [(x["node"], x["observed"], x["last_stage"]) for x in fc] == [
        ("sj-worker2", "timeout_after_completion_marker", "T2")]  # 완료 표시가 있어도 종료 안 하면 통과 안 함


def test_ssh_check_timeout_mid_run_aborts_cleans_up_with_partial_evidence(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO)
    # 사전검사 3노드(정상) → 실행 중 점검 sj-worker·sj-worker2 2회 정상 → 3번째 sj-worker2에서 부분 출력 후 시간 초과
    modes = ["normal"] * 3 + ["normal"] * 4 + ["normal", "partial_timeout"]
    s = w.run("grp01", deps=_ssh_evidence_deps(w, tmp_path, modes))
    assert s["outcome_status"] == "aborted" and "디스크 검사 실패" in s["outcome_reason"]
    assert "TimeoutExpired" in s["outcome_reason"] and "timeout_after_start_no_completion_marker" in s["outcome_reason"]
    assert "cleanup" in w.injector.calls and w.promotes == [] and w.prober.stop_time is not None
    assert s["run_guard"]["disk_checks"] == 3 and s["run_guard"]["disk_failure"]
    fc = s["ssh_check_evidence"]["failed_checks"]
    assert len(fc) == 1 and fc[0]["node"] == "sj-worker2" and fc[0]["last_stage"] == "T1" and fc[0]["stdout_bytes"] > 0
    part = (Path(s["ssh_check_evidence"]["dir"]) / fc[0]["stdout_file"]).read_text(encoding="utf-8")
    assert "---DMESG" in part  # 부분 출력은 근거 파일에만 - 판정은 실패


# ---------------- D5: 승격 시도 뒤 자동 preview abort 방지 (정책 상태·원장·탐지기 기록·실제 Rollout) ----------------
def _held(s):
    return [e for e in s["cleanup_errors"] if e.startswith("preview_cleanup_held")]


@pytest.mark.parametrize("switched", [True, False])
def test_promotion_timeout_holds_preview_cleanup_even_if_selector_changed(tmp_path, monkeypatch, switched):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, promote_verified=None, switch_on_unknown=switched,
              wrap_preview=True)
    s = w.run("gap01", approval_path=w.approval())
    assert len(w.promotes) == 1 and w.preview_cleanups == []  # 기존 abort 정리를 부르지 않음
    assert "cleanup" in w.injector.calls and "stop" in w.prober.calls  # 압박 자원 제거·probe 정리는 계속
    held = _held(s)[0]
    assert "promotion_unknown" in held and ("live_selector_changed" in held) is switched
    assert s["preview_cleanup_gate"][-1]["action"] == "held" and s["preview_cleanup_gate"][-1]["remaining"]["live"]
    assert w.delay == 420 and w.restores == []  # 유예도 자동으로 줄이지 않음
    assert list(w.results.glob("running-*.json"))
    with pytest.raises(SystemExit, match="실행 중 표시"):
        w.run("gap02", approval_path=w.approval())


def test_policy_state_unreadable_holds_preview_cleanup_but_other_cleanup_runs(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO, wrap_preview=True)
    real_state, down = w.policy_client.state, {"v": False}

    def stress_cleanup():  # 원래 cleanup(압박 제거) - 이 시점부터 정책 상태 조회가 실패한다
        w.injector.calls.append("cleanup")
        down["v"] = True
    w.injector.cleanup = stress_cleanup

    def state():
        if down["v"]:
            raise RuntimeError("HTTP 503")
        return real_state()
    w.policy_client.state = state
    s = w.run("grp01")
    assert w.preview_cleanups == [] and "cleanup" in w.injector.calls and "stop" in w.prober.calls
    assert "policy_state_unreadable" in _held(s)[0]
    with pytest.raises(SystemExit, match="실행 중 표시"):
        w.run("grp02")


@pytest.mark.parametrize("mode,run,send_at", [(gp.MODE_RO, "grp01", (50.0,)), (gp.MODE_ACT, "gap01", ())])
def test_preview_cleanup_only_when_not_promoted_and_target_identical(tmp_path, monkeypatch, mode, run, send_at):
    w = World(tmp_path, monkeypatch, mode=mode, send_at=send_at, wrap_preview=True)
    s = w.run(run, approval_path=w.approval() if mode == gp.MODE_ACT else None)
    assert len(w.preview_cleanups) == 1 and w.preview_cleanups[0]["created_pod_hash"] == "bbb222"
    assert s["preview_cleanup_gate"][-1]["action"] == "cleanup_allowed" and s["cleanup_errors"] == []
    if mode == gp.MODE_ACT:  # 미승격 확인 → 기존 원복(즉시 30)
        assert w.promotes == [] and s["scale_down_delay_after"] == 30
        assert s["scale_down_delay_restore"]["path"] == "not_promoted"


@pytest.mark.parametrize("case", ["identity_changed", "send_without_result"])
def test_preview_cleanup_held_when_target_identity_or_send_outcome_unconfirmed(tmp_path, monkeypatch, case):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO, wrap_preview=True, lose_response=(case == "send_without_result"))
    if case == "identity_changed":
        w.cluster.current = "ccc333"  # 준비한 preview와 다른 리비전 - 정리 대상 동일성 미확인
    s = w.run("grp01")
    assert w.preview_cleanups == [] and "cleanup" in w.injector.calls
    assert ("cleanup_target_identity_unconfirmed" if case == "identity_changed" else "attempt_without_result") in _held(s)[0]


# ---------------- D6: 확인된 정상 전환만 구 RS 0 실측 뒤 30 원복 ----------------
def test_verified_switch_waits_for_old_rs_zero_after_420s_then_restores_30(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, send_at=(400.0,), wrap_preview=True)
    s = w.run("gap01", approval_path=w.approval())
    r = s["scale_down_delay_restore"]
    assert r["path"] == "promoted_service_ok" and r["restored"] is True and s["scale_down_delay_after"] == 30
    assert r["wait"]["nonzero_samples"] > 0  # 420초 전에는 구 RS가 남아 있음을 실측
    assert len(w.restores) == 1 and w.restores[0]["old_zero"] is True
    assert w.restores[0]["t"] >= w.cluster.switched_at + 420  # 시간만이 아니라 구 RS 0을 본 뒤 원복
    assert s["preview_cleanup_gate"][-1]["action"] == "skipped_preview_is_new_active" and w.preview_cleanups == []
    assert s["final_promotion_assessment"]["verdict"] == "promoted_service_ok" and s["cleanup_errors"] == []
    assert r["wait"]["safety_checks"] >= r["wait"]["polls"] >= 1


@pytest.mark.parametrize("case", ["target_changed", "deadline", "read_failed", "safety_failed"])
def test_wait_problems_hold_restore_and_block(tmp_path, monkeypatch, case):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, send_at=(400.0,))
    calls = {"n": 0}
    base_rs = w.cluster.list_rs

    def rs():
        calls["n"] += 1
        if case == "target_changed" and calls["n"] == 2:
            w.cluster.active = "ccc333"  # 대기 중 다른 주체가 바꿈
        if case == "read_failed" and calls["n"] >= 2:
            raise TimeoutError("rs list timeout")
        return base_rs()
    w.cluster.list_rs = rs
    if case == "deadline":
        w.cluster.old_zero = lambda: False  # 상한까지 구 RS가 0이 안 됨
    if case == "safety_failed":  # 원복 대기 중(첫 RS 조회 뒤)에 기존 정책 격리가 풀림
        w.old_policy = lambda: {"isolated": calls["n"] == 0, "reasons": [] if calls["n"] == 0 else ["pods_running=1"]}
    s = w.run("gap01", approval_path=w.approval())
    r = s["scale_down_delay_restore"]
    want = {"target_changed": "target_changed_during_wait", "deadline": "old_rs_not_zero_by_deadline",
            "read_failed": "state_read_failed_during_wait", "safety_failed": "safety_failed_during_wait"}[case]
    assert r["restored"] is False and r["held"] is True and want in r["error"], r
    assert w.restores == [] and w.delay == 420  # 유예를 자동으로 줄이지 않음
    assert any(e.startswith("scale_down_delay_restore") for e in s["cleanup_errors"])
    assert list(w.results.glob("running-*.json"))



# ---------------- Codex 검토 반영 (D5·D6 보완) ----------------
def test_not_promoted_restore_rechecks_selector_right_before_patch(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, send_at=())
    w.before_patch = lambda: setattr(w.cluster, "active", "ccc333")  # 판정 뒤·patch 직전에 다른 주체가 전환
    s = w.run("gap01", approval_path=w.approval())
    r = s["scale_down_delay_restore"]
    assert r["path"] == "not_promoted" and r["restored"] is False and "not_promoted_view_changed_before_patch" in r["error"]
    assert w.delay == 420 and list(w.results.glob("running-*.json"))


def test_promoted_restore_rechecks_target_right_before_patch(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, send_at=(400.0,))
    w.before_patch = lambda: setattr(w.cluster, "current", "ddd444")  # selector는 같고 새 리비전이 생김
    s = w.run("gap01", approval_path=w.approval())
    r = s["scale_down_delay_restore"]
    assert r["restored"] is False and "currentPodHash=ddd444" in r["error"] and w.delay == 420


def test_policy_state_unavailable_during_wait_holds(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, send_at=(400.0,))
    real, n = w.policy_client.state, {"rs": 0}
    base_rs = w.cluster.list_rs

    def rs():
        n["rs"] += 1
        return base_rs()
    w.cluster.list_rs = rs
    w.policy_client.state = lambda: {"state_available": False, "reason": "state_unreadable"} if n["rs"] else real()
    s = w.run("gap01", approval_path=w.approval())
    r = s["scale_down_delay_restore"]
    assert r["held"] and "policy_state_unavailable" in r["error"] and w.restores == [] and w.delay == 420


def test_interrupt_during_wait_is_held_and_summary_still_written(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, send_at=(400.0,))
    n = {"rs": 0}
    base_rs, base_sleep = w.cluster.list_rs, w.clock.sleep

    def rs():
        n["rs"] += 1
        return base_rs()

    def sleep(sec):
        if n["rs"]:
            raise KeyboardInterrupt("signal 21")  # 원복 대기 중 중단 신호
        base_sleep(sec)
    w.cluster.list_rs = rs
    s = w.run("gap01", approval_path=w.approval(), deps=w.deps(sleep_fn=sleep))
    r = s["scale_down_delay_restore"]
    assert r["held"] and "KeyboardInterrupt" in r["error"] and w.delay == 420
    assert (w.results / f"summary-{s['run_id']}.json").exists() and list(w.results.glob("running-*.json"))


def test_lost_response_with_already_verified_action_still_restores_after_old_rs_zero(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_ACT, send_at=(400.0,), lose_response=True)
    s = w.run("gap01", approval_path=w.approval())
    assert s["watcher"]["action"]["source"] == "policy_state_after_detector_stop" and s["watcher"]["guard_starts"] == 0
    r = s["scale_down_delay_restore"]
    assert r["path"] == "promoted_service_ok" and r["restored"] is True and w.delay == 30, r
    assert w.restores[0]["old_zero"] is True and s["cleanup_errors"] == []



def test_run_id_reuse_refused_even_after_summary_archived(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO)
    s = w.run("grn01")
    arc = tmp_path / "archive"
    arc.mkdir()
    (w.results / f"summary-{s['run_id']}.json").rename(arc / "summary.json")  # 보관 절차로 요약을 옮긴 상태
    with pytest.raises(SystemExit, match="실행 ID 재사용 금지"):
        w.run("grn01")
    w.run("grn02")  # 다른 ID는 시작 가능


# ---------------- 전환/무전환 비교(2026-10-07 계약): ID·폴더 분리, 쌍 전제, 승인 범위, 조건 기록 ----------------
TARGET = {"name": "vllm-serving-aaa111-x", "uid": "up", "node_name": "sj-worker", "baseline_cpu_cores": 0.02}


def _ab_pair(tmp_path, monkeypatch, *, b_node="sj-worker", send_at=(50.0,)):
    wa = World(tmp_path / "a", monkeypatch, mode=gp.MODE_RO, send_at=send_at)
    wb = World(tmp_path / "b", monkeypatch, mode=gp.MODE_ACT, send_at=send_at)
    wb.results = wa.results  # 같은 비교 폴더
    for w in (wa, wb):
        w.injector.get_stress_delivery_record = lambda: {"planned_cr_names": [], "target": dict(TARGET)}
    wb.injector.get_real_promotion_diagnostic_exception_info = lambda: {"prediction": {"active_node": b_node}}
    return wa, wb


def test_ab_ids_plan_and_default_folder_are_separate_from_pilot():
    assert gp.run_id_for("ab01a") == "guarded_pressure_record_only-ab01a-capstone-guarded-ab-v1-20261007"
    assert gp.run_id_for("ab01b").startswith("guarded_pressure_act-ab01b-") and gp.RUNS["ab15b"][1] == gp.MODE_ACT
    assert gp.AB_RESULTS_DIR != gp.RESULTS_DIR and gp.PLAN_ID not in gp.run_id_for("ab00a")
    assert len({gp.RUNS[r][2] for r in gp.RUNS}) == len(gp.RUNS)  # probe 반복 번호 겹침 없음


def test_ab_pair_a_never_promotes_b_promotes_once_same_conditions(tmp_path, monkeypatch):
    wa, wb = _ab_pair(tmp_path, monkeypatch)
    sa = wa.run("ab01a")
    sb = wb.run("ab01b", approval_path=wb.approval(runs=["ab01b"]))
    assert wa.promotes == [] and wa.delay == 30 and sa["comparison"]["arm"] == "A_no_switch"
    assert len(wb.promotes) == 1 and sb["comparison"]["arm"] == "B_if_switch" and sb["cleanup_errors"] == []
    assert sb["comparison"]["expected_start_active_node"] == sa["start_active_node"] == "sj-worker"
    ca, cb = (json.loads((wa.results / f"contract-{s['run_id']}.json").read_text(encoding="utf-8")) for s in (sa, sb))
    assert ca["conditions"] == cb["conditions"] and ca["observation_sec"] == cb["observation_sec"]
    assert ca["threshold"] == cb["threshold"] and ca["artifact_hashes"] == cb["artifact_hashes"]
    assert sa["comparison"]["counted_in_comparison"] is True and sa["cpu_raw_counter_samples"]
    assert ca["plan_id"] == gp.AB_PLAN_ID and ca["contract_doc"] == gp.AB_CONTRACT_DOC


def test_rehearsal_pair_is_never_counted(tmp_path, monkeypatch):
    wa, _ = _ab_pair(tmp_path, monkeypatch, send_at=())
    assert wa.run("ab00a")["comparison"]["counted_in_comparison"] is False


@pytest.mark.parametrize("case", ["no_a", "a_cleanup_error", "a_not_completed"])
def test_b_refused_without_clean_same_pair_a(tmp_path, monkeypatch, case):
    wa, wb = _ab_pair(tmp_path, monkeypatch)
    if case != "no_a":
        sa = wa.run("ab02a")
        p = wa.results / f"summary-{sa['run_id']}.json"
        a = json.loads(p.read_text(encoding="utf-8"))
        a.update({"cleanup_errors": ["x"]} if case == "a_cleanup_error" else {"outcome_status": "invalid"})
        p.write_text(json.dumps(a), encoding="utf-8")
    with pytest.raises(SystemExit, match="ab02b 거부"):
        wb.run("ab02b", approval_path=wb.approval(runs=["ab02b"]))
    assert wb.promotes == [] and wb.overrides == []


def test_b_refused_outside_approved_runs(tmp_path, monkeypatch):
    wa, wb = _ab_pair(tmp_path, monkeypatch)
    wa.run("ab03a")
    for runs in (None, ["ab02b", "ab04b"]):
        with pytest.raises(SystemExit, match="승인 범위 밖"):
            wb.run("ab03b", approval_path=wb.approval(**({"runs": runs} if runs else {})))
    assert wb.promotes == [] and not list(wb.results.glob("running-*ab03b*"))


def test_b_on_different_start_node_is_not_injected_or_promoted(tmp_path, monkeypatch):
    wa, wb = _ab_pair(tmp_path, monkeypatch, b_node="sj-worker2")
    wa.run("ab04a")
    sb = wb.run("ab04b", approval_path=wb.approval(runs=["ab04b"]))
    assert "inject" not in wb.injector.calls and wb.promotes == [] and wb.delay == 30
    assert "쌍 노드 불일치" in str(sb["outcome_reason"]), sb["outcome_reason"]


def test_cpu_raw_counter_failure_is_recorded_not_fatal(tmp_path, monkeypatch):
    wa, _ = _ab_pair(tmp_path, monkeypatch, send_at=())
    s = wa.run("ab05a", deps=wa.deps(cpu_raw_counter=lambda pod, t0: (_ for _ in ()).throw(TimeoutError("prom"))))
    assert s["cpu_raw_counter_samples"] is None and "TimeoutError" in s["cpu_raw_counter_error"]
    assert s["cleanup_errors"] == []


# ---------------- 계약 r2: canonical 시작 검사·미승격 canonical 복원 routing·ab90 rehearsal ----------------
START_POD = ("vllm-serving-aaa111-x", "up")


def _r2_pair(tmp_path, monkeypatch, *, send_at_b=(50.0,)):
    wa = World(tmp_path / "a", monkeypatch, mode=gp.MODE_RO, send_at=(), wrap_preview=True)
    wb = World(tmp_path / "b", monkeypatch, mode=gp.MODE_ACT, send_at=send_at_b, wrap_preview=True)
    wb.results = wa.results
    return wa, wb


def test_r2_a_unpromoted_cleanup_restores_canonical_and_records_contract(tmp_path, monkeypatch):
    wa, _ = _r2_pair(tmp_path, monkeypatch)
    s = wa.run("ab90a")
    assert wa.restore_calls == [("bbb222", START_POD)] and wa.promotes == [] and s["cleanup_errors"] == []
    assert s["canonical_end"]["ok"] is True and s["comparison"]["counted_in_comparison"] is False
    assert s["preview_cleanup_gate"][-1]["canonical_restore"]["ok"] is True
    c = json.loads((wa.results / f"contract-{s['run_id']}.json").read_text(encoding="utf-8"))
    assert c["contract_doc"] == gp.AB_CONTRACT_DOC and c["contract_doc"].endswith("-r2-20261007.md")
    assert c["conditions"]["start_state_rule"] == "canonical_healthy_r2" and c["conditions"]["detector_rule"]["consecutive"] == 3
    p = json.loads((wa.results / s["preflight_file"]).read_text(encoding="utf-8"))
    assert p["canonical_start"]["problems"] == [] and tuple(p["canonical_start"]["start_pod"]) == START_POD


def test_r2_b_promoted_uses_d6_never_canonical_restore(tmp_path, monkeypatch):
    wa, wb = _r2_pair(tmp_path, monkeypatch)
    wa.run("ab90a")
    s = wb.run("ab90b", approval_path=wb.approval(runs=["ab90b"]))
    assert len(wb.promotes) == 1 and wb.restore_calls == []
    assert s["scale_down_delay_restore"]["path"] == "promoted_service_ok" and wb.delay == 30 and s["cleanup_errors"] == []


def test_r2_b_without_signal_uses_unpromoted_restore_and_d6_not_promoted_path(tmp_path, monkeypatch):
    wa, wb = _r2_pair(tmp_path, monkeypatch, send_at_b=())
    wa.run("ab90a")
    s = wb.run("ab90b", approval_path=wb.approval(runs=["ab90b"]))
    assert wb.promotes == [] and wb.restore_calls == [("bbb222", START_POD)]
    assert s["scale_down_delay_restore"]["path"] == "not_promoted" and wb.delay == 30 and s["cleanup_errors"] == []


def test_r2_restore_failure_keeps_running_marker_and_blocks_next_arm(tmp_path, monkeypatch):
    wa, wb = _r2_pair(tmp_path, monkeypatch)
    wa.restore_result = {"ok": False, "problems": ["new_rs: ['zzz']"]}
    s = wa.run("ab90a")
    assert any(e.startswith("canonical_restore_failed") for e in s["cleanup_errors"])
    assert list(wa.results.glob("running-*ab90a*"))
    with pytest.raises(SystemExit):
        wb.run("ab90b", approval_path=wb.approval(runs=["ab90b"]))
    assert wb.promotes == [] and wb.overrides == [] and "inject" not in wb.injector.calls


@pytest.mark.parametrize("change", [{"phase": "Degraded"}, {"abort": True}, {"current": "bbb222"}, {"delay": 420},
                                    {"svc_selectors": {"vllm-active": {"rollouts-pod-template-hash": "aaa111"},
                                                       "vllm-preview": {"rollouts-pod-template-hash": "bbb222"}}},
                                    {"rs": {"aaa111": {"desired": 1, "current": 1, "ready": 1},
                                            "bbb222": {"desired": 1, "current": 1, "ready": 0}}}])
def test_r2_non_canonical_start_is_refused_without_fixing(tmp_path, monkeypatch, change):
    wa, _ = _r2_pair(tmp_path, monkeypatch)
    wa.canon.update(change)
    with pytest.raises(SystemExit, match="canonical_start"):
        wa.run("ab90a")
    assert wa.injector.calls == [] and wa.restore_calls == [] and not list(wa.results.glob("running-*"))


def test_r2_unresolved_policy_action_refuses_start(tmp_path, monkeypatch):
    wa, _ = _r2_pair(tmp_path, monkeypatch)
    real = wa.policy_client.state
    wa.policy_client.state = lambda: {**real(), "unresolved": {"k": {"status": "promotion_unknown"}}}
    with pytest.raises(SystemExit, match="policy_unresolved"):
        wa.run("ab90a")
    assert wa.injector.calls == []


def test_r2_end_not_canonical_is_cleanup_error(tmp_path, monkeypatch):
    wa, _ = _r2_pair(tmp_path, monkeypatch)

    def restore(prep, pod):  # 복원은 ok라고 했지만 끝 상태가 canonical이 아님(읽기 확인이 잡아야 함)
        wa.canon["abort"] = True
        return {"ok": True, "problems": []}
    s = wa.run("ab90a", deps=wa.deps(canonical_restore=restore))
    assert any(e.startswith("canonical_end_not_reached") for e in s["cleanup_errors"]) and s["canonical_end"]["ok"] is False
    assert list(wa.results.glob("running-*ab90a*"))


def test_ab90_is_rehearsal_separate_and_not_reusable(tmp_path, monkeypatch):
    assert {"ab90a", "ab90b"} <= set(gp.RUNS) and gp.RUNS["ab90a"][1] == gp.MODE_RO and gp.RUNS["ab90b"][1] == gp.MODE_ACT
    ids = {gp.run_id_for(r) for r in ("ab90a", "ab90b", "ab00a", "ab01a", "ab01b")}
    assert len(ids) == 5 and "ab90" in gp.AB_REHEARSAL_PAIRS and "ab01" not in gp.AB_REHEARSAL_PAIRS
    assert len({gp.RUNS[r][2] for r in gp.RUNS}) == len(gp.RUNS)
    wa, _ = _r2_pair(tmp_path, monkeypatch)
    wa.run("ab90a")
    with pytest.raises(SystemExit, match="이미 결과가 있음|재사용 금지"):
        wa.run("ab90a")


def test_pilot_runs_unchanged_no_canonical_restore(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode=gp.MODE_RO, send_at=(), wrap_preview=True)
    s = w.run("grp01")
    assert w.restore_calls == [] and s.get("canonical_end") is None and "canonical_restore" not in s["preview_cleanup_gate"][-1]


# ---------------- 사전검사 기록 단계별 분리(2026-10-07 ab90b attempt1 덮어쓰기 뒤) ----------------
def _pf(w, run):
    return sorted(p.name for p in w.results.glob(f"preflight-{gp.run_id_for(run)}*.json"))


def test_preflight_only_then_run_keeps_both_files(tmp_path, monkeypatch):
    wa, _ = _r2_pair(tmp_path, monkeypatch)
    po = wa.run("ab90a", preflight_only=True)
    s = wa.run("ab90a")
    names = _pf(wa, "ab90a")
    assert len(names) == 2 and po["preflight_file"] in names and s["preflight_file"] in names
    assert "-preflight-only-" in po["preflight_file"] and "-run-" in s["preflight_file"]


def test_preflight_only_ok_then_run_preflight_fails_keeps_both(tmp_path, monkeypatch):
    wa, _ = _r2_pair(tmp_path, monkeypatch)
    po = wa.run("ab90a", preflight_only=True)
    first = (wa.results / po["preflight_file"]).read_text(encoding="utf-8")
    wa.canon["abort"] = True
    with pytest.raises(SystemExit, match="canonical_start"):
        wa.run("ab90a")
    names = _pf(wa, "ab90a")
    assert len(names) == 2 and (wa.results / po["preflight_file"]).read_text(encoding="utf-8") == first
    fail = json.loads((wa.results / next(n for n in names if "-run-" in n)).read_text(encoding="utf-8"))
    assert fail["ok"] is False and json.loads(first)["ok"] is True


def test_run_only_writes_one_run_preflight_named_in_summary(tmp_path, monkeypatch):
    wa, _ = _r2_pair(tmp_path, monkeypatch)
    s = wa.run("ab90a")
    assert _pf(wa, "ab90a") == [s["preflight_file"]] and s["preflight_file"].startswith(f"preflight-{s['run_id']}-run-")


def test_existing_and_legacy_preflight_files_are_never_overwritten(tmp_path, monkeypatch):
    wa, _ = _r2_pair(tmp_path, monkeypatch)
    wa.results.mkdir(parents=True, exist_ok=True)
    rid = gp.run_id_for("ab90a")
    legacy = wa.results / f"preflight-{rid}.json"
    older = wa.results / f"preflight-{rid}-run-20000101T000000000000Z.json"
    legacy.write_text('{"legacy": true}', encoding="utf-8")
    older.write_text('{"older": true}', encoding="utf-8")
    wa.run("ab90a", preflight_only=True)
    assert legacy.read_text(encoding="utf-8") == '{"legacy": true}' and older.read_text(encoding="utf-8") == '{"older": true}'
    assert len(_pf(wa, "ab90a")) == 3


def test_preflight_files_of_different_runs_do_not_collide(tmp_path, monkeypatch):
    wa, wb = _r2_pair(tmp_path, monkeypatch)
    sa = wa.run("ab90a")
    sb = wb.run("ab90b", approval_path=wb.approval(runs=["ab90b"]))
    assert sa["preflight_file"] != sb["preflight_file"] and _pf(wa, "ab90a") == [sa["preflight_file"]]
    assert _pf(wb, "ab90b") == [sb["preflight_file"]]
