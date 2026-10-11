# 장애 시나리오

> Status: **HISTORICAL**(초기 계획). 실제 사용한 부하·압박 생성기와 결과는 루트 `README.md`와 `docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md` 참조.

Chaos Mesh 장애 주입 3종(Pod kill / OOM / 네트워크 지연) + 무개입 대조군(calibration run) 스크립트.

무개입 대조군은 개입 없이 장애를 진행시켜 "실제 장애 발생 시점"의 기준값을 측정한다 — 제안 방식의 "선행 탐지 시간" 지표 계산에 필요하다.
