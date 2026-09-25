# Phase 8 — rep03 한 쌍 완료(fixed_threshold 재시도 + proposed) (§147)

§140~§146 그대로 보존한다(수정 0건). 이번 턴에 **사전등록된 load_ramp
후속 비교 rep03 한 쌍을 실제로 완료했다** - fixed_threshold rep03
기술적 재시도(신규 attempt) 1회, 이어서 proposed rep03(최초 시도) 1회.
**rep04/05는 시작하지 않았다.**

---

## 0. 실행 전 확인(전부 통과, 아래 순서로 확인)

| 확인 항목 | 결과 |
|---|---|
| `HEAD == origin/master == 192300b` | 확인(로컬/원격 정확히 일치, 작업 트리 clean) |
| 기존 결과 경로 | rep01/rep02 8개 파일 + rep03 원본 invalid 3개 파일 전부 존재 확인 |
| 자원 시작 기준(현재 값, §146 수치 재사용 안 함) | commit headroom 27,785~28,735MB(3회 측정, 기준 4,096MB의 약 7배), free RAM 4,395~4,942MB(기준 2,048MB의 약 2배) - 신규 측정 |
| 클러스터 연결 | `kubectl get nodes` 정상(sj-control/sj-worker 둘 다 Ready) |
| Rollout/활성 endpoint | DESIRED=CURRENT=AVAILABLE=1, active pod 1개(`vllm-serving-5979bb6496-s7jns`) |
| quiescence | `{"quiescent":true,"active_count":0}` |
| 잔여 실험 자원 | pod 정확히 2개(recovery-policy+vllm-serving), 여분 preview/canary 없음 |
| 필수 port-forward | recovery-policy(8080)·Prometheus(9090) 둘 다 새로 기동 + healthz/healthy 확인 |
| 지표 신선도 | vLLM CPU 메트릭 0.2초 전 스크레이프(기준 120초 이내) |
| 모델·계약·계측 코드 hash | `arm_controller.py`/`load_ramp_adapter.py`/`slo_judge.py` - rep01 이후 **완전 동결**(hash 100% 동일). `load_ramp_followup_adapter.py`/`probe_followup.py`/`followup_cost_sampler.py` - rep02 이후 **완전 동결**(rep01만 §139 drain-race 수정 이전 버전 - 이미 §139에 문서화된 차이, 새로 발견된 문제 아님). `run_once.py`/`run_load_ramp_followup_trial.py` - 이번 세션에서 의도적으로 수정(§144, 진단로그+provenance, 회귀 0건 검증됨) |
| 900초 cohort·부하 설정 | `timeout_sec=900, fixed_duration_observation=true` - rep01/02/03 전부 동일(계약 파일로 확인) |
| 원본 invalid fixed_threshold rep03 | `trial-load_ramp-fixed_threshold-03-post_hoc_followup-v1.json` sha256=`39ebb009a666f0cc5be4ba3b08a157175d06dacf37768ba892ed398df82ea290` - 확인·보존(이번 턴에도 미수정) |
| 신규 attempt ID 충돌 | `-retry1-` 접미사 파일 사전 미존재 확인 |

기준 미달이나 예상 밖 잔여 상태 없음 - 그대로 진행했다.

**사전 실무 메모**: `--dry-run-contract-only`로 provenance를 오프라인
검증하는 과정에서 그 자체가 contract 파일을 실제로 써서, 뒤이은 진짜
실행이 내가 §144에 만든 충돌 방지 로직에 걸릴 뻔했다 - dry-run이 만든
그 계약 파일 1개만 삭제하고(이번 세션에 내가 그 목적으로 만든 파일,
원본과 무관) 진짜 실행을 시작했다.

---

## 1. 신규 attempt ID + provenance 기록

§144 절차 그대로 사용: `python run_load_ramp_followup_trial.py --arm
fixed_threshold --rep 3 --attempt-of load_ramp-fixed_threshold-03-post_
hoc_followup-v1 --attempt-suffix retry1 --replacement-reason "..."`.

