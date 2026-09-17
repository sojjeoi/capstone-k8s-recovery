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

**아직 안 한 것(다음 세션, 별도 명시적 승인 필요)**: overlay 실제
적용·promote·calibration 스크립트 실행·10초 후보값 확정, `network_degrade`
실클러스터 첫 trial, `memory_pressure_adapter.py`, Isolation Forest 검증
강화, CPU headroom 해결, 3-arm 파일럿. 오프라인 스위트 전체 64 passed,
2 skipped(live_cluster) - `test_pod_kill_adapter.py`(리팩터 후 재확인)
5개, `test_network_degrade_adapter.py`(신규) 5개 포함.
