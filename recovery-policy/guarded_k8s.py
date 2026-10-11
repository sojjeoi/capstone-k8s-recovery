"""guarded 정책의 실제 Kubernetes 연결부(새 파일, 기존 rollouts_client.py 무수정). 모든 외부 호출은 주입된 객체로만 한다
(테스트는 가짜 API·명령 실행기). import만으로는 네트워크·CLI를 호출하지 않는다.

분리된 네 단계:
1) read_view(): Rollout(UID·status.blueGreen active/preview selector·BlueGreenPause)과 Rollout selector 범위의 Pod
   (UID·hash·Ready·종료 중·vllm 컨테이너 재시작 수·IP). API 호출마다 timeout, 전체 조회에 monotonic 기한.
   두 번의 API 조회 사이도 원자적이지 않다(조회 묶음 자체가 순간 스냅샷이 아님).
2) promote_once(): kubectl-argo-rollouts promote를 **정확히 1회** 호출한다. 자동 재시도·API patch 대체 경로 없음.
3) verify_switch(): Rollout UID가 그대로이고 status.blueGreen.activeSelector와 activeService selector가 모두 기대 preview
   hash가 될 때까지 기한 안에서 조회한다.
4) ServiceChecker: 선택자 전환과 별개로 endpoint·새 Pod 요청 수신을 기존 검증 기준으로 확인한다(아래).

결과 계약(promote_once 반환):
  {"called": True, "cli": {...}, "verify": {...}, "verified": True|False|None, "verified_at": ...}
  - CLI 정상 응답 + 선택자 전환 관측 → verified=True. 단, 상태 전환 관측은 우리 명령이 원인이라는 증명이 아니다.
  - CLI timeout·응답 유실(예외) → 전환 관측 여부와 무관하게 verified=None(결과 불명). 관측 사실은 verify.switched에 남는다.
  - CLI 정상 종료(rc 0)인데 기한 안 전환 미관측 → verified=False(미검증).
  - CLI rc≠0 → 승격 미실행의 증거가 아니므로 전환 미관측이면 verified=False(미검증, 재시도 금지).
  - 검증 조회 자체가 끝까지 실패 → verified=None.

남는 경쟁 조건(원자적 보장 없음): 정책의 최종 재조회와 CLI 실행 사이(수 ms~수 s)에 다른 주체가 Rollout을 바꾸면 막을 수 없다.
kubectl-argo-rollouts promote에는 resourceVersion 같은 전제 조건 옵션이 없다. 완화: 최종 재조회 직후 호출, 승격 뒤 Rollout UID·
기대 hash 검증, 단일 기록자·기존 정책 비활성(배포 절차).
"""
import math
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

ROLLOUT_GVR = ("argoproj.io", "v1alpha1", "rollouts")
HASH_LABEL = "rollouts-pod-template-hash"

# 기존 검증 기준 재사용 - 출처: experiments/timed_switch_verdict.py evaluate_new_pod_traffic 기본값
# (settle 10초, endpoint 표본 3개 이상, 새 Pod 30 이상, 옛 Pod 2 이하) + run_timed_switch_recovery_trial.py
# TRAFFIC_WINDOW_SEC=60, SAMPLE_INTERVAL_SEC=5. 값을 새로 고르지 않았다.
REUSED_TRAFFIC_CRITERIA = {"settle_sec": 10.0, "window_sec": 60.0, "sample_interval_sec": 5.0,
                           "min_endpoint_samples": 3, "min_new": 30.0, "max_old": 2.0}


class ViewError(Exception):
    pass


def _utcnow():
    return datetime.now(timezone.utc)


def _g(obj, *path, default=None):
    for p in path:
        if obj is None:
            return default
        obj = obj.get(p) if isinstance(obj, dict) else getattr(obj, p, None)
    return default if obj is None else obj


