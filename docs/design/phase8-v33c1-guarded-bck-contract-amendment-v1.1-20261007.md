# B/C/K 계약 개정 v1.1 (guarded-bck-v1.1) — 실행 진입점(CLI) 호환성 결함 수정

작성: 2026-10-07~08(KST). **상태: FROZEN v1.1 (사용자 승인) — 실행 승인 아님.** 동결 해시는 `phase8-v33c1-guarded-bck-freeze-manifest-v1.1-20261007.json`.

## 1. 관계

- **v1(보존, superseded):** 계약 `phase8-v33c1-guarded-bck-comparison-contract-draft-20261007.md`(sha256 `c363e9e66c090adef1874a15f38b41e1adc6c9a2e3d9b583497030770259841e`, 수정 없음), manifest `phase8-v33c1-guarded-bck-freeze-manifest-20261007.json`(sha256 `77164a0e157a8cf4ed5c63faf4e49cdcd4f6a09c00c8c245f7ccc9c7a1dba365`, 수정 없음), v1 runner 원본 바이트 사본 `bck-v1-superseded/run_guarded_bck.py.v1`(sha256 `ea471e5fbe085200230e04196b524e1bc201129d77ce74f68ca0a1c5142ea9f3` = v1 manifest 기록값).
- v1은 **live 결과 수집 전에 session wrapper → runner CLI 비호환을 발견해 superseded**됐다. v1 계약의 모든 연구 조건(§1–§15)은 v1.1에 그대로 적용되며, 이 문서는 아래 §3의 한 가지만 바꾼다.
- 실행 계약 파일의 `contract_doc`는 v1 계약 경로 그대로다(runner·분석기 상수 불변). 적용 버전은 v1.1 manifest(runner 해시)로 식별한다.

## 2. 발견 경위 (결과 전)

- 발견 시점: 2026-10-07, 사용자 승인 bc90 pilot 시작 **전** 실행 절차 점검 중.
- 상태: **bc90b/c/k ID 미사용, 결과 폴더(`experiments/results/capstone_guarded_bck_v1/`) 없음, 클러스터 조회·변경 없음, CPU pressure 없음, promotion 없음, 결과·SLO 미확인.**
- 원인: A/B와 같은 운영 절차의 세션 실행기 `experiments/guarded_pilot_session.ps1`(수정 없음)은 runner에 항상 `--run <Run> --mode <Mode> --policy-url … --session-file … [--act-approval …]`을 넘긴다(83–86행). v1 `run_guarded_bck.py`의 CLI에는 `--mode`가 없어 argparse가 `unrecognized arguments: --mode act`로 즉시 종료했다(로컬 확인, 클러스터 접근 전). v1 테스트는 실행 함수(`run_bck`)를 직접 불러 CLI 진입점을 검사하지 않았다.
- 성격: 결과에 의존하지 않는 결정적 실행 진입점 결함(A/B r2 §6 correction 원칙과 같은 기준). 사후 조정 아님.

## 3. 변경 (이것만)

`experiments/run_guarded_bck.py` `main()`에 한 인자 추가(주석 1줄 포함 +2줄):
```python
p.add_argument("--mode", required=True, choices=[MODE_ACT])
```
- `--mode act` → 허용. `--mode record_only`·그 밖의 값·누락 → argparse 거부(종료 코드 2, 아무 작업 전). B/C/K는 모두 실제 전환 arm이므로 act가 아닌 상태로 시작하지 않는다.
- 새 테스트 `experiments/test_run_guarded_bck_cli.py`(15개): ps1과 같은 인자 형식으로 runner를 subprocess로 실행 — 실제 스크립트로 비-act mode·필수 인자 누락·잘못된 run ID·토큰 없음 거부, 결과 폴더만 tmp로 바꾼 같은 `main()`으로 `--mode act` 진입(승인 기록 확인 단계에서 fail-closed)과 dry-run 계약(mode act, arm, 임계값) 확인, 실제 결과 폴더 미생성 확인, ps1이 여전히 `--mode`를 넘김 확인.
- 기존 v1 테스트 파일(`test_run_guarded_bck.py` 등)은 수정하지 않았다.

## 4. 바뀌지 않은 것

판단 알고리즘(공통 tick·세 규칙·3연속·쿨다운 60), 임계값 B `-0.07667538512242333` · C `1.6842544293089357` · K `2.7`, 규칙 명세(`rule-spec-v1.json`, 해시 동일), 탐지기·분석기 코드(해시 동일), signal identity·context, arm 정의, 압박 조건, 관찰 600초, 서비스 확인, cleanup(D5·D6·canonical), 유효성 규칙, 지표, 통계 단계, 반복 수, Latin-square 순서, 승인 단위(D4), 세션 실행기 `guarded_pilot_session.ps1`.

## 5. 검증

- B/C/K 테스트: 탐지기 22, runner 23(v1 그대로), 분석기 34, CLI 15 — 전부 통과.
- experiments 전체 회귀: 2114 통과, 2 실패, 3 skip.
  - 기존 frozen A/B 테스트 1건(`test_guarded_ab_analysis.py::test_real_folder_dry_run_excludes_rehearsals_and_has_no_official_pairs`)은 ab01 이전의 공식 pair 0개 가정을 갖고 있어 현재 실제 결과 폴더에서는 실패한다. B/C/K 변경으로 인한 회귀가 아니며 frozen 파일이므로 수정하지 않았다.
  - `test_ssh_check_evidence.py::test_timeout_with_partial_output_fails_and_preserves_partial`: 전체 실행 중 1회 실패, 단독 재실행 3/3 통과 — 실제 subprocess 시간 제한에 의존하는 부하 민감 테스트로 판단(추정). B/C/K 변경과 무관한 파일이며 수정하지 않았다.
- Ponytail(v1.1 변경 파일만): High 0 / Medium 0 / Low 1(`test_run_guarded_bck_cli.py` `_argv()`의 쓰이지 않는 `approval` 인자) — accepted/no-change.
