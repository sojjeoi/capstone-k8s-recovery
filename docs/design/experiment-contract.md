# 실험 계약 (Phase 8 실행 전 확정)

> **3-arm 파일럿 보류 중(2026-09-16)** - Phase 7 BlueGreen promotion 경로
> 자체는 preflight로 끝까지 검증 완료했으나, 그 과정에서 8코어 노드에
> vLLM(4코어 limit) 2개가 동시에 뜨면 CPU headroom이 없어지는 문제와
> NodeNotReady 사건을 발견했다. 재개 전 판단 순서는
> `docs/design/phase8-blue-green-preflight-incident.md` 참고.

> **이 문서는 파일럿·본 실험 데이터를 보기 전에 확정한다.** 실험을 시작한 뒤 arm
> 정의·성공조건·timeout·결과 스키마를 바꾸면 제안 방식에 유리하게 기준을 고쳤다는
> 지적을 받을 수 있다(slo-definition.md와 같은 이유, guideline.md 9-6절). 확정 후
> 바꿔야 한다면 하단 "변경 이력"에 사유와 함께 남기고, 이미 그 정의로 계산된 결과가
> 있으면 같이 밝힌다.

## 1. 3개 arm — 정확한 이름과 정의

| arm 이름 | 구성 | 비고 |
|---|---|---|
| `native` | K8s 기본 self-healing만. standby(preview) 없음, recovery-policy 미기동 | 탐지·개입 자체가 없는 조건 — Alertmanager가 알림을 보내도 받아줄 서비스가 없음 |
| `fixed_threshold` | 고정 임계치 예측(`anomaly-detection/fixed_threshold.py`, CPU>90%) + **공통 Alertmanager 반응형 fallback** + standby/promotion | `proposed`와 **예측 모델만** 다르다 |
| `proposed` | Isolation Forest 예측(`score_server.py`) + **공통 Alertmanager 반응형 fallback** + standby/promotion | `fixed_threshold`와 **예측 모델만** 다르다 |

recovery-policy 서비스(`/signal` + `/webhooks/alertmanager` + Alertmanager 라우팅)는 `fixed_threshold`·`proposed` 두 arm 모두에서 동일하게 기동한다 — 차이는 오직 어떤 스크립트(`fixed_threshold.py` vs `score_server.py`)가 예측 신호를 `/signal`로 보내느냐뿐이다. **"Isolation Forest"라고만 부르지 않는다** — 두 arm 다 Alertmanager 반응 경로를 공유하므로 `proposed`를 "예측+반응 하이브리드"로 표기한다.

## 2. 비교는 두 가지로 나눠서 해석한다

- **`fixed_threshold` vs `proposed`** — recovery-policy·standby·promotion·Alertmanager 반응 경로가 전부 동일하므로 이제 정말로 **예측 모델(고정 임계치 vs Isolation Forest)만의 순수 비교**(guideline.md 9-7절 "순수 탐지방식 비교")다. `pod_kill`처럼 예측 자체가 불가능한 돌발 장애는 두 arm 모두 같은 Alertmanager 반응 경로로 대응하므로, 그 시나리오에서는 두 arm의 차이가 거의 없는 게 정상이다(예측 모델의 차이가 드러나는 건 점진적 열화 시나리오 쪽).
- **`native` vs `proposed`** — `native`엔 standby도 recovery-policy도 없으므로 이 비교는 "탐지 알고리즘 차이"가 아니라 **standby 유무까지 포함한 시스템 전체 구성 차이**다. 논문에서 이 둘을 같은 성격의 비교로 쓰지 않는다(guideline.md 9-7절 "현실적 운영 비교") — **이 conflation을 명시적으로 문서화한다.**

## 3. SLO·타임스탬프 정의 — 기존 문서를 그대로 참조

- SLO(`t_SLO`/`t_recovery` 판정 기준)는 새로 정의하지 않고 `docs/design/slo-definition.md`를 그대로 쓴다: **SLO v2**(2026-09-16 확정, probe profile `inference-max1-rps1`) 기준 `L_baseline=0.256s`, Latency SLO=P95>0.512s 30초 연속, Availability SLO=60초 윈도우 성공률<99%(30초 timeout도 실패로 카운트). v1(`L_baseline=2.686s`, max_tokens=10)은 probe observer effect로 폐기됨 - slo-definition.md 변경이력 참고.
- 9개 타임스탬프(`t_injection`~`t_audit_push`)는 guideline.md 9-2절 정의를 그대로 쓴다.

### `outcome` — 4가지, `prevented`를 조건부로만 인정

```
prevented       SLO 위반 없이 선제 전환 성공
recovered       SLO 위반 후 정상화
timeout         제한시간 내 정상화 실패
invalid_run     probe·주입·사전조건 문제
```

`prevented`는 **다음 세 조건을 모두 만족할 때만** 판정한다 — 그냥 "SLO 위반이 안 일어남"만으로는 인정하지 않는다(장애가 애초에 SLO를 못 건드릴 만큼 약했던 것일 수도 있어서, 이 경우는 선제복구 성공이 아니라 무효한 근거다):
1. probe가 trial 내내 정상 작동함(`probe_valid=true`)
2. 장애 주입이 실제 대상에 적용됐음(`injection_valid=true`)
3. 같은 시나리오의 `native` 반복(rep)들에서는 SLO 위반이 재현됨 — 즉 이 시나리오가 무개입 시 진짜로 SLO를 위반할 만큼 강하다는 게 경험적으로 확인됨

`native` 자체는 개입이 없으므로 `prevented`가 나올 수 없다(`recovered` 또는 `timeout`만 가능) -
`collect_metrics.py`가 `arm=native AND outcome=prevented`를 이상으로 검출한다.

위 3조건과 별개로, `run_once()`는 **관측 자체가 신뢰할 만했는지**를 기계적으로
확인한다(2026-09-17 추가 - pod_kill native 파일럿에서 관측 창이 열리기도
전에 probe 데이터를 한 번도 못 읽은 채 `prevented`로 오판정된 사건 이후):
probe가 주입 이후 실제로 유효한 표본을 충분히 확보했는지(`slo_evaluable_at_exit`)
확인하지 못하면 조기 종료하지 않는다. 본 실험의 `prevented`는 이 값이
`true`가 아니면 검증 오류로 취급한다(§5 참고).

## 4. 시나리오별 timeout·종료 조건

각 trial은 `t_injection`부터 시작해 아래 예산 안에서 관찰한다. **trial은 `t_recovery`가 찍혀도 즉시 끝나지 않는다** — 선제 promotion 이후에도 chaos가 기존 active(또는 원래 대상)에 계속 적용 중일 수 있기 때문이다. trial 종료 조건은 다음 네 가지가 전부 만족된 시점이다:

1. chaos 시나리오 자체가 종료(리소스 정리까지 포함)
2. 신규 active가 연속 30초 정상(`t_recovery`, 또는 예산 소진 시 timeout)
3. 잔여 Alert가 전부 `resolved` 상태로 전환(§6 quiescence와 연결)
4. probe가 깨끗하게 종료

| 시나리오 | chaos 자체 지속시간 | 관찰 여유 | trial 예산(timeout) |
|---|---|---|---|
| `pod_kill` | 즉시(지속시간 없음) | — | **5분** |
| `memory_pressure` | 14분(Workflow, 5단계) | +5분 | **19분** |
| `load_ramp` | 7.5분(450초, 5단계) | +7.5분 | **15분** |
| `network_degrade`(순수 열화) | 6분(360초, 4단계) | +5분 | **11분** |

> **`memory_pressure` 행은 2026-09-20부로 잠정 무효(provisionally invalid)** - 5단계(500MB→1GB→1.5GB→2.5GB→**5000MB**) 설계는 Phase 5 조사(`docs/design/phase5-memory-pressure-investigation.md` §2)에서 5000MB 단계가 vLLM이 아니라 stress worker 자신의 self-OOM(재시작 순환)만 유도한다고 확인돼 본 실험 후보에서 제외했다(§48.3). 새 강도 후보(500/1000/1500/2000MB, `chaos/scenario-memory-pressure-explore.yaml`)의 단계 수·지속시간이 확정되기 전까지 이 행의 14분/19분 값은 참고용 이력이며, 재계산 전에는 이 값으로 본 실험을 진행하지 않는다.

- `load_ramp`는 설계상 마지막 단계에서 SLO 위반이 보장되도록 이미 튜닝돼 있음(9-3절 반사실 요구사항). **`memory_pressure`는 이 보장 문구를 제거한다(2026-09-20)** - 위 5000MB 단계가 빠지면서 새 강도 후보(500~2000MB)가 마지막 단계에서 SLO 위반을 보장하는지는 아직 실측 근거가 없다(§48.3의 Phase 5 §3 재해석은 "포함 가능성이 있다"는 정황 증거일 뿐 사전 등록된 반사실 검증이 아니다) - live smoke 이후 별도 calibration으로 근거를 확보한 뒤에만 이 문구를 되살린다.
- **이 예산은 "조기 종료 가능한 최악의 경우"가 아니라 거의 기본 실행시간이다** — chaos 자체 종료를 기다려야 하므로 복구가 일찍 됐다고 trial이 일찍 끝나지 않는다. (5+19+15+11)분 × 3 arm × 5회 = **약 750분(12시간 30분)이 기본값**이고, preview 준비·quiescence 대기·`invalid_run` 재실행까지 포함하면 실제 일정은 **약 14~16시간**으로 잡는다.

### `load_ramp` 확정 설정(2026-09-16 - 본 실험 시작 후 변경 금지)

> **⚠️ 2026-09-18부로 잠정 무효(provisionally invalid) - `lab-cpu3-v1` 자원
> 재구성 때문**: 아래 값은 vLLM CPU limit **4코어** 기준으로 확정된
> 것이다. `gitops/apps/vllm-serving/rollout.yaml`의 limit을 CPU headroom
> 확보 목적으로 **3코어**로 낮췄다(`docs/design/phase8-blue-green-
> preflight-incident.md` §11) - 동일 부하에서 처리량/지연이 달라질 수
> 있어, 아래 SLO v2 latency threshold·ramp 단계 경계는 새 자원 구성
> (`lab-cpu3-v1`)에서 재검증하기 전까지 신뢰할 수 없다. **이 표 자체는
> 삭제·수정하지 않는다** - 4코어 시절의 실측 기록으로 그대로 남기고,
> 재검증 결과는 새 절로 추가한다.
>
> **SUPERSEDED(2026-09-18)**: SLO는 v2(`L_baseline=0.256s`, threshold
> `0.512s`)에서 **v3**(`L_baseline=0.324s`, threshold `0.648s`,
> `lab-cpu3-warm-v1` 하에서 재측정)로 갱신됐다 - 현재 유효 기준은
> `docs/design/slo-definition.md`의 SLO v3다. 이 페이지의 SLO v2 값과
> 아래 ramp 단계 표는 여전히 4코어 시절 이력으로 보존하며, ramp 단계
> 재보정은 `docs/design/phase8-blue-green-preflight-incident.md`
> §22 이하 참고.

| 항목 | 값 |
|---|---|
| probe | `1 RPS`, `max_tokens=1` (`chaos/probe-config.yaml`, `inference-max1-rps1`) |
| SLO v2 latency threshold | `0.512초`(slo-definition.md) |
| ramp 요청 | `max_tokens=10`(`chaos/scenario-load-ramp.yaml` target, probe와 별개) |
| 단계 | `0.10 → 0.25 → 0.50 → 0.75 → 1.00 RPS` (5단계, 6번째 collapse 단계 의도적으로 없음) |
| 단계별 지속시간 | `90초` |
| 총 주입시간 | `450초` |
| trial timeout | `900초` |
| `t_injection` 정의 | 첫 ramp 요청이 실제 전송된 시각(`ramp.py` stage 시작 마커를 처음 확인한 시각 - `load_ramp_adapter.py`의 `get_actual_injection_time()`) |
| 확정 커밋 | `83bb61a`(`chaos/scenario-load-ramp.yaml`·`chaos/probe-config.yaml`·`experiments/slo_judge.py` 확정본) |

3회 독립 calibration(`explore-20260916T064939Z`/`070319Z`/`071454Z`)은 재보정 근거로만 쓰고 본 실험 5회 반복에서 제외한다. 낮은 부하 구간(0.10/0.25 RPS)에서는 측정 노이즈에 따른 소폭 역전이 있었으나, 0.50 RPS 이후에는 부하 증가에 따른 지연 상승이 일관되게 나타났다. 0.50 RPS까지는 3회 모두 SLO를 준수했고, 0.75 RPS는 2/3회, 1.00 RPS는 3/3회 SLO를 위반했다. 모든 요청은 성공했으며 부하 종료 후 정상 범위로 회복했다. 0.75 RPS가 매번 위반하지 않는 것은 문제가 아니다 - 경계 구간의 변동성을 보여주고, 1.00 RPS에서 3/3회 위반했으므로 시나리오의 유효성은 확보됐다.

## 5. 결과 스키마 — trial 1회 = row 1개

