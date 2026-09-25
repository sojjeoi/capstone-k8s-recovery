# Phase 8 — §137 정정 + 후속 계측 구현 + 첫 라이브 비교 1쌍 (§138)

이번 지시는 §137을 정정하고, "허용 범위와 동결선"에 따라 후속 계측을
**실제로 구현**하고, 조건이 충족되면 load_ramp에서 fixed_threshold→proposed
1쌍을 실제로 실행하는 것이었다. 아래는 요청한 8개 항목 순서 그대로다.

**동결선 준수 확인(먼저 명시)**: 기존 공식 결과(core 45건)·state·hash 수정
0건, 모델·feature·threshold·streak·cooldown·SLO 정의 수정 0건, 장애/부하
강도 변경 0건, serving 응답 헤더·프록시·라우팅·keepalive 변경 0건, 결과가
나쁘다고 재시도한 사례 0건, 이번 1쌍 이후 자동 반복 확대 0건. 처리 pod
식별(응답 헤더 등)은 이번에도 손대지 않았다.

---

## 1. §137에서 정정한 계산·분모·표현

재현: `experiments/correct_137_errors.py`(신규, 읽기 전용, `slo_judge`의
기존 함수를 `not_before`로 반복 재사용할 뿐 새 판정 로직 없음).

**A. SLO 위반 누적시간** — §137의 `viol_sec_total`은 `t_recovery-t_slo`
합계였다(= 확정된 **첫 episode 길이**의 합, 독립적으로 계산한 위반
누적시간이 아니었다). 이번에 두 가지를 분리해 다시 계산했다:

| 시나리오/arm | raw 위반시간(persistence 미적용) | 평가가능시간 | 비율 | 재위반(2+ episode) | 관측종료 시 미회복 |
|---|---|---|---|---|---|
| load_ramp/fixed_threshold | 933.0s | 2512.0s | 37.1% | 0건 | 0건 |
| load_ramp/proposed | 694.0s | 2232.0s | 31.1% | 0건 | 0건 |
| pod_kill/fixed_threshold | 859.0s | 1297.0s | 66.2% | 0건 | 0건 |
| pod_kill/proposed | 727.0s | 1166.0s | 62.3% | 0건 | 0건 |
| network_degrade/fixed_threshold | 2121.0s | 2577.0s | 82.3% | 0건 | 0건 |
| network_degrade/proposed | 1841.0s | 2301.0s | 80.0% | 0건 | 0건 |

45건 전수에서 재위반(같은 trial 안에 확정 episode가 2개 이상)이나 관측종료
시 미회복 episode는 0건 — `recovery_sec`(첫 episode)이 "그 trial의 유일한
위반 구간"이라는 전제는 이 45건에서는 실제로 성립한다(다만 이는 "재확인된
사실"이지 애초에 "당연히 그럴 것"이라고 가정해도 됐다는 뜻은 아니다).
**raw 위반시간과 recovery_sec(첫 episode)는 방향은 같지만 값이 다르다**
(예: load_ramp/fixed_threshold는 raw 933.0s vs recovery_sec 중앙값
226.17s×4건 합계와는 다른 값 - 전자는 point 단위 위반시간 합, 후자는
episode 길이) - 이 문서 이후로 "SLO 위반 누적시간"이라는 표현은 위 raw
값만 가리키고, `recovery_sec`은 계속 "첫 확정 episode의 길이"로만 부른다.

**A-2. 탐지 시각이 실제로 그 위반 안에 있었는지(신규 발견, §137에 없던
검증)** — `detection_source=predictive` 기록이 그 trial의 `recovery_sec`
을 만든 원인이라고 자동으로 가정하면 안 된다는 걸 실측으로 확인했다:

