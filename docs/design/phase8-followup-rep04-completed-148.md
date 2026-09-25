# Phase 8 — rep04 한 쌍 완료(proposed → fixed_threshold, 사전등록 순서) (§148)

§140~§147 그대로 보존한다(수정 0건 - rep03 원본 invalid·retry1·비교
결과 전부 그대로 유지, §147의 결론도 재해석하지 않는다). 이번 턴에
**사전등록된 rep04 한 쌍을 완료했다** - proposed rep04 → fixed_
threshold rep04(사전등록 순서 그대로, rep03과는 반대 순서). **rep05는
시작하지 않았다.**

모델·threshold·정책·부하·분석 기준은 이번 실행 전후로 전혀 조정하지
않았다(§147의 rep03 결과를 "설명하기 위해" 아무것도 바꾸지 않음 -
지시 그대로).

---

## 0. 실행 전 확인(전부 통과)

| 확인 항목 | 결과 |
|---|---|
| git | `HEAD==origin/master==ce4bde0`, 작업 트리 clean |
| 자원 게이트(신규 측정, §147 수치 재사용 안 함) | commit headroom 26,176~26,304MB(3회, 기준 4,096MB의 약 6.4배), free RAM 5,556~5,665MB(기준 2,048MB의 약 2.7배) |
| 클러스터 연결 | 두 노드 Ready |
| Rollout/Endpoint | DESIRED=CURRENT=AVAILABLE=1, active pod 1개 |
| quiescence | `{"quiescent":true,"active_count":0}` |
| 잔여 리소스 | pod 정확히 2개, 여분 없음 |
| port-forward | recovery-policy(8080)·Prometheus(9090) 재기동 + healthz/healthy 확인 |
| 지표 신선도 | vLLM CPU 메트릭 0.3초 전 |
| 모델·계측 코드 hash | rep03 contract와 8개 파일 전부 hash 동일 확인(`ALL_IDENTICAL`) - 이번 세션에 코드 변경 없었으므로 당연한 결과지만 실제로 재확인함 |
| 부하 설정 | `timeout_sec=900, fixed_duration_observation=true` 동일 |
| rep03 원본 invalid + retry1 보존 확인 | 3개 파일(원본/retry1/proposed) sha256 재확인 - 원본 invalid는 §147과 동일 해시(`39ebb0...`)로 불변 |
| rep04 run_id 충돌 | 사전 미존재 확인(둘 다) |

기준 미달·예상 밖 잔여 상태 없음.

---

## 1~3. 실행·검증

### proposed rep04 (사전등록 순서상 1번째)

`load_ramp-proposed-04-post_hoc_followup-v1`, `outcome=recovered,
state=completed`. 검증 전부 통과: 900/900 cohort(미완료·중복·이상
0), raw CSV 906줄, evidence 정상 drain(`stop_file, elapsed_sec=
905.001`), 비용 표본 180개(진짜 수집실패 0), audit `complete`/
`executed_verified`, postflight 정상(Rollout·pod 2개·quiescent).

### fixed_threshold rep04 (사전등록 순서상 2번째, 1번째 검증 통과 후 시작)

`load_ramp-fixed_threshold-04-post_hoc_followup-v1`, `outcome=
recovered, state=completed`. 검증 전부 통과: 900/900 cohort(미완료·
중복·이상 0), raw CSV 905줄, evidence 정상 drain(`stop_file, elapsed_
sec=904.001`), 비용 표본 181개(진짜 수집실패 0), audit `complete`/
`executed_verified`, postflight 정상.

두 trial 모두 유효한 무탐지·재위반·성능 악화 없이 정상 완료됐다 -
중단·재시도 사유 자체가 발생하지 않았다.

---

## 4. rep01~rep04 나란히 (동일 분석 파이프라인, 8개 trial 전부 재산출값)

| | rep01 fixed | rep01 proposed | rep02 fixed | rep02 proposed | rep03 fixed(재시도) | rep03 proposed | rep04 fixed | rep04 proposed |
|---|---|---|---|---|---|---|---|---|
| 발신/완료(900s) | 900/900 | 900/900 | 900/900 | 900/900 | 900/900 | 900/900 | 900/900 | 900/900 |
| 미완료 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 실패 | 1(0.11%) | 0(0.00%) | 1(0.11%) | 1(0.11%) | 1(0.11%) | 1(0.11%) | 1(0.11%) | 1(0.11%) |
| 지연초과 | 69(7.67%) | 60(6.67%) | 51(5.67%) | 54(6.00%) | 43(4.78%) | 93(10.33%) | 68(7.56%) | 63(7.00%) |
| 확정 episode 수 | 2 | 2 | 2 | 3 | 2 | 2 | 2 | 2 |
| raw 위반초(900s) | 331.0s | 350.0s | 379.0s | 378.0s | 266.0s | 413.0s | 367.0s | 335.0s |
| 첫 episode recovery_sec | 9.91s | 10.03s | 30.09s | 1.03s | 2.93s | **103.30s** | 11.07s | 2.07s |
| 탐지-회복 시간관계 | DURING ep2 | DURING ep2 | DURING ep2 | 어느ep도아님 | DURING ep2 | **BEFORE ep1(선제)** | DURING ep2 | DURING ep2 |
| detection_source | predictive | predictive | predictive | predictive | predictive | predictive | predictive | predictive |
| decision_outcome | executed_verified | executed_verified | executed_verified | executed_verified | executed_verified | executed_verified | executed_verified | executed_verified |
| 감사기록(총/실행/중복) | 1/1/0 | 4/1/3 | 1/1/0 | 7/1/6 | 1/1/0 | 6/1/5 | 1/1/0 | 4/1/3 |
| CPU(관측 900s) | 0.640s | 3.703s | 0.656s | 1.906s | 0.734s | 5.343s | 0.891s | 7.500s |
| 메모리 평균 | 44.8MB | 189.7MB | 45.0MB | 176.4MB | 45.8MB | 186.6MB | 44.9MB | 188.7MB |
| 메모리 최대 | 46.1MB | 190.5MB | 46.1MB | 190.5MB | 46.2MB | 190.1MB | 46.0MB | 190.4MB |

