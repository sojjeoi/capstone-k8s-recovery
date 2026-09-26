# Phase 8 — §160 계측 준비: 오프라인 구현·테스트 완료 (§161)

§140~§160 전부 보존(수정 0건). §152의 0/40, 후속 5쌍 결과, §154·§156·
§158의 NO-GO 판정 범위 전부 그대로 보존한다(§1에서 계산 기준만 다시
명시할 뿐, 수치는 안 바꾼다). 이번 턴은 §160이 요구한 **계측 준비를
오프라인으로 구현·테스트**한 결과 보고다 - 클러스터 접속 0건, live
smoke 0건, 새 pilot 0건, 모델·임계값 변경 0건. 아래 코드 변경은 전부
**기존 탐지·정책·SLO 판정·승격 동작을 바꾸지 않는, 관찰 전용 계측
추가**이고, 그걸 전체 회귀 테스트로 직접 확인했다(§2).

---

## 0. 중요한 정정 - score_server.py는 이미 이 계측을 갖고 있었다

§153 §6.1·§154 §6·§156에서 "`score_server.py`는 각 평가 tick의 원점수를
파일로 남기지 않는다"고 반복해서 썼는데, **이번에 코드를 다시 읽어보니
부정확했다.** `score_server.py`의 `main()`은 **§92(2026-09-21)부터 이미**
`evidence_log_path`가 주어지면 매 tick마다 `evaluation_decision` JSONL
레코드(`window_start_utc`/`window_end_utc`/`raw_feature_vector`[8개 원시
특성 전부, queue 포함]/`score`/`is_anomalous`/`consecutive_anomalous`
등)를 write-ahead로 기록한다(`score_server.py` 379~397행, 550~576행).
**다만 이 인자는 opt-in이고, `run_all_scenarios.py`(core 45건 오케스트
레이터)도 `run_load_ramp_followup_trial.py`(후속 5쌍)도 이걸 전달한
적이 없다** - 둘 다 코드로 직접 확인했다(`grep`으로 두 파일 어디에도
`--evidence-log`/`evidence_log_path` 없음). 그래서 **§153~§156이
분석해 온 core 45건·aux 5건·후속 5쌍에는 실제로 이 tick별 로그가
존재하지 않는다** - 이 부분(기존 40개 trial에 tick 로그가 없다는
결론)은 그대로 맞다. 다만 "capability 자체가 없다"는 표현은 부정확
했으므로 이 자리에서 정정한다: **capability는 있었지만 켠 적이
없었다.**

이 발견 덕분에 §160이 계획한 코드 변경 범위가 줄었다: `score_server.py`
(proposed arm)는 **수정이 필요 없고**, `fixed_threshold.py`만 이
capability가 없었다(§1).

---

## 1. 구현한 것 (오프라인, 모든 변경 테스트 완료)

| 파일 | 변경 | 판정 로직 영향 |
|---|---|---|
| `anomaly-detection/fixed_threshold.py` | `evaluate_verbose()` 신규(WINDOW_SEC 구간 8개 원시 feature 전체+window 시각 반환, `score_server.py`의 `evaluate_v32b()`/`_evaluate_v32b_verbose()` 쌍과 동일 패턴). `evaluate()`는 그 `cpu_mean`만 뽑는 얇은 래퍼로 변경(반환값 100% 동일). `main()`에 `evidence_log_path`(선택, 기본 None) 추가 - 지정 시 매 tick `evaluation_decision` JSONL 기록(`score_server._write_evidence_line()` 재사용, 새 포맷 발명 안 함). `--evidence-log` CLI 인자 추가. | **없음** - 신호 발행 조건(연속 3회+cooldown)은 값 그대로 재현했고, 기록 실패도 신호를 막지 않는다(§92의 fail-closed 게이트는 이식 안 함 - 관찰 전용 계측이 baseline arm에 새 실패 경로를 만들면 안 되므로) |
| `experiments/arm_controller.py` | `_build_detector_command()`가 `evidence_log_path` 지정 시 `fixed_threshold` arm에도 `--evidence-log`를 붙이도록 확장(기존엔 `proposed`에만 붙임). `--stop-file`은 여전히 `proposed` 전용(fixed_threshold.py에 그 옵션 자체가 없음). | 없음(옵션 미지정 시 커맨드 100% 동일, 기존 회귀 테스트로 확인) |
| `anomaly-detection/test_fixed_threshold.py` | 기존 1개 테스트의 patch 대상을 `evaluate`→`evaluate_verbose`로 조정(리팩터링 반영), 신규 5개 테스트 추가(§2) | - |
| `experiments/test_arm_controller.py` | 기존 1개 테스트(`..._ignored_for_fixed_threshold`)를 새 동작에 맞게 뒤집어 이름 변경(`..._appended_for_fixed_threshold_too`) | - |