class K8sAdapter:
    def __init__(self, *, core, custom, namespace: str, rollout_name: str, active_service: str,
                 runner: Callable = subprocess.run, cli: tuple = ("kubectl-argo-rollouts",), container: str = "vllm",
                 cli_timeout_sec: float = 30.0, verify_timeout_sec: float = 10.0, poll_sec: float = 0.5,
                 api_timeout_sec: float = 5.0, read_deadline_sec: float = 10.0,
                 monotonic: Callable = time.monotonic, sleep: Callable = time.sleep, now: Callable = _utcnow):
        self.core, self.custom, self.ns, self.name, self.active_service = core, custom, namespace, rollout_name, active_service
        self.runner, self.cli, self.container = runner, tuple(cli), container
        self.cli_timeout, self.verify_timeout, self.poll = cli_timeout_sec, verify_timeout_sec, poll_sec
        self.api_timeout, self.read_deadline = api_timeout_sec, read_deadline_sec
        self.monotonic, self.sleep, self.now = monotonic, sleep, now

    # ---------- 1) 상태 조회 ----------
    def _rollout(self) -> dict:
        return self.custom.get_namespaced_custom_object(*ROLLOUT_GVR[:2], self.ns, ROLLOUT_GVR[2], self.name,
                                                        _request_timeout=self.api_timeout)

    def read_view(self) -> dict:
        deadline = self.monotonic() + self.read_deadline
        ro = self._rollout()
        if self.monotonic() > deadline:
            raise ViewError("read_deadline_exceeded(rollout)")
        labels = _g(ro, "spec", "selector", "matchLabels", default={})
        if not labels:
            raise ViewError("rollout_selector_missing")
        sel = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        pods = self.core.list_namespaced_pod(self.ns, label_selector=sel, _request_timeout=self.api_timeout)
        if self.monotonic() > deadline:
            raise ViewError("read_deadline_exceeded(pods)")
        bg = _g(ro, "status", "blueGreen", default={})
        view = {"rollout": {"namespace": self.ns, "name": _g(ro, "metadata", "name"), "uid": _g(ro, "metadata", "uid"),
                            "active_selector": bg.get("activeSelector"), "preview_selector": bg.get("previewSelector"),
                            "paused_bluegreen": any(_g(c, "reason") == "BlueGreenPause"
                                                    for c in (_g(ro, "status", "pauseConditions", default=[]) or []))},
                "pods": []}
        for p in _g(pods, "items", default=[]) or []:
            conds = _g(p, "status", "conditions", default=[]) or []
            cs = [c for c in (_g(p, "status", "container_statuses", default=[]) or []) if _g(c, "name") == self.container]
            view["pods"].append({
                "name": _g(p, "metadata", "name"), "uid": _g(p, "metadata", "uid"),
                "hash": (_g(p, "metadata", "labels", default={}) or {}).get(HASH_LABEL),
                "ready": any(_g(c, "type") == "Ready" and _g(c, "status") == "True" for c in conds),
                "terminating": _g(p, "metadata", "deletion_timestamp") is not None,
                "restart_count": _g(cs[0], "restart_count") if len(cs) == 1 else None,
                "ip": _g(p, "status", "pod_ip")})
        return view

    # ---------- 2) 승격 요청(1회) ----------
    def promote_once(self, target: dict) -> dict:
        rollout_uid = target["rollout"]["uid"]
        expected = target["observed"]["preview_hash"]
        started = self.now()
        cli = {"argv": [*self.cli, "promote", self.name, "-n", self.ns]}
        try:
            cp = self.runner(cli["argv"], capture_output=True, text=True, timeout=self.cli_timeout)
            cli.update(returncode=cp.returncode, stdout=(cp.stdout or "")[-500:], stderr=(cp.stderr or "")[-500:])
        except subprocess.TimeoutExpired:
            cli.update(timed_out=True)
        except Exception as e:  # noqa: BLE001 - 실행·응답 유실: 실행 여부 불명
            cli.update(response_lost=True, error=f"{type(e).__name__}: {e}")
        verify = self.verify_switch(rollout_uid, expected)
        # 명령 시간 초과·응답 유실은 전환이 관측돼도 결과 불명이다(명령이 끝까지 무엇을 했는지 모름) - 관측 사실은 verify에 남긴다
        if cli.get("timed_out") or cli.get("response_lost") or verify["switched"] is None:
            verified = None
        else:
            verified = verify["switched"] is True
        return {"called": True, "started_at": started.isoformat(), "cli": cli, "verify": verify, "verified": verified,
                "verified_at": verify.get("switched_at"),
                "note": "상태 전환 관측은 이 명령이 원인이라는 증명이 아니다"}

    # ---------- 3) 승격 검증 ----------
    def verify_switch(self, rollout_uid: str, expected_hash: str) -> dict:
        """전체 확인 기한(verify_timeout_sec)과 개별 API 호출 제한(api_timeout_sec, _request_timeout)은 별개다.
        경계 규칙: 한 번의 조회(Rollout+Service)가 **끝난 직후** 단조 시계를 다시 읽어 그 시각이 기한 이하(≤)일 때만 그 관측을
        판정에 쓴다. 기한을 넘겨(>) 끝난 조회의 관측은 late_observation으로 보존만 하고, 결과는 switched=None(기한 내 검증 실패 -
        결과 불명)이다. 기한 뒤 관측된 전환을 "기한 내 검증 성공"으로 치지 않는다(read_view의 > 규칙과 같음)."""
        start = self.monotonic()
        deadline = start + self.verify_timeout
        info = {"deadline_sec": self.verify_timeout, "api_timeout_sec": self.api_timeout}
        any_read, last = False, None
        while True:
            obs = None
            try:
                ro = self._rollout()
                svc = self.core.read_namespaced_service(self.active_service, self.ns, _request_timeout=self.api_timeout)
                uid = _g(ro, "metadata", "uid")
                act = _g(ro, "status", "blueGreen", "activeSelector")
                svc_hash = (_g(svc, "spec", "selector", default={}) or {}).get(HASH_LABEL)
                obs = {"rollout_uid": uid, "active_selector": act, "service_selector": svc_hash,
                       "observed_at": self.now().isoformat()}
            except Exception as e:  # noqa: BLE001
                last = {"error": f"{type(e).__name__}: {e}"}
            done = self.monotonic()
            info["elapsed_sec"] = done - start
            if obs is not None and done > deadline:
                return {"switched": None, "reason": "verify_deadline_exceeded", "late_observation": {
                    **obs, "switched": obs["rollout_uid"] == rollout_uid and obs["active_selector"] == expected_hash
                    and obs["service_selector"] == expected_hash, "after_deadline_sec": done - deadline}, **info}
            if obs is not None:
                any_read, last = True, obs
                if obs["rollout_uid"] != rollout_uid:
                    return {"switched": False, "reason": "rollout_uid_changed", "last": last, **info}
                if obs["active_selector"] == expected_hash and obs["service_selector"] == expected_hash:
                    return {"switched": True, "switched_at": obs["observed_at"], "last": last, **info}
            if done >= deadline:
                return {"switched": False if any_read else None, "reason": "verify_timeout", "last": last, **info}
            self.sleep(self.poll)


