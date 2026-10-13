# 교수 발표용 최종 package (2026-10-10)

> Status: **CURRENT - 발표 문구·예상 질문**(2026-10-10 작성, E8은 2026-10-11 추가). **Phase 2 최종 결과(2026-10-12)는 [PHASE2-FINAL-RESULT-20261012.md](PHASE2-FINAL-RESULT-20261012.md)가 우선한다.** c3-A·Axis 2·문헌·S3 결과는 E8과 아래 synthesis 참조. 최신 결과와 최종 후속 연구 방향은 `docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md` 참조. 아래 E8에 후속 방향만 짧게 더함.

근거: `docs/final/FINAL-EVIDENCE-SUMMARY-20261010.md`(공식 결과 재확인·claim matrix), `docs/history/REPRODUCIBILITY-AUDIT-20261010.md`, `docs/demo/DEMO-RUNBOOK-20261010.md`.

## E1. 한 문장

> 느리지만 살아 있는 LLM 추론 서비스 저하에서, 정상 패턴 이탈 신호를 guarded 정책이 받아 미리 준비한 Pod로 실제 전환하면 기록만 할 때보다 사용자 SLO 피해가 줄었고(5/5), 전환은 실제 요청 카운터로 검증했다 - 단, 현재 Isolation Forest가 CPU 임계값보다 낫다는 것은 확인되지 않았다.

## E2. Problem

AI 추론 서비스는 죽지 않고 **느려지는**(slow-but-alive) 고장이 흔하다. Pod는 Ready라 Kubernetes 기본 재시작·헬스체크가 반응하지 않고, 사용자만 느린 응답을 겪는다.

## E3. Solution

Prometheus(컨테이너 CPU·메모리) → Isolation Forest 이상 점수(15초마다, 3연속이면 신호) → guarded recovery policy(신호 나이·시계·잠금·원장 확인 뒤에만 조치) → Argo Rollouts Blue/Green 승격(미리 띄운 preview로 Service 전환) → 실제 트래픽 검증(새/이전 Pod `/metrics` 요청 카운터 직접 비교).

## E4. 확인된 성과 (실험으로만)

1. **전환 효과**: 같은 압박·같은 detector에서 실제 전환(B)은 기록만(A)보다 누적 SLO 위반을 줄였다 - 5/5, A 중앙값 207 s → B 0 s(부호 검정 p = 0.0625, 기술값).
2. **실제 트래픽 이동**: 전환 5회 모두 새 Pod 요청 +60/61, 이전 Pod +0, Pod 이름·UID·IP 기록.
3. **경로 신뢰성**: 정책 → 승격 → selector 전환(≈0.5 s) → 서비스 확인이 매번 끝까지 동작(gap02·A/B·B/C/K).
4. **기존 고정 임계값의 한계**: CPU 2.7 core 고정 임계값은 196–246 s 늦게 반응해 피해가 컸다(5/5).
5. **재현성**: 모든 공식 수치가 원자료에서 독립 재계산으로 정확히 재현되고, frozen artifact 해시가 일치한다.

## E5. 한계 (숨기지 않음)

- 보정 CPU 임계값 대비 IF 우월성 **미검증**(B/C/K 1차 D 중앙값 0, 6쌍 중 1쌍은 IF 쪽이 더 나쁨).
- 정상 운전에서 IF가 불필요 신호를 더 일찍 냈다(N1 6/6).
- 무결성 감사: 계산은 정확, 그러나 학습·보정 데이터가 실제 정상 운전의 **Pod/세션별 메모리 수준과 정상 메모리 감소 동작**을 충분히 대표하지 못해 불필요 신호가 났다 → 빠른 신호를 "조기 탐지"로 해석 불가. 정상 데이터 확대·Pod-relative 메모리 두 개선 시도 모두 NOT PROMISING이라 detector 개선 연구는 종료(상세: `docs/history/C1-DETECTOR-LIMITATIONS-AND-FUTURE-WORK-20261010.md`).
- 지금 시나리오(CPU 압박)는 CPU 평균 하나로 정상과 완전히 갈린다(AUC 1.0, 탐색) → 다변량이 필요한 상황을 아직 실험으로 만들지 못했다(v2 부하·I/O·noisy-neighbor feasibility 모두 중단).
- 표본이 작다(A/B 5쌍, B/C/K 6쌍 stage 1, N1 6 세션).

## E6. 교수님 예상 질문