**요청별 원시 지연·성공·시각(probe/ramp 원자료)**: 새 코드 불필요 -
기존 `probe.py`/`probe_followup.py`가 이미 요청마다 `sent_at`/`latency`/
`success`를 남기고, 이 pilot도 §160 §6.5의 분리된 디렉터리에서 같은
메커니즘을 그대로 재사용한다.

---

## 2. 회귀 테스트 - 신규 5개 + 전체 스위트 재확인

**신규 테스트**(`test_fixed_threshold.py`, 전부 통과):
- `test_evidence_log_records_every_cycle_no_drops` - 5 cycle 실행 시
  `evaluation_decision` 정확히 5건, `evaluation_seq` 1~5 연속, `correlation_id`
  전부 고유(로깅 누락 회귀 방지).
- `test_evidence_log_window_matches_evaluate_verbose_exactly` +
  `test_evaluate_verbose_window_length_matches_window_sec` - 기록된
  `window_start_utc`/`window_end_utc`가 `evaluate_verbose()` 반환값과
  글자 그대로 일치, 그리고 실제 계산 결과가 `WINDOW_SEC`(60초)와
  정확히 같음(타임스탬프 재계산·off-by-one 회귀 방지).
- `test_evidence_log_schema_is_fixed_and_has_no_env_or_secret_leakage` -
  가짜 환경변수 비밀값이 로그 파일 텍스트 어디에도 없음 + 기록된 키가
  고정된 15개 필드 집합과 정확히 일치(요청 본문·자격증명 필드 자체가
  없음, `os.environ` 등 통째 덤프 회귀 방지).
