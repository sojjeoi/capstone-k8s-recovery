# Phase 8 BlueGreen Preflight + NodeNotReady 사건 기록 (2026-09-16)

> 3-arm 파일럿(`native`/`fixed_threshold`/`proposed`) 시작 전 Phase 7 인프라
> (Argo Rollouts BlueGreen promotion 경로)가 실제로 살아있는지 확인하려던
> preflight 도중 노드 장애가 발생했다. Promotion 자체는 preflight를 통해
> 끝까지 성공 검증했지만, 그 과정에서 발견한 리소스 문제 때문에 3-arm
> 파일럿은 보류한다. **후속(2026-09-17 새벽)**: `pod_kill` native E2E 실행
> 전 preflight에서 같은 사건의 잔존 영향으로 보이는 pod 간 네트워크
> 간헐적 이상을 발견해 주입을 다시 보류했다 — §5 참고. **후속2(2026-09-17
> 밤)**: 진단 통과 후 pod_kill native 하니스 버그(§6) 발견·수정을 거쳐
> `pod_kill native 경로 E2E 완료`(§7) - 단, 본 실험 전 타임스탬프 의미
> 보완이 남아 있다(§7.4). 3-arm 파일럿은 여전히 노드 CPU headroom 문제
> (§3.2) 해결 전까지 보류. **후속3(2026-09-18)**: §7.4/§7.5 타임스탬프
> v2를 구현한 뒤, 완성도 점검에서 나온 문서 불일치·`network_degrade`
> 어댑터 설계 요청에 대응했다 — §8 참고. 실클러스터 작업은 여전히 없음
> (v2 실측 재검증·network_degrade 실제 실행 모두 다음 세션 이후로 보류).
> `network_degrade` 오프라인 구현·집계 경로(comparison.csv 연동 포함)는
> §8.7에서 완료됐다 — §8.8에서 다음 세션 순서를 확정: tolerant probe
> calibration은 overlay가 새 preview vLLM을 띄워 CPU headroom 문제(§3.2)를
> 재발시킬 수 있어, AllInjected 최소강도 파일럿(overlay 미적용, 지금도
> 가능) → CPU headroom 해결·콜드스타트 3회 확인 → tolerant calibration
> 순서로 미룬다.

## 1. Preflight 체크리스트 — 전부 통과

| 항목 | 결과 |
|---|---|
| Rollout Healthy/Stable, active·preview 파드 모두 Ready | ✓ |
| `vllm-active`/`vllm-preview` selector가 의도한 ReplicaSet을 각각 가리킴 | ✓ |
| recovery-policy `/healthz` 정상 | ✓ |
| 활성 experiment context 없음(`GET /admin/experiment-run` → `null`) | ✓ |
| cooldown 초기화 가능(`POST /admin/reset-cooldown`) | ✓ |
| critical `vllm-serving` 알림 없는 quiescent 상태 | ✓ |
| recovery-policy ServiceAccount로 `rollouts`/`rollouts/status` get·patch 가능(RBAC) | ✓ |
| Alertmanager → recovery-policy `/webhooks/alertmanager` 라우팅(severity=critical) 정상 | ✓ |
| `fixed_threshold.py`(`detector="fixed_threshold"`)와 `score_server.py`(`detector="isolation_forest"`)가 서로 다른 detector 값 전송 | ✓ |

## 2. Promotion E2E 테스트 — `PREFLIGHT-EXCLUDED`

실제 promotion을 한 번 끝까지 실행해 "API 응답 성공"이 아니라 "서비스 selector
전환 + 실제 요청 성공"까지 확인했다. Phase 8 본 실험 5회 반복이나 3-arm
파일럿 데이터에는 포함하지 않는다.

| 항목 | 값 |
|---|---|
| 구분 | `PREFLIGHT-EXCLUDED` |
| `run_id` | `preflight-promote-test-20260916T093822Z` |
| 시나리오 | 합성 `anomaly_risk` 신호(`score=0.99`, `detector=isolation_forest`) 직접 `/signal` 전송 - 실제 chaos 주입 없음 |
| 승격 전 selector | `active=5fbc484886`, `preview=7b98b55c65` |
| 승격 후 selector(직접 재확인) | `active=7b98b55c65`, `preview=7b98b55c65` |
| recovery-policy 응답 | `outcome=executed_verified`, `result.method=cli`, `result.verified=true`, `result.stdout="rollout 'vllm-serving' promoted"` |
| 감사기록(`outbox.json`) | `record_id=4682fe94-80a9-47ba-95f0-f08630dc23f1`, `run_id` 일치, `status=pushed`, **`commit_sha=67ce4cf2f25e15c3e55ced666085584ff320e4d2`** |
| 승격 후 실제 요청 | `POST /v1/completions` → 200, 정상 completion 응답(recovery-policy pod 내부에서 확인 - 새로 만든 pod는 네트워크 미정착으로 오탐 발생, 기존 warm pod로 재확인) |
| 정리 | `POST /admin/experiment-run/clear` 성공, quiescent 유지 |

**결론: 예측 신호 → 정책 판단 → CLI promotion → 실제 selector 전환 → 감사기록
커밋까지 전체 경로가 실측으로 검증됨.**

## 3. NodeNotReady 사건

### 3.1 타임라인(UTC)

- **약 08:58** — `blue_green_prep.prepare_preview()`로 두 번째 vLLM 리비전(preview, `7b98b55c65`) 생성 시작. 기존 active(`5fbc484886`)는 계속 서빙 중이었음 - 즉 이 구간은 두 vLLM 인스턴스가 노드에 동시 존재하는 상태.
- **09:11:55 ~ 09:14:35** — kubelet이 `"invalid bearer token, service account token has been invalidated"` 에러를 반복 기록. 같은 구간에 containerd도 `context deadline exceeded`, `ttrpc: received message on inactive stream`, `collecting metrics ... cgroups: cgroup deleted` 에러 다수.
- **~09:1x** — `kubectl get nodes`에서 `sj-worker`가 `NotReady`로 관측됨. 노드 컨디션 메시지: `NodeStatusUnknown - Kubelet stopped posting node status`.
- **09:14:40 이후** — kubelet 로그가 정상 동작(컨테이너 정리, 볼륨 마운트, 새 파드 기동)으로 복귀.
- **09:14:48** — Rollout이 `BlueGreenPause`에 진입(preview Ready 확인). preview 파드는 이 과정에서 재생성됨(`-296xx` → `-6gjzt`).
- **동일 구간** — 기존에 8일간 안정적으로 떠 있던 active vLLM 파드(`-zdrmv`)와 recovery-policy 파드(`-kvkdn`)도 **재생성**됨(`-2wbl8`, `-5hmvj`; recovery-policy는 `RESTARTS=1` 기록). preview뿐 아니라 기존 워크로드도 영향을 받았다.
- **09:21 ~ 09:35** — 12분간 안정화 관찰(90초 간격 9회): 노드 `Ready` 9/9, active·preview 파드 재시작 0건, 합성 요청 7/9 성공(latency 5.6~10.55s, 실패 2건은 매번 새로 만든 테스트 파드 자체의 네트워크 미정착 - timeout/DNS 실패 패턴). `invalidated` 에러 이후 0건, IO pressure avg10 20.54 → 0.50로 하락.

### 3.2 원인 — **미확정**

**두 번째 vLLM 콜드스타트(모델 로딩·컴파일)와 CPU·I/O 부하 증가, containerd
응답 지연, kubelet heartbeat 중단이 같은 시간대에 발생했다는 강한 시간적
연관성은 확인됐으나, 직접적인 인과관계는 확정하지 못했다.**

- 오늘 `dmesg` 상 OOM-kill 없음(발견된 OOM 로그는 9/4, 12일 전 별도의
  memStress chaos 테스트 흔적).
- 디스크 공간·inode 여유 충분, 디스크 자체 장애 정황 없음.
- kubelet·containerd 서비스(`systemctl is-active`)는 사건 도중에도 `active` -
  프로세스 자체가 죽은 건 아니었음.
- **CPU limit 합계가 8/8코어(100%)이므로 시스템 프로세스에 보장된 CPU
  headroom이 없는 구성이다.** limit 합계가 100%라고 시스템 프로세스가
  CPU를 전혀 못 쓰는 건 아니지만, 두 vLLM이 동시에 한도까지 쓰면 kubelet
  등에 안정적으로 남는 여유가 없다는 뜻이다(`vllm-serving` Rollout 컨테이너
  `resources.limits.cpu: "4"` × 2).

### 3.3 복원 확인

정리 조치 없이 BlueGreen controller가 `scaleDownDelaySeconds: 30` 정책에
따라 자동으로 이전 리비전을 정리했다. 최종 상태:

- Rollout `phase=Healthy`, `active=preview=7b98b55c65`(단일 리비전으로 자연
  수렴 - 사건 이전과 동일한 형태의 정상 상태)
- `vllm-serving` 파드 1개만 남음(`Running`)
- 노드 `Ready`, quiescent(`active_count=0`)

## 4. 다음 세션 재개 순서

**막힌 건 Phase 8 전체가 아니라 preview가 필요한 실클러스터 3-arm 실행뿐이다**
- 코드·집계기 작업(1~2번)은 노드 문제와 무관하게 바로 진행 가능하다.

1. **`collect_metrics.py` 최소 버전 구현**(파일럿 전 필수) — **완료**(2026-09-16, 커밋 `adcf169`)
   - 스키마 검증, 누락 타임스탬프 처리
   - `is_pilot`/`invalid_run`/`PREFLIGHT-EXCLUDED`를 집계에서 구조적으로 제외
   - arm·scenario·반복 수 검증
   - timing 순서 오류와 "t_SLO 자체가 안 남" 상황을 구분
   - `comparison.csv` 생성
2. **`pod_kill` native 경로 E2E 검증** — 코드+오프라인 테스트는 **완료**(2026-09-16,
   `pod_kill_adapter.py` + `test_pod_kill_adapter.py` 5종, 커밋 `7288eb0`). 이
   항목까지 통과해야 "pod_kill adapter 완료"가 아니라 **"pod_kill native 경로
   E2E 완료"**로 표시할 수 있다. 통과 기준:
   - 단일 active revision, preview 없음
   - Node가 사전 10~15분 동안 계속 Ready
   - 기존 pod 이름·UID가 결과에 기록됨
   - Chaos CR 생성 후 기존 UID 소멸 확인
   - replacement pod가 새 UID로 생성되고 Ready
   - `t_injection`(관측 기반, `injection_observation_error_sec` 포함)·`t_SLO`·`t_recovery`가 논리적인 순서로 기록
   - native arm에서는 promotion·recovery-policy action·감사 commit이 발생하지 않음
   - Chaos CR과 experiment context가 모두 정리됨
   - 종료 후 Node Ready, quiescent, completion 요청 정상
   - 실행은 `is_pilot=true`로 본 실험에서 제외

   실행 중에는 Node Ready 상태, kubelet/containerd 오류, CPU·I/O pressure를
   별도로 관찰한다(§3.2의 CPU headroom 문제가 이 정도 부하에서도 재현되는지
   함께 확인하는 목적을 겸함).
3. `network_degrade` 순수 열화판과 adapter 확정
4. `memory_pressure` adapter 연결
5. **노드 CPU headroom 문제 해결** + 콜드스타트 3회 안정성 검증
   - 콜드스타트 순간 실제 CPU·I/O 사용량·지속시간 정밀 확인
   - vLLM CPU request/limit vs 노드 allocatable 재대조
   - 선택지: 노드 증설(기존 SLO·ramp 변경 최소) / vLLM CPU 축소(SLO v2·ramp 재보정 필요) / 워커 분산(토폴로지 자체가 달라짐)
6. **자원 구성이 바뀌었다면 재보정** — CPU limit 변경·노드 증설·워커 분산 전부
   probe baseline·load-ramp 곡선에 영향을 줄 수 있다. 최소 재검증하고, 차이가
   크면 SLO v3로 명시하고 다시 동결한다(v1→v2 폐기와 같은 원칙).
7. **파일럿 실행**(모두 `is_pilot=true`)
   - `load_ramp × 3 arms × 1` — 예측 경로
   - `pod_kill × 3 arms × 1` — 반응 경로
8. 파일럿 통과 후 `run_all_scenarios.py`(interleaved 실행)
9. 전체 60회 실행 + 최종 집계

**`pod_kill` 파일럿 해석 시 주의**: `proposed`가 `native`보다 반드시 빨라야
한다고 가정하면 안 된다. 전조 없는 돌발 장애라 K8s 기본 self-healing이
더 빠르거나 비슷한 게 정상적인 예상 결과다. 이 파일럿의 핵심은 우열이
아니라 Alertmanager 반응 경로·타임스탬프·cleanup·집계가 정확히 연결되는지
확인하는 것이다.

다음 세션 첫 구현 작업 = `collect_metrics.py`, 첫 인프라 작업 = 노드
headroom 해결. (둘 다 이후 상황 갱신: `collect_metrics.py`는 완료, `pod_kill`
어댑터 코드+오프라인 테스트도 완료 — 남은 인프라 작업은 §5 참고.)

## 5. pod_kill 실행 전 진단 — pod 간 네트워크 간헐적 이상 (2026-09-17 새벽, 읽기 전용)

`pod_kill × native × 1회` 실행 전 preflight 중 `recovery-policy → vllm-active`
경로에서 완전한 요청 실패(8~10초 타임아웃)를 발견해 주입을 보류하고,
변경·재시작 없이 증거만 수집했다(2026-09-16 약 23:13 KST(≈14:13 UTC)부터
2026-09-17 새벽까지, 클러스터 시각 기준).

**용어 정정(2026-09-17)**: 처음엔 "Service 라우팅 문제"로 좁혀 적었으나
부정확하다 - ClusterIP 직접 요청과 Pod IP 직접 요청을 **같은 실패 순간에**
같이 측정한 적이 없어서, kube-proxy/Service 경로로 범위를 좁힐 근거가
아직 없다. Pod IP 요청은 kube-proxy·Service를 우회하므로, 그게 같이
실패하는지가 원인 범위를 가르는 핵심 관측인데 아직 미확인이다. 그래서
"Service 경로 문제"가 아니라 더 넓은 **"pod 간 네트워크 간헐적 이상"**으로
표현을 바꾼다 - §5.3에 세 경로를 같은 시간축에서 동시 측정하는 방법을
남겨 낮에 이걸로 원인 범위를 실제로 좁힌다.

### 5.1 결론 요약

| 항목 | 결론 |
|---|---|
| `etcdInsufficientMembers`(critical) 등 control-plane alert 5종 | **실제 etcd/control-plane 장애 아님 - 원인 확정(2026-09-17)**: `kube-scheduler`/`kube-controller-manager` 커맨드에서 `--bind-address=127.0.0.1` 직접 확인(etcd는 어제 `--listen-metrics-urls=http://127.0.0.1:2381`로 이미 확인) - kubeadm 기본 하드닝으로 애초에 다른 pod(Prometheus)에서 못 읽는 구조. `lastError`도 전부 `connection refused`(타임아웃 아님 - 포트 자체가 그 주소에 안 열려 있다는 뜻)로 일치. **어제 사건과 무관** - `startsAt`이 사건 구간과 겹쳐 보인 건 Prometheus 자신도 그때 재생성된 pod라 재기동 후 처음 평가한 시점이 우연히 겹친 것. `vllm-serving` 자체 타겟(`vllm-active`/`vllm-preview`)은 별개로 계속 `up`(§5.4에서 재확인, 아래) |
| `recovery-policy → vllm-active`(ClusterIP 경유) | **간헐적 실패** — 완전히 깨진 것도 완전히 정상인 것도 아님. 같은 요청이 연속 5/5 타임아웃 → 곧바로 연속 13/13 성공. 원인 범위(kube-proxy 단독인지, 더 넓은 pod-네트워크 문제인지)는 아직 미확정 — Pod IP 직접 요청을 같은 실패 순간에 비교한 적이 없다(§5.3) |
| `vllm-serving` pod/vLLM 프로세스 자체 | 정상 — pod 내부 `localhost:8000/health`는 항상 즉시 200 |
| 12일 된 `PodChaos/vllm-pod-kill` | 무해(one-shot, 이미 발동 완료, 대상 pod도 이미 없음) — YAML·status 보존(§5.2), 정리는 낮에 |

### 5.2 증거 (요청하신 8개 항목)

**Service/Endpoints/EndpointSlice**: `vllm-active` ClusterIP `10.101.143.197:8000`,
selector `app=vllm-serving,rollouts-pod-template-hash=7b98b55c65`. Endpoints·
EndpointSlice 모두 `10.244.36.2`(현재 pod) 하나만 `ready=true, serving=true,
terminating=false`로 정상 등록. `last-change-trigger-time: 2026-09-16T09:14:47Z`
(오늘 사건 복구 시점과 일치).

**recovery-policy에서 DNS 해석**: `python3 -c "socket.gethostbyname(...)"` →
`10.101.143.197`(Service ClusterIP와 정확히 일치, 실패 없음).

**recovery-policy에서 ClusterIP/Pod IP 직접 요청**: 두 경로 모두 성공(각각
`200, ~2ms`) — 단, 이 측정은 **성공 구간**에서만 한 것이고 실패가 재현된
순간에 두 경로를 나란히 찍어본 적은 없다. 그래서 이 결과만으로 "Pod IP는
항상 되고 ClusterIP만 가끔 실패한다"를 주장할 수 없다(§5.3).

**kube-proxy(sj-worker) 로그**: 2026-09-04 최초 기동 이후 조용하다가, **2026-
09-16 08:53:19 UTC**부터 `watch of *v1.Service ended ... http2: client
connection lost`, 이어서 **08:54:39~09:02:39 UTC** 구간에 API 서버
(`https://192.168.30.4:6443`) 대상 `TLS handshake timeout`으로 Service/
EndpointSlice list·watch가 반복 실패. **09:02:39 UTC 이후로는 로그가 한
줄도 없음**(최근 150줄 기준, 지금까지 6시간 이상). 사건 시작(09:11:55Z)
**직전**부터 실패가 시작돼 사건과 시간적으로 이어져 있다는 점은 사실이다.
다만 **로그 침묵 자체를 "정지"의 증거로 쓰지 않는다** - kube-proxy는
Service/Endpoint 설정에 변화가 없으면 원래도 로그를 거의 안 남기므로,
몇 시간 조용한 게 그 자체로 비정상이라는 근거는 못 된다. 즉 08:53~09:02
구간의 반복 실패 기록은 사건과의 시간적 연관성을 보여줄 뿐, "그 이후
계속 멈춰 있다"는 결론까지 로그만으로는 낼 수 없다 - kube-proxy pod
자체의 Ready·재시작 횟수·health endpoint 확인이 낮에 별도로 필요하다
(§5.4).

**Calico(sj-worker) 로그**: 최근 로그에 BPF/XDP 상태 초기화 관련 에러
(`libbpf: Error loading .BTF...`, `failed to wipe the XDP state`)가 반복
등장(끝이 `try=0`이라 재시도 루틴으로 보임, 마지막 줄 타임스탬프
`2026-09-16 15:45:56`). 원인·심각도는 확인하지 못했다 - eBPF 데이터플레인
초기화 루틴의 정상 재시도일 수도, 지속 중인 이상일 수도 있어 단정하지
않는다.

**API 서버 `/readyz?verbose`**: 전 항목 `ok`(`etcd ok`, `etcd-readiness ok`
포함) - API 서버 관점에서 etcd는 완전히 정상.

**etcd 상태**: `/healthz/etcd`는 이 K8s 버전(v1.31.14)에서 404(폐기됨,
`/readyz`가 대체) - 예상된 결과. `etcd-sj-control` pod `Ready=True`,
`Restart Count=1`이나 `Started` 시각이 최초 생성(12일 전)과 거의 동일해
오늘 사건과 연관된 재시작으로 보이지 않는다.

**현재 알림 목록(전체)**: `etcdInsufficientMembers`(critical),
`etcdMembersDown`(warning), `KubeProxyInstanceUnreachable`(warning, sj-control
+ sj-worker 인스턴스 둘 다), `KubeSchedulerInstanceUnreachable`(warning),
`KubeControllerManagerInstanceUnreachable`(warning), `TargetDown`(warning,
kube-etcd/kube-proxy/kube-scheduler/kube-controller-manager 각각) - **전부
`startsAt`이 09:07:26~09:18:15 UTC 사이**(사건 구간과 정확히 겹침)이고
지금까지(최소 15:49 UTC까지 확인) 계속 `active`. `Watchdog`(정상, 상시
발화용)만 무관. `vllm-serving` 관련 critical alert는 없음(`/admin/quiescent`
결과와 일치).

**`PodChaos/vllm-pod-kill`**: `creationTimestamp: 2026-09-04T10:34:04Z`,
`mode: one`(재발동 없음), `status.experiment.containerRecords[0]`이 그날
`vllm-serving-6c769cc5c5-l5cr9`(현재와 다른 옛 리비전) 대상으로 1회
`injectedCount=1` 기록 후 정지. `AllRecovered=False`는 Chaos Mesh가 완료된
one-shot CR을 자동 GC하지 않아서 남은 상태 표시일 뿐, 재발동을 의미하지
않는다.

### 5.3 원인 범위를 좁히는 방법 — 세 경로 동시 비교(낮에 실행, 아직 미실행)

어젯밤 관측은 경로를 하나씩, 다른 시점에 찍은 것이라 "어느 구간에서
실패하는지"를 가르지 못한다. 낮에는 아래 세 경로를 **같은 시간축에서
동시에**(같은 반복문 안에서 번갈아, 예: 1초 간격으로 ClusterIP→PodIP→
localhost 순으로 계속 반복) 측정한다:

- `recovery-policy` → ClusterIP(`10.101.143.197:8000`)
- `recovery-policy` → Pod IP(`10.244.36.2:8000`, kube-proxy·Service 우회)
- `vllm-serving` pod 내부 → `localhost:8000`

실패가 재현되는 순간의 조합으로 분류한다:

| 실패 패턴 | 원인 범위 |
|---|---|
| ClusterIP만 실패, Pod IP·localhost 성공 | kube-proxy/iptables(DNAT 규칙) |
| ClusterIP·Pod IP 동시 실패, localhost는 성공 | Calico·veth·conntrack·노드 네트워크(kube-proxy 단독 아님) |
| 세 경로 모두 실패 | vLLM 자체 또는 노드 전체 정체 |
| `recovery-policy`에서 보내는 요청만(대상 무관하게) 실패 | 그 pod의 네트워크 namespace·소켓·클라이언트 쪽 문제 |

이번에 확보한 §5.2 증거는 이 표의 어느 칸에도 아직 확실히 채울 수 없다 -
그래서 원인을 단정하지 않고 이 비교표를 §5.4의 첫 실행 항목으로 남긴다.

### 5.4 낮 시간대 작업 순서(말씀하신 우선순위 그대로)

1. ~~`etcdInsufficientMembers` 원인 확인~~ **완료(2026-09-17)**: `kube-scheduler`/
   `kube-controller-manager`/etcd 전부 `--bind-address`·`--listen-metrics-urls`가
   `127.0.0.1`로 확인 - kubeadm 기본값, 어제 사건과 무관한 구조적 조건(§5.1).
   `vllm-serving` 자체 타겟(`vllm-active`/`vllm-preview`)은 Prometheus API로
   직접 확인 결과 `health=up`, `lastError` 없음, 최근 스크레이프 성공 -
   Phase 8이 쓰는 metrics 경로는 영향 없음. **해결 불필요(원래도 정상)**,
   control-plane 4종 타겟은 원하면 별도로 kube-prometheus-stack의
   `--bind-address`를 열거나 관련 alert를 무시 처리하면 되지만 Phase 8
   진행에는 영향 없다.
2. **§5.3의 세 경로 동시 비교부터 실행** - "복구"보다 "재현 후 원인 특정"이
   먼저다. 실패가 재현되는 순간마다 다음을 같이 기록:
   - curl `%{time_connect}`(TCP 연결까지)와 `%{time_starttransfer}`/
     `%{time_total}`(응답까지)을 분리해서 찍는다 - TCP connect 단계에서
     막히는지, 연결은 되고 응답 대기에서 막히는지에 따라 원인 범위가
     크게 갈린다.
   - `recovery-policy` pod 내부 TCP 상태(`ss -tn` 등)로 `TIME_WAIT` 누적
     여부 확인
   - conntrack 현재 사용량·최댓값(`conntrack -C`/`-M` 또는
     `/proc/sys/net/netfilter/nf_conntrack_{count,max}`)과 insert/drop
     관련 카운터
   - sj-worker 커널 로그(`dmesg`)에 `nf_conntrack: table full` 여부
   - kube-proxy pod Ready 상태·재시작 횟수·health endpoint(`/healthz`,
     보통 10256 포트) 확인 - 로그 침묵만으론 정지 여부를 판단 못 하므로
     이 확인이 필수(§5.2 정정 참고)
   - Calico/Felix 상태(`calicoctl node status` 또는 felix 로그의 현재
     상태 라인)와 veth 인터페이스 drop/error 카운터(`ip -s link`)
   - `iptables-save`로 해당 ClusterIP의 DNAT 규칙이 현재 pod IP
     (`10.244.36.2`)를 가리키는지 직접 확인(원래 계획, 위 비교와 병행)
3. 오래된 `PodChaos/vllm-pod-kill` 정리(`kubectl delete podchaos vllm-pod-kill -n vllm-serving`) - YAML·status는 위 §5.2에 보존됨
4. 정상 상태 최소 10~15분 유지 확인(위 세 경로 반복 요청 실패율 0%, Node Ready 유지)
5. 그 다음에만 `pod_kill × native × 1회` 재시도(§4의 pass 기준 그대로 적용)

또한 향후 실험 preflight에 `cluster_healthy`(control-plane 전체 alert +
pod 간 네트워크 반복 검증) 검사를 `/admin/quiescent`(vllm 전용)와 별도로
추가하는 것을 권장 - `quiescent=true`가 클러스터 전체 정상을 보장하지
않는다는 점이 이번에 확인됐다.

**§5.4 진행 결과(2026-09-17 저녁)**: 정리 전/후 각 15분(총 30분, 1,173회)
ClusterIP·Pod IP·localhost 요청 전부 성공, 실패 0건. `etcdInsufficientMembers`
원인도 확정(§5.1 갱신 - `kube-scheduler`/`kube-controller-manager`
`--bind-address=127.0.0.1` 직접 확인, `vllm-serving` 자체 Prometheus 타겟은
`up`). `PodChaos/vllm-pod-kill` 삭제 완료. Node Ready·pressure 정상. 8개
실행 허용 기준 전부 통과해 `pod_kill × native × 1회`를 실행했다 - 결과는
§6.

## 6. pod_kill native 1차 실행 — 하니스 버그 발견 및 수정 (2026-09-17 저녁)

### 6.1 1차 실행 결과가 잘못됐음을 발견

`pilot-pod_kill-native-01-20260917T104638Z` 실행 결과 `outcome=prevented`
(SLO 위반 없이 예방됨)로 기록됐으나, 다음 근거로 **오판정임을 확인**:

- 관측 구간(`t_injection_end` → `t_run_end`)이 **37초**에 불과 - `slo_judge`의
  `WINDOW_SEC`(60초) warmup보다 짧다.
- `load_ramp_adapter.make_load_ramp_prober().check_slo_violation()`은
  warmup 전엔 probe raw CSV를 아예 읽지 않고(`_refresh()` 미호출) `False`만
  반환 - 즉 이번 trial은 probe 데이터를 **단 한 번도 평가하지 않은 채**
  "위반 없음"으로 종료됐다.
- 클러스터에서 직접 확인: 당시 replacement pod(`vllm-serving-7b98b55c65-
  k9xzz`)는 trial 종료 시점 기준 **여전히 `0/1 Not Ready`**(트라이얼 종료
  약 3분 후에도 미기동) - 서비스가 실제로 다운된 상태에서 "prevented"가
  선언됐다.
- Node·kubelet/containerd는 병행 감시 15초 간격 14회 전부 정상(`Ready=True`,
  `MemoryPressure=False`) - 인프라 문제가 아니라 순수 하니스 타이밍 버그.

**근본 원인**: `pod_kill_adapter.py`의 `is_done()`은 대상 소멸이 확인되는
즉시 `True`가 된다(pod-kill은 순간 액션이므로 그 자체로는 맞는 설계). 하지만
`run_once()`의 OBSERVING 루프는 `t_injection_end`가 찍히면 `t_slo is None`인
것만으로 곧장 "prevented"로 조기 종료했다 - 이 전제는 `load_ramp`(주입
지속시간 자체가 450초라 `t_injection_end` 시점엔 이미 관측이 충분함)에는
맞지만, 즉발 주입인 `pod_kill`에는 정반대로 작동한다(위험 구간이 주입
"이후"의 회복 과정인데, `t_injection_end`가 그 위험 구간이 "시작"하는
순간에 찍힘).

### 6.2 지적받은 추가 결함과 수정 범위

최초 제안한 수정(`min_observation_sec` 시간 바닥 하나만 추가)은 불충분하다는
지적을 받았다 - `check_slo_violation()`의 `False`가 "정상"과 "아직 판정
불가"를 구분 못 하는 게 본질적 문제이므로, 시간만 늦춘다고 데이터가 실제로
확보됐다는 보장은 안 된다. 최종 수정:

- **`slo_judge.py`**: 매직넘버 `20`(P95를 실제 백분위수 대신 `max()`로
  근사하는 표본 수 경계)을 `MIN_SAMPLES_FOR_RELIABLE_P95` 명명 상수로
  추출.
- **`run_once.py`**: `Prober`에 선택 필드 `is_slo_evaluable()` 추가 -
  `check_slo_violation()`의 `False`가 COMPLIANT(정말 위반 없음)인지
  NOT_EVALUABLE(판정 근거 부족)인지 구분. `run_once()`에
  `min_observation_sec: float = 0.0`(기본값 - 기존 호출자는 동작 불변)
  파라미터를 추가해 "prevented" 조기 종료를 `t_slo is None and evaluable
  and (경과시간 >= min_observation_sec)`로 게이트. **POSITIVE 위반 감지는
  evaluable 여부와 무관하게 항상 그대로 신뢰**(이 게이트는 "위반 없음"
  결론에만 적용). 결과에 `slo_evaluable_at_exit: Optional[bool]` 필드 추가
  - prevented 확정 시에만 채워짐, 이 필드가 없는(과거) 기록과 새로 검증된
    기록을 구분하는 용도.
- **`load_ramp_adapter.py`**: `make_load_ramp_prober()`에 `is_slo_evaluable()`
  구현 - `_refresh()`가 실제로 읽은 원시 표본 수(`cache["sample_count"]`)가
  `slo_judge.MIN_SAMPLES_FOR_RELIABLE_P95` 이상인지로 판정(단순 경과시간이
  아니라 **실제 확보된 유효 데이터**를 기준으로 함 - probe fetch가 간헐적으로
  실패해 표본이 적으면 시간이 지나도 NOT_EVALUABLE 유지). `check_slo_
  violation()`의 기존 내부 warmup 게이트(`_warmed_up()`)는 그대로 둬서
  `load_ramp`의 이미 확정·재현성 검증된 동작을 전혀 건드리지 않았다.
- **`run_pod_kill_trial.py`/`run_load_ramp_trial.py`**: `min_observation_sec=
  slo_judge.WINDOW_SEC`를 명시적으로 전달(상수 중복 정의 안 함). load_ramp는
  이미 자연 발생하는 여유(450초)가 60초보다 훨씬 커서 실질적 동작 변화
  없음.
- 회귀 테스트 3개 추가(`test_run_once.py`): NOT_EVALUABLE인 동안 prevented
  조기 종료 차단, `min_observation_sec` 시간 바닥 적용, NOT_EVALUABLE
  상태에서도 실제 위반은 그대로 감지. 기존 29개 전부 그대로 통과(32 passed,
  2 skipped) - `load_ramp`/기존 시나리오 동작 불변 확인.

### 6.3 1차 실행 결과 사후 재분류

`trial-pilot-pod_kill-native-01-20260917T104638Z.json`(gitignore 대상,
로컬 파일)을 원본 타임스탬프는 그대로 두고 다음만 정정:
`outcome: prevented → invalid_run`, `state: completed → invalid`,
`invalid_reason: null → "observation_ended_before_slo_window_became_
evaluable"`, `slo_evaluable_at_exit: false` 추가, `notes`에 재분류 사유 기록.
`is_pilot=true`는 원래도 유지돼 있어 본 분석에서는 어차피 제외 대상이었다.

**(6.3은 §6.5에서 더 정밀화됨 - `outcome`/`state`를 직접 덮어쓰는 대신
원본과 판정을 분리하는 방식으로 바뀌었다. 이 절은 최초 접근을 기록으로
남긴다.)**

### 6.5 2차 정밀화 — 추가 리뷰 반영(2026-09-17)

1차 수정이 맞는 방향이었지만 다음이 불충분하다는 지적을 받아 추가로
고쳤다:

- **`min_observation_sec` 기준점 오류**: `injector.inject()` 호출 "전"에
  잡혀 있었다 - `t_injection`은 이후 `is_effective()` 확인 시점의 더 정밀한
  값으로 덮어써지는데, 기준점은 그 이전 채로 남아 있어 둘이 어긋났다.
  효과 확인이 느린 injector일수록 최소 관찰시간이 그만큼 줄어드는 역설이
  있었다. `is_effective()` 확인 직후로 기준점을 옮겼다.
- **`is_slo_evaluable()`이 전체 누적 표본만 봄**: probe가 주입 "전"에 이미
  20개 이상을 모았거나, 주입 "후" 갱신이 멈춰도 evaluable=True가 나올 수
  있었다. `Prober`에 `notify_injected(t_injection)` 계약을 추가해
  `run_once()`가 실제 주입 기준 시각을 넘겨주고, `load_ramp_adapter.py`는
  이제 **주입 이후** 표본만으로(최신 표본이 주입 후 `WINDOW_SEC` 이상
  지났고, 그 구간 표본 수가 `MIN_SAMPLES_FOR_RELIABLE_P95` 이상) 판정한다.
  순수 함수 `_is_post_injection_window_evaluable()`로 분리해 kubectl 없이
  단위 테스트 가능(`test_load_ramp_adapter.py`, 신규 6개).