**fixed−proposed 차이(반복별 %p, 합산 아님 - fixed% − proposed%, 지연초과율은 낮을수록 유리)**:

| | 실패율 차이 | 지연초과율 차이 | 지연초과 기준 유리한 쪽 |
|---|---|---|---|
| rep01 | +0.11%p | +1.00%p | proposed |
| rep02 | 0.00%p | −0.33%p | fixed(근소) |
| rep03 | 0.00%p | **−5.55%p** | **fixed(뚜렷)** |
| rep04 | 0.00%p | **+0.56%p** | proposed(근소) |

**4개 반복 전체를 합산한 단일 요청비율로 우열을 판단하지 않는다** -
반복별 방향 자체가 proposed 유리(rep01) → fixed 근소 유리(rep02) →
fixed 뚜렷 유리(rep03) → proposed 근소 유리(rep04)로 **일관되지
않는다.** rep03에서 컸던 격차가 rep04에서 사실상 사라지고 오히려
반대 방향으로 근소하게 기울었다는 사실 자체를 그대로 기록한다 -
어느 쪽이 "진짜" 우수한지 결론 내리지 않는다.

---

## 5. rep03 격차 설명 — 기존 원자료로 확인되는 사실 vs 가설

**이번 턴에 새 진단 실험이나 코드 변경을 하지 않았다** - 아래는 전부
이미 §147까지 수집·보존된 원자료(rep01~04의 trial JSON/cohort/cost
분석 결과)를 다시 읽어 계산한 것이지, 새로 측정한 것이 아니다.

**확인된 사실(원자료에서 직접 읽음)**:
1. proposed rep03의 첫 episode recovery_sec(103.30초)은 **이번까지
   수집된 8개 trial(rep01~04, 양 arm) 중 유일하게 두 자릿수 후반~세
   자릿수** - 나머지 7개는 전부 1.03~30.09초 범위 안에 있다. 명백한
   이상치.
2. proposed rep03의 탐지 시점(`BEFORE 첫 episode`)은 **8개 trial 중
   유일하게 "선제 탐지"로 분류된 경우** - 나머지는 전부 `DURING 2번째
   episode` 또는 `어느 episode에도 속하지 않음`이다.
3. proposed rep03에서 탐지(`t_detection`)→전환(`t_switch`)까지는
   6.07초로 빨랐다(14:54:23.30→14:54:29.37) - **전환 결정·실행 자체는
   느리지 않았다.**
4. 그런데 전환(`t_switch`=14:54:29.37) 이후 실제 SLO 회복(`t_recovery`
   =14:56:13.86)까지는 **104.49초가 더 걸렸다** - 즉 이번 trial의
   긴 recovery_sec은 "탐지가 늦어서"도 "전환 결정이 늦어서"도 아니고,
   **전환이 실행된 이후 실제로 SLO 위반이 멈추기까지의 구간**에서
   대부분 발생했다.
5. `decision_outcome=executed_verified`로 조치 자체는 실행·검증까지
   끝났다 - 조치가 누락되거나 감사에서 실패로 잡힌 게 아니다.

**가설(미확인 - 기존 자료만으로는 검증 불가, 이번 범위에서 추가
조사하지 않음)**:
- 승격된 새 pod가 실제로 SLO를 만족할 만큼 요청을 처리하기까지
  워밍업(모델 로딩·KV 캐시 등) 시간이 필요했을 가능성 - 이걸 확인하려면
  해당 pod의 준비 상태(readiness probe) 이력이나 vLLM 자체의 시작 로그가
  필요한데, 이번에 수집된 원자료에는 없다.
- 우연한 단일 trial 변동(표본 수 1개 - 반복 측정으로 재현성을 확인한
  적 없음)일 가능성 - 배제할 수 없다.