`run_once()`가 trial 하나를 마칠 때마다 아래 컬럼을 가진 row 하나를 결과 파일에 남긴다.

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `run_id` | str | `{scenario}-{arm}-{rep:02d}-{UTC타임스탬프}` |
| `scenario` | enum | `pod_kill` \| `memory_pressure` \| `load_ramp` \| `network_degrade` |
| `arm` | enum | `native` \| `fixed_threshold` \| `proposed` |
| `rep` | int | 1~5 |
| `sequence_index` | int | 전체 60회 중 이 trial의 실행 순번(1~60) — 순서 효과 확인용 |
| `order_seed` | int | 이 시나리오의 arm 셔플에 쓴 난수 시드 — 재현용 |
| `is_pilot` | bool | true면 본 실험 5회 반복 집계에서 제외(결과 파일도 `results/pilot/` 아래 별도 경로) |
| `probe_profile` | str | probe 요청 프로필 식별자(예: `inference-max1-rps1`) — SLO 재현성 메타데이터 |
| `slo_version` | str | 이 trial 판정에 쓰인 SLO 버전(`v2` 등) — slo-definition.md 버전과 대응 |
| `latency_slo_sec` | float \| null | 이 trial에 실제 적용된 latency SLO 임계치(초) |
| `probe_rps` | float | probe의 목표 발사율(RPS) |
| `detector_process` | str \| null | 이 trial에 실제로 배선된 detector 식별자(2026-09-18 추가, arm orchestration 보완) - `arm_controller.make_detector_for_arm()`이 만든 `Detector.name`을 그대로 기록. `arm=native`거나 `detector=None`으로 호출됐으면 null. `detector.start()`가 실제로 성공했는지와 무관하게(시작하자마자 크래시해 `invalid_run`이 됐어도) 채워지는 감사 기록 - "이 trial이 어떤 detector로 실행되려 했는가" |
| `min_observation_sec` | float | "prevented" 조기 종료를 막는 최소 관찰시간(초) - 주입 효과 확인 직후부터 계산(2026-09-17 추가) |
| `readiness_probe_profile` | str \| null | `network_degrade` 전용 - K8s readiness/livenessProbe.timeoutSeconds 설정 `"default"`(K8s 기본 1초 - 발견 5의 재시작 연쇄장애 재현)/`"network_tolerant"`(gitops/apps/vllm-serving/overlays/network-tolerant/ 적용). 다른 시나리오·미적용 trial은 null(2026-09-18 추가). SLO 측정용 HTTP probe를 가리키는 `probe_profile`과는 다른 축 |
| `readiness_probe_timeout_sec` | float \| null | 위 profile의 실제 timeoutSeconds 값 - `run_network_degrade_trial.py`가 실행 직전 active pod에서 직접 읽어 검증한 값을 기록(2026-09-18 추가) |
| `target_replaced` | bool | 주입이 실제 효과를 낸 뒤 대상 pod 자체가 바뀐 것을 어댑터가 관측했는지(2026-09-18 추가, 리뷰 정정). **invalid_run이 아니다** - `readiness_probe_profile=default`면 연쇄장애(재시작)가 예상 가능한 결과, `network_tolerant`면 calibration 실패나 예상 밖 재시작을 뜻할 수 있음(해석은 두 필드를 같이 봐야 함). 주입이 아직 효과를 내기 전의 대상 변경은 여전히 `invalid_run`으로 남는다(외부 오염과 실험 결과를 구분하는 경계가 "효과를 낸 적이 있는가"). **이 필드는 "바뀌었다"는 어댑터 관측일 뿐 원인을 말하지 않는다** - 검증된 promotion이 만든 변경(실험 처치)과 그 밖의 변경(재시작 연쇄·교체 후보)의 구분은 스키마 변경 없이 `collect_metrics.py`의 파생 해석이 한다(§5.9) |
| `t_target_replaced` | ISO8601 UTC \| null | 위 교체를 처음 관측한 시각 - `target_replaced=false`면 null |
| `target_replacement_pod_name` / `target_replacement_pod_uid` | str \| null | 교체된 pod의 이름/UID - active selector가 단일 pod으로 특정되면 채워지고, 전환 중이라 2개 이상 동시 매칭되는 등 특정할 수 없으면 null(교체 자체는 여전히 기록됨) |
| `timing_schema_version` | str | `"v2"` = 아래 3분할 주입 시각 + observed_at 기준 `t_slo`/`t_recovery`. 필드 없음 또는 `"v1"` = 옛 방식(단일 `t_injection`, `t_slo`/`t_recovery`가 `sent_at` 기준)(2026-09-18 추가) |
| `t_run_start` | ISO8601 UTC | preview 준비 등 trial 준비 시작 시각 |
| `t_baseline_ready` | ISO8601 UTC \| null | 주입 전 baseline 관찰 단계(2026-09-18 추가, §29)가 안정 상태(20+ 표본·P95≤threshold·가용성≥99%가 30초 연속)에 도달한 시각. `prober.get_baseline_status()` 미구현 어댑터는 이 단계 자체를 건너뛰어 항상 null |
| `baseline_sample_count` / `baseline_p95` / `baseline_availability` | int/float \| null | 위 시점의 창 표본 수·P95(초)·성공률 스냅샷 - baseline 조건이 제한시간(120초) 안에 안 채워졌으면 마지막 관측값(미달성 상태) |
| `baseline_valid` | bool \| null | `true`=조건 충족 후 주입 진행, `false`=120초 안에 못 채워 `invalid_run`, `null`=어댑터가 baseline 단계 자체를 구현 안 함(검증 안 함 - `slo_evaluable_at_exit=null`과 동일 관례) |
| `t_injection_request` | ISO8601 UTC | `injector.inject()` 호출 직전 시각 - 실제 주입 구간의 하한(2026-09-18 추가) |
| `t_injection` | ISO8601 UTC | **하위 호환용 대표값** - `t_injection_observed`와 항상 같다(2026-09-18부터. 그 전에는 이 필드 하나만 있었음) |
| `t_injection_last_seen` | ISO8601 UTC \| null | 기존 대상이 살아있음을 마지막으로 관측한 시각 - `injector.get_last_seen_present_time()` 미구현이거나 첫 poll에서 이미 사라졌으면 null(2026-09-18 추가) |
| `t_injection_observed` | ISO8601 UTC | 주입 효과를 처음 관측한 시각. 어댑터가 `get_actual_injection_time()`을 구현하면 실제 삭제/시작 시각 그 자체가 아니라 폴링으로 그 변화를 **처음 관측한** 시각(미구현이면 `t_injection_request`와 동일)(2026-09-18 추가, 이전엔 `t_injection`이 이 역할) |
| `injection_observation_error_sec` | float \| null | 3단계 우선순위로 계산(2026-09-18 정정): 1) 어댑터의 `get_injection_observation_error_sec()`, 2) 없으면 `t_injection_last_seen`~`t_injection_observed`, 3) `t_injection_last_seen`도 없으면(첫 poll에서 이미 사라짐) `t_injection_request`~`t_injection_observed`. 전부 실측 구간이지 `poll_interval_sec` 같은 임의 설정값은 없다(2026-09-16 정정) - `injection_valid=true`인 trial은 이제 이 필드가 절대 null로 남지 않는다 |
| `t_injection_end` | ISO8601 UTC | chaos 자체가 끝난 시각(§4 종료조건①) |
| `t_detection` | ISO8601 UTC \| null | **authoritative source: recovery-policy**(2026-09-19 명확화). 현재 run에 속하는 유효한 예측 신호(`/signal`) 또는 반응형 alert(`/webhooks/alertmanager`)를 recovery-policy가 처음 수락해 정책 판단 대상으로 확정한 시각 - `process_signal()`이 idempotency 통과 직후, `policy.decide()` 호출 전에 `datetime.now()`로 동기 기록한다. 재시도·중복 신호로는 덮어써지지 않고(첫 값만 유지), 다른 run_id의 신호나 stale alert(등록 시각 이전)는 애초에 후보가 안 된다. `run_once()`가 `GET /admin/experiment-run/timing`으로 회수 - detector 프로세스 stdout이나 비동기 Git 감사기록은 원천으로 안 씀. `arm=native`는 조회 대상 자체가 없어 항상 null. 조회 자체가 실패하면(non-native) `invalid_run`(§5.1) |
| `detection_stage` | str \| null | `t_detection`이 실제로 어느 실험 단계에 속했는지(2026-09-18 추가, stage 관측성 보완 - `load_ramp`, 2026-09-20부터 `network_degrade`도 구현). `t_detection`이 null이면 이 필드도 null(사건 자체가 없음). 값은 아래 `slo_stage`와 동일한 분류 체계 |
| `t_decision` | ISO8601 UTC \| null | **authoritative source: recovery-policy**(2026-09-19 확정, §5.5). 현재 run의 유효 신호(idempotency·run_id·stale 검사 통과)에 대해 정책(`policy.decide()`)이 action을 확정한 **직후**의 서버 시각 - `process_signal()`이 `datetime.now()`로 동기 기록한다. **observe-only여도 기록**하고, 조치 자체가 없는 판정(rule-out/unknown)도 "정책이 확정한" 사건이라 기록한다(뒤이은 cooldown-skip 후보 포함). 첫 값만 유지 - 중복·후속 신호로 덮어쓰지 않는다(그래서 예측 신호가 먼저 observe-only로 판정된 뒤 반응 신호가 promotion을 실행하면 `t_decision`은 첫 유효 판정의 시각이고 `t_api_request`/`t_switch`는 실행된 promotion의 것이다 - 순서는 여전히 성립). 미탐지·native는 null. **2026-09-19 이전 trial(과거 pilot)은 null이며 추정으로 채우지 않는다** |
| `t_api_request` | ISO8601 UTC \| null | **authoritative source: recovery-policy**(2026-09-19 명확화). 실제 promotion API/CLI 호출(`rollouts_client.promote()`)을 시작하기 직전의 시각 - cooldown 통과 후, `promote()` 호출 바로 앞에서 `datetime.now()`로 동기 기록. 조치가 없으면(observe_only/rule-out/unknown/cooldown-skip) null로 남는다. 구조적으로 `t_detection <= t_decision <= t_api_request`(같은 요청 처리 흐름 안에서 순서대로 기록되거나, 더 이른 신호가 이미 `t_detection`/`t_decision`을 채운 뒤 나중 신호가 조치로 이어짐). promotion이 없으면 `t_api_request`와 `t_switch`는 둘 다 null. 나머지 회수 방식은 `t_detection`과 동일 |
| `action_stage` | str \| null | `t_api_request`(정책이 실제로 조치를 실행한 시각) 기준 stage 분류 - `t_api_request`가 null이면 null. 분류 체계는 `slo_stage`와 동일 |
| `t_switch` | ISO8601 UTC \| null | **authoritative source: recovery-policy**(2026-09-19 확정, §5.5). promotion 후 **active selector가 preview와 일치함을 처음 검증한 서버 시각** - `rollouts_client.promote()`의 verify 루프가 첫 성공을 확인한 그 순간에 찍는 `verified_at`을 그대로 쓴다(`promote()`가 반환한 뒤의 시각이 아님, 감사기록의 promotion 결과에도 같은 `verified_at`이 남는다). selector 전환의 정확한 발생 시각이 아니라 그 변화를 **처음 관측한** 시각이라 폴링 간격(0.5초)+API 지연만큼의 관측 오차가 있다. **promotion이 없거나 selector 검증이 끝내 실패하면(`executed_unverified`) null**. 첫 값만 유지. promotion 경로의 구조적 순서 `t_detection <= t_decision <= t_api_request <= t_switch`는 `collect_metrics.py`가 검증한다. 과거 pilot은 null이며 추정으로 채우지 않는다 |
| `t_slo` | ISO8601 UTC \| null | `outcome=prevented`면 null. `timing_schema_version=v2`부터는 실패가 확정된 **완료 시각**(`sent_at+latency`) 기준 - 요청을 보낸 시각(`sent_at`)이 아니다(2026-09-18 정정 - pod_kill 파일럿에서 `t_slo`가 `t_injection`보다 앞서는 사례 발견, 원인은 전송 시각을 판정 시각으로 오용한 것). |
| `slo_stage` | str \| null | `t_slo`가 실제로 어느 실험 단계에 속했는지(2026-09-18 추가) - `injector.classify_stage()`가 `ramp.py --summary-out`이 기록한 실제(명목 아님) `stage_start_utc`/`stage_end_utc`로 판정한다. 값은 stage 이름(예: `stage-3-0.20rps`) 또는 `baseline`(첫 stage 시작 전)/`inter_stage_tail`(stage 사이 straggler 대기 구간)/`drain`(마지막 stage 종료 후)/`unknown`(summary fetch·파싱 실패 - 임의 추정 안 함). `t_slo`가 null이거나 어댑터가 `classify_stage` 미구현이면 null(스키마 §6.1 참고). **`network_degrade`(2026-09-20부터)**는 `ramp.py` 요약이 아니라 어댑터가 **실제로 만든** 각 NetworkChaos stage의 창(CR 생성 호출이 돌아온 시각 ~ 소멸을 확인한 시각, 양 끝 포함)으로 판정한다 - 만들어지지 않은 stage(§5.10 `treatment-induced truncation`)는 창이 없어 그 뒤 시각은 `drain`이고, 소멸을 확인하지 못한 창은 열린 채로 둔다(끝났다고 추정하지 않음). 이 구현 이전에 실행된 파일럿 JSON 3건은 세 stage 필드가 null이다(소급 생성하지 않음) |
| `t_recovery` | ISO8601 UTC \| null | timeout이면 null |
| `t_audit_write` | ISO8601 UTC \| null | **원천: recovery-policy의 outbox 상태**(2026-09-19 확정, §5.5). 감사기록을 PVC에 동기로 쓴 시각이라 push 전에도 알 수 있으면 채운다. 판정이 없으면(`audit_status=not_applicable`)·native는 null |
| `t_audit_push` | ISO8601 UTC \| null | **복구시간 계산에 포함 안 함**. 원천은 outbox의 push 완료 시각 - push가 끝나기 전(`audit_status=pending/failed`)엔 null을 유지한다(§5.5) |
| `commit_sha` | str \| null | 원천은 outbox의 push 후 HEAD SHA - `t_audit_push`와 같은 이유로 push 완료 전엔 null. 선택된 **primary 감사기록**의 것이다(§5.6) |
| `detected` | bool | **authoritative source: recovery-policy**(2026-09-19 확정, §5.5). 현재 run의 유효 신호가 idempotency/stale/run_id 검사를 통과했는지(= `t_detection`이 찍혔는지). 기본값을 유지하지 않고 trial 종료 시 recovery-policy 상태에서 채운다. native는 항상 false |
| `detection_source` | enum \| null | **의미 변경(2026-09-19)** - `predictive`(예측 경로 `/signal`) \| `reactive`(Alertmanager fallback) : **최초 유효 탐지의 경로**. 예전 정의("무엇이 먼저 반응했는지" - detector 이름 enum)는 아래 `detector`로 분리했다(이 필드는 그때까지 한 번도 채워진 적이 없어 과거 데이터와의 충돌 없음). 중복·후속 신호로 절대 덮어쓰지 않는다. 미탐지·native는 null |
| `detector` | str \| null | **신규(2026-09-19)** - 최초 유효 탐지의 **실제 source**: 예측 경로는 신호 payload의 detector 태그(`isolation_forest` \| `fixed_threshold`), 반응 경로는 `alertmanager`. `detector_process`(arm 배선이 "무엇을 띄우려 했는가")와 다른 값이다 - 예측 경로인데 `detector != detector_process`면 잘못된 detector가 신호를 낸 것. 미탐지·native는 null |
| `action` | enum | `promote_preview` \| `observe_only` \| `none`(2026-09-19 확장) - **정책이 실제로 선택한 조치**(primary 판정의 것, §5.6). `observe_only`는 탐지는 했으나 조치 없이 관찰만 하기로 한 판정. 조치 기록이 없으면(미탐지·rule-out·native) `none` |
| `decision_outcome` | str \| null | **신규(2026-09-19)** - primary 판정의 결과(`executed_verified` \| `executed_unverified` \| `no_action` \| `skipped_rule_out` \| `skipped_cooldown` \| `skipped_unknown_signal`). `skipped_duplicate`는 primary가 될 수 없어 이 값으로 나오지 않는다. 미탐지·native는 null |
| `idempotency_key` | str \| null | **신규(2026-09-19)** - primary 판정의 idempotency key(예측 경로는 `{run_id}:{signal_type}`, 반응 경로는 `{fingerprint}:{startsAt}`) |
| `promotion_verified` | bool \| null | promotion을 **실제로 실행했을 때**의 selector 검증 결과(`rollouts_client.promote()`의 `verified` - active selector가 preview와 일치하는가): `executed_verified`면 true, `executed_unverified`면 false, 실행하지 않았으면 null. native는 항상 null |
| `judgment_source` | str \| null | **신규(2026-09-19, provenance)** - 위 판정·조치 필드의 출처: `live_state`(trial 종료 시 recovery-policy 실시간 상태에서 회수) \| `audit_reconcile`(판정 필드가 기록되기 전에 만들어진 과거 trial을 `reconcile_audit.py`가 감사기록으로 보완) \| null(native, 또는 상태 조회 실패로 권위 없는 기본값 - 그 trial은 `invalid_run`) |
| `audit_status` | str \| null | **신규(2026-09-19)** - 비동기 감사 필드의 상태: `complete`(primary 감사기록이 push까지 끝남) \| `pending`(Git push 대기·기록 미확인·조회 실패) \| `failed`(push 실패) \| `not_applicable`(판정이 없어 감사기록 대상 아님) \| null(native). **정책 결과와 분리** - 어떤 값이어도 `outcome`/`action`은 바뀌지 않는다(§5.5) |
| `audit_status_reason` | str \| null | `pending`/`failed`의 사유(예: `Git push 대기 중(outbox status=pushing)`, outbox `last_error`, `audit 조회 실패: ...`) |
| `audit_record_id` | str \| null | 선택된 primary 감사기록의 `record_id`(§5.6) |
| `audit_reconciled_at` | ISO8601 UTC \| null | 감사 필드를 마지막으로 회수·재조정한 시각 |
| `reconciliation` | object \| null | **신규(2026-09-19, provenance)** - `reconcile_audit.py`가 과거 trial의 판정 필드를 보완했을 때만: `tool`, `source`, `primary_record_id`, `first_detection_record_id`, `excluded_records`(제외된 기록과 사유 - 예: `skipped_duplicate`), `supplemented_fields`, `inferred_fields`(감사기록 자체로는 확인 못 해 추론한 값의 출처), **`original_values`(보완 전 원래 값)**, `reconciled_at` |
| `outcome` | enum | `prevented` \| `recovered` \| `timeout` \| `invalid_run`. `arm=native`에서 `prevented`가 나오면 그 자체로 이상(§3 마지막 줄) - `collect_metrics.py`가 검출 |
| `slo_evaluable_at_exit` | bool \| null | `outcome=prevented`로 종료한 시점에 probe가 "위반 없음"을 신뢰할 만큼 유효한 표본을 확보했었는지(2026-09-17 추가). `prevented`가 아니거나 어댑터가 `is_slo_evaluable()`을 구현 안 했으면 null - null은 "검증 안 함"이지 "위반 없음이 확인됨"이 아니다. 본 실험(`is_pilot=false`)의 `prevented`는 이 값이 `true`가 아니면 `collect_metrics.py`가 검증 오류로 취급 |
| `injection_valid` | bool | 장애가 실제 대상에 적용됐는지 |
| `probe_valid` | bool | probe가 trial 내내 정상 작동했는지 |
| `invalid_reason` | str \| null | `outcome=invalid_run`일 때만 채움 |
| `p95_peak` | float \| null | trial 중 관측된 최고 P95(초) |
| `availability_min` | float \| null | trial 중 최저 60초-윈도우 성공률 |
| `t_run_end` | ISO8601 UTC | trial 종료(§4 네 조건 전부 만족) |
| `notes` | str | 자유 텍스트 |

### 비동기 감사기록 reconcile

`git_client.py`의 push는 비동기라 trial row를 처음 쓰는 시점엔 `t_audit_push`/`commit_sha`가 비어있을 수 있다. 전체 실험(또는 각 시나리오) 종료 후, recovery-policy의 outbox/audit 상태를 다시 읽어 각 `run_id`에 대응하는 결과 row의 감사 필드를 채워 넣는 **reconcile 단계**를 실험 절차에 명시한다(`collect_metrics.py` 실행 전에 반드시 거침). 2026-09-19부터 이 단계는 `experiments/reconcile_audit.py`(idempotent CLI)로 구현돼 있다 - 상세 규칙은 §5.5/§5.6.

