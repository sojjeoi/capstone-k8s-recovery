# Phase 1 A/B 실제 전환 시간 (B 세션 8회, 2026-10-15 재정리)

> 새 실험이 아니라 기존 실행 요약(summary)을 읽어 다시 정리한 표다. 공식 판정·분석 결과는 바꾸지 않는다.

## 정의

**전환 시간 = `promote_result.verify.switched_at` − `promote_called_at`**
= 승격 요청 시각부터 Rollout `activeSelector`와 `vllm-active` Service selector가 **둘 다 새 해시로 바뀐 것을 처음 확인한** 시각까지.

- 사전 동결 분석기의 `promotion_to_selector_sec`와 같은 정의다([experiments/guarded_bck_analysis.py](../../experiments/guarded_bck_analysis.py) 178행).
- 측정 코드: `promote_once`·`verify_switch`([recovery-policy/guarded_k8s.py](../../recovery-policy/guarded_k8s.py) 106–161행).
- `switched_at`은 **확인한** 시각이다. 실제 selector 변경은 그 이전 어느 시점이다.
- `verify.elapsed_sec`(약 0.52–0.55 s)는 promote 명령이 **끝난 뒤** 확인까지의 시간이라 이 표의 값과 다르다(명령 실행 시간 미포함).

## 결과

| 세션 | 구분 | 전환 시간 | old → new (ReplicaSet hash) | 새 / 기존 Pod 요청 (60 s) | 트래픽 확인 |
|---|---|---:|---|---|---|
| ab01b | 공식 5쌍 | 1.292 s | 5889d5fc8b → 6bcc45cb7c | +60 / +0 | verified |
| ab02b | 공식 5쌍 | 1.548 s | 6bcc45cb7c → 74fdf54796 | +60 / +0 | verified |
| ab03b | 공식 5쌍 | 1.017 s | 74fdf54796 → 7d75954c97 | +61 / +0 | verified |
| ab04b | 공식 5쌍 | 0.863 s | 7d75954c97 → 768d8fbd89 | +60 / +0 | verified |
| ab05b | 공식 5쌍 | 1.457 s | 768d8fbd89 → 6df78d474c | +61 / +0 | verified |
| ab06b | 사전등록 추가 3쌍 | 5.539 s | 59f65899d8 → 746d5d6588 | +60 / +0 | verified |
| ab07b | 사전등록 추가 3쌍 | 7.871 s | 746d5d6588 → d774768f6 | +60 / +0 | verified |
| ab08b | 사전등록 추가 3쌍 | 1.130 s | d774768f6 → 845756749f | +61 / +0 | verified |

- 중앙값 **1.374 s**, 범위 **0.863–7.871 s** (8회, 평균 2.590 s)
- 8회 모두 승격 1회, promote CLI 종료 코드 0, 트래픽 확인 `verified=true`
- ab06b·ab07b가 긴 이유: selector 확인 자체(약 0.53 s)는 다른 세션과 같고, `kubectl-argo-rollouts promote` 명령 실행이 약 5.0 s·7.3 s 걸렸다. 왜 명령이 오래 걸렸는지는 기록에 없다(미확인).

## 해석 범위

- 말할 수 있는 것: A/B 실제 전환 8회 모두 Service selector 변경과 새 Pod 요청 이동(+60~61, 기존 +0)을 확인했다.
- 말할 수 없는 것: 이 값은 **전환** 시간이며 탐지 시간·서비스 품질 회복 시간이 아니다. 공식 비교는 5쌍이고 추가 3쌍은 사전등록 반복이다 - 8회를 "공식 8쌍"으로 부르지 않는다.

## 근거 파일

8회 모두 [experiments/results/capstone_guarded_ab_v1/](../../experiments/results/capstone_guarded_ab_v1/EVIDENCE-INDEX.md)의 `summary-guarded_pressure_act-ab0{1..8}b-capstone-guarded-ab-v1-20261007.json`에서 읽었다.

- ab06b–ab08b: Stage 2 핵심 근거로 이미 커밋된 파일
- ab01b–ab05b: 이 표의 근거로 2026-10-15에 추가한 원본 실행 요약. 각 파일의 SHA256이 커밋된 공식 분석 파일(`ab05-20261007/analysis-official-ab01-ab05-*.json`, `ab08-20261013/analysis-official-ab01-ab08-*.json`)에 기록된 해시와 일치해, 공식 분석이 읽은 그 파일임을 확인할 수 있다.

| 파일 | SHA256 (앞 16자) | 크기(byte) |
|---|---|---:|
| `summary-guarded_pressure_act-ab01b-…json` | `0032506fe9e9963f…` | 35011 |
| `summary-guarded_pressure_act-ab02b-…json` | `90e9e25828a94805…` | 38032 |
| `summary-guarded_pressure_act-ab03b-…json` | `8961f0f7b39fce62…` | 41222 |
| `summary-guarded_pressure_act-ab04b-…json` | `c29e3c5499a3a4c5…` | 44484 |
| `summary-guarded_pressure_act-ab05b-…json` | `e628a02365518f41…` | 47673 |

탐지기 tick 원자료·로그 등 대용량 원자료는 계속 저장소 밖에 보존한다.

- 읽은 필드: `policy_state_after.actions[run_id].promote_called_at`, `promote_result.verify.switched_at`, `promote_result.cli.returncode`, `service_job.target_key`, `service_check.verdict`