# ---------- 4) 서비스 확인 ----------
def evaluate_new_pod_traffic(*, t_switch, new_pod_ip, endpoint_samples, new_pod_increase, old_pod_increase,
                             settle_sec=10.0, min_new=30.0, max_old=2.0, min_endpoint_samples=3) -> dict:
    """experiments/timed_switch_verdict.evaluate_new_pod_traffic와 같은 판정 논리(교차 동등성 테스트로 확인).
    정책 이미지에 실험 패키지를 넣지 않으려고 같은 논리를 둔다 - 기준 값을 바꾸지 않는다."""
    def fin(v):
        return v is not None and isinstance(v, (int, float)) and math.isfinite(v)
    if t_switch is None or new_pod_ip is None:
        return {"verified": None, "reasons": ["switch_time_or_new_pod_ip_missing"]}
    after = [x for x in endpoint_samples if x["t"] >= t_switch + timedelta(seconds=settle_sec)]
    if len(after) < min_endpoint_samples:
        return {"verified": None, "reasons": [f"endpoint_samples_after_switch={len(after)}"]}
    reasons = []
    if any(sorted(x.get("endpoint_ips") or []) != [new_pod_ip] for x in after):
        reasons.append("active_endpoint_not_only_new_pod")
    if not (fin(new_pod_increase) and fin(old_pod_increase)):
        return {"verified": False if reasons else None, "reasons": reasons + ["request_counter_unavailable"]}
    if new_pod_increase < min_new:
        reasons.append(f"new_pod_requests_{new_pod_increase}<{min_new}")
    if old_pod_increase > max_old:
        reasons.append(f"old_pod_requests_{old_pod_increase}>{max_old}")
    return {"verified": not reasons, "reasons": reasons, "new_pod_increase": new_pod_increase,
            "old_pod_increase": old_pod_increase}