### 5.1 stage 분석은 참고용이며 본 실험에서 필수가 아니다 (2026-09-18 추가)

`slo_stage`/`detection_stage`/`action_stage`, 그리고 `experiments/
results/`에 trial마다 별도로 저장되는 `ramp-summary-{run_id}-{arm}-
{rep}.csv`(stage별 실제 `stage_start_utc`/`stage_end_utc`·목표/실제
RPS·성공률)는 **참고용 보조 정보**다. 본 실험(60회)에서 trial의 핵심
판정(`outcome`/`t_slo`/`t_recovery`/`injection_valid`/`probe_valid`
등)은 이 정보의 확보 여부와 완전히 독립적으로 결정된다.

`ramp.py --summary-out` 요약을 fetch하는 데 실패해도(kubectl 오류, pod
조기 종료, 손상된 CSV 등) 해당 trial은 그대로 정상 진행되고, 관련
stage 필드만 `unknown`(대응하는 timestamp 자체가 null이면 그대로
`null`)으로 남는다 - **trial이 이 이유만으로 `invalid_run`이 되지
않는다.** 따라서 본 실험 60회 중 일부 trial의 stage 필드가 `unknown`
으로 남는 것 자체는 재실행 사유가 아니다.

다만 `load_ramp` 60회 전체가 체계적으로(예: 이미지 자체의 `--summary-
out` 관련 결함으로 전부 fetch 실패) stage 정보를 못 얻으면, 그 사실은
실행 로그·집계 보고서에 명시하고 사후 해석 시 "stage 위치는 명목값
(주입 시각+90초 단위)으로만 참고 가능, 실제 경계는 확인 불가"라고
표시한다 - 추정값을 확정값처럼 보고하지 않는다.

**`network_degrade` 예외(2026-09-20)**: 이 시나리오는 stage 창을 어댑터가 직접 기록하므로(`ramp.py` 요약 같은 외부 fetch가 없다)
`action_stage`가 `unknown`이 되는 경우는 창이 하나도 없을 때(주입 전 실패) 정도뿐이고, `action_stage`는 §5.10의 주 비교 지표다.
그래도 trial의 핵심 판정(`outcome` 등)이 stage 필드에 의존하지 않는다는 위 원칙은 그대로다.

### 5.2 `t_detection`/`t_api_request` 회수 - admin 엔드포인트 (2026-09-19 추가)

recovery-policy가 `_current_experiment`(§1 ambient 등록 메커니즘)에
`t_detection`/`t_api_request`를 직접 보관하고, `GET /admin/experiment-
run/timing`으로 조회할 수 있게 한다:

```
GET /admin/experiment-run/timing
-> {"run_id": str | null, "t_detection": ISO8601 | null, "t_api_request": ISO8601 | null}
```

**(2026-09-19 확장, §5.5)** 같은 경로(URL 유지 - `run_once.py`가 이미 쓰는 경로)가 이제
timing뿐 아니라 판정·조치 필드까지 담는 "현재 실험 상태" 조회다:
`detected`, `detection_source`, `detector`, `action`, `decision_outcome`,
`idempotency_key`, `promotion_verified`가 추가됐고(위 스키마 표의 정의 그대로),
기존 세 필드는 무변경이라 이전 클라이언트와 호환된다. 같은 날 뒤이어 `t_decision`(정책이
action을 확정한 직후)과 `t_switch`(promotion 후 active selector 검증이 처음 성공한
시각)도 같은 응답에 추가했다 - 파이프라인 timing 4종(`t_detection`/`t_decision`/
`t_api_request`/`t_switch`)이 모두 이 한 엔드포인트의 서버 시각이다.

활성 experiment context가 없으면 전부 null. `run_once()`는 이 값을
읽을 때 응답의 `run_id`가 자기 trial의 `run_id`와 일치하는지 반드시
확인한다 - 불일치(레이스, 등록 유실 등)나 조회 자체의 실패(네트워크
오류 등)는 조용히 null로 남기지 않고 **명시적으로 `invalid_run`**
처리한다("무탐지"와 "확인 불가"를 구분하기 위함, 지시) - 단, 이미 다른
사유로 `invalid_run`이 확정된 trial의 기존 `invalid_reason`은 덮어쓰지
않는다(더 구체적인 원인 보존). `arm=native`는 이 엔드포인트 자체를
호출하지 않는다(recovery-policy가 안 떠있음 - §1).

### 5.3 `RECOVERY_POLICY_SIGNAL_URL` 도달성 사전 확인 (2026-09-19 추가)

`score_server.py`/`fixed_threshold.py`를 로컬 서브프로세스로 띄우는
`arm_controller.make_detector_for_arm()`은, 실제로 서브프로세스를
시작하기 전에 `RECOVERY_POLICY_SIGNAL_URL`(또는 미지정 시 로컬 기본값
`http://localhost:8080/signal`)이 도달 가능한지 같은 host:port의
`/healthz`로 확인한다(`/signal`에 직접 요청하면 진짜 신호로 처리돼
idempotency·감사기록이 오염되므로 부작용 없는 엔드포인트를 씀). 도달
불가면 `TrialInvalid`를 던져 detector 프로세스 자체를 시작하지 않고,
`detector.start()`가 baseline 확보 후·주입 직전에 호출되므로(§32) chaos
주입도 자동으로 일어나지 않는다(fail-closed, 지시).

### 5.4 preview 준비 timeout·자동 rollback (2026-09-19 추가)

`fixed_threshold`/`proposed`의 `injector.prepare()` 앞에 배선되는 BlueGreen
preview 준비는 480초(기존 180초 - 실측 apply~Ready 163.7~350.3초 근거,
§35 참고) 안에 Ready 안 되면 실패로 처리하되, 실패 시 이번 호출이 만든
preview만 자동 abort하고 activeSelector·단일 revision 복원을 실측
재확인한다. rollback까지 성공하면 `TrialInvalid`(이 trial만 무효),
activeSelector가 예상 밖으로 바뀌었거나(다른 프로세스 개입 가능성 -
fail-closed로 abort 자체를 시도 안 함) rollback 자체가 실패하면
`HarnessCorrupted`(배치 중단, 수동 확인 필요)로 승격한다. 이 timeout은
SLO·복구시간 판정 기준이 아니라 실험 준비 단계의 최대 대기시간이며,
`t_preview_prep_start`/`t_preview_ready`/`preview_prep_duration_sec`/
`preview_rollback_attempted`/`preview_rollback_ok`로 결과에 기록된다
(§5 스키마 반영).

preview 준비가 **성공**했는데 detector가 끝내 promote를 안 하고 trial이
끝나는 경우(미탐지·`prevented`·`timeout` 등)는 위 timeout-rollback
경로를 안 타므로 별도 처리가 필요하다 - `injector.cleanup()` 실행 후
`cleanup_unpromoted_preview()`가 activeSelector가 여전히 준비 전
값이면(=promote 안 됨) 그 preview만 abort하고 복원을 재확인한다.
이미 promote됐으면(activeSelector가 전환됨) 손대지 않는다(§35.8).

### 5.5 판정·조치 필드와 비동기 감사 필드의 전파 (2026-09-19 추가)

**배경**: `detected`/`detection_source`/`action`/`promotion_verified`/`t_audit_*`/`commit_sha`는
스키마에만 있고 `run_once.py`가 채우는 코드가 없어, 실제 promotion이 검증까지 된
`proposed` 파일럿(§36)의 결과 JSON에도 기본값(`detected=false`, `action="none"`)으로만
남았다. 아래 원칙으로 전파를 고정한다.

**1. 판정·조치 필드의 authoritative source는 recovery-policy다.** `process_signal()`이
`_current_experiment`(§5.2)에 동기로 기록한다(스레드풀에서 동시 처리될 수 있어 락 안에서):
- `t_detection` + `detection_source` + `detector`: 현재 run에 속하는 유효 신호가
  idempotency/stale/run_id 검사를 **처음 통과한 순간** 한 번에 확정하고 **절대 덮어쓰지
  않는다**(중복·후속 신호 무관).
- `action`/`decision_outcome`/`idempotency_key`: primary 판정(§5.6과 같은 우선순위 -
  실행된 조치 `executed_verified` > `executed_unverified` > 최초 유효 탐지의 판정)만
  기록한다. `skipped_duplicate`는 절대 primary가 될 수 없고, 같은 등급이면 먼저
  기록된 것이 유지된다. 즉 실제로 실행된 조치는 observe-only·skip 기록보다 우선하지만
  (예: 예측 경로가 observe-only로 첫 탐지한 뒤 반응 경로가 promotion을 실행하면
  `detection_source=predictive`, `action=promote_preview`), 실행된 조치 뒤의
  observe-only/cooldown-skip 기록이 그것을 내리지 못한다.
- `detected`(=`t_detection` 존재)와 `promotion_verified`(=`decision_outcome`에서 파생)는
  저장하지 않고 조회 시 계산한다 - 원천을 하나로 유지한다.
- 등록 요청 본문의 판정·조치 필드는 무시하고(식별 필드만 옮김), 같은 `run_id` 재등록은
  이미 기록된 상태를 지우지 않는다(진짜 idempotent).

**2. 회수.** `run_once()`는 trial `finally`에서 **context clear 전에** §5.2 엔드포인트를
읽어 `TrialResult`에 기록한다(`judgment_source="live_state"`). 조회 실패·`run_id` 불일치는
timing과 동일하게 `invalid_run`이고 `judgment_source`는 null로 남는다(권위 없는 기본값
표시). 탐지는 됐는데 판정이 아직 없는 순간(recovery-policy가 `promote()` 진행 중)에
trial이 끝났다면 확정될 때까지 최대 10초 기다린다 - 그래도 미확정이면 추측하지 않고
`notes`에 남긴다. native는 recovery-policy를 조회하지 않고 `detected=false`,
`action="none"`, 나머지는 전부 null이다(§1).

**3. 비동기 감사 필드는 정책 결과와 분리한다.** 원천은 recovery-policy의 기존
outbox/audit 상태이고(읽기 전용 `GET /admin/audit/{run_id}` = audit-log 레코드 + outbox
전송 상태 조인), recovery 실행 경로는 Git 완료를 기다리지 않는다(`git_client.enqueue`는
파일 기록+큐잉만). `run_once()`는 cleanup·context clear가 끝난 **뒤** 짧은 bounded wait
(기본 20초, 2초 간격)로 primary 감사기록(§5.6)이 authoritative 판정(`idempotency_key`/
`decision_outcome`)과 일치하는 채로 push까지 끝나길 기다린다. 못 끝나면 `audit_status=
pending|failed` + `audit_status_reason`을 남기고 `t_audit_push`/`commit_sha`는 null을
유지한다 - **Git 지연·실패·감사 조회 실패는 `outcome`이나 실제 `action`을 바꾸지
않고 `invalid_run`/`HarnessCorrupted`로도 번지지 않는다.** 판정이 없으면 감사 조회 없이
`not_applicable`. 그때 못 끝낸 것은 나중에 `reconcile_audit.py`로 채운다.

**4. `reconcile_audit.py`(idempotent).** 감사·판정 필드와 provenance만 건드리고
타임스탬프·`outcome`·`state`는 절대 수정하지 않는다(`collect_metrics.py`의 "원본 수정 금지"
원칙의 유일한 공인 예외). 첫 수정 전에 원본을 `*.pre-reconcile.bak`으로 한 번만 보존하고
(`.gitignore` 대상), 같은 입력으로 다시 돌리면 파일을 다시 쓰지 않는다. `judgment_source=
live_state`인 trial의 판정 필드는 덮어쓰지 않는다(감사 필드만 갱신). 판정 필드가 기록되기
전의 과거 trial은 primary 감사기록으로 보완하고 `judgment_source="audit_reconcile"` +
`reconciliation`(제외된 기록·**보완 전 원래 값**·추론한 값의 출처·재조정 시각)을 남긴다.
`t_detection`은 있는데 귀속 가능한 감사기록이 없으면 판정 필드를 추측해 채우지 않는다.
`detector`는 2026-09-19 이전 감사기록의 `evidence`에 없다 - 그 경우 **pilot(`is_pilot=true`)에
한해**(2026-09-19 승인, 과거 proposed 파일럿 1건) trial의 `detector_process`(arm 배선값)로 채우고
`reconciliation.inferred_fields`에 그 사실을 반드시 명시한다. **앞으로 생성되는 본 실험
(`is_pilot=false`) 데이터에는 detector 추론을 허용하지 않는다** - live 경로는 recovery-policy
상태의 값만 쓰고, 재조정 도구도 본 실험 데이터의 detector를 추론하지 않고 null로 남긴다.

**4-1. 파이프라인 timing 4종(2026-09-19 확정).** `t_detection`(유효 신호 최초 수락) -> `t_decision`
(정책이 action을 확정한 직후, **observe-only여도 기록**) -> `t_api_request`(실제 promotion 호출 직전) ->
`t_switch`(promotion 후 active selector 검증이 처음 성공한 시각)는 전부 recovery-policy가
`process_signal()`에서 서버 시각으로 동기 기록하고 첫 값만 유지한다(중복·후속 신호로 덮어쓰지
않음). promotion이 없으면 `t_api_request`/`t_switch`는 null이고, `t_switch`는 selector 검증이 성공한
promotion(`promotion_verified=true`)에서만 있다(`rollouts_client.promote()`의 `verified_at` -
`promote()` 반환 후가 아니라 검증이 처음 성공한 그 순간). promotion 경로에서는 `t_detection <=
t_decision <= t_api_request <= t_switch`가 구조적으로 성립하며 `collect_metrics.py`가 검증한다.
`judgment_source=live_state`인 trial은 탐지했으면 `t_decision`이, 검증된 promotion이면
`t_api_request`/`t_switch`가 반드시 있어야 한다. **이 필드들이 채워지기 시작하기 전의 trial(과거
proposed 파일럿 등)의 `t_decision`/`t_switch`는 추정으로 채우지 않고 null로 보존한다** -
`reconcile_audit.py`도 이 두 필드는 건드리지 않는다(감사기록의 `decided_at`은 promotion 실행
**후**에 찍히므로 `t_decision`의 근거가 될 수 없다).

**5. `collect_metrics.py`.** 새 필드를 comparison 행에 싣고(arm↔detector 대조는 §5.7), 모순을 issue로 남긴다:
non-native에서 `t_detection`은 있는데 `detected`가 true가 아님(또는 그 반대), `action=
promote_preview`인데 `t_api_request` 또는 `promotion_verified`가 없음. 비동기 감사
미완료(`audit_status=pending|failed`, 또는 판정 필드 전파 이전의 과거 promotion trial)는
**timing anomaly가 아니라** `audit_pending` 컬럼과 별도 issue로 표시한다 -
`promotion_verified=true`여도 마찬가지다.

### 5.6 primary 감사기록 선택 규칙 (2026-09-19 고정)

한 trial(`run_id`)의 감사기록(`audit-log/{run_id}.jsonl`)이 여러 개일 수 있다(예: 실제
조치 기록 + 이후 중복 신호의 `skipped_duplicate`, 또는 예측·반응 경로가 각각 남긴 기록).
결과 row의 `audit_record_id`/`commit_sha`/`t_audit_*`가 가리키는 **primary** 기록은 다음 순서로
고른다(`reconcile_audit.select_records()`가 유일한 구현이며 `run_once()`와 CLI가 공유):

1. **`skipped_duplicate`는 primary가 될 수 없다.** 제외하되 지우지 않고
   `reconciliation.excluded_records`에 사유와 함께 보존한다.
2. **run_id 귀속 근거가 있는 기록만 자격이 있다(2026-09-19 승인).** 근거는 신호 경로(`signal_source`)별로
   정해져 있고 **서로 대체되지 않는다**. 예측 경로(`anomaly`) 기록은 `idempotency_key`가
   정확히 `"{run_id}:"` 접두어를 가져야 한다 - 접두어+콜론으로 비교하므로 `run-1`이
   `run-11`의 기록을 가져가지 않는다. 반응 경로(`alertmanager`)의 key는
   `{fingerprint}:{startsAt}`라 run_id를 담을 수 없으므로(key에 run_id 포함을 문자 그대로 적용하면
   반응 경로 기록이 전부 탈락한다), recovery-policy가 2026-09-19부터 감사기록
   `evidence.experiment_run_id`에 귀속 근거를 남기고(`evidence.detector`도 함께 - 예전엔
   detector 태그가 감사기록 어디에도 안 남았다) 이 값이 현재 `run_id`와 **정확히 일치**해야 자격을
   인정한다. 예측 기록이 evidence만 맞는 경우(예: run_id 없이 보낸 예측 신호가 ambient로 태깅된
   경우 - 이 trial의 detector 프로세스가 보낸 신호가 아니다)나 반응 기록이 key 접두어만 맞는
   경우, 경로를 알 수 없는 기록, 어느 근거도 없는 기록(다른 run, 2026-09-19 이전의 근거 없는
   반응 기록)은 **primary 후보에서 제외**(fail-closed)하고 사유를 남긴다.
