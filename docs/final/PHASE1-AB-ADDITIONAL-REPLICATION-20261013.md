# Phase 1 A/B 추가 반복(Stage 2) 결과 - 2026-10-13

> 이 문서는 새 요약이다. 기존 Phase 1 공식 결과(ab01~ab05)·`FINAL-EVIDENCE-SUMMARY-20261010.md`·Phase 2 evidence는 수정하지 않았다.
> 모든 수치는 저장된 summary / contract / probe CSV / 동결 분석기 출력에서 다시 읽어 확인한 값이다.

## 1. 목적

이미 끝난 공식 A/B 비교(Stage 1, 5쌍)를 **같은 동결 조건**에서 추가 반복해 같은 방향이 다시 나오는지 확인한다. 새 실험 설계가 아니라, 계약 r1 §7에 처음부터 사전 등록돼 있던 2단계(ab06~ab15) 경로를 사용했다.

- A = guarded `record_only`(전환 없음), B = guarded `act`(IF 신호 → 정책 → Argo 승격, 실행당 최대 1회)
- 동결 조건: v3.3-c1 IF(threshold −0.07667538512242333, 3연속, 쿨다운 60초), CPU 압박 1·2·3·4 workers × 90초, probe 1 RPS·`max_tokens=1`, 관찰 600초, SLO v3(지연 > 0.648초 30초 지속 등), 분석기 `guarded_ab_analysis.py` 동결본
- 실행기 `run_guarded_pilot.py`, 계약 `docs/design/phase8-v33c1-guarded-ab-comparison-contract-r2-20261007.md`, 분석 규칙 `docs/design/phase8-v33c1-guarded-ab-analysis-rules-20261007.md` - 모두 변경 없음

## 2. Stage 1 - 공식 결과(변경 없음)

| 쌍 | A 누적 SLO 위반(초) | B(초) | D = A − B |
|---|---|---|---|
| ab01 | 204.504 | 0 | 204.504 |
| ab02 | ≥210.83 (A 하한) | 0 | 210.83 |
| ab03 | 201.68 | 0 | 201.68 |
| ab04 | 287.79 | 0 | 287.79 |
| ab05 | 207.396 | 0 | 207.396 |

유효 5쌍, B < A 5/5, D 중앙값 207.396초, 평균 222.44초, 부호 검정 양측 p = 0.0625(참고값).
근거 파일 `experiments/results/capstone_guarded_ab_v1/ab05-20261007/analysis-official-ab01-ab05-20261007T130127Z.json`(sha256 `86dcbf1e4ac6e4dc43e7185306193ac988f883392a2577ee8a218e25d3364251`, 수정 시각 2026-10-07T13:01Z 그대로). 새 분석 파일의 ab01~ab05 값과 정확히 같다.

## 3. Stage 2 - 사전 등록 추가 반복

- 중단 기준(결과 전 기록): `ab06-20261013/stage2-stopping-rule-20261013.json` - 생성 2026-10-13T08:41:14Z, Stage 2 첫 실행 파일(ab06a preflight, 08:46:49Z)보다 앞섬
  - 목표 유효 3쌍, 예정 ID ab06~ab08
  - 무효 실행은 기록 보존, 대체는 다음 ID(ab09~, 계약 상한 ab15)
  - 유효 3쌍 도달 시 종료, 결과가 좋아도 추가 실행 없음, 나쁜 결과도 제외하지 않음
  - Stage 1 결과 소급 변경 없음, 계약상 조기 중단 규칙 유지
- 결과: 시도 3쌍, 유효 3쌍, 무효 0, 대체 실행 없음, 주입 전 승격 0, B 메커니즘 실패 0 → **중단 기준 충족, ab09 이후 실행하지 않음**

## 4. 쌍별 결과

