<div align="center">

# Kubernetes LLM 서빙 환경의 이상 탐지 및 자동 복구

**운영 환경에 맞는 감지기를 선택하고, 이상 신호가 오면 안전 조건을 확인한 뒤<br>Blue/Green 전환과 실제 트래픽 이동까지 검증하는 Kubernetes 기반 자동 복구 시스템**

![Kubernetes](https://img.shields.io/badge/Kubernetes-326CE5?logo=kubernetes&logoColor=white)
![Argo Rollouts](https://img.shields.io/badge/Argo_Rollouts-EF7B4D?logo=argo&logoColor=white)
![vLLM](https://img.shields.io/badge/vLLM-Qwen2.5--0.5B-30A2FF)
![Prometheus](https://img.shields.io/badge/Prometheus-E6522C?logo=prometheus&logoColor=white)
![Python](https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![scikit-learn](https://img.shields.io/badge/scikit--learn-F7931E?logo=scikitlearn&logoColor=white)

[![Final Result](https://img.shields.io/badge/Final_Result-Phase_2-0D786E?style=for-the-badge)](docs/final/PHASE2-FINAL-RESULT-20261012.md)
[![Demo](https://img.shields.io/badge/Demo-Runbook-2EA44F?style=for-the-badge)](demo/README.md)
[![Evidence](https://img.shields.io/badge/Evidence-Index-555555?style=for-the-badge)](experiments/README.md)

<sub>2026 숭실대학교 전자정보공학부 IT융합전공 졸업작품 · 지도교수 김영한 교수님 · 작성자 박소정</sub>

</div>

---

## 왜 필요한가?

Kubernetes는 Pod·Container 상태와 선언된 Desired State를 관리하지만, Pod가 정상인 동안에도 LLM 응답 성능은 부하에 따라 저하될 수 있습니다. **Pod가 정상이라는 것과 사용자가 정상 품질을 받는 것은 같지 않습니다.**

> **서비스가 살아 있지만 느려진 상황을 어떻게 감지하고, 잘못된 전환 없이 실제 복구까지 확인할 것인가?**

## 시스템 구조

<p align="center">
  <img src="docs/images/architecture.png" alt="시스템 구조 - 환경별 감지기 선택, ACTIVE/SHADOW 감지기, Recovery Policy, Argo Rollouts Blue/Green 전환, 실제 트래픽 검증" width="100%">
</p>

1. **Detect** - Prometheus의 CPU·메모리 지표를 15초마다 평가하고, 3회 연속 이상이면 신호를 보냅니다. 현재 환경(vLLM CPU, Qwen2.5-0.5B)에서는 **CPU 임계값이 ACTIVE**, Isolation Forest는 **SHADOW(기록만)** 입니다.
2. **Guard** - 신호가 와도 바로 전환하지 않습니다. 신호 시점의 대상과 지금 상태(active/preview 버전, Pod UID, 종료 여부, 새 Pod Ready)가 같을 때만 진행합니다.
3. **Promote** - Argo Rollouts에 Blue/Green 승격을 1회 요청합니다. 승격 자체는 Argo의 기능이고, **언제 승격해도 되는지 판단하고 결과를 확인하는 Recovery Policy**가 이 프로젝트의 구현입니다.
4. **Verify** - 승격 API 성공만 믿지 않고, 기존/새 Pod의 `:8000/metrics` 요청 카운터로 실제 요청 이동을 확인합니다(예: 기존 +0, 새 +60).

## 핵심 결과

| 검증 항목 | 결과 |
|---|---|
| 자동 복구가 서비스 품질 저하 시간을 줄이는가 | 공식 A/B **5쌍 모두** 자동 복구 쪽이 짧음(0 s) · 사전등록 추가 **3쌍도 같은 방향** |
| 실제 트래픽 전환 | 자동 복구 **8회 모두** 확인 · 승격 요청 → 전환 확인 **중앙값 1.374 s** |
| 정상 상태 불필요 신호(Isolation Forest 재학습) | **14 → 1** (−92.9%) |
| 현재 환경의 감지기 선택 → 실제 복구 | **CPU 임계값 5/5 · IF 0/5** → 선택된 감지기로 **3/3 성공** |

공식 비교는 5쌍이며 추가 3쌍과 합쳐 "공식 8쌍"으로 부르지 않습니다. 감지기 선택은 현재 CPU-vLLM 환경의 결과이며 CPU 임계값이 일반적으로 더 낫다는 뜻이 아닙니다. 14 → 1은 정상 상태의 불필요 신호 감소이며 압박 탐지 성능 결과가 아닙니다. → [최종 결과](docs/final/PHASE2-FINAL-RESULT-20261012.md) · [연구 정리](docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md) · [전환 시간](docs/final/AB-TRAFFIC-SWITCH-TIMING-20261015.md)

## 시연

정상 동작 → CPU 압박 → 이상 감지 → 복구 조건 검증 → 승격 → Service 전환 → 실제 요청 이동 검증

[demo/](demo/README.md)(시연 스크립트) · [dashboard/](dashboard/README.md)(읽기 전용 시연 화면, 복구 로직 아님) · [runbook](docs/demo/PHASE2-DEMO-RUNBOOK-20261012.md)

## 핵심 구현

| 파일 | 역할 |
|---|---|
| [recovery-policy/main_guarded.py](recovery-policy/main_guarded.py) | Recovery Policy 서버 - 신호 수신 `/signal/v2` |
| [recovery-policy/guarded_policy.py](recovery-policy/guarded_policy.py) | 전환 가능 여부 판단 - `check_target`, `GuardedPolicy`, `_act_locked` |
| [recovery-policy/guarded_k8s.py](recovery-policy/guarded_k8s.py) | 승격·검증 - `promote_once`, `verify_switch`, `evaluate_new_pod_traffic` |
| [experiments/phase2_detector_selection/](experiments/phase2_detector_selection/) | 감지기 선택(`selection.py`)과 ACTIVE/SHADOW 실행(`p2_executor.py`) |
| [anomaly-detection/](anomaly-detection/README.md) | 감지기 규칙·특징 계산 |
| [gitops/](gitops/README.md) | Argo Rollout, Service, RBAC, Recovery Policy 배포 매니페스트 |

`recovery-policy/`의 `main.py`·`policy.py` 등은 초기 버전입니다. 문서 안내·세부 연구 기록은 [docs/README.md](docs/README.md), 실험 근거 위치는 [experiments/README.md](experiments/README.md)에 있습니다.

## 한계

- CPU 기반 vLLM 단일 환경(Qwen2.5-0.5B)에서 검증했고, 다른 GPU·모델·장애 유형은 추가 검증이 필요합니다.
- 환경이 바뀌면 감지기를 다시 평가·선택해야 하며, Isolation Forest의 추가 우월성은 확인되지 않았습니다.
- 표본이 작고(공식 A/B 5쌍, 실제 복구 3회), clone만으로 모든 과거 실험을 재현할 수는 없습니다([재현성 범위](docs/README.md#재현성-범위)).

**이 프로젝트의 핵심은 특정 AI 모델의 우월성이 아니라, 환경에 맞는 감지기를 선택하고 안전한 복구 경로에 연결해 실제 트래픽 이동까지 확인하는 구조를 구현·검증한 것입니다.**