3. **우선순위: `executed_verified` > `executed_unverified` > 최초 유효 탐지의 decision 기록.**
   조치 기록이 없으면 자격 있는 기록 중 audit-log 순서(=recovery-policy 처리 순서)로 가장 먼저인
   기록이 primary다. 같은 등급이 여럿이면 먼저 기록된 것.
4. `detection_source`/`detector`는 primary가 아니라 **최초 유효 탐지 기록**(자격 있는 첫
   기록)의 것이다 - 둘이 다를 수 있다(§5.5 예시).
5. live 경로에서는 primary가 authoritative 상태의 `idempotency_key`/`decision_outcome`과
   일치할 때만 `complete`로 인정한다 - 다른 기록이 이미 push됐어도 이 판정의 감사기록이
   아직 안 생겼으면 `pending`이다.

### 5.7 arm별 기대 detector와 Alertmanager fallback 예외 (2026-09-19 추가)

`detector`(§5 스키마 - 최초 유효 탐지의 **실제** source)가 arm이 띄우기로 한 detector와 다르면 그
trial은 잘못된 detector(예: 정리 안 된 이전 arm의 프로세스)가 신호를 낸 것일 수 있다. `collect_metrics.py`의
`_check_detector_consistency()`가 이를 validation issue로 검출하고 결과를 comparison의
`detector_check` 컬럼에 남긴다.

| arm | 예측 경로(`detection_source=predictive`) 기대 `detector` | 탐지 없음 |
|---|---|---|
| `native` | (탐지 자체가 없음 - `detector`는 항상 null) | null |
| `fixed_threshold` | `fixed_threshold` | null |
| `proposed` | `isolation_forest` | null |

**Alertmanager fallback 예외**: §1대로 `fixed_threshold`/`proposed`는 공통 Alertmanager 반응형
fallback을 함께 가지므로, 최초 유효 탐지가 그 fallback이면 `detection_source=reactive`,
`detector=alertmanager`가 **정의된 예외로 허용**된다(`detector_check=reactive_fallback`, 오류 아님).
반응 경로인데 다른 detector 이름이거나, 예측 경로인데 `alertmanager`이면 예외가 아니라 불일치다.
`native`가 `detector`를 가지면 불일치(recovery-policy 미개입 - §1).

`detector_check` 값: `ok`(예측 경로가 arm과 일치, native의 null) \| `reactive_fallback`(정의된 예외) \|
`inferred_pilot`(아래) \| `not_applicable`(탐지 없음·검증 대상 아닌 arm) \| `mismatch`·`missing`(validation
issue - 불일치, 또는 예측 탐지인데 `detector`를 알 수 없음/`detection_source`가 predictive·reactive가 아님).

**과거 inferred pilot(§5.5)**: `reconciliation.inferred_fields`에 `detector`가 있고 `is_pilot=true`이며 추론값이
arm 기대와 일치하면 오류가 아니라 `inferred_pilot`으로 **별도 표시**한다(provenance가 있으므로). 추론값이 arm과
어긋나면 여전히 불일치다. **본 실험(`is_pilot=false`) 데이터에 추론된 detector가 있으면 허용되지 않아 불일치**로
검출한다(재조정 도구가 본 실험 데이터의 detector를 추론하지 않으므로 정상 경로에서는 나타나지 않고,
나타나면 절차 위반이다).

### 5.8 network_tolerant profile의 probe 실패 분류 - `transition_straddling` (2026-09-20 추가)

`network_degrade`를 `network_tolerant` profile(readiness/liveness `timeoutSeconds` = calibration 확정값 11초)로 돌릴 때 kubelet의
`Readiness/Liveness probe failed` 이벤트를 NetworkChaos CR 단계 타임라인(생성·`AllInjected=True` 확인·**삭제 요청**·소멸)에 대해 아래처럼
분류한다. 이 분류는 분석·판정 코드(`calibrate_network_tolerant_probe.py`의 `classify_occurrence`/`judge_v2`, `trial_observer.py`의
`analyze`)와 문서에만 있고 **`TrialResult`에는 새 필드가 없다**(스키마 동결 유지).

**원칙: 이벤트 시각만 보고 `teardown`으로 단정하지 않는다.** probe의 **추정 실행 구간**을 쓴다 - timeout 유형 실패(`Client.Timeout exceeded`·
`context deadline exceeded`·`i/o timeout`)는 `[이벤트 시각 - timeoutSeconds, 이벤트 시각]`, 그 밖의 실패(연결 거부 등)는 이벤트 시각 한 점이다.
이벤트 시각은 worker 시계 - 오프셋 + 0.5초(§44.1).

| 분류 | 조건 (삭제 요청 시각 = d) |
|---|---|
| `steady` | 이벤트가 d 이전(d +-1초 안이면 보수적으로 steady) - **중단 조건** |
| `transition_straddling` | 이벤트가 d 이후 teardown 구간(삭제 요청 ~ 소멸 확인 + 15초)이고 추정 실행 구간의 시작이 d + 1초보다 이르다(= d를 **가로지름**) |
| 순수 `teardown` | 같은 teardown 구간인데 추정 시작이 d + 1초 이후(삭제 뒤에 시작한 probe) |
| 그 밖 | ramp·stage 사이·post_teardown·기동, **target pod 자신의 종료가 시작된 뒤**(pod 삭제 요청·kubelet `Killing`·promotion 뒤 Argo scale-down)의 종료 아티팩트(`shutdown` - 기록만, 판정 제외) |

`transition_straddling`은 **steady 실패에도 순수 teardown 실패에도 포함하지 않고 별도 집계**하며, 무시하지 않고 최종 표에 **횟수·Ready 전이·
Endpoint 영향·restart 여부**를 함께 표시한다.

**판정**: 단발(같은 전이 구간에 같은 종류 1건, 같은 종류의 다른 실패와 15초 초과 간격)이고 Ready=False·Endpoint 제거·restart 어느 쪽에도
영향이 없으면 network-tolerant profile 실패로 판정하지 않는다(기록·보고만). **연속 실패**(같은 전이 구간 2건 이상 또는 15초 이내),
**Ready=False**(폴링 사이 순간 전이 포함), **Endpoint 제거**, **restart·UID 변경(교체)** 중 하나로 이어지면 해당 trial은 실패다. Endpoint 영향은
target pod가 Service 뒤에 있으면 Endpoints 객체로 직접 확인하고(promotion으로 selector가 바뀐 경우는 제외), Service 뒤가 아닌 격리
calibration pod는 "Ready 전이 0 = Endpoint 유지"로 갈음한다(Endpoints 컨트롤러는 Ready pod만 등록한다).

**소급 적용**: calibration 두 회차(§45)의 stage-4 readiness 실패는 원본 JSON을 그대로 두고 이 정의로 재분류했다 - 둘 다 `transition_straddling`(추정
probe 시작이 삭제 요청 -5.8초/-7.6초), Ready 전이·Endpoint 영향·restart 없음 -> profile 실패 아님(상세 `phase8-blue-green-preflight-incident.md` §46).

### 5.9 계획된 promotion과 target 변경의 해석 (2026-09-20 추가)

`target_replaced`(§5)는 "주입이 효과를 낸 뒤 active target이 바뀐 것을 어댑터가 관측했는가"일 뿐 **왜** 바뀌었는지는 말하지 않는다. `fixed_threshold`/`proposed`
arm에서는 정책이 promotion(장애 pod에서 준비된 정상 pod로 트래픽 이탈)을 실행하면 active selector가 바뀌어 target이 바뀌는데, 이것은 재시작 연쇄나 probe 격리
실패가 아니라 **실험 처치 자체**다. `network_degrade` 3-arm 파일럿의 `proposed`가 실제로 이 경우였다(`target_replaced=true`, 검증된 promotion 뒤). 그런데 예전
파생 해석은 `target_replaced` 하나만 봐서 이를 `probe_isolation_held=false`로 오판정했다 - `test_collect_metrics_promotion.py`가 실제 파일럿 JSON 3건으로 이를 고정한다.

`collect_metrics.py`는 `TrialResult`와 원본 JSON을 건드리지 않고(**새 필드 없음**) 분석 산출물 `comparison.csv`에 파생 열 `target_change_kind`/`target_change_reason`을
만든다. `readiness_probe_profile`이 `default`/`network_tolerant`가 아니거나(다른 시나리오·옛 결과) `target_replaced` 필드가 없으면 `not_applicable`이고, 나머지는:

| `target_change_kind` | 조건 | `probe_isolation_held` (`network_tolerant`) | `restart_chain_observed` (`default`) |
|---|---|---|---|
| `none` | `target_replaced=false` | true | false |
| `planned_promotion` | **전부**: `target_replaced=true`, `action=promote_preview`, `promotion_verified=true`, `t_api_request`·`t_switch` 존재·`t_api_request <= t_switch`, `t_target_replaced >= t_api_request`, 교체 pod name/uid 존재, 아래 pod 증거 없음 | true (교체만으로 격리 실패라 하지 않음) | false |
| `unplanned` | (a) `target_replaced=true`인데 promotion 활동이 전혀 없음(`action`≠`promote_preview`이고 `promotion_verified`·`t_api_request`·`t_switch`도 없음), (b) promotion 정보는 완결인데 `t_target_replaced < t_api_request`(promotion 요청보다 앞선 교체 - promotion으로 설명 불가), (c) 아래 **pod 증거** 존재 | false | true |
| `indeterminate` | promotion 활동은 있는데 정보가 불완전·모순: `promotion_verified`≠true, `action`≠`promote_preview`, `t_api_request`/`t_switch` 누락 또는 `t_switch < t_api_request`, `t_target_replaced` 누락, 교체 pod name/uid 누락 | **null** (추정 안 함) | **null** (추정 안 함) |

`indeterminate`는 True/False로 추정하지 않고 validation issue(`field=target_replaced`)로 드러낸다. `network_tolerant`에서 교체가 있는데 `outcome=prevented`로만 남으면
"설정이 열화를 견뎠다"로 오해할 수 있어 남기는 별도 issue(`_check_tolerant_profile_prevented_misleading`)는 `unplanned`일 때만 발동한다.

**pod 증거는 promotion과 별도**: pod restart·UID 교체·promotion 전 target 소멸의 증거(관찰 결과를 사람이 옮겨 적은 `--pod-evidence` JSON `{run_id: {restarts, uid_replaced,
target_lost_before_promotion}}`)가 있으면 promotion이 검증돼 있어도 promotion으로 가리지 않고 `unplanned`다. 어댑터가 못 본 교체(`target_replaced=false`)에 증거가 있으면
"관측하지 못한 교체" issue를 남긴다. 증거 파일이 없으면 어댑터 필드만으로 판단한다.

**한계**: 어댑터는 stage 경계에서만 대상을 다시 조회하므로 `t_target_replaced`는 교체가 일어난 시각이 아니라 **처음 관측한 시각**이다. "promotion 요청 뒤에 관측됐다"는 것은
promotion이 만들 수 있는 변경이라는 뜻이지 그 이전에 재시작이 없었다는 증명이 아니다 - 그래서 pod 증거가 있으면 항상 우선한다.

### 5.10 arm별 주입 노출 차이의 해석 - 동결 (2026-09-20 추가)

`network_degrade`의 세 arm은 **같은 stage schedule**(500ms/1000ms/2000ms/4000ms, 각 90초)로 시작하지만, 조치하는 arm(`fixed_threshold`/`proposed`)은 promotion 뒤
장애 pod가 더 이상 트래픽을 받지 않아 arm마다 실제로 받는 주입 노출이 달라진다. 이는 결함이 아니라 처치의 결과이므로 해석을 **본 실험 전에 동결**한다. 파일럿 n=1의 실제
모양(`phase8-blue-green-preflight-incident.md` §46): `proposed`는 stage 1 중 promotion(`t_api_request` 주입 관측 +59.5초) -> stage 1 경계(+90.7초)에서 교체를 관측해
stage 2~4를 만들지 않았고, `fixed_threshold`는 stage 4 중 promotion(+324.5초)이라 만들 stage가 남아 있지 않았으며(`target_replaced=false`), `native`는 조치 없이 4 stage 전부를 받았다.

1. **동일 출발**: 모든 arm은 같은 stage schedule로 시작한다. arm은 injector 동작을 바꾸지 않는다(detector·preview 배선만 다르다 - §1).
2. **promotion = 처치 자체**: promotion은 장애 pod에서 준비된 정상 pod로 트래픽을 이탈시키는 실험 처치 그 자체다. 주입을 멈추는 것이 아니다 - 진행 중인 NetworkChaos는 옛 pod에 그대로 남고 트래픽만 그 pod를 떠난다.
3. **현재 injector 동작 유지**: 검증된 promotion 뒤 원래 target이 active가 아니게 되면 injector는 다음 stage 경계에서 이를 관측하고(`_check_target()`) 남은 stage를 만들지 않는다. 이 동작을 **바꾸지 않는다**(진행 중이던 stage의 CR은 그 지속시간까지 옛 pod에 남았다가 경계에서 삭제된다).
4. **`treatment-induced truncation`**: 그 뒤 stage들은 실패나 누락이 아니라 처치가 만든 truncation이다. `target_change_kind=planned_promotion`(§5.9)인 trial이 truncation이 일어난 trial이며, 마지막 stage 중·뒤에 promotion이 일어나 `target_replaced=false`이면 truncation은 없다(만들 stage가 남아 있지 않았다). truncation은 `invalid_run`이나 재실행 사유가 아니다.
5. **직접 비교 금지**: promotion 이후의 stage latency와 전체 누적 노출량(만들어진 stage 수·총 지연 노출 시간)을 arm 간 직접 비교하지 않는다.
6. **주 비교 지표**: `t_detection`, `t_decision`, `t_api_request`, `t_switch`, `t_recovery`, `outcome`, `action_stage`(`t_api_request`가 속한 실제 stage). 전부 기존 필드이고 **새 `TrialResult` 필드는 없다**.
7. **stage별 SLO 곡선은 공통 노출 구간에서만**: stage별 SLO 곡선(raw probe 표본을 stage로 나눈 P95·성공률 등)의 arm 간 비교는 **action 이전의 공통 노출 구간**에서만 한다 - 각 trial의 `t_injection_observed`를 0으로 둔 상대 시각에서 `[0, 비교 대상 arm들의 상대 t_api_request 중 최솟값]`(조치가 없는 arm은 상한을 만들지 않는다). 이 구간은 가장 빠른 조치가 정하므로 첫 stage의 일부에 그칠 수 있고, 그러면 곡선 비교는 그 범위까지만 한다. stage 경계는 `slo_stage`/`detection_stage`/`action_stage`가 알려주는 실제 창을 쓰고, 명목 일정(90초 단위)으로 근사하면 그렇게 표시한다(§5.1과 같은 원칙).
8. **dose 동일시 금지**: native·미조치 arm이 받은 전체 stage 노출과 조치 arm의 짧은 노출을 **같은 dose로 해석하지 않는다** - 처치가 노출을 바꾼 결과로 보고하고, 노출이 긴 쪽이 "더 나쁘다/더 잘 견뎠다"로 읽지 않는다.
9. **실행 순서 균형화**: 본 실험에서 arm 실행 순서를 균형화해 시간대·캐시 효과를 줄인다(§7).

이 해석은 분석 규칙이며 `TrialResult`·injector 동작을 바꾸지 않는다. 다만 6번의 `action_stage`가 `network_degrade`에서 채워지도록 어댑터에 실제 stage 창 기록(`classify_stage`)을 추가했다(§5 스키마 `slo_stage` 행 - 새 필드가 아니라 기존 선택 훅·기존 필드).

## 6. 안전장치 — `run_once()`가 매 trial마다 반드시 함

- 이전 trial의 firing 상태 Alertmanager 알림이 다음 `run_id`로 새지 않도록, trial 사이 **quiescence 대기**(모든 알림이 resolved 상태가 될 때까지) — §4 trial 종료조건③과 동일 개념
- `safety.py`의 idempotency(`state/safety_state.json`)·cooldown을 매 trial 시작 전 명시적으로 초기화
- trial 시작 전 매번: preview Ready 상태 확인 + active/preview selector가 분리(다름)돼 있는지 확인(`native`는 preview 자체를 안 만듦)
- probe가 trial 도중 죽거나 비정상 응답을 내면 `probe_valid=false` → `invalid_run` 처리 — arm의 실패로 안 셈
- 장애 주입이 실제로 대상에 적용됐는지 확인 못 하면 `injection_valid=false` → `invalid_run`
- `fixed_threshold.py`의 임계치(CPU>90%)와 `anomaly-detection/artifacts/model.pkl`은 파일럿 이후, 본 실험 전에 **동결**한다. 본 실험 결과를 본 뒤에는 절대 재학습·재조정하지 않는다.

## 7. 실행 순서 — arm을 섞어서 수행