- **`slo_evaluable_at_exit`의 None/True 혼동**: hook 미구현이어도 게이트는
  하위호환으로 통과시키지만(기존 동작 유지), 기록값은 "검증됨"이 아니라
  "검증 안 함"을 뜻하는 `None`이어야 한다 - hook이 실제로 판정한 경우에만
  `True`/`False`를 기록하도록 수정.
- **스키마·집계기 반영**: `min_observation_sec`/`slo_evaluable_at_exit`을
  결과 스키마(`experiment-contract.md` §5)와 `collect_metrics.py`의
  `comparison.csv`에 추가. `collect_metrics.py`에 두 검증 추가 - (1)
  `arm=native`인데 `outcome=prevented`면 이상(계약서 §3, pilot 무관하게
  항상 검출), (2) 본 실험(`is_pilot=false`)의 `prevented`는
  `slo_evaluable_at_exit=true`가 아니면 검증 오류(파일럿은 옛 하니스로
  실행됐을 수 있어 제외).
- **1차 재분류 방식 교체**: `outcome`/`state`를 직접 덮어쓰지 않는다 -
  원본 필드(`outcome=prevented`, `state=completed`,
  `slo_evaluable_at_exit=null`, 당시 하니스가 실제로 기록한 그대로)는
  손대지 않고, 별도 필드 `adjudicated_outcome`(`invalid_run`)·
  `adjudication_reason`·`adjudication_detail`·`adjudicated_at`으로 사후
  판정을 분리했다. 이제 이 trial은 `collect_metrics.py`의 새
  native+prevented 검증에 실제로 걸리는 것으로 확인됨(위 §6.3의
  `invalid_run` 직접 기록 방식은 그 검증을 우회했었음).
- 회귀 테스트 5개 추가(anchor 타이밍, hook 미구현 None 기록,
  `test_load_ramp_adapter.py` 6개 중 이번에 추가된 것, `collect_metrics.py`
  검증 2종). 오프라인 스위트 전체 43 passed, 2 skipped(live_cluster).

### 6.6 재실행 전 남은 것

코드 수정·테스트는 완료했으나 **재실행은 아직 하지 않았다** - §5.4의
재확인 요건(세 경로 10~15분 재검증, replacement vLLM Ready, Node 상태 등)을
다시 통과해야 한다.

**(2026-09-17 밤 재실행 완료 - §7 참고.)**

## 7. pod_kill native 경로 E2E 완료 (2026-09-17 밤)

§5.4 게이트(코드 43 passed·2 skipped, Node·pod·CR·quiescent 정상, 3경로
10분 351/351 성공 - 단, 첫 10분 검증은 pod_kill로 이미 죽은 옛 pod를 스크립트가
계속 찌른 자체 버그였고 원인 확인 후 스크립트를 현재 active pod를 동적으로
조회하도록 고쳐 재검증함)를 전부 통과한 뒤 재실행했다.

### 7.1 결과 요약

`pod_kill × native`(`run_id=pilot-pod_kill-native-01-20260917T145337Z`,
기존 오판정 실행 `...20260917T104638Z`와 명확히 구분되는 새 run_id,
`is_pilot=true`)에서 기존 Pod 소멸, SLO 위반, replacement Pod Ready 및
SLO 회복까지 E2E 흐름을 확인했다. Node와 하니스는 전 구간(Node·kubelet/
containerd 감시, 주입 전부터 시작 + 실행 중 localhost 감시를 replacement
pod로 동적 전환) 정상적으로 동작했다 - kubelet/containerd 경고 0건,
Node Ready·MemoryPressure 정상 유지.

| 항목 | 값 |
|---|---|
| `injection_valid` | `true` |
| `t_injection` | `14:54:47.378`(폴링 확인 시각) |
| `injection_observation_error_sec` | `null`(첫 poll에서 이미 죽어있어 기준점 없음) |
| `t_slo` | `14:54:46.879` |
| `t_recovery` | `14:57:52.880` |
| `outcome` | `recovered` |
| `action`/`promotion_verified`/`commit_sha`/`detected` | `none`/`null`/`null`/`false`(native라 전부 무개입 - 정상) |
| replacement pod | `vllm-serving-7b98b55c65-gm5bm`, 1/1 Ready |
| Chaos CR·experiment context | 정리 확인(`podchaos` 없음, `experiment-run=null`) |

**기능 검증: 통과. `pod_kill native 경로 E2E 완료`로 기록한다.**

### 7.2 발견 — `t_slo`가 `t_injection`보다 0.5초 빠름(기능 실패 아님)

다만 `t_slo < t_injection`은 "문제없음"으로 넘기지 않고 타임스탬프 의미
차이로 인한 측정 설계 보완 사항으로 남긴다. 현재 두 값의 의미:

- `t_injection`: 기존 Pod 소멸을 **처음 관측한** 시각 - 실제 장애 시각의
  상한일 뿐, 정확한 시각이 아니다.
- `t_slo`: 나중에 실패로 확정된 요청을 **처음 전송한**(`sent_at`) 시각 -
  완료(실패 확정) 시각이 아니다.

요청 전송 후 Pod가 죽어 그 요청이 실패했다면 `t_slo`가 `t_injection`보다
앞서는 것 자체는 가능하다(이번 사례: `sent_at=14:54:46.879`에 보낸 요청이
2.54초 뒤 실패로 확정됨 - raw probe CSV로 직접 확인). 하지만 **아직
실패하지도 않은 전송 시각**을 SLO 위반 시각으로 쓰는 것은, 본 실험에서
`t_detection`과 `t_SLO`를 비교할 때(탐지가 SLO 위반보다 먼저 오는지 확인)
의미가 모호해진다.

### 7.3 이번 파일럿 처리

- 기능 검증: 통과
- `outcome`: `recovered`(그대로)
- 본 분석: `is_pilot=true`로 제외(원래도 제외 대상)
- 정량 timing 검증: **타임스탬프 의미 보완 전까지 참고값**(trial JSON
  `notes`에 동일 내용 기록)
- 재실행: 이 문제만을 이유로 즉시 다시 할 필요 없음

### 7.4 본 실험 전 보완 사항 — 타임스탬프 의미 분리(요구사항, §7.5에서 구현 완료)

1. **주입 시각을 구간으로 기록**
   - `t_injection_request`: Chaos CR 요청 직전 시각
   - `t_injection_last_seen`: 기존 Pod를 마지막으로 확인한 시각
   - `t_injection_observed`: 기존 Pod 소멸을 처음 확인한 시각(현재의
     `t_injection`)
   - 첫 poll에서 이미 사라졌다면(`injection_observation_error_sec=null`인
     경우) 실제 장애는 최소한 `t_injection_request ~ t_injection_observed`
     구간에 있었다고 표현한다.
2. **SLO 시각 의미 분리**
   - `t_request_sent`: 요청 전송 시각(현재 raw CSV의 `sent_at`)
   - `t_request_completed`: 실패·timeout이 확정된 시각(`sent_at + latency`)
   - `t_slo`: 해당 샘플까지 포함해 SLO 위반을 판정할 수 있게 된 시각 -
     **현재처럼 실패 요청의 `sent_at`을 그대로 쓰지 말고, 최소한
     `sent_at + latency`로 계산한 완료 시각을 쓰는 것이 더 방어적이다**
     (롤링 P95·실패율도 요청이 완료돼야 계산 가능하므로).
3. **`collect_metrics.py` 판단 로직 추가**
   - `t_slo < t_injection_request` → "주입 전 위반 의심"
   - `t_slo`가 주입 관측 구간(`t_injection_request`~`t_injection_observed`)
     안에 있음 → `temporally_ambiguous`
   - `t_slo >= t_injection_observed` → 정상적인 사후 위반

**요약(기록용 확정 문구)**: `pod_kill × native` 파일럿에서 기존 Pod 소멸,
SLO 위반, replacement Pod Ready 및 SLO 회복까지 E2E 흐름을 확인했다. Node와
하니스는 전 구간 정상적으로 동작했다. 다만 현재 `t_slo`는 실패 요청의
전송 시각, `t_injection`은 Pod 소멸의 최초 관측 시각이어서 `t_slo`가
0.5초 앞서는 결과가 발생했다. 이는 기능 실패가 아니라 서로 다른 관측
기준에서 생긴 시간적 모호성이며, 본 실험 전 요청 완료 시각과 주입 관측
구간을 별도 기록하도록 보완한다. 즉, E2E 완료 표시는 가능하지만 정량
타이밍 정의는 본 실험 전에 한 번 더 고정해야 한다.

### 7.5 §7.4 구현 완료 (2026-09-18, 로컬 코드·테스트·문서만 - 실클러스터 작업 없음)

**1. 주입 시각 3분할**: `run_once.py`에 `t_injection_request`(`inject()`
호출 직전)·`t_injection_last_seen`·`t_injection_observed` 필드 추가.
`t_injection`은 하위 호환용으로 유지하되 `t_injection_observed`와 항상
같은 대표값으로 명시. `Injector` 계약에 `get_last_seen_present_time()`
선택 필드를 추가해 `pod_kill_adapter.py`/`load_ramp_adapter.py`가 이미
내부적으로 추적하던 "마지막 생존 관측" 시각을 그대로 노출하도록 구현.

`injection_observation_error_sec`는 3단계 우선순위로 계산(전부 실측값,
임의 설정값 없음): (1) 어댑터의 `get_injection_observation_error_sec()`
(있으면 최우선), (2) 없으면 `t_injection_last_seen`~`t_injection_observed`,
(3) last_seen도 없으면(첫 poll에서 이미 사라짐) `t_injection_request`~
`t_injection_observed`. `injection_valid=true`인 trial은 이제 이 필드가
절대 null로 남지 않는다. `min_observation_sec`의 monotonic 기준점은
그대로 `is_effective()` 확인 직후 유지(§6.2에서 이미 고정한 것 - 이번에
`t_injection_request` 도입으로 흔들리지 않게 재확인).

**2. `t_slo`/`t_recovery`를 `observed_at` 기준으로 전환**: `slo_judge.py`의
`evaluate()`가 각 point에 `sent_at`(기존 `"t"`와 동일)과
`observed_at`(=`sent_at+latency`)을 함께 남기고, `find_t_slo()`/
`find_t_recovery()`는 이제 `observed_at`을 반환한다. **윈도우 구성·P95·
성공률·위반 여부 판정 로직 자체는 전부 `sent_at` 기준 그대로**(계산값이
바뀌면 안 된다는 요구사항) - 바뀐 건 "판정에 쓰인 사건이 언제
일어났다고 보는가"가 아니라 "그 사실을 언제 알 수 있었는가"를
반환한다는 점뿐이다. `find_t_recovery()`의 "t_slo 이후" 필터도 두 값이
같은 도메인(observed_at)이어야 앞뒤가 안 섞이므로 함께 맞췄다.

이 변경은 이미 확정된 `load_ramp` 5-stage 재현성 결론(0.50 RPS까지 3회
모두 준수, 0.75 RPS 2/3회·1.00 RPS 3/3회 위반 등 - §4 "위반 여부"의
COUNT)에 영향을 주지 않는다 - 그 결론은 `t_slo is not None` 여부(위반
있었는지)에만 의존하고, 그건 여전히 sent_at 기준 스트릭 판정 그대로다.
바뀌는 건 "위반이 있었다고 판정된 정확한 순간"의 타임스탬프뿐이다.

**3. `timing_schema_version` 필드**: 새로 기록되는 trial은 전부 `"v2"`.
기존(이번 정정 이전) trial JSON은 이 필드가 아예 없거나 `"v1"` - 재수정
안 함(원본 보존 원칙).

**4. `collect_metrics.py`**: `t_injection_request`/`t_injection_last_seen`/
`t_injection_observed`/`timing_schema_version`을 스키마·`comparison.csv`에
반영. `temporal_relation` 판정 추가 - 하한(`t_injection_last_seen` 있으면
그 값, 없으면 `t_injection_request`)과 상한(`t_injection_observed`) 대비
`t_slo` 위치로 `pre_injection`/`temporally_ambiguous`/`post_injection`/
`unknown`(필요한 시각 없음) 분류. 주입 3시각의 순서가 서로 모순되면(예:
`last_seen`이 `request`보다 이름) validation issue로 남김.

**5. 회귀 테스트**: `test_slo_judge.py`(신규, kubectl 의존 없는 순수 함수
테스트) - 실패 요청의 `t_slo`가 `sent_at+latency`임을 확인, 같은 fixture로
P95·위반 여부·SLO calibration 상수가 안 바뀜을 확인. `test_run_once.py` -
효과 확인 지연 시 `min_observation_sec` 60초 전 조기 종료 안 됨(기존
§6.5 테스트), 첫 poll에 이미 사라진 경우 request~observed 구간 기록,
last_seen 있으면 더 좁은 구간 사용. `test_collect_metrics.py` - pre/
ambiguous/post/unknown 4가지 분류, 주입 3시각 모순 검출, v1 결과(신규
필드 없음)도 오류 없이 읽힘. 오프라인 스위트 전체 59 passed, 2 skipped
(live_cluster).

**6. 1차 파일럿 결과 처리**: `trial-pilot-pod_kill-native-01-
20260917T145337Z.json`(gitignore 대상)의 원본 측정값은 재수정하지
않았다 - 이 trial은 §7.4 구현 이전에 실행돼 `timing_schema_version`이
없는(v1 성격) 기록으로 남는다. `notes`에 이미 남긴 캐비어트가 여전히
정확하다.

**실클러스터 재실행은 하지 않았다** - 다음 pod_kill/load_ramp 실행부터
자동으로 v2 스키마로 기록된다.

## 8. 완성도 점검 대응 — 문서 정합성 + network_degrade 어댑터 (2026-09-18)

§7.4/§7.5 구현 완료 뒤 진행한 완성도 점검에서 나온 항목들에 대응했다.
전부 오프라인(코드·문서·YAML 저작만) - 실클러스터 작업 없음.

**1. 문서/설정 정합성**: 루트 `README.md`가 recovery-policy(구현·E2E 검증
완료 - 실제로는 "본 구현 예정"이 아님)·감사 이력(`git_client.py`/
`outbox.json` 실동작 중 - "개발 예정"이 아님)·Isolation Forest 정상 표본
수(실제 `anomaly-detection/data/regimes.jsonl` 19줄 확인 - "7개"는 stale)를
전부 낡은 상태로 기술하고 있었다. `experiments/README.md`의 "3종 시나리오
×10회+"도 확정 계약(4종×3arm×5회=60)과 안 맞았다. 전부 실제 코드/데이터
상태와 대조해 고쳤고, 새 "⚠️ 알려진 제한사항" 절을 추가해 이상탐지 검증
깊이·rule-out 미구현·본 실험 미실행을 명시적으로 남겼다.
`chaos/scenario-progressive-memory-pressure.yaml`의 헤더 주석도 실제 stage-5
설정(5000MB, ~8407Mi)과 다른 "3.5GB/6871Mi"(3차 시도 실패 후 5000MB로
올린 이력 - `docs/design/phase5-memory-pressure-investigation.md` §1 참고)를
그대로 남기고 있어 실제 값으로 고쳤다.

**2. `rule-out`(반대증거) 미구현**: `recovery-policy/policy.py`의
`PolicyContext.has_contradicting_evidence`는 `main.py`의 유일한 실제
호출부(`policy.PolicyContext(preview_ready=preview_ready)`)가 세팅하지
않아 항상 `False`다. `main.py`(8-12행)에 이미 "무엇을 반대증거로 볼지는
5~6단계 범위 밖"이라고 의도적 범위 제외가 기록돼 있었다 - 근거 없는
가짜 구현을 새로 만드는 대신, 이 사실을 README "알려진 제한사항"에
노출하는 쪽을 선택했다.

**3. `network_degrade_adapter.py`**: `pod_kill_adapter.py`와 같은 이유로
`vllm-active` Service 기반 동적 active pod 고정이 필요해, 그 로직을
`active_pod_resolver.py`로 추출해 `pod_kill_adapter.py`도 이걸 쓰도록
리팩터했다(기존 5개 테스트 그대로 통과 확인). `chaos/scenario-network-
degrade.yaml`과 같은 4단계(500/1000/2000/4000ms, 90초씩)를 Workflow CRD
대신 NetworkChaos 4개의 직접 순차 생성/삭제(백그라운드 스레드 + `threading.
Event`로 조기 cleanup 가능)로 재현했다 - Workflow의 하위 단계별 status
스키마를 문서로 확인 못 했고, 단일 NetworkChaos의 `status.conditions`
(`AllInjected` 등)는 Chaos Mesh 공식 문서로 확인했기 때문이다(검증
가능한 메커니즘만 채택). 이 상태 필드 경로는 문서 기반이지 실클러스터
`kubectl get networkchaos -o yaml`로 직접 대조한 적은 없다 - 첫 실클러스터
trial 전 반드시 확인 필요. 오프라인 테스트 5개(정상 완료/대상없음/
다중대상/효과 미관측/cleanup 중도중단) 작성, 자체 버그 1건 발견·수정
(getter를 직접 poll하는 테스트가 `is_started()`의 부수효과를 안 거쳐
영원히 조건이 안 참이 됨 - poll 대상을 getter에서 `is_started()` 자체로
수정).

**4. "발견 5" 재발견과 Kustomize overlay**: `network_degrade`의 "기본
probe로 재시작이 발생하는 연쇄장애 실험 vs probe timeout을 조정한 순수
열화 실험" 분리 요청을 조사하다, `gitops/apps/vllm-serving/rollout.yaml`의
readinessProbe/livenessProbe에 `timeoutSeconds`가 아예 없어(K8s 기본값
1초 그대로) 여전히 발견 5(`anomaly-detection/test_model.py`의 known-
anomaly 구간, 2026-09-05 성공률 11% - NetworkChaos 지연이 probe 자체를
실패시켜 kubelet이 재시작하는 false-positive)를 그대로 재현하는 상태임을
확인했다. `gitops/apps/recovery-policy/deployment.yaml`은 이미 이 교훈으로
`timeoutSeconds: 3`을 명시해뒀지만 vLLM 자신의 Rollout엔 반영 안 돼 있었다.
Rollout을 영구 변경하거나 복제하는 대신, `gitops/apps/vllm-serving/
overlays/network-tolerant/`에 probe 필드만 patch하는 Kustomize overlay를
새로 만들었다(base는 그대로 유지 - "기본 probe" 실험이 계속 재현
가능해야 하므로). 오버레이 위치는 처음 `gitops/overlays/`에 뒀다가
Kustomize의 root 보안 제약("overlay 자기 디렉터리 밖 파일 참조 금지")에
막혀(`kubectl kustomize`로 로컬 실측 확인) `gitops/apps/vllm-serving/
overlays/network-tolerant/`로 옮겼고, 그래도 `--load-restrictor=
LoadRestrictionsNone` 플래그가 필요함을 확인해 overlay 주석에 남겼다.
patch 값(10초)은 **미검증 후보**로 명시했다 - 임의로 정하지 말라는
지시에 따라 확정은 하지 않았다.

**5. 실클러스터 calibration 스크립트**: `calibrate_network_tolerant_probe.py`
(신규) - NetworkChaos 최악조건(4000ms/400ms, stage-4와 동일)을 주입하는
동안 restartCount·`vllm-active` endpoint 소속 여부를 끝에서 한 번이 아니라
계속 폴링해서 false-positive 재시작/제외를 직접 관측한다. `run_network_
degrade_trial.py`(신규, `run_pod_kill_trial.py`와 같은 패턴)는 `--probe-
profile {default,network_tolerant}`를 받아 TrialResult에 기록하고,
실행 직전 active pod의 실제 probe timeoutSeconds를 읽어 요청한 profile과
다르면 `ProbeProfileMismatch`로 즉시 중단한다(라벨과 실제 설정 불일치
방지, fail-closed). `run_once.py`에 `readiness_probe_profile`/
`readiness_probe_timeout_sec` 필드 2개를 추가했다(기존 `probe_profile`은
SLO 측정용 HTTP probe 설정을 가리키는 다른 축이라 이름 충돌 피함) -
기존 19개 테스트 그대로 통과 확인.

**§8.6 방법론 정정 - UID 변경을 무조건 invalid로 처리하면 안 됨(2026-09-18,
커밋 리뷰)**: 위 3번(network_degrade_adapter.py)의 대상 pod UID 재확인
로직이 "매 단계 전환마다 UID가 바뀌면 무조건 TrialInvalid"였는데, 이건
틀렸다 - 기본(default) probe 실험에서는 네트워크 열화로 인한 probe 실패·
재시작 자체가 측정 대상이고, tolerant profile에서도 재시작은 "그 설정이
열화를 견디지 못했다"는 유효한 결과일 수 있다. 이걸 전부 invalid_run으로
버리면 정작 관찰하려던 연쇄장애 현상 자체를 데이터에서 지워버리는 셈이다.

"주입이 한 번도 효과를 내기 전"(아직 아무 일도 안 일어났으므로 대상이
바뀌면 외부 오염 가능성이 높음 - 여전히 invalid_run)과 "이미 효과를 낸
뒤"(네트워크 열화 자체의 결과일 수 있음 - invalid로 버리지 않고 기록)를
`injection_started_at`(is_started()가 처음 True를 준 시각) 기준으로
구분하도록 고쳤다. 후자는 TrialResult에 `target_replaced`/
`t_target_replaced`/`target_replacement_pod_name`/`target_replacement_pod_uid`
로 기록하고, injector는 "주입 끝남"(is_done=True)으로만 취급해 이후
outcome은 평소대로 prober의 SLO 판정에 맡긴다 - target_replaced 자체가
outcome을 결정하지 않는다. 대상 재확인 방식도 `get_pod_fn(고정된 이름)`
에서 `get_active_pods_fn()` 재호출(prepare()와 같은 메커니즘)로 바꿨다 -
이름 하나만 다시 조회해서는 "이름은 같지만 다른 pod"를 구분 못 하고,
교체된 새 pod의 이름/UID를 얻으려면 vllm-active selector를 다시 물어야
하기 때문이다(그래서 더 안 쓰는 get_pod_fn 파라미터는 제거).

회귀 테스트 3개 추가(효과 전 변경=invalid_run, 효과 후 단일 교체=
target_replaced로 기록·default profile 연쇄장애 맥락, 효과 후 2개 동시
매칭되는 모호한 전환=역시 기록하되 교체 pod은 특정 안 함·tolerant
profile calibration 맥락). 오프라인 스위트 69 passed, 2 skipped
(live_cluster). 실클러스터 작업 없음 - 이 커밋도 별도 fix 커밋으로 분리.

**§8.7 target_replaced가 실제로 comparison.csv까지 남는지 확인(2026-09-18)**:
바로 위에서 추가한 6개 필드(`readiness_probe_profile`/
`readiness_probe_timeout_sec`/`target_replaced`/`t_target_replaced`/
`target_replacement_pod_name`/`target_replacement_pod_uid`)가 adapter
내부 상태·raw trial JSON에만 있고 `collect_metrics.py`의 `comparison.csv`
에는 안 남는 게 아니냐는 질문을 받았다. `build_comparison()`을 직접 읽어
확인한 결과 - 맞는 지적이었다. `TrialResult`에 필드를 추가하면
`asdict()`로 raw JSON에는 자동으로 실리지만, `comparison.csv`는
`build_comparison()` 안의 명시적 화이트리스트 dict 리터럴이라 거기 안
넣으면 절대 안 나온다. 6개 필드 전부 빠져 있었다 - 추가했다.

동시에 `readiness_probe_profile`+`target_replaced` 조합을 해석하는 분석
전용 필드 2개(`restart_chain_observed`, `probe_isolation_held`)를
`collect_metrics.py`에 새로 추가했다 - default profile에서는
target_replaced 그대로가 연쇄장애 관찰 여부, network_tolerant profile
에서는 그 반대가 "그 설정이 열화로부터 probe를 실제로 격리했는지"다.
outcome은 절대 안 바꾼다(SLO 판정과 별개 필드). tolerant profile에서
교체가 있었는데 `outcome=prevented`만 남으면 "설정이 열화를 견뎠다"로
오해할 위험이 있어(실은 pod이 바뀌어 측정 자체가 무의미해진 것일 수
있음) `_check_tolerant_profile_prevented_misleading()`으로 별도 issue도
남기게 했다(기존 native+prevented 검증과 같은 패턴 재사용).

회귀 테스트 6개 추가 - default/tolerant profile 조합 3가지(default+교체
->restart_chain_observed=true, tolerant+교체->probe_isolation_held=false,
tolerant+무교체->probe_isolation_held=true), 오해소지 issue 검출,
신규 필드가 아예 없는 기존 결과의 하위호환(전부 None으로 읽힘, 오류
없음), 그리고 trial JSON 파일을 실제로 써서 `comparison.csv` 파일까지
왕복시켜 6개 필드+2개 해석 필드가 실제 CSV 컬럼으로 나오는지 파일
단위로 직접 확인하는 테스트. 오프라인 스위트 75 passed, 2 skipped
(live_cluster). 실클러스터 작업 없음 - 이 커밋도 별도 fix 커밋으로 분리.

**§8.8 다음 세션 순서 확정(2026-09-18) - tolerant calibration은 CPU
headroom 해결 전까지 보류**: overlay 적용은 Rollout의 Pod template을
바꾸는 것이라 BlueGreen 특성상 새 preview vLLM이 뜬다 - 이게 아직 해결
안 된 8코어 노드의 CPU headroom 문제(§3.2)를 다시 일으킬 위험이 있다.
그래서 tolerant probe calibration을 AllInjected 실측 확인보다 먼저 하면
안 된다(지적받음) - 안전한 순서는 다음과 같다.

1. 노드·네트워크 preflight
2. 현재 기본(default) probe에서 최소 강도로 AllInjected -> 삭제 ->
   AllRecovered/잔존물 없음 경로만 파일럿 검증 - 단일 active pod 그대로,
   overlay 미적용이라 2번째 vLLM을 안 띄우므로 CPU headroom과 무관하게
   지금도 가능
3. CPU headroom 해결 + vLLM 콜드스타트 3회 안정성 확인
4. 그 다음에야 `gitops/apps/vllm-serving/overlays/network-tolerant/`
   적용 + `calibrate_network_tolerant_probe.py`로 실제 timeout calibration
5. 확정된 값으로 NetworkChaos 전체 파일럿

**아직 안 한 것(다음 세션, 위 순서대로·각 단계 별도 명시적 승인 필요)**:
위 1~5단계 전부, `memory_pressure_adapter.py`, Isolation Forest 검증
강화. 오프라인 스위트 전체 75 passed, 2 skipped(live_cluster) -
`test_pod_kill_adapter.py`(리팩터 후 재확인) 5개, `test_network_degrade_
adapter.py`(신규) 8개, `test_collect_metrics.py`(target_replaced 연동)
28개 포함.

## 9. §8.8 1단계 - fresh preflight + 방치된 CR 정리 (2026-09-18, 읽기 전용 조사 후 최소 삭제)

### 9.1 워크로드 수준 preflight - 전부 정상

노드 2개 Ready(`sj-worker`는 2026-09-16 18:01:33부터 끊김 없이 약 43시간
지속 - 10~15분 기준 훨씬 상회), MemoryPressure/DiskPressure/PIDPressure
전부 False. vLLM은 단일 파드(`vllm-serving-7b98b55c65-gm5bm`, 재시작 0,
13시간)만 존재하고 Rollout은 `Healthy`/`stable=current`로 preview 없음 -
`vllm-active`/`vllm-preview` Endpoints도 같은 파드를 가리켜 모호함 없음.
CPU/메모리 headroom(1-vLLM 상태): CPU limits 50%(8코어 중 4), 메모리
limits 38% - §3.2가 우려한 "2-vLLM=8/8코어 100%" 상태가 전혀 아님을 직접
확인. `/health` 5회 연속 4~10ms - §5의 간헐적 네트워크 이상 징후 없음.
recovery-policy `/healthz` 200, `/admin/quiescent`={"quiescent":true,
"active_count":0}, `/admin/experiment-run`={"current":null}. recovery-
policy의 재시작 1회(43h 전, exitCode 137)는 새 사건이 아니라 §3.1에
이미 기록된 그 NodeNotReady 사건의 결과물(파드 `-5hmvj` 그 자체) -
종료 시각 2026-09-16T09:08:00Z가 §3.1 타임라인보다 4~8분 앞선다는
세부값만 새로 확인.

### 9.2 control-plane 경보 - "실제 etcd 장애 아님"으로 성급히 결론짓지 않고 분류만 확정

Alertmanager에 `etcdInsufficientMembers`(critical)·`etcdMembersDown`·
`KubeProxy/Scheduler/ControllerManagerInstanceUnreachable`·`TargetDown`
×4 총 9건(`Watchdog` 제외)이 **2026-09-16T09:07~09:18Z부터 지금까지
43시간 넘게 한 번도 안 풀리고 계속 active** 상태로 확인됨(recovery-policy
재시작·chaos-controller-manager 파드 AGE=43h와 정확히 같은 시각대).

직접 조사(단순 "단일 멤버라 오탐"으로 넘기지 않고 실측):
- `kubectl get pods -n kube-system`: etcd/kube-proxy(양쪽)/kube-scheduler/
  kube-controller-manager 전부 `1/1 Running`, 13일째, 최근 재시작 없음
  (kube-scheduler의 재시작 30회는 13일 누적치이지 최근 사건 아님)
- `etcdctl member list`: 단일 멤버(`sj-control`) 1개, `STATUS=started`
- `etcdctl endpoint health`: `is healthy: successfully committed
  proposal: took=19.7ms` - 실제 write 성공까지 확인

**분류(확정, "단순 오탐"으로 종결하지 않음)**: etcd 자체는 실제로 정상
동작 중임을 직접 증거로 확인했다. 다만 이 알림들이 43시간째 하나도
안 풀렸다는 사실 자체는 **Prometheus의 scrape 경로가 이 control-plane
컴포넌트들에 계속 도달 못 하고 있는 모니터링 결함**으로 기록한다(단일
멤버 etcd에 다중 멤버 가정 쿼럼 공식이 적용되는 문제 + kube-proxy/
scheduler/controller-manager metrics 엔드포인트 스크레이핑 실패로
추정 - 근본 원인의 정확한 지점은 아직 안 밝힘). 최소 강도 500ms
smoke 1회를 막을 사유는 아니지만, **본 실험(60회) 실행 전에는 별도
해결 또는 명시적 제외 근거가 필요한 미해결 항목**으로 남긴다.

### 9.3 방치된 Chaos Mesh CR 2건 - 증거 보존 후 삭제

둘 다 대상 파드가 이미 사라졌고(`AllRecovered`/`Accomplished` 확인 후
재확인) 오늘 계획된 작업과 무관한 과거 조사의 잔재임을 확인한 뒤 삭제했다.

**`stresschaos/vllm-memory-pressure-debug`**(2026-09-04 phase5 조사의
디버그 실행, `docs/design/phase5-memory-pressure-investigation.md` §1
"4차 시도"와 동일 건) - 삭제 전 최종 상태:

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  creationTimestamp: "2026-09-04T11:48:28Z"
  name: vllm-memory-pressure-debug
  namespace: vllm-serving
  uid: 6446bdb1-32a3-41bf-8af7-19126f286120
spec:
  duration: 10m
  mode: one
  selector:
    labelSelectors: {app: vllm-serving}
    namespaces: [vllm-serving]
  stressors:
    memory: {oomScoreAdj: 0, size: 5000MB, workers: 2}
status:
  conditions:
    - {type: Selected, status: "True"}
    - {type: AllInjected, status: "False"}
    - {type: AllRecovered, status: "True"}
    - {type: Paused, status: "False"}
  experiment:
    containerRecords:
      - id: vllm-serving/vllm-serving-6c769cc5c5-h9st7/vllm
        events:
          - {operation: Apply, type: Succeeded, timestamp: "2026-09-04T11:48:28Z"}
          - {operation: Recover, type: Succeeded, timestamp: "2026-09-04T11:58:28Z"}
        injectedCount: 1
        recoveredCount: 1
        phase: Not Injected
    desiredPhase: Stop
```

대상이던 `vllm-serving-6c769cc5c5-h9st7`는 삭제 직전 재확인 결과
`NotFound`(현재 유일한 파드는 `vllm-serving-7b98b55c65-gm5bm`, 전혀
다른 ReplicaSet).

**`workflow/vllm-network-degrade`**(2026-09-05 실행분 - `test_model.py`의
`KNOWN_ANOMALY_START/END`(13:35:02~13:41:31Z)와 거의 정확히 겹침 -
**"발견 5"의 실제 발생원 그 자체**) - 삭제 전 최종 상태:

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: Workflow
metadata:
  creationTimestamp: "2026-09-05T13:34:56Z"
  name: vllm-network-degrade
  namespace: vllm-serving
  uid: 306c6561-40f2-488b-b26a-f7a77388f44a
spec:
  entry: degrade-serial
  templates:
    - {name: degrade-serial, templateType: Serial, deadline: 7m,
       children: [stage-1-500ms, stage-2-1000ms, stage-3-2000ms, stage-4-4000ms]}
    - {name: stage-1-500ms, templateType: NetworkChaos, deadline: 90s,
       networkChaos: {action: delay, mode: one, delay: {latency: 500ms, jitter: 50ms},
         selector: {labelSelectors: {app: vllm-serving}, namespaces: [vllm-serving]}}}
    - {name: stage-2-1000ms, templateType: NetworkChaos, deadline: 90s,
       networkChaos: {action: delay, mode: one, delay: {latency: 1000ms, jitter: 100ms},
         selector: {labelSelectors: {app: vllm-serving}, namespaces: [vllm-serving]}}}
    - {name: stage-3-2000ms, templateType: NetworkChaos, deadline: 90s,
       networkChaos: {action: delay, mode: one, delay: {latency: 2000ms, jitter: 200ms},
         selector: {labelSelectors: {app: vllm-serving}, namespaces: [vllm-serving]}}}
    - {name: stage-4-4000ms, templateType: NetworkChaos, deadline: 90s,
       networkChaos: {action: delay, mode: one, delay: {latency: 4000ms, jitter: 400ms},
         selector: {labelSelectors: {app: vllm-serving}, namespaces: [vllm-serving]}}}
status:
  conditions:
    - {type: Scheduled, status: "True"}
    - {type: Accomplished, status: "True"}
  startTime: "2026-09-05T13:34:56Z"
  endTime: "2026-09-05T13:40:56Z"
  entryNode: degrade-serial-kcz7q
```

