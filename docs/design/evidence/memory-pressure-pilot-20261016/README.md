# 메모리 압박 Pilot - 원본 증거 (2026-10-16)

> **DEVELOPMENT / NOT OFFICIAL** - 사전등록된 공식 실험이 아니다. 공식 Phase 1·Phase 2 결과와 합치지 않는다.

안전 규칙 안에서 허용되는 메모리 압박 중 가장 센 **1500MB × 120초**를 active vLLM Pod(sj-worker2)에 2회 주입했다(사용자 승인, 안전 감시 기존 어댑터 그대로).
1600MB는 사전등록 §52.2(작업 메모리 + 1600MB가 5GiB 상한까지 128MiB 이상 남아야 함)를 지금 작업 메모리(약 3.41GiB)로는 통과하지 못해 **주입하지 않았다**(규칙을 낮추지 않았다).

| 항목 | 1회 (13:45Z) | 2회 (13:52Z) |
|---|---|---|
| 어댑터 안전 점검 | 통과 | 통과 |
| 공식 SLO v3 위반(`t_slo`) | 없음 | 없음 |
| 성공률 | 100% | 100% |
| P95 최고(임계 0.648초) | 0.889초(순간) | 0.638초 |
| 작업 메모리 | 3.41 → 4.85 GiB | 3.41 → 4.81 GiB |
| 재시작·OOM | 0·0 | 0·0 |
| 주입 구간 CPU 최대 | 1.647 core | 1.647 core |
| 분류(`mf_stage0_analysis.py`) | C. 저하 없음 | C. 저하 없음 |

- 기준선은 라운드 안의 기준선(약 1~3분)이다. 계획했던 20분 기준선 세션은 하지 않았으므로 CPU 정상 범위 판정은 약하다.
- 분석 규칙(원인 귀속 R1~R3, 분류 A~D)은 실행 전에 고정했고 도구 자체 점검을 통과했다: `python experiments/mf_stage0_analysis.py --selftest`
- 재분석: `python experiments/mf_stage0_analysis.py <summary.json>` (Prometheus port-forward 필요)

## 파일 (byte-exact 저장, `.gitattributes -text`)
| 파일 | 크기(byte) | sha256 |
|---|---|---|
| `explore-memory_pressure-native-1500mb-120s-20261016T134514Z-summary.json` | 24,736 | `014ae73b4251d2863e87db3501549f39c0e2ac64924233e34ab28b1927a465c7` |
| `explore-memory_pressure-native-1500mb-120s-20261016T135228Z-summary.json` | 24,301 | `9c6b50ce3d231e62d3ed829466ad40d2b4cbee3fbd8bdbf9049ef32b51397ca3` |
| `mf-analysis-explore-memory_pressure-native-1500mb-120s-20261016T134514Z.json` | 2,347 | `a2cef60374b8f364740d22dabc99ccf2fe86882eaae5e912666db23748be4ab1` |
| `mf-analysis-explore-memory_pressure-native-1500mb-120s-20261016T135228Z.json` | 2,244 | `434785fafb560ef783cfcd9dd74071a44d6dab4cd8ac795026fb30314cd39ffb` |
| `probe-explore-memory_pressure-native-1500mb-120s-20261016T134514Z-native-1-raw.csv` | 41,602 | `0324a97f79a447cb0e7601ba21766955e64171837ac67e02378f7fe2601cda95` |
| `probe-explore-memory_pressure-native-1500mb-120s-20261016T135228Z-native-1-raw.csv` | 41,746 | `7d81dd2c77b30c936d71bfb1f321a825abad1a05813694cfa56c8bcd99ee597b` |