60회를 arm별로 몰아서 돌리지 않고 **섞어서(interleaved)** 수행한다 — 특정 arm이 특정 시간대(클러스터 상태 drift, 캐시 워밍 등)에 몰리는 걸 방지. 시나리오별로 5회×3arm=15회 블록 안에서 arm 순서를 `order_seed`로 셔플하고, 그 시드와 결과 순서(`sequence_index`)를 결과 스키마에 남겨 재현 가능하게 한다.

**균형화 (2026-09-20 동결, §5.10 9번)**: 완전 무작위 셔플은 시드에 따라 한 arm이 계속 앞이나 뒤에 몰릴 수 있다(예: `native`가 5번 모두 묶음의 첫 실행). 그래서 시나리오별 15회를 rep마다 arm 3종이 정확히 1번씩 든
5개 묶음으로 나누고, 묶음 안 순서는 `order_seed`로 재현 가능하게 정하되 5개 묶음에 걸쳐 **각 arm이 각 위치(1·2·3번째)에 오는 횟수의 최댓값-최솟값이 1 이하**(= 1~2회)가 되게 한다. 이 순서를 만드는
`run_all_scenarios.py`는 **아직 구현되지 않았고** 이 균형 조건을 여러 시드에서 검증하는 오프라인 테스트가 그 구현의 일부다(순서를 손으로 넘기는 러너 CLI의 `--sequence-index`/`--order-seed`는 균형을 보장하지 않는다).

## 변경 이력

- 2026-09-16: 최초 확정.
- 2026-09-16: 1차 리뷰 반영 — (1) `fixed_threshold`에도 공통 Alertmanager fallback 추가(비교 타당성 문제), (2) `outcome`에 `prevented` 추가하고 3조건 명시, (3) trial 종료조건을 "chaos 종료+안정화 확인"으로 재정의(조기종료 아님, 예산은 기본 실행시간), (4) 스키마에 `t_run_start/end`·`t_injection_end`·`detection_source`·`action`·`injection_valid`·`probe_valid`·`invalid_reason`·`sequence_index`·`order_seed`·`commit_sha` 추가 + 비동기 감사기록 reconcile 단계 명시. 총 예상 소요시간 11시간15분→13~15시간으로 수정.
- 2026-09-16: load-ramp 파일럿에서 개별 요청 latency는 정상화됐으나, 60초 롤링 P95와 30초 연속 정상 판정에 필요한 관찰시간이 부족해 회복 여부가 우측 검열됨을 확인했다. 본 실험 전 timeout을 600초에서 900초로 조정했다. 탐지 방식에 유리하도록 변경한 것이 아니라 모든 arm의 회복 여부를 동일한 기준으로 끝까지 관찰하기 위한 변경이다. 이 조정 전에 실행된 native 파일럿(run_id=`load_ramp-native-01-20260916T033817Z`)은 파이프라인 검증에는 성공했으나 결과 데이터는 본 실험 5회 반복에서 제외한다. 총 예상 소요시간 13~15시간→14~16시간으로 수정.
- 2026-09-16: 파일럿에서 `max_tokens=10` probe가 vLLM CPU limit인 4코어를 거의 전부 사용해 측정 도구가 실험 대상에 유의미한 부하를 가하는 observer effect를 확인했다. 본 실험에서는 `max_tokens=1` 경량 probe를 사용하고, 요청 특성이 변경된 만큼 동일한 산정 원칙으로 baseline과 latency SLO를 다시 측정해 동결했다(SLO v2 — slo-definition.md 참고). `is_pilot`/`probe_profile`/`slo_version`/`latency_slo_sec`/`probe_rps`를 §5 스키마에 추가해 재현성을 높였다.
- 2026-09-16: probe 포함 조건에서 `load_ramp` stage RPS를 재보정했다(기존 1~10RPS는 probe 없이 ramp.py 단독으로 캘리브레이션된 값이라, probe 상시 동반 + SLO v2 하에서는 stage-1부터 이미 위반이었음). 0.10~1.00 RPS 구간을 사전에 고정한 판정 기준으로 3회 독립 반복해 재현성을 확인했다: 낮은 부하 구간(0.10/0.25 RPS)에서는 측정 노이즈에 따른 소폭 역전이 있었으나, 0.50 RPS 이후에는 부하 증가에 따른 지연 상승이 일관되게 나타났다. 0.50 RPS까지는 3회 모두 SLO를 준수했고, 0.75 RPS는 2/3회, 1.00 RPS는 3/3회 SLO를 위반했다. 모든 요청은 성공했으며 부하 종료 후 정상 범위로 회복했다. 사전 기준을 그대로 통과했으므로 추가 조정 없이 확정했다(사후 편향 방지). "시스템 붕괴 확인용" 6번째 stage는 의도적으로 넣지 않았다 - 이 실험은 한계까지 무너뜨리는 게 목적이 아니라 점진적 열화에서 선제탐지·복구시간을 비교하는 것이라, collapse를 넣으면 다른 실험이 된다. `t_injection`을 "inject() 호출 시각"에서 "첫 ramp 요청이 실제 전송된 시각"으로 더 정밀하게 정의하고 `load_ramp_adapter.py`에 반영했다.
- 2026-09-16: `pod_kill` 어댑터의 코드+오프라인 테스트를 완료했다(`pod_kill_adapter.py`, active Service selector 기반 동적 대상 탐지 + fail-closed + idempotent cleanup, 실클러스터 검증은 별도 native E2E 단계로 분리). 테스트 실행 경로를 오프라인/`live_cluster`로 명확히 나눴다 - `recovery-policy` 실제 연동을 확인하는 `test_run_once.py`의 테스트 2개(non-native arm 등록, 활성 context 차단)에 `@pytest.mark.live_cluster`를 부여하고 `conftest.py`가 `RUN_LIVE_TESTS=1`일 때만 실행하도록 기본 skip 처리했다 - 이제 `pytest` 기본 실행은 클러스터 없이 항상 전부 통과한다(실행법은 `experiments/README.md` "테스트" 절). 또한 `t_injection` 정의를 바로잡았다: `get_actual_injection_time()`이 돌려주는 값은 어댑터가 실측한 정확한 사건 발생 시각이 아니라 폴링으로 그 변화를 처음 관측한 시각이며, 관측 오차의 상한(`poll_interval_sec`)을 새 필드 `injection_observation_error_sec`으로 결과에 함께 남기도록 `run_once.py`를 수정했다 - 바로 위 항목의 "실제 전송된 시각"이라는 표현은 이 관측 기반 특성을 충분히 드러내지 못했다.
- 2026-09-17: `pod_kill` native 첫 파일럿 실행에서 `outcome=prevented`
  오판정을 발견했다 - 즉발 injector가 주입 직후 바로 `t_injection_end`를
  찍어, probe가 `slo_judge`의 60초 warmup을 넘기지 못한 채(표본 0개) "위반
  없음"으로 조기 종료됐다(그 시점 replacement pod는 실제로 Not Ready였음).
  `run_once()`에 `min_observation_sec`(주입 효과 확인 직후부터 계산)과
  `Prober.is_slo_evaluable()`(NOT_EVALUABLE/COMPLIANT 구분) 게이트를
  추가했고, `load_ramp_adapter.py`의 `is_slo_evaluable()`은 전체 누적
  표본이 아니라 **주입 이후** 표본만으로 유효한 관측 창이 쌓였는지
  판정하도록 구현했다(`Prober.notify_injected()` 계약 추가). 결과 스키마에
  `min_observation_sec`·`slo_evaluable_at_exit` 추가, `collect_metrics.py`에
  "`arm=native`인데 `prevented`"·"본 실험 `prevented`인데
  `slo_evaluable_at_exit!=true`" 두 검증을 추가했다. 상세 경과는
  `docs/design/phase8-blue-green-preflight-incident.md` §6 참고.
