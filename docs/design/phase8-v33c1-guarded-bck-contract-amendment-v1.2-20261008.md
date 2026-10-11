# B/C/K 계약 개정 v1.2 (guarded-bck-v1.2) — 실행 provenance 자동 검증·기록

작성: 2026-10-08(KST). **상태: FROZEN v1.2 (사용자 승인 P2) — 실행 승인 아님.** 동결 해시는 `phase8-v33c1-guarded-bck-freeze-manifest-v1.2-20261008.json`(이 문서는 manifest 해시를 담지 않는다 - manifest가 이 문서 해시를 담으므로 순환 방지).

## 1. 관계 (provenance chain)

- **v1** (superseded, 보존): 기본 계약 `phase8-v33c1-guarded-bck-comparison-contract-draft-20261007.md`(sha256 `c363e9e66c090adef1874a15f38b41e1adc6c9a2e3d9b583497030770259841e`), manifest `phase8-v33c1-guarded-bck-freeze-manifest-20261007.json`(`77164a0e157a8cf4ed5c63faf4e49cdcd4f6a09c00c8c245f7ccc9c7a1dba365`). 사유: session wrapper → runner CLI 비호환.
- **v1.1** (superseded, 보존): 개정 `phase8-v33c1-guarded-bck-contract-amendment-v1.1-20261007.md`(`f3e1c86f02bc89a1d8d6e5cd5e482557753040ec5932feb6cd74f44b6b638056`), manifest `phase8-v33c1-guarded-bck-freeze-manifest-v1.1-20261007.json`(`ab38e273ccfa8b1623d3616f3e971bf5bf51e9fa7dd7ba95aa18eaa7b9d4f0b4`), v1.1 runner 원본 바이트 사본 `bck-v1.1-superseded/run_guarded_bck.py.v1.1`(`74e384f90844bf1603ed9baa3cac3426c483afc71829671b88395ca14b24470a`). 사유: 아래 §2.
- **v1.2** (현재): 이 문서. v1 기본 계약 §1–§15와 v1.1 개정 §3(`--mode act`)은 그대로 적용된다.

## 2. 발견 경위 (결과 전)

- v1.1 freeze 이후 bc90 실행 직전 provenance review(사용자 요청, 2026-10-07)에서 발견: **실제 run evidence(run contract·summary·preflight·탐지기 기록·session.json)만으로는 v1/v1.1을 구분할 수 없었다.** run contract `contract_doc`는 v1 계약 경로(해시 없음)뿐이고, runner·manifest·개정 문서·session wrapper 해시는 어디에도 자동 기록되지 않았으며, 기록되는 `rule_spec_sha256`는 v1·v1.1이 같았다.
- 상태: **bc90 ID 미사용, 결과 폴더 없음, cluster 미접속, CPU pressure 없음, promotion 없음, outcome/SLO 미확인.** 결과 기반 tuning 아님.
- 수정 목적: **실행 provenance 자동 검증·기록뿐.**

## 3. 변경

`experiments/run_guarded_bck.py`(v1.2 sha256 `1dd6b5fb250f5cd137fb8ea4def5f4142d09714c98ae603668ff688b6d4f5cfb`):
1. `verify_provenance()`: v1.2 manifest(`FREEZE_MANIFEST` 경로 상수 - manifest 해시는 코드에 넣지 않음)를 읽어 `status=frozen`·`version=v1.2`·`files` 사전을 확인하고, **manifest에 적힌 모든 파일**을 다시 해시해 비교한다. 필수 항목(`PROVENANCE_REQUIRED`: base contract, v1.1 개정, v1.2 개정, runner 자신, 탐지기, 규칙 명세) 누락·해시 불일치·파일 없음·manifest 없음·형식 오류·manifest 자기 참조 → `SystemExit`(fail-closed).
2. `main()`: argparse 직후, 토큰 확인·클러스터 연결 조립(`real_deps`)·`run_bck`(결과 폴더 생성) **전에** `verify_provenance()` 호출. 결과를 `run_bck(..., code_provenance=...)`로 넘겨 **run contract와 final summary에 `code_provenance`로 자동 기록**: `freeze_version`, `freeze_manifest_path`, `freeze_manifest_sha256`(실행 시 계산), 필수 6항목별 `*_path`·`*_sha256`(실제 계산값), `verified_files`(manifest 전 항목의 실제 해시), `verification_status=verified`, `verified_at_utc`.
3. 함수 직접 호출(테스트)에서 `code_provenance`를 넘기지 않으면 기존 기록 형식 그대로(동결 v1 characterization 테스트 보존). 라이브 실행은 세션 실행기 → CLI(`main`)만 쓴다.

