#!/usr/bin/env python3
"""네트워크 지연 환경 Stage 1 압박 세션 본체·유효성 판정(2026-10-17, M1, DEVELOPMENT - 동결 전, 이 파일은 클러스터를 바꾸지 않는다).

원본 run_stage1.run_pressure(CPU 압박, 동결)의 흐름을 그대로 따르고 네트워크에서 달라지는 것만 바꾼다:
  - 주입기: 파일럿 네트워크 주입기(make_network_degrade_injector)를 CPU 주입기와 같은 두 겹(preview 준비·승격 진단)으로 감싼다.
    지연 설정 생성·삭제·적용 확인 함수를 DeliveryRecorder로 감싸 **어느 Pod에 어떤 설정을 만들었고 실제로 어떻게 저장됐는지** 증거를 남긴다.
    각 단계 설정에 duration(단계 시간 + 여유)을 더해 실행기가 죽어도 Chaos Mesh가 스스로 지연을 해제하게 한다(안전망).
  - 사전 점검: 원본 점검 + n2_preflight(PROFILE_STALE·11 s·남은 지연 설정·Pod 구성·net_probe 정상·정책 상태).
  - 정리 확인: 원본 + n2_cleanup.session_end_problems(지연 설정 잔여·probe 설정 유지·정책 상태·표식), NetworkChaos 소멸 확인.
  - 전달 증거: CPU 스트레스 전달 확인 대신 단계 4개 적용·설정 읽어보기·대상 Pod 1개·보호 Pod probe 시간(n2_pressure 3겹).
  - 유효성: 원본 gab.run_validity를 그대로 부르고 CPU 전용 V3만 네트워크 전달 확인(N3)으로 교체한다.
실제 클러스터 조회(RealNetLive)는 읽기 전용이며 get 명령만 쓴다(테스트가 확인)."""
import sys
sys.dont_write_bytecode = True
import copy  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.parse  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import n2_cleanup as cl  # noqa: E402
import n2_common as nc  # noqa: E402
import n2_preflight as pfm  # noqa: E402
import n2_pressure as pr  # noqa: E402

pc = nc.pc
NAMESPACE = "vllm-serving"
CR_DURATION_MARGIN_SEC = 60            # 단계 시간 + 여유 = Chaos Mesh 자동 해제(안전망). 실행기는 단계 끝에 명시적으로 삭제한다.
OBSERVATION_SEC = 600.0                # 원본 압박 관찰 시간(gp.PRESSURE_OBSERVATION_SEC)과 같다 - 363 s(Gate 2) + 복구 관찰
PROM_PROXY = "/api/v1/namespaces/monitoring/services/kube-prom-kube-prometheus-prometheus:9090/proxy/api/v1/"
PROBE_SERIES = ('sum by (pod)(rate(prober_probe_duration_seconds_sum{{namespace="vllm-serving",container="vllm",probe_type="Readiness",'
                'pod="{pod}"}}[60s]))/sum by (pod)(rate(prober_probe_duration_seconds_count{{namespace="vllm-serving",container="vllm",'
                'probe_type="Readiness",pod="{pod}"}}[60s]))')


def _iso(t=None):
    return (t or datetime.now(timezone.utc)).isoformat()


# ---------------- 전달 증거 ----------------
# 판정 규칙(사전등록 §8.2 N3 초안): 보호(새) Pod의 probe 소요 시간은 kubelet의 prober_probe_duration 60 s 평균(15 s 간격)이다.
# 단일 probe의 최댓값이 아니라 "지연이 새 Pod에 지속적으로 닿았는가"를 본다 - 정상 약 0.02 s, 최소 지연 단계(0.5 s)가 새면 한 창 안에 평균이 0.1 s를 넘는다.
PROTECTED_RECHECK_SEC = 30.0  # 보호(새) Pod 목록을 이 간격 이상으로 다시 읽는다(안전 점검 호출 주기보다 드물게 - 조회 비용 제한)
STAGE_MIN_POINTS = 3          # 단계마다 이 개수 이상의 값이 있어야 한다
MAX_GAP_SEC = 45.0            # 단계 구간(적용 확인~삭제) 안에서 값이 없는 구간의 최대 길이(15 s 간격 3칸) - 일부만 보고 통과하는 것을 막는다
GRACE_SEC = 30.0              # 삭제 요청 뒤에도 60 s 평균에는 지연이 남고 실제 소멸은 조금 늦다 - 이 시간까지의 값도 본다
QUERY_PREROLL_SEC = 30.0      # 값 조회 구간 = [첫 생성 - 이 시간, 마지막 삭제 + QUERY_TAIL_SEC]
QUERY_TAIL_SEC = 45.0         # GRACE_SEC보다 길게 - 마지막 단계의 꼬리 값까지 확보
OBS_BEFORE_SEC = 60.0         # 보호 Pod 관측을 판정하는 구간 = [첫 적용 - 이 시간, 마지막 삭제 + GRACE_SEC]. 그 밖(주입 전후)의 변화는 격리 증거가 아니다
OBS_STAGE_BEFORE_SEC = 30.0   # 단계 시작 전 이 시간 안의 관측도 그 단계의 관측으로 센다(재확인 간격 30 s)
WATCH_POLL_SEC = 1.0
JOIN_SEC = 10.0               # 삭제 때 감시 스레드가 끝나기를 기다리는 최대 시간
FINAL_SEC = 15.0              # 삭제 직전 마지막 확인·읽기에 쓰는 최대 시간 - 넘으면 증거만 포기하고 지우기는 한다
STAGE_TOLERANCE_SEC = 2.0     # 단계가 계획한 시간(90 s)만큼 유지됐는지 볼 때 허용하는 오차(생성 반환~삭제 반환 사이)
LOCK_SEC = 20.0               # 삭제 때 단계 잠금을 기다리는 최대 시간 - 넘어도 지우기는 한다(증거 기록만 포기)


