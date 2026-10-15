# recovery-policy - Recovery Layer

detector 신호를 받아 **안전 조건을 다시 확인한 뒤에만** Argo Rollouts Blue/Green 승격을 실행하고, 승격 뒤 새/기존 Pod의 실제 요청 수로 트래픽 이동을 확인하는 정책 서비스. detector 종류(CPU 임계값·Isolation Forest)와 무관하게 같은 경로를 쓴다.

## 현재 최종 구현 (guarded) - 여기부터 읽기

| 파일 | 역할 | 먼저 볼 곳 |
|---|---|---|
| [main_guarded.py](main_guarded.py) | FastAPI 서버. 신호 수신 `/signal/v2`, 관리 API(`/admin/context`, `/admin/state` 등), 환경변수로 정책 조립 | `create_app` (125행), `build_app_from_env` (263행) |
| [guarded_policy.py](guarded_policy.py) | 조치 여부 판단. 신호 시점 상태와 현재 클러스터 상태 비교, 신호 나이·시계 오차 한도, 중복 신호 차단, 실행당 조치 1회, 승격 직전 재확인 | `GuardConfig` (67행), `check_target` (95행), `GuardedPolicy` (140행), `_act_locked` (404행) |
| [guarded_k8s.py](guarded_k8s.py) | Kubernetes 실행·검증. promote 1회, selector 전환 확인, 새/기존 Pod `:8000/metrics` 요청 카운터로 트래픽 판정 | `promote_once` (106행), `verify_switch` (129행), `evaluate_new_pod_traffic` (168행), `ServiceChecker` (234행), `pod_metrics_counter_reader` (414행) |
| [guarded_state.py](guarded_state.py), [guarded_ledger.py](guarded_ledger.py), [guarded_schema.py](guarded_schema.py) | 정책 상태 저장, 조치 원장(중복 방지·사후 확인), 신호 형식 | |

동작 모드: `record_only`(판단·기록만, 조치 없음) / `act`(조건이 모두 맞을 때 승격 1회). 실험과 시연은 승인된 경우에만 `act`로 전환했다.

배포 매니페스트: [gitops/apps/guarded-recovery-policy/guarded-recovery-policy.yaml](../gitops/apps/guarded-recovery-policy/guarded-recovery-policy.yaml) (실제 클러스터에서 내보낸 것)

`check_target`이 거부하는 경우(하나라도 해당하면 전환하지 않음): Rollout이 다름, active/preview selector가 신호 시점과 다름, preview가 승격 대기 상태가 아님, 버전별 Pod가 정확히 1개가 아님, Pod UID 변경·종료 중·신호 이후 재시작 증가, preview가 Ready가 아님.

## 초기 버전 (historical - 최종 실험에 쓰지 않음)

`main.py`, `policy.py`, `rollouts_client.py`, `safety.py`, `decision_log.py`, `git_client.py`

초기 정책 서비스와 Git 감사 기록 경로다. 저장소의 [audit-log/](../audit-log/README.md)는 이 초기 버전(`git_client.py`)이 남긴 기록이며, 최종 guarded 정책은 audit-log에 쓰지 않는다(조치 기록은 정책 상태·원장에 남는다). [gitops/apps/recovery-policy/](../gitops/apps/recovery-policy/)도 이 초기 버전의 배포 매니페스트다.

## 테스트

`test_*.py` - 예: `test_guarded_policy.py`, `test_guarded_k8s.py`, `test_main_guarded.py`, `test_guarded_pod_metrics.py`

결과 문서: [../docs/final/PHASE2-FINAL-RESULT-20261012.md](../docs/final/PHASE2-FINAL-RESULT-20261012.md)