새 테스트 `experiments/test_run_guarded_bck_provenance.py`(sha256 `7d32bfd6efb8b7bcac5c51adce35d1caca52aa1698ff36925f44ad93feaa1eeb`): 정상 일치·정확한 기록값·manifest 해시 / 필수 6항목 각각 불일치·각각 누락 / 추가 항목 불일치 / manifest 없음·형식 오류 6종·자기 참조 / CLI subprocess(ps1 형식)에서 불일치 시 결과 폴더 생성 전 종료(토큰 유무 둘 다) / CLI dry-run 계약에 `code_provenance` 기록 / 동결 뒤 실제 v1.2 manifest로 현재 트리 검증. v1·v1.1 테스트 파일은 수정하지 않았다(v1.1 CLI 테스트는 실제 manifest 검증을 거치므로 v1.2 manifest 동결 뒤 통과해야 한다).

결과 폴더만 보고: run contract의 `contract_doc`(v1 기본 계약) + `code_provenance.base_contract_sha256`·`v1_1_amendment_sha256`·`v1_2_amendment_sha256`·`runner_sha256`·`freeze_manifest_sha256`로 "v1 기본 계약을 기반으로 v1.2 동결 구현이 실행됐다"를 재구성할 수 있다.

## 4. session wrapper provenance (운영 절차 - wrapper 수정 없음)

`experiments/guarded_pilot_session.ps1`은 A/B 공용 실행 환경이므로 B/C/K freeze 대상에 넣지 않고 **execution-environment provenance**로 run마다 기록한다(판단 알고리즘 freeze와 별개).

각 live run 직전(읽기 전용):
1. wrapper 경로와 SHA-256을 **그 시점에 새로 계산**한다(이전 값을 가정하지 않음). 참고 기준값: 2026-10-08 오프라인 계산 `68c9349ab27216a3ad5b5b8f63329919bdc3b7994162e3b72343d238b9ba4efe`. 다르면 실행하지 않고 보고한다.
2. `experiments/results/capstone_guarded_bck_v1/execution-provenance-<run>-<UTC시각>.json`을 새 파일("x")로 쓴다: `run`, `run_id`, `wrapper_path`, `wrapper_sha256`, `computed_at_utc`, `runner_path`·`runner_sha256`, `freeze_manifest_path`·`freeze_manifest_sha256`(교차 확인용).
3. 그 run의 act 승인 기록(`runs: ["<run>"]`)에 `execution_provenance_file`·`execution_provenance_sha256`을 넣는다. 기존 runner가 승인 기록 전체를 run contract `act_approval`에 저장하므로 run contract에서 이 파일로 연결된다.
4. 세션 실행: `guarded_pilot_session.ps1 -Run <run> -Mode act -ActApproval <승인> -RunnerScript experiments\run_guarded_bck.py -SessionRoot experiments\results\capstone_guarded_bck_v1` (세션 폴더·session.json·session-end.json이 결과 폴더 안에 남는다).

## 5. 바뀌지 않은 것

탐지기 판단 로직(공통 tick·세 규칙·3연속·쿨다운 60), 임계값 B `-0.07667538512242333` · C `1.6842544293089357` · K `2.7`, 규칙 명세, signal identity·context, arm, CPU pressure 조건, 관찰 600초, 서비스 확인, cleanup(D5·D6·canonical), SLO·지표·통계, 유효성 규칙(분석기 해시 동일), Latin-square 순서, 반복 계획(6/9·12/18), 승인 단위, v1.1 `--mode act` CLI, 세션 실행기.

## 6. 검증 (동결 전 오프라인)

- 신규 provenance 테스트 26 통과(+ 실제 v1.2 manifest 검증 1건은 동결 뒤 실행). runner v1 테스트 23 통과(characterization 포함).
- 동결 뒤 전체 B/C/K·v1.1 CLI·실제 manifest 검증·experiments 전체 회귀를 다시 돌려 별도 검증 기록에 남긴다(manifest·이 문서에 넣으면 순환).
- 알려진 실패(수정하지 않음): 기존 frozen A/B 테스트 1건(`test_guarded_ab_analysis.py::test_real_folder_dry_run_excludes_rehearsals_and_has_no_official_pairs`)은 ab01 이전의 공식 pair 0개 가정을 갖고 있어 현재 실제 결과 폴더에서는 실패한다. B/C/K 변경으로 인한 회귀가 아니며 frozen 파일이므로 수정하지 않았다. `test_ssh_check_evidence.py::test_timeout_with_partial_output_fails_and_preserves_partial`은 v1.1 검증 때 전체 suite에서 1회 실패·단독 3/3 통과 — timing-sensitive/flaky 가능성이 있으나 원인 미확정.
- Ponytail(v1.2 변경 파일): High 0 / Medium 0 / Low 1(`verify_provenance` 반환부 이중 루프 컴프리헨션 가독성) — accepted/no-change.