def counter_increase(c0, c1, *, read_at0, read_at1, max_staleness_sec: float, max_future_sec: float = 0.0):
    """수집 유효성 - 판정 함수와 분리. (값, 원본 표본 시각) 쌍 두 개에서 증가량. 누락·NaN·Inf·잘못된 시각·오래된 표본·미래 표본·
    reset(감소)은 None(근거 부족).
    오래됨 허용(max_staleness_sec)과 미래 허용(max_future_sec)은 따로다. 미래 허용 기본값 0 = 시계 오차 근거가 없으면 정책 시각보다
    조금이라도 앞선 표본은 통과시키지 않는다(Prometheus↔정책 Pod 시계 오차를 측정한 뒤에만 0보다 크게 정한다).
    경계: 나이가 정확히 max_staleness_sec이거나, 앞선 정도가 정확히 max_future_sec이면 통과(≤)."""
    reasons = []
    for tag, c, at in (("start", c0, read_at0), ("end", c1, read_at1)):
        if c is None:
            reasons.append(f"counter_missing_{tag}")
            continue
        try:
            v, ts = c
            age = (at - ts).total_seconds()
        except (TypeError, ValueError, AttributeError, OverflowError):
            reasons.append(f"counter_bad_sample_{tag}")
            continue
        vals = list(v.values()) if isinstance(v, dict) else [v]
        if not vals or any(isinstance(x, bool) or not (isinstance(x, (int, float)) and math.isfinite(x)) for x in vals):
            reasons.append(f"counter_not_finite_{tag}")
        elif not math.isfinite(age):
            reasons.append(f"counter_bad_timestamp_{tag}")
        elif age > max_staleness_sec:
            reasons.append(f"counter_stale_{tag}")
        elif -age > max_future_sec:
            reasons.append(f"counter_future_{tag}")
    if reasons:
        return None, reasons
    v0, v1 = c0[0], c1[0]
    if isinstance(v0, dict) or isinstance(v1, dict):  # 계열별(Pod /metrics): 한 계열이라도 감소·사라짐이면 불명
        if not (isinstance(v0, dict) and isinstance(v1, dict)):
            return None, ["counter_shape_mismatch"]
        if set(v0) - set(v1):
            return None, ["counter_series_disappeared"]
        if any(v1[k] < v0[k] for k in v0):
            return None, ["counter_reset"]
        return sum(v1[k] - v0.get(k, 0.0) for k in v1), []  # 끝에 새로 생긴 계열은 0에서 시작한 카운터
    if v1 < v0:
        return None, ["counter_reset"]
    return v1 - v0, []