class DeliveryRecorder:
    """파일럿 주입기의 주입 가능한 함수(create/delete/is_stage_injected)를 감싸 만든 지연 설정의 증거를 남긴다(주입기 코드는 수정하지 않는다).
    - create: 단계 설정(+duration 안전망)을 만들고 직후 읽어 `created`에 둔다. 단계마다 감시 스레드가 "적용됐는가"를 직접 확인한다
      (실제 주입기의 is_started는 첫 단계에서 한 번 True가 되면 굳어 이후 단계를 다시 확인하지 않는다).
    - `actual`은 **적용을 확인한 시각 이후**에 읽은 가장 최근 설정이다(생성 직후 값으로 대신하지 않는다). 읽기에 실패하면 None으로 남는다.
      적용 뒤 읽은 **모든** 설정의 위반은 `violations`에 쌓인다 - 나중에 읽은 깨끗한 값이 앞의 나쁜 값을 지우지 못한다.
    - 단계마다 잠금 하나로 만들기·적용 확인·읽기·삭제를 순서대로 처리한다(주입기 단계 스레드와 정리(cleanup)가 동시에 삭제해도 안전).
      삭제가 끝나면 단계를 닫고(closed), 늦게 돌아온 감시 스레드는 증거를 바꾸지 못한다. 감시가 끝나지 않으면 문제로 보고한다."""

    def __init__(self, *, create_fn, delete_fn, injected_fn, read_fn, now_fn=_iso, namespace=NAMESPACE,
                 margin_sec=CR_DURATION_MARGIN_SEC, poll_sec=WATCH_POLL_SEC, join_sec=JOIN_SEC, lock_sec=LOCK_SEC,
                 final_sec=FINAL_SEC):
        self._create, self._delete, self._injected, self._read = create_fn, delete_fn, injected_fn, read_fn
        self._now, self.ns, self.margin, self.poll = now_fn, namespace, margin_sec, poll_sec
        self.join_sec, self.lock_sec, self.final_sec = join_sec, lock_sec, final_sec
        self.crs = {}                 # name -> record (생성 순서 보존)
        self._watch = {}              # name -> (thread, stop_event)
        self._locks = {}
        self._guard = threading.Lock()    # 위 딕셔너리 구조만 보호(짧게)

    def _lock_for(self, name):
        with self._guard:
            return self._locks.setdefault(name, threading.RLock())

    def create(self, cr_name, run_id, arm, target_pod, stage):
        duration = f"{int(stage['duration_sec'] + self.margin)}s"
        with self._lock_for(cr_name):                         # 만드는 중에 정리가 끼어들지 못한다
            self._create(cr_name, run_id, arm, target_pod, stage, duration=duration)
            rec = {"name": cr_name, "stage": stage["name"], "stage_def": dict(stage), "target_pod": target_pod, "duration": duration,
                   "expected": pr.expected_cr_body(cr_name, run_id, arm, target_pod, stage, namespace=self.ns, duration=duration),
                   "created_at": self._now(), "injected_at": None, "deleted_at": None, "closed": False, "created": None,
                   "actual": None, "actual_at": None, "read_error": None, "injected_check_error": None, "violations": [],
                   "watcher_alive_at_delete": None, "lifecycle_problems": []}
            with self._guard:
                self.crs[cr_name] = rec
            rec["created"] = self._try_read(rec)[0]
            stop = threading.Event()
            t = threading.Thread(target=self._watch_loop, args=(rec, stop, stage["duration_sec"]), daemon=True, name=f"n2-watch-{cr_name}")
            self._watch[cr_name] = (t, stop)                  # 시작하기 전에 등록 - 정리가 항상 핸들을 찾는다
            t.start()

    def _try_read(self, rec):
        try:
            return self._read(rec["name"]), None
        except Exception as e:  # noqa: BLE001 - 읽기 실패도 증거(확인 불가)
            return None, f"{type(e).__name__}: {e}"

    def _check_snapshot(self, rec, snap):
        found = [f"actual:{p}" for p in pr.cr_spec_problems(snap, rec["target_pod"], namespace=self.ns, stage=rec["stage_def"])]
        found += [f"actual:{p}" for p in pr.cr_status_problems(snap, rec["target_pod"], namespace=self.ns)]
        if (snap.get("spec") or {}).get("duration") != rec["duration"]:
            found.append("duration_not_stored")
        for f in found:
            if f not in rec["violations"]:
                rec["violations"].append(f)

    def _readback_locked(self, rec):
        got, err = self._try_read(rec)
        if rec["closed"]:
            return                                            # 기다리는 사이 단계가 닫혔다 - 늦게 돌아온 읽기는 증거로 쓰지 않는다
        if got is not None:
            rec["actual"], rec["actual_at"], rec["read_error"] = copy.deepcopy(got), self._now(), None
            self._check_snapshot(rec, got)
        elif rec["actual"] is None:
            rec["read_error"] = err or "not_found"            # 적용 뒤 읽은 값이 하나도 없다 - 생성 직후 값으로 대신하지 않는다

    def _mark_injected_locked(self, rec) -> bool:
        if rec["injected_at"] is not None:
            return True
        try:
            ok = bool(self._injected(rec["name"]))
        except Exception as e:  # noqa: BLE001
            rec["injected_check_error"] = f"{type(e).__name__}: {e}"
            return False
        if rec["closed"]:
            return False                                      # 위와 같은 이유 - 늦은 "적용됨" 응답은 닫힌 증거를 바꾸지 못한다
        if ok:
            rec["injected_at"] = self._now()
            self._readback_locked(rec)                        # 적용된 뒤의 상태(containerRecords 등)
        return ok

    def _bounded(self, fn, label, rec):
        """외부 호출(적용 확인·읽기)이 멈춰도 삭제가 막히지 않게 시간 제한을 둔다. 넘으면 증거만 포기하고 문제로 남긴다(늦은 결과는 closed 검사가 막는다)."""
        box = {}

        def run():
            try:
                box["v"] = fn()
            except Exception as e:  # noqa: BLE001
                box["e"] = e
        t = threading.Thread(target=run, daemon=True, name=f"n2-bounded-{label}-{rec['name']}")
        t.start()
        t.join(self.final_sec)
        if t.is_alive():
            rec["lifecycle_problems"].append(f"{label}_timeout")
        return box.get("v")

    def _final_evidence_locked(self, rec):
        self._mark_injected_locked(rec)                       # 지우기 직전 마지막 확인
        if rec["injected_at"] is not None:
            self._readback_locked(rec)                        # 적용 중 마지막 상태

    def _observe(self, rec) -> bool:
        """감시 스레드·주입기 공용 적용 확인. 잠금을 못 잡으면(삭제 중) 이번엔 건너뛴다. 닫힌 단계는 증거를 바꾸지 않는다."""
        lk = self._lock_for(rec["name"])
        if not lk.acquire(timeout=1.0):
            return False
        try:
            return False if rec["closed"] else self._mark_injected_locked(rec)
        finally:
            lk.release()

    def _watch_loop(self, rec, stop, stage_sec):
        deadline = time.monotonic() + stage_sec + self.margin
        while not rec["closed"]:
            if self._observe(rec) or stop.wait(self.poll) or time.monotonic() > deadline:
                return

    def is_injected(self, cr_name) -> bool:
        """주입기가 쓰는 적용 확인(첫 단계). 감시 스레드와 같은 기록 경로를 쓴다."""
        rec = self.crs.get(cr_name)
        if rec is None:
            return bool(self._injected(cr_name))
        return self._observe(rec) or rec["injected_at"] is not None

    def delete(self, cr_name):
        lk = self._lock_for(cr_name)
        w = None
        if lk.acquire(timeout=self.lock_sec):                 # 1단계: 만드는 중이면 끝날 때까지 기다린 뒤 감시 핸들을 읽고 멈추게 한다
            try:
                w = self._watch.get(cr_name)
                if w:
                    w[1].set()
            finally:
                lk.release()
        else:                                                 # 잠금을 못 잡아도(감시가 쥔 채 멈춤) 핸들은 읽고 멈추게 한다
            w = self._watch.get(cr_name)
            if w:
                w[1].set()
        if w:
            w[0].join(timeout=self.join_sec)                  # 잠금 밖에서 기다린다 - 감시가 잠금을 기다리다 시간을 허비하지 않게
        got = lk.acquire(timeout=self.lock_sec)               # 2단계: 증거 기록과 삭제
        try:
            rec = self.crs.get(cr_name)
            first = rec is not None and not rec["closed"]
            if first:
                if w:
                    rec["watcher_alive_at_delete"] = w[0].is_alive()
                if got:
                    self._bounded(lambda: self._final_evidence_locked(rec), "final_evidence", rec)
                else:
                    rec["lifecycle_problems"].append("lifecycle_lock_timeout")
            self._delete(cr_name)                             # 증거 기록과 별개로 항상 지운다(이미 지워졌어도 안전한 호출)
            if first:
                rec["deleted_at"] = self._now()
                rec["closed"] = True
        finally:
            if got:
                lk.release()

    def planned_cr_names(self):
        return list(self.crs)

    def record(self) -> dict:
        with self._guard:
            crs = [copy.deepcopy(r) for r in self.crs.values()]
        return {"crs": crs, "targets": sorted({r["target_pod"] for r in crs}), "cr_duration_margin_sec": self.margin,
                "first_created_at": crs[0]["created_at"] if crs else None,
                "last_deleted_at": max((r["deleted_at"] for r in crs if r["deleted_at"]), default=None)}