삭제 명령: `kubectl delete stresschaos vllm-memory-pressure-debug -n
vllm-serving`, `kubectl delete workflow vllm-network-degrade -n
vllm-serving`. 삭제 후 `kubectl get podchaos,networkchaos,stresschaos,
workflow -n vllm-serving`로 네 타입 전부 잔존 없음 확인(§9.4).

### 9.4 삭제 후 잔존 확인

```
kubectl get podchaos,networkchaos,stresschaos,workflow -n vllm-serving
  -> No resources found in vllm-serving namespace.
```

### 9.5 로컬 머신·모니터링 재확인 후 NetworkChaos 500ms 단일-stage smoke - `SMOKE-EXCLUDED`, PASS

**사전 재확인**: 로컬 가용 메모리 1.81GB(§9 preflight 시점) -> 사용자가
Notion·Slack·미사용 ChatGPT 직접 종료 -> **4.71GB로 회복**(3GB 기준 통과).
저장소 경로·HEAD(`dab6a34`) 불변 확인. `kubectl get nodes` 4초 간격
10회 전부 성공(`cannot allocate memory` 재발 없음).

**Prometheus/Alertmanager 재분류(§9.2 확정)**: `etcdInsufficientMembers`
등 9개 경보가 43시간째 미해소인 것은 실제 etcd 장애가 아니라(직접
`etcdctl endpoint health`로 정상 write 확인) Prometheus scrape 경로
결함으로 기록 - **60회 본 실험 전 별도 해결 또는 명시적 제외 근거 필요**
항목으로 남김(단순 오탐으로 종결하지 않음).

**smoke 실행**(`network_degrade_adapter.py`의 4단계 순차 로직을 쓰지
않고 `create_network_chaos`/`delete_network_chaos`/`is_stage_injected`/
`does_chaos_exist`만 직접 재사용한 1회성 스크립트, 90초 정규 stage나
나머지 1/2/4초 stage는 아예 만들지 않음):

| 항목 | 값 |
|---|---|
| `run_id` | `network-degrade-SMOKE-EXCLUDED-20260918t045134z` |
| CR | `netdelay-smoke-excluded-20260918t045134z`(NetworkChaos, 500ms/50ms jitter 단일) |
| 대상 | `vllm-serving-7b98b55c65-gm5bm`(uid `94662317-d673-4fbb-b19d-63dd2658e9f9`) - 실행 전후 이름·UID 동일 |
| `AllInjected=True` 확인 | **최초로 실클러스터에서 직접 확인됨**(문서 기반·미검증이던 §8 docstring의 우려 해소) - 확인 시각 `2026-09-18T04:51:46.681053+00:00` |
| cleanup | `AllInjected` 확인 0.121초 후 delete 요청, 0.844초 후 CR 완전 소멸 확인(`does_chaos_exist=False`) - 10초 한도 여유 있게 충족 |
| pre/post 파드 | Ready=true/true, restart_count=0/0(delta 0), UID 동일 |
| pre/post Node(`sj-worker`) | Ready=True/True |
| pre/post completion 요청 | `200 0.654s` / `200 0.262s` - 둘 다 성공, 지연 증가 징후 없음(500ms 단일 CR이라 예상된 결과) |
| 중단 조건 | 전혀 발동 안 됨(교체·재시작·Node 이상·cleanup 실패·로컬 명령 실패 없음) |
| 결과 | **PASS** |