class ServiceChecker:
    """선택자 전환 확인과 별개로, 전환 뒤 endpoint가 새 Pod IP 하나만 가리키는지·새 Pod가 실제 요청을 성공 처리하는지(카운터
    증가)·옛 Pod로 요청이 거의 가지 않는지를 기존 검증 기준으로 본다. 짧은 확인이며 사용자 품질 개선의 입증이 아니다.
    주의: 옛 Pod는 scaleDownDelaySeconds 뒤 사라진다. 기본 30초면 60초 창 끝의 옛 Pod 카운터가 없어 결과는 근거 부족(None)이 된다
    - 0으로 간주하지 않는다. pilot은 기존 scaleDownDelay 연장(420초) 절차를 함께 써야 판정이 가능하다."""

    def __init__(self, *, endpoints_reader: Callable[[], list], counter_reader: Callable[[str], Optional[tuple]],
                 criteria: dict, max_counter_staleness_sec: float, max_counter_future_sec: float = 0.0,
                 now: Callable = _utcnow, sleep: Callable = time.sleep):
        missing = [k for k in REUSED_TRAFFIC_CRITERIA if k not in criteria]
        if missing:
            raise ValueError(f"서비스 확인 기준 누락: {missing}")
        self.endpoints_reader, self.counter_reader = endpoints_reader, counter_reader
        self.c, self.max_stale, self.max_future = dict(criteria), max_counter_staleness_sec, max_counter_future_sec
        self.now, self.sleep = now, sleep

    def _sleep_until(self, t):
        while True:
            d = (t - self.now()).total_seconds()
            if d <= 0:
                return
            self.sleep(min(d, self.c["sample_interval_sec"]))

    def __call__(self, job: dict) -> dict:
        c = self.c
        t_switch = datetime.fromisoformat(job["verified_at"])
        start = t_switch + timedelta(seconds=c["settle_sec"])
        end = start + timedelta(seconds=c["window_sec"])
        self._sleep_until(start)
        read_fail = []

        def read(role, tag):  # 옛·새 Pod를 서로 독립적으로 읽는다(서비스 selector가 같아져도 Pod 신원으로 읽음)
            try:
                return self.counter_reader(job.get(f"{role}_pod"), uid=job.get(f"{role}_pod_uid"),
                                           ip=job.get(f"{role}_pod_ip"))
            except CounterReadError as e:  # 결과 불명 근거 - 0으로 대체하지 않는다
                read_fail.append(f"{role}_counter_read_failed_{tag}: {e}")
                return None
        # 기준 시각은 카운터 조회가 끝난 뒤 - 조회 중에 새로 scrape된 표본이 '미래'로 오판되지 않게
        new0, old0 = read("new", "start"), read("old", "start")
        at0 = self.now()
        samples, read_errors = [], 0
        while self.now() <= end:
            t = self.now()
            try:
                samples.append({"t": t, "endpoint_ips": sorted(self.endpoints_reader())})
            except Exception:  # noqa: BLE001
                read_errors += 1
            self.sleep(c["sample_interval_sec"])
        new1, old1 = read("new", "end"), read("old", "end")
        at1 = self.now()
        validity = list(read_fail)
        ts = [s["t"] for s in samples]
        if len(set(ts)) != len(ts):
            validity.append("duplicate_sample_times")
        if read_errors:
            validity.append(f"endpoint_read_errors={read_errors}")
        if not samples or (samples[0]["t"] - start).total_seconds() > c["sample_interval_sec"] \
                or (end - samples[-1]["t"]).total_seconds() > c["sample_interval_sec"]:
            validity.append("window_not_covered")
        lim = dict(max_staleness_sec=self.max_stale, max_future_sec=self.max_future)
        new_inc, r1 = counter_increase(new0, new1, read_at0=at0, read_at1=at1, **lim)
        old_inc, r2 = counter_increase(old0, old1, read_at0=at0, read_at1=at1, **lim)
        validity += [f"new_{x}" for x in r1] + [f"old_{x}" for x in r2]
        verdict = evaluate_new_pod_traffic(t_switch=t_switch, new_pod_ip=job.get("new_pod_ip"), endpoint_samples=samples,
                                           new_pod_increase=new_inc, old_pod_increase=old_inc, settle_sec=c["settle_sec"],
                                           min_new=c["min_new"], max_old=c["max_old"],
                                           min_endpoint_samples=c["min_endpoint_samples"])
        ok = verdict["verified"]
        if ok is True and validity:  # 수집이 불완전하면 통과로 보지 않는다
            ok = None
        return {"ok": ok, "verdict": verdict, "validity": validity, "endpoint_samples": len(samples),
                "counter_window_sec": round((at1 - at0).total_seconds(), 3),
                "criteria": c, "note": "선택자 전환과 별개의 짧은 서비스 확인 - 사용자 품질 개선 입증 아님"}


