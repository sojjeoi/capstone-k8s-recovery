# Phase 8 — 중복 차단 해석 보완 + rep03 실행 결과(차단됨) (§142)

§141은 그대로 보존한다(수정하지 않음, 아래는 보완·신규 실행 기록).
이번 턴은 (1) §141의 `skipped_duplicate` 해석을 기존 저장 데이터 범위
안에서만 보완하고, (2) 동결된 계약대로 rep03 한 쌍을 시도했으나
**첫 trial에서 로컬 머신 자원 문제로 무효화돼 두 번째 trial을 시작하지
않고 멈췄다.**

---

## 1. 중복 차단(`skipped_duplicate`) 해석 보완

§141이 원본 그대로 유지한 사실(재확인, 변경 없음):
- proposed rep02에서 추가 `anomaly_risk` 신호 6건이 발생했다.
- 전부 같은 `idempotency_key`(`{run_id}:anomaly_risk`)로 차단됐다
  (`skipped_duplicate`).
- 실제 추가 전환(promotion)은 실행되지 않았다.

**코드로 확인한 사실(신규 보완)**: `recovery-policy/main.py`의
`process_signal()`은 **가장 먼저** `safety.check_and_reserve(idempotency_key)`
를 확인하고, 실패하면(`skipped_duplicate`) 그 즉시 반환한다 - `is_paused_
pre_promotion()`(preview 준비 확인)도, `policy.decide()`(정책 판단)도
**이 시점 이후에야 실행된다.** 즉 중복 차단된 신호는 그 뒤의 어떤
게이트도 코드상 아예 실행하지 않는다.

**따라서**: "중복 차단을 통과했다면 실행 가능한 preview가 있었는지"
"정책의 다른 안전 게이트를 통과했을지"는 **이 시스템의 기존 기록에
원리적으로 존재할 수 없는 정보**다 - 조사가 부족해서가 아니라, 그
코드 경로 자체가 실행된 적이 없기 때문이다. 이걸 확인하기 위한 새
실험이나 로깅 기능은 이번에 만들지 않았다(지시대로).

**이미 저장된 자료 범위 안에서 확인 가능했던 것**(신규 실험 아님 -
기존 비용 계측이 독립적으로 수집한 `kubectl top pod` 이력을 이 목적으로
재사용): `proposed rep02` 포함 지금까지의 4개 trial(rep01/02 × 2 arm)
전부에서, 관측 구간 동안 **`vllm-serving` pod 이름이 정확히 2개만
등장했다** - 원래 active pod는 promotion 직후(수십 초~1분 내) `kubectl
top` 목록에서 사라졌고, 그 뒤로 새로운 3번째(신규 preview) pod는 한
번도 나타나지 않았다. 이 패턴은 **fixed_threshold와 proposed 양쪽
모두 동일하게** 관측됐다.

이 관측은 "중복 차단을 통과했다면 preview가 있었을지"에 대해
**간접적인** 근거(직접 증거 아님 - `kubectl top`에 안 잡힌다고 Rollout
컨트롤러 내부의 `pauseConditions` 상태까지 확정하는 건 아님)를 준다:
promotion 이후 새 preview가 관측되지 않는다는 사실은, 이후 신호가
중복 차단을 통과했더라도 `preview_ready=False`로 판정돼 `observe_only`
에 그쳤을 가능성과 **부합**한다 - 그러나 이것도 추정 수준이며 확정이
아니다.

**분류 명확화**:
- 중복 차단 규칙은 두 arm이 **공유하는 recovery-policy의 동작**이다
  (`idempotency_key` 구조 자체가 arm과 무관) - proposed(IF)만의 결함이나
  두 방식의 성능 차이의 원인으로 분류하지 않는다.
- 추가 신호 수가 많다고 탐지 성능이 좋다고 해석하지 않는다.
- 중복 차단 수가 많다고 그만큼 복구 기회를 "잃었다"고 해석하지 않는다
  (놓친 기회가 실제로 존재했는지 자체가 미확인).

**금지사항 준수**: 중복 차단 완화·idempotency key 변경·preview
재생성·반복 promotion 구현 - 전부 0건.