| 항목 | ab06 | ab07 | ab08 |
|---|---|---|---|
| A 누적 SLO 위반 | **202.578초** | **238.484초** | **294.715초** |
| B 누적 SLO 위반 | 0초 | 0초 | 0초 |
| D = A − B | 202.578 | 238.484 | 294.715 |
| A·B 유효(V1~V7) | valid / valid | valid / valid | valid / valid |
| injection_valid (A/B) | true / true | true / true | true / true |
| 요청 A / B | 602 / 601 | 597 / 601 | 601 / 600 |
| 실패·timeout | 0 / 0 | 0 / 0 | 0 / 0 |
| 최대 요청 공백 A / B | 1.878 / 1.508초 | 3.2 / 1.49초 | 1.518 / 1.571초 |
| CPU 압박 전달(raw-counter-v2.3) | verified / verified | verified / verified | verified / verified |
| 하한 값 | 없음 | 없음 | 없음 |
| 시작 노드 A = B | sj-worker | sj-worker2 | sj-worker |
| A 승격·조치 | 없음 | 없음 | 없음 |
| B 승격·조치 | `promoted_verified` 1회 | 1회 | 1회 |
| 주입 관측 → 승격 명령(B) | 09:49:06 → 09:49:31Z | 13:17:27 → 13:17:51Z | 15:33:50 → 15:34:21Z |
| 주입 전 승격 | 아님 | 아님 | 아님 |
| cleanup_errors (A/B) | [] / [] | [] / [] | [] / [] |
| canonical end (A/B) | ok / ok | ok / ok | ok / ok |
| B D6(420→30 원복) | promoted_service_ok, 복원 | 복원 | 복원 |

Stage 2 기술 통계(n = 3, 검정 없음 - 분석 규칙상 유효 쌍 < 5): B < A 3/3, D 중앙값 238.484초, 평균 245.259초, 범위 202.578~294.715초.

## 5. 실제 Service 전환 / 트래픽 확인(B)

| 쌍 | selector 전 → 후 | 이전 Pod(UID, 노드) | 새 Pod(UID, 노드) | 새 Pod 요청 증가 | 이전 Pod 요청 증가 | 결과 |
|---|---|---|---|---|---|---|
| ab06 | `59f65899d8` → `746d5d6588` | `…g4zvq` (270e73fd…, sj-worker) | `…2ss8f` (e48583fd…, sj-worker2) | +60 | +0 | verified |
| ab07 | `746d5d6588` → `d774768f6` | `…2ss8f` (e48583fd…, sj-worker2) | `…zfrlk` (76e876f9…, sj-worker) | +60 | +0 | verified |
| ab08 | `d774768f6` → `845756749f` | `…zfrlk` (76e876f9…, sj-worker) | `…xwltv` (202fb0c6…, sj-worker2) | +61 | +0 | verified |

기준(정책 이미지 상수): 전환 확인 +10초 뒤 60초 창에서 새 Pod ≥ 30, 이전 Pod ≤ 2, endpoint 표본 ≥ 3(실측 12). 요청 카운터는 Pod IP `/metrics`에서 직접 읽었다. 이는 짧은 서비스 확인이며 사용자 품질 개선 자체의 입증은 아니다(사용자 품질은 §4의 SLO 지표).

## 6. stopping rule 충족

유효 쌍 3/3(ab06·ab07·ab08)으로 사전 기준을 충족해 Stage 2를 종료했다. ab09 이후 live run은 실행하지 않았고 실행하지 않는다.

## 7. 전체 8쌍 - 참고용 합산 관측(공식 결과 아님)

Stage 1 공식 5쌍을 8쌍으로 소급 변경하지 않는다. 아래는 같은 동결 분석기가 공식 ID 규칙(ab01~ab15)으로 모은 누적 관측이다. 분석기는 유효 쌍 < 10·시도 < 15라서 이를 `stage1_descriptive`(최종 아님)로 표시하고, 계약의 최종 확인 검정(Wilcoxon)은 계산하지 않았다.

- 유효 8쌍, B < A 8/8, 같음 0, B > A 0
- D 중앙값 209.113초, 평균 230.997초, 범위 201.68~294.715초
- 부호 검정 양측 p = 0.0078125 (**참고값**)
- 민감도(A 하한이 있는 ab02 제외): 7/7, D 중앙값 207.396초, p = 0.015625 (**참고값**)

## 8. 해석 한계