| trial | recovery_sec | t_slo~t_detection | 판정 |
|---|---|---|---|
| load_ramp-fixed_threshold-02 | 40.99s | **+410.0s** | **AFTER-RECOVERY** - 탐지가 회복 6분49초 뒤에 일어남, 이 recovery_sec과 무관할 가능성 높음 |
| load_ramp-fixed_threshold-05(retry1) | 258.77s | +121.0s | DURING - 위반 중 탐지, 기여 가능성과 시간적으로 부합 |
| load_ramp-proposed-02 | 113.98s | +19.2s | DURING |
| load_ramp-proposed-03 | 135.16s | -61.3s | BEFORE(선제) |
| load_ramp-proposed-04 | 1.10s | **+194.5s** | **AFTER-RECOVERY** - §134에서 "자기 해소성 blip"으로 이미 밝힌 그 근접-0초 값과 정확히 같은 성격 |
| load_ramp-proposed-05 | 235.87s | -35.8s | BEFORE(선제) |

**정정**: §137은 load_ramp/fixed_threshold의 "실제 개입 2건"(rep2, rep5)을
둘 다 "탐지가 회복에 기여했다"는 뉘앙스로 다뤘다. 실제로는 **rep2는 탐지가
이미 끝난 회복과 무관한, 훨씬 나중에 일어난 별개 사건**이다 - fixed_threshold
가 "탐지+실행으로 회복을 만든" 것으로 시간상 뒷받침되는 사례는 유효 4건
중 **rep5 하나뿐**이다(§0의 audit-log 버그 수정으로 rep5도 executed_verified
임을 재확인했지만, 그 실행이 41초짜리 빠른 회복을 만든 게 아니라 258.77초짜리
느린 회복 쪽이라는 뜻). proposed도 대칭적으로 rep4가 같은 성격의 "무관한
사후 탐지"임을 이번에 처음 확인했다(§134는 이 값을 "자기 해소성"으로만
설명했고 탐지 시각과의 관계까지는 안 봤다).

**B. load_ramp 전환율 분모 분리**:

| arm | 전체 유효 trial 기준 | SLO 위반 관측 trial 기준 |
|---|---|---|
| fixed_threshold | 2/5 | 2/4 |
| proposed | 5/5 | 4/4 |

`recovery_sec`가 정의되는 4건을 "유효 trial 전체"라고 부르지 않는다 -
무탐지·무개입 자연회복(rep1 `prevented` 제외, rep3/rep4처럼 위반은
있었지만 개입 없이 회복)도 기준선의 유효한 결과로 유지했다. 전환이
많다는 사실 자체를 "우수함"으로 해석하지 않는다.

**C. 요청 실패율 - "pooled(저장된 요청 기준)" 명시 + trial별 분해**:
`experiments/correct_137_errors.py`가 시나리오·arm마다 반복별 요청 수·
관측구간·실패 수·지연초과 수를 전부 분해해 출력한다(전문은 스크립트
실행 결과 참고, 지면상 요약만 남긴다) - **핵심 정정**: "실패가 약 33%
감소했다"는 §137의 표현은 **저장된 요청 기준 pooled 비율의 차이**로
한정한다. 같은 5개 trial 안의 요청 수천 건은 서로 독립적인 실험이
아니므로 "독립 시행 수천 회에서 33% 감소"처럼 읽으면 안 되고, 산업
적용 효과나 인과적 감소로 확정하지 않는다.

**D. 미완료 요청 재점검** — 마지막 저장 시각과 `t_run_end`의 격차를 45건
전수 대조했다. load_ramp는 40~104초, pod_kill/network_degrade는 34~37초
격차가 "정상"(probe 종료 후 detector.stop() graceful 대기(최대 50초)+
quiescence 확인 등 정상 정리 시간 - 확인 코드: `arm_controller.py`
`GRACEFUL_STOP_TIMEOUT_SEC=50.0`) 범위로 일관되게 나타났다. 단
**load_ramp-fixed_threshold-02(416.7s)와 load_ramp-proposed-02/04
(341.2s/345.5s)는 같은 arm의 다른 반복보다 3~4배 크다** - 조사 결과
이 편차는 A-2에서 확인한 "회복 훨씬 뒤에 탐지+전환이 다시 일어난"
바로 그 trial들과 일치한다(예: fixed_threshold-02는 t_switch가
t_recovery보다 6분 넘게 뒤) - 즉 **미완료 요청 손실이 아니라, 나중에
일어난 두 번째 탐지·전환·그 뒤 정리 절차가 t_run_end를 그만큼 늦춘
것**으로 설명된다(관측종료 미완료 요청은 45건 전수에서 여전히
"우려할 결측 없음"으로 유지, 원인만 더 정확해졌다).