def _epoch(iso):
    return datetime.fromisoformat(iso).timestamp()


class ProtectedWatch:
    """주입 중 보호(새) Pod를 주기적으로 읽어 시각과 함께 쌓는다(읽기 전용). 빈 목록·조회 실패도 그대로 남긴다(판정은 delivery_problems)."""

    def __init__(self, net, *, now_fn=time.time, mono_fn=time.monotonic, recheck_sec=PROTECTED_RECHECK_SEC):
        self.net, self.now, self.mono, self.recheck = net, now_fn, mono_fn, recheck_sec
        self.obs, self._last = [], None

    def check(self, info):
        """info = 주입기의 get_preview_prep_info() 결과. 새 Pod가 Ready가 되기 전에는 아무것도 하지 않는다."""
        if not (info and info.get("ready") and info.get("created_pod_hash")):
            return
        m = self.mono()
        if self._last is not None and m - self._last < self.recheck:
            return
        self._last, t = m, self.now()
        try:
            self.obs.append({"t": t, "pods": sorted(self.net.protected_pods(info["created_pod_hash"])), "error": None})
        except Exception as e:  # noqa: BLE001
            self.obs.append({"t": t, "pods": None, "error": f"{type(e).__name__}: {e}"})


def protected_observation_problems(delivery, observations, *, before_sec=OBS_BEFORE_SEC, grace_sec=GRACE_SEC):
    """(문제 목록, 보호 Pod 목록). 판정 구간 = [첫 적용 - before_sec, 마지막 삭제 + grace_sec]; 그 밖의 변화는 격리 증거가 아니라 판정하지 않는다.
    구간 안에서 성공한 관측이 (1) 하나라도 비어 있지 않아야 하고 (2) 모두 같은 집합이어야 하며 (3) 단계마다 하나 이상 있어야 한다.
    조회 실패 자체는 기록만 하고, 단계의 관측을 비게 만들 때만 문제로 본다(그 단계의 probe 값은 같은 Pod 이름으로 별도 검증되므로 교체는 값 공백으로도 드러난다)."""
    crs = [c for c in delivery["crs"] if c.get("injected_at") and c.get("deleted_at")]
    if not crs:
        return [], []                                               # 단계 적용·삭제 자체의 문제는 다른 검사가 이미 보고한다
    lo = min(_epoch(c["injected_at"]) for c in crs) - before_sec
    hi = max(_epoch(c["deleted_at"]) for c in crs) + grace_sec
    ok = [o for o in observations or [] if lo <= o["t"] <= hi and o.get("pods") is not None]
    nonempty = [o for o in ok if o["pods"]]
    if not nonempty:
        return ["no_protected_pod_captured(새 Pod 확인 불가 - 지연이 새 Pod에 닿았는지 알 수 없음)"], []
    protected, probs = sorted(nonempty[0]["pods"]), []
    changed = sorted({tuple(sorted(o["pods"])) for o in ok if sorted(o["pods"]) != protected})
    if changed:
        probs.append(f"protected_pods_changed_during_injection:{[list(x) for x in changed]} (처음 {protected})")
    for c in crs:
        a, b = _epoch(c["injected_at"]) - OBS_STAGE_BEFORE_SEC, _epoch(c["deleted_at"]) + grace_sec
        if not any(a <= o["t"] <= b for o in ok):
            probs.append(f"{c['stage']}: protected_pods_unobserved_in_stage")
    return probs, protected


def protected_coverage_problems(delivery, samples_by_pod, protected_pods, *, limit=pr.PROTECTED_MAX_PROBE_SEC,
                                min_points=STAGE_MIN_POINTS, max_gap=MAX_GAP_SEC, grace_sec=GRACE_SEC) -> list:
    """단계마다 보호(새) Pod의 probe 소요 시간(60 s 평균)이 [적용 확인, 삭제 + grace] 구간에서 (1) min_points개 이상이고 (2) 구간 시작부터 삭제 뒤 첫 값까지
    값이 없는 구간이 max_gap 이하(삭제 이후 값이 하나는 있어야 함)이며 (3) 모두 limit 이하인가. samples_by_pod = {pod: [[epoch, value], ...]}. 부족하면 확인 불가 = 위반(fail-closed)."""
    probs = []
    for c in delivery["crs"]:
        if not c.get("injected_at") or not c.get("deleted_at"):
            continue                                                  # 단계 적용·삭제 자체의 문제는 다른 검사가 이미 보고한다
        lo, end = _epoch(c["injected_at"]), _epoch(c["deleted_at"])
        for pod in sorted(set(protected_pods or [])):
            pts = sorted((t, v) for t, v in (samples_by_pod or {}).get(pod, []) if lo <= t <= end + grace_sec)
            finite = [v for _, v in pts if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]
            if len(finite) < min_points or len(finite) != len(pts):
                probs.append(f"{c['stage']}: {pod}: unverifiable_in_stage({len(finite)}/{len(pts)} finite, need {min_points})")
                continue
            ts = [t for t, _ in pts]
            if not any(t >= end for t in ts):
                probs.append(f"{c['stage']}: {pod}: no_sample_after_stage_end(삭제 뒤 꼬리 증거 없음)")
                continue
            gap = max([ts[0] - lo] + [b - a for a, b in zip(ts, ts[1:])])
            if gap > max_gap:
                probs.append(f"{c['stage']}: {pod}: unobserved_gap_in_stage({gap:.0f}s > {max_gap:.0f}s)")
            elif max(finite) > limit:
                probs.append(f"{c['stage']}: {pod}: probe_duration_mean_max={max(finite):.4f}s > {limit}s (지연이 샌 것으로 봄)")
    return probs


