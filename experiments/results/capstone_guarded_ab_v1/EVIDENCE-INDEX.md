# Phase 1 A/B 핵심 근거 목록 (capstone_guarded_ab_v1)

이 폴더의 원자료 전체(약 2,400개 파일)는 저장소 밖에 보존하고, **결과를 검증하는 데 필요한 최소 핵심 파일 36개만** Git에 넣었다(2026-10-14). 파일은 로컬 canonical 파일을 내용 변경 없이 그대로 추가했다(아래 SHA-256은 추가 시점 로컬 파일 바이트 기준).

- 계약: `docs/design/phase8-v33c1-guarded-ab-comparison-contract(-r2)-20261007.md`, 분석 규칙 `docs/design/phase8-v33c1-guarded-ab-analysis-rules-20261007.md`
- 실행기·분석기(동결): `experiments/run_guarded_pilot.py`, `experiments/guarded_ab_analysis.py`(sha256 `51886c97152d639e4a4a86a7370ac19c8a91b2f78a8fb1b10d279455985b3bbd`)
- 결과 요약 문서: `docs/final/PHASE1-AB-ADDITIONAL-REPLICATION-20261013.md`

## 추가한 파일과 이유

| 파일 | SHA-256(앞 16자) | 바이트 | 이유 |
|---|---|---|---|
| `ab05-20261007/analysis-official-ab01-ab05-20261007T130127Z.json` | `86dcbf1e4ac6e4dc…` | 35408 | Stage 1 공식 분석(5쌍, 2026-10-07 원본 그대로) |
| `ab08-20261013/analysis-official-ab01-ab08-20261013T154627Z.json` | `00c0d43aaa4d8f19…` | 53843 | Stage 2 종료 후 동결 분석기 출력(실행별 원자료 경로·해시, 유효성, 쌍 판정, result_digest `232024e4d0a2e3e8…`) |
| `ab06-20261013/stage2-stopping-rule-20261013.json` | `e8a4a7a044ee39bd…` | 1782 | 결과 전 고정한 Stage 2 중단 기준 |
| `ab06-20261013/ab06b-act-approval-FINAL.json` | `5dd337945c3d8e4f…` | 2235 | B act 승인(runs = ab06b 하나) |
| `ab06-20261013/clock-offset-pre-ab06b.json` | `e74c619ddd8f4050…` | 6349 | B 직전 시계 오차(≤ 0.5 s 확인) |
| `ab06-20261013/act-switch-ab06b.json` | `1e6cc0fd89278d8b…` | 2226 | act 전환·잠금 인계·원장 확인 |
| `ab06-20261013/revert-record-only-after-ab06b.json` | `bca3ef5e59e77cf6…` | 1975 | record_only 복귀 확인 |
| `ab06-20261013/isolate-result.json` | `2cd6a43c25159fd5…` | 714 | 기존 recovery-policy 격리 결과 |
| `ab06-20261013/restore-result.json` | `53a6940c927c8e90…` | 619 | 기존 recovery-policy 복원 결과 |
| `old-policy-isolation-ab06.json` | `5b21a5aa538c196f…` | 645 | 격리 전 원래 값 기록 |
| `contract-guarded_pressure_record_only-ab06a-capstone-guarded-ab-v1-20261007.json` | `a2c805910834017e…` | 2938 | 실행 계약(조건·모델 해시·comparison) |
| `summary-guarded_pressure_record_only-ab06a-capstone-guarded-ab-v1-20261007.json` | `23a29dbac85f5146…` | 28765 | 실행 요약(결과·정리·canonical end·CPU 카운터) |
| `contract-guarded_pressure_act-ab06b-capstone-guarded-ab-v1-20261007.json` | `9f62295ecee18199…` | 5208 | 실행 계약 |
| `summary-guarded_pressure_act-ab06b-capstone-guarded-ab-v1-20261007.json` | `42d86b75824046e1…` | 130818 | 실행 요약(승격·selector 전환·새/이전 Pod 요청 카운터 service_check 포함) |
| `ab07-20261013/ab07b-act-approval-FINAL.json` | `7e5f11a38bbdb011…` | 2197 | B act 승인 |
| `ab07-20261013/clock-offset-pre-ab07b.json` | `854687ced805fd06…` | 6345 | 시계 오차 |
| `ab07-20261013/act-switch-ab07b.json` | `b27e7c42684475ee…` | 2110 | act 전환 확인 |
| `ab07-20261013/revert-record-only-after-ab07b.json` | `9e5f30c27dc8ea21…` | 1975 | record_only 복귀 |
| `ab07-20261013/isolate-result.json` | `fe5270683ca80f48…` | 715 | 격리 결과 |
| `ab07-20261013/restore-result.json` | `bdda70b53b376f6e…` | 618 | 복원 결과 |
| `old-policy-isolation-ab07.json` | `376467a05441bbe3…` | 645 | 격리 전 원래 값 |
| `contract-guarded_pressure_record_only-ab07a-capstone-guarded-ab-v1-20261007.json` | `48ee16a00d4aae52…` | 2938 | 실행 계약 |
| `summary-guarded_pressure_record_only-ab07a-capstone-guarded-ab-v1-20261007.json` | `60db0b7e91bcdaae…` | 28780 | 실행 요약 |
| `contract-guarded_pressure_act-ab07b-capstone-guarded-ab-v1-20261007.json` | `5becf87aeee6f4e9…` | 5171 | 실행 계약 |
| `summary-guarded_pressure_act-ab07b-capstone-guarded-ab-v1-20261007.json` | `7e4bce790be49e5a…` | 133986 | 실행 요약(트래픽 확인 포함) |
| `ab08-20261013/ab08b-act-approval-FINAL.json` | `2a37bd811be89cdc…` | 2199 | B act 승인 |
| `ab08-20261013/clock-offset-pre-ab08b.json` | `e0e42df93186ed8b…` | 6349 | 시계 오차 |
| `ab08-20261013/act-switch-ab08b.json` | `486e49cbd9eb77b9…` | 2111 | act 전환 확인 |
| `ab08-20261013/revert-record-only-after-ab08b.json` | `f33e7cbee23e1da6…` | 1975 | record_only 복귀 |
| `ab08-20261013/isolate-result.json` | `0abf0f57bfae781c…` | 713 | 격리 결과 |
| `ab08-20261013/restore-result.json` | `9afbf9de537d349b…` | 618 | 복원 결과 |
| `old-policy-isolation-ab08.json` | `f4febeb12c9f83a6…` | 645 | 격리 전 원래 값 |
| `contract-guarded_pressure_record_only-ab08a-capstone-guarded-ab-v1-20261007.json` | `5690dbbc812f7b02…` | 2938 | 실행 계약 |
| `summary-guarded_pressure_record_only-ab08a-capstone-guarded-ab-v1-20261007.json` | `c21194fe7b6ad52f…` | 28904 | 실행 요약 |
| `contract-guarded_pressure_act-ab08b-capstone-guarded-ab-v1-20261007.json` | `897c1651da05117b…` | 5172 | 실행 계약 |
| `summary-guarded_pressure_act-ab08b-capstone-guarded-ab-v1-20261007.json` | `c3b264d87489978b…` | 137024 | 실행 요약(트래픽 확인 포함) |

