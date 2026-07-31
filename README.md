# 이상 탐지 기반 위험도 산정을 통한 선제적 GitOps 자동 복구 시스템

쿠버네티스 기반 AI 서빙 인프라(vLLM)를 대상으로, K8s 지표와 vLLM 고유 지표를 이상 탐지 모델(Isolation Forest)로 분석해 장애 위험도를 산정하고, 위험도가 임계치를 넘으면 실제 장애 발생 전에 GitOps(ArgoCD + Argo Rollouts) 기반으로 선제적 복구를 실행하는 시스템입니다.

쿠버네티스 기본 self-healing / 고정 임계치 규칙 / 제안 방식(이상 탐지 기반 선제적 복구) 3가지를 Chaos Mesh로 재현한 장애 상황에서 비교하고, 종단 간 복구 조치 지연시간·가용성·복구 정확성을 정량적으로 검증합니다.

- 지도교수: 김영한 교수님
- 작성자: 박소정 (숭실대 전자정보공학부 IT융합전공)
- 상세 제안서: [docs/proposal.md](docs/proposal.md)

## 구조

```
cluster/              클러스터 구축 (kubeadm)
gitops/                ArgoCD가 감시하는 "정상 상태" 선언 + Argo Rollouts
monitoring/            Prometheus(K8s+vLLM 지표) + Grafana
chaos/                 Chaos Mesh 장애 시나리오 + 무개입 대조군
anomaly-detection/     이상 탐지 모델 (Isolation Forest)
recovery-policy/       복구 정책 결정 서비스 (Git 커밋 기반)
experiments/           실험 자동화 + 결과 수집
operator/              (보너스 1) Operator 패턴 리팩터링
pvc-backup/            (보너스 2) Velero 스냅샷
finops/                (보너스 3) 비용 관측
docs/                  제안서, 논문 원고
```

## 진행 상태

- [x] 주제 확정 및 지도교수 승인
- [x] 실습 서버 확보 진행 중 (2-VM: control-plane + worker)
- [ ] 클러스터 구축
- [ ] GitOps 파이프라인
- [ ] AI 서빙 배포
- [ ] 관측성 스택
- [ ] 장애 시나리오
- [ ] 이상 탐지 모델
- [ ] 복구 정책 결정 서비스
- [ ] 실험 및 측정
