# SLO 정의 (Phase 8 평가 전 사전 확정)

> **이 문서는 Phase 8 실험 데이터를 보기 전에 확정한다.** 실험 결과를 보고 임계치를
> 조정하면 제안 방식에 유리하게 기준을 고른 것이라는 지적을 받을 수 있다(9-7절
> 공정한 비교 설계와 같은 이유). 아래 정의를 고정한 뒤에는 Phase 8 평가가 끝날
> 때까지 바꾸지 않는다. 불가피하게 바꿔야 한다면 이 파일 하단 "변경 이력"에 사유와
> 함께 남기고, 이전 정의로 이미 계산된 결과가 있다면 그것도 같이 밝힌다.

## 1. 왜 상대(relative) SLO인가

이 프로젝트에는 실제 고객과 맺은 SLA가 없다. "P95 5초가 적당해 보인다" 같은 절대
기준은 근거를 대기 어렵고 임의로 골랐다는 지적을 받기 쉽다. 대신 **정상 상태에서
실측한 값 대비 상대 배수**로 정의해서, 기준 자체의 출처를 데이터로 추적 가능하게
한다.

## 2. `L_baseline` (정상 상태 기준 latency) — SLO v3 공식, v1·v2는 폐기(이력용 보존)

Phase 8 실험(합성 probe를 별도 프로세스로 분리 - guideline.md/2차 리뷰)에서
probe 자신의 요청 payload가 SLO 판정 대상 서비스에 유의미한 부하를 주면
안 된다. **v1은 이 조건을 만족하지 못해 폐기했다** — probe payload를
바꾸면 SLO 판정 대상 자체(어떤 요청 모양을 "정상"으로 보는지)가 바뀌므로
baseline·threshold를 반드시 같이 재계산해야 한다. **v2는 자원 구성이
바뀌어(4코어→3코어, `lab-cpu3-v1`) 더 이상 현재 환경을 반영하지 않아
폐기했다** — probe·payload는 그대로지만 서빙 환경의 CPU 여유가
달라지면 baseline latency 자체가 달라지므로 마찬가지로 재계산이
필요하다.

### SLO v3 — 본 실험 공식 기준 (2026-09-18 확정, `lab-cpu3-warm-v1`)

| 항목 | 값 |
|---|---|
| `L_baseline` (P95) | **0.324초** |
| 출처 | `experiments/calibrate_probe_only.py` 3회 독립 실행(각 300건, 1RPS, 300초), 성공률 100%, 각 회차 대표 P95(60초 슬라이딩 윈도우의 마지막 값) [0.300s, 0.348s, 0.324s]의 **중앙값** |
| 조건 | `lab-cpu3-warm-v1`(3코어 CPU 제한 + exec startupProbe warmup gate, `docs/design/phase8-blue-green-preflight-incident.md` §19) 동결 직후, 장애 미주입, quiescent 상태, 정상 부하(1 RPS), vLLM `Qwen/Qwen2.5-0.5B-Instruct`, `max_tokens=1`, `chaos/probe-config.yaml` |
| probe profile | `inference-max1-rps1`(v2와 동일 - probe 조건 자체는 안 바뀜) |
| 계산 규칙 사전 고정 기록 | `docs/design/phase8-blue-green-preflight-incident.md` §20(측정 전 커밋 `09803c2`) - v1→v2 전환과 동일한 산정 원칙(3회 독립 실행, 마지막 안정화 P95, 중앙값) 재사용, 새로 발명하지 않음 |

### SLO v2 — 폐기됨, 이력 보존용

| 항목 | 값 |
|---|---|
| `L_baseline` (P95) | 0.256초 |
| 출처 | `experiments/calibrate_probe_only.py` 3회 독립 실행(각 300건, 1RPS, 300초), 성공률 100%, 각 회차 P95 [0.2565s, 0.2545s, 0.2586s]의 **중앙값** |
| 조건 | 장애 미주입, quiescent 상태, 정상 부하(1 RPS), vLLM `Qwen/Qwen2.5-0.5B-Instruct`, `max_tokens=1`, `chaos/probe-config.yaml`, vLLM CPU limit **4코어**(`lab-cpu3-v1` 이전) |
| probe profile | `inference-max1-rps1` |
| 폐기 사유 | CPU limit을 4→3코어로 낮춰(`lab-cpu3-v1`) 동일 probe 조건에서도 baseline latency가 달라짐 - 재측정 필요 |

### SLO v1 — 폐기됨, 이력 보존용

| 항목 | 값 |
|---|---|
| `L_baseline` (P95) | 2.686초 |
| 출처 | `chaos/loadgen/results/load-ramp-20260905-022341.csv`, `stage-1-baseline` (1 RPS, 60건, 성공률 100%) |
| 조건 | 장애 미주입, 정상 부하(1 RPS), vLLM `Qwen/Qwen2.5-0.5B-Instruct`, `max_tokens=10` |
| 폐기 사유 | probe가 이 payload(`max_tokens=10`)를 그대로 쓰면 vLLM pod CPU를 거의 4코어(≈4000m) 전부 점유하는 것이 실측 확인됨(`calibrate_probe_only.py`) - 측정 도구가 그 자체로 부하 주입기가 되는 observer effect. 본 실험에는 쓰지 않는다. |

