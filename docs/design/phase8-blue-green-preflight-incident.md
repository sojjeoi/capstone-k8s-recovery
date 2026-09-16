# Phase 8 BlueGreen Preflight + NodeNotReady 사건 기록 (2026-09-16)

> 3-arm 파일럿(`native`/`fixed_threshold`/`proposed`) 시작 전 Phase 7 인프라
> (Argo Rollouts BlueGreen promotion 경로)가 실제로 살아있는지 확인하려던
> preflight 도중 노드 장애가 발생했다. Promotion 자체는 preflight를 통해
> 끝까지 성공 검증했지만, 그 과정에서 발견한 리소스 문제 때문에 3-arm
> 파일럿은 보류한다. **후속(2026-09-17 새벽)**: `pod_kill` native E2E 실행
> 전 preflight에서 같은 사건의 잔존 영향으로 보이는 Service 라우팅 간헐적
> 이상을 발견해 주입을 다시 보류했다 — §5 참고.

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

## 5. pod_kill 실행 전 진단 — Service 라우팅 간헐적 이상 (2026-09-17 새벽, 읽기 전용)

`pod_kill × native × 1회` 실행 전 preflight 중 `recovery-policy → vllm-active`
Service 경로에서 완전한 요청 실패(8~10초 타임아웃)를 발견해 주입을 보류하고,
변경·재시작 없이 증거만 수집했다(2026-09-16 약 23:13 KST(≈14:13 UTC)부터
2026-09-17 새벽까지, 클러스터 시각 기준).

### 5.1 결론 요약

| 항목 | 결론 |
|---|---|
| `etcdInsufficientMembers`(critical) 등 control-plane alert 5종 | **실제 etcd/control-plane 장애 아님** — API 서버 `/readyz?verbose`의 `etcd ok`/`etcd-readiness ok` 확인, Prometheus가 kube-etcd·kube-proxy·kube-scheduler·kube-controller-manager 스크레이프 타겟을 못 읽는 모니터링 파이프라인 문제로 판단됨 |
| `recovery-policy → vllm-active` Service 경로 | **간헐적 실패** — 완전히 깨진 것도 완전히 정상인 것도 아님. 같은 요청(Service DNS이름 + POST `/v1/completions`)이 연속 5/5 타임아웃 → 곧바로 연속 13/13 성공. 아래 4가지 분류 중 하나로 깔끔히 안 떨어짐(§5.3) |
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
`200, ~2ms`) — 다만 이건 특정 시점 1회 관측일 뿐이다(§5.3).

**kube-proxy(sj-worker) 로그**: 2026-09-04 최초 기동 이후 조용하다가, **2026-
09-16 08:53:19 UTC**부터 `watch of *v1.Service ended ... http2: client
connection lost`, 이어서 **08:54:39~09:02:39 UTC** 구간에 API 서버
(`https://192.168.30.4:6443`) 대상 `TLS handshake timeout`으로 Service/
EndpointSlice list·watch가 반복 실패. **09:02:39 UTC 이후로는 로그가 한
줄도 없음**(최근 150줄 기준, 지금까지 6시간 이상). 사건 시작(09:11:55Z)
**직전**부터 실패가 시작돼 사건과 시간적으로 이어져 있다 - 다만 로그가
멈췄다는 사실과 "그 이후 reconcile을 안 하고 있다"는 것은 다른 주장이라,
간헐적 성공이 실제로 관측된 이상(§5.3) 현재 kube-proxy가 완전히 죽어
있다고 단정할 근거는 아니다.

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

### 5.3 Service 경로 이상 분류 — 깔끔히 안 떨어짐

말씀하신 4분류(Pod IP만 성공/전부 실패/Ready endpoint 없음/DNS부터 실패)는
"고정된" 증상을 전제하는데, 실제 관측 순서는:

1. 최초 발견(전날 늦은 밤): DNS이름 경유 `/health`·`/v1/models`·
   `/v1/completions` 전부 8~10초 타임아웃(→ "전부 실패"로 보임)
2. 그 직후: DNS 해석 성공, ClusterIP 숫자·Pod IP 숫자 둘 다 `/health` 성공
3. 곧이어: 정확히 같은 요청(DNS이름+`/v1/completions`)이 **5/5 연속 타임아웃**
4. 그 직후: 같은 요청이 **13/13 연속 성공**(0.13~0.37초, 정상 지연)

즉 "항상 이 경로만 실패"가 아니라 **버스트성 간헐적 실패**다. kube-proxy
watch 실패(09:02:39 UTC 이후 로그 없음)가 유력한 용의자이지만, 완전히
멈춰있다면 간헐적 성공을 설명하기 어렵다 - conntrack 테이블 문제나
kube-proxy가 실제로는 살아서 반복 재동기화 중일 가능성도 배제 못 한다.
**이 시점에서 단일 원인으로 단정하지 않는다.**

### 5.4 낮 시간대 작업 순서(말씀하신 우선순위 그대로)

1. `etcdInsufficientMembers` 원인 확인 - **1차 결론은 이미 남(모니터링
   스크레이프 문제, §5.1)**, 낮에 Prometheus target 페이지에서
   kube-etcd/kube-proxy/kube-scheduler/kube-controller-manager 타겟이
   실제로 어떤 에러로 down 상태인지 확인해 확정
2. `recovery-policy → vllm-active` Service 경로 복구 - 간헐적이라 "복구"보다
   "재현 후 원인 특정"이 먼저: kube-proxy 재시작 전후 동일한 반복 요청으로
   실패율 비교, 가능하면 sj-worker에서 `iptables-save`로 해당 ClusterIP의
   DNAT 규칙이 현재 pod IP(`10.244.36.2`)를 가리키는지 직접 확인
3. 오래된 `PodChaos/vllm-pod-kill` 정리(`kubectl delete podchaos vllm-pod-kill -n vllm-serving`) - YAML·status는 위 §5.2에 보존됨
4. 정상 상태 최소 10~15분 유지 확인(반복 요청 실패율 0%, Node Ready 유지)
5. 그 다음에만 `pod_kill × native × 1회` 재시도(§4의 pass 기준 그대로 적용)

또한 향후 실험 preflight에 `cluster_healthy`(control-plane 전체 alert +
Service 경로 반복 검증) 검사를 `/admin/quiescent`(vllm 전용)와 별도로
추가하는 것을 권장 - `quiescent=true`가 클러스터 전체 정상을 보장하지
않는다는 점이 이번에 확인됐다.