**E. 과도한 인과 표현 정정**:
- "load_ramp에서는 fallback 구조적으로 불가능" → "load_ramp 15건
  전수에서 반응형 신호(`up==0` 30초)가 관측된 적이 없고, 이 규칙 자체가
  '프로세스 생사'만 보므로 순수 부하 압박형 고장(pod가 살아있는 채로
  느려짐)과는 조건이 안 맞는다"로 수정 - 극단적 부하가 이론상 health
  check까지 실패시킬 가능성 자체를 "불가능"이라고 단정하지 않는다.
- "네트워크 feature 부재가 작은 개선폭의 원인이다" → 이미 §137에서
  "구조적 설명이 가능하다/정합적이다"로 가설 수준으로 썼던 것을 이
  문서에서도 동일하게 가설로만 유지한다(확정 인과 아님).
- "양쪽에 동일 계측을 적용하면 왜곡 없음" → 정정: **동일 적용 ≠ 오버헤드
  0**. `probe_followup.py`는 원본 `probe.py`보다 요청당 쓰기 이벤트가
  하나 더 많다(발신 시 evidence 1회 추가 append+flush) - 두 arm 사이의
  **상대 비교**는 여전히 공정하지만(동일 코드, 동일 오버헤드), 이번
  후속 결과의 **절대값**을 기존 45건(원본 `probe.py`)과 직접 비교하면
  안 된다(계측 자체의 조건이 다르다).

---

## 2. 기존 원자료에서 유지되는 효과 / 아직 불확실한 효과

**유지되는 것**(위 정정을 반영해도 그대로 성립):
- 세 시나리오 모두 `recovery_sec` 중앙값은 proposed가 낮다.
- pod_kill의 요청 실패율(pooled) 격차는 recovery_sec 격차보다 크다.
- network_degrade는 두 arm 다 대부분 반응형 경로에 의존해 격차가 작다.
- 45건 전수에서 재위반·관측종료 미회복 episode는 0건(A절 재확인).

**더 불확실해진 것**(이번 정정으로 신뢰도가 낮아짐):
- load_ramp/fixed_threshold의 "실제 개입이 회복을 만들었다"는 사례는
  기존에 생각한 2건이 아니라 **최대 1건(rep5)** - rep2는 무관한 사후
  탐지였다(A-2). proposed도 4건 중 1건(rep4)이 같은 성격이라 "예측
  경로가 실제로 그 회복을 만든" 확실한 사례는 **3건(rep2/3/5)**으로
  좁아진다.
- "실패가 약 33% 감소"는 pooled 비율 차이로만 한정해야 하고(C절),
  독립시행 수천 회의 감소로 일반화할 수 없다.

---

## 3. 후속 관측 구간·요청 cohort·미완료 처리 규칙

동결한 그대로 구현·실행했다:
- 동일 주입 기준 시작점(`t_injection`), 동일 사전 고정 관측 길이
  (`timeout_sec=900`, 공식 `run_load_ramp_trial.py`와 동일값 재사용),
  동일 부하 stage 스케줄(`scenario-load-ramp.yaml` 무변경), 동일 요청
  프로필(`probe-config.yaml` 무변경, max_tokens=1 그대로).
- `run_once.py`에 **`fixed_duration_observation=False`(기본값) 추가
  파라미터 1개**로 조기종료를 껐다 - 기존 18개 호출부는 전부 이 인자를
  안 넘기므로 동작 100% 무변경(회귀 테스트 56/56 통과, 아래 4절).
- cohort: 관측 구간 내 **발신된** 요청 전체(`request_id` 기준) -
  구간 종료 뒤 완료돼도 같은 `request_id`로 계속 추적한다(evidence
  JSONL, 아래 4절).
- 미완료 처리 규칙: grace(60초) 안에도 안 끝나면 취소하고 "unresolved"로
  명시 기록 - 성공/실패/가짜 latency로 채우지 않는다(실측: fixed_threshold
  0건, proposed 1건 unresolved).