향후 baseline을 또 갱신해야 한다면(probe profile 변경 등) 이 파일 하단
"변경 이력"에 사유·전후 값과 함께 남기고, Phase 8 평가 데이터를 보기 **전에**
끝낸다 — 지금까지와 같은 원칙.

## 3. Latency SLO

> `P95 latency > 2 × L_baseline`(SLO v3 = **0.648초**, v2 = 0.512초는
> 폐기)가 **30초 이상 연속** 지속되면 latency SLO 위반.

- 순간적으로 튀는 값이 아니라 지속되는 열화만 위반으로 잡기 위해 30초 지속 조건을
  둔다(일반적인 SRE 관행 — 단발 스파이크로 인한 오탐/flapping 방지).
- `P95`는 직전 60초 슬라이딩 윈도우 기준으로 계산한다(availability SLO의 윈도우와
  통일).

## 4. Availability SLO

> 직전 **60초** 슬라이딩 윈도우에서 요청 성공률이 **99% 미만**이면 availability
> SLO 위반.

- "성공"의 정의는 `chaos/loadgen/ramp.py`의 기준과 동일: HTTP 상태코드 200.
- **단일 요청의 timeout도 실패로 센다.** timeout 기준은 `ramp.py`가 이미 쓰는
  `aiohttp.ClientTimeout(total=30)`(30초)과 통일해서, 부하생성기 결과와 실시간
  판정 결과가 서로 다른 잣대를 쓰지 않게 한다.

## 5. `t_SLO` (SLO 위반 판정 시각)

> Latency SLO와 Availability SLO 위반 조건 **둘 중 하나라도** 먼저 만족된 시각을
> `t_SLO`로 정의한다.

이 판정은 `collect_metrics.py`가 아니라 별도의 실시간 합성 프로브가 전담한다
(guideline.md 9-2절 — `collect_metrics.py`는 집계 역할만 함).

## 6. `t_recovery` (정상화 판정 시각)

> 위 두 위반 조건이 모두 해소된 상태가 **연속 30초** 유지되면 그 구간의 시작
> 시각을 `t_recovery`로 정의한다(단발 성공 1건으로 판정하지 않음 — guideline.md
> 9-2절과 동일한 정의를 그대로 따름).

## 7. 무엇에는 쓰고 무엇에는 안 쓰는지

- 이 정의는 Phase 8의 `t_SLO`, `t_SLO_counterfactual`, lead time
  (`t_SLO_counterfactual − t_detection`) 계산에 그대로 쓴다.
- 지금까지 탐색적으로 수집한 CSV(부하 증가·네트워크 열화 등)는 `L_baseline`을
  뽑는 용도로만 쓰고, Phase 8 평가 자체의 입력 데이터로는 쓰지 않는다 — 애초에
  재검증 전 오염된 실행 이력도 섞여 있었기 때문에(중요발견 3) 평가용으로 재사용하지
  않는다.

## 변경 이력

- 2026-09-06: 최초 확정. `L_baseline = 2.686s`, 위 5개 규칙(§3~§6).
- 2026-09-16: 파일럿에서 `max_tokens=10` probe가 vLLM CPU limit인 4코어를 거의
  전부 사용해 측정 도구가 실험 대상에 유의미한 부하를 가하는 observer effect를
  확인했다. 본 실험에서는 `max_tokens=1` 경량 probe를 사용하고, 요청 특성이
  변경된 만큼 동일한 산정 원칙(정상 상태 P95, 중앙값)으로 baseline과 latency
  SLO를 다시 측정해 동결한다. `L_baseline` v1(2.686s) → v2(0.256s), latency
  SLO v1(5.372s) → v2(0.512s). v1로 이미 계산된 결과는 없음(Phase 8 실제
  arm 비교 데이터 수집 전 단계였음).
- 2026-09-18: CPU headroom 확보를 위해 vLLM CPU limit을 4→3코어로
  낮췄다(`lab-cpu3-v1`, `docs/design/phase8-blue-green-preflight-incident.md`
  §11~§12). 이어서 Ready 판정이 첫 추론 완료보다 먼저 나던 문제를
  startupProbe warmup gate로 구조적으로 해결하고 3회 공식 콜드스타트
  검증(headroom 3/3 PASS)을 거쳐 `lab-cpu3-warm-v1`을 Phase 8 공식
  기준선으로 동결했다(§14~§19). probe 조건(payload·RPS·profile)은 v2와
  동일하지만 서빙 환경의 CPU 여유가 달라져 baseline latency 자체가
  달라질 수 있어, 데이터를 보기 전에 계산 규칙을 먼저 고정(§20, 커밋
  `09803c2`)한 뒤 동일한 산정 원칙(3회 독립 실행, 각 회차 마지막
  안정화 P95, 중앙값)으로 재측정했다. `L_baseline` v2(0.256s) →
  v3(0.324s), latency SLO v2(0.512s) → v3(0.648s). Availability SLO
  (60초 윈도우, 99%, timeout 30초)는 `L_baseline`과 무관해 변경 없음.
  재측정 3회 모두 성공률 100% - 원시 결과는 `experiments/results/
  probe-calib-probe-only-*-raw.csv`(gitignore 대상)에 보존.