- `test_evidence_logging_does_not_change_signal_decision` - `evidence_
  log_path` 유무와 무관하게 신호 발행 횟수·인자가 완전히 동일함을
  직접 비교로 확인(가장 중요한 회귀 방지 - "기존 탐지·정책·승격 동작
  변경 금지" 요구사항의 핵심 증거).
- `test_evidence_log_path_appended_for_fixed_threshold_too`(`test_arm_
  controller.py`) - 오케스트레이터 배선 확인.

**전체 스위트 재확인**(이번에 실제로 실행, 결과 그대로 기록):
- `anomaly-detection/` (pytest, `test_score_server_data_gap_subprocess.py`
  포함): **242 passed, 0 failed**.
- `experiments/` (pytest 전체): **790 passed, 3 skipped(무관), 0 failed**.

---

## 3. 개별 요청 피해 기준 - 여전히 미결정(pilot 결과로 정하지 않음)

§160 §3.1에서 이미 코드로 확인한 사실을 재확인만 했다(새로 계산
없음): `slo-definition.md` 63행·68행은 `LATENCY_THRESHOLD`(0.648초)를
**집계 P95(직전 60초 슬라이딩 윈도우) 기준**으로만 정의한다. 같은
문서 77행의 유일한 개별 요청 규칙은 "timeout=실패"뿐이다. **독립적으로
정해진 개별 요청 지연 기준은 찾지 못했다** - 이번에도 새로 만들지
않고 **미결정**으로 남긴다. 정상 자료 기반 기준 제안(§160 §3.3의
`p_normal` 절차)은 이번 턴에도 실행하지 않았다(제안하려면 pilot
데이터와 분리된 별도 분석이 필요하고, 그 결과는 라이브 측정 전에
별도 승인을 받아야 한다 - §160 §9 그대로 유지).

---

## 4. `queue` 지표 - 기존 자료로 확인한 것과, 그래도 남는 불확실성

**기존 자료로 확인됨(새 쿼리 없음)**: `anomaly-detection/v3/data/
v3-inventory.json` 240~241행 - **`queue_mean_nonzero_valid_rows: 0`,
`queue_mean_nonzero_fraction: 0.0`**(유효 58행 전체 기준, 학습 부분집합
뿐 아니라 인벤토리 전체). `model_v32/artifacts/feature-schema.json`의
학습 통계와 정합적으로 일치한다 - **지금까지 수집된 어떤 세션에서도
`queue_mean`이 0이 아니었던 적이 없다.**

**그래도 smoke test가 필요한 이유(코드로 확인한 구조적 모호함)**:
`features.py`의 `_query_range()`는 Prometheus가 **빈 결과(시계열
자체가 없음)**를 반환하면 `[]`를 돌려주고, `_mean_slope([])`는
`(0.0, 0.0)`을 반환한다 - 즉 **"쿼리는 성공했고 실제 값이 0이었다"**
와 **"쿼리 자체가 아무 시계열도 못 받았다"**가 최종 feature 값에서
**구분 불가능하게 합쳐진다.** 기존 자료(전부 이미 이 합쳐진 값만
저장)만으로는 이 둘을 가를 수 없다 - **§160이 이미 정확히 지적한
대로, 이건 smoke test에서 원시 Prometheus 응답(시계열 개수)까지
직접 봐야 풀리는 문제**로 남긴다(이번 턴엔 클러스터 접속 없이는
불가능 - 미실행).

---

## 5. `0/40`·`12/19`의 원래 계산 기준 재확인 (재분류 아님)

지시대로, 이 두 수치를 "검증된 개별 사용자 피해"로 단정하지 않고
원래 계산 기준을 다시 명시한다 - **수치 자체는 그대로 보존**한다:

- **§152의 `0/40`**: 40개 non-native trial 전부에서 `t_switch`가
  `t_first_bad`보다 앞선 사례가 0건이라는 뜻이다. `t_first_bad`는
  "개별 요청이 `success=False`이거나 `latency > 0.648s`인 첫 번째
  요청"으로 계산됐다(정렬+첫 매치). **`0.648s`는 §3에서 다시 확인한
  대로 집계 P95용으로 정의된 값을 개별 요청 판정에 재사용한 것**이다
  - 이 재사용의 타당성이 별도로 검증된 적은 없다. 그래서 `0/40`은
  "이 특정 대리 기준으로 계산했을 때 선제 전환 성공이 0건"이라는
  뜻이지, "독립적으로 검증된 개별 사용자 피해 기준으로 0건"이라는
  뜻이 아니다.
- **§156의 `12/19`**: 유효 `load_ramp` trial 19개 중 12개가 이 **같은
  `t_first_bad` 정의**로 주입 후 2초 안에 "첫 피해"가 나왔다는
  뜻이다 - 마찬가지로 개별 기준의 타당성 문제를 그대로 물려받는다.
- **재분류하지 않음**: 이 두 수치를 다시 계산하거나, 기존 trial을
  §3의 "미결정" 기준으로 다시 나누지 않았다 - 이번 문서는 **원래
  계산 기준을 정확히 설명**하는 것으로 끝낸다.

---

## 6. 예상 라이브 소요시간 (구현이 끝났으므로 §160보다 더 좁혀진 추정)

| 세션 | 내용 | §160의 추정 | 이번 갱신 |
|---|---|---|---|
| 세션 0(로깅 코드 추가) | ~~코드 작성+오프라인 테스트~~ | 추정, 미실측 | **완료**(이 턴에서 실행 - 클러스터 불필요, 오프라인) |
| 세션 1(smoke) | 로깅 동작 확인 + `queue` 원시 응답 확인 | 30~45분(추정) | **여전히 추정 30~45분** - 코드는 이제 테스트로 검증됐지만, 실제 클러스터의 Prometheus·vLLM과 맞물렸을 때의 동작은 미검증이라 추정 폭을 줄이지 않는다. `queue`의 원시 Prometheus 응답 확인(§4)이 새로 추가된 확인 항목. |
| 세션 2~4(본 측정) | §160 §6.2 그대로 | 세션당 60~90분(추정) | 변경 없음(미실행) |

세션 0이 끝났다는 것 외에 §160의 승인 체크포인트 순서·조건은 전혀
안 바뀐다 - 다음은 **세션 1(smoke test) 실행 승인**이다.

---

## 7. 하지 않은 것 (지시 그대로)

클러스터 접속 0건, live smoke 0건, 새 pilot 0건, 모델·임계값·연속
판정 규칙 변경 0건, 기존 trial 재분류 0건, §152~§159 파일 수정 0건.
개별 요청 피해 기준은 pilot 결과나 이번 구현 결과를 근거로 정하지
않았다 - 여전히 미결정.

---

*작성: 2026-09-26. 오프라인 코드 구현·테스트만 수행(클러스터 접속
0건). §140~§160 전부 보존. 전체 회귀 테스트 실행 결과: anomaly-
detection 242 passed, experiments 790 passed/3 skipped, 0 failed.*