- 선제 탐지 자체가 "아직 덜 진행된" 위반 상태에서 조치를 걸어, 오히려
  전환 과정 자체(예: 이전 pod에서 새 pod로 트래픽이 옮겨가는 과도기)와
  자연 위반 진행이 겹쳤을 가능성 - 이것도 검증 안 됨.

이 중 어느 가설도 확정하지 않는다. **위 "확인된 사실" 4개(특히 3·4번의
탐지→전환은 빠르고 전환→회복이 느렸다는 시간 분해)는 그대로
인용해도 되는 확정 사실이지만, "왜 그랬는가"는 미확인 상태로 남긴다.**

---

## 6. rep01 계측 코드 버전 차이 (계속 표시, 판정 기준 불변)

§139에서 이미 문서화된 그대로: rep01은 `load_ramp_followup_adapter.py`
/`probe_followup.py`/`followup_cost_sampler.py`의 §139 drain-race
수정 **이전** 버전으로 실행됐고, rep02·rep03·rep04는 전부 그 수정
**이후** 버전(hash 동일)으로 실행됐다. 이 차이를 이유로 rep01을
표에서 빼거나 다른 방식으로 집계하지 않는다 - **사전 고정된 지표별
포함 기준(전부 포함, 별도 표시만)을 결과를 보고 나서 바꾸지 않았다.**
4절 표·차이표 모두 rep01 포함 그대로 유지.

---

## 7. 자원 headroom — 시작·최저·종료

| 시점 | Commit headroom | 비고 |
|---|---|---|
| 시작 | 26,304MB | 00:22:13 KST(3회 측정 중 마지막) |
| 최저 | 24,889MB | fixed_threshold rep04 관측 중(약 00:58 KST) |
| 종료 | 25,522MB | 01:12:17 KST |

5분 간격 모니터링 총 12회(proposed 5회 + fixed_threshold 5회, 2개
trial 경계에서 중복 확인 포함) - **중단기준(1.5GB) 위반 0건.** 전체
구간 편차 약 1.4GB(24,889~26,304MB) - rep03보다도 더 안정적이었다.

---

## 8. 클러스터·로컬 cleanup

**클러스터**: 두 trial 모두 postflight 확인 - Rollout 정상, pod
정확히 2개(promotion마다 active pod 이름 교체, 정상), quiescent=true.

**로컬**: 이번 턴에 새로 시작한 백그라운드 프로세스(port-forward 2개+
자원 모니터 2개) **전부 명시적으로 종료**(`TaskStop`) - 종료 후
8080/9090 connection refused 확인. 다른 프로세스는 건드리지 않았다.

**git**: recovery-policy의 비동기 audit-log 커밋 7개(`load_ramp-
proposed-04-...jsonl` 4건, `load_ramp-fixed_threshold-04-...jsonl`
1건, `adhoc.jsonl` 2건)를 `git pull --no-rebase`로 정상 병합(겹침
0건, fast-forward).

---

## 9. 실제 소요 시간

00:21 KST(사전 확인 시작) ~ 01:12 KST(최종 cleanup+자원 확인 완료) -
총 **약 51분**: 사전 확인 약 10분, proposed rep04 trial 약 22분,
중간 검증 약 4분, fixed_threshold rep04 trial 약 22분(관측 시작
~00:47, 완료 확인 ~01:09) - 정확히는 전체가 서로 겹치며 진행됐으므로
칼같이 구간을 나누기보다 "사전확인 시작~최종cleanup"의 총 경과로
보고한다. **90분 한도 이내.**

---

## 10. rep05 진행 전 확인해야 할 사항 (진행하지 않음)

**이번 턴에 rep05를 실행하지 않는다** - 승인 범위는 rep04 한 쌍뿐이고,
완결됐다. 사전등록 순서상 rep05는 `fixed_threshold→proposed`(rep03과
같은 순서). 다음 진행 전:
1. 4개 반복 모두에서 방향이 일관되지 않았다는 사실(4절) - rep05
   결과가 어느 쪽으로 나오든 "드디어 결론이 난다"는 기대로 접근하지
   않는다. 반복 수를 얼마나 더 늘려야 방향성 있는 결론(또는 "방향성
   없음"이라는 결론)을 내릴 수 있는지는 이 문서 범위 밖의 연구설계
   판단이다.
2. 라이브 시작 전 자원 게이트를 이번과 동일하게(스냅샷 하나로 판단
   하지 않고) 재확인해야 한다 - 이번 통과가 다음을 보장하지 않는다.
3. rep01의 계측 코드 버전 차이(6절)를 분석에 계속 반영해야 한다.

---

*작성: 2026-09-26 01:1x KST. 모델·feature·threshold·SLO·정책·부하
강도·라우팅·분석 지표 변경 0건. rep01~04(rep03 원본 무효 시도 포함)의
모든 결과·state·CSV·evidence·cost·contract·audit·detector 로그 전부
보존 - 삭제·덮어쓰기 없음. 늦게 도착한 예약 메시지로 인한 중복 실행
없음(이번 턴 전체가 사용자의 명시적 rep04 실행 승인에 대한 직접
응답이었다).*
