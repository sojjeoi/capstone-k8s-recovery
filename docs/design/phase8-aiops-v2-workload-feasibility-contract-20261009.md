# AIOps v2 workload feasibility pilot 계약 r2 (freeze 후보 v2, counted=false, 2026-10-09)

> 상위 설계: `phase8-aiops-v2-design-draft-20261009.md`(r3·r2 결정). **실행 승인 아님 - pilot 실행 직전 사용자 승인 필요.**
> 이 pilot은 학습·보정·검증·우월성 근거가 아니다. 결과는 v2 정상 workload 수준(low/medium/high) 정의에만 쓴다.
> current c1·A/B·B/C/K·N1 결과와 섞지 않는다.
> r1→r2(실행 전, 결과 없음): ① 고정 간격 도착의 15 s scrape aliasing 제거(§2.1) ② `request_success_total`은 완료 counter로 재분류(§5) ③ SLO 경계 종료 규칙(§2.3). freeze 후보 v1(`...-freeze-manifest-v1-...json`)은 이력으로 보존, 그 해시는 r1 파일 기준이라 현재 파일과 맞지 않는다(대체됨).

## 1. 질문

정상(공식 SLO·안전 정상)이면서 vLLM service-state metric(`vllm:num_requests_running` 등)이 실제로 움직이는 배경 부하 수준이 있는가?
IF score·CPU 임계값 발화는 계산하지 않고 선택 기준으로 쓰지 않는다(실행 중 탐지기 없음).

## 2. workload (실행 전 고정, 한 축만 변화)

- 배경 부하 이미지: `loadgen-runner:phase8-v3-boundaries`(두 워커에 존재 확인). 요청 전송은 이미지 내장 동결 `/ramp.py`(sha256 `aadab9fc…`)의 `fire_request` 그대로(같은 aiohttp 세션 설정, timeout 30 s, open-loop).
- 요청 모양 고정: model Qwen/Qwen2.5-0.5B-Instruct, prompt "Hello", **max_tokens 10**(기존 동결 load_ramp 설정과 같음).
- ladder `chaos/v2-workload-ladder.yaml`: L1 0.025 / L2 0.05 / L3 0.10 / L4 0.15 / L5 0.20 RPS, 각 300 s. L0 = ramp 전 기준선 300 s(probe만). 단계 사이에서 바뀌는 값은 목표 평균 RPS 하나.
- 근거: 기존 확정 설정(2026-09-18, lab-cpu3-warm-v1/SLO v3)에서 0.025·0.05 RPS 3/3 SLO 준수, 0.20 경계, 0.30·0.40 3/3 위반(각 90 s). 0.30 이상 넣지 않음. 300 s = 15 s 수집 20점.
- SLO 측정: 기존 probe(1 RPS, max_tokens=1, 같은 설정 sha256)·동결 slo_judge v3·slo_episodes r2.
- **max_tokens 변경 금지(이번 단계).** ladder 전체에서 활성화가 없으면 `feasible=false because no activation`으로 보고하고, 새 고정 max_tokens workload는 그 뒤 별도 승인.

### 2.1 도착 방식 - jittered_grid_v1 (aliasing 제거)

- **기존 방식(코드 확인):** 동결 `ramp.py run_stage`는 `next_fire += 1/rps` 고정 간격, jitter·난수 없음. 간격 40·20·10·6.67·5 s는 15 s scrape와 위상 고정(5·10·20·40 s는 위상 3개, 6.67 s는 9개)되어 point-in-time gauge의 비zero 비율이 실제 busy 비율과 다르게(0 또는 1까지) 관측될 수 있다. 요청별 `sent_at`은 pod 안 raw CSV에만 있고 기존 어댑터는 회수하지 않았다.
- **수정(최소 변경):** 새 파일 `chaos/loadgen/v2_arrival.py`가 발사 시각 계획만 바꾼다. 동결 ramp.py·이미지는 그대로(이미지 재빌드·노드 반입 없음 - 파일은 `kubectl cp`로 ramp pod에 복사).
  - N = ceil(rps × 300), slot = 300 / N, k번째 요청 시각 = (k + U_k) × slot, U_k = `random.Random(stage_seed).random()` 순서대로.
  - Poisson 대신 이 방식을 고른 이유: 요청 수가 고정되어 달성 RPS가 목표와 맞고(L1 7.5건 수준에서 Poisson은 ±37% 흔들림), 각 요청 위상이 slot 안에서 균등이라 scrape 격자와 주기 동기화가 없다. 평균 간격은 기존과 같다.