결과 `contract-load_ramp-fixed_threshold-03-retry1-post_hoc_followup-
v1.json`에 기록됨:
```json
"replacement": {
  "attempt_of_run_id": "load_ramp-fixed_threshold-03-post_hoc_followup-v1",
  "original_result_sha256": "39ebb009a666f0cc5be4ba3b08a157175d06dacf37768ba892ed398df82ea290",
  "same_logical_rep": {"scenario": "load_ramp", "arm": "fixed_threshold", "rep": 3, "plan_id": "post_hoc_followup-v1"}
}
```
`original_result_sha256`가 사전에 독립적으로 계산해 둔 해시와 정확히
일치함을 확인(이중 확인). **원본 invalid 파일·contract는 전혀 수정하지
않았고, "completed"로 바꾸지도 않았다** - 원본은 여전히
`state=invalid`, `outcome=invalid_run`으로 그대로 남아있다. 충돌
0건(새 run_id가 완전히 별도 파일 세트를 만듦).

`proposed rep03`은 원본 시도 자체가 없었다(§142가 fixed_threshold
실패 직후 멈춰서 proposed rep03은 애초에 한 번도 실행된 적이 없음) -
그래서 표준 run_id(`load_ramp-proposed-03-post_hoc_followup-v1`, 접미사
없음)로 실행했다. 대체가 아니라 최초 시도이므로 `--attempt-of`를
쓰지 않았다.

---

## 2~3. 실행 결과 + 검증

### fixed_threshold rep03(재시도)

`outcome=recovered, state=completed` - **이번엔 무효화 없이 정상
완료됐다**(WinError 1455 재발 없음). 검증 전부 통과:
- 900초 cohort: 발신 900 / 완료 900 / 미완료 0 / 중복 0 / 이상 0
- raw CSV 908줄(헤더+907행, 907.001초 경과와 부합), evidence 마지막
  이벤트 `send_stopped(reason=stop_file, elapsed_sec=907.001)` - 이건
  **매 trial 정상 종료 때마다 쓰는 표준 drain 신호**이지 비상 중단이
  아니다(§139에서 만든 매커니즘, 이번엔 정상 경로로만 사용됨)
- 비용 표본: 총 182개, 진짜 수집실패 0개, 정상종료후꼬리 14개(정상)
- audit: `audit_status=complete`, `decision_outcome=executed_verified`,
  기록 1건(중복 없음)
- postflight: Rollout 정상, pod 2개(새 active pod로 교체됨, 잔여
  preview 없음), quiescent=true

### proposed rep03(최초 실행)

`outcome=recovered, state=completed`. 검증 전부 통과:
- 900초 cohort: 발신 900 / 완료 900 / 미완료 0 / 중복 0 / 이상 0
- raw CSV 905줄, evidence 마지막 이벤트 `send_stopped(reason=stop_file,
  elapsed_sec=904.001)` - 정상 종료
- 비용 표본: 총 180개, 진짜 수집실패 0개, 정상종료후꼬리 14개(정상)
- audit: `audit_status=complete`, `decision_outcome=executed_verified`,
  기록 6건(1 executed_verified + 5 skipped_duplicate - recovery-policy의
  idempotency 구조, rep02 proposed와 같은 종류의 패턴이나 정확한
  건수는 다름 - rep02는 총 7건(중복 6), rep03은 총 6건(중복 5))
- postflight: Rollout 정상, pod 2개(새 active pod), quiescent=true

**proposed 실행 조건 재확인**: fixed_threshold retry가 검증까지 전부
통과했고, 자원(26.9GB 헤드룸, 안정)과 시간(경과 약 40분, 남은 예산
충분) 모두 다음 trial을 정상 cleanup까지 마칠 여유가 있어 진행했다.

---

## 4. rep01·rep02·rep03 나란히 (전부 동일 분석 파이프라인으로 재계산 - 요약이 아니라 재산출값)