이번 smoke로 `network_degrade_adapter.py`의 핵심 미검증 가정("문서
기반이지 실클러스터 kubectl get networkchaos -o yaml로 직접 본 적은
없다")이 실측으로 해소됐다 - `status.conditions`의 `AllInjected` 필드
경로가 실제로 존재하고 정확히 동작함을 확인. tolerant overlay 적용과
실제 timeout calibration은 지시대로 이어서 진행하지 않았다(§8.8 순서상
3번 CPU headroom 해결이 먼저).

## 10. CPU headroom 해결 - 읽기 전용 자원 설계 조사 (2026-09-18, Rollout·노드·VM 미변경)

**주의사항 준수**: request(스케줄링 기준)와 limit(CFS 상한)을 구분해서
본다 - "8/8코어 limit"만으로 사건 원인을 확정하지 않는다. 두 번째 vLLM
기동과 NodeNotReady의 시간적 연관은 아래 실측으로 더 뚜렷해졌지만
여전히 상관관계이지 인과관계 확정이 아니다.

### 10.1 `sj-worker` capacity/allocatable

`cpu: 8`(capacity) = `cpu: 8`(allocatable) - **system-reserved/kube-
reserved CPU carve-out이 전혀 설정 안 돼 있다**(메모리도 동일하게
carve-out 없음, capacity 16380344Ki = allocatable에 근접). 즉 kubelet
자신을 위해 커널이 별도로 떼어둔 CPU가 0이라, 워크로드가 자기 limit을
다 쓰면 시스템 프로세스와 완전히 같은 runqueue에서 경쟁한다 - 이 자체가
기존에 문서화 안 된 새 사실이다.

### 10.2 전체 파드 request/limit 합계 (`sj-worker`, 18개 파드)

| | Requests | Limits |
|---|---|---|
| CPU | 2550m (31%) | 4 (50%) |
| Memory | 5590Mi (35%) | 6Gi (38%) |

request/limit이 설정된 파드는 `vllm-serving`(2/4 CPU, 4Gi/6Gi)과 소수의
작은 시스템 파드(chaos-controller-manager ×3=75m/768Mi, chaos-dashboard
25m/256Mi, chaos-dns-server 100m/70Mi, calico-node 250m, metrics-server
100m/200Mi)뿐이다. **kube-proxy·chaos-daemon·argo-rollouts·recovery-
policy·prometheus 스택 대부분(prometheus 본체 포함)은 request/limit이
아예 0** - 스케줄러 계산에는 안 잡히지만 실제로는 자원을 쓴다. 실측
(`kubectl top node`) 기준 현재 노드 전체 사용량은 **2648m(33%) CPU,
10253Mi(64%) 메모리**로, request 합계(31%)보다 실사용이 이미 더 높다 -
0-request 파드들(특히 calico-node)의 실사용이 회계 밖에 있기 때문이다.

### 10.3 active vLLM 실측 - idle vs 처리 중

- **idle**: CPU ~20~22m(코어의 0.2~0.3%), 메모리 ~3504Mi
- **동시 요청 8개 처리 중**(`max_tokens=200`, 동시 발사): 처리 완료 직후
  샘플에서 **CPU 4000m - 4코어 limit을 정확히 가득 채움**. 메모리는
  3504→3515Mi로 거의 안 움직임(메모리가 아니라 CPU가 제약임을 실측
  확인). 즉 vLLM은 콜드스타트(모델 컴파일)뿐 아니라 **평상시 동시
  처리 부하에서도 4코어를 실제로 다 쓴다** - "limit 합계가 이론상
  100%"라는 지적을 넘어, 실제로 그 한도까지 쓰는 워크로드임을 확인.

### 10.4 이전 preview 콜드스타트 시간대(2026-09-16) 실측 재구성

§3.1 서술 타임라인에 없던 정량값을 Prometheus 과거 데이터(node_exporter,
아직 보존돼 있음)로 새로 확인했다 - `node_cpu_seconds_total`/`node_load1`,
`instance=192.168.30.76`(`sj-worker`) 기준:

| 시각(UTC) | CPU non-idle | iowait | load1 | 비고 |
|---|---|---|---|---|
| 09:09:00 | 76.0% | 45.6% | 16.7 | 관측 구간 내 load1 최고치(8코어 대비 2배 이상 과포화) |
| 09:09:30 | 80.0% | 47.0% | 15.1 | |
| 09:10:00~09:10:30 | 72~70% | 39~36% | 13.7→11.6 | |
| **09:11:00~09:11:30** | **57.0%→56.8%** | **24.5%→22.4%** | **9.74→8.26** | §3.1의 kubelet 에러 시작 시각(09:11:55) 직전 - 절대 최고치는 지났지만 여전히 상당히 높은 수준 |
| 09:12:00~09:13:30 | 51→47% | 20→6.8% | 7.39→4.14 | 완만히 하강 |
| 09:14:00 | 43.2% | 0.72% | 4.07 | 거의 평시 수준 |
| **09:14:30** | **63.8%** | **29.6%** | **7.66** | 재상승 - §3.1의 "09:14:48 BlueGreenPause 진입"(기존 active vLLM·recovery-policy 재생성 시점)과 거의 일치, 별개의 2차 부하로 보임 |
| 09:15:00~09:16:00 | ~30% | ~0% | 7.2~7.66 | 안정화 |

**해석(상관관계로만 기록)**: kubelet 에러가 시작된 09:11:55 시점은
CPU/iowait의 절대 최고치(09:09~09:10)는 이미 지난 뒤였지만, load1은
여전히 8~10(정상 대비 2배 이상)으로 높았다 - "단발성 순간 스파이크"보다
"~3분간 지속된 높은 부하가 누적돼 kubelet heartbeat 예산을 소진했다"는
쪽에 더 부합하는 패턴이나, 이 수치들만으로 정확한 인과 메커니즘을
증명하지는 못한다. 09:14:30의 2차 스파이크는 기존 워크로드 재생성이라는
"별개의" 부하로, 최초 원인(§3.2)과는 구분해서 봐야 한다.

### 10.5 worker VM vCPU 확장 / 별도 worker 추가 가능성 - 저장소에서 확인 불가

Vagrantfile·terraform·설치 스크립트 등 하이퍼바이저/호스트 사양을 기록한
파일을 찾지 못했다(`README.md`의 "2-VM: control-plane + worker" 한 줄이
전부). 이 두 항목은 클러스터 내부에서 알 수 없고 호스트 환경 정보가
필요해 사용자 확인이 필요하다.

### 10.6 vLLM 3코어 limit 변경 시 설정 변경 범위

`gitops/apps/vllm-serving/rollout.yaml`의 `resources.limits.cpu: "4"`
한 줄을 `"3"`으로 바꾸는 것만으로 충분하다(`requests.cpu: "2"`는 유지) -
코드 변경 범위 자체는 작다. **다만 이건 Rollout의 Pod template 필드라
BlueGreen 특성상 적용 자체가 새 preview 리비전을 띄운다** - 즉 이 변경을
"적용"하는 순간 잠깐이라도 구 4코어 active + 신 3코어 preview가 동시에
뜨는, 지금 피하려는 바로 그 상황이 재현된다(모니터링 강화 후 신중히
진행하거나 별도 절차 필요). §10.3에서 vLLM이 실제로 4코어를 다 쓰는
워크로드임을 확인했으므로, 3코어로 낮추면 동시 처리 부하에서 처리량/
지연에 실질적 영향이 있을 가능성이 높다 - `experiment-contract.md`의
"load_ramp 확정 설정(본 실험 시작 후 변경 금지)"은 4코어 기준으로
보정된 값이라 3코어로 바뀌면 재보정이 불가피하다.

### 10.7 우선순위 후보 비교

| | 1. worker 12코어+ 확장(vLLM 4코어 유지) | 2. vLLM 3코어로 하향 | 3. 별도 worker 분산 |
|---|---|---|---|
| 실행 위치 | 호스트/하이퍼바이저 (레포 밖) | `rollout.yaml` 1줄 | 새 노드 join + 스케줄링 제약 추가 |
| 실현 가능성 | **저장소에서 확인 불가 - 사용자 확인 필요** | 확인됨(코드 변경만) | **저장소에서 확인 불가 - 사용자 확인 필요** |
| 확보되는 CPU headroom(2-vLLM 동시 기준) | 4코어+ 여유(12-8) | 2코어 여유(8-6) | 사실상 무제한(노드 분리) |
| vLLM 자체 성능 영향 | 없음(4코어 그대로) | 있음(동시 처리량/지연 저하 가능 - §10.3 실측) | 없음(4코어 그대로) |
| SLO/load_ramp 재검증 필요 여부 | **불필요** | **필요**(§10.6) | 불필요(자원 관점) - 단 노드 간 배치 변수로 pod_kill/network_degrade 시나리오 자체의 재검증은 필요할 수 있음 |
| 적용 자체의 위험 | VM 리사이즈 중 노드 일시 중단 가능(호스트 재부팅 여부에 따라 다름 - 미확인) | **적용 순간 새 preview 발생 - 지금 피하려는 상황을 순간적으로 재현** | 새 노드 join·Calico 확장·anti-affinity 설정 등 변경 범위가 가장 큼 |
| 상대적 소요/복잡도 | 호스트 자원에 달림(미확인) | 가장 빠름(코드 변경만, 재보정 작업이 별도로 붙음) | 가장 큼(인프라+일정 비교 필요, 사용자 판단 사항으로 남김) |

각 옵션의 실행 여부·순서는 판단하지 않고 비교만 보고한다. Rollout·노드·
VM 설정 변경, preview 생성 전부 하지 않았다.

## 11. worker 12코어 확장 가능성 확인 - OpenStack Nova라 기각 (2026-09-18)

30분 한도로 읽기 전용 확인. `kubectl exec`로 `sj-worker`의 DMI 정보를
직접 조회한 결과 `sys_vendor=OpenStack Foundation`, `product_name=
OpenStack Nova` - 학교/기관이 운영하는 공유 OpenStack 위의 테넌트
인스턴스임을 실측 확인했다. 이 세션이 접속한 로컬 노트북(Windows,
Samsung 960XFG, 논리 프로세서 16개·RAM 16GB)은 하이퍼바이저가 아니다
(`VBoxManage`/`vmrun`/`virsh` 전부 없음, `~/.ssh/config`에 `sj-control`/
`sj-worker`가 `ubuntu` 계정+개인키로 원격 등록돼 있어 SSH/kubectl
클라이언트일 뿐임을 확인 - 애초에 두 VM이 각각 16GB를 쓰는데 이 노트북
자체가 16GB뿐이라 host일 수 없음).

호스트(OpenStack 컴퓨트 노드)의 실제 물리 코어·스레드 수, 다른
테넌트와의 오버커밋 비율은 테넌트 권한으로는 확인할 방법이 없다.
vCPU 변경(flavor resize)은 통상 인스턴스 **정지 후에만** 가능해(hot-
resize 아님) stop→resize→start 사이클 동안 `sj-worker`(vLLM·recovery-
policy·chaos-mesh·monitoring 대부분이 위치)가 전부 중단되고, 원하는
12vCPU flavor가 프로젝트 쿼터에 있는지도 별도(관리자) 확인이 필요하다.
사용자의 사전 판단 기준("확인할 수 없거나 과할당·복잡한 변경이면
즉시 3코어 하향안으로 전환")에 따라 **12코어 확장안은 기각**하고
3코어 하향안(`lab-cpu3-v1`)으로 전환했다.

## 12. `lab-cpu3-v1` 자원 재설계 - 결정 근거

이번 변경은 임의 성능 하향이 아니라 **8vCPU 단일 worker에서 active·
preview 동시 운영 시 시스템 안정성을 확보하기 위한 자원 재설계**다.
근거(전부 §10에서 실측):

1. 8vCPU 노드에서 vLLM 2개(active+preview)가 각각 4코어 limit이면
   합계가 allocatable(8)의 **100%** - system 프로세스가 경쟁 없이 쓸
   여유가 구조적으로 없었다.
2. vLLM은 콜드스타트뿐 아니라 **실제 동시 처리 부하에서도 4000m(4코어
   limit 전부)까지 실측 확인** - 이론상 한도가 아니라 실제로 다 쓰는
   워크로드다.
3. `sj-worker`는 system-reserved/kube-reserved가 전혀 설정 안 돼
   있다(allocatable=capacity) - kubelet 자신을 위한 보장된 여유가 0.

**결정**: 각 vLLM을 3코어로 제한해 2개 동시 운영 시 최대 6코어만
쓰도록 하고, 시스템용으로 2코어 상당의 여유를 남긴다.

**한계(반드시 남겨야 하는 사실)**: 이건 **kubelet의 강제 예약이 아니라
워크로드 limit을 통한 사실상(de facto) headroom**이다 - system-
reserved처럼 커널이 보장하는 게 아니라, "두 vLLM이 동시에 최대치를
써도 6코어까지만"이라는 상한일 뿐이고, 그 상한 자체를 넘는 제3의
프로세스(예: 예상 밖의 시스템 부하)가 겹치면 여전히 압박이 생길 수
있다. **실제 운영환경이라면 12vCPU 이상 전용 노드풀 + system-reserved
명시 설정 + 오토스케일링을 권장**하며, 이번 3코어 하향은 어디까지나
학교 실습 클러스터(OpenStack, 노드 확장 불가)라는 제약 안에서의 완화
조치임을 명시한다.

**자원 프로필 이름**: `lab-cpu3-v1`(2026-09-18 확정) - CPU limit만
4→3, request(`2`)·메모리(`4Gi`/`6Gi`)·모델(`Qwen/Qwen2.5-0.5B-
Instruct`)·probe 설정은 전부 그대로. `gitops/apps/vllm-serving/
rollout.yaml` 1줄만 변경.

기존 4코어 기준 SLO v2/load_ramp 확정 설정(`experiment-contract.md`
§4)은 삭제·수정하지 않고 그대로 보존하며, 새 자원 구성에서 재검증
전까지 잠정 무효로 표시했다(해당 절 참고).

## 13. `HEADROOM-MIGRATION-PILOT-01` - 첫 콜드스타트·promotion 완료 (2026-09-18)

`lab-cpu3-v1` 적용(05:16:50Z) 후 첫 콜드스타트를 관찰하다 모니터링
스크립트 자체의 버그를 발견해 재분류했다 - 전체 원본 실측값·gap 백필
증거는 `experiments/results/pilot/headroom-migration-pilot-01-
20260918T051650Z.json`에 보존(`is_pilot=true`, `exclusion_reason=
monitoring_tool_changed_during_run`, 공식 콜드스타트 3회에서 제외).

**모니터링 버그**: 1차 스크립트가 정상적인 `Unhealthy: Startup probe
failed`(모델 로딩 중 반복 실패 - `failureThreshold: 90` 설계 의도)를
위험 이벤트로 오판해 관찰 시작 1.3초 만에 조기 abort했다. 직접 재확인
결과 실제 클러스터·파드엔 전혀 영향 없었음(Running, restart_count=0,
Node Ready) - 관찰 공백(05:18:49~05:20:54Z, 125초)은 K8s
events·restart count·Node condition·Prometheus CPU/load1으로 사후
백필했고 전부 정상 범위였다. 버그를 고쳐 `experiments/coldstart_
monitor.py`로 판정 로직을 분리·오프라인 테스트 8개로 고정했다(커밋
`faa7a0f`).

**콜드스타트 결과**: apply~Ready 246.3초(4분6초). Ready 이후 10분
안정성 관찰(39회 polling) 전부 clean - Node Ready 유지, pressure
전무, 양쪽 파드 restart_count=0 유지, 위험 이벤트 0건, active
completion 39/39 성공.

**promotion**: 로컬에 `kubectl-argo-rollouts` 바이너리가 없어
recovery-policy 컨테이너 안의 `/usr/local/bin/kubectl-argo-rollouts`
(그 서비스 자신의 promote 코드가 쓰는 바이너리와 동일)를 `kubectl exec`
로 직접 호출 - `rollouts_client.py`의 `promote_via_cli()`와 같은
메커니즘 재사용. active selector가 신규(3코어) revision으로 전환된
것까지 직접 확인(requested가 아니라 verified). 승격 직후 첫 completion
요청 1회가 10초 타임아웃났으나 즉시 재시도 시 200/1.44초로 정상화, 새
파드 로그에 오류 없음 - Node/파드 재시작·pressure 전혀 없어 vLLM 첫
실제 추론 요청의 웜업 비용으로 추정한다(원인 확정 안 함, 관찰 사실로만
기록). 이후 completion 3회 연속 정상(0.71/0.34/0.24초).

**최종 상태**: 이전 4코어 revision 자동 scale-down 확인, 3코어
revision(`vllm-serving-69544744bf-hrnqn`) 단독 유지, Node Ready,
recovery-policy `/healthz` 200.

**참고**: 이 콜드스타트는 active=4코어 + preview=3코어의 과도기
조합이었다(promotion 전까지) - active도 3코어인 진짜 3+3 비교는
다음 공식 콜드스타트 3회에서 확인한다. 아직 SLO v2·load_ramp
재보정으로는 넘어가지 않았다.

## 14. 공식 `HEADROOM-COLDSTART-01` - 3코어+3코어, preview/active 경로
분리 진단 (2026-09-18, 1/3회차)

migration pilot의 promotion 직후 completion 10초 timeout 원인(모델 웜업
vs Service/Endpoint 전환 지연)을 추정하지 않고 분류하기 위해, 동결된
`experiments/coldstart_monitor.py`를 재사용하는 공식 스크립트로 promotion
전 `vllm-preview` 직접 경로와 promotion 후 `vllm-active` 경로를 각각
실측했다. 트리거는 `rollout.yaml`의 `spike-revision` annotation 값만
변경(커밋 `ed55856`) - 자원·probe·모델 설정 무변경, active(3코어)도
그대로.

**headroom(자원 안정성) - PASS**: apply~Ready 176.1초. Ready 후 10분
안정성(39회 polling) 전부 clean - Node Ready 유지, pressure 전무, 양쪽
파드 restart_count=0 유지, 위험 이벤트 0건, active completion 39/39
성공(0.66~1.1초 - migration pilot 대비 더 안정적인 범위). 이 구간 동안
preview는 `/health` 기준 Ready였지만 실제 추론 요청은 한 번도 안
들어갔다(아래 진단에서 처음 들어감).

**전환 품질 진단(별도 판정) - 결정적 실측**:
- promotion 전 `vllm-preview` 직접 completion 3회 - **3/3 전부 10초
  타임아웃**(http_code=000, 각 10.4~10.5초). 이게 이 preview pod에 대한
  최초의 실제 추론 요청이었다.
- promotion: `active_selector_changed_at` +0.017초, `endpointslice_
  changed_at` +0.056초 - Service/Endpoint 전환 자체는 사실상 즉시
  일어났다(전환 지연이라 부를 만한 게 없음).
- promotion 후 `vllm-active`(같은 pod) completion 5회 연속 - **5/5 전부
  즉시 성공**(첫 성공 promotion +0.935초, 이후 0.686~0.915초).

**분류(관찰값 기준, 추정 아님)**: `preview_first_request_timeout` -
같은 pod이 promotion 전엔 3/3 타임아웃, promotion 후(불과 8~17초 뒤,
Service/Endpoint는 이미 그 훨씬 전에 즉시 전환 완료)엔 5/5 즉시 성공한
패턴은 Service/Endpoint 전환 지연 가설과는 맞지 않고(전환 자체가 이미
끝나 있었으므로) 모델 첫 추론 자체의 웜업 비용 가설과 부합한다. 다만
이번 1회차만으로 확정하지 않는다 - 나머지 공식 2회에서 재현되는지가
최종 판정 기준이다.

**최종 상태**: 이전 revision(`69544744bf`) 자동 scale-down 확인, 신규
3코어 revision(`75d8859d89`)만 단독 유지, Node Ready, recovery-policy
`/healthz` 200. 원본 실측 전체는 `experiments/results/headroom/
headroom-coldstart-01-20260918T054527Z.json`에 보존(gitignore 대상 -
`results/pilot/`이 아니라 `results/headroom/`에 둬서 `collect_metrics.py`
의 `load_all_results()` 스캔 대상에서 제외했다 - migration pilot 기록도
같은 이유로 이 디렉터리로 옮김).

아직 02·03회차나 SLO v2·load_ramp 재보정으로는 넘어가지 않았다.

### 14.1 사후 로그 포렌식 - preview 타임아웃 3건 서버측 흔적 확인 (읽기 전용, 02회차 착수 전)

§14의 `preview_first_request_timeout` 판정에 대해 "모델 웜업"을 확정
짓지 않고, 02회차 설계 전에 지시받은 5개 항목을 로그로만 재확인했다.
새 revision 생성·설정 변경 없음.

**조사 대상**: 01회차에서 preview였고 promotion 후 유일하게 남은 pod
`vllm-serving-75d8859d89-9bq8z`. (1) `--since-time="2026-09-18T05:58:00Z"
--timestamps`로 3건의 preview 진단 요청 시각(05:58:23~05:58:54) 전후
구간을 좁게 조회, (2) 컨테이너 시작부터 전체 로그를 `grep -iE
"error|warn|exception|Loading|compil|engine|Started server"`로 넓게 조회.

**항목 1-2 (요청이 실제로 pod에 도착했는가)**: 좁은 구간(05:58:00~
06:03:38, 약 150줄)에 `/health`(5초 간격)·`/metrics` GET 로그만 있고,
`POST /v1/completions` 로그는 **0건** - 3건의 preview 요청 시각대에
매칭되는 access log가 전혀 없다.

**항목 3 (10초 timeout 이후 서버에서 완료됐는가)**: 조회된 전체 로그를
통틀어 최초의 `POST /v1/completions` 로그가 `05:59:03.080883Z`
(`10.244.36.7:55864 - "POST /v1/completions HTTP/1.1" 200 OK`) - 이는
driver가 기록한 promotion 후 첫 completion 성공 시각과 정확히 일치한다.
엔진 주기 통계 로그(`[loggers.py:310] Avg prompt throughput...`)도
전체 로그에서 최초 등장이 `05:59:03.891869Z`로 동일 시점이다. 즉 3건의
preview 요청이 05:58:23~05:59:03(약 40~80초) 사이 어느 시점에든 서버
측에서 지연 완료됐다는 로그 흔적도 없다.

**항목 3의 한계(확인 불가 명시)**: uvicorn류 access log는 요청이
"도착한" 시점이 아니라 "응답을 반환한" 시점에 찍힌다. 따라서 로그가
없다는 사실만으로는 "애초에 pod에 도달하지 않음"과 "도달했지만 이
로그를 조회한 시점까지도 응답을 못 내고 있었음(예: 여전히 처리 중이거나
소켓 종료로 응답을 못 내보냄)"을 구분할 수 없다. 이 둘을 가르려면
tcpdump/소켓 레벨 증거가 필요하고, 이는 이번 읽기 전용 로그 점검 범위
밖이다 - **확인 불가**로 남긴다.

**항목 4 (preview/active 요청이 코드상 정말 동일했는가)**:
`headroom_coldstart_01_official.py`의 `completion_once(target_host,
timeout_sec=10.0)`을 재확인 - payload
`{"model":"Qwen/Qwen2.5-0.5B-Instruct","prompt":"Hi","max_tokens":1}`,
`curl --max-time {timeout_sec}`, URL 템플릿
`http://{target_host}.vllm-serving.svc.cluster.local:8000/v1/completions`
모두 고정. 실제 4개 호출 지점(라인 140/178/202/238) 전부 `timeout_sec`
인자를 생략해 기본값 10.0초를 그대로 쓴다. preview 진단(라인 202,
`target_host="vllm-preview"`)과 active 측정(라인 140/178/238,
`target_host="vllm-active"`) 사이 차이는 `target_host` 문자열 하나뿐 -
"timeout이나 payload가 달라서"라는 설명은 코드로 배제된다.

**부수 관찰(5개 항목 밖, 인과관계 미확인)**: 항목 1-2의 넓은 조회 중
같은 pod의 컨테이너 시작 로그(`05:47:36`, OpenMP 스레드 바인딩)에
`local_rank=0, core ids=[0, 1, 2, 3, 4, 5, 6]` / `reserved_cpus=[7]`가
찍혀 있다 - 합쳐서 0~7 총 8개 값으로, 이 파드의 cgroup CPU limit인
3코어가 아니라 **노드 전체 allocatable(8코어) 기준으로 보이는 값**이라는
점은 로그에서 직접 관찰된 사실이다. 다만 이게 실제로 vLLM/OpenMP가
cgroup 쿼터를 무시하고 스레드를 스폰한다는 뜻인지, 그리고 그것이 preview
첫 요청 지연에 기여했는지는 이 로그만으로는 **확인 불가** - in-container
cgroup/cpuset 조사가 별도로 필요하며 이번 점검 범위 밖이라 참고 사실로만
기록한다.

**종합**:
- 직접 확인된 사실: (a) 3건의 preview 요청 시각대엔 completions 관련
  access log가 전무, (b) 이후 첫 promotion-후 요청 이전까지도 지연 완료를
  나타내는 로그가 없음, (c) 모델 safetensors 로딩은 `05:48:01.660Z`에
  이미 끝나 있었음(`Loading safetensors checkpoint shards: 100%
  Completed | 1/1 [00:08<00:00, 8.26s/it]`) - 타임아웃 구간(05:58:23~)보다
  약 10분 전이라 "아직 모델 로딩 중이라서"라는 단순 설명과는 맞지 않음,
  (d) preview/active 요청은 코드상 `target_host`만 다르고 완전히 동일.
- 확인 불가: 요청이 실제로 pod 소켓까지 도달했는지 여부(위 한계 참고);
  최초 추론 요청에서 실제로 무엇이 지연을 유발했는지의 메커니즘(예: 최초
  요청시 lazy한 KV-cache/스레드풀 초기화 등은 전부 가설이며 로그상 직접
  근거 없음); CPU affinity 부수 관찰과 지연의 인과관계.
- §14의 결론은 바뀌지 않는다 - 웜업은 여전히 **유력 가설**일 뿐이고,
  02·03회차에서의 재현 여부가 최종 판정 기준이라는 원래 입장을 유지한다.

### 14.2 cgroup CPU quota/cpuset 확인 (읽기 전용, 02회차 착수 전)

§14.1의 부수 관찰(OpenMP가 core ids 0-6 + reserved_cpus=[7], 총 8개를
봄)이 `lab-cpu3-v1`의 3코어 제한과 실제로 어떤 관계인지 확인하기 위해,
같은 pod(`vllm-serving-75d8859d89-9bq8z`, 조사 시점 기준 재확인 -
0 restart, age 35분, 여전히 단독 pod) 안에서 cgroup 파일을 직접
읽었다. 새 revision·설정 변경 없음.

**직접 측정한 사실**:
- `stat -fc %T /sys/fs/cgroup` → `tmpfs`, `/sys/fs/cgroup/cgroup.controllers`
  없음 → **cgroup v1**(v2 unified hierarchy 아님).
- `cpu/cpu.cfs_quota_us` = `300000`, `cpu/cpu.cfs_period_us` = `100000`
  → quota/period = **3.0** → `rollout.yaml`의 `resources.limits.cpu: "3"`과
  정확히 일치. **3코어 CFS 대역폭 제한은 실제로 적용되어 있음을 계산으로
  확인**.
- `cpuset/cpuset.cpus` = `0-7` (8개 전부) - **cpuset 자체는 3으로 제한되어
  있지 않다**. `nproc`과 `/proc/cpuinfo`의 processor 수 둘 다 `8`.

**해석(일반적으로 문서화된 Linux cgroup v1 동작이며, 이번 측정으로 이
pod에 실제 적용됨을 확인함)**: cgroup v1에서 CFS quota(`cpu.cfs_quota_us`/
`cpu.cfs_period_us`)와 cpuset(`cpuset.cpus`)은 **서로 다른 독립된
컨트롤러**다. quota는 "주어진 period(100ms)당 총 CPU-시간 예산"만
제한하고, cpuset은 "어느 코어에서 스케줄될 수 있는지"만 제한한다.
Kubernetes가 (CPU Manager의 `static` policy 없이) `resources.limits.cpu`로
설정하는 기본 방식은 quota만 건드리고 cpuset은 노드 전체로 남겨둔다 -
이 클러스터가 정확히 이 상태다(cpuset=0-7).

그 결과, `nproc`/`sched_getaffinity`/`/proc/cpuinfo`처럼 "몇 개 코어가
보이는가"를 묻는 표준 API는 quota를 전혀 반영하지 못하고 cpuset 기준인
8을 그대로 돌려준다. OpenMP는 기본적으로 스레드 풀 크기를 이런 API로
정하므로, §14.1에서 관찰한 `core ids=[0..6]+reserved_cpus=[7]`(합 8)은
**우연이나 오설정이 아니라 이 조합(quota만 설정, cpuset 미설정)에서
일반적으로 예상되는 동작**이다 - 즉 vLLM(OpenMP)은 자신이 3코어
예산만 받았다는 사실을 모른 채 8개 코어 기준으로 스레드를 구성할
가능성이 있다.

**확인 불가로 남기는 부분**: 위 메커니즘은 일반적으로 성립하지만, 이게
실제로 01회차의 3건 타임아웃 순간에 CFS throttling을 유발했는지는
이번 측정으로 확인되지 않는다. `cpu.stat`의 `nr_throttled`/
`throttled_time`은 누적 카운터라 지금 읽어도 05:58:23~05:58:54 구간만
분리할 수 없다 - 그 구간에 한정한 throttling 여부를 보려면 Prometheus에
그 시각대 `container_cpu_cfs_throttled_seconds_total`(cAdvisor) 시계열이
남아 있는지 확인해야 하는데, 이는 지시받은 5개 확인 항목 밖이라 이번엔
수행하지 않았다 - 필요하면 별도로 승인받아 진행하겠다.

02회차 설계·실행으로는 아직 넘어가지 않았다 - 사용자 검토·승인 대기.

## 15. 공식 `HEADROOM-COLDSTART-02` - Pod IP 우선 진단 프로토콜 (2026-09-18, 2/3회차)

§14.1/§14.2 검토 후 승인받은 프로토콜대로, 추론을 유발하지 않는 경로
확인부터 시작해 최초 추론을 Service가 아니라 preview Pod IP로 직접
보내 애플리케이션 자체 지연/Service 경로 문제/kubectl exec 계층 문제를
분리했다. 트리거는 `rollout.yaml`의 `spike-revision` annotation 값만
변경(커밋 `a37f4b9`) - 자원·probe·모델 설정은 01회차와 동일(3코어+3코어).
frozen `coldstart_monitor.py`는 이번에도 변경하지 않았다.

**headroom(자원 안정성) - PASS**: apply~Ready 350.3초(01회차 176.1초보다
느림 - 원인 확인 안 함, 재현성 확인은 범위 밖). Ready 후 10분 안정성
(43회 polling) 전부 clean - Node Ready 유지, pressure 전무, 양쪽 pod
restart_count=0, 위험 이벤트 0건, active completion 43/43 성공
(0.54~2.19초).

**1단계 - 추론 없이 경로만 확인**: source pod
`recovery-policy-76fbd457b8-5hmvj`(uid
`2f3b02d1-efbc-4c3e-bec5-3ad3041d8cc8`), preview pod
`vllm-serving-7d6fbc8f96-wf96q`(IP `10.244.36.47`). DNS
`vllm-preview.vllm-serving.svc.cluster.local` -> `10.110.177.238`(Service
ClusterIP와 정확히 일치). EndpointSlice가 이 preview pod의 IP·port
8000을 정확히 가리킴을 확인. ClusterIP `/health`(connect 0.6ms, TTFB
2.1ms) · Pod IP `/health`(connect 0.27ms, TTFB 1.5ms) 둘 다 200 -
**추론 이전 시점에 DNS·EndpointSlice·TCP·health 경로는 전부 정상이고
사실상 즉시 응답했다.**

**2단계 - 최초 추론(Pod IP 직접, 60초 예산) - 결정적 실측**: 이
preview pod에 대한 최초의 실제 추론 요청을 Service를 거치지 않고 Pod
IP(`10.244.36.47`)로 직접 전송. **60초 예산 안에 성공(200 OK)했고,
실제로는 27.53초 소요** - `time_connect=0.53ms`(즉시)인데
`time_to_first_byte=27.00초`. 즉 지연은 TCP/네트워크 구간이 아니라
**connect 이후~첫 바이트 사이, 애플리케이션 내부 구간에 전부 몰려
있다.** 같은 시각대 vLLM 로그를 보존했고, `POST /v1/completions`
200 OK 로그가 정확히 06:42:31.767(요청 완료 시각과 일치)에 처음
등장 - 그 전까지는 `/health`·`/metrics`만 있었다(01회차 §14.1의
"access log가 요청 완료 시점에 찍힌다"는 한계와 일치하는 패턴).

**3단계 - 사후 검증(preview Service 3회 -> promotion -> active Service
5회)**: 최초 추론 직후 같은 pod에 `vllm-preview` Service로 3회 -
**3/3 즉시 성공**(1.31/0.71/0.75초, TTFB 0.26~0.37초로 active와 동일한
정상 범위). Promotion: selector 전환 +0.035초, EndpointSlice 전환
+0.091초(역시 즉시). `vllm-active`로 5회 - **5/5 즉시 성공**
(0.60~0.79초), 첫 성공 promotion +0.878초.

**분류(관찰값 기준, 4갈래 판정 그대로 적용)**: 어떤 probe에서도
`outer_timed_out`(kubectl exec/subprocess 자체 timeout) 없음 -> 측정
계층 문제 아님. `vllm-preview` Service 3회 전부 성공 -> preview Service
경로 문제 아님. Pod IP 요청이 기준선(2.0초)을 크게 초과(27.53초) ->
**`pod_ip_first_inference_slow_possible_init`** - TCP/health는 모두
정상이고 Pod IP 직접 요청조차 오래 걸렸다는 조건에 해당 - "애플리케이션
첫 추론 초기화 비용 가능성"으로 분류.

**이번 회차가 01회차보다 더 직접적으로 배제/지지하는 것**:
- DNS 오설정, EndpointSlice 미전파, Service/kube-proxy 라우팅 문제는
  **배제** - Pod IP 직접 요청도 똑같이 27초 걸렸고, health는 두 경로
  다 즉시 응답했으므로 네트워크 경로 자체는 문제가 아니었다.
- "10초보다 얼마나 더 걸리는지 몰랐던" 01회차와 달리, 이번엔 실제
  소요시간(27.53초)과 지연이 걸린 정확한 구간(connect 이후~첫 바이트)을
  직접 측정했다.
- 첫 요청 이후로는 Service 경로를 포함해 전부 즉시 정상 - 반복되는
  문제가 아니라 **1회성 비용**이라는 점도 이번엔 직접 확인됐다(01회차는
  Service 경유 3회 전부 실패라 "1회성"인지조차 알 수 없었다).

**확인 불가로 남기는 부분**: 애플리케이션 내부의 정확히 어떤 코드
경로가 27초를 쓰는지는 HTTP 레벨 블랙박스 타이밍만으로는 확인 불가.
`--enforce-eager`가 명시적으로 켜져 있어(`rollout.yaml` 주석 참고,
torch.compile 중 OOM 확인되어 배제됨) torch.compile 지연은 아니다.
§14.2에서 확인한 cgroup 상태(CFS quota=3코어인데 cpuset=8코어라
OpenMP 등이 8코어 기준으로 스레드를 구성할 수 있음)가 그럴듯한 후보
메커니즘이지만, 이번 27초 구간에 실제로 CFS throttling이 발생했는지는
측정하지 않았다 - 여전히 **확인 불가**하고, 정확한 내부 메커니즘도
미확정이다.

**최종 상태**: Node Ready, 양쪽 pod Running(구 revision은
`scaleDownDelaySeconds: 30` 대기 중이라 promotion 직후 스냅샷엔 아직
남아있음 - 정상), recovery-policy `/healthz` 200. 원본 실측 전체는
`experiments/results/headroom/headroom-coldstart-02-20260918T062557Z.json`
에 보존(gitignore 대상, `results/headroom/` 유지).

**상태 확정(2026-09-18, 사용자 승인)**: `HEADROOM-COLDSTART-02`는
**공식 headroom 2/3 PASS**로 확정됐다. 위 4가지 관찰(TCP·`/health`는
즉시 정상 / 최초 Pod IP completion만 27.53초 소요 / 이후 동일 pod의
completion은 정상 / selector·EndpointSlice 전환은 즉시 완료)에 근거해,
**"Kubernetes Ready 판정 이후에도 최초 추론 초기화 비용이 존재한다"는
운영 현상 자체는 확인된 것으로 기록한다.** 다만 그 비용을 유발하는
내부 세부 메커니즘(§14.2의 cgroup quota/cpuset 불일치 포함, 정확히 어느
코드 경로가 27초를 쓰는지)은 여전히 **미확정**으로 남긴다 - 확인된 것은
"현상이 실재한다"는 사실이지 "왜 발생하는가"가 아니다.

이 확정에 따라 03회차 전에 이 현상을 startupProbe 교체로 영구 해결하는
작업을 시작한다 - §16 참고.

## 16. `lab-cpu3-warm-v1` - startupProbe를 localhost 합성 completion 확인으로 교체 (2026-09-18, 구현+오프라인 테스트만, 실클러스터 미적용)

§15에서 확정된 "Ready 이후에도 최초 추론 초기화 비용이 존재한다"는
운영 현상을 03회차 전에 영구 해결한다. 단순 `/health` 통과만으로
Ready 처리하는 현재 startupProbe로는 이 비용을 못 잡아낸다 - 실제
첫 추론이 끝났는지를 직접 확인하도록 startupProbe 자체를 바꾼다.

**구현 방식 선택**: 저장소를 확인한 결과 `Dockerfile.cpu`(rollout.yaml
주석의 이미지 빌드 출처)가 이 저장소 안에 아예 없다 - `vllm-cpu-env:latest`는
`sj-worker` 노드에서 버전관리 밖으로 로컬 빌드된 이미지이고, 이
세션에는 노드에서 이미지를 재빌드할 수단(SSH·원격 빌드 트리거)이 없다.
따라서 1순위(이미지에 포함)는 실행 불가로 배제하고, 2순위인 ConfigMap
마운트 방식을 채택했다 - 저장소에 기존 ConfigMap 패턴은 없어 K8s
표준 방식(볼륨 마운트)으로 새로 만들었다.

**변경 파일**:
- `gitops/apps/vllm-serving/probes/warmup_probe.py`(신규, 단일 출처) -
  `localhost:8000/v1/completions`에 `{"model": "Qwen/Qwen2.5-0.5B-Instruct",
  "prompt": "Hi", "max_tokens": 1}`로 최소 요청, 60초 내부 timeout.
  종료 코드: `0`=성공, `2`=연결 실패, `3`=timeout(bare `TimeoutError`와
  `URLError`로 감싸인 timeout 둘 다 처리), `4`=비정상 status,
  `5`=응답 형식 오류(JSON 파싱 실패 또는 `choices[0].text` 없음).
- `gitops/apps/vllm-serving/probes/test_warmup_probe.py`(신규) -
  `urllib.request.urlopen`을 모킹한 오프라인 테스트 8건.
- `gitops/apps/vllm-serving/warmup-probe-configmap.yaml`(신규, 생성
  파일) - `kubectl create configmap ... --from-file=probes/warmup_probe.py
  --dry-run=client -o yaml`로 위 스크립트에서 그대로 생성(수기 복사
  아님 - 전사 오류로 테스트 대상과 배포본이 어긋나는 것을 방지). 파일
  상단에 재생성 명령과 "스크립트 수정 시 반드시 재생성" 경고 주석 포함.
- `gitops/apps/vllm-serving/rollout.yaml` 수정:
  - `startupProbe.httpGet` → `startupProbe.exec.command:
    ["python3", "/opt/probes/warmup_probe.py"]`, `periodSeconds: 10`·
    `failureThreshold: 90`(기존 유지, 요구사항 6) 그대로, `timeoutSeconds: 65`
    신규 추가 - K8s exec probe 자체의 timeout(기본 1초!)이 스크립트
    내부 60초 timeout보다 먼저 프로세스를 죽이면 깨끗한 종료 코드를
    낼 수 없어, 스크립트가 자기 timeout으로 먼저 끝나도록 5초 여유를
    더했다.
  - `volumeMounts`/`volumes`에 `warmup-probe`(ConfigMap `vllm-warmup-probe`,
    `/opt/probes`에 read-only 마운트) 추가.
  - `readinessProbe`/`livenessProbe`는 전혀 손대지 않음(요구사항 8).
  - `overlays/network-tolerant/probe-timeout-patch.yaml`은 확인만 하고
    수정 안 함 - 그 파일은 `readinessProbe`/`livenessProbe.timeoutSeconds`만
    JSON patch로 건드리고 `startupProbe`나 volume은 다루지 않아 이번
    변경과 겹치지 않는다(요구사항 9).
  - `spike-revision` → `"HEADROOM-COLDSTART-03"`, CPU 리소스 주석
    `lab-cpu3-v1` → `lab-cpu3-warm-v1`.

**오프라인 테스트 결과**: `pytest gitops/apps/vllm-serving/probes/test_warmup_probe.py -v`
→ **8/8 PASS** - 정상 completion(exit 0), 연결 실패(exit 2), bare
`TimeoutError`(exit 3), `URLError`로 감싸인 timeout(exit 3), HTTP
500(exit 4), JSON 파싱 실패(exit 5), `choices` 필드 누락(exit 5),
실제 전송 payload의 `max_tokens==1`·`model` 일치 확인 각 1건씩.

**검증**: `kubectl apply --dry-run=client`로 `rollout.yaml`·
`warmup-probe-configmap.yaml` 둘 다 스키마 유효성 확인(클러스터 미접촉).
`volumeMounts`/`volumes`/`configMap.name` 3곳의 이름이 서로 정확히
일치하는지 직접 재확인함(dry-run은 이 참조 일치까지는 검증 안 함).

**아직 하지 않은 것**: 실클러스터에 `kubectl apply` 미실행, `HEADROOM-COLDSTART-03`
미실행 - 사용자 검토·승인 대기.

## 17. `HEADROOM-COLDSTART-03-ATTEMPT1` - 측정 버그로 인한 오탐 abort, 정리 및 수정 (2026-09-18)

§16 구현 승인 후 final preflight(저장소 clean·HEAD 일치, 단일 3코어
active·preview 없음, Node Ready, Chaos CR 없음, `python3` 실행파일
확인, warmup 모델명이 실제 서빙 모델과 3중 일치, Argo CD 미설치 확인
- 배포는 순수 `kubectl apply`뿐이라 순서는 직접 통제, `kubectl diff`로
ConfigMap·volume·volumeMount·exec startupProbe 4가지가 모두 포함되고
readinessProbe/livenessProbe·network-tolerant overlay는 안 건드림을
확인) 전부 통과 후 ConfigMap -> Rollout 순서로 적용, 새 preview
`vllm-serving-748f568b45-2f7gx` 생성 확인.

**측정 버그 발생**: 콜드스타트 관찰 중 `subprocess.run(..., text=True)`가
Windows 로캘 기본 인코딩(cp949)으로 kubectl의 UTF-8 출력을 디코딩하려다
실패하는 버그를 발견 - 특히 `risky_events()`가 이 예외를 조용히 삼켜
매 polling마다 위험 이벤트를 사실상 탐지 못하고 있었다. 관찰을 중단하고
`encoding="utf-8"`을 모든 `subprocess.run(text=True)` 호출 7곳에
추가해 수정, 같은 pod(이미 콜드스타트 진행 중)를 대상으로 재개했다 -
`compute_effective_start_mono`가 이런 관찰 재개 시나리오를 위해 이미
있어 재적용 없이 이어갈 수 있었다.

**재개 직후 `ABORTED_READY_BEFORE_WARMUP` 오탐**: 재개 시점엔 이미
Ready 상태였다(수정하는 5분 사이 콜드스타트 자체는 계속 진행됨).
warmup completion 로그 timestamp(`2026-09-18T08:12:51.314199658Z`,
나노초 정밀도)와 Ready condition의 `lastTransitionTime`
(`2026-09-18T08:12:51+00:00`, K8s API가 초 단위로 절삭)을 raw
문자열로 비교(`warmup_log_ts < ready_ts`)했다가 **False**가 나와
"Ready가 warmup보다 먼저 발생"으로 오판정, 지시받은 중단 조건대로
정확히 promotion 전에 abort됐다.

**원인 규명(추정 아니라 직접 로그로 확인)**: 같은 pod의 원본 vLLM
access log를 직접 조회한 결과:
```
08:12:51.314199658Z  127.0.0.1:59398      POST /v1/completions  200 OK   (startupProbe warmup)
08:12:51.352724455Z  192.168.30.76:41198  GET  /health          200 OK   (최초 /health, readinessProbe)
```
이 구간 이전에는 `/health` 로그가 **단 한 줄도 없다**. K8s는
startupProbe가 성공하기 전엔 readiness/liveness probe를 아예 실행하지
않는다는 것이 문서화된 보장이므로, 최초 `/health`가 warmup POST
완료 38.5ms 뒤에 나타났다는 사실은 "warmup이 Ready보다 먼저 완료됐다"는
직접적이고 정밀한 증거다. abort는 **측정 버그**(문자열 비교 시
`.`(46)이 `+`(43)보다 큰 ASCII 순서 때문에 이른 시각이 늦은 것으로
계산됨 + `lastTransitionTime` 자체가 초 단위로 절삭돼 있어 같은 초
안에서는 이 필드만으로 순서를 확정할 수 없음)였지 실제 시스템
결함이 아니다. **실제 cluster abort·pod/Node 재시작은 전혀 없었다.**

**ATTEMPT1 처리**: 사용자 승인에 따라 이 회차를 공식 3/3에 포함하지
않고 pilot으로 재분류했다. 원본 실측(경로 검증 이전까지의 전체
polling 로그, warmup 로그 라인, Ready transition, pod/Node 상태)은
그대로 보존하고 `is_pilot=true`·`exclusion_reason=
timestamp_comparison_bug_in_monitor`·`included_in_main_analysis=false`
필드만 추가해
`experiments/results/headroom/headroom-coldstart-03-attempt1-20260918T081006Z.json`
로 저장(gitignore 대상, 원본 수치는 전혀 수정하지 않음).

**클러스터 정리**: `kubectl-argo-rollouts abort vllm-serving`으로
promotion 없이 안전하게 중단 - `status.phase=Degraded`는 명시적
abort 후 정상적으로 나타나는 상태 표시일 뿐 장애가 아니다.
확인 결과: `activeSelector`가 원래 3코어 active(`7d6fbc8f96`)를 계속
가리킴, ATTEMPT1 preview pod는 이미 완전히 scale-down됨(pod 목록에서
사라짐, ReplicaSet은 `DESIRED=0`으로 `revisionHistoryLimit` 정책대로
보존), active completion 실측 200 OK로 재확인. 양쪽 Node Ready·
pressure 없음, Chaos CR 0건, 잔존 experiment context 없음.

**수정**: `experiments/timestamp_order.py`(신규, 커밋 `b76c060`) -
`compare_before(a, b)`가 timezone-aware `datetime`으로 정확히 파싱하고,
같은 초 안에서 어느 한쪽이라도 소수초 정보가 없어 실제 순서를 확정할
수 없으면 추정하지 않고 `None`(확인 불가)을 반환한다. 회귀 테스트
7개(정상 순서/같은 초 microsecond만 다른 정밀 순서/`Z`·`+00:00` 형식
동등성/실제 실패(Ready가 먼저) 순서/timestamp 누락/ATTEMPT1 정확한
재현(초단위 절삭 시 None)/양쪽 다 정밀하면 절삭 없이 확정 판정) 전부
PASS, `experiments/` 전체 스위트 87 passed·2 skipped(무관)로 회귀
없음 확인. 03회차 드라이버 스크립트도 이 함수로 교체하고, 같은 초 안
확인 불가(`None`) 상황에 대비해 같은 로그(나노초 정밀도) 안에서
warmup 라인과 최초 `/health` 라인의 등장 순서로 교차 확인하는 대체
경로를 추가했다.

**다음**: annotation에 새 실행 ID를 넣어 완전히 새로운 preview를
생성하고, 수정된 비교 로직으로 공식 `HEADROOM-COLDSTART-03`을
처음부터 다시 수행한다 - 이 정리·수정 결과가 확인된 뒤 진행.

## 18. 공식 `HEADROOM-COLDSTART-03` - warmup gate 검증 성공, 공식 headroom 3/3 완료 (2026-09-18)

§17 정리·수정 승인 후 final preflight(저장소 clean·HEAD `d36fd92` 일치,
active `7d6fbc8f96` 단독 Running, preview 없음, Node Ready, Chaos 없음,
active completion 200, 수정된 `timestamp_order.py`+동결된
`coldstart_monitor.py` 사용 확인) 전부 통과 후 `spike-revision:
"HEADROOM-COLDSTART-03-RETRY"`(커밋 `2808ea6`)로 완전히 새로운 preview
`vllm-serving-85c55758c6-ljc6n` 생성, 08:47:18Z 적용.

**1. warmup gate - PASS**: `127.0.0.1:53824 - "POST /v1/completions
HTTP/1.1" 200 OK`가 `08:49:58.996551762Z`에 기록됨. `ready_transition_utc
= 08:49:59+00:00`로 서로 다른 초라 `timestamp_order.compare_before`가
보조 근거(로그 순서) 없이 직접 확정: **warmup이 Ready보다 먼저 완료됨
(True)** - 이번엔 같은 초 절삭 문제 자체가 발생하지 않았다(약 3.4ms
차이). apply~Ready 163.7초.

**2. 자원 안정성 - PASS**: 10분 안정성 완료(`abort: null`). 로그 전체를
직접 스캔해 재확인 - `coldstart_poll`/`stability_poll` 79회 전부
`risky_events: []`, `active_completion` 38회 전부 `success: true`
(요약 수치가 아니라 로그 79건·38건을 직접 grep해 확인). Node CPU
스냅샷(apply 시 load1 4.44/iowait 7.2%/사용률 45.5% -> 안정성 종료 시
load1 4.08/iowait 1.0%/사용률 36.4%, Prometheus 실측)도 안정 또는
감소 추세로 이상 없음. 양쪽 Node Ready 유지, pressure·재시작·OOM 전무.

**3. 전환 품질 - PASS**: Ready 직후 preview completion **3/3 성공,
timeout 0건**(1.28/0.57/0.80초, TTFB 0.98/0.26/0.31초 - 01회차의
"3/3 전부 10초 타임아웃"과 정반대). Promotion 요청 09:00:15.633Z ->
EndpointSlice 전환 +0.93초 -> selector 전환 +2.63초(01/02회차의
<0.1초보다 느리지만 여전히 수 초 내). Promotion 후 active completion
**5/5 성공, timeout 0건**(0.48~0.73초, 첫 성공 +0.834초).

**4. 최종 상태 - PASS**: promotion 직후 스냅샷·이후 재확인 둘 다로
검증 - `vllm-serving-7d6fbc8f96`(구)·`vllm-serving-748f568b45`
(ATTEMPT1) 둘 다 `DESIRED=0/CURRENT=0/READY=0`로 scale-down 완료,
`vllm-serving-85c55758c6`만 `1/1/1` 단독 유지. Rollout
`status.phase=Healthy`. 양쪽 Node Ready, Chaos CR·experiment context
0건, recovery-policy `/healthz` 200(재확인).

**종합**: `success_criteria_met: true`,
`transition_verdict: "all_criteria_met..."`. 원본 실측은
`experiments/results/headroom/headroom-coldstart-03-20260918T084718Z.json`
에 보존(gitignore 대상).

**공식 headroom 3/3 완료**: 01(§14, preview 웜업 유력 가설로 분류)·
02(§15, Pod IP 직접 실측으로 웜업 가설 강하게 뒷받침, 사용자 확정)·
03(§18, warmup gate로 구조적 해결 및 실측 검증) 세 회차 모두
headroom(자원 안정성) PASS - `lab-cpu3-v1`(3코어 CPU 제한)의 자원
설계는 3회 반복으로 확인됐다. 추가로 03회차는 01/02가 남겼던 전환
품질 문제(모델 첫 추론 웜업 비용이 Ready 판정에 반영 안 됨)를
`lab-cpu3-warm-v1`(exec startupProbe로 실제 warmup 완료를 Ready 조건에
포함)로 구조적으로 해결하고, 그 해결이 실제로 작동함을 직접 로그
증거로 검증했다. **동결 여부는 사용자 확정 대기** - 확정되면
`gitops/apps/vllm-serving/`의 현재 설정(3코어+exec warmup
startupProbe)이 이후 SLO v2 재검증·load_ramp 재보정의 새 기준선이
된다.

아직 SLO나 load-ramp 재보정으로는 넘어가지 않았다 - 사용자 검토·승인
대기.

## 19. `lab-cpu3-warm-v1` - Phase 8 공식 기준선 동결 (2026-09-18)

**동결 근거**(사용자 확정, §14~§18 실측에 근거): 3코어 구성 콜드스타트
자원 안정성 3/3 PASS(§14/§15/§18) - warmup completion이 Ready보다
먼저 완료됨을 로그로 직접 확인(§18, 08:49:58.996Z < 08:49:59Z,
`timestamp_order.compare_before`로 확정) - preview 3/3·promotion 후
active 5/5 요청 성공, timeout 0건(§18) - Node·Rollout·recovery-policy·
cleanup 최종 상태 정상(§18) - readiness/liveness와
`overlays/network-tolerant/`는 전혀 변경하지 않음(§16에서 확인, 이후
미변경).

**고정 값(2026-09-18, 아래 전부 `kubectl diff`로 live 클러스터와
git 파일이 완전히 일치함을 확인한 시점 기준)**:

| 항목 | 값 |
|---|---|
| CPU 제한 | `resources.limits.cpu: "3"`(`gitops/apps/vllm-serving/rollout.yaml`) |
| startupProbe | `exec.command: ["python3", "/opt/probes/warmup_probe.py"]`, `periodSeconds: 10`, `timeoutSeconds: 65`, `failureThreshold: 90` |
| readinessProbe/livenessProbe | 미변경 - `httpGet /health`, 기존 그대로 |
| ConfigMap | `vllm-warmup-probe`(namespace `vllm-serving`), uid `ac269725-5a33-4538-a711-0e9cad6f0cfe`, 내용은 `gitops/apps/vllm-serving/probes/warmup_probe.py` 단일 출처(생성 파일 `warmup-probe-configmap.yaml`) |
| 이미지 | `docker.io/library/vllm-cpu-env:latest`, digest `sha256:203c637f747a53bbc9914b084d38f37cb06cf4b372152af9e616adbf9d177e35`(노드 로컬 빌드, 레지스트리 미사용) |
| Rollout revision | `rollout.argoproj.io/revision: "23"`, `currentPodHash: 85c55758c6`, `generation: 25`(= `observedGeneration`, 반영 완료) |
| 기준 커밋 SHA | `398b448`(이 문서 작성 시점 HEAD) - `kubectl diff -f rollout.yaml -f warmup-probe-configmap.yaml` 결과 빈 diff로 live와 완전 일치 확인 |

**동결의 의미**: 위 조합(3코어 CPU 제한 + exec 기반 warmup gate
startupProbe + 기존 readiness/liveness)이 Phase 8의 공식 리소스·probe
기준선이 된다. 이후 SLO v2 재검증·load_ramp 재보정은 이 기준선 위에서
수행하며, 이 커밋 이후 `gitops/apps/vllm-serving/`에 대한 어떤 변경도
이 절을 갱신하거나 새 기준선 절을 추가해야 한다.

`docs/design/experiment-contract.md`의 "⚠️ 2026-09-18부로 잠정 무효"
경고(§5 인근 load_ramp 확정 설정)는 이 동결로 재검증 절차가
시작됐음을 뜻하며, 아직 해제되지 않았다 - SLO baseline·load_ramp
재보정이 완료되고 확정될 때 별도로 해제한다.

## 20. SLO baseline 재측정 계획 사전 고정 (측정 전, 2026-09-18)

`lab-cpu3-warm-v1`(§19) 동결 후 CPU 4→3코어 변경(§12)으로 `L_baseline`
(`slo-definition.md` §2, 현재 v2=0.256초)이 더 이상 현재 환경을 반영하지
않는다 - 재측정이 필요하다. `slo-definition.md`의 자체 원칙("실험
데이터를 보기 전에 확정한다")을 그대로 따라, **아래 규칙을 실측 실행
전에 고정**한다 - v1->v2 전환 때 이미 쓴 것과 **동일한 산정 원칙**이며
새로 발명하지 않는다:

1. 측정 도구: `experiments/calibrate_probe_only.py --config
   ../chaos/probe-config.yaml --duration-sec 300`(1 RPS, 300초=300건,
   `chaos/probe-config.yaml`의 payload - `max_tokens=1`, prompt `"Hi"`,
   `vllm-active` Service 대상) - **3회 독립 실행**.
2. 각 회차는 성공률 100%가 아니면 그 회차를 폐기하고 재실행한다(측정
   자체가 오염된 것으로 간주 - `slo-definition.md` v2 조건과 동일).
3. 각 회차의 대표 P95는 `slo_judge.evaluate()`가 반환하는 60초
   슬라이딩 윈도우 포인트 중 **마지막 값**(전체 300초 중 가장 안정화된
   구간)을 쓴다 - v2 확정 시(`0.2565/0.2545/0.2586`) 쓴 것과 동일한
   방식.
4. 새 `L_baseline` = 3회 대표 P95의 **중앙값**.
5. 새 Latency SLO = **2 × 새 L_baseline**(공식 자체는 불변,
   `slo_judge.py`의 `LATENCY_THRESHOLD = 2 * L_BASELINE` 그대로).
   Availability SLO(60초 윈도우 성공률<99%, timeout 30초 실패 처리)는
   `L_baseline`과 무관하므로 변경하지 않는다.
6. 위 1~5 실행 후 나온 수치를 그대로 `slo-definition.md`(새 버전
   섹션+변경이력)와 `experiments/slo_judge.py`(`L_BASELINE` 상수)에
   반영한다 - 결과를 보고 규칙 자체를 바꾸지 않는다.

측정은 아직 시작하지 않았다 - 이 계획을 커밋한 뒤 실행한다.

## 21. SLO baseline 재측정 실행 결과 - SLO v3 확정 (2026-09-18)

§20에서 사전 고정한 규칙대로 `lab-cpu3-warm-v1`(§19) 동결 상태(active
`vllm-serving-85c55758c6-ljc6n` 단독, Chaos·experiment context 없음,
Node Ready)에서 `calibrate_probe_only.py --config
../chaos/probe-config.yaml --duration-sec 300`를 3회 독립 실행했다.

| 회차 | run_id | 성공률 | P95 범위 | 대표 P95(마지막) |
|---|---|---|---|---|
| 1 | `calib-probe-only-20260918T092200Z` | 100.0% | 0.297s~1.093s | **0.300s** |
| 2 | `calib-probe-only-20260918T092940Z` | 100.0% | 0.325s~0.928s | **0.348s** |
| 3 | `calib-probe-only-20260918T093648Z` | 100.0% | 0.313s~0.924s | **0.324s** |

3회 모두 성공률 100%로 §20 규칙의 재실행 조건(미달 시 폐기)에 걸리지
않아 그대로 채택. 각 회차 초반 구간에서 P95가 최대 ~1.1초까지 튀는
구간이 있었다(60초 슬라이딩 윈도우가 아직 표본을 다 채우지 못한
구간의 소표본 노이즈로 추정 - 3회 요청 전부 성공했고 vLLM/Node 쪽
이상 이벤트도 없어 기능적 문제는 아니다). §20 규칙이 "마지막(안정화)
값"을 쓰도록 사전에 고정해둔 덕에 이 노이즈가 결과에 영향을 주지
않았다.

**계산(규칙 그대로 적용, 사후 조정 없음)**: 중앙값(0.300, 0.324,
0.348) = **0.324초** = 새 `L_baseline`. 새 Latency SLO = 2 × 0.324 =
**0.648초**(공식 불변). Availability SLO는 변경 없음(60초 윈도우,
99%, timeout 30초).

**반영**: `docs/design/slo-definition.md`에 SLO v3 섹션 추가(v1·v2는
이력 보존, 삭제 안 함) + 변경이력 기록. `experiments/slo_judge.py`의
`L_BASELINE`을 0.256→0.324로 갱신(`LATENCY_THRESHOLD`는 `2 *
L_BASELINE` 공식이라 자동으로 0.648 반영). `test_slo_judge.py`의
고정값 테스트(`test_calibration_constants_pinned`, 구
`_unchanged`)를 새 값으로 갱신하고, 구 임계치(0.512s)보다는 크지만
신 임계치(0.648s)보다는 작아 더 이상 "위반"을 재현하지 못하게 된
회귀 fixture(`latency=0.6s`)를 0.8s로 올렸다(테스트 의도는 "임계치
초과 latency의 처리 로직 검증"이지 특정 숫자 자체가 아니므로, 임계치
변경에 맞춰 갱신하는 것이 맞다). 전체 스위트 87 passed, 2 skipped
(무관) - 회귀 없음.

`chaos/scenario-load-ramp.yaml`·`chaos/scenario-load-ramp-explore.yaml`·
`docs/design/experiment-contract.md`의 4코어 시절 ramp 단계 값과 그
경고 블록은 이번 작업 범위 밖이라 손대지 않았다 - load_ramp 재보정은
별도 승인 후 진행한다.

원시 calibration CSV 3개는 `experiments/results/probe-calib-*-raw.csv`
에 보존(gitignore 대상, 본 실험 데이터 아님 - calibration 전용).

**상태 확정(2026-09-18, 사용자 승인)**: `lab-cpu3-warm-v1`(§19) 기준선
동결과 SLO v3(`L_baseline=0.324s`, latency threshold=0.648s) 재보정
모두 **완료**로 확정됐다. 현재 유효 기준은 SLO v3다(v1·v2는
`slo-definition.md`에 이력으로만 보존).

다음으로 load_ramp 재보정을 진행한다(§22) - 아직 본 실험(60회)이나
다른 시나리오 실행으로는 넘어가지 않는다.

## 22. `load_ramp` 재보정 계획 사전 등록 (측정 전, 2026-09-18)

SLO v3(§21)와 `lab-cpu3-warm-v1`(§19) 하에서 `chaos/scenario-load-ramp.yaml`
의 기존 단계(4코어·SLO v2 시절 확정, 커밋 `83bb61a`)가 여전히 유효한
경계를 보여주는지 알 수 없다 - CPU는 줄고(4→3코어) threshold는
늘어서(0.512→0.648s) 두 효과가 서로 다른 방향이라 사전 예측이
불가능하다. `slo-definition.md`·`experiment-contract.md` 자체 원칙과
동일하게, **아래를 측정 전에 고정**한다:

1. 기존 0.10/0.25/0.50/0.75/1.00 RPS는 **후보값일 뿐** - 자동으로
   재사용하지 않는다. 새 환경에서 처음부터 재탐색한다.
2. 고정 조건: `lab-cpu3-warm-v1`(3코어+exec warmup startupProbe,
   §19), probe 1 RPS·`max_tokens=1`·`chaos/probe-config.yaml`(SLO v3
   판정용, ramp 요청과 별개), ramp 요청은 기존과 동일하게
   `max_tokens=10`·`prompt="Hello"`(payload를 바꾸면 RPS 효과와
   payload 효과를 구분할 수 없음 - `scenario-load-ramp-explore.yaml`
   자체 원칙 재사용). 각 단계 90초(기존과 동일 - 60초 P95 롤링
   윈도우가 이전 단계 표본을 완전히 밀어낼 시간 확보).
3. 목표 패턴(관찰로 확인, 강제로 맞추지 않음): 낮은 단계=안정(SLO
   준수), 중간 단계=threshold 근접(경계 구간 - 반복시 일부만 위반해도
   문제 아님, v2 때도 그랬다), **후반 최소 2단계=반복적으로 위반**
   (v2 때보다 엄격 - v2는 마지막 1단계만 3/3 위반이었다). 위반 중에도
   **요청 실패(성공률 저하)나 시스템 붕괴는 없어야 한다** - 있으면
   그 후보는 기각하고 다시 설계한다.
4. ramp 종료 후(post-ramp drain) P95가 threshold(0.648s) 아래로
   회복해야 한다 - 회복 안 하면 그 후보는 기각.
5. 탐색 실행(`experiments/explore_ramp_intensity.py`, run_id
   `explore-*`)은 전부 **calibration으로 분류하고 본 분석·본 실험
   입력 데이터에서 제외**한다(v2 때와 동일 원칙, `slo-definition.md`
   §7). 탐색용 설정은 `chaos/scenario-load-ramp-explore-v3.yaml`
   (신규 - 기존 `scenario-load-ramp-explore.yaml`은 v2/4코어 시절
   이력이라 덮어쓰지 않고 보존)에 반복해서 고쳐 쓴다.
   `chaos/scenario-load-ramp.yaml`(본편)은 최종 후보 확정 전까지
   손대지 않는다.
6. 최종 후보가 3·4의 패턴을 **3회 독립 반복**으로 재현하는 것을
   확인한 뒤에만 `scenario-load-ramp.yaml`을 그 값으로 동결한다 -
   사후 조정 없이 사전 기준을 그대로 통과해야 확정(v2 때와 동일한
   반편향 원칙).

각 실행에서 단계별 P95·성공률·post-drain P95·Node/Pod 상태를 남긴다.
탐색은 아직 시작하지 않았다 - 이 계획을 커밋한 뒤 실행한다.

## 23. 탐색 1·2회차 - stage 경계 측정 버그 발견, harness 수정 (2026-09-18)

### 23.1 탐색 1회차 (`explore-20260918T095809Z`, 0.10~2.00 RPS 7단계)

100% 성공률(720/720건), Node·pod 이상 없음 - 시스템 붕괴는 아니었다.
다만 stage별 P95가 0.10RPS부터 이미 위반, drain(60초)도 회복 안 하는
것으로 나왔다(mean 2.842s, P95 8.288s). 원시 CSV를 초 단위로 직접
확인한 결과 t=675초부터는 즉시 0.2초대로 복귀했다 - 스크립트가 보고한
"drain 위반"과 모순됐다.

### 23.2 탐색 2회차 (`explore-20260918T101717Z`, 0.10~1.00 RPS 5단계, 기존 v2 범위 재검증)

1회차의 극단적 후반 단계(1.50/2.00RPS)를 빼고 기존 범위로 재시도했으나
같은 종류의 왜곡이 남아있었다(drain n=78, mean 0.559, P95 2.195 -
여전히 위반으로 표시).

### 23.3 근본 원인 확인(사용자 진단) - `explore_ramp_intensity.py`의 실제 버그

1·2회차 모두 **측정 도구(harness) 버그**였다: 기존 코드는 probe의 첫
`sent_at`을 t0로 삼고 stage **명목** `duration_sec`만으로 시간 구간을
잘랐다. 그런데 `ramp.py`는 각 stage 종료 시 미완료 요청을 최대 10초
기다린 뒤 다음 단계로 넘어간다(`chaos/loadgen/ramp.py` `run_stage()`) -
이 대기가 stage마다 실제 종료 시각을 명목보다 밀리게 하고, 이게
누적된다(1회차 7단계 실행에서 명목 630초가 실제로는 668.4초 -
차이 38.4초, 대략 단계당 5~6초씩 누적과 일치). 그 결과:
- 뒤 stage로 갈수록 probe 표본이 "아직 안 끝난 이전 stage"와
  "이미 시작된 다음 stage" 사이에서 명목 경계 기준으로 잘못 섞여
  분류됐다(점진적으로 나빠지는 것처럼 보인 패턴의 일부가 이 스미어링
  때문이었다).
- **drain 버킷이 가장 심하게 영향받았다** - 명목 합계(630초) 이후를
  전부 drain으로 봤지만, 실제로는 그 시점에 마지막 stage(2.00RPS)가
  아직 38초 더 진행 중이었다 - 그 트래픽이 drain으로 잘못 들어가
  "회복 안 함"으로 보였다.
- 부수적으로 stage-1 초반 ~6초에는 ramp+probe가 동시에 처음 트래픽을
  내보내는 콜드스타트성 급등(최대 4.7초, `n=90` 중 6~7건)도 있었다 -
  이건 별개 현상(L_baseline calibration 3회에서도 같은 패턴 관찰,
  §21)이지만 명목 경계 문제와 겹쳐 stage-1 판정도 흐렸다.

**1·2회차 처리**: 사용자 지시에 따라 중단하지 않고 그대로 보존했으나
`is_pilot=true`, `exclusion_reason=nominal_stage_boundary_misalignment`
로 본 분석·후보 확정 근거에서 제외한다. 원시 CSV(`experiments/results/
probe-explore-20260918T{095809,101717}Z-raw.csv`, gitignore 대상)는
그대로 유지 - harness 버그를 보여주는 증거로서 보존한다.

### 23.4 수정

- `chaos/loadgen/ramp.py`: 각 stage의 실제 `stage_start_utc`(요청 발사
  시작 직전)/`stage_end_utc`(straggler 최대 10초 대기 이후)를
  `datetime.now(timezone.utc).isoformat()`로 찍어 summary CSV에 추가.
  `--summary-out` 옵션 추가(고정 경로 지정 가능, 미지정 시 기존
  자동 타임스탬프 파일명 유지 - 하위 호환).
- `experiments/explore_ramp_intensity.py`: (1) probe를 ramp보다 먼저
  시작하고 `BASELINE_SEC=60`초(1RPS 기준 SLO 판정 가능한 최소
  표본이자 60초 안정 구간) 대기 후에만 ramp 시작 (2) stage 분류를
  명목 계산 대신 `ramp.py`가 기록한 실제 `stage_start_utc`/
  `stage_end_utc`로 수행하는 `classify_stages()`로 교체(순수 함수로
  분리, 테스트 가능) (3) drain = 마지막 stage의 실제 `stage_end_utc`
  이후 표본만 (4) ramp 시작 전 probe 단독 구간을 별도 `baseline`
  버킷으로 분리 (5) 출력에 표본 수·성공률·P95·mean·max·실제 시작~종료
  시각을 stage별로 표시(기존엔 성공률 자체가 출력에 없었음).
- `experiments/test_explore_ramp_intensity.py`(신규) - 회귀 테스트 7개:
  경계 드리프트 없는 정상 케이스, **지연된 stage 종료의 정확한
  재현**(명목 경계로는 다음 stage에 속할 시각이지만 실제
  `stage_end_utc`가 그보다 늦으면 이전 stage에 남아야 함),
  **drain이 명목 합계가 아니라 마지막 stage의 실제 종료 이후만
  포함**하는지, `ramp_stages` 없음(전부 baseline) 엣지케이스,
  `bucket_stats`의 빈 버킷·성공률/위반 계산, `parse_ramp_summary`의
  timestamp 파싱. 전체 스위트 94 passed, 2 skipped(무관) - 회귀 없음.

### 23.5 재실행 전 확인된 제약 - `loadgen-runner:local` 이미지 재빌드 필요

`ramp.py` 변경은 **이미지에 구워진 사본**에는 반영 안 된다 -
`experiments/loadgen-runner/Dockerfile`이 빌드 시점에
`chaos/loadgen/ramp.py`를 복사해 넣는 구조라(`.gitignore` 주석 참고),
지금 클러스터에 이미 존재하는 `loadgen-runner:local` 이미지는 이번
수정 이전 버전의 `ramp.py`를 담고 있다. `vllm-cpu-env:latest`와 같은
이유로 이 저장소에는 이미지 재빌드를 트리거할 CI/스크립트가 없고
(`sj-worker` 노드에서 직접 build해야 하는 구조, Dockerfile 자체 주석
확인), 이 세션은 `kubectl`만 쓰고 노드 SSH·이미지 빌드 권한이 없다 -
안전하게 재빌드를 트리거할 방법이 없어 **재실행 전에 사용자 확인이
필요**하다.

수정된 harness로 재실행하기 전, 사용자 검토·승인 대기 - 아직 YAML
동결이나 3회 재현성 검증으로 넘어가지 않았다.

### 23.6 `ssh capstone-worker`로 새 이미지 빌드·검증 (사용자 승인, 2026-09-18)

`ssh capstone-worker`(실제 호스트 `sj-worker`, 사용자 `ubuntu`) 접근이
확인돼, `loadgen-runner:local`을 덮어쓰지 않고 새 태그
**`loadgen-runner:phase8-v3-boundaries`**로 빌드했다.

**빌드 전 읽기 전용 확인**:
- 컨테이너 런타임: `containerd://1.7.24`.
- 워커에 `docker`(26.1.3)·`ctr`(containerd 1.7.24 동봉) 있음,
  `nerdctl`/`buildctl`은 없음. `sudo` passwordless.
- 기존 `loadgen-runner:local`이 `docker images`(워커 자체 Docker
  엔진 저장소)와 `sudo ctr -n k8s.io images ls`(kubelet이 실제로
  참조하는 containerd 네임스페이스) **양쪽 모두**에 존재함을 확인 -
  이 노드는 `docker build` → `docker save` → `ctr -n k8s.io images
  import`로 이미지를 K8s에 노출하는 구조라는 뜻이므로 새 이미지도
  동일한 경로로 주입했다.
- 디스크: `/` 155G 중 94G 여유(40% 사용). Node Ready, pressure 전무.

**빌드**: 워커의 격리된 임시 디렉터리(`/tmp/loadgen-build-phase8-v3`,
작업 후 삭제)에 저장소의 다음 4개 파일만 `scp`로 복사 - 그 외
저장소 파일은 빌드 컨텍스트에 없음:
- `chaos/loadgen/ramp.py`(§23.4 수정본)
- `experiments/loadgen-runner/probe.py`
- `experiments/loadgen-runner/requirements.txt`
- `experiments/loadgen-runner/Dockerfile`

`scp` 직후 워커에서 `ramp.py` SHA-256을 로컬과 대조해 전송 무결성을
확인한 뒤 `sudo docker build -t loadgen-runner:phase8-v3-boundaries .`
로 빌드, `docker save | sudo ctr -n k8s.io images import`로 주입.

**빌드 산출물**:
| 항목 | 값 |
|---|---|
| 이미지 태그 | `loadgen-runner:phase8-v3-boundaries` |
| docker image ID(config digest) | `sha256:e58a37b2d1c5903d1ce50474fd00c7d3a39cb300549408c0e0c2305482db897a` |
| containerd k8s.io manifest digest | `sha256:21d6b8ef8bcb1804a28359b2db7a64faae19853493bddb52202b72ac6e9b7aaf` |
| 이미지 내부 `/ramp.py` SHA-256 | `aadab9fc7f2a5a51cfee4e666ba7872c8e8fa378389d47a0e68e501f39153a82` (로컬 `chaos/loadgen/ramp.py`와 정확히 일치, scp 직후·smoke pod 양쪽에서 확인) |

**smoke pod 검증** (`vllm-serving` 네임스페이스, `imagePullPolicy: Never`):
- pod `1/1 Running` 정상 기동.
- `sha256sum /ramp.py` = 위 값, 로컬과 정확히 일치.
- `python /ramp.py --help`에 `--summary-out SUMMARY_OUT` 확인.
- `kubectl delete pod --wait` 후 재조회 `NotFound` - 잔존 리소스 없음.
- Node Ready 유지, `MemoryPressure`/`DiskPressure`/`PIDPressure` 전부
  `False`(빌드·smoke 전후 모두 확인).
- 워커의 임시 빌드 디렉터리(이미지 tar 포함, ~150MB)도 검증 후 삭제.

**코드 반영**: `experiments/explore_ramp_intensity.py`가 더 이상
`load_ramp_adapter.IMAGE`(본 실험/실제 trial harness가 계속 쓰는
`loadgen-runner:local`, 미변경)를 쓰지 않고 자체
`IMAGE = "loadgen-runner:phase8-v3-boundaries"`를 정의 - 탐색 코드만
새 이미지로 바뀌고 실제 60회 본 실험 harness의 이미지 선택에는 영향
없음. 위 태그·digest·SHA-256을 파일 상단 주석에도 기록.

이제 새 이미지로 baseline·실제 stage 경계·실제 drain 경계 기록이
올바른지 확인하는 1회 탐색을 실행한다 - 통과 전에는 RPS 후보 선택이나
3회 재현성 검증으로 넘어가지 않는다.

### 23.7 harness 수정 검증 - 통과 (`explore-20260918T110801Z`, 0.10~1.00 RPS)

새 이미지(`loadgen-runner:phase8-v3-boundaries`)로 2회차와 동일한
0.10~1.00 RPS 설정을 재실행해 수정 자체를 검증했다(RPS 후보 판단
목적 아님).

| bucket | n | 성공률 | mean | P95 | 위반? | 구간(UTC) |
|---|---|---|---|---|---|---|
| baseline(ramp 전) | 61 | 100% | 0.273 | 0.358 | 아니오 | (probe 단독, ramp 시작 전) |
| explore-0.10rps | 90 | 100% | 0.343 | 0.691 | 예 | 11:10:11.728 ~ 11:11:41.729(90.00초 - 드리프트 없음) |
| explore-0.25rps | 91 | 100% | 0.512 | 0.776 | 예 | ~91.3초 |
| explore-0.50rps | 93 | 100% | 0.719 | 1.012 | 예 | ~92.4초 |
| explore-0.75rps | 94 | 100% | 0.948 | 1.280 | 예 | ~94.7초 |
| explore-1.00rps | 99 | 100% | 1.711 | 2.548 | 예 | ~98.5초(명목 90초 대비 straggler 누적 최대) |
| post-ramp(drain) | 65 | 100% | 0.264 | 0.356 | 아니오 | 11:17:58.627(stage-5 실제 stage_end_utc) 이후 |

**검증 항목 확인**:
- **baseline 60초 확보**: n=61(1RPS×61초와 정확히 일치), 100% 성공,
  P95=0.358s로 정상 - ramp 시작 전 진짜 정상 상태였음을 직접 확인.
- **실제 stage 경계 사용**: 각 stage의 실제 소요시간이 90.0 → 91.3 →
  92.4 → 94.7 → 98.5초로 RPS가 높아질수록 점점 늘어남(straggler 누적,
  총 드리프트 21.8초 = 명목 450초와 실제 471.8초의 차) - 명목
  duration이 아니라 `ramp.py`가 기록한 실제 시각을 그대로 쓰고 있음을
  숫자로 확인.
- **drain이 실제 종료 이후만 포함**: drain 구간 시작 시각이 stage-5의
  `stage_end_utc`와 정확히 일치. **drain이 이제 정상(성공률 100%,
  P95=0.356s, 위반 아님)으로 나온다** - 2회차(버그 있던 버전)에서
  drain이 위반(P95=2.195s)으로 잘못 나왔던 것과 대조하면 버그 수정이
  실제로 문제를 고쳤음을 직접 확인한 것이다.
- **부수 확인**: probe를 ramp보다 먼저 시작해 60초 baseline을 확보한
  덕에, 1회차에서 관찰됐던 "ramp+probe 동시 시작 콜드스타트 급등"이
  이번엔 baseline 구간에 흡수되고 stage-1 판정을 오염시키지 않았다
  (stage-1 P95=0.691s로 threshold를 살짝 넘는 수준 - 1회차의
  1.917/max 4.744와 대조적).

harness 수정 검증 통과. 이 결과는 RPS 후보 판단에는 아직 안 쓴다
(같은 설정을 다시 도는 검증용 실행) - 원본은
`experiments/results/probe-explore-20260918T110801Z-raw.csv`·
`ramp-explore-20260918T110801Z-summary.csv`(gitignore 대상)에 보존.

Node·pod 상태 정상(Ready, pressure 없음, 재시작 없음) 확인 완료.

## 24. RPS 후보 탐색 3차 - 0.05~0.25 RPS 정밀 구간 (`explore-20260918T113503Z`, 2026-09-18)

harness 수정·검증(§23) 승인 후, 0.10RPS 근방을 촘촘히 보는 사용자 지정
7단계(0.05/0.075/0.10/0.125/0.15/0.20/0.25 RPS, 각 90초)로 탐색했다.
이 실행도 **exploration으로만 분류**하고 최종 후보 동결에는 아직
안 쓴다.

| bucket | n | 성공률 | mean | P95 | 위반? | 실제 소요시간 |
|---|---|---|---|---|---|---|
| baseline | 61 | 100% | 0.281 | 0.326 | 아니오 | - |
| 0.05rps | 90 | 100% | 0.298 | 0.527 | 아니오 | 90.0초 |
| 0.075rps | 90 | 100% | 0.323 | 0.619 | 아니오 | 90.0초 |
| 0.10rps | 90 | 100% | 0.343 | 0.621 | 아니오 | 90.0초 |
| 0.125rps | 91 | 100% | 0.345 | 0.616 | 아니오 | 90.9초 |
| 0.15rps | 90 | 100% | 0.359 | 0.604 | 아니오 | 90.0초 |
| 0.20rps | 90 | 100% | 0.407 | 0.629 | 아니오 | 90.0초 |
| 0.25rps | 91 | 100% | 0.466 | 0.682 | **예** | 91.0초 |
| drain | 64 | 100% | 0.264 | 0.317 | 아니오 | (stage-7 실제 종료 이후) |

threshold(v3) = 0.648초.

**판단 기준 대조**:
- **낮은 단계 중 최소 2개 안정적 미위반**: 0.05rps만 여유 있게
  미위반(P95=0.527, threshold 대비 여유 0.121초·19%). 0.075~0.20rps
  5단계는 전부 P95 0.60~0.63초 범위에 몰려 있어 미위반이긴 하나
  threshold와의 여유가 0.019~0.044초(3~7%)로 매우 좁다 - "안정적"이라
  부르기엔 경계에 바짝 붙어있다. **부분 충족**(개수는 6개로 넘지만
  "안정적" 여유를 가진 건 사실상 0.05rps 하나뿐).
- **경계 단계는 threshold 부근**: 0.20rps(P95=0.629, 여유 0.019초)와
  0.25rps(P95=0.682, 초과 0.034초) 사이가 실제 경계다 - **충족**.
- **높은 단계 중 최소 2개 위반**: **미충족**. 7단계 중 0.25rps
  **1개만** 위반으로 나왔다(그것도 threshold를 5%만 초과). 이 범위는
  "낮은~경계" 구간의 해상도는 좋지만 "반복적으로 분명히 위반"하는
  높은 단계가 부족하다 - 범위를 더 높은 RPS(예: 0.30~0.40대)로
  확장해야 이 기준을 만족시킬 수 있을 것으로 보인다(추정, 실측
  전까지 확정 아님).
- **전 구간 성공률 100%**: **충족**(baseline·7단계·drain 전부 100%).
- **drain에서 threshold 아래로 회복**: **충족**(P95=0.317초, 여유
  0.331초).
- **Node·Pod 이상 및 재시작 없음**: **충족**(재확인 - Node Ready,
  pressure 전무, 양쪽 pod 재시작 0).

**관찰**: 이번 3-core+SLO v3 환경은 0.075~0.20RPS라는 꽤 넓은 구간에서
P95가 threshold 바로 아래(0.60~0.63초)에 몰려있다가 0.25RPS에서만
넘어가는, 좁고 평평한 전이 구간을 보인다 - 2회차까지의 v2/4코어
곡선(0.10~1.00RPS에 걸쳐 완만하게 상승)보다 훨씬 가파르고 좁다. 이
관찰은 사실 보고이며, 다음 탐색 범위를 어떻게 조정할지는 결정하지
않았다.

원본은 `experiments/results/probe-explore-20260918T113503Z-raw.csv`·
`ramp-explore-20260918T113503Z-summary.csv`(gitignore 대상)에 보존.

이 결과만으로 최종 후보를 동결하지 않는다. 사용자 검토·승인 대기 -
아직 다음 탐색 범위 조정이나 3회 재현성 검증으로 넘어가지 않았다.
`load_ramp_adapter.IMAGE`도 아직 미변경(사용자 지시대로 최종 강도
동결 시 함께 처리 예정).

## 25. 최종 후보 재현성 검증 - 사전 등록 (측정 전, 2026-09-18)

§24 탐색 결과에 근거해 사용자가 최종 후보 5단계를 확정:
**0.025 → 0.05 → 0.20 → 0.30 → 0.40 RPS**, 각 90초
(`chaos/scenario-load-ramp-explore-v3.yaml`에 반영). 실행 전, 판정
규칙을 코드(`experiments/verify_ramp_candidate.py`의
`judge_candidate()`, 오프라인 테스트 9개로 검증 완료)와 문서 양쪽에
고정한다:

1. `0.025`·`0.05` RPS: 유효한 반복 전부에서 SLO **미위반**(한 번이라도
   위반하면 실패).
2. `0.20` RPS: **경계 단계로만 기록** - 위반 여부가 통과/실패를
   좌우하지 않는다.
3. `0.30`·`0.40` RPS: 각각 유효한 반복의 **최소 2/3에서 위반**.
4. 모든 유효한 반복에서 요청 성공률 **100%**.
5. 모든 유효한 반복에서 실제 ramp 종료(마지막 stage의 `stage_end_utc`)
   이후 drain P95가 threshold 아래로 회복.
6. 모든 유효한 반복에서 Node Ready·pressure 없음, vLLM pod restart
   증가 없음(반복 시작 전/후 비교).
7. 구간 분류는 `ramp.py`가 기록한 실제 `stage_start_utc`/
   `stage_end_utc`만 쓴다(§23 수정, 명목 duration 아님).
8. 각 반복 시작 전 60초 baseline이 이미 threshold를 넘으면 ramp를
   시작하지 않고 그 시도를 `invalid`(reason=`baseline_violating`)로
   분리 - 유효 반복 3회 집계에 넣지 않는다(`run_candidate()`의
   baseline gate로 구현).
9. 반복 사이에는 Node/Pod 상태를 확인(quiescence)하고 60초
   cooldown(`verify_ramp_candidate.COOLDOWN_SEC`) 후 다음 반복.

**실행 원칙**: 3회를 전부 완료한 뒤 위 규칙을 기계적으로 적용해
판정한다 - 첫 반복 결과가 기대와 달라도 중간에 후보 값을 바꾸지
않는다. 안전 문제(Node 이상, pod restart 증가)나 하네스 오류가 생기면
그 시점에 중단하고 원인을 먼저 확인한다.

**통과 시**: (1) `scenario-load-ramp.yaml`을 이 5단계로 동결 (2)
`load_ramp_adapter.IMAGE`를 `loadgen-runner:phase8-v3-boundaries`로
변경 (3) 이미지 ID/digest·`/ramp.py` SHA-256을 문서에 기록(§23.6
참고, 이미 확보됨) (4) 전체 오프라인 테스트 실행 (5) 커밋·푸시.
**미통과 시**: 결과를 그대로 보존하고 새 값을 정하기 전에 보고한다.

측정은 아직 시작하지 않았다 - 이 사전 등록을 커밋한 뒤 실행한다.

## 26. 최종 후보 3회 재현성 검증 결과 - `target_rps` 버그 발견·수정 후 PASS, 동결 (2026-09-18)

§25 사전 등록대로 `verify_ramp_candidate.py`로 3회 독립 반복 실행
(`verify-20260918T120401Z`/`121658Z`/`122953Z`, 사이 60초 cooldown,
전부 유효 - baseline gate에 걸린 무효 시도 없음).

| stage | rep1 P95 | rep1 위반? | rep2 P95 | rep2 위반? | rep3 P95 | rep3 위반? |
|---|---|---|---|---|---|---|
| baseline | 0.349 | 아니오 | 0.355 | 아니오 | 0.347 | 아니오 |
| 0.025rps | 0.536 | 아니오 | 0.601 | 아니오 | 0.586 | 아니오 |
| 0.05rps | 0.581 | 아니오 | 0.634 | 아니오 | 0.583 | 아니오 |
| 0.20rps | 0.686 | 예 | 0.701 | 예 | 0.670 | 예 |
| 0.30rps | 0.721 | 예 | 0.714 | 예 | 0.699 | 예 |
| 0.40rps | 0.777 | 예 | 0.879 | 예 | 0.879 | 예 |
| drain | 0.283 | 아니오 | 0.306 | 아니오 | 0.291 | 아니오 |

3회 전부 요청 성공률 100%, Node Ready·pressure 없음, pod restart
증가 없음(반복 전/후 비교).

### 26.1 `judge_candidate()` 자체의 버그 발견 - 최초 판정은 오탐 FAIL

**첫 실행 직후 자동 판정은 8개 기준 중 4개(0.025rps/0.05rps 미위반,
0.30rps/0.40rps 2/3 이상 위반)가 전부 FAIL로 나왔다** - 그런데 위 표를
보면 실제로는 0.025·0.05rps는 3/3 전부 미위반(기준 충족), 0.30·0.40rps는
3/3 전부 위반(기준을 오히려 초과 충족)이다. 표와 자동판정이 정면으로
모순돼 판정 로직 자체를 의심하고 원시 CSV(`ramp-verify-*-summary.csv`)
를 직접 열어 확인했다.

**원인**: `explore_ramp_intensity.run_candidate()`가 stage별 결과 dict를
만들 때 `{"stage": s["stage"], "stage_start_utc":..., "stage_end_utc":...,
**bucket_stats(bucket)}`처럼 3개 필드만 골라 담아, `ramp.py`가 실제로
기록한 `target_rps`가 통째로 빠졌다. `judge_candidate()`의 RPS별 stage
탐색(`float(s.get("target_rps", -1))`)이 전부 못 찾는 값(`-1`)으로
떨어져 매 stage가 `None`(판정 불가) 취급되고, "미위반"·"위반"
어느 쪽도 아닌 `None`이 쌓여 두 기준 모두 기계적으로 FAIL 처리된
것이었다 - 실제 후보 성능과는 무관한 **순수 harness 버그**.

**수정**: `{**s, **bucket_stats(bucket)}`로 원본 stage dict 전체(`target_rps`
포함)를 보존하도록 변경. 회귀 테스트
(`test_parse_ramp_summary_preserves_target_rps`) 추가. **이미 저장된
3회분 원시 CSV를 재파싱해 재판정했다(클러스터 재실행 없음)** - 아래
26.2가 수정 후 진짜 결과다.

### 26.2 수정 후 판정 - PASS

| 기준 | 결과 |
|---|---|
| 유효 반복 3회 이상 | PASS (3/3) |
| 0.025rps 전부 미위반 | PASS (3/3) |
| 0.05rps 전부 미위반 | PASS (3/3) |
| 0.30rps 최소 2/3 위반 | PASS (3/3) |
| 0.40rps 최소 2/3 위반 | PASS (3/3) |
| 전 구간 성공률 100% | PASS |
| drain 회복 | PASS |
| Node·Pod 상태 정상 | PASS |

**종합: PASS.** 0.20rps는 3/3 전부 위반으로 나왔으나 사전 등록대로
"경계 단계, 방향 강제 없음"이라 판정에 포함하지 않는다(참고: v2 때의
0.75rps처럼 매번 위반하지 않아야 하는 건 아니다 - 그냥 threshold
바로 위에서 안정적으로 위반하는 것도 유효한 경계).

### 26.3 동결 조치

- `chaos/scenario-load-ramp.yaml`: 5단계를 0.025/0.05/0.20/0.30/0.40
  RPS(각 90초)로 확정. 이전 4코어/SLO v2 확정본(0.10~1.00RPS, 커밋
  `83bb61a`)은 파일 상단 주석으로 이력 링크만 남기고
  `experiment-contract.md`의 원본 표는 그대로 보존(삭제 안 함).
- `experiments/load_ramp_adapter.py`의 `IMAGE`를
  `loadgen-runner:phase8-v3-boundaries`로 변경 - image ID(config
  digest) `sha256:e58a37b2d1c5903d1ce50474fd00c7d3a39cb300549408c0e0c2305482db897a`,
  containerd manifest digest
  `sha256:21d6b8ef8bcb1804a28359b2db7a64faae19853493bddb52202b72ac6e9b7aaf`,
  이미지 내부 `/ramp.py` SHA-256
  `aadab9fc7f2a5a51cfee4e666ba7872c8e8fa378389d47a0e68e501f39153a82`
  (§23.6과 동일 - 재검증 아니라 인용).
- `experiments/explore_ramp_intensity.py`/`load_ramp_adapter.py`의
  관련 주석도 "본 실험은 loadgen-runner:local을 계속 씀"이라던 이제
  틀린 서술을 갱신.
- 전체 오프라인 스위트 **104 passed, 2 skipped**(무관, 기존
  live_cluster 스킵) - 회귀 없음.

원본 3회분 CSV는 `experiments/results/{probe,ramp}-verify-2026091
8T{120401,121658,122953}Z-*.csv`(gitignore 대상)에 보존.

**load_ramp 재보정 완료.** 아직 60회 본 실험이나 다른 시나리오로는
넘어가지 않았다.

## 27. 공식 `run_once()` 경로 `load_ramp × native` 파일럿 - 측정 버그 발견으로 중단 (2026-09-18)

목적: 동결된 SLO v3·5단계 ramp·새 runner 이미지가 실제 Phase 8
하네스(`run_once.py`)와 끝까지 연결되는지 확인(성능 비교 목적 아님).
`run_id=pilot-load_ramp-native-01-20260918T130111Z`, `is_pilot=true`.

**preflight 10개 전부 통과**: 저장소 `ed83c40` clean·동기화, 양쪽 Node
Ready/pressure 없음, Rollout Healthy·active 1개·preview 없음, active
pod 1/1·restart 0·completion 200, pod가 `lab-cpu3-warm-v1`(CPU limit
"3", exec warmup startupProbe) 그대로, recovery-policy `/healthz`
200, `quiescent=true`/`experiment-run.current=null`, Chaos CR 없음,
`loadgen-runner:phase8-v3-boundaries`가 워커 containerd `k8s.io`
네임스페이스에 존재(digest 동일), `scenario-load-ramp.yaml`이 동결된
5단계와 일치.

**실행 결과(표면)**: `outcome=recovered`, `state=completed`,
`t_injection=13:03:27.036`, `t_slo=13:03:55.462`(주입 28초 후),
`t_recovery=13:04:27.450`. `injection_valid=true`, `probe_valid=true`,
`detected=false`, `action=none`, `promotion_verified=null`,
`commit_sha=null` - 여기까지는 통과 기준과 일치하는 것처럼 보였다.

**이상 감지**: 0.025rps(1단계)는 §26에서 3/3 반복 전부 미위반으로
검증된 단계인데, 주입 28초 만에(아직 1단계 초반) 위반이 잡힌 건
비정상적으로 빠르다고 판단해 원시 probe CSV를 직접 확인했다 - t=0~37초
구간 요청은 전부 성공(200)이고 latency도 0.18~0.3초로 완전히 정상이었다
(유일한 예외: 주입 **이전** t=-1.78초 시점 1건이 0.989초). "정상
표본뿐인데 위반 판정"이라는 모순을 발견해 자동 수정하지 않고 원인부터
추적했다.

**근본 원인(직접 코드 확인)**: `slo_judge.evaluate()`는 윈도우 표본이
20개 미만이면 `statistics.quantiles()`(정식 백분위수) 대신
`max(latencies)`를 P95 대용으로 쓴다. probe는 1RPS라 처음 20초 가량은
윈도우에 20개 미만 표본만 쌓이고, 그동안 "P95"는 사실상 "지금까지 본
표본 중 최댓값"이 된다. 주입 직전(-1.78초)에 우연히 찍힌 콜드스타트성
단일 샘플(0.989초, threshold 0.648초 초과)이 이후 한동안 이 "최댓값"
자리를 차지하면서, 실제로는 이후 모든 요청이 정상이었음에도 30초
넘게 "위반 지속" 조건을 인위적으로 만족시켜 `t_slo`가 찍혔다
(`slo_judge.find_t_slo()`를 원시 CSV에 직접 재실행해 지점별
p95/violating 값을 전부 출력, t=-1.78~37초 구간 내내
`latency_violating=True`로 나오는 것을 직접 확인). **이는 이번 세션
탐색 도구(explore_ramp_intensity.py 등)의 버그가 아니라, 이미 동결돼
있던 `slo_judge.py`(L_baseline 계산 자체는 정상) 안에 원래부터 있던
결함이며, 이번 파일럿이 처음으로 실제 `run_once()` 경로에서 이 결함을
발동시킨 것이다.** `load_ramp_adapter.py`의 `_warmed_up()`(60초 대기
게이트)은 `check_slo_violation()`의 **폴링 시작 시점**만 늦출 뿐,
`get_actual_slo_time()`이 `find_t_slo()`로 역산하는 **기록 시각
자체**는 이 게이트의 보호를 받지 않는다 - 그래서 게이트가 있어도
28초짜리 `t_slo`가 그대로 기록됐다.

**부수 발견(경미)**: `TrialResult.slo_version`이 `"v2"`로
하드코딩된 기본값(`run_once.py`)을 그대로 쓰고 있어, 실제로는 v3
threshold(0.648초, `latency_slo_sec` 필드는 정확함)를 썼는데도 버전
라벨만 `"v2"`로 잘못 기록됐다 - `run_load_ramp_trial.py`가
`run_once()` 호출 시 `slo_version`을 명시적으로 넘기지 않아서다.

**정상 확인된 부분(참고용, 위 결함과 무관)**: 정리(probe/ramp pod
완전 삭제, Chaos 리소스 0건, experiment context null), 종료 후
Rollout Healthy·Node Ready/pressure 없음, `timing_schema_version=v2`
정확, `t_injection` 근거 필드(`t_injection_request`/`_last_seen`/
`_observed`, `injection_observation_error_sec=1.334`)에 모순 없음,
native arm이라 promotion·감사 Git commit 전혀 없음(사전 HEAD
`ed83c40` 이후 origin에 새 커밋 없음, 직접 확인), 결과 JSON이
`collect_metrics.py`의 `build_comparison()`에서 오류 없이 읽히고
`included_in_main_analysis=False`/`exclusion_reason='pilot'`로 정확히
분류됨.

**처리**: 지시대로 이 실행의 원본(결과 JSON
`experiments/results/pilot/trial-pilot-load_ramp-native-01-20260918T
130111Z.json`, probe raw CSV, 둘 다 gitignore 대상)을 그대로 보존하고
**어떤 코드도 수정하지 않은 채** 여기서 멈춘다. 이 발견은
`load_ramp`에만 국한되지 않는다 - `slo_judge.evaluate()`는
pod_kill/network_degrade/memory_pressure 등 SLO 판정을 쓰는 모든
시나리오가 공유하는 모듈이라, 같은 결함이 어디서든 재현될 수 있다.

다음 조치(수정 방향·범위·검증 방법)는 사용자 결정을 기다린다 - 아직
`fixed_threshold`·`proposed` arm, preview 생성, promotion, 다른
시나리오, 60회 본 실험으로는 넘어가지 않았다.

## 28. `slo_judge.py` small-sample P95 결함 수정 + 파일럿 재분류 (2026-09-18)

### 28.1 파일럿 재분류

`pilot-load_ramp-native-01-20260918T130111Z`을 오염된 실행으로 확정.
원본 CSV·모든 timestamp 필드는 그대로 두고 판정 필드만 수정:
`outcome=invalid_run`, `state=invalid`,
`invalid_reason=small_sample_p95_fallback_false_violation`.
`is_pilot=true`는 원래도 true였고, `collect_metrics.py`의
`_classify_exclusion()` 우선순위(PREFLIGHT-EXCLUDED > is_pilot >
invalid_run)상 `exclusion_reason`은 여전히 `"pilot"`으로 나오며
`included_in_main_analysis=False`도 그대로 - 둘 다 요구사항 충족.

### 28.2 `slo_judge.py` 수정

`evaluate()`: 윈도우 표본이 `MIN_SAMPLES_FOR_RELIABLE_P95`(20) 미만이면
더 이상 `max(latencies)`를 P95 대용으로 쓰지 않는다 - 그 point는
`sample_count`(신규 필드)만 기록하고 `latency_evaluable=False`,
`p95=None`, `latency_violating=False`로 판정을 보류한다. 20개
이상이면 기존 `statistics.quantiles(...)[94]` 그대로,
`latency_evaluable=True`. Availability는 표본 수와 무관하게 기존과
완전히 동일(§4, 즉시 판정 유지).

`find_t_slo()`: 코드 변경 없음 - `latency_violating`이 이제 표본
부족 구간에서 항상 `False`가 되므로, 기존 스트릭 로직
(`if violating: 연장 else: 리셋`)이 자동으로 "표본 부족 구간에서
스트릭 시작·연장 안 함"을 만족한다.

`find_t_recovery()`: 회복 스트릭 조건에 `latency_evaluable`을 추가
(`p["latency_evaluable"] and not violating and not avail_violating`
이어야 스트릭 연장) - "모른다"를 "정상"으로 오인해 회복을 조기
확정하지 않도록.

`SLO_VERSION = "v3"`를 `slo_judge.py`에 단일 출처로 추가.
`run_load_ramp_trial.py`/`run_network_degrade_trial.py`/
`run_pod_kill_trial.py` 3개 launcher 전부 `run_once(...,
slo_version=slo_judge.SLO_VERSION)`로 명시 전달하도록 수정.
`run_once.py`의 하드코딩된 기본값 2곳(`"v2"`)을 `"unspecified"`로
변경 - 이후 launcher가 값을 안 넘기면 오래된 버전이 조용히 기록되는
대신 명시적으로 드러난다.

### 28.3 오프라인 테스트

`test_slo_judge.py`에 7개 추가: 20개 미만 표본에서 고지연 1건도 위반
아님, 20개 이상에서 지속 고지연 정상 검출, 이번 파일럿의 정확한
패턴(초반 급등+이후 정상)에서 원래 버그 시각(13:03:55) 재현 안 됨,
20개 미만이라도 실패 요청은 즉시 availability 위반 검출, latency
비평가 구간만으로 recovery 미확정, 표본 충분한 위반→회복 흐름은
기존과 동일, **보존된 이번 파일럿 원본 CSV를 직접 재분석하는 통합
테스트**. 기존 테스트 2개(`test_latency_violation_t_slo_is_observed_
at_domain`/`test_recovery_uses_observed_at_and_filters_by_observed_
at`)는 표본 수를 35→65로 늘렸다 - 20개 미만 구간이 더 이상 위반으로
안 잡히니 30초 연속 조건을 채우려면 그만큼 표본이 더 필요해서다(값
자체가 아니라 새 판정 규칙에 맞춘 자연스러운 조정).
`test_collect_metrics.py`에 `slo_version="v3"`가
`build_comparison()`을 거쳐도 보존되는 테스트 1개 추가. **전체 스위트
112 passed, 2 skipped**(무관).

### 28.4 중요 - 통합 테스트가 드러낸 별개의 추가 현상(이번 수정 범위 밖)

보존된 파일럿 원본을 고친 로직으로 재분석하면, **원래 버그 시각
(13:03:55)은 더 이상 안 나오지만 t_slo 자체는 여전히 어딘가에서
찍힌다**(다른 시각). 원인을 추적한 결과 표본 부족 문제가 아니라
**서로 다른 두 개의 짧은 고지연 구간이 60초 슬라이딩 윈도우를 통해
겹쳐 보이는 별개 현상**이었다: 주입 직후 콜드스타트성 클러스터
(t=-1.78~1.22초, 4건, 0.5~0.99초)와 t=39~41초의 독립된 클러스터(3건,
0.61~0.71초, **표본 38~54개의 정상 통계량 - 표본 부족 아님**)가 있고,
둘 다 실제로는 30초 미만의 짧은 blip인데 겹치는 60초 윈도우 안에서
함께 잡혀 windowed P95가 더 오래 threshold를 넘는 것처럼 보인다. 이번
수정(표본 부족 시 판정 보류)은 이 현상을 고치지 않는다 - 별개의,
더 근본적인 방법론 질문(겹치는 윈도우가 짧은 독립 blip들을 합쳐
보이게 하는 문제)이라 이번 지시 범위 밖으로 판단해 손대지 않았다.
`test_preserved_pilot_raw_csv_small_sample_points_never_violate`가
이 사실을 코드로 명시하고, 원래 버그 시각이 재현되지 않는 것만
검증한다.

전체 오프라인 테스트 통과 확인, 실클러스터 재실행 없음. 이 부수
현상에 대한 추가 조치는 사용자 결정 대기 - 아직 다른 arm·시나리오·
60회 본 실험으로 넘어가지 않았다.

## 29. 주입 전 baseline 미확보 문제 수정 (2026-09-18)

### 29.1 문제 재정의

§28.4에서 확인한 "서로 다른 두 클러스터가 겹치는 60초 윈도우를 통해
합쳐 보이는 현상" 자체는 **결함이 아니다** - rolling P95·60초
윈도우·30초 지속 조건은 현재 SLO 정의상 의도된 동작이며 이번
수정에서 손대지 않는다. 대신 지시에 따라 진짜 근본 원인인 **실험
프로토콜의 결함**을 수정한다: 지금까지 하네스는 probe가 살아있다는
것만 확인하면(`_wait_for(prober.is_alive, ...)`) 바로
`injector.inject()`를 호출했다 - 주입 전 상태가 실제로 안정적인지
(콜드스타트 잔재 없이 60초 이상 SLO를 만족하는 상태인지)는 한 번도
확인하지 않았다. 파일럿 CSV에 주입 전 표본이 단 2개뿐이었던 것
자체가 이 결함의 직접 증거다.

### 29.2 수정 원칙

- `WINDOW_SEC`(60초)·`LATENCY_PERSIST_SEC`(30초)·rolling P95 계산
  로직은 절대 변경하지 않는다 - §28.4의 겹침 현상은 그대로 유지.
- §28의 small-sample 수정(`MIN_SAMPLES_FOR_RELIABLE_P95`=20 미만이면
  판정 보류)도 그대로 유지 - 이번 수정과 독립적이다.
- 새 보호장치는 두 층으로 나눈다: (1) 주입 **전**에 실제로 baseline이
  안정 상태에 도달할 때까지 기다리는 예방 계층, (2) 그래도 주입 전
  표본이 판정에 섞여 들어가는 걸 막는 `not_before` 게이트(사후
  방어). 표본 자체는 자르지 않는다 - 주입 전 정상 표본은 주입 후
  point의 rolling P95/가용성 계산에 계속 입력으로 쓰인다(윈도우
  계산은 `evaluate()`가 그대로 담당, `not_before`는 오직
  `find_t_slo()`의 **후보 자격**만 제한).

### 29.3 구현

**`experiments/slo_judge.py`**: `find_t_slo(points, not_before=None)` -
`not_before` 지정 시 `sent_at < not_before`인 point는 latency 스트릭의
시작/연장에도, availability 위반의 t_slo 후보에도 기여하지 못하고
건너뛴다(단순 `continue` - 이후 point의 스트릭은 항상 처음부터 다시
셈, 주입 전 위반이 주입 후 스트릭 지속시간에 "보태지는" 것도
막힘). `find_baseline_ready(points)` 신규 - points 맨 앞부터
`latency_evaluable and not violating and not availability_violating`
상태가 30초 연속 유지되는 첫 시점을 찾아 `{ready_at, sample_count,
p95, availability}`를 반환(`find_t_recovery()`와 조건식은 같고, t_slo
이후가 아니라 처음부터 찾는다는 점만 다름). 준비 안 됐으면 `None`.

**`experiments/run_once.py`**: `TrialState.BASELINE` 상태 추가
(READY와 INJECTING 사이). `Prober`에 선택 필드
`get_baseline_status: Optional[Callable[[], dict]]` 추가 - 미구현이면
(pod_kill/network_degrade 등, 아직 이 훅이 없는 임의의 향후 어댑터)
BASELINE 단계 전체를 건너뛰고 기존과 완전히 동일하게 바로 주입한다
(하위호환, `baseline_valid=None`="검증 안 함"으로 기록 - `is_slo_
evaluable` 미구현 시의 기존 관례와 동일). 구현돼 있으면 새 헬퍼
`_wait_for_baseline()`이 `baseline_timeout_sec`(기본값
`BASELINE_TIMEOUT_SEC=120`, `run_once()` 인자로 테스트 등에서 조정
가능) 동안 `poll_interval_sec` 간격으로 `get_baseline_status()`를
반복 호출 - `ready=True`가 나오면 그 시점 값을 기록하고 주입 진행,
120초 안에 못 나오면 **`injector.inject()`를 아예 호출하지 않고**
`TrialInvalid`로 `invalid_run` 처리(`baseline_valid is False`로만
게이트 - `None`과 명확히 구분해야 훅 미구현 어댑터를 오판정하지
않음). `TrialResult`에 `t_baseline_ready`/`baseline_sample_count`/
`baseline_p95`/`baseline_availability`/`baseline_valid` 5개 필드
추가.

**`experiments/load_ramp_adapter.py`**: `check_slo_violation()`/
`get_actual_slo_time()`/`check_recovered()` 3곳 모두
`slo_judge.find_t_slo(..., not_before=injection_ref["t"])`로 통일
(`injection_ref`는 기존 `notify_injected()` 훅이 이미 채워주던 값을
그대로 재사용 - 프로토콜 시그니처를 바꾸지 않고 어댑터 내부 클로저만
수정). `get_baseline_status()` 신규 구현 - probe raw CSV를 갱신하고
`slo_judge.find_baseline_ready()`를 호출, 아직 준비 안 됐으면 최신
point의 표본수/p95/가용성 스냅샷을 반환(사후 분석용 가시성).
**`run_pod_kill_trial.py`/`run_network_degrade_trial.py` 둘 다 별도
Prober 없이 `make_load_ramp_prober()`를 "범용 Prober 팩토리"로 그대로
재사용한다는 걸 직접 확인**(코드 주석에도 명시) - 즉 이번 수정은
`load_ramp_adapter.py` 한 곳만 고쳐도 pod_kill/network_degrade
포함 3개 시나리오 전부에 동일하게 적용된다. `pod_kill_adapter.py`/
`network_degrade_adapter.py`는 애초에 `Injector`만 정의하고
`Prober`는 정의하지 않는다(직접 확인 - `Prober(` 생성 호출이 저장소
전체에서 `load_ramp_adapter.py`와 `test_run_once.py`에만 존재).

**`experiments/collect_metrics.py`**: `build_comparison()` 출력에
`baseline_valid`/`t_baseline_ready`/`baseline_sample_count`/
`baseline_p95`/`baseline_availability` 5개 필드 추가 - 기존 필드가
없는 과거 결과는 `row.get(...)`이 안전하게 `None`을 반환.

### 29.4 오프라인 테스트

- `test_slo_judge.py`: 14→22개(+8) - `not_before`가 주입 전 위반을
  후보에서 제외(단독/가용성 축 각 1개), 주입 전+후 합산으로 30초를
  채우던 경우가 게이트 적용 시 스트릭이 주입 후부터 다시 시작해
  더 이상 안 채워짐, `not_before`가 point의 rolling window 계산
  자체(주입 전 표본 포함 여부)는 건드리지 않음, 순수 주입 후 지속
  위반은 기존과 동일하게 탐지, **보존된 파일럿 원본 CSV를
  `not_before=t_injection`으로 재분석해도 실제 클러스터 탐지력이
  줄지 않음**(§29.6), `find_baseline_ready()` 준비됨/미준비 각 1개.
- `test_run_once.py`: 오프라인 18→21개(+3) - `get_baseline_status`가
  점진적으로 ready가 되는 경우 그때까지 대기 후 정상 주입+결과 필드
  기록, 끝까지 ready가 안 되면 **`injector.inject()`가 한 번도
  호출되지 않고** `invalid_run`, 훅 미구현 시 단계 자체를 건너뛰고
  기존과 동일하게 즉시 주입(회귀 없음 명시적 확인).
- `test_collect_metrics.py`: +1개 - baseline 5개 필드가
  `comparison.csv` 행까지 보존되고, 이 필드가 아예 없는 과거 결과도
  전부 `None`으로 안전하게 읽힘.
- **부수 발견·수정(이번 작업과 무관한 사전 결함)**: `test_slo_judge.
  py`와 `test_collect_metrics.py` 둘 다 `__main__` 블록이 파일 뒤쪽에
  나중에 추가된 테스트 함수를(§28에서 추가된 `test_preserved_pilot_
  raw_csv_...`/`test_slo_version_v3_preserved_through_comparison`)
  정의보다 먼저 호출하고 있어, `python test_X.py`로 직접 실행하면
  `NameError`로 죽는 상태였다(`pytest` 경로는 이름 기반 자동 수집이라
  영향 없었음 - 그래서 지금까지 안 드러남). 정의 위치를 `__main__`
  앞으로 옮겨 두 파일 다 직접 실행도 정상 동작하도록 고쳤다(판정
  로직 변경 없음, 순수 위치 정리).
- 전체 스위트 `pytest experiments/ -m "not live_cluster"`:
  **124 passed, 2 deselected**(live_cluster 훅, `RUN_LIVE_TESTS=1`
  필요).

### 29.5 파일럿 재분류 갱신

`pilot-load_ramp-native-01-20260918T130111Z`의 `outcome`/`state`(둘 다
이미 `invalid_run`/`invalid`)는 지시대로 다시 건드리지 않았다.
`notes` 필드에만 `pre_injection_baseline_not_established`가 2차
원인으로 추가 확인됐다는 문장과 이 절(§29) 참고 경로를 남겼다 - JSON
diff는 `notes` 한 필드뿐, 다른 모든 필드(원본 timestamp 포함)는
그대로.

### 29.6 보존된 CSV를 새 로직으로 재분석 - 이번엔 값이 같았다(하지만 이유가 있다)

지시대로 보존된 원본 CSV(`probe-pilot-load_ramp-native-01-
20260918T130111Z-native-1-raw.csv`)를 `not_before=t_injection`
(`13:03:27.036375`)으로 재분석했다. 결과: `t_slo=2026-09-18
13:04:15.461843+00:00` - **§28.4에서 이미 보고한 값과 정확히
동일하다**(`not_before` 있음/없음 두 값을 직접 코드로 비교해 확인,
차이 없음).

원인도 확인됐다: 이 CSV에서 주입 시각(`t_injection`) 이전 표본은
정확히 2개뿐이고, §28의 small-sample 수정 이후 이 2개는 애초에
`latency_evaluable=False`라(20개 미만) 한 번도
`latency_violating=True`였던 적이 없다(직접 조회해 확인). 즉
`not_before`가 걸러낼 "주입 전 위반 후보" 자체가 이 CSV에는 이미
없었다 - §28(small-sample 보류)과 §29(not_before) 두 수정이 이번
한 건에 한해 우연히 같은 지점을 이미 막고 있었던 것이다. 두 수정은
서로 다른 상황을 겨냥한다(§28은 "표본이 적어 신뢰 못 할 때", §29는
"표본은 충분해도 주입 전 시점이라 인정 못 할 때") - 이번 CSV가
전자에만 해당했을 뿐, 후자가 무의미하다는 뜻은 아니다(baseline 관찰
단계가 이번에 처음 생겼으므로, 이 run은 애초에 그 단계를 거친 적이
없다).

**그렇다고 이 run이 유효해지는 것은 아니다.** 주입 전 표본이 2개뿐인
것 자체가 "60초 baseline 관찰"이 전혀 없었다는 직접 증거이고, t=39~
41초의 두 번째 클러스터가 "정상 baseline을 거친 뒤 주입했다면"
나왔을 값과 같은지는 이 run만으로는 확인할 수 없다(비교 대상이 될
정상 baseline 자체가 없었으므로 - 확인 불가, 추정하지 않는다). 이
run은 §28에서 이미 확정한 대로 `invalid_run`/파일럿 제외로 유지하며,
이번 재분석은 "§29 수정이 기존 위반 탐지력을 깎아먹지 않았다"는
회귀 확인 용도로만 쓴다 -
`test_preserved_pilot_raw_csv_with_not_before_still_detects_real_
cluster`가 이 사실(값이 같다는 것 포함)을 코드로 고정한다.

### 29.7 실클러스터 재실행 여부

지시대로 여기서 멈춘다. 이번 작업은 전부 오프라인 코드·테스트·
문서였고, baseline 관찰 단계가 실제 하네스에서 의도대로 동작하는지
(예: 진짜 콜드스타트 직후에 붙여서 60초+ 관찰 후에만 주입되는지,
120초 안에 준비 안 되면 실제로 주입 없이 invalid_run으로 끝나는지)
실클러스터로 확인하는 건 다음 지시를 기다린다. 다른 arm·시나리오·
60회 본 실험으로도 넘어가지 않았다.

## 30. baseline gate 실클러스터 검증 - `load_ramp × native` 파일럿 재실행 (2026-09-18)

### 30.1 사전 확인

실행 전 7개 항목을 읽기 전용으로 직접 확인:

- 저장소: `git status` clean, `HEAD=d5a6ea9`, `git fetch` 후
  `git rev-list --left-right --count HEAD...origin/master` = `0 0`
  (완전 동기화).
- Node: `sj-control`/`sj-worker` 둘 다 `Ready=True`,
  Memory/Disk/PIDPressure 전부 `False`.
- Rollout: `phase=Healthy`, `replicas=updated=ready=available=1`,
  `currentPodHash==stableRS==85c55758c6`,
  `blueGreen.previewSelector==activeSelector`(구분되는 preview 없음).
- active completion: recovery-policy pod에서
  `vllm-active.vllm-serving.svc.cluster.local`로 직접 POST ->
  `http_code=200`, `vllm-serving` pod `restarts=0`.
- `GET /admin/quiescent` -> `{"quiescent":true,"active_count":0}`,
  `GET /admin/experiment-run` -> `{"current":null}`.
- `kubectl get podchaos,networkchaos,stresschaos,iochaos -n
  vllm-serving` -> 리소스 없음.
- 로컬 설정 직접 확인: `slo_judge.py`의 `L_BASELINE=0.324`/
  `LATENCY_THRESHOLD=0.648`/`SLO_VERSION="v3"`,
  `chaos/scenario-load-ramp.yaml`의 5단계(0.025/0.05/0.20/0.30/
  0.40rps × 90초, §26에서 동결된 값 그대로),
  `load_ramp_adapter.py`의
  `IMAGE="loadgen-runner:phase8-v3-boundaries"`.

7개 전부 통과 확인 후 실행.

### 30.2 실행

`python run_load_ramp_trial.py --pilot`(전부 기본값 - `arm=native`,
`rep=1`, `sequence_index=1`, `order_seed=1`, config/probe-config는
동결된 파일 경로, `timeout_sec=900`) - §27의 첫 파일럿과 동일한
설정으로 재실행.

`run_id=pilot-load_ramp-native-01-20260918T141420Z`,
`outcome=recovered`, `state=completed`. `t_injection=14:17:32.895986`,
`t_slo=14:21:34.258091`, `t_recovery=14:26:00.024205`(전부 UTC).

### 30.3 15개 확인 항목

1. **state가 baseline을 거침**: 상태 전이 로그 자체는 안 남지만(같은
   파일을 매 전이마다 덮어씀), `t_baseline_ready`/`baseline_sample_
   count`/`baseline_p95`/`baseline_availability`가 전부 채워져 있다는
   것 자체가 충분한 증거다 - `run_once.py` 코드상 이 필드들은
   `result.state = TrialState.BASELINE.value`를 설정한 바로 다음
   블록(`_wait_for_baseline()` 호출)에서만 채워질 수 있고 다른 코드
   경로는 없다(직접 확인). ✓
2. `baseline_valid: true` ✓
3. `t_baseline_ready: 2026-09-18T14:17:27.941448+00:00` ✓
4. `baseline_sample_count: 57`(≥20) ✓
5. `baseline_p95: 0.3345`(≤0.648) ✓
6. `baseline_availability: 1.0`(≥0.99) ✓
7. `t_baseline_ready(14:17:27.941) < t_injection_request(14:17:
   30.889)` - 약 2.95초 차 ✓
8. **baseline 이후에만 injector 호출**: `outcome=recovered`(invalid_
   run 아님)이므로 `if baseline_valid is False: raise` 분기를
   통과했다는 뜻이고, 실측 타임스탬프 순서(`t_baseline_ready` <
   `t_injection_request`)도 일치 - 코드 경로·실측 둘 다로 확인 ✓
9. `slo_version: "v3"` ✓
10. `t_slo(14:21:34.258091) ≥ t_injection(14:17:32.895986)` - 약
    241.4초 뒤(항상 이후여야 한다는 §29의 `not_before` 보장이 실제로
    지켜짐) ✓
11. `t_recovery: 2026-09-18T14:26:00.024205+00:00` 기록,
    `outcome=recovered` ✓
12. `detected=false`, `action=none`, `promotion_verified=null`,
    `commit_sha=null` - native arm이라 정책 엔진 개입 없음, 예상대로
    ✓
13. cleanup 후 `kubectl get pods -n vllm-serving`에 probe/ramp pod
    없음(`recovery-policy`/`vllm-serving` 2개만 남음), Chaos CR
    없음, experiment context=null ✓
14. Node `Ready=True` 유지, Rollout `Healthy`·단일 revision
    (`currentPodHash==stableRS`) 유지, `vllm-serving` pod
    `restarts=0`(실행 전후 불변) ✓
15. `collect_metrics.py`의 `build_comparison()`에 이 실행의 실제
    결과 JSON을 직접 넣어 확인 - `exclusion_reason='pilot'`,
    baseline 5개 필드 전부 정상 전달, validation issue 0건 ✓

**15개 전부 통과.**

### 30.4 위반 원인 분석 (raw CSV 직접 재확인)

지시에 따라 "발생 시점이 예상보다 빠르다"는 이유만으로 버그로
분류하지 않고, raw CSV와 판정 과정을 직접 재현해 근거를 확인했다.

- 원본 probe CSV(`probe-pilot-load_ramp-native-01-20260918T141420Z-
  native-1-raw.csv`, 601건)를 `slo_judge.find_t_slo(evaluate(rows),
  not_before=t_injection)`으로 독립 재계산 -> `t_slo=2026-09-18T14:
  21:34.258091+00:00`로 결과 JSON과 정확히 일치(재현성 확인).
- `latency_violating`이 `False->True`로 바뀌는 시점은 **주입 후
  209.8초**(14:21:02.694, window 표본 61개, p95=0.649) 단 한 번뿐이고,
  그 뒤로 **주입 후 506.8초**(14:25:59.698, n=60, p95=0.336)까지
  **끊김 없이 계속 위반 상태**였다(전체 구간을 직접 스캔해 중간에
  False로 꺾이는 지점이 없음을 확인). `t_slo`(+241.4초)는 이
  스트릭이 `LATENCY_PERSIST_SEC`(30초) 조건을 처음 만족한 지점의
  observed_at이다(209.8+30=239.8초 부근과 정확히 부합).
- 스트릭 구간(주입+195.8~244.8초)의 원본 요청을 직접 나열해 보면
  0.15~0.28초(정상)와 0.45~0.67초(threshold 근접·초과)가 섞여 있다 -
  단일 이상치가 아니라 **표본 다수가 실제로 threshold 부근까지
  올라간 상태**이고, 창 표본 수가 60~61개(기준 20개의 3배)라 §28의
  small-sample 문제와도 무관하다.
- 명목(주입 시각+90초 단위) stage 경계로 보면 stage3(0.20rps)는
  주입+180.9~270.9초 구간이다. 위반 스트릭 시작(+209.8초)과
  `t_slo`(+241.4초) 둘 다 이 구간 안에 있다 - **stage3(0.20rps)
  진행 중에 발생한 것이지, stage4/5보다 빠르지도 stage 경계보다
  이르지도 않다.** 다만 이 stage 경계는 명목값이다 -
  `load_ramp_adapter.py`의 `inject()`는 `--summary-out`을 넘기지
  않아(exploration 전용 도구 `explore_ramp_intensity.py`만 이 옵션을
  씀) `ramp.py`가 기록하는 실제 stage 시각을 이번 실행에서는 캡처하지
  못했다 - §23에서 이미 확인된 대로 straggler 대기로 인한 최대
  ~10초 드리프트 가능성이 있으나 이번 실행 자체의 정확한 드리프트
  값은 확인 불가(추정하지 않음).
- `t_injection_end`(ramp 종료, +451.7초) 이후에도 위반이 +506.8초까지
  이어지다 해소됐다 - ramp가 멈춘 뒤로도 부하 잔재가 ~55초간 남았다가
  정상화된 것으로 보이며, 이는 관측된 사실과 일관된다.

종합하면 표본 수(60~61개, 기준의 3배), 명확한 단일 전환점(중간에
꺾이지 않는 연속 위반), threshold 근접·초과가 섞인 실제 latency
분포, stage3 진행 중 발생 - 이 네 가지 모두 §28(small-sample
오탐)·§29(주입 전 오염) 두 결함 중 어느 쪽 징후와도 일치하지 않는다.
판정 로직 자체가 잘못 반응했다고 볼 근거는 찾지 못했다(다만 이 결과를
"연구 결과로서 타당한 위반"으로 최종 해석하는 것은 사용자 판단 영역
이므로, 여기서는 재현 가능한 사실 관계까지만 보고한다).

### 30.5 결론

15개 확인 항목 전부 통과 - baseline gate가 실클러스터에서 의도대로
동작함을 확인했다(baseline 57표본·30초 안정 후에만 주입, `t_slo`가
항상 주입 이후로만 기록됨, 훅 정상 배선). 이번 위반은 표본 수·전환
패턴·raw latency 분포·stage 시점 네 축 모두에서 하네스 결함 징후가
없는, 근거가 명확한 위반으로 판단된다. 실행 전후 클러스터 상태
불변(Node/Rollout/pod restart count 동일, 잔여 pod·Chaos CR·
experiment context 없음) 확인. 실클러스터 추가 재실행 없음 - 다른
arm·시나리오·60회 본 실험으로는 넘어가지 않는다.

## 31. `load_ramp native` E2E 확정 + 3-arm 파일럿 전 stage 관측성 보완 (2026-09-18)

### 31.1 `load_ramp native` 경로 확정

`pilot-load_ramp-native-01-20260918T141420Z`을 유효한 `load_ramp
native 경로 E2E PASS`로 확정한다(지시). §30에서 분석한 위반은 정상
baseline 이후 충분한 표본으로 지속적으로 관찰됐고, §4(`load_ramp`
확정 설정)의 기존 0.20 RPS 근방 재현성 결과와도 방향이 일치해 추가
SLO 수정이나 재실행이 필요 없다고 판단됐다. 이 pilot의 원본
(결과 JSON·raw CSV)과 §30의 문서 내용은 이번 절에서 수정하지
않는다 - "stage3" 표현은 명목 경계 추정이라는 서술 그대로 유지하고,
아래에서 새로 만든 stage 분류 메커니즘으로 이 pilot의 값을 소급
생성하지 않았다(이 실행은 `ramp.py --summary-out`을 캡처하지 않은
채 진행돼 실제 stage 경계 데이터 자체가 없다 - 확인 불가).

### 31.2 stage 관측성 보완 - 동기

3-arm 파일럿(아직 미시작)부터는 `t_slo`/`t_detection`/`t_api_request`
등이 실제로 어느 ramp stage에서 발생했는지 사후 분석 없이도 결과
파일 자체에서 바로 알 수 있어야 한다는 지시에 따라, §30에서
명목값(주입 시각+90초 단위)으로만 참고할 수 있었던 stage 경계를
실제 값으로 대체하는 인프라를 추가했다. §23에서 이미 확인된 대로
`ramp.py`는 각 stage 종료 시 최대 10초의 straggler 대기를 하므로
명목 경계와 실제 경계가 어긋날 수 있다 - 이번 보완은 그 어긋남을
그대로 반영한다.

### 31.3 구현

**`chaos/loadgen/ramp.py`**: 변경 없음 - §23에서 이미 `--summary-out`
(stage별 `stage`/`target_rps`/`actual_rps`/`sent`/`success`/
`success_rate`/`p95`/`p99`/`stage_start_utc`/`stage_end_utc` CSV)을
지원하고 있었다. `explore_ramp_intensity.py`(탐색 전용 도구)만 이
옵션을 쓰고 있었고, 실제 trial 하네스(`load_ramp_adapter.py`)는 한
번도 이 값을 안 넘기고 있었다는 걸 이번에 확인했다.

**`experiments/load_ramp_adapter.py`**:
- `make_load_ramp_injector()`의 `inject()`가 이제
  `--summary-out /ramp-summary-{run_id}.csv`(pod 내부 경로 - pod
  자체가 trial마다 새로 뜨므로 필수는 아니지만 run_id를 넣어 명확히
  구분)를 넘긴다.
- `is_done()`이 `ramp.py`의 정상 종료(exit=0)를 **처음** 확인한
  직후 `_fetch_stage_summary()`를 호출 - `stage_cache["fetched"]`
  플래그로 딱 한 번만 시도한다(OBSERVING 루프가 `is_done()`을
  반복 호출하므로 매번 kubectl exec하면 낭비). pod에서 요약을
  가져와 로컬 `experiments/results/ramp-summary-{run_id}-{arm}-
  {rep}.csv`에 저장하고(probe raw CSV와 동일한 명명 관례),
  `csv.DictReader`로 파싱해 메모리에도 캐시한다. kubectl 오류·빈
  응답·파싱 실패는 전부 조용히 삼킨다(`stages=None`으로 남음) -
  **예외를 던지지 않는다**(§5.1 정책 - stage 정보 부재가
  `invalid_run`을 유발하면 안 됨).
- 새 순수 함수 `_classify_timestamp_against_stages(timestamp_iso,
  stages)`(kubectl 의존 없음, `test_load_ramp_adapter.py`에서 직접
  단위 테스트) - `stages`가 없거나 한 행이라도 파싱 실패하면
  `"unknown"`. 있으면 각 stage의 실제 `[stage_start_utc,
  stage_end_utc]` 구간에 timestamp가 속하는지 확인해 stage 이름을
  반환하고, 어느 구간에도 안 속하면 첫 stage 이전은 `"baseline"`,
  stage 사이 틈은 `"inter_stage_tail"`, 마지막 stage 이후는
  `"drain"`으로 구분한다.
- `classify_stage(timestamp_iso)` 클로저가 `Injector`의 새 선택
  훅으로 노출된다(`_fetch_stage_summary()`를 방어적으로 한 번 더
  호출 - idempotent라 무해).

**`experiments/run_once.py`**:
- `Injector`에 `classify_stage: Optional[Callable[[str], str]] =
  None` 추가(미구현 어댑터는 하위호환 - pod_kill/network_degrade는
  현재 stage 개념이 없으므로 이 훅 자체가 없고, `run_once()`가
  아예 호출하지 않는다).
- `TrialResult`에 `slo_stage`/`detection_stage`/`action_stage`
  추가. `finally` 블록에서(`cleanup()`으로 pod가 삭제되기 전) 훅이
  있으면 `t_slo`→`slo_stage`, `t_detection`→`detection_stage`,
  `t_api_request`→`action_stage` 순으로 호출한다 - **대응하는
  timestamp가 null이면 아예 호출하지 않고 필드도 null로 남긴다**
  (사건이 없었던 것과 분류 못 한 것을 구분). `action_stage`는
  `t_api_request`(정책이 K8s API를 실제로 호출해 조치를 실행한
  시각) 기준으로 골랐다 - `action` 필드 자체는 문자열이라 대응
  timestamp가 없어, "조치가 실행된 시각"에 가장 가까운 필드를
  선택한 설계 판단이다.
- 이 블록 전체를 다시 한 번 `try/except`로 감쌌다 -
  `classify_stage()` 자체가 계약을 어기고 예외를 던져도(구현
  버그), 이미 확정된 핵심 판정(`outcome`/`t_slo` 등)이나 trial의
  성공적인 종료가 오염되지 않는다. 실패하면 `notes`에 사유만
  남긴다.

**`experiments/collect_metrics.py`**: `build_comparison()` 출력에
`slo_stage`/`detection_stage`/`action_stage` 3개 필드 추가. 겸사겸사
§29에서 추가했지만 이 문서 §5 스키마 표에는 반영이 누락돼 있던
baseline 5개 필드도 `experiment-contract.md`에 함께 보완했다(발견
즉시 수정 - 이전에도 같은 유형의 누락이 있었던 전례, §변경이력
2026-09-18 "6개가 전부 빠져 있었다" 항목 참고).

### 31.4 오프라인 테스트

- `test_load_ramp_adapter.py`: +7개 - 실제 stage 구간 안 분류, 첫
  stage 이전 `baseline`, 마지막 stage 이후 `drain`, **명목 90초
  경계가 아니라 실제(지연된) `stage_end_utc`로 분류되는지 확인하는
  핵심 회귀 테스트**(stage-1이 straggler로 8초 늦게 끝난 걸
  가정 - 명목 경계였다면 오분류됐을 시점이 실제 경계로는 올바르게
  분류됨을 직접 확인), stage 사이 틈은 `inter_stage_tail`, summary
  없음/손상된 행은 각각 `unknown`.
- `test_run_once.py`: +4개 - `classify_stage` 구현 시 `t_slo` 기준
  `slo_stage` 정상 채움, `t_detection`/`t_api_request`가 null이면
  대응 stage 필드도 null(unknown 아님), 훅 미구현 시 3개 필드 전부
  None(회귀 없음), `classify_stage`가 예외를 던져도 trial 핵심
  판정은 영향 없고 `notes`에만 남음.
- `test_collect_metrics.py`: +1개 - stage 3개 필드가
  `comparison.csv` 행까지 보존되고, 필드 자체가 없는 과거 결과도
  전부 `None`으로 안전하게 읽힘.
- 전체 스위트 `pytest experiments/ -m "not live_cluster"`:
  **136 passed, 2 deselected**(live_cluster).

### 31.5 결론

3-arm 파일럿 전 요구된 stage 관측성 보완을 오프라인으로 완료했다 -
실클러스터 작업은 하지 않았다(요구된 그대로). `load_ramp native`
경로는 §31.1에서 확정한 대로 E2E PASS로 남고, 이번 작업은 다음
실행(3-arm 파일럿, 아직 미시작)부터 stage 정보가 정확히 기록되도록
하는 순수 인프라 추가다. 다른 arm·시나리오·60회 본 실험, 그리고
3-arm 파일럿 자체로도 아직 넘어가지 않는다.

## 32. arm 오케스트레이션 완성 - 3-arm 파일럿 전 detector·preview 배선 (2026-09-18)

### 32.1 문제 확인

`load_ramp native` E2E PASS 확정(§31.1)과 함께, 3-arm 파일럿에 앞서
지적된 문제: `run_load_ramp_trial.py --arm fixed_threshold|proposed`는
`--arm` 이름만 결과에 태깅할 뿐, 실제 `fixed_threshold.py`/
`score_server.py` 프로세스를 실행·종료하지도, non-native arm에 필요한
preview(standby)를 준비하지도 않았다(직접 코드 확인 - `run_once()`
호출 인자 어디에도 detector·preview 관련 항목이 없었음). 그대로
non-native arm을 실행했다면 detector가 실제로는 동작하지 않은 채
"fixed_threshold"/"proposed"로 잘못 라벨링된 결과가 생겼을 것이다.

### 32.2 계약서 §1 재확인 - 세 arm의 실제 실행 조건

새로 추정하지 않고 `docs/design/experiment-contract.md` §1을 그대로
표로 정리했다:

| 항목 | `native` | `fixed_threshold` | `proposed` |
|---|---|---|---|
| 예측 모델(탐지 알고리즘) | 없음 | 고정 임계치(CPU>90%, `anomaly-detection/fixed_threshold.py`) | Isolation Forest(`anomaly-detection/score_server.py`) |
| Alertmanager 반응형 fallback | 없음 | 있음(fixed_threshold와 공통) | 있음(fixed_threshold와 공통) |
| standby(preview) | 없음 | 있음 | 있음 |
| promotion 경로 | 없음 | 있음 | 있음 |
| recovery-policy 서비스 | 미기동(개입 자체 없음) | 기동(`/signal`+`/webhooks/alertmanager`+Alertmanager 라우팅) | 기동(동일 - fixed_threshold와 완전히 같은 인프라) |
| detector 프로세스 | 없음 | `fixed_threshold.py --run-id <run_id>` | `score_server.py --run-id <run_id>` |

fixed_threshold와 proposed는 예측 모델만 다르고 나머지 인프라
(recovery-policy·Alertmanager fallback·standby/promotion)는 완전히
동일하다는 게 계약서의 핵심 통제변수이므로(§1 "순수 탐지방식 비교"),
아래 구현에서 두 arm은 실행할 스크립트만 다를 뿐 동일한 메커니즘
(`arm_controller.py`)을 공유한다.

### 32.3 구현

**`anomaly-detection/score_server.py`**: `RECOVERY_POLICY_URL`을
하드코딩된 in-cluster DNS 대신 `RECOVERY_POLICY_SIGNAL_URL`
환경변수로 덮어쓸 수 있게 했다(미지정 시 기존 in-cluster DNS 기본값
그대로 - 하위호환). `fixed_threshold.py`는 이 상수를 `score_server.py`
에서 직접 import해 쓰므로 같이 고쳐진다. 로컬 서브프로세스로 돌릴
때(아래) 기존엔 소스를 직접 고쳐야 했던 절차(파일 자체 주석에 그렇게
쓰여 있었음)를 없앴다.

**신규 `experiments/arm_controller.py`**:
- `_DETECTOR_SCRIPTS` - native 제외 2개 arm만 담는 dispatch 테이블
  (`{"fixed_threshold": {"script": "fixed_threshold.py", "name":
  "fixed_threshold"}, "proposed": {"script": "score_server.py",
  "name": "isolation_forest"}}`) - "name"은 각 스크립트가
  `post_to_recovery_policy()`로 실제 보내는 `detector=` 태그와
  정확히 일치시켜, 사후에 recovery-policy 수신 신호와 대조 검증할 수
  있게 했다. dict 기반 단일 조회라 arm과 스크립트가 구조적으로 어긋날
  수 없다(if/elif 분기 복붙 실수 같은 경로 자체가 없음).
- `_build_detector_command(arm, run_id)` - 순수 함수(커맨드 리스트만
  조립, 프로세스 안 띄움) - 오프라인 테스트에서 run_id 전파를
  실행 없이 검증 가능하게 분리.
- `_subprocess_detector(cmd, name, cwd)` - 서브프로세스 생명주기
  (start/is_alive/stop, `run_once.py`의 새 `Detector` 프로토콜)만
  담당하는 작은 헬퍼 - detector 스크립트 자체 내용과 분리해서,
  trivial한 커맨드(`python -c "..."`)로도 생명주기 정확성을
  오프라인 검증할 수 있게 했다(실제 detector 스크립트는 Prometheus·
  모델 파일 의존이라 오프라인 테스트 대상이 아님). `stop()`은
  idempotent(시작 전 호출도 안전), `terminate()` 후
  `STOP_TIMEOUT_SEC`(10초) 안에 안 죽으면 `kill()`.
- `make_detector_for_arm(arm, run_id)` - native면 `None`,
  아니면 위 둘을 엮어 실제 `Detector`를 만든다. 서브프로세스 시작 시
  `RECOVERY_POLICY_SIGNAL_URL=http://localhost:8080/signal`을
  환경변수로 넘긴다(`kubectl port-forward -n vllm-serving
  svc/recovery-policy 8080:8080` 전제 - `run_once.py`의
  `RECOVERY_POLICY_URL`과 동일한 기존 로컬 실행 전제를 그대로 따름,
  새 조건 아님).
- `wrap_injector_with_preview_prep(injector, arm, ...,
  prepare_preview_fn=blue_green_prep.prepare_preview)` - native는
  원본 injector를 그대로 반환. non-native면 `injector.prepare()`
  앞에 `prepare_preview()`(기존 `experiments/blue_green_prep.py` -
  `run_calibration.py`가 이미 쓰고 있던 코드, 새로 안 만듦)를 배선 -
  실패하면(시간 내 Ready 안 됨) `TrialInvalid`를 던져 `run_once()`의
  PREPARING 단계가 그대로 `invalid_run` 처리하고
  `injector.inject()`는 호출되지 않는다(기존 "probe 미준비면 주입
  안 함"과 동일한 안전장치 재사용).

**`experiments/run_once.py`**: 새 `Detector` 프로토콜(start/is_alive/
stop/name, `Injector`/`Prober`와 같은 작은 콜백 묶음 dataclass 패턴).
`run_once(..., detector: Optional[Detector] = None)` - baseline 확보
(BASELINE 단계 통과, §29)와 context 등록(READY 단계, 기존 순서상
이미 baseline보다 앞)이 모두 끝난 뒤, 주입 직전에 `detector.start()`를
정확히 한 번 호출한다(지시). OBSERVING 루프에서 `prober.is_alive()`와
나란히 `detector.is_alive()`를 확인 - 죽으면 `TrialInvalid("detector가
관찰 도중 비정상 종료")`. finally에서 prober/injector보다 먼저
`detector.stop()`을 호출하고(정리 과정 자체가 오염되는 걸 최소화하려는
순서), 곧바로 `is_alive()`로 실제 종료를 재확인 - 여전히 살아있으면
`critical_failures`에 추가돼 `HarnessCorrupted`(prober의 기존
leak-check와 동일한 심각도). `TrialResult.detector_process`에
`detector.name`을 기록(detector가 실제로 시작에 성공했는지와 무관하게
채워지는 감사 필드 - "이 trial이 어떤 detector로 실행되려 했는가").

**`experiments/run_load_ramp_trial.py`**: `injector`를
`arm_controller.wrap_injector_with_preview_prep()`으로 무조건 감싸고,
`arm_controller.make_detector_for_arm()`으로 만든 `detector`를
`run_once()`에 무조건 전달하도록 고쳤다 - `--arm` 값만 바꿔 orchestration
없이 non-native arm을 실행할 경로 자체가 이제 없다(fail-closed, 지시).
`--rollout`/`--namespace` CLI 인자를 추가(기존 `run_calibration.py`와
동일한 패턴, 기본값 `vllm-serving`/`vllm-serving` - 실제 클러스터
설정과 일치, 이번 세션 직접 확인).

### 32.4 오프라인 테스트

- 신규 `test_arm_controller.py`(10개): native는 detector 없음,
  fixed_threshold/proposed는 각각 정확한 스크립트·`detector.name`
  하나만 반환(서로 안 섞임), dispatch 테이블이 native 제외 2개 arm만
  담고 서로 다른 스크립트/이름을 가짐, run_id가 `--run-id` 인자로
  정확히 전파, 서브프로세스 생명주기(시작 전 죽어있음→시작 후
  살아있음→정지 후 죽어있음→재정지 idempotent, 즉시 크래시하는
  프로세스는 곧 is_alive()=False로 관측), preview 준비가 native는
  건너뜀/성공 시 원본 prepare() 호출/**실패 시 원본 prepare()(=이후
  injection)가 호출되지 않음**(핵심 회귀).
- `test_run_once.py`(+7개): detector는 baseline 확보 이후에만
  시작(실제 호출 순서 로그 `['baseline_ready', 'detector_start',
  'inject', 'detector_stop']`로 확인), injection은 detector 시작
  이후에만 실행, detector가 관찰 도중 크래시하면 invalid_run,
  injector.inject() 예외·timeout 각각에서도 detector.stop()은 반드시
  호출됨, `detector_process` 필드가 `detector.name`을 정확히 기록,
  detector 미지정(기본값) 시 필드가 null로 남는 하위호환.
- 전체 스위트 `pytest experiments/ -m "not live_cluster"`:
  **153 passed, 2 deselected**(live_cluster).

### 32.5 결론

3-arm 파일럿 전 요구된 arm 오케스트레이션을 오프라인으로 완성했다 -
실클러스터 작업은 하지 않았다(지시대로). `run_load_ramp_trial.py`는
이제 세 arm 모두 이 스크립트 하나로 안전하게 돌 수 있고, non-native
arm은 orchestration을 우회할 방법이 없다. 3-arm 파일럿 자체는 아직
실행하지 않았다 - 다음 지시를 기다린다. 다른 시나리오·60회 본
실험으로도 넘어가지 않았다.

## 33. `t_detection`/`t_api_request` authoritative source 확정 및 회수 (2026-09-19)

### 33.1 문제 확인 - 조사 결과

§32 arm 오케스트레이션은 승인됐지만, 3-arm 파일럿 전 별도로 확인된
gap: `run_once.py` 어디에도 `result.t_detection`/`result.t_api_request`
를 대입하는 코드가 없어 두 필드가 항상 기본값(null)으로 남는다(직접
grep으로 확인 - `detected`/`action`/`promotion_verified`/
`detection_source`도 마찬가지였으나 이번 지시 범위는 두 timestamp로
한정돼 그 넷은 손대지 않았다).

구현 전에 recovery-policy(`recovery-policy/main.py` 등)의 실제 경로를
코드로 직접 추적했다(지시):
- **예측 신호 수신**: `POST /signal` → `normalize_anomaly_signal()` →
  `process_signal()`. `score_server.py`/`fixed_threshold.py`가 자체
  생성 시각(`timestamp`)을 payload에 실어 보내는데, 이건 "언제 그
  클라이언트가 신호를 만들었나"이지 "언제 recovery-policy가 처리했나"가
  아니다.
- **반응형 Alertmanager fallback**: `POST /webhooks/alertmanager` →
  `normalize_alertmanager_webhook()` → 각 alert의 `startsAt`(Alertmanager
  가 조건이 firing으로 바뀐 시각) 기준으로 `NormalizedSignal.received_at`
  설정 - 이 값도 recovery-policy의 처리 시각이 아니라 알림 조건 자체가
  시작된 시각이라 서로 다른 의미.
- **정책 결정**: `process_signal()`이 idempotency 체크(`safety.
  check_and_reserve()`) 통과 후 `policy.decide()` 호출.
- **promotion 호출**: `decision.action == ACTION_PROMOTE_PREVIEW`이고
  cooldown이 아니면 `rollouts_client.promote()` 호출.
- **감사기록**: `decision_log.build()`가 `datetime.now()`로
  `decided_at`을 동기적으로 찍긴 하지만, **모든** outcome(중복 차단
  포함)에 대해 호출되므로 그대로 재사용하면 "재시도로 덮어쓰지 않음"
  조건을 못 지킨다. 게다가 실제 파일 기록·Git 커밋·푸시(`git_client.
  enqueue()`)는 백그라운드 큐로 비동기 처리되고(main.py 모듈 docstring:
  "git 지연·실패가 이 함수의 반환을 막지 않는다"), 지연·실패가 있어도
  요청 처리 자체를 막지 않게 의도적으로 설계돼 있다 - timestamp
  원천으로 쓰면 안 됨(지시).

결론: **detector 프로세스 stdout도, Git 감사기록도 authoritative
source가 아니다.** recovery-policy가 신호를 실제로 수락/조치를 실제로
시작하는 그 순간 자신이 직접 찍는 서버측 벽시계 시각만이 신뢰할 수
있는 원천이다.

### 33.2 의미 확정과 구현

지시된 의미를 그대로 구현했다(추정·재해석 없음):

- **`t_detection`**: "현재 run에 속하는 유효한 신호를 recovery-policy가
  처음 수락해 정책 판단 대상으로 확정한 시각" - `process_signal()`이
  idempotency 통과 **직후**, `policy.decide()` 호출 **전**에
  `datetime.now(timezone.utc)`로 기록한다. `decide()`의 결과(rule-out/
  observe_only/promote 무엇이든)와 무관 - "신호를 판단 대상으로
  삼았다"는 사실만 기록.
- **`t_api_request`**: "실제 promotion API/CLI 호출을 시작하기 직전의
  시각" - cooldown 통과 후, `rollouts_client.promote()` 호출 바로 앞에서
  기록. 조치가 없으면(observe_only/rule-out/unknown/cooldown-skip)
  이 코드에 도달하지 않아 null로 남는다.
- **첫 값만 유지**: 둘 다 `_current_experiment`의 필드가 이미 `None`이
  아니면 갱신하지 않는다(재시도·후속 신호가 덮어쓰지 못함).
- **격리**: `_signal_belongs_to_current_experiment()`가 (ambient 보정
  이후의) `signal.raw["experiment_run_id"]`가 `_current_experiment.
  run_id`와 정확히 일치할 때만 기록을 허용한다 - 다른 run_id를 직접
  실은 신호(예: 정리 안 된 이전 trial의 detector가 계속 보내는 신호)와
  stale alert(기존 ambient 보정 로직이 이미 걸러냄 - 등록 시각 이전
  `startsAt`은 애초에 태깅 자체가 안 됨) 둘 다 배제된다.
- 새 `GET /admin/experiment-run/timing` 엔드포인트가 `{run_id,
  t_detection, t_api_request}`를 반환(활성 context 없으면 전부 null).

**`experiments/run_once.py`**: `_get_experiment_timing(arm)`이(기존
`_get_active_experiment_context()`와 동일한 `arm=="native": None` 관례)
`finally`에서(§32 stage 분류보다 먼저 - stage 분류가 이 값을 씀,
`_clear_experiment_context()`보다 먼저 - clear되면 서버측 값도 사라짐)
호출된다. 응답의 `run_id`가 자기 trial 것과 다르거나 조회 자체가
예외를 던지면, 조용히 null로 남기지 않고 **명시적으로 `invalid_run`**
처리한다(지시 - "무탐지"와 "확인 불가"를 혼동하면 안 됨). 단, 이미 다른
사유(예: `injector.inject()` 예외)로 `invalid_run`이 확정된 trial의
기존 `invalid_reason`은 덮어쓰지 않는다 - 더 구체적인 원인을 보존.
`context_registered`가 `False`면(등록 자체가 실패해 애초에 조회할
context가 없음이 이미 확실함) 조회 자체를 시도하지 않는다.

**`experiments/arm_controller.py`**: `make_detector_for_arm()`이 반환
하는 `Detector.start()`가 실제 서브프로세스 실행 전에
`RECOVERY_POLICY_SIGNAL_URL`(환경변수 우선, 없으면 로컬 기본값)의
도달성을 확인한다 - `/signal`은 부작용이 있어(진짜 신호로 처리됨) 대신
같은 host:port의 `/healthz`(부작용 없음)로 확인. 불가능하면
`TrialInvalid`를 던져 detector도 chaos 주입도(§32에서 detector.start()
가 주입 직전 호출) 시작되지 않는다(fail-closed, 지시).

### 33.3 오프라인 테스트

- `recovery-policy/test_main.py`(+8개): 예측 신호(`/signal`)·반응형
  alert(`/webhooks/alertmanager`) 둘 다 `t_detection` 기록, 동일 신호
  재전송(중복)은 덮어쓰지 않음, 다른 run_id를 실은 신호·stale alert
  둘 다 배제, 조치 없으면(`observe_only`) `t_api_request` null 유지,
  실제 promotion 시 `t_api_request` 기록 + `t_detection<=t_api_request`
  확인, context clear 후 재등록한 다음 trial에 이전 timing이 안 남음,
  활성 실험 없으면 엔드포인트가 전부 null.
- `experiments/test_run_once.py`(+6개): timing 정상 회수, 엔드포인트
  조회 실패 시 명시적 `invalid_run`(무탐지와 구분), 응답 run_id 불일치
  시 `invalid_run`, 이미 확정된 `invalid_reason`은 timing 실패로
  덮어써지지 않음, native는 recovery-policy에 전혀 접근 안 함(계약서
  §1 재확인), 회수된 두 timestamp로 `detection_stage`/`action_stage`가
  §31 메커니즘 그대로 정상 계산됨(두 작업의 통합 확인).
- `experiments/test_arm_controller.py`(+3개): reachability 실패 시
  detector가 시작 자체를 안 함(fail-closed), 성공 시 서브프로세스
  시작까지 정상 진행, `RECOVERY_POLICY_SIGNAL_URL` 환경변수가 로컬
  기본값보다 우선.
- 전체 스위트: `experiments/` 162 passed, 2 deselected(live_cluster).
  `recovery-policy/` 44 passed(각 파일 단독 실행 기준).

**부수 발견(수정 안 함, 이번 범위 밖)**: `recovery-policy/test_main.py`
최상단의 `patch("main.git_client.start_worker", ...).start()` /
`patch("main.git_client.enqueue", ...).start()` 두 줄이 `with`문이
아니라 `.start()`만 호출하고 대응하는 `.stop()`이 없다 - 같은 pytest
세션에서 `test_main.py`가 먼저 수집되면 이 패치가 프로세스 전역에
남아, 이후 수집되는 `test_git_client.py`의 실제 `git_client` 함수 호출
3개가 실패한다. `git stash`로 이번 세션 변경분을 전부 제거한 원본
코드에서도 동일하게 재현되는 것을 직접 확인해 - 이번 작업이 만든
결함이 아니라 기존부터 있던 테스트 격리 문제임을 확정했다(각 테스트
파일을 단독으로 돌리면 전부 통과 - `recovery-policy/` 전체를 한 번에
`pytest`로 돌릴 때만 드러남). 수정 범위 밖이라 손대지 않았다.

### 33.4 결론

`t_detection`/`t_api_request`의 authoritative source를 recovery-policy
자신의 동기 처리 경로로 확정하고, 조회 실패·불일치를 침묵 없이
`invalid_run`으로 명시하는 회수 경로까지 구현했다 - Git 감사기록(비동기)
이나 detector stdout에 기대지 않는다(지시 그대로). §31의 stage 분류가
이 값들로 실제 계산되는 것도 확인했다. RECOVERY_POLICY_SIGNAL_URL
도달성 사전 확인으로 fail-closed도 추가했다. 실클러스터 작업 없음 -
3-arm 파일럿은 아직 시작하지 않았다.

## 34. 테스트 격리 수정 + recovery-policy 실클러스터 배포·smoke 검증 (2026-09-19)

§33 승인 후, 3-arm 파일럿 전 마지막 선행 작업 - (1) 테스트 격리 결함
수정, (2) 변경된 recovery-policy를 실클러스터에 배포, (3) 안전한
조건에서 timing 엔드포인트 신호 전파를 실측 확인.

### 34.1 `test_main.py` patch 누수 수정

**원인 재확인**: `TestClient(app)`를 직접 실험해 확인한 결과(설치된
FastAPI 0.137.2 / Starlette 1.3.1 기준), bare `TestClient(app)` 생성·
요청은 `@app.on_event("startup")`을 아예 발화시키지 않는다 - 즉
`git_client.start_worker` patch는 애초에 막을 대상이 없었다(무해하지만
불필요). 반면 `git_client.enqueue`는 `process_signal()`이 매 신호마다
실제로 호출하므로 이 patch만은 진짜로 필요했다.

**재현(변경 전, 이번 절 작업 시작 시점의 커밋 `b221da7`에서 직접 확인)**:
```
$ pytest test_main.py test_git_client.py -q
3 failed, 23 passed
FAILED test_git_client.py::test_enqueue_commits_and_pushes
FAILED test_git_client.py::test_non_fast_forward_recovers_via_rebase
FAILED test_git_client.py::test_restart_requeues_pending
```
직접 파고든 결과 원인이 두 겹이었다: ① `test_main.py`가
`patch(...).start()`만 하고 `.stop()`이 없어 mock이 프로세스 전역에
남음(1차 원인, §33.3에서 이미 기록). ② 더 근본적으로, `test_git_client.py`
는 자기 모듈 docstring에 "env var를 git_client import 전에 세팅해야
모듈 상수에 반영된다"고 명시돼 있다 - `git_client.py`의 `DATA_DIR`/
`REPO_DIR`/`GIT_REMOTE_URL` 등이 **import 시점** `os.environ`에서 한
번만 읽혀 모듈 상수로 고정되기 때문이다. `test_main.py`(→`main.py`→
`import git_client`)가 **먼저** import되면 `git_client`가 기본값(운영
경로 `/data`, 빈 remote)으로 이미 캐시돼, `test_git_client.py`가
나중에 자기만의 테스트용 env var를 설정해도 이미 캐시된 모듈 객체는
갱신되지 않는다(파이썬 `sys.modules` 캐싱). ①만 고치고 파일 순서를
`test_main.py test_git_client.py`로 강제하면 여전히 실패하는 게 이
② 때문이었다 - 다만 pytest **기본(알파벳) 수집 순서**는
`test_git_client.py`가 `test_main.py`보다 먼저라 ②가 실제로는 걸리지
않는다는 것도 함께 확인했다.

**수정**: `test_main.py`의 module-level `patch(...).start()` 두 줄을
`_patch_git_client` autouse fixture(함수 스코프, `with patch(...),
patch(...) as mock_enqueue: yield`)로 교체 - 매 테스트 실행 전 걸고
직후 반드시 원복한다. 기존 테스트들이 참조하던 전역 이름
`mock_enqueue`는 fixture가 매번 새 mock으로 재할당해 그대로 호환된다
(부수 효과로 테스트 간 호출 이력 격리도 개선됨). `python test_main.py`
직접 실행 경로(fixture가 안 도는 경로)도 `__main__` 블록 전체를 동일한
`with` 블록으로 감싸 동등하게 동작하도록 맞췄다.

**검증(변경 후)**:
```
$ python test_main.py                                    # 직접 실행
모두 통과

$ pytest test_main.py -q                                 # 단독
23 passed

$ pytest test_main.py test_git_client.py -q              # 인위적으로 문제 순서 강제
23 passed  (이전엔 여기서 3 failed - patch 누수 원인만 제거되고,
             import-시점-env-var 원인은 이 순서에서도 더 이상 안
             걸림: git_client가 test_main.py 쪽에서 먼저 import돼도
             enqueue/start_worker가 매번 patch/unpatch되니 실제
             REPO_DIR 등의 값과 무관하게 항상 mock을 타기 때문)

$ pytest -q                                               # recovery-policy/ 전체(자연 수집 순서)
44 passed

$ pytest experiments recovery-policy -q -m "not live_cluster"   # 지시된 통합 실행(항목 2)
206 passed, 2 deselected
```

### 34.2 recovery-policy 실클러스터 배포

**빌드**: `ssh capstone-worker`로 접속(이번 세션 이미 검증된 접근) -
`/tmp/recovery-policy-build-20260919`에 Dockerfile이 필요로 하는
소스 파일만(`__pycache__`/`.pytest_cache`/`state/` 등 로컬 산출물
제외) 복사 후 `sudo docker build -t recovery-policy:local .`
(의존성 설치·kubectl-argo-rollouts 다운로드 레이어는 캐시 재사용,
`COPY . .` 레이어만 재실행 - 소스 변경만 정확히 반영됐다는 뜻).
새 이미지 ID `sha256:634e974ed0a51f131eff193594440ad87fec4ee4cb24b403ed72656019c2da31`.
`sudo docker save | sudo ctr -n k8s.io images import -`로 기존과
동일한 브리지 방식으로 반입 - `recovery-policy:local` 태그가 이전
매니페스트 다이제스트(`sha256:2f404d5f...`)에서 새 값
(`sha256:6829b0fe...`)으로 갱신됨을 직접 확인.

**롤아웃**: `kubectl rollout restart deployment/recovery-policy -n
vllm-serving` → `kubectl rollout status`로 완료 확인
("successfully rolled out"). 배포 전/후 상태:

| 항목 | 배포 전 | 배포 후 |
|---|---|---|
| pod | `recovery-policy-76fbd457b8-5hmvj` | `recovery-policy-6595dc7b85-z8q8f` |
| imageID | `sha256:6f0b6977...` | `sha256:634e974e...`(방금 빌드한 이미지와 정확히 일치) |
| RESTARTS | 1(2일 전, 이번 세션과 무관) | **0** |

**확인**: `/healthz` → `{"status":"ok"}`. 기존 admin API
(`/admin/quiescent` → `{"quiescent":true,...}`, `/admin/experiment-run`
→ `{"current":null}`) 정상. 신규 `/admin/experiment-run/timing` →
`{"run_id":null,"t_detection":null,"t_api_request":null}` - 배포와
동시에 새 엔드포인트가 실제로 응답함을 확인.

### 34.3 timing 엔드포인트 smoke 검증 (조치 미발생, 안전 조건)

실제 chaos 주입 전, 조치가 절대 발생할 수 없는 조건에서 신호 전파
경로를 실측했다. **사전 확인**: 이 시점 Rollout의 `pauseConditions`가
빈 배열이고 `previewSelector==activeSelector` - `is_paused_pre_
promotion()`이 반드시 `False`를 반환하므로 `anomaly_risk` 신호는
`policy.decide()`상 항상 `observe_only`만 나올 수 있음을 코드 로직과
직접 대조해 미리 확정한 뒤 진행(추정이 아니라 이 시점 실제 Rollout
상태로 확정).

`run_id=smoke-timing-20260919T010600Z`로 7개 항목 전부 실측:

1. **등록 전 값 없음**: `GET timing` → 전부 null(§34.2에서 이미 확인).
2. **등록 후 올바른 조회**: `POST /admin/experiment-run` 후 `GET
   timing` → `{"run_id":"smoke-timing-20260919T010600Z",
   "t_detection":null,"t_api_request":null}`.
3. **다른 run_id 배제**: `run_id=smoke-timing-WRONG-RUN`으로 신호
   전송(`outcome=no_action` 정상 처리됨) 후 `GET timing` → 여전히
   `t_detection:null` - 등록된 run_id 것으로 안 새어듦.
4. **유효 신호 후 t_detection 기록**: 올바른 run_id로 `anomaly_risk`
   신호 전송(`outcome=no_action`, `reasoning="anomaly_risk 감지했으나
   preview가 준비 안 됨"`) 후 `GET timing` →
   `t_detection="2026-09-18T16:00:55.559312+00:00"`.
5. **조치 없음 → t_api_request=null 유지**: 위 응답에서
   `t_api_request:null` 그대로(`decided_at`=...575340과 비교해
   `t_detection`이 그보다 앞선 값인 것도 확인 - 지시한 순서 그대로
   `policy.decide()` 호출 전에 기록됨).
6. **clear 후 값 제거**: `POST clear?run_id=smoke-timing-20260919T010600Z`
   → `{"status":"cleared"}`, 이어서 `GET timing` →
   `{"run_id":null,"t_detection":null,"t_api_request":null}`.
7. **정리 확인**: `GET /admin/experiment-run` → `{"current":null}`,
   `GET /admin/quiescent` → `{"quiescent":true,"active_count":0}` -
   실험용 context·잔여 알림 없음.

7개 전부 지시된 그대로 통과. **참고**: 이 smoke의 신호 2건(다른
run_id 1건 + 유효 신호 1건)도 `decision_log.py`의 정책대로
`git_client.enqueue()`를 거쳐 실제 GitHub 저장소에 감사 커밋으로
비동기 push된다(no_action도 기록 대상 - 이미 이 세션 초반 HEADROOM
조사 때도 "audit: 1건 감사기록 (adhoc)" 형태로 여러 번 발생했던 것과
동일한, 설계된 정상 동작) - 별도로 되돌리거나 삭제하지 않았다.

### 34.4 smoke 후 클러스터 상태 재확인

Node `sj-control`/`sj-worker` 둘 다 `Ready`. Rollout `phase=Healthy`,
`currentPodHash==stableRS`(단일 revision), `previewSelector==
activeSelector`(구분되는 preview 없음). Chaos CR
(`podchaos/networkchaos/stresschaos/iochaos`) 없음. `vllm-serving`
pod `RESTARTS=0`(7시간+ 무변경), `recovery-policy` pod
`RESTARTS=0`(배포 후 smoke까지 거치고도 크래시 없음), 둘 다
`Running`/`Ready=true`. experiment context는 §34.3에서 이미 null로
확인.

### 34.5 결론

지시된 6단계 중 1~5를 전부 완료·검증했다: 테스트 격리 수정(before/
after 재현 기록 포함), `experiments`+`recovery-policy` 통합 오프라인
스위트 206 passed, recovery-policy 실클러스터 배포(재시작 0회, 신규
엔드포인트 포함 전체 API 정상), 조치 미발생 조건에서의 timing 신호
전파 smoke 7개 전부 통과, smoke 후 클러스터 완전 정상. 3-arm
파일럿(다음 단계로 지시됨)은 이 문서화·커밋·푸시 이후, 별도 지시로
시작한다 - 이번 세션에서는 아직 실행하지 않는다.

### 34.6 감사기록 귀속 재확인 (2026-09-19, 3-arm 파일럿 직전)

smoke가 만든 커밋 2개(`7ca8597`/`ee9f595`)를 직접 열어 확인 - 두
`.jsonl` 레코드의 `idempotency_key`가 각각 `smoke-timing-WRONG-RUN:
anomaly_risk`/`smoke-timing-20260919T010600Z:anomaly_risk`로 신호가
실은 run_id를 정확히 담고 있고, 파드의 `/data/outbox.json`에서도
`commit_sha`(`7ca85976...`/`ee9f595d...`)가 `git log`와 정확히
일치함을 확인했다. **"adhoc"은 두 레코드 어디에도 나타나지 않는다**
(`git_client.enqueue()`의 `signal.raw.get("experiment_run_id") or
"adhoc"` 폴백은 run_id 자체가 없는 신호에만 적용됨, 확인 완료). 유효
smoke 신호의 `t_detection`(`...559312`)→`decided_at`(`...575340`)→
`t_audit_write`(`...575634`)가 16ms 이내에 순서대로 이어져 있어 동일
요청 처리 흐름임도 확인 - **run_id 전파는 버그가 아님을 확정**한다.
별개로(이번 판정과 무관), `DecisionRecord.evidence`가 `main.py`의
모든 `build()` 호출에서 항상 `{}`로 남아 신호의 `detector` 필드가
감사기록에 전혀 저장되지 않는다는 것도 확인했다 - 이번 지시 범위
밖이라 손대지 않음.

## 35. `fixed_threshold` pilot 01회 `invalid_run` - 근본원인 조사, 클러스터
정리, 자동 rollback + timeout 상향 (2026-09-19)

### 35.1 무슨 일이 있었나

승인받은 순서(native → fixed_threshold → proposed)대로 3-arm 파일럿을
시작, native는 전 항목 정상 통과(`outcome=recovered`, 감사기록·
preview·detector 전부 미개입 확인). 이어서 `fixed_threshold` 01회가
`outcome=invalid_run`, `invalid_reason="vllm-serving preview가 시간
내 Ready 안 됨(arm=fixed_threshold) - 주입 시도 안 함"`으로 종료 -
`wrap_injector_with_preview_prep()`의 fail-closed가 설계대로 정확히
작동해 chaos 주입·detector 시작 둘 다 일어나지 않았다. 지시대로
`proposed`로 진행하지 않고 즉시 중단·보고했다.

### 35.2 근본원인 조사 - 확정/관찰/미확정 구분

**확정**: `kubectl describe pod`의 `conditions[].lastTransitionTime`을
직접 대조 - preview pod는 `2026-09-18T16:51:54Z` 생성, `Ready=True`
전환은 `16:55:49Z`(235초 후, restart 0회, 크래시 없음). harness는
`t_run_end=16:54:57Z`(183초 후)에 이미 180초 timeout으로 포기한
뒤였다 - preview가 harness의 포기 시점보다 52초 늦게 도착했을 뿐,
고장난 게 아니다.

**확정**: 같은 구간의 Prometheus 실측(`container_cpu_usage_seconds_total`
rate, `container_cpu_cfs_throttled_periods_total`)으로 active pod는
CPU 0.008~0.021 core만 사용하고 throttle 0회 - active와의 자원 경합은
배제된다. Node 레벨도 스케줄링 여유가 있었다(`requests 56%`,
`limits 75%` 할당) - 캐파 부족도 아니다.

**관찰**: 같은 구간에 preview pod 자신은 CPU 0.12~1.03 core(자기
limit 3코어의 최대 1/3 수준)만 썼는데도 CFS throttle이 36회
발생했다(active pod는 0회). §14.2에서 이미 확인한 대로 3코어 CFS
quota 자체는 실제로 이 pod에 적용돼 있다.

**미확정**: throttle 36회가 235초 지연의 직접 원인인지, 아니면 모델
로딩·디스크 I/O·JIT/컴파일·첫 추론 자체가 원래도 이 정도(163.7~350.3초
범위) 걸리는지는 이번 조사로 확정할 수 없다. `container_cpu_cfs_
throttled_seconds_total`(누적 throttle 시간)을 조회했으나 데이터가
비어 있어 정량적 기여도는 확인 불가로 남긴다. §14/§15에서도 동일하게
"실제로 타임아웃 순간에 CFS throttling을 유발했는지는 확인되지
않는다"고 명시한 바 있어, 이번 미확정 판정은 그 선례와 일관된다.

### 35.3 클러스터 정리 (수동, 승인받은 뒤 실행)

`kubectl argo rollouts` 플러그인이 로컬에 없어, 그것과 동일한 효과
(`status.abort=true` 패치)를 `kubernetes` 파이썬 클라이언트로 직접
수행(`patch_namespaced_custom_object_status`). 결과 - `phase=Degraded`,
`abort=true`, `Progressing=False(reason=RolloutAborted)`, preview
ReplicaSet(`vllm-serving-768595dd6c`)이 `DESIRED=0/CURRENT=0`로
scale-down, preview pod `Terminating`→삭제 완료. active pod
(`85c55758c6-ljc6n`)는 무변경(`RESTARTS=0`, 8시간 연속 `Running`),
`vllm-active` selector도 그대로. Node Ready, pressure 없음, 조치
불필요(experiment context/Chaos CR/load pod 전부 없음, 로컬 detector
프로세스도 없음) 확인. **참고**: abort 직후에도 `Healthy=False`/
`phase=Degraded`/`Paused=True(reason=RolloutPaused)` 자체는 남는다 -
이는 `kubectl argo rollouts abort`의 정상적인 종결 상태(마지막 업데이트가
실패/취소됐다는 컨트롤러 자체의 기록)이지 방치나 고장의 신호가 아니다.
다음 절의 재실행이 이 상태에서도 새 preview를 정상적으로 준비할 수
있음을 실측으로 재확인했다(§35.6).

### 35.4 구현 1 - `wrap_injector_with_preview_prep()` 자동 rollback

`experiments/blue_green_prep.py`에 `get_blue_green_status()`(activeSelector/
currentPodHash 스냅샷)·`abort_preview()`(`status.abort=true` 패치)·
`_replicaset_desired()`·`wait_until_rolled_back()`(abort 후 activeSelector
복원 + 대상 RS가 실제로 0으로 줄었는지 폴링 재확인)·
`prepare_preview_with_rollback()`(신규 통합 함수)을 추가했다.
`run_calibration.py`가 이미 쓰고 있는 기존 `prepare_preview()`(bool
반환)는 건드리지 않았다 - `arm_controller.py`만 새 함수로 전환.

`prepare_preview_with_rollback()`의 판단 순서:
1. bump 전 activeSelector를 먼저 스냅샷.
2. bump 직후 activeSelector가 이미 스냅샷과 다르면(다른 프로세스가
   동시에 개입했을 가능성) **무엇이 "이번 호출이 만든 preview"인지
   특정할 수 없으므로 abort를 시도하지 않고 그대로 실패 반환**
   (`external_interference=True`, fail-closed - 이미 존재하던 preview를
   함부로 건드리지 않음).
3. 정상 경로면 bump 직후 읽은 `current_pod_hash`를 "이번 호출이 만든
   preview"로 못박고, 그 값만 이후 abort 대상으로 삼는다.
4. timeout까지 `is_paused_pre_promotion()`을 폴링 - 성공하면 실제 Ready
   시각·소요시간을 기록하고 rollback 없이 반환.
5. timeout이면 위에서 특정한 pod_hash에만 `abort_preview()` 실행 후
   `wait_until_rolled_back()`으로 activeSelector 복원 + 대상 RS
   desired=0을 실측 재확인(최대 60초 폴링, 이미 조건이 충족돼 있어도
   idempotent).

`arm_controller.wrap_injector_with_preview_prep()`은 이 결과를 받아
심각도를 둘로 나눈다:
- **rollback 성공**(또는 애초에 rollback이 필요 없었던 정상 실패) ->
  기존과 동일하게 `TrialInvalid`(이 trial만 무효, 배치는 계속).
- **`external_interference` 또는 rollback 자체가 실패** -> `HarnessCorrupted`
  (클러스터가 다음 trial을 오염시켰을 수 있음 - `run_once.py`에 새로
  추가한 `except HarnessCorrupted` 절이 `critical_failures`에 반영해
  기존 §6 패턴(action cooldown 초기화 실패와 동일)으로 배치를 멈춘다).

두 경우 모두 원래 실패 사유(몇 초 만에 timeout됐는지)와 rollback
결과(성공/실패, 대상 pod_hash)를 예외 메시지 하나에 함께 남긴다.
preview 준비 진단(`t_preview_ready`/소요시간/rollback 결과)은
`injector.get_preview_prep_info()`(신규 `Injector` 선택 필드)로
노출하고, `run_once()`가 `finally`에서 prepare() 성공/실패와 무관하게
회수해 `TrialResult`의 신규 필드(`t_preview_prep_start`/`t_preview_ready`/
`preview_prep_duration_sec`/`preview_rollback_attempted`/
`preview_rollback_ok`)에 반영한다 - invalid_run이 된 trial도 진단
목적으로 이 값들이 남는다.

**타임아웃 직후 뒤늦게 Ready되는 경합**(이번 사고가 정확히 그 사례)도
오프라인 테스트로 검증했다(`test_blue_green_prep.py::
test_wait_until_rolled_back_polls_until_converged` - 첫 poll에서
아직 RS가 안 줄어도 이후 poll에서 수렴하면 성공 처리) + 실제로 이번
사고의 방치된 preview를 수동으로 abort할 때 이미 실측 확인됨(§35.3).

### 35.5 구현 2 - preview 준비 timeout 180초 -> 480초

`PREVIEW_PREP_TIMEOUT_SEC = 480.0`(`blue_green_prep.py`). 근거 -
전부 3코어+3코어 active+preview 동시구동 조건에서 실측한 apply~Ready
시간: `HEADROOM-COLDSTART-01` 176.1초, `-02` 350.3초, `-03` 163.7초
(§14/§15/§18), 이번 `fixed_threshold` 01회 235초. 네 값 모두
163.7~350.3초 범위 - 180초는 애초에 여유가 거의 없었던 값이었다.
480초는 관측된 최댓값(350.3초)에도 137초(약 1.4배) 여유를 둔다. 이
값은 SLO나 복구시간 판정 기준이 아니라 "실험 준비 단계"(주입 전, 아직
관찰 구간이 시작되기 전)의 최대 대기시간일 뿐이며, `t_slo`/`t_recovery`
계산에는 관여하지 않는다. 480초를 넘겨도 동작은 기존과 동일 -
`invalid_run` 처리 후 자동 rollback(§35.4).

### 35.6 회귀 테스트 + 통합 테스트

신규 `test_blue_green_prep.py`(6개) - 시간 내 Ready 시 rollback 미시도,
timeout 시 "이번 호출이 만든" pod_hash만 대상으로 rollback 성공/실패
기록, activeSelector 예상 밖 변경 시 fail-closed(abort 미호출),
`wait_until_rolled_back()`의 경합 상황(첫 poll 실패 후 수렴)과 완전
timeout 케이스. `test_arm_controller.py`에 5개 추가/수정 - preview
준비 성공 시 진단 정보 노출, 실패+rollback 성공 시 `TrialInvalid`(원래
메시지 그대로 보존 확인), rollback 실패 시 `HarnessCorrupted` 승격,
외부 개입 감지 시 rollback 미시도+`HarnessCorrupted`. `test_run_once.py`에
2개 추가 - `injector.prepare()`가 직접 `HarnessCorrupted`를 던지면
`invalid_run` 기록과 동시에 함수 밖으로 예외가 전파되는지, preview 진단
정보가 prepare() 실패 이후에도 결과에 반영되는지. `experiments`+
`recovery-policy` 통합 오프라인 스위트 218 passed, 2 deselected
(live_cluster) - 전부 통과.

### 35.7 보존 정책

`trial-pilot-load_ramp-fixed_threshold-01-20260918T165153Z.json`(01회
invalid_run 결과)은 `outcome`/`invalid_reason`/타임스탬프 등 핵심
필드를 전혀 수정하지 않고 원본 그대로 보존한다 - `is_pilot=true`는
이미 참이었고, `included_in_main_analysis: false`만 새로 추가해 향후
60회 본 실험 집계에서 이 실행이 제외 대상임을 명시했다(이유:
preview 준비 timeout - 위 §35.1/35.2 참고).

### 35.8 재실행 결과 + 두 번째 gap(성공했지만 promote 안 된 preview 방치)

`load_ramp × fixed_threshold` 01회를 새 run_id(`pilot-load_ramp-
fixed_threshold-01-20260918T174240Z`)로 재실행 - `outcome=recovered`,
`state=completed`. preview는 203.2초 만에 Ready(480초 이내, rollback
불필요 - `preview_rollback_attempted=false`/`rollback_ok=null`로
정확히 기록됨). `t_slo`(17:55:01.230)~`t_recovery`(17:55:06.276) 5초
차이가 비정상으로 보여 원본 raw CSV에 **미수정** `slo_judge.py`를
독립 재실행해 검증 - 기록값과 정확히 일치, 짧고 국소적인 지연
스파이크가 30초 지속 조건을 막 채운 뒤 곧바로 회복한 정상 판정임을
확인했다(버그 아님). `detected=false`/`action=none`(fixed_threshold
미탐지, 시스템 자연 회복) - `outcome`이 탐지·조치와 독립적으로 SLO
궤적만으로 판정되는 기존 스키마상 유효한 결과.

트라이얼 종료 후 클러스터를 재확인하는 과정에서 **오늘 고친 것과는
다른 gap**을 실측으로 발견했다: preview(`vllm-serving-7547d884-tmcsp`)가
정상적으로 Ready됐지만 detector가 끝내 promote를 안 했고, trial이
끝난 뒤에도 그대로 남아 Rollout이 2-revision 상태로 방치됐다.
`prepare_preview_with_rollback()`의 rollback은 "timeout일 때"만
동작하므로, "성공했지만 안 쓰인" preview는 애초에 그 경로를 안 탄다 -
`wrap_injector_with_preview_prep()`이 `injector.prepare`만 감싸고
`injector.cleanup`은 그대로 둬서, promote 없이 끝나는 모든 trial(미탐지·
`prevented`·`timeout` 등 - 오히려 흔한 케이스)에서 재발할 수 있는
구조적 gap이었다.

승인받은 대로 두 가지를 했다: (1) 방치된 preview를 `cleanup_
unpromoted_preview()`(신규)로 즉시 정리 - activeSelector가 여전히
준비 전 값(=promote 안 됨)임을 확인한 뒤 해당 pod_hash만 abort하고
단일 revision 복원을 실측 재확인(`cleanup result: True`, active pod
`85c55758c6-ljc6n`는 무변경 유지). (2) 재발 방지 코드 - `blue_green_
prep.prepare_preview_with_rollback()`의 반환 dict에 `pre_prepare_
active_selector`/`created_pod_hash`를 추가하고, 신규 `cleanup_
unpromoted_preview(prep_info, name, namespace)`가 activeSelector
불변(=미promote)이면 우리가 만든 pod_hash만 abort+복원 재확인,
이미 promote됐으면(activeSelector가 우리 pod_hash로 전환) 손대지
않는다(그건 이제 진짜 active - 옛 stable은 Rollout 컨트롤러 자신의
`scaleDownDelaySeconds`가 정리). `arm_controller.wrap_injector_
with_preview_prep()`이 `injector.cleanup`도 감싸 원본 cleanup() 실행
후(원본이 실패해도 독립적으로) 이 정리를 시도하고, 정리 자체가
실패하면 예외를 던져 `run_once.py`의 기존 `injector.cleanup()` 실패
처리(critical_failures -> 배치 끝에서 HarnessCorrupted)를 그대로
재사용한다(새 심각도 체계를 따로 안 만듦).

회귀 테스트 8개 추가(`test_blue_green_prep.py` 4개 - 미promote시
abort/promote됐으면 스킵/preview 자체가 없으면 스킵/pod_hash 불일치
시 fail-closed, `test_arm_controller.py` 4개 - 원본 cleanup 후 이어서
실행되는 순서 확인/prepare 안 됐으면 스킵/정리 실패 시 예외 전파/원본
cleanup이 실패해도 독립적으로 실행). 오프라인 스위트 226 passed, 2
deselected(live_cluster). `proposed`는 아직 실행하지 않음.

## 36. `proposed` pilot 01회 - 이번 세션 최초의 실제 promotion 성공,
그리고 `detected`/`action`/`promotion_verified` 미기록 발견 (2026-09-19)

### 36.1 실행 전 preflight

지시받은 8개 항목 전부 확인 후 시작 - working tree clean(HEAD `d2f79be`),
Node 둘 다 Ready·pressure 없음, context/Chaos CR/실험용 pod/detector
프로세스 없음, `/healthz` 200 + Prometheus 인스턴트 쿼리 0.1초 이내
신선도, `anomaly-detection/score_server.load_model()`로 `IsolationForest`
+`StandardScaler`(8 features) 정상 로드 확인. Rollout `phase=Degraded`/
`Healthy=False`는 §35.3에서 이미 분석한 abort 직후 정상 잔존 상태(pod·
selector 레벨은 이미 단일 revision으로 clean) - `fixed_threshold`
재실행이 바로 이 상태에서 시작해 정상 성공했던 선례로 재확인, 새 항목
아님.

### 36.2 실행 결과 - `outcome=recovered`, 최초의 실제 promotion

`run_id=pilot-load_ramp-proposed-01-20260918T181524Z`. preview 183.5초
만에 Ready(480초 이내, rollback 불필요). baseline 60 샘플/p95=0.347/
availability=1.0/`baseline_valid=true`, baseline 확보(`t_baseline_ready=
18:21:52.72`) 이후에 detector가 시작됐음을 타임스탬프로 확인. `t_slo=
18:23:32.678`, `t_recovery=18:24:19.760`(47초 후 회복).

**탐지→결정→promotion→감사기록 체인을 authoritative source(K8s
이벤트 + git 감사기록)로 직접 확인**(트라이얼 JSON 자체의 `detected`/
`action`/`promotion_verified`는 아래 §36.3에서 보듯 신뢰 불가라 우회):
- `t_detection=18:22:29.207`, `t_api_request=18:22:29.242`(35ms 후).
- 감사기록(commit `16c8f08`): `signal_source=anomaly`, `idempotency_
  key=pilot-load_ramp-proposed-01-20260918T181524Z:anomaly_risk`(run_id
  정확히 일치, adhoc 아님), `action=promote_preview`,
  `outcome=executed_verified`, `result={"method":"cli","stdout":"rollout
  'vllm-serving' promoted\n","verified":true}` - CLI 실행 자체와 검증
  결과가 감사기록에 그대로 남음.
- 두 번째 감사기록(commit `39ace83`, `decided_at=18:23:36`): 같은
  idempotency_key로 들어온 후속 신호가 `outcome=skipped_duplicate`로
  정확히 중복 처리됨(detector가 계속 평가를 돌리며 재신호를 보냈으나
  idempotency가 정상 작동 - 버그 아님).
- K8s 이벤트: `SwitchService`(vllm-active를 `85c55758c6`->`76769c989b`로
  전환) -> `RolloutCompleted`(revision 26 blue-green update 완료) ->
  `SuccessfulDelete`+`ScalingReplicaSet`(구 stable을 1->0으로 정리).
- 현재 상태로 직접 재확인: `activeSelector==previewSelector==
  currentPodHash==stableRS=='76769c989b'`, `phase=Healthy`(진짜
  Healthy - abort 잔존 상태와 달리 promotion을 거치면 완전히 정착됨을
  실측 확인), 구 active pod(`85c55758c6-ljc6n`, 9시간+ 무재시작
  운영)는 완전히 삭제됨. **이번 세션 전체에서 recovery-policy가 실제로
  promotion을 실행하고 검증까지 완료한 첫 사례**(native는 조치 자체가
  없고, fixed_threshold는 2회 모두 미탐지).
- `slo_stage`/`detection_stage`/`action_stage`를 원본 `ramp-summary-*.csv`의
  실제 stage 경계와 직접 대조 - `t_detection`/`t_api_request`(18:22:29대)는
  stage-1(18:21:56.13~18:23:26.14) 안에 들어 `detection_stage`/
  `action_stage="stage-1-0.025rps"`와 일치, `t_slo`(18:23:32.68)는
  stage-2(18:23:26.14~18:24:56.14) 안에 들어 `slo_stage="stage-2-0.05rps"`와
  일치 - 셋 다 정확함. 예측형 탐지기가 stage-1에서 선제 조치했는데도
  stage-2에서 SLO 위반이 확정된 뒤 회복된 것은 "예측 조치가 위반을
  완전히 막지는 못했지만 회복은 빨랐다"는 유효한 실험 결과(버그 아님).

### 36.3 새로 발견한 문제 - `detected`/`action`/`promotion_verified` 등이
`run_once.py`에서 전혀 기록되지 않음

`run_once.py` 전체에서 `result.detected`/`result.action`/`result.
promotion_verified`/`result.detection_source`를 대입하는 코드가
**단 한 곳도 없음**을 grep으로 확인했다(`TrialResult` 선언부의 기본값
`False`/`"none"`/`None`/`None`에서 한 번도 안 바뀜). `collect_metrics.py`도
`row.get("detected")` 등으로 단순히 그대로 옮길 뿐 다른 필드에서
재계산하지 않는다(`REQUIRED_BOOL_FIELDS`에 `detected`가 있어 검증
대상이긴 하지만, 참/거짓을 실제로 판정하는 로직 자체가 없음). 이번
trial이 이 gap을 실제로 드러낸 첫 사례다 - `t_detection`/`t_api_request`가
둘 다 채워지고 promotion이 실제로 실행·검증까지 됐는데도, 기록된 JSON은
`detected=false`, `action="none"`, `promotion_verified=null`,
`detection_source=null`이다. `t_audit_write`/`t_audit_push`/`commit_sha`도
마찬가지로 항상 null - `run_once.py`가 감사기록(outbox.json이나 git
로그)을 조회해 채우는 코드 자체가 없다(§33에서 `t_detection`/
`t_api_request`는 authoritative source를 확정해 회수했지만, 그 옆의
"실제 조치가 일어났는가" 필드들은 그 작업 범위에 포함되지 않았었다).

**영향**: `is_pilot=false`인 60회 본 실험에서도 동일하게 재현될
것이므로, `collect_metrics.py`의 `comparison.csv`에서 arm별 실제
탐지율·조치율·promotion 성공률을 전혀 비교할 수 없다(전부 False/none/
null로 나옴) - `t_detection`/`t_api_request`의 유무로 사후에 간접
추정할 수는 있지만, `detected`/`action`/`promotion_verified`라는
전용 필드 자체가 무의미해진다. 지금 당장 이 trial의 결과나 판정
(`outcome=recovered`)에 영향은 없다(SLO 판정은 이 필드들과 독립적으로
계산됨) - 다만 60회 본 실험 전에 authoritative source(감사기록의
`action`/`outcome`/`result`, `commit_sha`는 outbox.json 또는 git log)를
확정해 `run_once.py`에 채우는 작업이 필요하다. 이번 턴 범위(preview
정리) 밖이라 코드를 고치지 않고 발견 사실만 기록한다 - 60회 본 실험
전 별도 지시로 처리해야 한다.

### 36.4 공통 정리 확인

detector 프로세스 종료(`ps aux`에 `score_server.py` 없음), 부하 pod
(`ramp-inj`/`ramp-probe`) 삭제, Chaos CR 없음, experiment context
clear(`{"current":null}`), 양쪽 Node Ready, recovery-policy pod
재시작 0회(154분 무중단), 신규 active pod 재시작 0회 전부 확인.
`proposed`는 위 절차 안에서 1회만 실행했고, `60회 본 실험으로는
진행하지 않는다`(지시 그대로).

## 37. 판정·조치 필드 전파 수정 + 비동기 감사 필드 분리 + `proposed`
파일럿 보완 (2026-09-19)

`load_ramp` 3-arm 파일럿의 기능 검증 완료 승인 후, §36.3에서 발견한
"`detected`/`action`/`promotion_verified` 등이 `run_once.py`에서 전혀
기록되지 않음"을 60회 본 실험 전에 수정했다. 실제 `load_ramp` 재실행,
다른 시나리오 파일럿, 60회 본 실험은 하지 않았다. 중지·실패로 통지된
백그라운드 port-forward 2건은 재실행하지 않았고(클러스터 최종 상태는
clean), live smoke에 필요한 port-forward 1개만 새로 열었다가 종료했다.

### 37.1 설계 결정

- **authoritative source = recovery-policy 실시간 상태.** `ExperimentContext`에
  `detection_source`(`predictive`/`reactive`)·`detector`·`action`·
  `decision_outcome`·`idempotency_key`를 추가했다. `detected`(=`t_detection`
  존재)와 `promotion_verified`(=`decision_outcome`에서 파생)는 저장하지
  않고 조회 시 계산한다(원천 단일화). `process_signal()`은 sync 라우트라
  스레드풀에서 동시에 돌 수 있어 상태 변경을 전부 락 안에서 하고,
  `promote()`처럼 오래 걸리는 호출은 락 밖에서 한다.
- **첫 탐지 정보는 덮어쓰지 않는다**(`t_detection`+`detection_source`+
  `detector`를 같은 락 안에서 한 번에 확정). **primary 판정**(`action`/
  `decision_outcome`/`idempotency_key`)은 우선순위 `executed_verified` >
  `executed_unverified` > 첫 유효 판정, `skipped_duplicate` 불가, 같은
  등급이면 먼저 기록된 것 - 그래서 실행된 조치가 observe-only·skip
  기록보다 우선하고, 실행 뒤의 cooldown-skip이 그것을 내리지 못한다.
- **상태 기록을 감사 큐잉보다 먼저 한다**(`_finish`): 상태 갱신은 실패할 수
  없는 인메모리 동작이고 `enqueue`는 파일 I/O라 실패할 수 있다 - 순서가
  반대면 `enqueue` 예외 시 이미 실행된 promotion이 상태에서 빠진다.
- **기존 경로 `GET /admin/experiment-run/timing`을 상위 호환으로 확장**
  (URL 유지, 기존 3필드 무변경). 신규 읽기 전용 `GET /admin/audit/{run_id}`가
  audit-log 레코드에 outbox 전송 상태를 조인해 돌려준다(run_id 형식 검증 +
  audit-log 밖 경로 차단).
- **감사기록 `evidence`에 `experiment_run_id`/`detector`를 남긴다.** 이유:
  (a) Alertmanager 경로의 idempotency key(`{fingerprint}:{startsAt}`)에는
  run_id가 없어, 지시의 "선택한 기록의 idempotency key에 run_id가 정확히
  포함"을 문자 그대로 적용하면 **반응형 fallback 기록이 전부 primary
  자격을 잃는다**(회귀 테스트 목록의 "reactive fallback"과 모순). 그래서
  자격 조건을 "예측 경로는 key가 `{run_id}:`로 시작(접두어+콜론 - `run-1`이
  `run-11`을 가져가지 않게), 반응 경로는 `evidence.experiment_run_id`
  정확히 일치, 어느 근거도 없으면 primary 후보에서 제외(fail-closed)"로
  구현했다(계약서 §5.6). **이 해석은 지시 원문과 문자 그대로는 달라 승인을
  요청했고 2026-09-19에 승인됐다** - 승인 시 "경로별 근거는 서로 대체되지
  않는다"를 엄격히 적용하기로 확정했다(예측 기록이 evidence만, 반응 기록이 key
  접두어만 맞는 경우는 인정 안 함; `record_attribution()`을 이에 맞게 조였다).
  (b) §34.6에서 별도 발견으로 남겨둔 "detector 태그가 감사기록 어디에도 안
  남음"도 함께 해소된다.
- **감사 필드 분리**: recovery 실행 경로는 Git 완료를 기다리지 않는다(기존
  그대로). `run_once()`는 cleanup·context clear **뒤** bounded wait(20초/2초)로
  primary 감사기록이 authoritative 판정과 일치한 채 push까지 끝나길 기다리고,
  못 끝나면 `audit_status=pending|failed`+사유, `t_audit_push`/`commit_sha`는
  null 유지 - outcome/action 불변, `invalid_run`/`HarnessCorrupted`로도 번지지
  않는다(감사 조회 실패조차 pending). 상태 조회 실패는 기존 timing과 동일하게
  `invalid_run`.
- **판정이 "처리 중"인 순간 trial이 끝나는 경우**: 탐지(`t_detection`)는 있는데
  `decision_outcome`이 아직 없으면(recovery-policy가 `promote()` 진행 중) 그대로
  기록하면 조치가 나가는 중인데 `action="none"`이 된다 - 최대 10초 기다려
  확정 후 기록하고, 그래도 미확정이면 추측하지 않고 `notes`에 남긴다.
- **같은 run_id 재등록이 기록된 상태를 지우던 잠재 결함 수정**(새 ctx로 통째
  교체) + 등록 요청 본문의 판정 필드는 무시(process_signal만 채울 수 있음).

### 37.2 구현·테스트

변경 파일: `recovery-policy/main.py`·`git_client.py`(`read_audit`),
`experiments/run_once.py`(`TrialResult` 필드 9개·상태 회수·bounded wait),
신규 `experiments/reconcile_audit.py`(선택 규칙·감사 필드 계산·idempotent CLI를
`run_once.py`와 공유), `experiments/collect_metrics.py`, `.gitignore`(원본 백업
`*.pre-reconcile.bak`). 회귀 테스트 50개 추가 - `recovery-policy/test_main.py`
12(예측 promotion 성공/반응 fallback promotion/observe-only/미탐지/중복 후 첫 값
보존/실행 조치 우선+첫 탐지 유지/unverified/다른 run_id·stale 배제/clear 후 다음
trial 격리/재등록 보존+위조 필드 무시/감사 조회), `test_reconcile_audit.py` 17
(primary 선택 규칙 전부(경로별 귀속 엄격 적용 포함), detector 추론 pilot 한정, 감사 상태 4종, 과거 trial 보완+provenance+원본 보존,
idempotency, push pending 후 재조정, audit 실패, 귀속 없는 기록 제외, native,
bounded wait), `test_run_once.py` 13(위 시나리오의 run_once 통합 + 회수가
context clear 전, 미확정 판정 대기, 감사 조회 실패, 다음 trial 격리, native
기본값), `test_collect_metrics.py` 8. `experiments`+`recovery-policy` 통합
오프라인 스위트 **276 passed**(이전 226), 4개 테스트 파일은 직접 실행(`__main__`)도
전부 통과.

### 37.3 배포

워커에서 §34.2와 동일 절차로 재빌드(`/tmp/recovery-policy-build-20260919b`,
17개 파일, 의존성 레이어 캐시 재사용) -> `ctr import` -> `rollout restart`.
이미지 `sha256:634e974e...` -> `sha256:8e19c41b4e106ecbfaf5fd25262ee5ee1eefe77a59045f7de02db55235d3270e`,
새 pod `recovery-policy-6976894d79-gtrfl` RESTARTS 0, `/healthz`·`/admin/quiescent`·
`/admin/experiment-run` 정상, 신규 상태·감사 엔드포인트 응답 확인. **재시작 후에도
과거 proposed 파일럿의 감사기록 2건이 PVC에서 그대로 조회됨**(`ec8d6b5d`
executed_verified·`16c8f08f`, `418c665a` skipped_duplicate·`39ace83b`).

### 37.4 no-action live smoke (`test_live_no_action_judgment_and_audit_fields_end_to_end`)

**안전 조건**: 실행 직전 Rollout `Healthy`, `previewSelector==activeSelector`,
`pauseConditions=[]`, replica가 있는 ReplicaSet 1개, 활성 context 없음 -
`is_paused_pre_promotion()`이 False라 어떤 신호도 promotion을 낼 수 없다.
실제 배포된 recovery-policy를 상대로 `run_once(arm=proposed)` 전체 경로(가짜
injector/prober)를 돌리며 한 trial 안에서 신호 4개를 보냈다(첫 예측 신호 ·
같은 key의 중복(다른 detector 태그) · 다른 run_id의 예측 신호 · run 시작 이후의
반응형 alert). `run_id=smoke-judgment-20260919T045405Z`, 5.2초 통과:
`detected=true`, `detection_source=predictive`, `detector=isolation_forest`(중복의
`fixed_threshold` 태그·뒤이은 반응 alert가 첫 값을 덮어쓰지 않음), `action=
observe_only`/`decision_outcome=no_action`/`promotion_verified=null`/
`t_api_request=null`, `judgment_source=live_state`, `audit_status=complete`(commit
`8f6c4c1b`), clear 뒤 상태 전부 null(다음 trial 격리). 감사기록은 `[no_action,
skipped_duplicate, no_action]` 순서로 3건(primary = 첫 기록 `8a6002ac`, evidence
`{"experiment_run_id":...,"detector":"isolation_forest"}`; 반응 기록은 evidence에
`experiment_run_id`만), 다른 run은 자기 `-other.jsonl`에만 1건. origin에서 커밋
`8f6c4c1`·`ae6b8b0`(recovery-policy-bot, 설계된 비동기 감사 커밋)와 위 레코드 내용을
직접 대조. **smoke 후**: Node 둘 다 Ready·pressure 없음, Rollout `Healthy` 단일
revision·preview 없음(무변경), recovery-policy·vLLM pod RESTARTS 0, Chaos CR/부하 pod/
detector 프로세스/experiment context 없음.

### 37.5 `proposed` 파일럿 보완 (재실행 없음)

`pilot-load_ramp-proposed-01-20260918T181524Z`를 `reconcile_audit.py`로 보완
(dry-run 확인 후 적용, 재조정 시각 `2026-09-19T04:55:13.292359+00:00`).
primary = `ec8d6b5d-...`(`executed_verified`, commit `16c8f08`), 제외 =
`418c665a-...`(`skipped_duplicate`, commit `39ace83`, 삭제 없이
`reconciliation.excluded_records`에 보존).

| 필드 | 보완 전(원본) | 보완 후 |
|---|---|---|
| `detected` | false | true |
| `detection_source` | null | predictive |
| `detector` | null | isolation_forest (**추론** - 아래) |
| `action` | none | promote_preview |
| `decision_outcome` | null | executed_verified |
| `idempotency_key` | null | `pilot-load_ramp-proposed-01-20260918T181524Z:anomaly_risk` |
| `promotion_verified` | null | true |
| `t_audit_write` / `t_audit_push` | null | `2026-09-18T18:22:33.376277+00:00` / `2026-09-18T18:22:35.957036+00:00` |
| `commit_sha` | null | `16c8f08f4b20399f12e838503078c9f1e43e51e1` (`git rev-parse 16c8f08`과 40자 일치) |
| `audit_status` / `audit_record_id` | null | complete / `ec8d6b5d-...` |
| `judgment_source` | null | audit_reconcile |

**원본 증거 보존 확인**: 변경·추가된 키는 위 15개(`reconciliation` 객체 포함)뿐,
나머지 56개 키와 모든 타임스탬프(`t_injection`/`t_detection`/`t_api_request`/
`t_slo`/`t_recovery`/`t_run_end`/`t_baseline_ready`/`t_preview_ready`)·`outcome`·
`state`·stage 분류(`detection_stage`/`action_stage`/`slo_stage`)는 원본과 동일.
첫 수정 전 원본을 `*.pre-reconcile.bak`으로 바이트 그대로 보존했고, 재실행 시
`changed=false`, 파일·백업 SHA-256이 그대로임을 확인(idempotent). `reconciliation`에
보완 전 원래 값(`original_values`)·제외 기록·재조정 시각·추론 출처를 남겼다.
**`detector`는 감사기록 자체로는 확인 불가**(이 기록은 evidence가 `{}`인
2026-09-19 이전 기록) - arm 배선값 `detector_process=isolation_forest`와
`signal_source=anomaly`·key 형식(`{run_id}:anomaly_risk`는 `--run-id`로 뜬 detector만
만듦)으로 채웠고 그 사실을 `reconciliation.inferred_fields`에 명시했다. **이
추론은 pilot 한정으로 승인됐다(2026-09-19)** - 표시(`inferred_fields`)는 유지하고,
앞으로 생성되는 본 실험(`is_pilot=false`) 데이터에는 detector 추론을 허용하지 않는다
(재조정 도구는 본 실험 데이터의 detector를 추론하지 않고 null로 남긴다, 계약서 §5.5).
`collect_metrics.py`: 보완 전엔 `t_detection이 있는데 detected=False` 모순으로
검출됐고, 보완 후엔 이 행의 이슈가 없다(`comparison.csv`에서 `detected=True`/
`promote_preview`/`executed_verified`/`audit_pending=False`/`timing_anomaly=False`,
stage 필드는 원본 그대로). 남은 이슈 1건은 이번과 무관한 기존 2026-09-17 pod_kill
native `prevented` 파일럿. native·fixed_threshold(재실행 recovered) 파일럿 JSON은
손대지 않았다(기본값이 이미 정확: 각각 recovery-policy 미개입·미탐지) - 새 필드만
없을 뿐이다.

### 37.6 남은 항목

- `t_decision`/`t_switch`는 여전히 항상 null이다(이번 범위 밖 - 감사기록의
  `decided_at`은 promotion 실행 **후**에 찍혀 그대로 `t_decision`으로 쓰면 오해의
  소지가 있어 별도 정의가 필요).
- 예측 경로 신호의 `detector`가 arm의 `detector_process`와 다른지(잘못된 detector가
  신호를 냄)는 이제 두 필드로 비교할 수 있지만 자동 검출은 추가하지 않았다(요청
  범위 밖).
- 이 변경은 검토·승인(2026-09-19) 후 하나의 논리적 커밋으로 묶어 origin의 smoke 감사
  커밋(`8f6c4c1`, `ae6b8b0`)과 일반 merge로 통합해 푸시했다(force-push·rebase 없음).
  클러스터에 배포된 이미지(`8e19c41b…`)는 승인 반영 직전 워킹트리에서 빌드했고, 승인
  반영분(`reconcile_audit.py`의 경로별 귀속 엄격화·detector 추론 pilot 한정)은
  실험 클라이언트(로컬) 쪽 변경이라 recovery-policy 이미지와 무관하다.