- **seed(구현 전 고정, ladder `arrival.seeds`):** `v2wl90` = 2026100990, `v2wl91` = 2026100991, `v2wl92` = 2026100992. 단계 seed = run_seed × 10 + 단계 번호(1..5). seed는 실행 ID에 묶여 인자로 바꿀 수 없다(같은 ID 재시도 = 같은 seed, 결과를 보고 바꾸지 않음).
- **증거 보존:** 단계마다 raw CSV(k, 계획 시각, 실제 `sent_at`, 발사 지연, 지연·상태·성공)와 summary(목표·계획 수·실제 발사 수·달성 RPS·최대 발사 지연·단계 시작/끝 UTC)를 결과 폴더에 `x` 모드로 회수.
- **허용 오차(실행 전 고정):** 실제 발사 수 = 계획 수 N, |N/300 − 목표 RPS| ≤ 1/300(L1만 8건 = 0.0267, 나머지는 정확히 같음), 요청별 발사 지연 ≤ 1.0 s, 분석기가 seed로 계획을 재계산해 raw의 계획 시각과 정확히 일치. 하나라도 어기면 그 단계 `load_conformant=false` → 선택 불가.
- pod 준비 때 pod 안 `/ramp.py`·`/v2_arrival.py`·ladder 해시를 로컬 기대값과 대조(불일치면 시작 안 함).

### 2.2 활성화 규칙 (유지, pilot 결과로 조정 금지)

- 단계 `[start, end)`(반열림)의 원본 scrape 표본(job=`vllm-active`, 평가 격자 아님) 중 `vllm:num_requests_running ≥ 1` 비율 ≥ **0.20**이면 activated.
- 단계별 출력: 표본 수·기대 표본 수(300 s/15 s = 20)·차이 사유(`as_expected` / ±1 격자 정렬 / scrape 누락·중복), running≥1 수·비율, waiting≥1 수·비율, `request_queue_time` 요청 수·평균, 계획·실제 발사 수, 달성 RPS, 최대 발사 지연.

### 2.3 SLO 경계 종료 규칙

- SLO 위반은 rep invalid 사유도, 진행 중 단계의 즉시 중단 사유도 아니다. 단계 300 s는 끝까지 관찰한다.
- 단계가 끝나면 실행기가 공식 판정: [기준선 시작, 단계 끝] 관찰을 동결 slo_episodes r2(응답이 단계 끝 이전에 확인된 요청만 증거)로 계산해 위반 구간이 단계와 겹치면 SLO-violating. probe 증거가 단계 끝을 덮을 때까지(최대 60 s) 기다린다.
- SLO-violating이면: 그 단계 결과 보존, rep invalid 아님(outcome completed), **더 높은 단계는 시작하지 않음**, `ramp_terminated_after_slo_boundary=true`·`slo_boundary_stage`·`stages_not_run` 기록, 그 단계와 이전 단계만으로 선택 규칙 적용.
- 같은 판정 함수(`v2_workload_pilot_analysis.stage_slo_verdict`)를 실행기와 분석기가 같은 입력으로 쓰므로 판정이 같다.
- 판정 불가(probe 증거 미도착·표본 부족·공백)는 aborted(그 rep invalid, fail-closed).
- 단계 사이에 ramp 부하가 없는 짧은 간격(판정 대기 수 초~최대 60 s + 단계 시작 kubectl 왕복)이 생긴다. 단계 창은 실제 단계 시각으로 자르므로 판정에 섞이지 않는다.
- 이 규칙은 결과를 보고 만든 것이 아니라 live pilot 전에 고정한 안전/실험 경계다.

## 3. 실행 구조 (`experiments/run_v2_workload_pilot.py`)

사전검사(동결 함수: 전원·노드·SSH·시계·canonical Healthy·delay 30·record_only·unresolved 0·context 없음·기존 정책 격리) → probe 시작·SLO 기준선 준비 → L0 300 s → ramp pod 준비(파일 복사·해시 대조) → 단계 i 실행 → 단계 회수 → 단계 SLO 판정(§2.3) → … → 정리(probe·ramp pod) → canonical 끝 확인 → Prometheus 원본 표본 저장(`metrics-<run_id>.json`) → 요약.
- preview·승격·guarded context·신호 없음. 실행 중 탐지기 없음.
- **안전 감시:** 동결 무주입 안전 감시 그대로(probe 생존, active Pod 1개, OOM·재시작, 노드 상태) + 기존 정책 격리 유지. SLO 위반 즉시 중단만 끈다(§2.3이 대신 단계 경계에서 판정).
- 중단 규칙: 안전 감시 위반·단계 비정상 종료·단계 시간 상한(370 s)·SLO 판정 불가 → aborted(그 rep invalid). 정리 오류 → 다음 rep 차단.
- 실행: 세션 실행기 `guarded_pilot_session.ps1 -Run v2wlNN -Mode record_only -RunnerScript run_v2_workload_pilot.py`(PowerShell Start-Process, provenance prepare→launch→verify).