---

## 2. 비교 계약 유지 확인

§140의 분석 계약·첫 쌍 포함 판정을 그대로 사용했다 - 이번 턴에 계약
자체를 수정하지 않았다. 모델·feature·threshold·streak·cooldown·SLO·
부하 강도·라우팅·keepalive·복구 정책 변경 0건. core 45건·rep01·rep02
원본·hash·state 전부 보존.

---

## 3. 이번 실행 범위 — rep03 (부분 실행, 차단됨)

**계획**: 1) fixed_threshold rep03 → 2) proposed rep03(사전등록 순서
그대로), 새 run_id(`-03-`), 기존 파일과 충돌 없음 확인 후 실행.

**실행 전 확인**(이미 검증된 내용 반복 조사 없음 - 필요한 것만):
클러스터 연결·quiescence·활성 context 없음·rep03 파일 미존재 전부 확인
후 진행. stop-file 메커니즘은 rep02에서 이미 라이브 검증됐으므로
재검증하지 않음(지시대로 - 별도 fault pilot 추가 안 함).

**trial 1(fixed_threshold rep03) 실행 결과**: `state=invalid`,
`outcome=invalid_run`.

```
invalid_reason = 예외: OSError: [WinError 1455] 이 작업을 완료하기
위한 가상 페이징 파일이 너무 작습니다
```

**원인 분석**(저장된 원자료로 확인):
- `t_slo`=12:41:00.94, `t_recovery`=12:41:16.02(15.09초) - **SLO
  판정 자체는 정상적으로 이뤄졌다**(probe/판정기 문제 아님).
- `probe_valid=True` - probe는 끝까지 살아있었다(is_alive() 실패 아님).
- detector 로그는 12:48:44까지 15~16초 간격으로 계속 정상 평가를
  기록했다 - **detector crash 아님**.
- `t_injection_end`=12:47:44(주입 452.4초 후) - load_ramp의 5-stage
  스케줄은 정상 완료됐다.
- evidence의 마지막 이벤트는 `send_stopped(reason=stop_file, elapsed_
  sec=524.001)` - **stop-file이 정상 예정(900초)보다 한참 이른 524초
  시점에 눌렸다** - 즉 `run_once()`의 OBSERVING 루프 자체가 524초
  근처에서 예외로 조기 종료됐고, 그 예외가 `TrialInvalid`로 내부
  처리돼 정리 절차(quiescence 대기 포함)는 정상 수행됐다(클러스터
  잔여 자원 없음 확인, §4).
- **결론**: probe·detector·판정기·클러스터 어느 쪽도 원인이 아니다.
  `WinError 1455`는 **로컬 Windows 머신의 가상 메모리(페이징 파일)
  고갈** - 이 세션이 매우 오래 지속되며 누적된 로컬 프로세스 부하와
  관련 있을 가능성이 높다(현재 페이지 파일 사용량은 6.5GB/30GB로
  정상 범위로 돌아와 있음 - 일시적 스파이크였을 가능성, 확정 아님).
  후속 계측 코드(stop-file, drain, cohort 재구성)의 결함이 원인이라는
  근거는 없다 - 코드 자체가 실행되는 로컬 프로세스 환경의 문제다.

**지시에 따른 조치**: **proposed rep03(trial 2)를 시작하지 않았다.**
자동 재시도·대체 실행도 하지 않았다. 원본(trial 1의 결과 JSON·cost
JSON·evidence·raw CSV·detector 로그 전부)은 그대로 보존했다 - 삭제·
덮어쓰기 없음.

---

## 4. postflight 확인

| 확인 항목 | 결과 |
|---|---|
| 클러스터 pod 상태 | 정상(active pod 1개만, 잔여 preview/probe pod 없음) |
| quiescent | true |
| 활성 experiment-run context | null |
| Rollout 상태 | DESIRED=1/CURRENT=1/AVAILABLE=1(정상) |