def delivery_problems(delivery, protected_samples, protected_observations, *, namespace=NAMESPACE) -> dict:
    """전달 증거 판정(유효성 V3 대체). {"problems": [...], "container_records_present": bool}. 모두 읽은 값 기준이다.
    protected_samples = {pod: [[epoch, value], ...]} 보호(새) Pod의 probe 소요 시간(60 s 평균),
    protected_observations = ProtectedWatch.obs(주입 중 보호 Pod 목록 관측 - 시각·빈 목록·조회 실패 포함)."""
    if not delivery or not delivery.get("crs"):
        return {"problems": ["no_delivery_record"], "container_records_present": False}
    crs = delivery["crs"]
    probs, protected = protected_observation_problems(delivery, protected_observations)
    probs += pr.stage_windows_problems([{"name": c["stage"], "injected": c["injected_at"] is not None} for c in crs])
    if len(delivery.get("targets") or []) != 1:
        probs.append(f"targets={delivery.get('targets')} (대상 Pod 1개여야 함)")
    stage_by_name = {s["name"]: s for s in nc.NETWORK_STAGES}
    records_present = False
    for c in crs:
        tgt, st = c["target_pod"], stage_by_name.get(c["stage"])
        probs += [f"{c['name']}: intended:{p}" for p in pr.cr_spec_problems(c["expected"], tgt, namespace=namespace, stage=st)]
        if c.get("created") is not None:                                     # 생성 직후 저장값도 맞아야 한다(적용 뒤 값과 별개)
            probs += [f"{c['name']}: created:{p}" for p in pr.cr_spec_problems(c["created"], tgt, namespace=namespace, stage=st)]
        mine = list(c.get("violations") or [])                               # 적용 뒤 읽은 모든 설정의 위반(나중 값이 지우지 못함)
        if c.get("actual") is None:
            probs.append(f"{c['name']}: cr_readback_missing_after_injection {c.get('read_error') or ''}".strip())
        else:
            mine += [f"actual:{p}" for p in pr.cr_spec_problems(c["actual"], tgt, namespace=namespace, stage=st)]
            mine += [f"actual:{p}" for p in pr.cr_status_problems(c["actual"], tgt, namespace=namespace)]
            if ((c["actual"].get("spec") or {}).get("duration")) != c.get("duration"):
                mine.append("duration_not_stored")
            records_present = records_present or pr.has_container_records(c["actual"])
        probs += [f"{c['name']}: {m}" for m in dict.fromkeys(mine)]
        probs += [f"{c['name']}: {m}" for m in c.get("lifecycle_problems") or []]
        if c.get("watcher_alive_at_delete"):
            probs.append(f"{c['name']}: watcher_still_running_after_delete")
        if c.get("deleted_at") is None:
            probs.append(f"{c['name']}: not_deleted_by_runner")
        elif st is not None and c.get("created_at"):
            held = _epoch(c["deleted_at"]) - _epoch(c["created_at"])
            if held < st["duration_sec"] - STAGE_TOLERANCE_SEC:     # 정리(cleanup)가 대기를 끊었거나 중단된 단계
                probs.append(f"{c['name']}: stage_cut_short({held:.0f}s < planned {st['duration_sec']}s)")
    if protected:
        probs += protected_coverage_problems(delivery, protected_samples, protected)
    return {"problems": probs, "container_records_present": records_present}


def collect_delivery_evidence(rec, watch, net) -> dict:
    """주입이 끝난 뒤 전달 증거를 모아 판정한다(읽기 전용: Prometheus 조회). Stage 1·2 공용. 조회 실패 등 예외는 '확인 불가'(문제)로 남긴다."""
    try:
        delivery = rec.record() if rec else None
        names = protected_observation_problems(delivery, watch.obs)[1] if delivery and delivery.get("crs") else []
        t0, t1 = (delivery or {}).get("first_created_at"), (delivery or {}).get("last_deleted_at")
        q0 = _iso(datetime.fromtimestamp(_epoch(t0) - QUERY_PREROLL_SEC, timezone.utc)) if t0 else None
        q1 = _iso(datetime.fromtimestamp(_epoch(t1) + QUERY_TAIL_SEC, timezone.utc)) if t1 else None
        samples = net.protected_probe_seconds(names, q0, q1) if (q0 and q1 and names) else {}
        return {"network_delivery": delivery, "network_protected_probe_seconds": samples,
                "network_protected_pod_observations": watch.obs,
                "network_delivery_check": delivery_problems(delivery, samples, watch.obs)}
    except Exception as e:  # noqa: BLE001
        return {"network_delivery": None, "network_protected_pod_observations": watch.obs,
                "network_delivery_check": {"problems": [f"delivery_check_error: {type(e).__name__}: {e}"],
                                           "container_records_present": False}}


def network_preflight(net, profile, *, policy_mode="record_only") -> dict:
    """세션 시작 전 네트워크 환경 점검(읽기 전용, Stage 1·2 공용). 읽지 못하면 시작하지 않는다(fail-closed). 시작 상태에는 새(preview) Pod가 없다."""
    try:
        st = net.read_state()
        return pfm.preflight_problems(
            profile=profile, observed_env=nc.observe_environment(st["template"]),
            policy_sha256=nc.contract_hashes()["recovery_policy_sha256"], template=st["template"], chaos_names=st["chaos_names"],
            pods=st["pods"], active_hash=st["active_hash"], preview_hash=st["preview_hash"], net_probe_body=st["net_probe_body"],
            policy=st["policy"], policy_mode=policy_mode, require_preview=False)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "problems": [f"network_preflight_error: {type(e).__name__}: {e}"], "protected": [], "target": None}