- 안전 중단은 항상 우선 - 이번 실행에서 안전 중단이 실제로 발동한 적은
  없다(두 trial 모두 정상 완료).

**실행 중 실제로 발견한 계측 공백 1건(투명 고지)**: `make_load_ramp_prober_followup()`
이 상속한 `_refresh()`(원본 `make_load_ramp_prober()`와 동일 설계)는
`check_slo_violation()`/`check_recovered()` 내부에서만 호출되는데, 이
둘은 각각 `t_slo`/`t_recovery`가 "아직 안 정해졌을 때만" 불린다. 원본
(조기종료) 설계에서는 무해하다 - 정해지자마자 루프가 break하기 때문이다.
그런데 `fixed_duration_observation=True`로 그 뒤에도 수백 초를 더
관찰하면, 로컬 raw CSV 캐시가 **그 시점 이후로 전혀 안 갱신된다** -
실측: fixed_threshold는 발신 904건 중 로컬 CSV엔 97건만 남았다(약
96초분, 그 뒤 발신분은 로컬에 없음). **원자료 자체가 사라진 건 아니다**
- evidence JSONL은 `stop()`에서 원격 파일 전체를 통째로 회수하므로
  904/905건 전부 보존됐고, 이번 분석(6절)은 전부 evidence 기준으로
  다시 계산했다. 다만 이건 **다음 반복 전에 고쳐야 할 계측 자체의
  진짜 공백**이다(8절에 반영).

---

## 4. 계측 구현 및 오프라인 검증

신규 파일(전부 §137 문서에서 이미 설계, 이번에 실제 구현):

| 파일 | 역할 |
|---|---|
| `experiments/loadgen-runner/probe_followup.py` | request_id 기반 sent/completed/timeout/error/unresolved 이벤트 분리 기록. 기존 payload/timeout(30s)/connector(`TCPConnector(limit=0)`) 전부 동일값 유지 |
| `experiments/load_ramp_followup_adapter.py` | 같은 pinned 이미지·pod 생명주기 헬퍼 재사용, probe_followup.py를 kubectl cp로 반입해 실행 + evidence 회수 |
| `experiments/followup_cost_sampler.py` | 로컬 detector PID(psutil, CPU 누적·RSS) + 클러스터 pod(`kubectl top`) 독립 주기 샘플링 |
| `experiments/run_load_ramp_followup_trial.py` | 위 전부 배선 + 코드 SHA-256 계약 기록 + `results/followup/` 별도 네임스페이스 |
| `run_once.py` | `fixed_duration_observation` 파라미터 1개 추가(기본 False) |
| `arm_controller.py` | `Detector.get_pid()` 접근자 추가(기본 None) |

**오프라인 검증**(라이브 실행 전 전부 통과):
- `test_probe_followup.py`(신규 5개, 실제 로컬 aiohttp 서버 사용, mock
  아님): 정상완료 sent/completed 쌍 일치, HTTP 500이 실패로 정확히
  기록(누락 아님), timeout이 명시적으로 기록(latency 분석에서 조용히
  제거 안 됨), **grace 만료 후 미완료 요청이 CSV엔 안 남고 evidence엔
  명시적으로 남음**(성공/실패/가짜 latency로 안 채움), request_id 중복
  생성 시 예외로 탐지.
- `test_run_once.py`(신규 2개 추가): `fixed_duration_observation=True`가
  실제로 조기종료 없이 전체 구간을 채우면서도 recovered/prevented를
  정확히 재분류하는지 확인.
- 회귀: `test_run_once.py` 56/56, `test_arm_controller.py` 36/36,
  전체 실험 스위트 763/763 통과(3 skip, 기존과 동일) - 기존 공식 경로·
  artifact·판정 로직 무변경 확인.
- 코드 해시 사전기록: 각 trial 실행 직전
  `results/followup/contract-{run_id}.json`에 관여 파일 8개의 SHA-256을
  기록(재현 가능한 증거).