| | rep01 fixed | rep01 proposed | rep02 fixed | rep02 proposed | **rep03 fixed(재시도)** | **rep03 proposed** |
|---|---|---|---|---|---|---|
| 발신/완료(900s) | 900/900 | 900/900 | 900/900 | 900/900 | 900/900 | 900/900 |
| 미완료 | 0 | 0 | 0 | 0 | 0 | 0 |
| 실패 | 1(0.11%) | 0(0.00%) | 1(0.11%) | 1(0.11%) | 1(0.11%) | 1(0.11%) |
| 지연초과 | 69(7.67%) | 60(6.67%) | 51(5.67%) | 54(6.00%) | **43(4.78%)** | **93(10.33%)** |
| 확정 episode 수 | 2 | 2 | 2 | 3 | 2 | 2 |
| raw 위반초(900s 전체) | 331.0s | 350.0s | 379.0s | 378.0s | 266.0s | 413.0s |
| 첫 episode recovery_sec | 9.91s | 10.03s | 30.09s | 1.03s | 2.93s | 103.30s |
| 탐지-회복 시간관계 | DURING ep2 | DURING ep2 | DURING ep2 | 어느 ep도 아님 | DURING ep2 | **BEFORE ep1(선제)** |
| detection_source | predictive | predictive | predictive | predictive | predictive | predictive |
| decision_outcome | executed_verified | executed_verified | executed_verified | executed_verified | executed_verified | executed_verified |
| 감사기록(총/실행/중복) | 1/1/0 | 4/1/3 | 1/1/0 | 7/1/6 | 1/1/0 | 6/1/5 |
| CPU(관측 900s) | 0.640s | 3.703s | 0.656s | 1.906s | 0.734s | 5.343s |
| 메모리 평균 | 44.8MB | 189.7MB | 45.0MB | 176.4MB | 45.8MB | 186.6MB |
| 메모리 최대 | 46.1MB | 190.5MB | 46.1MB | 190.5MB | 46.2MB | 190.1MB |

**fixed−proposed 차이(반복별, 합산 아님)**:

| | 실패율 차이 | 지연초과율 차이 | 유리한 쪽(지연초과 기준) |
|---|---|---|---|
| rep01 | +0.11%p | +1.00%p | proposed |
| rep02 | 0.00%p | −0.33%p | fixed(근소) |
| rep03 | 0.00%p | **−5.55%p** | **fixed(뚜렷)** |

**해석상 명시적 제약(지시 그대로 준수)**: rep01/02/03은 서로 다른
시각에 순차 실행된 시도다 - 동시 실행된 반사실 비교가 아니다(기존
제약 그대로). **이번 rep03의 fixed_threshold 우위를 일반적 우월성으로
결론 내리지 않는다** - 세 반복의 방향이 이미 다르게 나왔다(rep01은
proposed 유리, rep02는 근소하게 fixed 유리, rep03은 뚜렷하게 fixed
유리) - 이 자체가 "결론을 내리기엔 반복 간 일관성이 부족하다"는
근거이지, 어느 한 반복(특히 유리해 보이는 rep03)만 골라 결론 내릴
근거가 아니다. 유리한 trial만 합산하지 않는다.

---

## 5. 자원 headroom — 시작·최저·종료

| 시점 | Commit headroom | 비고 |
|---|---|---|
| 시작(라이브 실행 전 최종 게이트 확인) | 27,785MB | 23:15:43 KST |
| 최저(관측된 전체 구간 중) | 25,738MB | proposed 관측 중(23:57경) - 여전히 시작기준의 6배, 중단기준(1,536MB)과는 큰 거리 |
| 종료(cleanup 후) | 26,509MB | 00:12:46 KST(2026-09-26) |

5분 간격 모니터링 총 11회 확인(fixed_threshold 5회 + proposed 6회) -
**중단기준(1.5GB) 위반 0건**, 안전 중단 발동 0건. 전체 구간에서 헤드룸이
2GB 이상 벗어난 적이 없을 만큼 안정적이었다(최고 28,735MB - 최저
25,738MB, 편차 약 3GB, 여전히 기준 대비 큰 여유).

