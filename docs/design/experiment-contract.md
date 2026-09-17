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

- `memory_pressure`/`load_ramp`는 설계상 마지막 단계에서 SLO 위반이 보장되도록 이미 튜닝돼 있음(9-3절 반사실 요구사항).
- **이 예산은 "조기 종료 가능한 최악의 경우"가 아니라 거의 기본 실행시간이다** — chaos 자체 종료를 기다려야 하므로 복구가 일찍 됐다고 trial이 일찍 끝나지 않는다. (5+19+15+11)분 × 3 arm × 5회 = **약 750분(12시간 30분)이 기본값**이고, preview 준비·quiescence 대기·`invalid_run` 재실행까지 포함하면 실제 일정은 **약 14~16시간**으로 잡는다.

### `load_ramp` 확정 설정(2026-09-16 - 본 실험 시작 후 변경 금지)

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
| `min_observation_sec` | float | "prevented" 조기 종료를 막는 최소 관찰시간(초) - 주입 효과 확인 직후부터 계산(2026-09-17 추가) |
| `t_run_start` | ISO8601 UTC | preview 준비 등 trial 준비 시작 시각 |
| `t_injection` | ISO8601 UTC | chaos 주입 시작. 어댑터가 `get_actual_injection_time()`을 구현하면 실제 삭제/시작 시각 그 자체가 아니라 폴링으로 그 변화를 **처음 관측한** 시각(미구현이면 `inject()` 호출 시각) |
| `injection_observation_error_sec` | float \| null | `t_injection`이 폴링 관측값일 때만 채움 - "대상이 살아있음을 마지막으로 관측한 시각"과 `t_injection`의 실측 차이(상한, 정확한 오차 아님). `poll_interval_sec` 설정값이 아니다 - 어댑터의 조회 자체(kubectl exec 등)도 시간이 걸려 설정값만으론 상한을 보장 못 한다(2026-09-16 정정). null이면 관측 기반 값이 아니거나(어댑터 미구현) 비교 기준점이 없음(첫 poll에서 이미 상태가 바뀜) |
| `t_injection_end` | ISO8601 UTC | chaos 자체가 끝난 시각(§4 종료조건①) |
| `t_detection` | ISO8601 UTC \| null | 미탐지면 null |
| `t_decision` | ISO8601 UTC \| null | |
| `t_api_request` | ISO8601 UTC \| null | |
| `t_switch` | ISO8601 UTC \| null | |
| `t_slo` | ISO8601 UTC \| null | `outcome=prevented`면 null |
| `t_recovery` | ISO8601 UTC \| null | timeout이면 null |
| `t_audit_write` | ISO8601 UTC \| null | |
| `t_audit_push` | ISO8601 UTC \| null | **복구시간 계산에 포함 안 함**. 비동기라 trial 종료 시점엔 비어있을 수 있음(§6 reconcile) |
| `commit_sha` | str \| null | 위와 동일한 이유로 reconcile 단계에서 채워질 수 있음 |
| `detected` | bool | |
| `detection_source` | enum \| null | `fixed_threshold` \| `isolation_forest` \| `alertmanager` \| `none` — 실제로 무엇이 먼저 반응했는지 |
| `action` | enum | `promote_preview` \| `none` |
| `promotion_verified` | bool \| null | `native`는 항상 null |
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

`git_client.py`의 push는 비동기라 trial row를 처음 쓰는 시점엔 `t_audit_push`/`commit_sha`가 비어있을 수 있다. 전체 실험(또는 각 시나리오) 종료 후, recovery-policy의 `outbox.json`을 다시 읽어 각 `run_id`에 대응하는 결과 row에 `t_audit_push`/`commit_sha`를 채워 넣는 **reconcile 단계**를 실험 절차에 명시한다(`collect_metrics.py` 실행 전에 반드시 거침).

## 6. 안전장치 — `run_once()`가 매 trial마다 반드시 함

- 이전 trial의 firing 상태 Alertmanager 알림이 다음 `run_id`로 새지 않도록, trial 사이 **quiescence 대기**(모든 알림이 resolved 상태가 될 때까지) — §4 trial 종료조건③과 동일 개념
- `safety.py`의 idempotency(`state/safety_state.json`)·cooldown을 매 trial 시작 전 명시적으로 초기화
- trial 시작 전 매번: preview Ready 상태 확인 + active/preview selector가 분리(다름)돼 있는지 확인(`native`는 preview 자체를 안 만듦)
- probe가 trial 도중 죽거나 비정상 응답을 내면 `probe_valid=false` → `invalid_run` 처리 — arm의 실패로 안 셈
- 장애 주입이 실제로 대상에 적용됐는지 확인 못 하면 `injection_valid=false` → `invalid_run`
- `fixed_threshold.py`의 임계치(CPU>90%)와 `anomaly-detection/artifacts/model.pkl`은 파일럿 이후, 본 실험 전에 **동결**한다. 본 실험 결과를 본 뒤에는 절대 재학습·재조정하지 않는다.

## 7. 실행 순서 — arm을 섞어서 수행

60회를 arm별로 몰아서 돌리지 않고 **섞어서(interleaved)** 수행한다 — 특정 arm이 특정 시간대(클러스터 상태 drift, 캐시 워밍 등)에 몰리는 걸 방지. 시나리오별로 5회×3arm=15회 블록 안에서 arm 순서를 `order_seed`로 셔플하고, 그 시드와 결과 순서(`sequence_index`)를 결과 스키마에 남겨 재현 가능하게 한다.

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