- 라이브 전 최종 점검: 클러스터 연결(`kubectl cluster-info`), recovery-policy·
  Prometheus 포트포워딩, quiescence(`/admin/quiescent`=true, `/admin/experiment-run`
  =null), 클린 pod 상태(1개 active pod만 존재) - 전부 확인 후, 실제 실험
  전용 pod 1개로 실제 클러스터·실제 이미지에서 probe_followup.py를
  5초간 먼저 태워보는 스모크 테스트를 수행해(정리까지 완료) 계측 코드
  자체가 실제 환경에서 정확히 동작함을 확인한 뒤에만 본 실행에 들어갔다.

---

## 5. 첫 비교 실행 여부와 정확한 이유

**실행함.** 6개 선행조건(§137 §6에서 미충족으로 남겼던 것들)이 이번
턴에 전부 충족됐다: 계측 코드 작성 완료 → 오프라인 검증 통과 → 코드
해시 사전기록 → 클러스터 안전·연결·quiescence 확인 → 스모크 테스트로
실환경 동작 확인 → 예상 소요시간(트라이얼당 preview prep 최대 8분+
고정관측 900초+정리 ≈ 25분, 2트라이얼 순차 ≈ 50분)이 90분 이내로 추정됨.
pod 식별(서빙 응답 경로 변경)은 이번에도 승인 없이 구현하지 않았지만,
이는 §6에서 이미 "필요 없어도 진행 가능한 비교"(요청 cohort 실패·지연,
동일 구간 서비스 품질, 실제 전환 시각, 탐지기 추가 비용)로 분류돼 있어
차단 사유가 아니었다.

순서: `load_ramp/fixed_threshold` 1회 → `load_ramp/proposed` 1회
(사전 고정 순서 그대로, 두 trial만으로 우열을 판정하지 않음).

---

## 6. 실행 결과 - 실패율·지연초과율·전환·자원 비용

**주의**: n=1/arm, 우월성 판정용 표본이 아니다. 계측·프로토콜의 실행
가능성 확인과 탐색적 비교가 목적이다.

| 지표 | fixed_threshold | proposed |
|---|---|---|
| outcome | recovered | recovered |
| recovery_sec(첫 episode) | 9.91s | 10.03s |
| detection_source/detector | predictive/fixed_threshold | predictive/isolation_forest |
| 실제 전환 | 실행됨(activeSelector가 preview pod로 교체 확인) | 실행됨(동일하게 확인) |
| 관측 요청 수(발신, request_id 기준) | 904 | 905 |
| 미완료(unresolved, grace 60s 후) | 0건(0.0%) | 1건(0.11%) |
| 실패율(동일 고정 903초 구간) | 0.11%(1/904) | 0.0%(0/904) |
| latency 초과율(threshold=0.648s) | 7.85%(71/904) | 6.86%(62/904) |
| 실패∪지연초과 합집합율 | 7.85% | 6.86% |

**비용(§5 계측, 이 프로젝트 최초의 실측값)**:

| | fixed_threshold | proposed |
|---|---|---|
| detector CPU 누적 사용(관측 구간 동안) | 0.64초 | **4.47초**(약 7배) |
| detector RSS 평균/최대 | 44.8MB / 46.1MB | **189.0MB / 190.5MB**(약 4.2배) |
| detector 표본 수집 누락 | 13/181(~7%) | 15/181(~8%) |
| active/preview pod CPU 평균(mCPU) | 구·신 각각 1074.8 / 684.1 | 구·신 각각 798.2 / 1093.3 |
| 공통 인프라(주입기+probe pod) CPU 평균 | 11.2 + 17.6 mCPU | 11.3 + 18.0 mCPU |

**해석(3단계 그대로 유지)**:
- **관측된 사실**: 이 1쌍에서는 recovery_sec이 사실상 동일했고(9.9 vs
  10.0초), latency 초과율은 proposed가 조금 낮았고(6.86 vs 7.85%),
  detector 쪽 CPU·메모리 사용은 proposed가 명확히 더 컸다(약 4~7배,
  단 절대값 자체는 둘 다 작다 - 초 단위 CPU, 수십~2백MB 메모리).
- **부합하는 가설**: Isolation Forest가 CPU 임계치 검사보다 평가당
  더 많은 연산(스케일링·모델 추론)을 하므로 이 비용 차이는 방식의
  구조적 차이와 부합한다.