| 질문 | 짧은 답(발표용) | 상세 |
|---|---|---|
| 왜 Isolation Forest인가? | 장애 라벨 없이 정상만으로 학습할 수 있는 가벼운 비지도 이상 탐지라서. | 장애 데이터는 드물고 종류가 많다. IF는 정상 tick(CPU·메모리 평균·기울기 4특징)으로 학습해 "학습된 정상에서 얼마나 벗어났나"를 점수로 낸다. 추론이 가볍다. |
| CPU/Memory밖에 안 쓰는데 왜 AI인가? | 솔직히 지금 시나리오에서는 CPU 하나로도 충분했고, 그래서 AI가 이기지 못했다. | 탐색에서 CPU 평균만으로 정상·압박이 완전히 갈렸다(AUC 1.0). AI의 가치는 여러 지표의 관계가 필요한 상황에서 나올 수 있는데, 그런 상황(부하 변화·I/O·이웃 경합)을 재현하는 feasibility는 모두 실패했다. 이를 그대로 보고한다. |
| 단순 threshold가 더 낫다면 AI가 필요한가? | 이번 조건에선 필요하다는 근거가 없다. 대신 시스템은 신호 공급자를 바꿔 끼울 수 있다. | guarded 정책·전환·검증 경로는 신호가 IF든 CPU 규칙이든 같다. 효과의 핵심은 "검증된 전환"이고 탐지기는 교체 가능한 부품이다. B/C/K에서 보정 CPU 규칙도 같은 경로로 피해 0을 냈다. |
| 장애를 사전에 감지했다고 말해도 되는가? | 아니다. "정상 패턴 이탈 신호"라고만 말한다. | 무결성 감사에서 이른 신호는 압박 전 시작 직후 저CPU 상태에서 온 것으로 확인됐다. 예측·조기 탐지 성능으로 해석할 수 없다. |
| IF가 정상 상태도 anomaly로 본 이유는? | 학습한 정상 범위가 실제 정상 변동보다 좁았다. | 시작 직후 cpu_mean 0.6–1.35(학습 1.43–1.70)와 정상 운전 중 메모리 working-set 감소(memory_slope)가 학습에 없던 패턴이라 이상으로 나왔다(탐색: 정상 구간 IF 이상 tick을 가르는 특징 memory_slope AUC 0.84). |
| A/B는 전환하면 당연한 결과 아닌가? | 맞다, 그래서 "AI가 좋다"가 아니라 "검증된 전환이 실제로 피해를 줄인다"는 결과로만 쓴다. | 실제 클러스터에서 신호 → 정책 → 승격 → 트래픽 이동 → 사용자 SLO 개선이 끝까지 이어지는지는 측정해야 안다(카운터·SLO로 5/5 확인). 탐지 우수성 주장은 하지 않는다. |
| Promotion 성공과 실제 복구는 어떻게 구분했나? | API 성공이 아니라 새/이전 Pod의 실제 요청 카운터와 사용자 SLO로 판정했다. | 정책이 각 Pod IP의 `/metrics`에서 `vllm:request_success_total`을 직접 읽어 60초 창에서 새 Pod ≥30 증가·이전 Pod ≤2를 확인하고, Pod 이름·UID·IP·재시작 여부를 앞뒤로 대조한다. 읽지 못하면 UNKNOWN(0으로 채우지 않음). |
| 왜 Preview Pod를 미리 띄워 두나? | LLM 모델 로딩이 느려서, 장애 때 새로 띄우면 이미 늦다. | preview는 트래픽 없이 대기하다가 승격 순간 Service selector만 바뀌어 0.5초 안에 전환된다. 비용(자원 2배)은 한계로 인정. 반대 노드 배치(anti-affinity)로 같은 노드 문제를 피한다. |
| AI가 들어가는 부분은 어디인가? | 이상 점수(Isolation Forest) 한 곳. 조치 결정은 규칙 기반 guarded 정책이다. | AI는 "정상에서 벗어났다"는 신호만 낸다. 그 신호를 언제 믿고 행동할지(신호 나이, 시계 오차, 잠금, 원장, 1회 제한)는 guarded 정책이 결정한다. |
| 앞으로 개선한다면? | 정상 학습 데이터를 넓히고, 다변량이 정말 필요한 상황을 먼저 증명한 뒤 비교하겠다. | (1) 이미 모은 정상 세션으로 학습 범위 보강(세션 교차 설계 완료, 실행은 승인 대기) (2) 시작 직후 웜업 규칙 사전 등록 (3) CPU per token 같은 관계 특징 검토 (4) 표본 확대. |