- 2026-09-16: 바로 위 항목의 `injection_observation_error_sec` 계산 방식을 정정했다. `poll_interval_sec`(설정값)을 그대로 오차 상한으로 쓰는 건 부정확하다는 지적을 받았다 - `is_started()` 호출 자체(pod_kill의 K8s API 조회, load_ramp의 `kubectl exec`)의 실행시간과 스케줄링 지연이 `poll_interval_sec`을 넘을 수 있어 설정값만으로는 진짜 상한을 보장하지 못한다. `pod_kill_adapter.py`/`load_ramp_adapter.py`가 각각 "대상이 살아있음(또는 마커 없음)을 마지막으로 관측한 시각"과 "처음 사라짐(또는 마커 확인)을 관측한 시각"을 직접 실측해 그 차이를 `get_injection_observation_error_sec()`으로 넘기도록 `Injector` 계약에 새 선택 필드를 추가하고, `run_once.py`는 이 값을 그대로 기록하도록(더 이상 `poll_interval_sec`으로 대신 채우지 않도록) 수정했다. 두 비교 기준점이 모두 있어야만 값을 채우고, 없으면(예: 첫 poll에서 이미 상태가 바뀐 경우) `injection_observation_error_sec`은 null로 남긴다.
- 2026-09-17: `pod_kill × native` 파일럿(`run_id=pilot-pod_kill-native-01-20260917T145337Z`)으로 **`pod_kill native 경로 E2E 완료`** - 기존 Pod 소멸→SLO 위반→replacement Pod Ready→SLO 회복 전 흐름과 Node·kubelet/containerd 정상 동작을 실측 확인했다. 다만 `t_slo`(실패 확정 요청의 **전송** 시각 기준)가 `t_injection`(Pod 소멸 **관측** 시각)보다 0.5초 앞서는 사례를 발견했다 - 기능 실패는 아니지만(raw probe 로그로 원인 확인: 전송 뒤 2.54초 만에 실패 확정된 요청), 본 실험에서 `t_detection`↔`t_SLO` 비교 의미가 모호해질 수 있어 **본 실험 전 보완 필요** 항목으로 남긴다: (1) `t_injection`을 `t_injection_request`/`t_injection_last_seen`/`t_injection_observed` 구간으로 분리, (2) `t_slo`를 요청 전송(`sent_at`)이 아니라 완료(`sent_at + latency`) 시각 기준으로 재계산, (3) `collect_metrics.py`에 `temporally_ambiguous` 등 판정 로직 추가. 이번 파일럿의 `outcome=recovered`는 그대로 유효(기능 검증 통과), 정량 timing 값만 보완 전까지 참고용. 상세는 `docs/design/phase8-blue-green-preflight-incident.md` §7 참고.
- 2026-09-18: 바로 위 항목의 타임스탬프 재설계를 구현했다(실클러스터 작업 없음, 코드·테스트·문서만). §5 스키마에 `timing_schema_version`(`v2`=새 방식, 필드 없음/`v1`=옛 방식)·`t_injection_request`·`t_injection_last_seen`·`t_injection_observed` 추가, `t_injection`은 `t_injection_observed`의 하위호환 대표값으로 유지. `injection_observation_error_sec`은 3단계 우선순위(어댑터 직접 계산 > last_seen~observed > request~observed)로 계산해 `injection_valid=true`인 trial에서 더 이상 null로 남지 않는다. `slo_judge.py`의 `find_t_slo()`/`find_t_recovery()`는 이제 `sent_at`이 아니라 `observed_at`(=`sent_at+latency`)을 반환한다 - 윈도우 구성·P95·성공률·위반 여부 계산 자체는 그대로라 이미 확정된 load_ramp 5-stage 재현성 결론(위반 회수 등)에는 영향 없다. `collect_metrics.py`에 `temporal_relation`(`pre_injection`/`temporally_ambiguous`/`post_injection`/`unknown`) 판정과 주입 3시각 순서 모순 검증을 추가했다. `run_once.py`/`pod_kill_adapter.py`/`load_ramp_adapter.py`/`slo_judge.py`/`collect_metrics.py` 전부 오프라인 테스트 통과(신규 `test_slo_judge.py` 포함), v1 결과(신규 필드 없음)도 오류 없이 읽힘을 확인. 상세는 `docs/design/phase8-blue-green-preflight-incident.md` §7.4 참고.
- 2026-09-18: 완성도 점검 대응(실클러스터 작업 없음). `pod_kill_adapter.py`의 active pod 동적 탐지 로직을 `active_pod_resolver.py`로 추출해 공용화(기존 테스트 영향 없음 확인). `network_degrade_adapter.py`(신규)를 같은 패턴으로 구현 - Chaos Mesh Workflow 대신 NetworkChaos 4단계를 어댑터가 직접 순차 생성/삭제(status.conditions의 AllInjected 폴링, 문서 기반이라 실클러스터 미검증 명시). §5 스키마에 `readiness_probe_profile`/`readiness_probe_timeout_sec` 추가(network_degrade의 "기본 probe 연쇄장애 vs probe timeout 조정 순수 열화" 실험 분리용, 발견 5 대응). `gitops/apps/vllm-serving/overlays/network-tolerant/`에 probe timeoutSeconds만 patch하는 Kustomize overlay 신규 작성(base rollout.yaml은 무수정 유지) - patch 값(10초)은 미검증 후보로 명시, 확정엔 `calibrate_network_tolerant_probe.py`(신규)로 실클러스터 calibration 필요. `run_network_degrade_trial.py`(신규)는 실행 직전 active pod의 실제 probe timeoutSeconds를 읽어 요청한 profile과 다르면 fail-closed. 오프라인 스위트 64 passed, 2 skipped(live_cluster) - `test_network_degrade_adapter.py`(신규 5개) 포함, 자체 버그 1건 발견·수정. 상세는 `docs/design/phase8-blue-green-preflight-incident.md` §8 참고.
- 2026-09-18: 바로 위 커밋들에 대한 리뷰에서 방법론 문제가 지적됐다 - 주입 도중 active pod의 UID가 바뀌면 무조건 `invalid_run`으로 처리하던 것은, 네트워크 열화가 probe 실패->재시작(발견 5)으로 이어지는 것 자체가 실험의 관찰 대상이 될 수 있다는 점을 놓친 것이었다. "주입이 한 번도 효과를 내기 전"(외부 오염 가능성이 높음 - 여전히 `invalid_run`)과 "이미 효과를 낸 뒤"(실험 자체의 결과일 수 있음)를 구분하도록 `network_degrade_adapter.py`를 정정했다. §5 스키마에 `target_replaced`/`t_target_replaced`/`target_replacement_pod_name`/`target_replacement_pod_uid` 추가, `Injector`에 `get_target_replacement()` 선택 훅 추가. 대상 재확인은 이름 하나가 아니라 `get_active_pods_fn()`을 다시 불러 vllm-active selector가 실제로 지금 가리키는 pod을 다시 조회하는 방식으로 바꿔(교체 시 새 pod의 name/uid를 얻으려면 이 방식이 필요), 더 이상 안 쓰는 `get_pod_fn` 파라미터를 제거했다. 회귀 테스트 3개 추가(효과 전 변경=invalid, 효과 후 단일 교체=기록, 효과 후 모호한 전환(2개 동시 매칭)=역시 기록). 오프라인 스위트 69 passed, 2 skipped(live_cluster). 실클러스터 작업 없음.
- 2026-09-18: 바로 위 항목에서 추가한 필드들이 실제로 trial JSON -> `comparison.csv`까지 이어지는지 질문받아 `collect_metrics.py`의 `build_comparison()`을 직접 읽어 확인했다 - `TrialResult`에 필드를 추가하면 `asdict()`로 원본 JSON에는 자동으로 남지만, `comparison.csv`는 `build_comparison()`의 명시적 화이트리스트 dict라 새 필드를 안 넣으면 절대 안 나온다. 실제로 `readiness_probe_profile`/`readiness_probe_timeout_sec`/`target_replaced`/`t_target_replaced`/`target_replacement_pod_name`/`target_replacement_pod_uid` 6개가 전부 빠져 있었다 - 6개 모두 추가. 또한 `readiness_probe_profile`+`target_replaced` 조합을 해석하는 분석 전용 필드 2개를 새로 추가했다: `restart_chain_observed`(default profile에서 target_replaced 그대로 - 연쇄장애 자체가 관찰 대상), `probe_isolation_held`(network_tolerant profile에서 target_replaced의 반대 - 그 설정이 열화로부터 probe를 실제로 격리했는지). 어느 쪽도 `outcome`을 바꾸지 않는다(SLO 판정과 별개). tolerant profile에서 교체가 있었는데 `outcome=prevented`로만 남으면 "설정이 열화를 견뎠다"로 오해할 위험이 있어(위반이 안 잡힌 이유가 실제로는 pod이 바뀌어 무의미해진 측정일 수 있음) `_check_tolerant_profile_prevented_misleading()`으로 별도 issue도 남기게 했다(native+prevented 검증과 같은 패턴). 회귀 테스트 6개 추가(default/tolerant 조합 3가지, 오해소지 issue 검출, 신규 필드 없는 기존 결과의 하위호환, trial JSON 파일→comparison.csv 파일까지의 실제 왕복 확인). 오프라인 스위트 75 passed, 2 skipped(live_cluster). 실클러스터 작업 없음.
- 2026-09-18: `load_ramp × native` 파일럿(`pilot-load_ramp-native-01-20260918T141420Z`)을 유효한 `load_ramp native 경로 E2E PASS`로 확정(§29 baseline gate가 실클러스터에서 정상 동작함을 실측 확인, 위반 자체도 표본 충분·단일 전환점·stage3 진행 중 발생으로 근거가 명확 - 상세는 `docs/design/phase8-blue-green-preflight-incident.md` §30 참고). 3-arm 파일럿 전 stage 관측성을 보완했다(실클러스터 작업 없음, 코드·테스트·문서만) - `load_ramp_adapter.py`가 `ramp.py --summary-out`에 run별 고유 경로(`/ramp-summary-{run_id}.csv`)를 넘기고, `is_done()`이 정상 종료를 처음 확인한 직후 이 요약을 1회만 fetch해 `experiments/results/ramp-summary-{run_id}-{arm}-{rep}.csv`에 저장한다. 새 순수 함수 `_classify_timestamp_against_stages()`가 실제(명목 아닌) `stage_start_utc`/`stage_end_utc`로 timestamp를 stage 이름/`baseline`/`inter_stage_tail`/`drain`/`unknown` 중 하나로 분류하고, `Injector.classify_stage()`(선택 훅, 미구현 어댑터는 하위호환으로 무시됨) 경유로 `run_once()`가 `t_slo`→`slo_stage`, `t_detection`→`detection_stage`, `t_api_request`→`action_stage`를 채운다. 소스 timestamp가 null이면 대응 stage 필드도 null(`unknown`과 구분), summary fetch 실패나 `classify_stage()` 자체의 예외는 전부 삼켜 stage 필드만 `unknown`/`null`로 남기고 trial의 핵심 판정(`outcome`/`t_slo` 등)은 전혀 건드리지 않는다(§5.1 정책 신설). §5 스키마에 baseline 5개 필드(이전에 §29에서 추가했으나 이 표에는 누락돼 있던 것을 발견해 함께 보완)와 stage 3개 필드 추가, `collect_metrics.py`의 `build_comparison()`에도 반영. 회귀 테스트 12개 추가(`test_load_ramp_adapter.py` 7개 - 실제 지연된 stage 경계가 명목 경계보다 우선한다는 핵심 케이스 포함, `test_run_once.py` 4개, `test_collect_metrics.py` 1개). 기존 파일럿(`...141420Z`)의 "stage3" 표현은 명목 경계 추정이라는 점을 그대로 유지했고 원본값을 소급 생성하지 않았다(summary-out을 캡처 안 한 실행이라 실제 경계 데이터가 없음). 오프라인 스위트 136 passed, 2 deselected(live_cluster). 실클러스터 작업 없음 - 3-arm 파일럿은 아직 시작 안 함.
- 2026-09-18: `load_ramp native` E2E PASS 확정을 승인받은 뒤, 3-arm 파일럿(아직 미실행) 전 arm 오케스트레이션 완성 지시를 받았다 - `run_load_ramp_trial.py --arm fixed_threshold|proposed`가 지금까지 arm 이름만 결과에 태깅할 뿐 `fixed_threshold.py`/`score_server.py` 실행·종료나 preview 준비를 전혀 담당하지 않아, non-native arm을 그대로 돌리면 detector가 실제로 동작 안 한 채 잘못 라벨링된 결과가 생길 위험이 있었다. §1 arm 정의를 다시 확인(새로 추정 없음) - native만 detector·standby(preview) 둘 다 없고, fixed_threshold/proposed는 예측 모델만 다르고 나머지(recovery-policy 기동, 공통 Alertmanager 반응형 fallback, standby/promotion)는 동일. 신규 `experiments/arm_controller.py`가 이 매핑을 그대로 구현 - `make_detector_for_arm(arm, run_id)`가 native면 None, fixed_threshold/proposed면 각 스크립트를 `RECOVERY_POLICY_SIGNAL_URL` 환경변수(로컬 서브프로세스 실행 시 recovery-policy in-cluster DNS를 `localhost:8080`으로 덮어씀 - `score_server.py`에 `os.environ.get()` 기반으로 추가, 기존 값은 기본값으로 유지)로 로컬 서브프로세스로 띄우는 `Detector`(신규 `run_once.py` 프로토콜, start/is_alive/stop/name)를 반환한다. `wrap_injector_with_preview_prep()`이 기존 `experiments/blue_green_prep.py`의 `prepare_preview()`(이미 있던 코드, run_calibration.py가 먼저 쓰고 있었음)를 non-native arm의 `injector.prepare()` 앞에 배선 - 실패하면 `TrialInvalid`로 그대로 `invalid_run` 처리되고 `injector.inject()`는 호출되지 않는다(기존 PREPARING 단계 안전장치 재사용, 새로 안 만듦). `run_once()`는 `detector` 선택 인자를 받아 baseline 확보+context 등록이 끝난 뒤·주입 직전에 정확히 한 번 `start()`, OBSERVING 루프에서 `prober.is_alive()`와 나란히 `is_alive()` 확인(죽으면 invalid_run), finally에서 prober/injector보다 먼저 `stop()`+재확인(여전히 살아있으면 HarnessCorrupted). `TrialResult.detector_process`에 실제 배선된 detector 식별자를 기록(§5 스키마 반영). `run_load_ramp_trial.py`는 이 배선을 무조건 거치도록 수정해 non-native arm을 orchestration 없이 직접 실행할 수 없게 했다(fail-closed). 회귀 테스트 17개 추가(`test_arm_controller.py` 10개 - native=detector 없음/arm별 정확한 단일 detector·스크립트 dispatch/run_id 전파/서브프로세스 생명주기(trivial 커맨드로 검증, 실제 detector 스크립트는 Prometheus·모델 파일 의존이라 오프라인 대상 아님)/preview 준비 실패 시 injection 차단, `test_run_once.py` 7개 - baseline 확보 후에만 detector 시작·detector 시작 후에만 injection(실제 호출 순서 로그로 확인)·detector crash→invalid_run·예외/timeout 각각에서도 detector 정리·detector_process 필드 전파·detector 미지정 시 하위호환). 오프라인 스위트 153 passed, 2 deselected(live_cluster). 실클러스터 작업 없음 - 3-arm 파일럿은 아직 시작하지 않음. 상세는 `docs/design/phase8-blue-green-preflight-incident.md` §32 참고.
- 2026-09-19: arm 오케스트레이션(§32)은 승인받았으나, `t_detection`/`t_api_request`가 `run_once.py` 어디에도 채워지는 코드가 없다는 별도 gap이 지적돼(3-arm 파일럿 전 필수 선행 작업) 이를 완성했다. 구현 전에 recovery-policy의 예측 신호(`/signal`)·반응형 Alertmanager fallback(`/webhooks/alertmanager`)·정책 결정(`policy.decide()`)·promotion 호출(`rollouts_client.promote()`)·감사기록(`git_client.py`, 비동기) 경로를 코드로 직접 추적해 authoritative source를 확정했다 - Git 감사기록은 명시적으로 배제(비동기라 반환을 안 막게 설계됨), detector 프로세스 stdout도 배제. `recovery-policy/main.py`의 `_current_experiment`(기존 ambient 등록 메커니즘)에 두 필드를 추가하고, `process_signal()`이 idempotency 통과 직후(정책 판단 대상 확정 시각 = `t_detection`, 첫 값만 유지)와 promote() 호출 직전(`t_api_request`, 조치 없으면 null)에 각각 `datetime.now()`로 동기 기록한다 - 다른 run_id·stale alert는 기존 ambient 보정 로직이 이미 걸러내는 것에 더해 재확인. 신규 `GET /admin/experiment-run/timing` 조회 엔드포인트 추가(§5.2). `run_once.py`가 finally에서(context clear 전) 이를 읽어 `TrialResult`에 반영 - 응답 run_id 불일치나 조회 실패는 조용히 null로 남기지 않고 명시적 `invalid_run`(단, 이미 확정된 더 구체적인 invalid_reason은 보존, §5.2). 추가로 `arm_controller.make_detector_for_arm()`이 detector 서브프로세스를 실제로 띄우기 전 `RECOVERY_POLICY_SIGNAL_URL`(`/healthz`로 부작용 없이 확인) 도달성을 사전 확인해, 불가능하면 detector도 chaos 주입도 시작하지 않는다(fail-closed, §5.3). §31의 `slo_stage`/`detection_stage`/`action_stage` 계산 메커니즘은 코드 변경 없이 그대로 재사용됨을 통합 테스트로 확인(이제 실제 non-null 값으로 처음 검증됨). 회귀 테스트 17개 추가(`recovery-policy/test_main.py` 8개 - 예측/반응 신호 각각 t_detection 기록, 중복 신호 미덮어씀, 다른 run_id·stale alert 배제, 무조치 시 t_api_request null, 실제 promotion 시 `t_detection<=t_api_request`, context clear 후 다음 trial에 안 남음, 활성 실험 없으면 전부 null; `experiments/test_run_once.py` 6개 - 정상 회수, 조회 실패→invalid_run, run_id 불일치→invalid_run, 기존 invalid_reason 보존, native는 recovery-policy 완전 비접근, stage 필드 통합 계산; `experiments/test_arm_controller.py` 3개 - reachability 실패 시 fail-closed, 성공 시 정상 진행, 환경변수 우선순위). 오프라인 스위트 experiments/ 162 passed, recovery-policy/ 44 passed(각각 2 deselected/live_cluster 없음). 부수 발견(수정 안 함, 이번 범위 밖) - `recovery-policy/test_main.py`의 모듈 최상단 `patch(...).start()` 호출 2개가 `.stop()` 없이 남아있어, `test_git_client.py`와 같은 pytest 세션에서 함께 수집되면(`test_main.py`가 먼저 로드된 뒤) `git_client`의 실제 함수가 계속 mock으로 남아 `test_git_client.py` 3개가 실패한다 - `git stash`로 이번 세션 변경분을 전부 제거해도 동일하게 재현되는 것을 확인해 기존부터 있던 테스트 격리 결함임을 확정(각 파일을 단독 실행하면 전부 통과). 실클러스터 작업 없음 - 3-arm 파일럿은 아직 시작하지 않음.
- 2026-09-19: 위 항목의 "부수 발견"(test_main.py patch 누수)을 승인받은 authoritative source·전파 구조와 함께 수정 지시받아 완료했다. `test_main.py`의 module-level `patch(...).start()` 2개를 함수 스코프 autouse fixture(`with patch(...): yield`)로 교체 - 재현 확인 결과 원인이 두 겹이었다(①patch 미해제 ②`test_git_client.py`가 env var를 `git_client` import 전에 설정해야 하는데 다른 파일이 먼저 import하면 `sys.modules` 캐싱으로 무효화됨, pytest 기본 알파벳 수집 순서에서는 ②가 안 걸림). 수정 후 `pytest experiments recovery-policy -q -m "not live_cluster"` 206 passed(§34.1에 before/after 전체 기록). 변경된 recovery-policy를 워커 노드에서 재빌드(`docker build` → `docker save | ctr import`, 기존과 동일한 브리지 방식) 후 `kubectl rollout restart`로 실클러스터에 배포 - RESTARTS=0, `/healthz`·기존 admin API·신규 `GET /admin/experiment-run/timing` 전부 정상. 실제 chaos 없이 조치가 발생할 수 없는 조건(Rollout에 준비된 preview 없음을 사전 확인)에서 timing 신호 전파 smoke 7개 항목(등록 전 null/등록 후 올바른 조회/다른 run_id 배제/유효 신호 시 t_detection 기록/무조치 시 t_api_request null 유지/clear 후 제거/정리 확인) 전부 실측 통과, smoke 후 Node·Rollout·Chaos CR·context·양쪽 pod 재시작 0회까지 재확인. 상세는 `docs/design/phase8-blue-green-preflight-incident.md` §34 참고. 3-arm 파일럿(다음 지시 대상)은 아직 시작하지 않음.
- 2026-09-19: 3-arm 파일럿을 native → fixed_threshold 순으로 시작 - native는 전 항목 정상 통과(§34.6에서 이미 확정된 감사기록 귀속과 함께), `fixed_threshold` 01회는 preview가 180초 timeout보다 늦게(235초) Ready된 채 `invalid_run`으로 종료됐다(`wrap_injector_with_preview_prep()`의 fail-closed가 설계대로 작동해 chaos 주입·detector 시작 둘 다 없었음). `kubectl describe`+Prometheus 실측으로 근본원인을 확정/관찰/미확정으로 구분했다 - active pod·Node 자원 포화는 배제(확정), preview 자신의 CFS throttle 36회는 관찰됐으나 지연에 대한 정량적 기여는 미확정(§35.2). 방치된 preview/Paused Rollout은 `kubectl argo rollouts abort`와 동일한 효과(`status.abort=true`, `kubernetes` 파이썬 클라이언트로 직접 패치)로 수동 정리하고 active/Node 정상을 재확인했다(§35.3). 재발 방지로 두 가지를 구현했다 - (1) `wrap_injector_with_preview_prep()`에 자동 rollback 추가: 이번 호출이 만든 preview만 대상으로 삼고(activeSelector 사전 스냅샷으로 구분, 예상 밖 변경 시 fail-closed로 아무 것도 안 건드림), rollback 성공은 `TrialInvalid` 그대로, rollback 실패는 `HarnessCorrupted`로 승격(§5.4). (2) preview 준비 timeout을 180초→480초로 상향(§35.5, 기존 실측 176.1/350.3/163.7초 + 이번 235초 근거). `run_once.py`에 `except HarnessCorrupted` 절을 추가해 `injector.prepare()`가 직접 던진 경우도 기존 `critical_failures`→배치중단 경로(§6, action cooldown 초기화 실패와 동일 패턴)를 타도록 했다. 신규 `test_blue_green_prep.py`(6개) + `test_arm_controller.py`/`test_run_once.py` 추가 테스트, 통합 오프라인 스위트 218 passed, 2 deselected(live_cluster). 01회 invalid_run 결과 파일은 원본 그대로 보존하고 `included_in_main_analysis: false`만 추가했다. 상세는 `docs/design/phase8-blue-green-preflight-incident.md` §35 참고. `proposed`는 아직 실행하지 않음 - 클러스터 preflight 재확인 후 `fixed_threshold`를 새 run_id로 재실행할 예정.
- 2026-09-19: `fixed_threshold`를 새 run_id로 재실행해 `outcome=recovered`로 검증 완료(`t_slo`/`t_recovery`를 원본 raw CSV에 미수정 `slo_judge.py`로 독립 재검증해 기록값과 일치 확인). 검증 중 두 번째 gap을 실측 발견 - preview 준비가 성공했는데 detector가 promote를 안 하면(미탐지 등) trial 종료 후에도 아무도 정리하지 않아 Rollout이 2-revision으로 방치됐다. `blue_green_prep.cleanup_unpromoted_preview()`(신규)를 추가해 `wrap_injector_with_preview_prep()`이 `injector.cleanup`도 감싸도록 일반화 - activeSelector가 준비 전 값 그대로면(=미promote) 그 preview만 abort+복원 재확인, 이미 promote됐으면 손대지 않는다. 회귀 테스트 8개 추가, 오프라인 스위트 226 passed. 이어서 `proposed`를 새 run_id로 1회 실행 - `outcome=recovered`, **이번 세션 최초로 recovery-policy가 실제 promotion을 실행·검증까지 완료**(K8s 이벤트+git 감사기록의 CLI stdout으로 authoritative하게 확인, run_id 정확히 일치, 중복 후속 신호는 idempotency로 정상 skip). stage 분류 3개 전부 원본 stage-boundary CSV와 대조해 정확함을 확인. 이 과정에서 **`detected`/`action`/`promotion_verified`/`detection_source`/`t_audit_write`가 `run_once.py` 어디에도 대입되는 코드가 없어 항상 기본값으로만 남는다는 것**을 발견했다(§5 스키마 표에 경고 추가, §36.3) - 실제 promotion이 검증까지 됐는데도 trial JSON은 `detected=false`/`action="none"`/`promotion_verified=null`로 기록됨. 이번 trial의 SLO 판정 자체에는 영향 없으나(독립 계산), 60회 본 실험의 arm별 탐지율·조치율 비교가 이 필드들로는 불가능하므로 본 실험 전 authoritative source를 확정해 채우는 작업이 필요하다 - 이번 턴 범위 밖이라 코드는 고치지 않고 발견 사실만 기록·공유한다. 상세는 `docs/design/phase8-blue-green-preflight-incident.md` §35.8/§36 참고. `proposed` 1회로 3-arm 파일럿 전체 완료 - 60회 본 실험으로는 진행하지 않음(지시 대기).
- 2026-09-19: `load_ramp` 3-arm 파일럿의 기능 검증 완료(승인)에 이어, 위에서 발견한 판정·조치·감사 필드 미기록을 본 실험 전에 고쳤다(§5.5/§5.6, 상세는 `docs/design/phase8-blue-green-preflight-incident.md` §37). (1) recovery-policy를 판정·조치 필드의 authoritative source로 확정 - `ExperimentContext`에 `detection_source`(`predictive`/`reactive`)·`detector`·`action`·`decision_outcome`·`idempotency_key`를 락 안에서 기록(첫 탐지 정보는 중복·후속 신호로 덮어쓰지 않음, 실행된 조치가 observe-only·skip 기록보다 우선, `skipped_duplicate`는 primary 불가), `detected`·`promotion_verified`는 저장 없이 조회 시 파생. 같은 run_id 재등록이 기록된 상태를 지우던 잠재 결함도 수정하고 등록 요청 본문의 판정 필드는 무시. (2) `GET /admin/experiment-run/timing`(경로 유지, 상위 호환)이 위 필드를 함께 반환 - `run_once()`가 context clear 전에 회수해 `TrialResult`에 기록, 조회 실패·run_id 불일치는 기존과 동일하게 `invalid_run`, native는 기본값+null. (3) 비동기 감사 필드는 정책 결과와 분리 - 신규 읽기 전용 `GET /admin/audit/{run_id}`(audit-log + outbox 조인), trial 종료 후 bounded wait(20초)만 하고 미완료는 `audit_status=pending|failed`+사유·null 유지(outcome/action 불변), 신규 idempotent `experiments/reconcile_audit.py`로 나중에 재조정(원본 `.pre-reconcile.bak` 보존, provenance·보완 전 원래 값 기록). (4) 감사기록 evidence에 `experiment_run_id`/`detector`를 남기도록 변경(반응 경로 key엔 run_id가 없어 귀속 근거가 필요했고, detector 태그가 감사기록 어디에도 안 남던 문제도 해소) 후 primary 선택 규칙 고정(§5.6). (5) `collect_metrics.py`에 새 필드·모순 검출(non-native `t_detection`↔`detected`, promote 조치의 `t_api_request`/검증 누락)·`audit_pending`(timing anomaly와 분리) 추가. (6) `proposed` 파일럿은 재실행 없이 원본 timestamp·outcome을 보존한 채 감사기록 `16c8f08`(executed_verified)을 primary로, `39ace83`(skipped_duplicate)은 제외·보존으로 보완(`judgment_source=audit_reconcile`, `reconciliation`에 원래 값·제외 기록·추론 출처·재조정 시각). 회귀 테스트 50개 추가(`recovery-policy/test_main.py` 12, `test_reconcile_audit.py` 17, `test_run_once.py` 13, `test_collect_metrics.py` 8), 오프라인 스위트 276 passed(live_cluster 3개는 기본 deselect). 변경된 recovery-policy를 실클러스터에 배포하고(재시작 0회) 실제 `run_once()` 경로로 no-action live smoke(preview 없는 상태에서만 - promotion 불가) 통과. 실제 `load_ramp` 재실행·다른 시나리오 파일럿·60회 본 실험은 하지 않았다.
- 2026-09-19: 위 판정·감사 필드 전파 커밋(승인 완료: primary 귀속은 예측=`"{run_id}:"` 접두어·반응=`evidence.experiment_run_id` 정확 일치·경로별 근거는 서로 대체 불가·근거 없으면 후보 제외, detector 추론은 과거 pilot 한정 + `inferred_fields` 표시·본 실험 데이터엔 추론 불가)에 이어 마지막 필수 timing gap을 별도 커밋으로 처리했다(§5.2/§5.5 4-1, 상세는 `docs/design/phase8-blue-green-preflight-incident.md` §38). `t_decision` = 현재 run의 유효 신호에 대해 정책이 action을 확정한 직후의 recovery-policy 서버 시각(observe-only·조치 없는 판정도 기록), `t_switch` = promotion 후 active selector 검증이 처음 성공한 서버 시각(`rollouts_client.promote()`가 verify 루프 첫 성공 순간에 찍는 `verified_at`), `t_api_request`는 기존대로 실제 promotion 호출 직전. 셋 다 첫 값만 유지(중복·후속 신호로 덮어쓰지 않음), promotion이 없으면 `t_api_request`/`t_switch`는 null(검증 실패한 promotion도 `t_switch`는 null). `GET /admin/experiment-run/timing`(경로 유지)이 두 필드를 함께 반환하고 `run_once()`가 context clear 전에 회수해 `TrialResult`에 기록. `collect_metrics.py`는 `t_decision`/`t_api_request`/`t_switch` 컬럼을 노출하고(예전엔 comparison.csv에 이 셋이 없었다) 순서 `t_detection <= t_decision <= t_api_request <= t_switch`(기존 CAUSAL_CHAIN)에 더해 존재 규칙(promotion 없으면 `t_api_request`/`t_switch` null, `t_switch`는 검증된 promotion에만, `live_state` trial은 탐지 시 `t_decision`·검증된 promotion 시 `t_api_request`/`t_switch` 필수)을 검증한다. **기존 proposed 파일럿의 `t_decision`/`t_switch`는 추정해 채우지 않고 null로 보존**(`reconcile_audit.py`는 이 필드를 건드리지 않으며 `judgment_source=audit_reconcile` trial에는 존재 요구를 적용하지 않음). 회귀 테스트: `recovery-policy/test_main.py` 6·`test_rollouts_client.py`(신규) 3·`test_run_once.py`·`test_collect_metrics.py` 5. 오프라인 테스트까지만 수행 - **다음 실제 promotion 파일럿(`pod_kill` non-native)에서 live 검증**하며, 그 전에 변경된 recovery-policy 이미지를 재배포해야 한다(현재 배포 이미지는 `t_decision`/`t_switch` 이전 버전).
- 2026-09-19: `collect_metrics.py`에 arm↔실제 detector 불일치를 validation issue로 추가(별도 커밋, §5.7, 상세는 `docs/design/phase8-blue-green-preflight-incident.md` §39). native는 detector null, `fixed_threshold`는 `fixed_threshold`, `proposed`는 `isolation_forest`(예측 경로 탐지 기준). 최초 유효 탐지가 Alertmanager fallback이면 `detection_source=reactive` + `detector=alertmanager`를 계약에 정의된 예외로 허용(`detector_check=reactive_fallback`, 오류 아님). 과거 inferred pilot은 provenance(`reconciliation.inferred_fields.detector`)가 있고 `is_pilot=true`이며 arm과 일치하면 오류가 아니라 `inferred_pilot`으로 별도 표시하고, 본 실험 데이터의 추론된 detector·arm과 어긋나는 추론값은 오류. comparison에 `detector_check` 컬럼 추가. 보존된 실제 pilot 데이터에 적용해 확인: proposed pilot=`inferred_pilot`, fixed_threshold 2건=`not_applicable`(미탐지), 새 이슈 0건. 테스트 fixture의 낡은 값(`detection_source="isolation_forest"` - 2026-09-19 이전 "누가" 의미)을 새 의미(`predictive`)로 정정.
- 2026-09-19: `pod_kill × fixed_threshold` / `pod_kill × proposed` 파일럿 각 1회 완료(승인된 스키마 동결에 따라 **필드 추가 없음**, 상세·타임라인은 `docs/design/phase8-blue-green-preflight-incident.md` §40). 둘 다 `recovered`, invalid_run·HarnessCorrupted·cleanup 실패·Node 이상·결과 필드 모순 없음(중단 조건 미충족). 실측으로 확정된 것: (1) §38.3의 live 검증 - promotion이 실제 실행된 두 경로(fixed_threshold=반응형 fallback `alertmanager`, proposed=예측 `isolation_forest`) 모두에서 `t_detection <= t_decision <= t_api_request <= t_switch` 순서가 성립하고 `t_switch`가 감사기록의 `verified_at`과 같다. (2) proposed에서 예측 신호가 먼저 promotion을 실행한 뒤 도착한 반응 신호(`observe_only`)는 최초 탐지 정보를 덮어쓰지 않았고, primary는 실제 실행된 promotion 기록이 선택됐다(§5.6); 귀속은 예측=idempotency key `"{run_id}:"` 접두어, 반응=`evidence.experiment_run_id` 정확 일치로 각각 실측에서 작동했다. (3) 감사 `commit_sha`는 push 직후의 HEAD라 같은 batch의 레코드가 공유하며 "그 레코드를 트리에 포함한 push된 커밋"이지 레코드 자신의 커밋과 같다는 보장은 없다(`git show <commit_sha>:audit-log/<run_id>.jsonl`에 record_id가 있는지로 연결을 검증). **해석 주의(스키마 불변)**: pod_kill의 `target_replaced=false`는 어댑터가 `get_target_replacement`를 구현하지 않아 "미측정"이지 "교체 없음"이 아니다(실제로는 교체 pod가 생겼다) - 분석에서 근거로 쓰지 않는다. `fixed_threshold` detector(CPU>90%)는 대상 pod가 죽으면 CPU가 0이 되므로 pod_kill에서 구조적으로 발화할 수 없어 최초 탐지가 반응형 fallback이 되는 것이 정상이다. non-native trial의 준비 단계(preview 기동 중)마다 `VLLMTargetDown` 알림이 `adhoc` 감사기록(`observe_only`)으로 남으므로 감사 로그 분석에서 trial 밖 잡음으로 거른다. 요청→전환 검증(`t_api_request`→`t_switch`)은 두 번 모두 약 14.8~15.3초다. 발견·수정: `run_pod_kill_trial.py`가 non-native arm의 detector·preview 오케스트레이션을 우회하던 결함(`0ba88fa`, `test_run_trial_wiring.py`로 두 러너를 고정), 배포 이미지에 CRLF가 들어간 결함(`git archive` + `core.autocrlf=true`; 이후 `git -c core.autocrlf=false archive`와 `git show` blob 해시 대조를 배포 절차로 규정 - §40.2). 미해결 후속(이번 지시 범위 밖): `run_network_degrade_trial.py`에 같은 arm 배선이 없고, `test_network_degrade_adapter.py`의 3개 테스트가 실제 클러스터에 NetworkChaos CR을 만든다 - 둘 다 `network_degrade` 파일럿 전에 처리해야 한다(§40.1, §40.6).
- 2026-09-19: 바로 위 항목의 미해결 후속 2건과 CRLF 재발 방지를 처리했다(상세 `docs/design/phase8-blue-green-preflight-incident.md` §41 - **스키마 변경·새 필드 없음**, 실험·이미지 배포 없음): `run_network_degrade_trial.py`에 pod_kill 러너와 같은 arm 배선(detector·preview 준비/자동 rollback, `--rollout`/`--namespace`)을 추가하고 `test_run_trial_wiring.py`가 세 러너를 고정하며, 오프라인 테스트가 실제 클러스터에 접근하지 못하게 `experiments/conftest.py`에 `cluster_guard`를 추가했고(§40.6 정정: 실제 NetworkChaos CR 생성은 테스트 1개, 나머지는 존재하지 않는 CR에 대한 GET/DELETE), `.gitattributes`로 `recovery-policy/git_askpass.sh` 한 파일만 LF로 고정했다. 전체 오프라인 스위트는 존재하지 않는 KUBECONFIG에서 324 passed.
- 2026-09-19: `network_degrade`의 network_tolerant probe `timeoutSeconds`를 격리 calibration pod(운영 Rollout·Service·recovery-policy와 무관, NetworkChaos는 그 pod에만)에서 실측해 확정했다(상세 `docs/design/phase8-blue-green-preflight-incident.md` §42~§45 - **스키마 변경·새 필드 없음**, calibration은 pilot이라 본 분석에서 제외). 10초 후보 1회 측정(§43)은 사전 등록 판정 `MARGINAL`/권고 `NONE`이었고, 사전 등록한 **후보 11초 독립 2회**(§44, 도구 v2 - 이벤트 timestamp 구간 분류·즉시 중단·kubelet 카운터 교차검증)가 모두 `PASS`(stage-4 `/health` 최대 지연 8.72/8.64초, `T_min` 11, steady 실패·liveness 실패·Ready 전이 0, cleanup 완전 성공)해 overlay `probe-timeout-patch.yaml`의 값을 10 -> 11로 바꾸고 원본 JSON을 `docs/design/evidence/network-tolerant-calibration/`에 보존했다. 해석 주의: stage-4 CR 삭제 직후 readiness probe 단발 실패가 2회 모두 관찰됐다(사전 등록상 teardown 구간의 단발로 허용되지만 역산한 probe 시작은 steady 구간이라 판정이 이벤트 timestamp 분류 기준에 민감). overlay는 파일만 바꿨고 클러스터에는 적용하지 않았으며 3-arm 파일럿·본 실험은 시작하지 않았다. 전체 오프라인 스위트는 존재하지 않는 KUBECONFIG에서 463 passed.
- 2026-09-20: `timeoutSeconds = 11` 확정 승인. 위 항목의 stage-4 readiness 실패는 §5.8(신설)의 정의로 분류한다 - 이벤트 시각만으로 teardown 단정 금지, 추정 probe 실행 구간이 CR 삭제 시각을 가로지르면 `transition_straddling`(steady·순수 teardown과 별도 집계, 최종 표에 횟수·Ready 전이·Endpoint 영향·restart 표시, 단발이고 영향 없으면 profile 실패 아님, 연속·Ready=False·Endpoint 제거·restart면 trial 실패). **`TrialResult` 새 필드 없음**(분석 코드 `calibrate_network_tolerant_probe.py`·신규 읽기 전용 `trial_observer.py`와 문서에만 반영). calibration 두 회차를 원본 불변으로 재분류해 둘 다 `transition_straddling`(Ready 전이·Endpoint 영향·restart 없음)임을 확인(`phase8-blue-green-preflight-incident.md` §46). 전체 오프라인 스위트 497 passed.
- 2026-09-20: `network_degrade` 3-arm 파일럿(`native -> fixed_threshold -> proposed`, 각 1회 `is_pilot=true`, network_tolerant profile `timeoutSeconds=11`)을 완료했다 - 세 arm 모두 `recovered`, 중단 조건·필드 모순 없음, **스키마 변경·새 필드 없음**(상세 `phase8-blue-green-preflight-incident.md` §46.2~§46.10). profile 전환·복원은 Rollout만 apply(preview -> warmup -> promotion, 파일럿 준비 단계라 데이터 제외)하고 종료 뒤 Git base(기본 timeout 1)로 복원·검증했다. §5.8 최종 표: 세 trial 모두 `transition_straddling` 0건·Ready 전이/Endpoint 영향/restart 없음(calibration 2회는 각 1건). 읽기 전용 `trial_observer.py`가 실측 결함 3건(kubectl watch compact JSON, Chaos Mesh AllInjected 시각 없음, target 종료 뒤 shutdown 분류)을 드러내 고쳤다(`01935cf`). 해석 주의: proposed의 `target_replaced=true`는 promotion에 의한 교체(재시작 연쇄 아님)라 파생 값 `probe_isolation_held=False`가 오해를 부른다 - 정의 수정은 사용자 결정으로 남겼다. `memory_pressure`·본 실험 미시작. 전체 오프라인 스위트 501 passed.
- 2026-09-20: `network_degrade` 파일럿 완료 승인. 본 실험 전 두 해석을 오프라인으로 수정·동결했다(§5.9, §5.10 - **`TrialResult` 스키마·원본 JSON 불변, 새 필드 없음**, 실클러스터 작업·재실행 없음). (1) `collect_metrics.py` 파생 해석: `promotion_verified=true`이고 `t_switch`가 있는 실행의 target 변경은 계획된 promotion(`target_change_kind=planned_promotion`)이라 `target_replaced=true`만으로 `restart_chain_observed=true`/`probe_isolation_held=false`로 판정하지 않는다 - promotion 정보가 불완전·모순이면 `None`+validation issue, pod restart·UID 교체 증거(`--pod-evidence`)가 있으면 promotion으로 가리지 않고 `unplanned`. 실제 파일럿 JSON 3건(native/fixed_threshold/proposed)을 fixture로 회귀 테스트 25개 추가. (2) arm별 주입 노출 차이의 해석 동결(§5.10): 동일 stage schedule로 시작, promotion은 처치 자체, 남은 stage 미생성은 `treatment-induced truncation`(현재 injector 동작 유지), promotion 이후 노출·stage latency는 arm 간 직접 비교하지 않고 stage별 SLO 곡선은 action 이전 공통 구간에서만, 주 비교 지표는 `t_detection`/`t_decision`/`t_api_request`/`t_switch`/`t_recovery`/`outcome`/`action_stage`, 본 실험 arm 순서 균형화(§7). 이 동결을 위해 필요했던 발견: `network_degrade`는 `classify_stage`가 없어 `slo_stage`/`detection_stage`/`action_stage`가 파일럿 3건 모두 null이었다(주 비교 지표에 `action_stage`가 들어 있어 그대로는 분석 불가) - 어댑터가 실제로 만든 stage 창을 기록해 기존 선택 훅 `classify_stage`를 구현했다(오프라인 테스트 7개, 새 필드 없음, 파일럿 JSON은 소급 생성하지 않음). `run_all_scenarios.py`(arm 순서 생성기)는 아직 없다.
- 2026-09-20: `fixed_threshold` 임계치 정정(§1 정의는 불변 - "할당 CPU limit의 90% 초과"라는 의미 자체는 그대로, 절대값 계산이 낡았던 것을 고침). `anomaly-detection/fixed_threshold.py`에 하드코딩돼 있던 `CPU_LIMIT_CORES = 4.0`(→ 임계치 3.6코어)은 `gitops/apps/vllm-serving/rollout.yaml`의 실제 CPU limit이 lab-cpu3-warm-v1(2026-09-18, §11·§16) 이후 **3코어**로 바뀐 뒤에도 갱신되지 않아, 컨테이너가 구조적으로 넘을 수 없는 임계치(할당량의 120%)로 남아 있었다 - 보존된 pilot 3건(load_ramp/pod_kill/network_degrade 각 1회) 전부 예측 경로가 한 번도 발화하지 않은 것과 일치한다(pod_kill은 대상 소멸 시 CPU가 0이 되므로 애초에 구조적 미발화가 정상이지만, 나머지 둘은 이 상수 때문이었다). **Phase 8 동결값을 CPU limit=3.0, threshold=2.7코어로 확정**한다 - 90% 비율 자체(계약서 §6)는 바뀌지 않았다. 코드에서 절대 CPU limit 하드코딩을 완전히 제거하고 `--cpu-limit-cores`(기본값 없음, 0 이하·NaN·무한대·미지정은 즉시 fail-closed로 거부 - 평가 루프·Prometheus 접근 진입 전에 종료)를 필수 인자로 받게 했고, 시작 로그에 실제 적용된 `cpu_limit_cores`/`threshold_cores`를 출력한다. `experiments/arm_controller.py`가 fixed_threshold detector 서브프로세스를 띄울 때 이 동결값(`FIXED_THRESHOLD_CPU_LIMIT_CORES = 3.0`)을 `--cpu-limit-cores`로 명시 전달하도록 배선했다(`_build_detector_command()`). **이 정정 이전에 실행된 pilot 3건은 재실행하지 않고 원본 그대로 보존** - 구 3.6코어 기준으로 돌았으므로 detector 발화율 비교의 근거로 쓰지 않고 "배선 검증용 제외 pilot"으로만 취급한다(수치는 여전히 유효 - `outcome`/타임스탬프 등은 이 정정과 무관). 회귀 테스트: `anomaly-detection/test_fixed_threshold.py`(신규 11개 - 동결 비율 적용값·경계값(0/음수/None/NaN/무한대) 거부·구 상수 제거 확인·시작 로그 실제값 출력·CLI 필수 인자 누락/잘못된 값 fail-closed), `experiments/test_arm_controller.py`(신규 2개 - fixed_threshold 커맨드에 동결값 3.0 전달, proposed 커맨드엔 없음 확인 + 기존 2개 테스트의 `cmd[-3]` 인덱싱을 `cmd[1]`로 수정 - 인자 추가로 길이가 달라져 깨졌던 것). **`TrialResult` 스키마 변경 없음**. 전체 오프라인 스위트(`pytest experiments recovery-policy -q -m "not live_cluster"`, 존재하지 않는 KUBECONFIG) 535 passed, 3 deselected(직전 533에서 +2).
- 2026-09-20: `memory_pressure` adapter·runner·오프라인 테스트를 network_degrade_adapter.py와 같은 lifecycle 패턴으로 구현했다(**`TrialResult` 스키마 변경 없음** - 새 필드 없이 안전 관측값은 별도 evidence 파일과 `notes`에만 남긴다). (1) 신규 `experiments/memory_pressure_adapter.py`: active Service가 가리키는 단일 pod을 prepare()에서 고정, StressChaos CR을 `selector.pods`로 그 pod에 고정해 어댑터가 직접 순차 생성/삭제(Workflow CRD 대신, network_degrade와 동일 이유), 각 CR에 `duration`(stage 지속시간+60초 여유) 안전망 - 하니스가 죽어도 Chaos Mesh가 스스로 회수. `is_effective()`는 `AllInjected`뿐 아니라 실제 target working set이 baseline 대비 최소 50MiB 오른 것까지 함께 확인해야 True(단일 조건 요구 사양 초과 충족). (2) **안전 감시**: 주입 전 게이트(Node 상태·MemAvailable≥3GiB·baseline working set 측정·headroom 투영 = baseline+최대 stage 크기가 컨테이너 memory limit과 5GiB 안전 상한 중 더 작은 값을 넘지 않아야 함, 못 읽으면 전부 fail-closed) + 주입 중 5초 간격 안전 tick(Node MemAvailable<3GiB·target working set>5GiB·OOMKilled·restartCount 증가(OOM 여부로 원인 구분)는 `TrialInvalid`, Node NotReady·pressure는 `HarnessCorrupted`) - 위반 시 CR을 즉시 삭제한 뒤 예외를 던진다. cleanup()은 전 CR 삭제+실측 소멸 확인(실패 시 `RuntimeError`→`HarnessCorrupted`) 후 working set이 baseline ±150MiB로 돌아오는지 30초 bounded, **비차단**으로 확인해 evidence에만 기록한다(느린 페이지 캐시 회수는 실패로 취급하지 않음). target replacement는 network_degrade_adapter.py와 완전히 같은 규칙(효과 전 교체=invalid, 효과 후 교체=기록만) 재사용. 안전 관측값은 `results/(pilot/)memory-pressure-safety-{run_id}.jsonl`에 JSON lines로 남긴다(`TrialResult`와 무관한 보조 evidence, 기록 실패는 trial 판정에 영향 없음) - `.gitignore`에 `experiments/results/*.jsonl` 추가. (3) 신규 `experiments/run_memory_pressure_trial.py`: 나머지 세 러너와 동일한 `arm_controller` 배선(non-native는 detector·preview 준비를 우회 불가), **기본 readiness/liveness probe만 사용**(network-tolerant 같은 overlay 없음 - 지시), `--timeout-sec` 기본 1140초, `--size-mb`/`--workers`/`--duration-sec` 전부 필수 인자(기본값 없음 - 우발적 실행 방지)이며 단일 stage만 생성, `--size-mb`가 1000 이상이면 즉시 종료(1GB 이상 탐색 금지). `test_run_trial_wiring.py`의 `RUNNERS`에 추가(러너별 필수 CLI 인자를 자동으로 채워주는 `REQUIRED_EXTRA_ARGV` 테이블 신설, 다른 세 러너는 영향 없음). (4) **강도 재설계**: 기존 5-stage Workflow(`chaos/scenario-progressive-memory-pressure.yaml`, 500MB→...→5000MB)는 이력으로 그대로 보존하고 손대지 않았다 - 마지막 단계(5000MB)는 Phase 5(§2)에서 stress worker 자신의 self-OOM 순환만 유도한다고 확인돼 있어 신규 후보에서 제외한다. 신규 `chaos/scenario-memory-pressure-explore.yaml`(참고용 데이터, 실행 코드 아님)에 500/1000/1500/2000MB 후보를 기록했다 - **2500MB·5000MB는 제외**. §4의 memory_pressure 표 행과 "마지막 단계 SLO 위반 보장" 문구는 옛 5000MB 설계 기준이라 **잠정 무효로 표시**하고(load_ramp의 동일 보장 문구는 유지), 새 강도의 그 보장은 근거 확보 전까지 주장하지 않는다 - §48.3에서 Phase 5 §3의 실제 타임아웃 관측(baseline 대비 순증분 약 1862MB)이 이 새 후보 구간에 포함됨을 정황 증거로만 남겼다(사전 등록된 반사실 검증 아님). (5) memory_pressure 재개 전 읽기 전용 클러스터 점검(§48, 실측)도 이번 구현의 근거로 함께 기록했다(Node MemAvailable sj-worker 8.09GiB/sj-control 5.21GiB, vLLM working set ~3.4GiB, 메모리 한도 6Gi 불변 - CPU만 4→3코어로 바뀜, chaos CR 0건). 회귀 테스트: `experiments/test_memory_pressure_adapter.py`(신규 35개 - stages 필수화, prepare 게이트 11개(대상 없음/다중/상세 조회 실패/Node IP·상태·MemAvailable 조회 실패/미달·working set 조회 실패/headroom 부족 3종·evidence 로그), is_effective 이중조건 2개, 주입 중 안전 위반 7종(MemAvailable 낮음·조회 실패, working set 높음, Node unhealthy→HarnessCorrupted, OOMKilled, restart 증가(원인 구분), target 조회 실패), CR duration 안전망, selector.pods 확인, target replacement 2개, cleanup 4개(잔존 CR 예외, 복귀 확인 성공/실패 비차단, prepare 전 스킵), classify_stage 4개), `experiments/test_run_trial_wiring.py`(4번째 러너 추가로 20 passed). 전체 오프라인 스위트(`pytest experiments recovery-policy anomaly-detection -q -m "not live_cluster"`, 존재하지 않는 KUBECONFIG) 586 passed, 3 deselected. **실클러스터 작업 없음** - live smoke는 이 오프라인 검증 통과 후 별도 진행.
- 2026-09-20: `memory_pressure` 최소 live smoke(`native`/`--pilot`/worker 1개/500MB/60초) 완료 - 상세는 `docs/design/phase8-blue-green-preflight-incident.md` §49. 1차 시도가 `injection_valid=False`(`invalid_run`)로 끝났으나 안전 로그 대조로 클러스터가 아니라 하니스 판정 버그(`is_effective()`가 Prometheus 지표 반영 지연을 흡수 못 함)임을 확정하고 `memory_pressure_adapter.py`(working set 상승 확인을 재시도되는 `is_started()`로 이동)·`run_memory_pressure_trial.py`(`injection_started_timeout_sec` 30→60초)를 수정(오프라인 스위트 586 passed 재확인, 개수 불변). 2차 시도 **PASS** - 지시된 7개 PASS 기준(AllInjected, working set 상승 478MB(≥400MiB), restartCount·OOMKilled 불변, Node MemAvailable 7.6~8.07GiB(≥4GiB)·조건 이상 0건, working set 5GiB 미만, CR 삭제 후 30초 이내 baseline 복귀, 클러스터 사후 정리 확인) 전부 충족, 즉시 중단 조건 미발동. `outcome=prevented`는 개입이 아니라 500MB 강도 자체가 SLO 위반을 못 일으킨 결과(Phase 5 §3 실제 타임아웃 관측 순증분 ~1862MB와 정합) - 이상 아님, 강도 calibration이 아직 필요함을 재확인. **`TrialResult` 스키마 변경 없음**, 1GB 이상 탐색·전체 ramp·non-native arm·`run_all_scenarios.py`·본 실험 전부 미실행.
- 2026-09-20: `memory_pressure` 강도 calibration 탐색(사전 등록 §50, 결과 §51) - 신규 `experiments/explore_memory_pressure_intensity.py`(`run_once()`/`TrialResult` 미사용, 순수 탐색 전용 - `run_memory_pressure_trial.py`의 1GB 차단은 불변, 산출물은 `results/`(top-level)에 별도 저장돼 본 분석과 안 섞임)로 1000MB → 1500MB 순서 실행, **둘 다 PASS**(9개 기준 전부 충족, 즉시 중단 없음, `t_slo`/`t_recovery` 둘 다 null - SLO 미위반). 핵심 발견: 현재 baseline(~3452MiB)에서 5GiB 안전 상한까지 주입 가능한 최대치는 약 1668MB인데, Phase 5 §3의 실제 붕괴 강도(baseline 대비 순증분 ~1862MB)는 그보다 194MB 커서 **현재 안전 상한을 유지하는 한 이 방식으로는 Phase 5와 같은 실제 SLO 붕괴를 재현할 수 없다**(계산상 비교이지 1668MB 자체를 실측한 것은 아님). 세 가지 다음 단계를 제안만 하고 실행하지 않았다 - (A) 1600~1650MB로 한 번 더 확인(위반 도달 가능성 낮음), (B) memory_pressure를 "sub-critical 성능 저하" 시나리오로 재정의(두 라운드 모두 `p95_peak`가 SLO 임계치를 순간적으로는 넘김), (C) 안전 상한 자체 재검토(사용자 결정 필요, 이번 범위 밖). 오프라인 테스트 23개 추가(`test_explore_memory_pressure_intensity.py`), 전체 스위트 609 passed. memory_pressure 3-arm 파일럿·`run_all_scenarios.py`·본 실험·2000MB 이상 탐색·non-native arm 전부 미실행, 안전 상한·결과 스키마 변경 없음.
- 2026-09-20: `memory_pressure` 2차 강도 calibration 사전 등록(`phase8-blue-green-preflight-incident.md` §52) - §51 옵션 A(1600~1650MB 재확인)만 진행, 안전 상한(5GiB)·sub-critical 재정의는 이번에도 하지 않는다. 기존 `explore_memory_pressure_intensity.py`를 확장(신규 파일 없음): `run_round()`에 `stage_duration_sec`(기본 90.0, 이번 라운드는 원본 YAML stage와 같은 **120.0**을 명시 전달)·`min_headroom_bytes`(기본 0.0) 인자 추가, `ALLOWED_SIZES_MB`에 **1600.0** 추가(1650.0·2000.0은 여전히 목록 밖 - 구조적으로 영구 거부). 1600MB 전용 사전 조건으로 순수 함수 `sufficient_headroom_for_injection()`을 추가했다 - baseline+1600MB가 5GiB 안전 상한까지 최소 128MiB 여유를 못 남기면 주입 시도 자체를 안 하고 `TrialInvalid`. 진행 순서는 **1500MB×120초 먼저** → `t_slo`(sustained SLO 위반, `slo_judge`의 30초 연속/즉시 availability 정의 그대로 재사용, 새 판정 로직 없음)가 null일 때만 **1600MB×120초** 조건부 실행 → 1600MB까지도 위반 없으면 1650MB·안전 상한 상향으로 자동 진행하지 않고 보고. 같은 크기의 90초/120초 라운드가 결과 파일에서 섞이지 않도록 `run_id`에 `-{stage_duration_sec}s-` 세그먼트 추가(저장 위치·`collect_metrics.py` 미참조는 §50.3과 동일, 불변). **`TrialResult` 스키마·어댑터 안전 상수(`MAX_TARGET_WORKING_SET_BYTES`=5GiB 등) 변경 없음**. 오프라인 테스트 7개 추가(`sufficient_headroom_for_injection` 5개 + 등록값 검증 2개), 전체 스위트(`pytest experiments recovery-policy anomaly-detection -q -m "not live_cluster"`, 존재하지 않는 KUBECONFIG) 616 passed(직전 609에서 +7), 3 deselected. 실클러스터 작업은 이 사전 등록 커밋 이후 별도 진행.
- 2026-09-20: `memory_pressure` 1500MB×120초 calibration 실행 결과(`phase8-blue-green-preflight-incident.md` §53) - **sustained SLO 위반 확인**(`t_slo` not null, 30초 연속 latency 위반 스트릭이 주입 후 약 31초 시점에 확정, 10초 만에 회복, availability는 위반 없음), 안전조건은 §50.4 9개 전부 PASS(restart·OOM·Node 이상 없음, 최대 working set 4.730GiB로 5GiB 상한까지 약 277MiB 여유, cleanup 후 baseline 완전 복귀). §52 사전 등록 규칙("1500MB에서 sustained 위반이 확인되면 1600MB는 실행하지 말고 멈춘다")에 따라 **1600MB는 실행하지 않았다**. 실행 전 로컬 Prometheus port-forward(`localhost:9090`)가 이전 세션 종료로 끊겨 있어 1차 시도가 `Node MemAvailable` 조회 실패로 fail-closed 중단됐다(클러스터 자체는 정상이었음, kubectl로 직접 확인) - 재연결 후 재실행. 핵심 발견: §51(90초 stage)에서는 같은 1500MB·거의 같은 실측 상승분(~95.7%)으로도 `t_slo`가 null이었는데, stage 지속시간만 120초(원본 YAML 기준)로 늘리자 sustained 위반이 재현됐다 - §51.2에서 우려한 "5GiB 안전 상한 때문에 구조적으로 위반을 재현할 수 없다"는 결론이 강도 축에서는 맞을 수 있어도 **지속시간 축에서는 성립하지 않음**을 보였다. 안전 상한·`TrialResult` 스키마·sub-critical 재정의 전부 변경 없음. 1500MB×120초가 본 실험 high-stage 후보로 유력하다고 제안했으나(재현성 확인은 미실행, 사용자 판단 필요), memory_pressure 3-arm 파일럿·`run_all_scenarios.py`·본 실험·1600MB 이상 탐색은 전부 미실행.