- **미검증 주장**: 이 1쌍만으로 "proposed가 항상 더 적은 latency
  초과율을 낸다"거나 "이 비용 차이가 일반적인 값이다"라고 결론 내릴
  수 없다(n=1, 단일 관측시점의 자원 수치일 뿐).

---

## 7. 결과 완결성 및 클러스터 cleanup

- 두 trial 모두 `state=completed`, `outcome=recovered`로 정상 종료됨 -
  강제 절단 없음, 안전 중단 발동 없음.
- 각 trial 직후 `kubectl get pods -n vllm-serving`을 확인 - preview→
  active 전환이 완료된 뒤 구 pod가 정리되고 신 pod 1개만 남는 정상
  BlueGreen 종료 상태를 두 번 다 확인했다.
- 최종 상태: `vllm-serving` 네임스페이스에 `recovery-policy` +
  `vllm-serving`(단일 active) pod 2개만 존재, `/admin/quiescent`=true,
  `/admin/experiment-run`=null - 다음 실행을 위한 정상 대기 상태.
- 결과 파일은 전부 `results/followup/`(별도 네임스페이스, `plan_id=
  post_hoc_followup-v1`)에만 저장됐다 - 기존 core 45건과 물리적으로
  분리돼 있고 `load_authoritative()`의 core 집합에도 포함되지 않는다
  (합산 안 됨, 구조적으로 보장).

---

## 8. 추가 반복 필요성과 예상시간

첫 쌍은 계측·프로토콜의 실행 가능성 확인과 탐색적 비교이지, 우월성
판정용 표본이 아니다(결과가 close했다는 사실 자체도 반복 수 결정에
쓰지 않는다). 반복을 늘리기 **전에** 먼저 고쳐야 할 것이 하나 있다:

**필수 선행 조치**: 3절에서 발견한 `_refresh()` 공백을 고친다 - 가장
작은 수정은 `stop()`이 pod 삭제 전에 raw CSV도 evidence처럼 마지막으로
한 번 더 전체 회수하는 것(이미 evidence는 그렇게 하고 있어 동일 패턴
재사용 - 새 로직 아님). 이번 결과 자체는 evidence 기준으로 이미
정확하게 재계산했으므로 이 공백 때문에 6절의 수치를 다시 낼 필요는
없지만, "raw CSV가 SLO 판정의 유일한 근거"인 기존 `slo_judge` 파이프라인
전체가 장시간 고정관측에서 이 공백에 영향을 받는지는 별도로 재검토가
필요하다(이번 두 trial은 `check_slo_violation`/`check_recovered`가
필요한 시점—판정 전—까지는 정상적으로 매번 최신 데이터를 봤으므로
`t_slo`/`t_recovery` 판정 자체는 영향 없었다, 영향은 판정 "이후"의
장기 관측 구간에서 로컬 캐시 활용에만 국한된다).

**반복 계획(고정, 결과에 따라 바꾸지 않음)**: 위 수정과 재검증을 거친
뒤, 탐지 성공/실패와 무관하게 보존하는 고정 반복 수 5회/arm(이 프로젝트
전체의 n=5 관례와 동일 규모)을 제안한다. arm 순서는 세션마다 교차
(이번은 fixed→proposed, 다음은 proposed→fixed)한다. 예상 소요시간:
트라이얼당 preview prep(최대 8분)+고정관측(900초)+정리(~2분) ≈ 25분,
90분 세션 예산 안에서 **세션당 최대 3~4trial**(교차 순서 유지 고려 시
세션당 짝수 개가 자연스러움 - 세션당 2쌍=4trial 권장) - 5회/arm(총
10trial) 완성에는 **약 3세션** 필요 예상. 이번 승인으로 자동 확대하지
않는다 - 위 선행 조치 완료 후 별도 승인을 받는다.

---

*작성: 2026-09-25. 재현: `python experiments/correct_137_errors.py`,
`python experiments/run_load_ramp_followup_trial.py --arm <arm> --rep 1`,
`python experiments/analyze_followup_results.py`(전부 `experiments/`
작업 디렉터리 기준). 원본 core 45건·모델·SLO 정의·공식 하니스 기본
동작 전부 미변경.*