---

## 6. 클러스터·로컬 프로세스 cleanup

**클러스터**: 두 trial 모두 postflight 확인 - Rollout DESIRED=CURRENT=
AVAILABLE=1, pod 정확히 2개(recovery-policy + 최신 active vllm-serving,
매 trial의 promotion으로 pod 이름이 바뀜 - 정상), quiescent=true,
active_count=0. 잔여 preview/canary pod 없음.

**로컬**: 이번 턴에 시작한 백그라운드 프로세스 3개(recovery-policy
port-forward, Prometheus port-forward, 자원 모니터) **전부 명시적으로
종료했다**(`TaskStop`) - 종료 후 `curl localhost:8080`/`localhost:9090`
둘 다 connection refused로 확인. 다른 프로세스는 건드리지 않았다(엄격한
권한 범위 그대로 유지).

**git**: recovery-policy가 비동기로 push한 audit-log 커밋 9개(`audit-
log/load_ramp-fixed_threshold-03-retry1-...jsonl` 1건, `audit-log/
load_ramp-proposed-03-...jsonl` 6건, `adhoc.jsonl` 2건)를 `git pull
origin master --no-edit`로 정상 병합(내 작업과 파일 겹침 0건, fast-
forward). 이 문서는 그 병합 위에서 커밋한다.

---

## 7. 실제 소요 시간

재시작 후 자원 재확인 시작(23:07 KST)부터 최종 cleanup 완료(00:15
KST, 2026-09-26)까지 총 **약 68분**(자정 경계 포함):
- 라이브 실행 전 확인(포트포워드 기동, 클러스터/자원/해시 확인): 약 14분
- fixed_threshold 재시도 trial: 약 22분(23:21:44 시작 ~ 23:43:44 완료,
  파일 타임스탬프 기준)
- 중간 검증(cohort·비용·audit·postflight): 약 4분
- proposed trial: 약 21분(23:47:21 시작 ~ 완료 알림 수신 15:08 UTC대)
- 최종 검증 + git merge + cleanup + 이 문서 작성: 약 7분

**세션 전체(준비+2 trial+검증+cleanup) 약 68분 - 90분 한도 이내.**

---

## 8. rep04 진행 전 확인해야 할 사항 (진행하지 않음)

**이번 턴에 rep04를 실행하지 않는다** - 승인 범위는 rep03 한 쌍뿐이었고,
그 쌍은 완결됐다. 다음에 rep04(사전등록 순서상 proposed→fixed_
threshold)를 진행하려면:
1. 이번 rep03의 fixed_threshold 우위가 유난히 컸던 이유(선제 탐지
   여부·episode 패턴 차이 등)를 성능적으로 더 파고들지, 아니면 반복
   수를 그대로 늘려 재현성만 볼지 - 이건 이 문서의 범위 밖(운영/연구
   판단)이다.
2. 라이브 세션 시작 전에 이번과 동일한 자원 게이트 재확인(스냅샷 1개로
   판단하지 않기)를 다시 수행해야 한다 - 이번 통과가 다음 실행을
   자동으로 보장하지 않는다.
3. §139에서 문서화된 rep01-vs-rep02/03의 코드 버전 차이(drain-race
   수정)를 다시 상기하고, 분석 시 rep01을 다른 반복과 섞어 평균 내지
   않는다.

---

*작성: 2026-09-26 00:1x KST. 모델·feature·threshold·SLO·정책·부하
강도·라우팅·분석 지표 변경 0건. rep01·rep02·rep03(원본 무효 시도 +
이번 재시도)의 모든 결과·state·CSV·evidence·cost·contract·audit·
detector 로그 전부 보존 - 삭제·덮어쓰기 없음. 늦게 도착한 예약 메시지로
인한 중복 실행 없음(이번 턴 전체가 사용자의 명시적 라이브 실행 승인에
대한 직접 응답이었다).*
