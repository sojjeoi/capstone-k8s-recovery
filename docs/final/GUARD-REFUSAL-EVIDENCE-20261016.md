# Recovery Guard 거부 경로 증거 (2026-10-16)

**질문**: 신호가 와도 조건이 어긋나면 Recovery Policy가 정말 승격을 호출하지 않는가?

**답**: 아래 17개 시나리오 모두에서 승격 호출 0회로 거부·중단했다(`recovery-policy/test_guarded_policy.py`, 단위 테스트).
지금까지의 최종 결과(복구 3/3, A/B 8쌍)는 모두 성공 경로였고, 이 문서는 막아야 할 때 막는다는 쪽의 증거다.

## 범위 - 먼저 읽을 것

- **단위 테스트로 확인한 동작이다.** 실제 클러스터에서 사고를 일으켜 막은 실험이 아니다. 클러스터 상태는 테스트가 만든 모의 값(`view()`)이다.
- 테스트의 시간 한도는 신호 나이 30 s · 미래 오차 5 s · 창→결정 30 s이며 코드에 **"테스트용 값(채택 아님)"** 으로 적혀 있다. 실제 배포값은 15 s · 2 s · 10 s다([배포 매니페스트](../../gitops/apps/guarded-recovery-policy/guarded-recovery-policy.yaml)). 거부 로직은 같고 한도 숫자만 다르다.
- 공식 A/B 5쌍·추가 3쌍·Phase 2 결과와 섞지 않는 별도 증거다.
- "잘못된 복구를 막았다"가 아니라 "조건이 어긋나면 승격을 호출하지 않는다"까지만 말한다.

## 결과

판정 코드는 `guarded_policy.py`의 `check_target`(95행)과 시각 검사 `_timing_reasons`, 승격 직전 재확인(`_act_locked`)에서 나온다.

| # | 어긋난 상황 | 정책 응답 | 거부 사유 | 승격 호출 |
|---|---|---|---|---|
| 1 | active 버전이 신호 시점과 다름(이미 바뀜) | `rejected_target` | `active_changed` | 0 |
| 2 | preview 버전이 신호 시점과 다름 | `rejected_target` | `preview_changed` | 0 |
| 3 | 새 Pod가 Ready가 아님 | `rejected_target` | `preview_not_ready` | 0 |
| 4 | preview가 승격 대기(paused) 상태가 아님 | `rejected_target` | `preview_not_paused` | 0 |
| 5 | 새 Pod UID가 신호 시점과 다름 | `rejected_target` | `preview_uid_changed` | 0 |
| 6 | 새 Pod 재시작 수가 신호 이후 늘어남 | `rejected_target` | `preview_restarted` | 0 |
| 7 | 기존 Pod 재시작 수가 신호 이후 늘어남 | `rejected_target` | `active_restarted` | 0 |
| 8 | 다른 Rollout | `rejected_target` | `rollout_mismatch` | 0 |
| 9 | 같은 버전 Pod가 2개 | `rejected_target` | `preview_pod_count=2` | 0 |
| 10 | 클러스터 조회 실패(API 불가) | `rejected_target` | `cluster_view_error` | 0 |
| 11-13 | 클러스터 조회 결과가 깨진 형식 3종 | `rejected_target` | 기록에 `rejected_target`로 남음 | 0 |
| 14 | 신호가 너무 오래됨 / 미래 시각 / 창→결정 간격 초과(한 테스트의 3경우) | `rejected_timing` | `stale_signal` · `future_signal` · `stale_window` | 0 |
| 15 | 판단 도중 대상이 바뀜(승격 직전 재확인) | `aborted_final_check` | `active_changed` | 0 |
| 16 | 처리 중 시간이 지나 신호가 오래됨 | `aborted_final_check` | `stale_signal` | 0 |
| 17 | 승격 직전 조회가 느려져 신호가 오래됨 | `aborted_final_check` | `stale_signal` | 0 |

- 15번은 중단 뒤에도 정책이 막히지 않는다: 다음 신호(대상이 다시 일치)는 정상 승격 1회로 처리되고, 앞선 중단은 `prior_aborts`에 기록된다.
- 대조군: 같은 입력 형식에서 조건이 모두 맞는 신호는 `promoted_verified`, 승격 호출 1회다(`test_first_valid_signal_promotes_once_without_reconfirm`). 거부는 입력 조건 때문이지 정책이 항상 거부해서가 아니다.

## 실행 기록

```
test_guarded_policy.py (거부 관련 17개 케이스)  17 passed
test_guarded_policy.py (전체)                    58 passed
환경: Windows, Python 3.14.3, pytest 9.1.1, 2026-10-16, 클러스터 접속 없음
```

## 아직 보여주지 못한 것

- 실제 클러스터에서 대상을 바꿔 가며 정책이 막는 모습(live 거부 실험)은 하지 않았다.
- 거부 사유가 대시보드의 "복구 조건 검증" 카드에 ✕로 표시되는 로직은 단위 테스트(`dashboard/test_dashboard.py`)로 확인했으며, 실제 거부 상황을 화면으로 재현하지는 않았다.
