# 실험

3-way(self-healing / 고정임계치 / 제안방식) × 3종 시나리오 × 10회+ 반복 실험 자동화 및 결과 수집.

- `run_all_scenarios.py`: 전체 실험 반복 실행
- `run_calibration.py`: 무개입 대조군 실행
- `collect_metrics.py`: 종단간 지연시간 분해, 가용성, 정확성 등 전체 평가지표 추출
- `results/`: 실험 결과 (CSV, `.gitignore`에 의해 커밋되지 않음 — 별도 백업 필요)
