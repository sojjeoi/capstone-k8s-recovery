# AI 서빙 인프라를 위한 선제적 AIOps 자동 복구 시스템

## 📖 프로젝트 개요

온프레미스 K8s 위에 vLLM AI 서빙을 올리고, K8s 지표와 vLLM 고유 지표를 시간창 특성(이동평균·기울기)까지 포함해 Isolation Forest로 분석해 위험도를 산정합니다. 위험도가 임계치를 넘으면 실제 장애 전에 복구 정책 결정 서비스가 Kubernetes API(Argo Rollouts promotion)로 즉시 트래픽을 전환하고, 판단 근거와 실행 결과를 Git에 구조화해 기록합니다. 정상 상태·배포 구성은 GitOps(ArgoCD)로 관리하되, 시간민감적 복구 전환 자체는 API로 직접 실행하고 Git은 사후 감사 기록 용도입니다.

Chaos Mesh로 점진적 열화(메모리 압력·부하 증가·네트워크 열화)와 돌발 장애(Pod kill)를 함께 주입해 (K8s 기본 self-healing / 고정 임계치 규칙 / 제안 방식) 3-way로 비교하고, 무개입 대조군 대비 선행탐지시간(lead time)·가용성·복구 정확성을 정량 검증합니다.

- 지도교수: 김영한 교수님
- 작성자: 박소정 (숭실대 전자정보공학부 IT융합전공)
- 상세 제안서: [docs/proposal.md](docs/proposal.md)

## ✨ 주요 기능

- **AI 서빙 워크로드 배포**: vLLM 기반 경량 모델을 Argo Rollouts BlueGreen으로 배포·서빙
- **관측성 스택**: Prometheus(K8s+vLLM 지표) + Grafana + Alertmanager, 영구 저장 구성 완료
- **장애 주입 및 실험 자동화**: Chaos Mesh로 Pod 종료·점진적 메모리 압력·고정 도착률(open-loop) 부하 증가·단계적 네트워크 열화 4종 시나리오 재현 (검증 완료). 3가지 방식(기본 self-healing / 고정 임계치 / 제안 방식) 자동 비교 오케스트레이션은 개발 예정
- **이상 탐지 기반 위험도 산정**: K8s 지표(CPU·메모리·재시작·OOM) + vLLM 고유 지표(KV Cache 사용률, 요청 수, 토큰 처리량 등)를 Isolation Forest로 분석해 장애 전조 감지 *(개발 예정)*
- **선제적 자동 복구**: 위험도가 임계치를 넘으면 장애 발생 전 Kubernetes API(Argo Rollouts promotion)로 트래픽 전환 *(API promotion 방식 실측 검증 완료, 정책 결정 서비스 본 구현은 개발 예정)*
- **감사 가능한 복구 이력**: 복구 조치의 판단 근거와 실행 결과를 Git에 구조화하여 기록, 사후 추적 가능 *(개발 예정)*
- **정량적 성능 검증**: 복구 지연시간 단계별 분해, 가용성, 정확성 등 종합 평가 *(개발 예정)*

## 🛠️ 기술 스택

```
Kubernetes (kubeadm) | ArgoCD | Argo Rollouts | Prometheus | Grafana | Alertmanager
Chaos Mesh | Python (scikit-learn, FastAPI) | vLLM | Docker
```

## 📂 프로젝트 구조

```
capstone-k8s-recovery/
├── README.md
├── docs/
│   ├── proposal.md
│   └── design/                     # Phase별 상세 조사 기록
│
├── gitops/                         # ArgoCD가 감시하는 "정상 상태" 선언 + Argo Rollouts
│   └── apps/
│       ├── vllm-serving/           # Rollout(BlueGreen)·Service·RBAC·ServiceMonitor·PrometheusRule
│       └── monitoring/             # StorageClass·Prometheus/Grafana PV
│
├── chaos/                          # Chaos Mesh 장애 시나리오 4종 + 무개입 대조군(예정)
│   └── loadgen/                    # 고정 도착률(open-loop) 부하 생성기
│
├── anomaly-detection/               # 이상 탐지 모델 (Isolation Forest) — 개발 예정
├── recovery-policy/                 # 복구 정책 결정 서비스 — Phase 2.5 API promotion PoC 완료, 본 구현 예정
│
└── experiments/                     # 실험 자동화 + 결과 수집 — 개발 예정
```

## 🚧 진행 상태

- [x] 주제 확정 및 지도교수 승인
- [x] 실습 서버 확보 완료 (2-VM: control-plane + worker)
- [x] 클러스터 구축
- [x] GitOps 파이프라인
- [x] AI 서빙 배포 (vLLM + Argo Rollouts BlueGreen)
- [x] 관측성 스택 (Prometheus + Grafana + Alertmanager)
- [x] 장애 시나리오 4종 검증 (Pod kill / 메모리 압력 / 부하 증가 / 네트워크 열화)
- [ ] 무개입 대조군 실행 스크립트
- [ ] 이상 탐지 모델
- [ ] 복구 정책 결정 서비스 (API promotion PoC 완료, 본 구현 진행 중)
- [ ] 실험 및 측정

> 진행 상태는 위 체크리스트로 갱신됩니다. 시나리오별 실측 결과와 트러블슈팅 상세는 `docs/design/`를 참고하세요.