1. B = 0초는 detector가 장애를 완벽하게 예측했다는 뜻이 아니다.
2. 전환으로 압박받던 기존 Pod가 Service 트래픽 경로에서 빠진 **조치의 직접 효과**다.
3. A/B는 복구 메커니즘(신호 → 정책 → 승격 → 전환)의 효과와 실제 트래픽 이동을 검증하는 실험이다.
4. detector 자체의 우월성을 입증하는 실험이 아니다(두 arm이 같은 detector를 쓰며, F1: 주입 전 저CPU 구간에서 IF 신호가 쌓이는 해석 문제는 그대로).
5. 쌍 안의 실행 순서는 항상 A → B로 고정이다(시간대·이월 효과를 통계적으로 분리하지 못함).
6. 표본 수가 작다(Stage 1 5쌍, Stage 2 3쌍).
7. Stage 1 공식 결과와 Stage 2 추가 반복은 분리해서 보고한다.
8. 전체 8쌍은 참고용 합산 관측일 뿐이며, p 값은 참고값이다. 유의성·일반화·완전한 재현성을 주장하지 않는다.

환경 메모(판정에 쓰지 않음): 실행 중 worker MemAvailable 최저 3.0~3.3 GiB(중단 기준 1 GiB 감소에 도달하지 않음), calico-node 메모리 4.8~6.4 GiB로 완만히 증가, node-exporter restart 93/3/0 변화 없음. 노드 3개 모두 Ready, 압박 조건 없음.

## 9. provenance / 주요 evidence

- 동결 분석기 `experiments/guarded_ab_analysis.py` sha256 `51886c97152d639e4a4a86a7370ac19c8a91b2f78a8fb1b10d279455985b3bbd`(Stage 1 실행 시 기록값과 같음)
- 최종 분석 파일 `experiments/results/capstone_guarded_ab_v1/ab08-20261013/analysis-official-ab01-ab08-20261013T154627Z.json`
  - 파일 sha256 `00c0d43aaa4d8f19ac922640a27ae027cb61f83356f9faadee25f981ca03cb75`
  - result_digest `232024e4d0a2e3e812d9e92beef504af2e2255da37890c3c2d11a1696bb6f9d9`
  - 분석 파일에 기록된 ab06~ab08 summary·contract·run-preflight·probe CSV의 sha256이 현재 파일과 모두 일치(불일치 0)
- 실행기 결과(공식 폴더 `experiments/results/capstone_guarded_ab_v1/`): 실행마다 `summary-*`, `contract-*`, `preflight-*-run-*`, `preflight-*-preflight-only-*`, `c1-live-evidence/stdout/stderr-*`, `ssh-evidence-*`, `pod-events-*`, probe CSV(`experiments/results/probe-*-proposed-812~817-raw.csv`)
- 쌍별 운영 기록 `ab06-20261013/`, `ab07-20261013/`, `ab08-20261013/`
  - `abNNb-act-approval-FINAL.json`(runs = 해당 B 하나, ab05b와 동결 값 동일): ab06b `5dd33794…`, ab07b `7e5f11a3…`, ab08b `2a37bd81…`
  - `clock-offset-pre-abNNb.json`(|offset| + uncertainty: 0.112 / 0.269 / 0.102초, 기준 0.5초): ab06b `e74c619d…`, ab07b `854687ce…`, ab08b `e0e42df9…`
  - `act-switch-abNNb.json`, `revert-record-only-after-abNNb.json`(모드 전환·잠금 해제·원장 확인)
  - `isolate-result.json`(isolated true), `restore-result.json`(restored true), 공식 폴더 `old-policy-isolation-abNN.json`
  - 세션 로그(`session-*`), `session-launcher-*.json`
- 중단 기준 `ab06-20261013/stage2-stopping-rule-20261013.json` sha256 `e8a4a7a044ee39bd665caa33abca364239a1d0ece0ee88b5e02535452b887561`
- ab06b 승인 초안 `ab06-20261013/ab06b-act-approval-DRAFT.json`(status draft, 실행기 거부 확인 후 보존)

## 10. 발표에 사용할 수 있는 문장

- 메인: "사전 고정된 공식 비교 5쌍에서 5/5 같은 방향을 확인했고, 사전 등록된 추가 반복 3쌍에서도 3/3 같은 방향을 재확인했다."
- 짧은 표기: "공식 5쌍 5/5 + 추가 반복 3쌍 3/3에서 동일 방향 확인"
- 보충: "실제 전환군에서는 새 Pod의 요청 증가(+60/+60/+61)와 이전 Pod의 요청 증가 없음(+0)까지 직접 확인해 Service 트래픽이 실제로 이동했음을 검증했다."
- 쓰지 않는 표현: 8/8을 공식 결과처럼 표기, "통계적으로 증명", "일반적으로 효과적", "유의성 확보", "재현성 완전 증명", "AI 탐지가 우수".