## 넣지 않은 것과 이유(저장소 밖 보존)

| 제외 | 이유 |
|---|---|
| ab01~ab05 실행별 summary·contract·운영 기록 | Stage 1은 공식 분석 파일(위)로 결과를 고정했고, 이번 정리 범위는 Stage 1 분석 + Stage 2 핵심 근거로 한정했다 |
| `c1-live-evidence-*.jsonl`, `c1-live-stdout/stderr-*.log` | 탐지기 tick 단위 원자료·전체 로그(대용량). 판정 결과는 summary·분석 파일에 있음 |
| `ssh-evidence-*` 폴더 | 노드 SSH 안전 점검 원자료(실행당 수십 파일) |
| `pod-events-*.json` | Pod 이벤트 원자료 |
| `preflight-*`(run·preflight-only) | 사전검사 원자료. 분석 파일이 경로·SHA-256을 기록 |
| probe raw CSV(`experiments/results/probe-*-proposed-8NN-raw.csv`) | 요청 단위 원자료(.gitignore 대상). 분석 파일이 경로·SHA-256을 기록 |
| `session-*` 폴더·`session-launcher-*.json` | 세션 프로세스 로그 |
| `ab06b-act-approval-DRAFT.json` | 실행기가 거부하는 초안(실행에 쓰이지 않음) |
| 약 950 MB의 이전 실험 원자료 | 저장소 밖 보존(README Limitations 재현성 범위) |

**재현 범위:** 이 파일들만으로 쌍별 값·유효성 판정·트래픽 확인 결과를 읽을 수 있다. 동결 분석기를 처음부터 다시 실행하려면 저장소 밖에 보존한 probe CSV·run-preflight 원자료가 필요하다(분석 파일에 각 원자료의 SHA-256이 남아 있어 대조 가능).