## 4. ID·반복

- `v2wl90`, `v2wl91`, `v2wl92`(rep 3090–3092), 결과 `experiments/results/capstone_v2_workload_pilot/`. 기존 ID와 충돌 없음 확인.
- 유효 목표 2, 최대 시도 3. 순서 강제(v2wl91은 v2wl90 시도 뒤). 같은 ID 재사용 금지. seed는 §2.1 고정값.

## 5. 단계 판정 (`experiments/v2_workload_pilot_analysis.py`, 기계 판정)

- rep 유효: outcome completed(SLO 경계 종료 포함), 정리 오류 없음, 단계 summary 있음, probe CSV 있음, 실행된 모든 단계의 공식 SLO 판정 가능.
- 단계별: `slo_normal`(§2.3 판정), probe 실패 0, ramp 실패(sent−success) 0, `evaluable`(15 s 표본 80% 이상), `load_conformant`(§2.1 허용 오차), `activated`(§2.2).
- metric은 `pod` + `job="vllm-active"`로 거른 원본 scrape 표본. 여러 라벨 줄(예: finished_reason)은 같은 scrape 시각끼리 합.
- **metric 의미(읽기 전용 확인, 설계 r3):** `vllm:request_success_total`은 엔진이 요청을 끝낼 때 증가하는 완료 counter(finished_reason=stop|length|abort|error|repetition) - 도착률 아님. 분석에서는 `finished_request_rate`(service throughput)와 `finished_abort_error_rate`(진단)로 기록. 실제 도착률 metric은 없음 → harness 목표·달성 RPS는 workload 라벨/진단으로만.

## 6. low / medium / high 선택 규칙 (사전 고정, 사람 선택 없음)

- **high** = 모든 유효 rep에서 normal_and_safe·evaluable·load_conformant·activated인 가장 높은 단계(L1 이상). SLO-violating 단계는 high가 될 수 없다. 어떤 유효 rep에서 실행되지 않은 단계(SLO 경계 종료 뒤)는 선택 불가.
- **medium** = 모든 유효 rep에서 normal_and_safe·evaluable·load_conformant인 단계 중 rps가 high의 절반에 가장 가까운 것(동률이면 낮은 쪽), high·low와 달라야 함.
- **low** = L1이 같은 조건이면 L1.
- 셋 중 하나라도 정해지지 않으면 `feasible = false` + 사유(보간하지 않음). IF·C2 결과는 기준이 아니다.

## 7. 결과 사용 범위

- 선택된 수준은 v2 정상 학습·보정·검증 workload 정의 후보로만 쓴다(별도 계약·승인).
- pilot 원자료는 학습·보정·검증 데이터에 넣지 않는다.
- 발표에서 말할 수 있는 최대: "다변량 metric이 활성화되는 workload를 확인했다(또는 확인하지 못했다)".

## 8. 코드·freeze

| 파일 | 역할 |
|---|---|
| `chaos/v2-workload-ladder.yaml` | ladder·도착 규칙·seed·허용 오차 |
| `chaos/loadgen/v2_arrival.py` | 단계 하나 실행(jittered_grid_v1), 계획 함수 |
| `experiments/run_v2_workload_pilot.py` | 실행기(단계별 실행·SLO 경계 종료) |
| `experiments/v2_workload_pilot_analysis.py` | 원본 표본 저장·단계 판정·선택 규칙 |
| `experiments/test_v2_workload_pilot.py` | 테스트 |

freeze manifest `phase8-aiops-v2-workload-feasibility-freeze-manifest-v2-20261009.json`(버전 `v2wl-v2`, 자기 해시 없음). runner는 시작 전 재해시, `status = frozen`이 아니면 시작하지 않는다.

## 9. 한계

- 실제 도착률(offered load) Prometheus metric 없음 - 탐지기 입력으로 쓸 workload 맥락은 throughput뿐(포화 시 도착률보다 낮음). 새 instrumentation 필요 여부는 pilot 뒤 별도 결정.
- 활성화 판정은 scrape 순간 값(gauge) 기반 - 도착 jitter로 위상 고정은 없앴지만 단계당 20점이라 표본 오차는 남는다(L5 기준 seed 3개 평균 위상 편차 ≤ 0.1, 테스트로 확인).
- probe(1 RPS)는 고정 간격 그대로(동결 SLO 측정기). 1 s 간격은 15 s scrape와 위상 고정될 수 있으나, max_tokens=1·약 0.25 s 요청이라 기존 1 RPS 관측에서 running이 계속 0이었다(L0 기준선에서 다시 기록됨).
