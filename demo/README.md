# demo - 발표 시연 스크립트 (demo-only)

발표 중 실제 클러스터에서 자동 복구 1회를 보여 주는 PowerShell 스크립트. **시연 결과는 공식 실험 결과가 아니다**(`counted=false`, 결과 폴더 `experiments/results/demo_only_phase2/`). 공식 실행기·Recovery Policy·감지기 임계값은 그대로 쓰고 바꾸지 않는다.

## 시연 흐름

```
정상 서비스 → CPU 압박 시작 → ACTIVE 감지기(CPU 임계값) 3회 연속 → Recovery Policy 안전 조건 재확인
→ Argo Rollouts 승격 → Service selector 전환 → 새/기존 Pod 실제 요청 수로 이동 확인
```

## 스크립트

| 언제 | 스크립트 | 하는 일 |
|---|---|---|
| 발표 4–6분 전 | [prepare-demo.ps1](prepare-demo.ps1) | 읽기 전용 사전 확인 → 정책 `act` 전환 → 시계 오차 측정 → 기존 정책 격리 → 대시보드 → 실행기 사전검사 → 세션 시작. 새 Pod·요청 발생기·감지기를 준비하고 압박 직전에서 대기(최대 15분) |
| 발표 중 | [start-pressure.ps1](start-pressure.ps1) | 시작 신호. 이후 압박 → 감지 → 승격 → 전환 → 트래픽 확인이 자동 진행 |
| 발표 뒤 | [cleanup-demo.ps1](cleanup-demo.ps1) | 세션 종료 대기 → 정책 `record_only` 복귀 → 기존 정책 복원 → 최종 상태 확인 |

- 세 스크립트는 클러스터 상태(정책 모드, 기존 정책 격리·복원, 시연 세션)를 바꾸므로 발표 시연 목적으로만 실행한다.
- `prepare-demo.ps1`은 실행 중 현재 폴더를 저장소 루트로 옮긴다. 이어서 다른 스크립트를 실행할 때는 전체 경로로 실행한다.
- 실행 ID(`p2demo01`–`09`)는 1회용이다. 실패하면 자동 재시도·수동 승격을 하지 않고, 공식 Phase 2 Stage 2 결과(3/3)를 과거 기록으로 밝혀 보여 준다.

## 대시보드

시연 화면(`http://127.0.0.1:8765/`)의 코드는 [dashboard/](../dashboard/README.md)에 있다. 클러스터·정책을 바꾸지 않는 **읽기 전용 demo-only 화면**이며 작품의 핵심 구현은 아니다. `python dashboard/server.py --mock`으로 클러스터 없이 각본 화면을 볼 수 있다. 없으면 `prepare-demo.ps1`이 건너뛰고, 시연 결과에는 영향이 없다.

자세한 절차·전제: [docs/demo/PHASE2-DEMO-RUNBOOK-20261012.md](../docs/demo/PHASE2-DEMO-RUNBOOK-20261012.md)
