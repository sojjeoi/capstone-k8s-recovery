# Phase 8 BlueGreen Preflight + NodeNotReady 사건 기록 (2026-09-16)

> 3-arm 파일럿(`native`/`fixed_threshold`/`proposed`) 시작 전 Phase 7 인프라
> (Argo Rollouts BlueGreen promotion 경로)가 실제로 살아있는지 확인하려던
> preflight 도중 노드 장애가 발생했다. Promotion 자체는 preflight를 통해
> 끝까지 성공 검증했지만, 그 과정에서 발견한 리소스 문제 때문에 3-arm
> 파일럿은 보류한다.

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

1. **`collect_metrics.py` 최소 버전 구현**(파일럿 전 필수)
   - 스키마 검증, 누락 타임스탬프 처리
   - `is_pilot`/`invalid_run`/`PREFLIGHT-EXCLUDED`를 집계에서 구조적으로 제외
   - arm·scenario·반복 수 검증
   - timing 순서 오류와 "t_SLO 자체가 안 남" 상황을 구분
   - `comparison.csv` 생성
2. **`pod_kill` adapter 구현**(native dry-run까지만 우선 목표)
   - Chaos CR 적용 시각과 실제 파드 종료 시각을 분리해서 기록
   - 대상 active pod의 UID를 주입 **전에** 고정
   - `is_effective()`: 고정해둔 UID가 사라졌는지로 판정
   - 회복 판정: 새 파드 Ready + SLO 정상화
   - `finally`에서 Chaos CR 정리
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
headroom 해결.