# ---------------- 읽기 전용 클러스터 조회(실제) ----------------
class RealNetLive:
    """읽기 전용 조회만 한다(kubectl get / Prometheus 질의). run은 주입 가능(테스트가 명령이 get뿐인지 확인한다)."""

    def __init__(self, snapshot_fn, profile_path, run=None, policy_state_fn=None):
        self.snapshot_fn, self.profile_path, self._policy_state = snapshot_fn, Path(profile_path), policy_state_fn
        self._run = run or self._subprocess_run
        self.commands = []

    @staticmethod
    def _subprocess_run(cmd):  # pragma: no cover - 라이브 전용
        env = dict(os.environ, MSYS_NO_PATHCONV="1")
        cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, env=env)
        return cp.returncode, cp.stdout, cp.stderr

    def kubectl_get(self, *args):
        cmd = ["kubectl", "get", *args]           # 이 클래스의 유일한 클러스터 접근 지점 - 동사는 항상 get
        self.commands.append(cmd)
        return self._run(cmd)

    def prom(self, path_query: str):
        rc, out, err = self.kubectl_get("--raw", PROM_PROXY + path_query)
        if rc != 0:
            raise RuntimeError(f"prometheus read rc={rc}: {err[-200:]}")
        return json.loads(out)

    def list_chaos_names(self):
        rc, out, err = self.kubectl_get("networkchaos", "-n", NAMESPACE, "-o", "json")
        if rc != 0:
            raise RuntimeError(f"networkchaos list rc={rc}: {err[-200:]}")
        data = json.loads(out)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("networkchaos list response malformed(items 배열 없음) - 빈 목록으로 보지 않는다")
        names = [(i.get("metadata") or {}).get("name") if isinstance(i, dict) else None for i in items]
        if not all(isinstance(n, str) and n for n in names):
            raise RuntimeError("networkchaos list item without name - 확인 불가")
        return sorted(names)

    def read_chaos_cr(self, name):
        rc, out, err = self.kubectl_get("networkchaos", name, "-n", NAMESPACE, "-o", "json")
        if rc != 0:
            if "NotFound" in err or "not found" in err:
                return None
            raise RuntimeError(f"networkchaos get rc={rc}: {err[-200:]}")
        cr = json.loads(out)
        if not isinstance(cr, dict) or not cr.get("spec"):
            raise RuntimeError("networkchaos get response malformed(spec 없음)")
        return cr

    def net_probe_body(self):
        return self.prom("query?" + urllib.parse.urlencode({"query": nc.NET_EXPR}))

    @staticmethod
    def _pods(snap) -> list:
        return [{"name": p["name"], "uid": p.get("uid"), "hash": p.get("hash"), "ready": p.get("ready"),
                 "terminating": bool(p.get("deleting"))} for p in snap.get("pods") or []]

    def protected_pods(self, created_hash) -> list:
        """prepare가 만든 preview(새) Pod 이름 = 지연이 닿으면 안 되는 보호 Pod. 읽기 전용."""
        return sorted(p["name"] for p in self._pods(self.snapshot_fn() or {})
                      if p["hash"] == created_hash and not p["terminating"])

    def read_state(self, snap=None) -> dict:
        snap = snap if snap is not None else (self.snapshot_fn() or {})
        pods = self._pods(snap)
        return {"template": snap.get("template"), "pods": pods, "active_hash": snap.get("active"),
                "preview_hash": snap.get("preview"), "chaos_names": self.list_chaos_names(),
                "net_probe_body": self.net_probe_body(), "policy": self._policy_state() if self._policy_state else None,
                "delay": snap.get("delay")}

    def read_end_state(self, results_dir) -> dict:
        s = self.read_state()
        markers = sorted(p.name for p in Path(results_dir).glob("running-*.json"))
        return cl.capture_state(s["template"], s["delay"], s["policy"] or {}, s["chaos_names"], markers)

    def protected_probe_seconds(self, pods, t0_iso, t1_iso) -> dict:
        """보호 Pod별 readiness probe 소요 시간 [[epoch, 초], ...](15 s 간격 60 s 평균)."""
        t0, t1 = datetime.fromisoformat(t0_iso).timestamp(), datetime.fromisoformat(t1_iso).timestamp()
        out = {}
        for pod in pods:
            q = urllib.parse.urlencode({"query": PROBE_SERIES.format(pod=pod), "start": t0, "end": t1, "step": 15})
            res = self.prom("query_range?" + q)["data"]["result"]
            if len(res) > 1:
                raise RuntimeError(f"protected pod {pod}: probe series {len(res)}개(1개여야 함)")
            out[pod] = [[float(t), float(v)] for t, v in (res[0]["values"] if res else [])]      # [epoch, 초] - 단계별 대조용
        return out

    def wait_network_chaos_gone(self, names, *, sleep=None, monotonic=None, timeout_sec=60.0) -> list:
        import time
        sleep, monotonic = sleep or time.sleep, monotonic or time.monotonic
        deadline, left = monotonic() + timeout_sec, list(names)
        while left and monotonic() < deadline:
            now = set(self.list_chaos_names())
            left = [n for n in left if n in now]
            if left:
                sleep(2.0)
        return left


# ---------------- 주입기 조립(원본 CPU 주입기와 같은 두 겹) ----------------
def real_wrappers(inj, cleanup_preview_fn):
    """원본 CPU 주입기(run_guarded_pilot.make_injector)와 같은 두 겹: preview 준비(D5 정리 관문 연결) + 승격 진단."""
    import arm_controller
    import node_resource_safety_gate as nrsg
    import run_detector_switch_ab_trial as ab
    inj = arm_controller.wrap_injector_with_preview_prep(inj, ab.ARM_LABEL, cleanup_preview_fn=cleanup_preview_fn)
    return nrsg.wrap_injector_with_real_promotion_diagnostic_exception(
        inj, ab.ARM_LABEL, nrsg.REAL_PROMOTION_DIAGNOSTIC_EXPECTED_RUN_MODE)


def make_injector_factory(recorder_box, net, hooks=None):
    """deps.make_injector 자리. hooks는 테스트 전용 교체 지점(기본은 모두 실제 파일럿 코드):
      chaos=(create, delete, injected) 지연 설정 함수 / base_injector(run_id, rep, recorder) 주입기 / wrap(inj, cleanup_preview_fn) 두 겹 감싸기."""
    h = hooks or {}

    def make_injector(run_id, rep, kind, cleanup_preview_fn):
        import network_degrade_adapter as nda
        import run_detector_switch_ab_trial as ab
        create, delete, injected = h.get("chaos") or (nda.create_network_chaos, nda.delete_network_chaos, nda.is_stage_injected)
        rec = DeliveryRecorder(create_fn=create, delete_fn=delete, injected_fn=injected, read_fn=net.read_chaos_cr)
        recorder_box["rec"] = rec
        if h.get("base_injector"):
            inj = h["base_injector"](run_id, rep, rec)
        else:
            inj = nda.make_network_degrade_injector(run_id, ab.ARM_LABEL, rep, create_chaos_fn=rec.create,
                                                    delete_chaos_fn=rec.delete, is_stage_injected_fn=rec.is_injected)
        out = (h.get("wrap") or real_wrappers)(inj, cleanup_preview_fn)
        # 동결 실행기들이 주입기에서 읽는 모양 - 지연 설정 이름(소멸 확인)과 대상 Pod. 네트워크 환경에는 CPU 스트레스 수치가 없다
        out.get_stress_delivery_record = lambda: {"kind": "network_delay", "planned_cr_names": rec.planned_cr_names(),
                                                  "target": {"name": (rec.record()["targets"] or [None])[0]}}
        return out
    return make_injector


def net_deps(deps, factory, net):
    """원본 deps의 복사본에서 CPU 전용 두 자리(주입기 조립, 스트레스 CR 소멸 확인)만 네트워크 것으로 바꾼다."""
    d = copy.copy(deps)
    d.make_injector = factory
    d.wait_chaos_gone = lambda names: net.wait_network_chaos_gone(names)
    return d


# ---------------- 계약 조건 ----------------
def pressure_conditions(inst) -> dict:
    smoke = inst.smoke
    return {"stress_stages": [dict(s) for s in nc.NETWORK_STAGES], "probe_config": str(smoke.DEFAULT_PROBE_CONFIG),
            "probe_config_sha256": hashlib.sha256(smoke.DEFAULT_PROBE_CONFIG.read_bytes()).hexdigest(),
            "probe_image": inst.IMAGE_FORCE_CLOSE, "probe_image_id": inst.IMAGE_FORCE_CLOSE_ID,
            "detector_rule": {"consecutive": inst.sd.CONSECUTIVE_THRESHOLD, "cooldown_sec": inst.sd.COOLDOWN_SEC},
            "start_state_rule": "canonical_healthy_r2", "probe_profile": dict(nc.PROBE_PROFILE),
            "network_target": nc.PRESSURE["target"], "cr_duration_margin_sec": CR_DURATION_MARGIN_SEC}