def endpoints_reader_from(core, namespace: str, service: str, api_timeout_sec: float = 5.0) -> Callable[[], list]:
    def read():
        ep = core.read_namespaced_endpoints(service, namespace, _request_timeout=api_timeout_sec)
        return [_g(a, "ip") for s in (_g(ep, "subsets", default=[]) or []) for a in (_g(s, "addresses", default=[]) or [])]
    return read


class CounterReadError(Exception):
    """카운터를 읽지 못함 - 결과 불명 근거(0건으로 대체하지 않는다)."""


NAME_RE = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
NUMBER_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)  # 유니코드 숫자 거부
LABEL_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _parse_sample(line: str):
    """Prometheus 텍스트 형식 표본 한 줄을 끝까지 해석한다(따옴표 안 }·쉼표·이스케이프 처리). 형식이 어긋나면 ValueError.
    exemplar(' # {...}')·여분 토큰은 허용하지 않는다."""
    m = NAME_RE.match(line)
    if not m:
        raise ValueError("no_name")
    name, i, labels = m.group(0), m.end(), {}
    if i < len(line) and line[i] == "{":
        i += 1
        while True:
            while i < len(line) and line[i] == " ":
                i += 1
            if i < len(line) and line[i] == "}":
                i += 1
                break
            km = LABEL_NAME_RE.match(line, i)
            if not km or line[km.end():km.end() + 2] != '="':
                raise ValueError("bad_label")
            k, i, v = km.group(0), km.end() + 2, []
            while True:
                if i >= len(line):
                    raise ValueError("unterminated_label")
                ch = line[i]
                if ch == "\\":
                    if i + 1 >= len(line):
                        raise ValueError("bad_escape")
                    v.append({"n": "\n", "\\": "\\", '"': '"'}.get(line[i + 1], line[i + 1]))
                    i += 2
                elif ch == '"':
                    i += 1
                    break
                else:
                    v.append(ch)
                    i += 1
            if k in labels:
                raise ValueError("duplicate_label_name")
            labels[k] = "".join(v)
            while i < len(line) and line[i] == " ":
                i += 1
            if i < len(line) and line[i] == ",":
                i += 1
            elif i < len(line) and line[i] == "}":
                i += 1
                break
            else:
                raise ValueError("bad_label_separator")
    rest = line[i:].split()
    if len(rest) not in (1, 2) or (len(rest) == 2 and not rest[1].lstrip("-").isdigit()):
        raise ValueError("bad_value_part")
    return name, labels, rest[0]


def parse_request_success(text: str, metric: str = "vllm:request_success_total") -> dict:
    """한 Pod의 /metrics 한 번 응답에서 요청 성공 카운터를 **계열별로** 돌려준다 {라벨 조합: 값}.
    한 응답은 한 Pod의 한 시점이라 수집 경로 중복이 없다. 계열 식별 = 이 metric의 전체 라벨 조합
    (finished_reason·engine·model_name 등). 합산은 증가분 단계에서 계열별 증가를 확인한 뒤에만 한다(counter_increase).
    model_name은 정확히 1개여야 한다(여러 모델이면 불명). 같은 라벨 조합 반복·형식 오류·NaN·Inf·음수·계열 없음은 불명."""
    if text and not text.endswith("\n"):
        raise CounterReadError("truncated_body")
    out, models = {}, set()
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        m = NAME_RE.match(ln)
        if not m or m.group(0) != metric:
            continue
        try:
            _, labels, val = _parse_sample(ln)
        except ValueError as e:  # 대상 계열인데 끝까지 해석되지 않으면 건너뛰지 않고 불명
            raise CounterReadError(f"malformed_sample: {e}") from e
        key = tuple(sorted(labels.items()))
        if key in out:
            raise CounterReadError(f"duplicate_series: {key}")
        if not NUMBER_RE.fullmatch(val):
            raise CounterReadError(f"bad_value: {val!r}")  # 1_0·유니코드 숫자·NaN·Inf 등 실제 exporter가 내지 않는 형식은 불명
        v = float(val)
        if v < 0:
            raise CounterReadError(f"bad_value: {v}")
        models.add(labels.get("model_name"))
        out[key] = v
    if not out:
        raise CounterReadError("metric_missing")
    if len(models) != 1:
        raise CounterReadError(f"multiple_model_series: {sorted(map(str, models))}")
    return out