## E7. detector 한계 - 슬라이드 배치 (2026-10-10 갱신)

**MAIN (한 장, 한 문장 + 그림 1개)**
> "현재 IF는 정상 패턴 이탈을 탐지했지만, 실제 정상 운전의 메모리 패턴을 충분히 대표하지 못해 불필요 anomaly가 발생했다."
- 그림 1개만: `experiments/results/exploratory_c1_calibration_memory_20261010/plot4-memory-baseline-by-session.png`(Memory mean baseline by session) 또는 `plot2-memory_mean-by-session.png`.
- MAIN에서 뺄 것: Pod-relative memory 실험, 신호 99회, 분산 비, 보강 fold 표.

**APPENDIX (질문 대응용)** - 근거 문서 `docs/history/C1-DETECTOR-LIMITATIONS-AND-FUTURE-WORK-20261010.md`
1. c1 integrity audit: 계산 정확, F1(시작 직후 저CPU 이상) - 조기 탐지로 해석 불가
2. calibration representativeness: CPU 범위 안 98.7 %, memory_mean 16.9 %(그림 3: 점수 분포 보정 vs N1)
3. memory_mean session baseline: 분산의 99.5 %가 세션 사이, 세션마다 다른 preview Pod 수준과 연관
4. memory_slope signal association: 3연속 신호를 이룬 평가의 90 % 이상이 메모리 감소 구간, 신호 29/31이 600 s 이후(그림 5)
5. normal coverage 확대: NOT PROMISING(1/6 개선)
6. Pod-relative memory: NOT PROMISING(세션 차이는 줄었지만 불필요 신호 24 → 99)

## E8. 최종 후속 연구 방향 (2026-10-11 추가, 미구현 제안)

> 2026-10-12: 이 방향은 Phase 2로 구현·검증되었다 - 현재 CPU-vLLM 환경에서 사전등록 규칙으로 cpu-threshold-c 선택(Gate 2 CPU 5/5 · c3-A IF 0/5), 선택된 detector로 live 복구 3/3 성공(새 Pod +60 / 이전 Pod +0), SHADOW 권한 누수 0, 최종 판정 `PHASE2_ARCHITECTURE_VALIDATED`. 발표 문구·말하지 않는 것은 [PHASE2-FINAL-RESULT-20261012.md](PHASE2-FINAL-RESULT-20261012.md) §6–§7. 아래는 2026-10-11 당시 기록이다.

**한 문장**: "환경별 workload와 병목 특성에 따라 적합한 detector를 선택할 수 있도록 Detection Layer를 교체 가능한 구조로 확장하고, 선택된 detector의 anomaly signal은 기존 Recovery Policy와 Argo Rollouts 기반 복구 파이프라인에 공통으로 연결하는 방향을 후속 연구로 제안한다."
- 근거(결과에서 도출): 단순 CPU 압박에서는 보정 CPU 임계값이 충분히 강했고, IF는 정상 coverage에 민감했으며(c3-A로 불필요 신호 c1 14 → 1), 현재 환경에서는 다변량 ML의 추가 우월성이 검증되지 않았다(Axis 2 NO_GO). 반면 복구 경로는 신호 출처와 무관하게 같게 동작했다.
- 구조: Environment → Detector Validation / Selection → Active Detector → Recovery Policy → Argo Rollouts → Traffic Switch → Verification.
- 말하지 않는다: 자동 benchmarking·자동 detector 선택·여러 detector 자동 학습을 "구현했다".
- 상세: `docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md` §3–§4.

## 발표 문구 체크

쓰지 않는다: "장애 예측", "미래 장애 감지", "조기 장애 예측", "미리 감지", "AI가 장애를 미리 알아냄", "AI가 더 정확/빠름", "오탐이 적다", "유의미하다(p<0.05)".
쓴다: "정상 패턴 이탈", "Anomaly", "이상 신호", "Recovery", "Traffic Switch", "Verified", "검증된 전환이 SLO 피해를 줄였다".
대시보드 제목은 **"AIOps Anomaly-Triggered Recovery Demo"**로 변경했고(2026-10-10), 타임라인 5단계는 "정상 서비스 → 정상 패턴 이탈 감지 → Recovery → Service Switch → Traffic Verified"다(표시 문구만 변경, 데이터 경로 그대로).