**결론**: 이번 무효화는 클러스터·하니스 상태를 오염시키지 않았다
(`TrialInvalid`로 정상 처리됨, `HarnessCorrupted`가 아님) - 다음 시도를
위한 클러스터 자체는 깨끗하다. 다만 이번 턴에는 그 "다음 시도"를
실행하지 않는다(지시 - rep04 이후 실행 금지, 그리고 rep03 자체도
재시도 대상이 아니라 다음 승인을 기다린다).

---

## 5. 결과 보고

**rep03**: fixed_threshold 1건 무효(비교 불가 - 표에서 제외, 삭제
아님), proposed 0건(미실행). **rep03 쌍은 이번엔 비교 가능한 결과를
생성하지 못했다** - 아래는 rep01·rep02만으로 계속 구성한다(rep03는
"차단됨"으로만 기록).

**반복별 결과(합산 아님, rep01/rep02만 유효)**:

| | rep01 fixed | rep01 proposed | rep02 fixed | rep02 proposed | rep03 fixed | rep03 proposed |
|---|---|---|---|---|---|---|
| 실패율 | 0.11% | 0.0% | 0.11% | 0.11% | **무효** | 미실행 |
| 지연초과율 | 7.67% | 6.67% | 5.67% | 6.00% | **무효** | 미실행 |

**fixed−proposed 차이(rep01/rep02만)**:

| | 실패율 차이 | 지연초과율 차이 | 유리한 쪽 |
|---|---|---|---|
| rep01 | +0.11%p | +1.00%p | proposed(둘 다) |
| rep02 | 0.00%p | −0.33%p | fixed(지연초과율), 실패율 동률 |

(§141과 동일 - 이번 턴에 새로 추가된 유효 반복 없음)

**실행 순서·세션 차이**: rep01은 fixed→proposed, rep02는 proposed→
fixed(계획대로 교차). rep03은 fixed_threshold부터 시작했으나 완료하지
못해 순서 교차 자체가 성립하지 않았다 - 다음 시도 때 이 순서(fixed→
proposed)를 다시 쓸지, 다른 순서로 조정할지는 이번 문서에서 결정하지
않는다(사용자 판단 대기).

**반사실 비교 한계 재확인**: rep01/02/03(시도)은 서로 다른 시각에
순차 실행됐다 - 동시 실행된 동일 환경의 반사실 비교가 아니다. 추가
신호 수·중복 차단 수를 탐지 성능이나 손실 기회로 해석하지 않는다
(§1).

---

## 6. 반복 계획과 정지점

**실제 소요시간**: trial 1(fixed_threshold rep03) 준비~무효화까지
약 17분 31초(`t_run_start` 12:33:08 ~ `t_run_end` 12:50:40) - 정상
완료 trial(약 24~25분)보다 짧다(524초 지점에서 조기 종료됐으므로).

**rep04 진행 가능 여부**: **진행하지 않는다.** 이번 승인 범위는
rep03 한 쌍이었고, rep03 자체가 완결되지 못했다 - rep04로 넘어가는
것은 지시 위반이다. rep03의 나머지(proposed rep03, 그리고 필요하면
fixed_threshold rep03 재시도)부터 다음 승인에서 다뤄야 한다.

**남은 사전등록 순서(변경 없음)**: rep03(fixed→proposed, 미완료) →
rep04(proposed→fixed_threshold) → rep05(fixed_threshold→proposed).

**차단 사유 요약(그대로 기록, 모델·정책 수정 없음)**: 로컬 머신의
가상 메모리 고갈(`WinError 1455`)로 fixed_threshold rep03이 524초
지점에서 무효화됨 - 클러스터·판정 로직·계측 코드 자체의 결함이라는
증거는 없음. 다음 시도 전에 로컬 머신의 여유 자원(특히 이 세션에서
누적된 프로세스)을 확인하는 게 좋을 수 있으나, 이건 이 문서의 범위를
넘는 운영 판단이라 여기서 조치하지 않았다.

---

*작성: 2026-09-25. 재현 불필요(이번 턴은 1개 trial 실행 + 기존 저장
자료 재확인만). 원본 core 45건·rep01·rep02·rep03(무효 trial 포함)의
결과·state·CSV·evidence·cost·contract·detector 로그 전부 보존 -
삭제·덮어쓰기 없음.*
