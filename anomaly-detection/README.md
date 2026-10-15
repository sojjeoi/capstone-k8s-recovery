# anomaly-detection - Detection Layer

운영 지표(Prometheus의 CPU·메모리)에서 15초마다 60초 창 특징(`cpu_mean`, `cpu_slope`, `memory_mean`, `memory_slope`)을 만들고, detector가 3회 연속 이상이면 Recovery Policy로 신호를 보내는 쪽 코드. 복구 쪽([recovery-policy/](../recovery-policy/README.md))과 분리돼 있어 detector를 바꿔도 복구 경로는 같다.

## 현재 환경의 detector (Phase 2 선택 결과)

| 역할 | detector | 정의 파일 |
|---|---|---|
| **ACTIVE** (복구 신호 송신 권한) | 보정 CPU 임계값 `cpu_mean > 1.6842544293089357`, 3회 연속 | [bck_rules/rule-spec-v1.json](bck_rules/rule-spec-v1.json) (arm C) |
| **SHADOW** (기록 전용, 송신·조치 권한 없음) | c3-A Isolation Forest (정상 범위를 넓혀 재학습) | [v3/model_c3_normal_coverage/artifacts/](v3/model_c3_normal_coverage/artifacts/) (해시·임계값·feature schema 메타데이터. `model.pkl`·`scaler.pkl`은 커밋하지 않음) |

어느 detector가 ACTIVE인지는 이 폴더가 아니라 Active Profile 파일이 정한다: [experiments/results/phase2_selection/active-profile.json](../experiments/results/phase2_selection/active-profile.json). 선택 규칙과 근거는 [docs/final/PHASE2-FINAL-RESULT-20261012.md](../docs/final/PHASE2-FINAL-RESULT-20261012.md).

## 어디서부터 읽나

1. ACTIVE/SHADOW 동시 실행(같은 평가 tick, ACTIVE만 송신): [experiments/phase2_detector_selection/p2_executor.py](../experiments/phase2_detector_selection/p2_executor.py)
2. 평가 1회(특징 계산·입력 무결성 검사): [c1_shadow_detector.py](c1_shadow_detector.py), [features.py](features.py), [input_integrity.py](input_integrity.py)
3. 3연속 판정·송신 결과 분류: [c1_bck_live_detector.py](c1_bck_live_detector.py)
4. Recovery Policy용 v2 신호 만들기: [c1_policy_link.py](c1_policy_link.py)

## 그 밖의 파일

- `c1_live_detector.py`: Phase 1 A/B에서 쓴 c1 IF 실전 탐지기(송신은 guarded 정책으로)
- `c1_bck_shadow_pair.py`: Phase 1 정상 운전(N1) 비교용 기록 전용 탐지기
- `fixed_threshold.py`: 초기 3-way 비교 설계의 고정 임계치 baseline (historical)
- `score_server.py`: 초기 실시간 IF 점수 서버. 지금도 `advance_streak` 등 판정 함수가 재사용된다.
- `v3/model_v33_c1/`: 초기 IF(c1) 학습·artifact 메타데이터 (historical, Phase 1 비교 대상)
- 그 밖의 `v3/` 하위 폴더, `train.py`, `collect_*.py`, `regime_configs/`: 초기 모델 단계 자료 (historical)
- `test_*.py`: 테스트