def pressure_frozen(inst, art_hashes: dict) -> dict:
    """원본 run_validity V4 비교값(네트워크 계약값). 실행기가 계약에 쓰는 값과 같은 함수에서 만든다."""
    return {**pressure_conditions(inst), "threshold": {d: nc.FROZEN_DETECTORS[d]["threshold"] for d in nc.DETECTOR_IDS},
            "artifact_hashes": art_hashes, "observation_sec": OBSERVATION_SEC}


# ponytail: 원본 run_stage1.run_pressure(CPU 압박) 순서 복사 + 네트워크 차이만 반영 - 원본은 수정 금지, CPU 전용 부분이 많아 인자로 바꿀 수 없다.
def run_pressure_net(inst, run, *, deps, starter, art_hashes, profile_sha, results_dir, net, hooks=None,
                     preflight_only=False, code_provenance=None):
    gp, gcg, gr, rt, sd = inst.gp, inst.gcg, inst.gr, inst.rt, inst.sd
    rep = inst.PRESSURE_RUNS[run][1]
    run_id = inst.run_id_for(run)
    summary_path = results_dir / f"summary-{run_id}.json"
    if summary_path.exists() or gr.live_evidence_path(results_dir, run_id).exists():
        raise SystemExit(f"실행 ID 재사용 금지: {run_id}")
    deps.check_unresolved()
    box = {}
    deps = net_deps(deps, make_injector_factory(box, net, hooks), net)
    obs_sec = OBSERVATION_SEC
    fz = pressure_frozen(inst, art_hashes)
    contract = {"run_id": run_id, "run": run, "plan_id": inst.pc.PLAN_ID, "contract_doc": inst.pc.PREREG_DOC,
                "comparison": {"series": inst.pc.PLAN_ID, "stage": "stage1", "session": run, "counted_in_comparison": True},
                "conditions": pressure_conditions(inst), "kind": "pressure", "mode": inst.MODE, "actuating_rule": None,
                "signal_send": False, "detector": "phase2_network_same_tick_v1", "threshold": fz["threshold"],
                "artifact_hashes": art_hashes, "observation_sec": obs_sec, "ready_timeout_sec": gp.READY_TIMEOUT_SEC,
                "scale_down_delay": {"base": gp.BASE_SCALE_DOWN_DELAY_SEC, "changed": False}, "act_approval": None,
                "no_promotion_path": True, "environment_profile_sha256": profile_sha,
                "net_probe": {"definition_sha256": nc.NET_DEFINITION_SHA256, "threshold": nc.NET_THRESHOLD}}
    if code_provenance is not None:
        contract["code_provenance"] = code_provenance
    pol = deps.policy
    policy_check = gp.check_policy(pol, inst.MODE, None)
    session_start = deps.session_check()
    ssh_ev = deps.make_ssh_evidence(results_dir / f"ssh-evidence-{run_id}")
    run_guard_disk_fn = ssh_ev.disk_fn(deps.run_guard_nodes)
    preflight = deps.preflight(pol, disk_check_fn=ssh_ev.disk_fn(deps.preflight_nodes))
    try:
        delay_before = deps.read_delay()
    except Exception as e:  # noqa: BLE001
        delay_before = f"read_error: {type(e).__name__}: {e}"
    try:
        old_policy = deps.old_policy_check()
    except Exception as e:  # noqa: BLE001
        old_policy = {"isolated": False, "reasons": [f"check_error: {type(e).__name__}: {e}"]}
    reasons = list(preflight.get("reasons") or []) + policy_check["problems"] + list(session_start.get("problems") or [])
    if old_policy.get("isolated") is not True:
        reasons.append(f"old_recovery_policy_not_isolated: {old_policy.get('reasons')}")
    if delay_before != gp.BASE_SCALE_DOWN_DELAY_SEC:
        reasons.append(f"scaleDownDelaySeconds={delay_before} != {gp.BASE_SCALE_DOWN_DELAY_SEC}")
    try:
        snap = deps.canonical_snapshot()
        cp = gcg.check_canonical(snap, delay=gp.BASE_SCALE_DOWN_DELAY_SEC)
    except Exception as e:  # noqa: BLE001
        snap, cp = None, [f"snapshot_error: {type(e).__name__}: {e}"]
    p0 = gcg.active_pod(snap or {})
    start_pod = (p0["name"], p0["uid"]) if p0 else None
    preflight["canonical_start"] = {"problems": cp, "start_pod": start_pod, "state": {k: (snap or {}).get(k) for k in (
        "phase", "abort", "active", "stable", "current", "preview", "delay", "pods", "svc_selectors", "endpoints", "rs")}}
    reasons += [f"canonical_start: {x}" for x in cp]
    # ---- 네트워크 사전 점검(읽기 전용) ----
    try:
        loaded = nc.load_profile(net.profile_path, stage="stage1")
        if loaded["sha256"] != profile_sha:
            raise nc.ProfileInvalid("profile 해시가 세션 시작 때와 다름")
        net_pf = network_preflight(net, loaded["profile"], policy_mode=inst.MODE)
    except Exception as e:  # noqa: BLE001 - 읽지 못하면 시작하지 않는다(fail-closed)
        net_pf = {"ok": False, "problems": [f"network_preflight_error: {type(e).__name__}: {e}"], "protected": [], "target": None}
    preflight["network"] = net_pf
    reasons += [f"network_preflight: {x}" for x in net_pf["problems"]]
    preflight.update(policy=policy_check, session=session_start, scale_down_delay_before=delay_before, old_policy=old_policy,
                     ok=not reasons, reasons=reasons, ssh_check_evidence=ssh_ev.summary())
    preflight_file = results_dir / (f"preflight-{run_id}-{'preflight-only' if preflight_only else 'run'}-"
                                    f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.json")
    with open(preflight_file, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(preflight, ensure_ascii=False, indent=2, default=str))
    if reasons:
        raise SystemExit(f"사전검사 실패 - 시작 안 함(클러스터 변경 없음): {reasons}")
    if preflight_only:
        return {"preflight_only": True, "run_id": run_id, "preflight": preflight, "preflight_file": preflight_file.name}
    (results_dir / f"contract-{run_id}.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")

    events_since = datetime.now(timezone.utc).isoformat()
    running_marker = results_dir / f"running-{run_id}.json"
    running_marker.write_text(json.dumps({"run_id": run_id, "started_at": events_since}), encoding="utf-8")
    evidence_path = gr.live_evidence_path(results_dir, run_id)
    state, cleanup_errors, probe_image_check = {}, [], {}
    watch = ProtectedWatch(net, mono_fn=deps.monotonic_fn)
    run_guard = {"mem_drop_abort_gib": 1.0, "disk_recheck_sec": 60.0}
    outcome, trial_error, canonical_end, rollout, detector = None, None, None, None, {"started": False}

    def assess_now(prep_info=None):
        prep_info = prep_info or gate.prep_info
        return gcg.assess(run_id=run_id, mode=inst.MODE, policy_state_fn=pol.state, ledger_fn=deps.ledger_read,
                          evidence_path=evidence_path, view_fn=deps.rollout_view, rollout_uid=(rollout or {}).get("uid"),
                          old_active=(prep_info or {}).get("pre_prepare_active_selector"), prep_info=prep_info,
                          detector_expected=detector["started"])
    gate = gcg.PreviewCleanupGate(assess_now, deps.cleanup_unpromoted_preview,
                                  restore_fn=lambda prep: deps.canonical_restore(prep, start_pod))
    old_policy_checks = {"n": 0, "last": None}

    def check_old_policy():
        try:
            r = deps.old_policy_check()
        except Exception as e:  # noqa: BLE001
            r = {"isolated": False, "reasons": [f"check_error: {type(e).__name__}: {e}"]}
        old_policy_checks["n"] += 1
        old_policy_checks["last"] = {k: r.get(k) for k in ("isolated", "reasons")}
        if r.get("isolated") is not True:
            raise inst.HarnessCorrupted(f"기존 recovery-policy 승격 경로가 열림 - 중단: {r.get('reasons')}")

    def _terminate(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")
    old_handlers = ({getattr(signal, s): signal.signal(getattr(signal, s), _terminate)
                     for s in ("SIGTERM", "SIGBREAK") if hasattr(signal, s)}
                    if threading.current_thread() is threading.main_thread() else {})
    try:
        rollout = deps.rollout_identity()
        injector = deps.make_injector(run_id, rep, "pressure", gate.cleanup)
        base_prepare = injector.prepare

        def prepare_isolated():
            check_old_policy()
            base_prepare()
        injector.prepare = prepare_isolated
        rt.compose_prepare(injector, arm="control", expected_start_node=None, state=state, apply_override_fn=lambda: None)
        prober = deps.make_prober(run_id, rep, "pressure", gp.READY_TIMEOUT_SEC + obs_sec)
        base = deps.base_safety(injector, "pressure")

        def capture_protected():          # prepare가 새 Pod를 Ready로 만든 뒤부터 주기적으로(읽기 전용) 확인 - 시각과 함께 쌓는다
            watch.check(injector.get_preview_prep_info() if hasattr(injector, "get_preview_prep_info") else None)

        def safety(baseline_state, prober_):
            base(baseline_state, prober_)
            deps.run_guard_check(run_guard, injector, state, disk_fn=run_guard_disk_fn)
            deps.probe_image_check(probe_image_check)
            check_old_policy()
            capture_protected()
        start_shadow = starter(results_dir)

        def detector_start(rid, path, sink_url):
            detector["started"] = True
            return start_shadow(rid, path, sink_url)
        outcome = deps.trial_pressure(
            run_id, injector, prober, deps.pick_port(), evidence_path, results_dir / f"sink-captured-{run_id}.jsonl",
            deps.probe_csv_path(run_id, rep), post_injection_observation_sec=obs_sec, check_safety_fn=safety,
            detector_start_fn=detector_start, sleep_fn=deps.sleep_fn, monotonic_fn=deps.monotonic_fn,
            detector_ready_timeout_sec=gp.READY_TIMEOUT_SEC, detector_ready_fn=lambda: sd.first_scored_evaluation(evidence_path))
    except BaseException as e:  # noqa: BLE001 - 하네스 오류·중단 신호도 요약·정리 확인을 남긴다
        trial_error = f"{type(e).__name__}: {e}"
        outcome = {"status": "harness_error", "reason": trial_error, "cleanup_status": "unknown"}
    finally:
        policy_after = None
        try:
            policy_after = pol.state()
        except Exception as e:  # noqa: BLE001
            cleanup_errors.append(f"policy_state_after_failed: {type(e).__name__}: {e}")
        if gate.held:
            cleanup_errors.append(f"preview_cleanup_held: reasons={gate.held['reasons']} "
                                  f"attempt_evidence={gate.held['attempt_evidence']} remaining={gate.held['remaining']}")
        try:
            delay_after = deps.read_delay()
        except Exception as e:  # noqa: BLE001
            delay_after = f"read_error: {type(e).__name__}: {e}"
        if delay_after != gp.BASE_SCALE_DOWN_DELAY_SEC:
            cleanup_errors.append(f"scale_down_delay_after={delay_after} != {gp.BASE_SCALE_DOWN_DELAY_SEC}")
        if state.get("override") or state.get("override_error"):
            cleanup_errors.append("record_only에서 유예 변경 흔적 - 수동 확인")
        for k in ("unresolved", "blocked", "context"):
            if (policy_after or {}).get(k):
                cleanup_errors.append(f"policy_{k}_after: {policy_after[k]}")
        try:
            old_policy_end = deps.old_policy_check()
        except Exception as e:  # noqa: BLE001
            old_policy_end = {"isolated": False, "reasons": [f"check_error: {type(e).__name__}: {e}"]}
        if old_policy_end.get("isolated") is not True:
            cleanup_errors.append(f"old_recovery_policy_not_isolated_at_end: {old_policy_end.get('reasons')}")
        if outcome.get("cleanup_status") != "complete":
            cleanup_errors.append(f"trial_cleanup_incomplete: prober={outcome.get('prober_stop_failed')}, "
                                  f"injector={outcome.get('injector_cleanup_failed')}")
        rec = box.get("rec")
        planned = rec.planned_cr_names() if rec else []
        try:
            left = deps.wait_chaos_gone(planned)
            if left:
                cleanup_errors.append(f"network_chaos_residual: {left}")
        except Exception as e:  # noqa: BLE001
            cleanup_errors.append(f"network_chaos_check_failed: {type(e).__name__}: {e}")
        if gate.restore_failed is not None:
            cleanup_errors.append(f"canonical_restore_failed: {gate.restore_failed.get('problems')}")
        canonical_end = gcg.wait_canonical(deps.canonical_snapshot, delay=gp.BASE_SCALE_DOWN_DELAY_SEC,
                                           sleep=deps.sleep_fn, monotonic=deps.monotonic_fn)
        if not canonical_end["ok"]:
            cleanup_errors.append(f"canonical_end_not_reached: {canonical_end['problems']}")
        if trial_error:
            cleanup_errors.append(f"harness_error: {trial_error}")
            try:
                left_pods = [p["name"] for p in deps.read_probe_pods() if not p["terminating"]]
                if left_pods:
                    cleanup_errors.append(f"probe_pod_residual: {left_pods}")
            except Exception as e:  # noqa: BLE001
                cleanup_errors.append(f"probe_pod_check_failed: {type(e).__name__}: {e}")
        try:  # 세션 끝 상태(지연 설정 잔여·probe 설정 유지·정책·표식). 이 세션의 running 표식은 아직 있으므로 제외하고 본다
            end_state = net.read_end_state(results_dir)
            end_state["running_markers"] = [m for m in end_state["running_markers"] if run_id not in m]
            cleanup_errors += [f"network_session_end: {x}" for x in cl.session_end_problems(end_state, planned, policy_mode=inst.MODE)]
        except Exception as e:  # noqa: BLE001
            cleanup_errors.append(f"network_end_state_failed: {type(e).__name__}: {e}")
        for sig_, handler in old_handlers.items():
            signal.signal(sig_, handler)

    pod_events_error = None
    try:
        (results_dir / f"pod-events-{run_id}.json").write_text(
            json.dumps(deps.capture_events(events_since), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        pod_events_error = f"{type(e).__name__}: {e}"
    summary = {"run_id": run_id, "run": run, "kind": "pressure", "mode": inst.MODE, "outcome_status": outcome.get("status"),
               "outcome_reason": outcome.get("reason"), "invalidity_reasons": outcome.get("invalidity_reasons"),
               "detector_readiness": outcome.get("detector_readiness") or {}, "evidence_counts": gp.evidence_counts(evidence_path),
               "watcher": {"action": None}, "context": None, "rollout": rollout, "policy_state_after": policy_after,
               "scale_down_delay_before": delay_before, "scale_down_delay_after": delay_after,
               "session_start": session_start, "session_end": deps.session_check(), "pod_events_error": pod_events_error,
               "probe_image_runtime_check": probe_image_check, "start_active_node": state.get("start_active_node"),
               "harness_error": trial_error, "cleanup_errors": cleanup_errors,
               "observation_ended_at_utc": outcome.get("observation_ended_at_utc"),
               "old_policy_isolation": {"start": old_policy, "checks": old_policy_checks,
                                        "end": {k: old_policy_end.get(k) for k in ("isolated", "reasons")}},
               "trial_outcome": outcome, "preview_cleanup_gate": gate.log, "ssh_check_evidence": ssh_ev.summary(),
               "run_guard": {k: v for k, v in run_guard.items() if not k.startswith("_")},
               "comparison": contract["comparison"], "preflight_file": preflight_file.name, "canonical_end": canonical_end,
               "detector_started": detector["started"],
               "network_preflight": {k: net_pf.get(k) for k in ("target", "net_probe")},
               "network_protected_pod_observations": watch.obs}
    summary.update(collect_delivery_evidence(box.get("rec"), watch, net))        # 전달 증거(기록 전용 - 판정은 유효성 단계)
    if code_provenance is not None:
        summary["code_provenance"] = code_provenance
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not cleanup_errors:
        running_marker.unlink()
    return summary


# ---------------- 유효성(오프라인, 탐지기 결과 미사용) ----------------
# 전달 증거 문제 중 "지연이 새(preview) Pod에 닿지 않았다"는 것을 확립하지 못한 것들(N3-3·N3-4). 이것은 감지기의 실패가 아니라 **세션 무효**다:
# 유효성은 감지기 결과를 보기 전에 확정되고 무효 세션은 Gate 계산에서 빠지므로 어떤 감지기의 Gate 2·3에도 들어가지 않는다.
ISOLATION_MARKERS = ("probe_duration_mean_max", "unverifiable_in_stage", "unobserved_gap_in_stage", "no_sample_after_stage_end",
                     "protected_pods_changed_during_injection", "no_protected_pod_captured", "protected_pods_unobserved_in_stage")
ISOLATION_INVALID = "SESSION INVALID — target isolation / new-Pod leakage exclusion not established"


def isolation_reasons(problems) -> list:
    """N3 문제 중 새 Pod 격리를 확립하지 못한 것이 있으면 무효 사유 한 줄(세부는 N3_ 사유에 그대로 남는다). 없으면 []."""
    n = sum(1 for p in problems if any(m in p for m in ISOLATION_MARKERS))
    return [f"{ISOLATION_INVALID} ({n}건)"] if n else []


def validity_flags(reasons) -> dict:
    """유효성 기록에 붙이는 분류. 무효 세션은 어떤 경우에도 감지기 실패로 세지 않는다(Gate 입력에서 제외될 뿐)."""
    return {"isolation_not_established": any(ISOLATION_INVALID in r for r in reasons), "counts_as_detector_failure": False,
            "detector_gate_inclusion": "excluded" if reasons else "included"}


def validate_pressure_net(inst, run, d, *, operator: str, probe_dir=None) -> dict:
    """원본 run_validity(A arm 경로)를 그대로 쓰고 CPU 전용 V3만 네트워크 전달 확인으로 교체한다."""
    import guarded_ab_analysis as gab
    d = Path(d)
    rec = inst._read(inst._session_path(d, run))
    if rec.get("status") != "run":
        raise SystemExit(f"{run}: 실행되지 않은 시도(유효성은 거부 시점에 확정됨)")
    post = list(rec.get("postflight_invalid_reasons") or [])
    dd, probe_csv = inst.pressure_run_dict(d, run, probe_dir or inst.gp.PROBE_RESULTS_DIR)
    v = gab.run_validity(dd, pressure_frozen(inst, pc.expected_if_hashes()), gab.primary(dd, probe_csv))
    base = [x for x in v["reasons"] if not x.startswith("V3_")]               # CPU 스트레스 전달 확인(V3)은 네트워크에 해당 없음
    chk = (dd.get("summary") or {}).get("network_delivery_check") or {"problems": ["network_delivery_check_missing"]}
    reasons = base + isolation_reasons(chk["problems"]) + [f"N3_{x}" for x in chk["problems"]] + post \
        + ([f"operator_intervention: {operator}"] if operator != "none" else [])
    return {"run": run, "run_id": rec["run_id"], "kind": "pressure", "status": "invalid" if reasons else "valid",
            "valid": not reasons, "invalid_reasons": reasons, **validity_flags(reasons),
            "t_injection_observed_utc": ((dd.get("summary") or {}).get("trial_outcome") or {}).get("t_injection_observed_utc"),
            "network_delivery": {"container_records_present": chk.get("container_records_present"), "n_problems": len(chk["problems"])},
            "evidence_file": rec["evidence_file"], "operator_intervention": operator,
            "rule": "prereg §8.2 B·§9 - 원본 run_validity(V3 제외) + 네트워크 전달 확인(N3), 탐지기 결과 미사용"}


def install(inst, net, hooks=None):
    """run_stage1 인스턴스의 CPU 전용 자리(압박 세션·압박 조건·압박 유효성)를 네트워크 것으로 교체한다. 원본 모듈은 건드리지 않는다."""
    inst.pressure_conditions = lambda: pressure_conditions(inst)
    inst.pressure_frozen = lambda art_hashes: pressure_frozen(inst, art_hashes)
    inst.run_pressure = lambda run, **kw: run_pressure_net(inst, run, net=net, hooks=hooks, **kw)
    orig_validate = inst.validate

    def validate(run, d, *, operator, probe_dir=None):
        if inst.kind_of(run) == "pressure":
            return validate_pressure_net(inst, run, d, operator=operator, probe_dir=probe_dir)
        return orig_validate(run, d, operator=operator, probe_dir=probe_dir)
    inst.validate = validate
    inst.NET_LIVE = net
    return inst
