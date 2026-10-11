# N1 계약 amendment v1.1 (freeze 후보, 2026-10-08)

> 기반: `phase8-v33c1-guarded-normal-bc-n1-contract-20261008.md`(N1 v1, frozen manifest `ff6950ab…`). v1 manifest·nb90 원자료는 수정하지 않는다.
> v1 파일 사본: `docs/design/n1-v1-superseded/*.v1`(해시는 v1 manifest와 같음).
> 사용자 지시(2026-10-08): nb90 pilot은 기능 성공으로 보존. 공식 nb01 전 경계 문제 수정 - 최소 범위 v1.1. 공식 실행 승인 아님.

## 1. 바뀐 것 (이 네 가지뿐)

### A. 신호 귀속 인프라 증거 창을 t0에서 자름 (M3)

- v1: `[t − 60 s, t + 600 s]`. v1.1: **`[max(t0, t − 60 s), t + 600 s]`**.
- 재현된 v1 비대칭(분석기 v1로 확인): Warning t0−20 s, B 신호 t0+30 s, C 신호 t0+80 s → B만 `indeterminate`(창 시작 t0−30), C는 `unnecessary`(창 시작 t0+20). B가 보통 C보다 먼저 발화하므로 연구 시작 전(preview 기동) 증거가 primary T_safe에 arm마다 다르게 작용할 수 있었다.
- t0 전 Warning·input skip은 원자료에 그대로 있고 분석 출력 `infra_events`에 `pre_t0_diagnostic_only: true`로 남는다. t0 뒤 신호를 `indeterminate`로 만드는 근거로만 쓰지 않는다.
- t0 뒤 Warning·input skip은 v1과 같이 분류에 반영한다.
- t0의 시작 조건(preview Ready, active/preview 식별, health, 노드, 입력 정상 - 첫 scored 평가)은 그대로.

**이 수정이 warm-up·grace가 아닌 이유:** t0는 그대로이고, t0 직후 평가·신호를 하나도 버리지 않는다(시작 직후 신호도 primary에 포함 - 테스트 7). 판정 창·T_safe·횟수 정의도 같다. 바뀐 것은 "연구 시작 전에 생긴 증거가 연구 안 신호의 귀속을 바꾸는 경계"뿐이다. nb90 결과(B 신호 1개, 600 s pilot이라 추적 미완으로 indeterminate)는 이 수정과 무관하며 결과를 보고 어느 arm을 유리하게 하려는 변경이 아니다 - 시간 창 경계 정확성 수정이다.

### B. 실행 provenance 기록 수정 (M1, 운영 보조 코드만)

- nb90 `invocation.command`가 손으로 쓴 Windows 경로의 Python 이스케이프(`\r` 등)로 깨졌다(원본·ERRATUM 보존).
- 이전: 실행 때 붙여 쓴 인라인 코드 — 보존 사본 `scratchpad/n1_exec_provenance_BEFORE_inline_nb90.py.txt`(sha256 `330016d7…`).
- 이후: `scratchpad/n1_exec_provenance.py` — 인자 목록을 Path 객체로 한 번만 만들고, 같은 목록으로 `launch-args-<run>.json`(PowerShell `Start-Process -ArgumentList`가 그대로 읽음)·`invocation.argument_list`·`invocation.command`(`subprocess.list2cmdline`)를 만든다. 실행 뒤 `verify`가 launch-args·session-launcher JSON·provenance 인자 목록 일치와 command 재생성 일치를 기록(`invocation-consistency-<run>.json`).
- 실행 인자·실험 의미 변경 없음. frozen 탐지기·실행기 로직 변경 없음.

### C. 알려진 한계 문서화 (M2, 코드 판정 변경 없음)

- `summary.evidence_counts.condition_met`(동결 `evidence_counts`)는 최상위 `condition_met`만 센다. N1 섀도 기록은 판정을 `rules.B/C/K`에 두므로 이 값은 항상 0이고 **실제 B·C·K 조건 수가 아니다**. 이 필드가 0이라고 "신호 조건이 없었다"고 해석하지 않는다.
- 공식 판정·보고는 분석기가 `rules`에서 직접 센 값과 원자료 기준. 분석 출력 `known_limitations`에 같은 문장을 넣었다.

### D. 테스트·manifest·재-pilot ID

- 새 테스트 `experiments/test_guarded_normal_bc_analysis_v1_1.py`(반례 7개 + 진단 보존 + 재-pilot ID).
- 기존 테스트 변경: 분석기 헬퍼가 `t0`를 넘기는 한 줄, 실행기 ID 목록에 nb91 추가 - 기존 단언은 그대로 통과.
- 실행기 변경: freeze 버전 `n1-v1.1`·manifest 경로, 필수 provenance에 이 amendment 추가, **재-pilot ID `nb91`**(counted=false, rep 2091, 관찰 600 s). v1 계약의 pilot ID는 nb90 하나뿐이고 재사용 금지라 새 ID가 필요하다. 공식 ID(nb01–nb09)·순서·관찰은 그대로.
- freeze 후보 manifest `phase8-v33c1-guarded-normal-bc-n1-freeze-manifest-v1.1-20261008.json`(status candidate).

## 2. 바뀌지 않은 것

임계값(B −0.07667538512242333, C 1.6842544293089357, K 2.7), 모델·특징, 3연속·쿨다운, grace 없음, t0 정의, 판정 창 3600 s·추적 600 s(관찰 4200 s), W = 600 s·lookback 60 s, 분류 순서와 그 밖의 의미, T_safe·D 정의, 세션 상태 규칙, 통계, 유효 6·최대 9, 정지 규칙, record_only·송신 없음, B/C/K v1.2 연구 전부.