def pod_metrics_counter_reader(core, namespace: str, *, http_get: Callable, port: int = 8000, container: str = "vllm",
                               metric: str = "vllm:request_success_total", call_timeout_sec: float = 5.0,
                               api_timeout_sec: float = 5.0, now: Callable = _utcnow) -> Callable[..., tuple]:
    """기본 경로: 정책 Pod에서 대상 Pod IP의 /metrics를 직접 읽는다(2026-10-02 V2 vllm_request_counters와 같은 Pod별 읽기,
    pods/proxy 권한 불필요). 호출 제한: API 조회·HTTP 각각 call/api_timeout_sec(Pod별). 전체 확인 기한은 실행기 서비스 대기 기한.
    읽기 전·후로 Pod를 조회해 이름·UID·IP가 승격 때 고정한 값과 같고, 종료 중이 아니며 Running인지 확인한다.
    Pod 없음·교체(UID 변화)·종료 중·IP 변화·컨테이너 재시작·읽기 실패·잘못된 응답이면 CounterReadError(결과 불명).
    반환 (계열별 값, 읽기 완료 시각).
    주의(2026-10-06 검토): 시작 카운터 읽기(두 Pod)가 sample_interval_sec보다 오래 걸리면 첫 endpoint 표본이 늦어
    window_not_covered(결과 불명)가 된다 - 안전 쪽이며 기준은 바꾸지 않는다. requests timeout은 소켓 동작 단위라 총 시간 상한이
    아니다 - 총 상한은 실행기 서비스 대기 기한(120초)이다."""
    first_start = {}  # (이름, UID) -> 처음 본 컨테이너 재시작 수·시작 시각(같은 Pod 안 재시작 감지)

    def pod_state(name, uid, ip):
        try:
            pod = core.read_namespaced_pod(name, namespace, _request_timeout=api_timeout_sec)
        except Exception as e:  # noqa: BLE001 - 404 포함
            status = getattr(e, "status", None)
            raise CounterReadError("pod_missing" if status == 404 else f"pod_read_failed: {type(e).__name__}") from e
        if _g(pod, "metadata", "uid") != uid:
            raise CounterReadError(f"pod_replaced: uid={_g(pod, 'metadata', 'uid')}")
        if _g(pod, "metadata", "deletion_timestamp"):
            raise CounterReadError("pod_terminating")
        if _g(pod, "status", "phase") != "Running":
            raise CounterReadError(f"pod_not_running: {_g(pod, 'status', 'phase')}")
        pip = _g(pod, "status", "pod_ip")
        if pip != ip:
            raise CounterReadError(f"pod_ip_mismatch: {pip} (want {ip})")
        cs = [c for c in (_g(pod, "status", "container_statuses", default=[]) or []) if _g(c, "name") == container]
        if len(cs) != 1:
            raise CounterReadError(f"container_status_missing: {container}")
        life = (_g(cs[0], "restart_count"), str(_g(cs[0], "state", "running", "started_at")))
        if first_start.setdefault((name, uid), life) != life:
            raise CounterReadError(f"container_restarted: {first_start[(name, uid)]} -> {life}")
        return pip

    def read(name: str, uid: Optional[str] = None, ip: Optional[str] = None):
        if not name or not uid or not ip:
            raise CounterReadError("pod_identity_missing")
        pip = pod_state(name, uid, ip)
        try:
            r = http_get(f"http://{pip}:{port}/metrics", timeout=call_timeout_sec, allow_redirects=False)
        except Exception as e:  # noqa: BLE001 - 시간 초과·연결 실패
            raise CounterReadError(f"metrics_read_failed: {type(e).__name__}") from e
        if r.status_code != 200:
            raise CounterReadError(f"metrics_http_{r.status_code}")
        value = parse_request_success(r.text, metric)
        pod_state(name, uid, ip)  # 읽는 동안 교체·종료되지 않았는지 다시 확인
        return value, now()
    return read
