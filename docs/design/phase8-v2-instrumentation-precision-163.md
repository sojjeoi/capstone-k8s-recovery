# Phase 8 — 계측 정밀화: 평가 시각 vs 원본 표본 시각 구분 (§163)

**작업 시간 추정(작성 당시 사전 고지): 약 2~2.5시간.** 실제 소요:
디버깅(시그니처 버그 1건 + 테스트 자체 버그 1건) 포함 추정 범위 내.

§140~§162 전부 보존(공식 결과 0건 수정). 이번 턴도 **완전히
오프라인**이다 - 클러스터 접속 0건, 본 pilot·장애 주입·promotion
0건, 모델·SLO·임계값·판정 로직 변경 0건. §162를 다음과 같이 최종
기록한다: **"기술 로그 기록 PASS, 전조 이용 가능 시각 계측은
미완료."**

이번 턴의 목적은 §162 §3이 확인한 계측 공백(평가 tick 시각과
Prometheus 원본 표본 시각을 구분할 수 없음) 중 **원리상 복원
가능한 부분만** 최소하게 메우는 것이다 - 복원 불가능한 부분은
"확인 불가"로 명시하고 넘어간다.

---

## 1. 무엇을 바꿨는가 (최소 계측, 판정 로직 무변경)

### 1.1 `features.py` — 원본 표본 시각 보존 + 신선도 계산

- `_query_range_with_timestamps()`([features.py:38](../../anomaly-detection/features.py#L38), 신규) - 기존
  `_query_range()`와 완전히 같은 쿼리·다중 시계열 합산을 하되,
  Prometheus가 실제로 돌려준 `(timestamp, value)` 쌍을 **시각까지
  보존**해 정렬된 리스트로 반환한다.
- `_query_range()`([features.py:61](../../anomaly-detection/features.py#L61))는 위 함수의 값만 뽑는
  얇은 래퍼로 재정의 - 반환값·순서 100% 동일(회귀 테스트로 확인,
  §2).
- `PROM_SCRAPE_INTERVAL_SEC = 15`([features.py:35](../../anomaly-detection/features.py#L35)) - `gitops/apps/
  vllm-serving/servicemonitor.yaml`의 `interval: 15s`를 인용한
  상수(코드로 재조회 불가한 K8s 리소스라 상수화, 출처를 주석에
  명시). "원본 표본이 언제 참이 됐는가"의 **하한**을 계산하는
  데만 쓴다 - 실시간 판정 로직에는 안 쓰임.
  **정정(작성 후 Codex 검토로 확인, §7 참고)**: 이 상수는
  `queue`/`cache` 두 지표에만 근거가 있다 - 이 ServiceMonitor는
  vLLM 앱 자신의 `/metrics`만 스크랩하고, `queue`/`cache`가 거기서
  나온다([servicemonitor.yaml:10-15](../../gitops/apps/vllm-serving/servicemonitor.yaml#L10)의
  `selector`/`endpoints`가 vLLM Service만 대상으로 함). 반면
  `cpu`/`memory`는 cAdvisor 출처(`container_cpu_usage_seconds_total`/
  `container_memory_working_set_bytes`)라 kubelet의 별도 스크레이프
  경로를 타는데, 이 저장소 `gitops/` 전체에 그 스크레이프 주기를
  정의하는 설정이 없다(직접 확인 - `interval:`로 전수 검색해도
  이 ServiceMonitor 한 건뿐). 즉 `cpu`/`memory`에 대해서는 15초라는
  숫자 자체가 근거 없음 - 아래 `_raw_sample_staleness()`가 이 상수를
  네 지표 모두에 동일 적용하는 것은 두 지표(`cpu`/`memory`)에
  대해서는 **미확인 값으로 하한을 계산하는 것**이다.
- `_raw_sample_staleness()`([features.py:65](../../anomaly-detection/features.py#L65)) - 핵심 원칙:
  **단일 시각을 단정하지 않는다.**
  **정정(Codex 읽기 전용 검토 2회차로 확인, 2026-09-26)**: 최초
  작성 시 이 항목은 "Prometheus 표본의 timestamp는 언제
  스크레이프됐는가이다"라고 무조건 서술했는데, 이는 부정확하다.
  `query_range`가 돌려주는 timestamp는 **PromQL 평가 격자 시각**
  이다 - Prometheus는 그 격자 시각 기준으로 lookback 구간(기본값
  5분, 이 저장소에서 재정의된 흔적 없음 - `gitops/` 전수 검색
  결과) 안의 가장 최근 표본 값을 찾아 그 **격자 시각**에 실어
  돌려준다(`memory`/`queue`/`cache`처럼 그냥 지표 이름만 쓰는
  instant selector가 이 방식이다 - `cpu`는 `rate(...[30s])`라는
  별도의 구간 계산값이라 여기 적용 안 됨, §7 참고). 그래서 실제
  스크레이프가 몇 분 전에 멈췄어도(값이 안 바뀌어도) lookback
  구간 안에서는 매 평가 시각마다 "방금 갱신된 것처럼" 보이는
  값이 반환될 수 있다 - 이건 스크레이프가 실제로 멈췄다는 뜻이
  아니라 원래 설계된 동작이다.
  **정정(Codex 읽기 전용 검토 3회차로 확인, 2026-09-26)**: 이
  "방금 갱신된 것처럼 보이는" 상태가 끝나는 조건은 두 가지이지
  하나가 아니다 - (1) Prometheus가 해당 시계열에 명시적 **stale
  marker**(대상이 사라졌다고 판단했을 때 쓰는 특수 NaN 표본)를
  기록해 그 시계열을 제외 처리하는 경우, **또는** (2) 그런 명시적
  marker 없이도 평가 시각이 마지막 실제 표본으로부터 lookback
  구간(기본값 5분)을 그냥 넘어가 버려 아무 값도 안 나오는 경우 -
  즉 lookback은 marker 유무와 무관하게 자체 만료 조건을 갖는다.
  "lookback 안의 오래된 값"·"lookback 만료로 반환 자체가 없음"·
  "stale marker로 제외됨"은 서로 다른 세 상태다. 따라서 아래
  상한/하한은 **물리적으로 보장된 경계가 아니라, 이 평가 격자
  시각에서 기계적으로 유도한, 검증되지 않은 참고값**이다:
  - 상한(`underlying_value_true_upper_bound_utc`) = 관측된 마지막
    평가 격자 시각(실제 스크레이프 시각과 다를 수 있음 - 위 참고)
  - 하한(`underlying_value_true_lower_bound_utc`) = 그보다
    `PROM_SCRAPE_INTERVAL_SEC`(15초, `queue`/`cache`에만 근거 -
    위 정정 참고) 앞 - `cpu`/`memory`에는 이 15초 자체가 미확인
    숫자이고, `queue`/`cache`에서도 lookback 때문에 유효성이
    보장되지 않는다.
  - 표본이 아예 없으면(빈 시계열) `raw_sample_status: "no_samples"`
    + 나머지 전부 `None` - 조용히 0이나 "지금"으로 채우지 않는다.
- `extract_features_with_provenance()`([features.py:105](../../anomaly-detection/features.py#L105)) - 지표별
  `_raw_sample_staleness` + 쿼리 요청/응답 시각(`query_sent_at_utc`/
  `query_received_at_utc`)을 묶어 반환. `extract_features()`
  ([features.py:136](../../anomaly-detection/features.py#L136))는 `["features"]`만 뽑는 얇은 래퍼로
  재정의 - 기존 호출부(다수) 전부 무변경.

### 1.2 `fixed_threshold.py` — evidence 로그에 4개 시각 필드 추가

`evaluate_verbose()`가 `extract_features()` 대신
`extract_features_with_provenance()`를 호출(쿼리는 **여전히 1회만**
- 중복 조회 없음). evidence 레코드에 `query_sent_at_utc`,
`query_received_at_utc`, `feature_computed_at_utc`,
`per_metric_provenance` 4개 필드 추가. 기존 판정 로직(`cpu_mean`
비교, 연속 3회 카운트)은 전혀 안 건드림.

### 1.3 `score_server.py` — 추가 HTTP 요청 없이 원본 표본 캡처

가장 조심스럽게 다룬 파일 - `_evaluate_v32b_verbose()`는 40개+
기존 테스트가 `query_range_fn`/`freshness_check_fn`을 직접
주입하는 계약에 의존한다. **조건부 캡처** 패턴으로 설계:

```
query_range_fn이 기본값(_query_range)일 때만
  → _capturing_query_range_fn()으로 감싸 같은 호출의 부산물로
    (timestamp, value) 쌍을 캡처(추가 요청 0건)
그 외(커스텀 query_range_fn 주입 - 기존 테스트 다수)
  → 캡처 전혀 안 함, per_metric_provenance는 빈 dict
```

**폐기한 첫 설계와 그 이유(기록)**: 최초 시도는 evidence 로깅
시점에 별도로 `extract_window_strict_with_provenance()`를
재조회하는 방식이었다. 이게 지표당 Prometheus 요청 횟수를
2배로 늘려 `test_score_server_data_gap_subprocess.py`의
가짜 서버가 쓰는 "요청 횟수로 cycle 판단" 로직을 깨뜨리는 것을
실측으로 확인했다(cycle 2가 "gap"이어야 하는데 desync로 감지
안 됨). 이건 "최소 계측"이라는 지시를 실제로 위반하는 설계였으므로
폐기하고, 기존 단일 쿼리 호출의 부산물로 캡처하는 현재 설계로
교체했다. 이 과정에서 쓸모없어진 `build_dataset.
extract_window_strict_with_provenance()`(및 그 테스트)는 완전히
제거했다 - `extract_window_strict()` 자체는 이번 세션 동안 단 한
줄도 안 바뀜(직접 diff로 확인).

---

## 2. 테스트 결과 (전부 실행 확인, 날조 없음)

| 파일 | 테스트 수 | 결과 |
|---|---|---|
| `test_features.py`(신규) | 9 | 전부 통과 |
| `test_fixed_threshold.py` | 18(§163 신규 2) | 전부 통과 |
| `test_score_server_v32b.py` | 46(§163 신규 4) | 전부 통과 |
| **anomaly-detection/ 전체** | **257** | **전부 통과**(173.26초) |

`test_score_server_data_gap_subprocess.py`(이번 재설계의 직접적
계기가 된 그 결정론적 subprocess 테스트) 포함 전체 스위트 재실행
- **0 실패**. 최소 계측 설계가 실제로 추가 HTTP 요청을 만들지
않는다는 것은 `test_default_query_range_fn_captures_provenance_
with_no_extra_http_calls`가 지표당 정확히 1회(4회/4지표)임을
직접 카운트해 확인한다.

작업 중 발견·수정한 버그 2건(둘 다 최종 커밋에는 반영됨, 진행
경위를 정직하게 남김):

1. **프로덕션 코드 버그**: `_capturing_query_range_fn(promql, s, e)`가
   `step` 인자를 안 받았는데, `query_range_with_bounded_retry()`
   (`v3/prom_health.py`)가 4개 위치 인자(`promql, start, end, step`)로
   호출해 `TypeError`. 수정: `step="15s"` 파라미터 추가 후
   `_query_range_with_timestamps()`로 그대로 전달.
2. **테스트 자체의 버그**: 새 테스트가
   `patch.object(ss, "check_metric_freshness", ...)`로 모듈
   속성을 바꿨지만, `_evaluate_v32b_verbose()`의
   `freshness_check_fn` 기본값은 **함수 정의 시점에 이미
   바인딩**돼 있어 모듈 속성 패치가 효과가 없었다(Python 기본
   인자의 표준 함정). 기존 테스트 전부가 쓰는 패턴대로
   `freshness_check_fn=_always_fresh`를 명시적으로 넘기도록 수정.

### 확인한 항목별 (지시 (a)~(d))

- **(a) 로깅 on/off일 때 feature 값·신호·판정 100% 동일**:
  `_query_range`/`extract_features`가 새 함수의 얇은 래퍼임을
  직접 비교하는 회귀 테스트로 확인(`test_features.py`). 커스텀
  `query_range_fn` 주입 경로(캡처 안 타는 경로)는
  `test_custom_query_range_fn_still_yields_empty_provenance_
  unchanged_feats`로, "판정 로직 불변"은
  `test_per_metric_provenance_passed_through_to_evidence_
  unchanged`의 `would_signal == (evaluation_seq >= 3)` 단언으로
  확인.
- **(b) 지연/stale/빈 응답에서 시각이 정직한지**:
  `test_evidence_log_honestly_reports_stale_sample_not_hidden`
  (180초 지연이 축소 없이 그대로 기록), `test_evidence_log_
  honestly_reports_no_samples_not_fabricated_timestamp`(queue
  무응답 시 `last_sample_ts_utc`가 `None` - 가짜 시각 생성 안 함).
  **범위 한정(정정, 2026-09-26 2회차 Codex 검토로 확인)**: 이
  테스트들은 **주입된 값의 산술·전달이 정직한지**(축소·날조 없음)
  만 검증한다 - 실제 Prometheus 응답에서 그 timestamp가 참으로
  무엇을 의미하는지(평가 격자 시각 vs 원본 스크레이프 시각)까지
  검증하지는 않는다. 그 의미론적 한계는 §1.1/§7에 별도로 기록.
- **(c) 레코드 누락 없음**: 기존 §92/§161의 write-ahead·flush
  회귀 테스트(변경 없이 그대로 재실행) + 이번 §2의 전체 스위트
  재통과로 확인 - 새 필드 추가가 레코드 생성 자체에 영향 없음.
- **(d) 민감정보 비유출**: 새로 추가된 4개 필드(`query_sent_at_
  utc`/`query_received_at_utc`/`feature_computed_at_utc`/
  `per_metric_provenance`)는 전부 ISO 시각 문자열과 지표명·
  숫자뿐 - §161에서 이미 확인한 "요청 본문·자격증명 필드 자체가
  코드에 없음" 스키마 고정 테스트(`test_evidence_log_schema_
  is_fixed_and_has_no_env_or_secret_leakage`)의 `expected_keys`에
  새 필드 4개를 추가해 재확인.

---

## 3. 미래 일정·주입 스케줄 정보 유입 여부 - 코드 전수 확인

두 detector의 `argparse` 표면을 전수 확인(이번 턴 기준, 변경
없음):

- `fixed_threshold.py`: `--once`, `--run-id`, `--cpu-limit-cores`,
  `--evidence-log`
- `score_server.py`: `--once`, `--run-id`, `--artifacts-dir`,
  `--model-version`, `--evidence-log`, `--stop-file`

**결론**: 두 detector 모두 장애 주입 시점·스케줄·시나리오 종류를
받을 수 있는 인자가 구조적으로 없다(`run-id`는 사후 감사 묶음용
불투명 문자열일 뿐, 시각·스케줄 정보 없음). §163에서 추가한
provenance 캡처도 Prometheus 응답에서만 시각을 뽑을 뿐 외부
스케줄 정보를 참조하지 않는다(코드 전체에서 `injection`/
`schedule`/`scenario` 관련 import·참조 0건 - `grep`으로 직접
확인). **유입 경로 없음, 확인 완료.**

---

## 4. 개별 요청 피해 임계값 - 여전히 "미결정"

§160에서 이미 확인한 사실을 재확인만 한다(재조사 없음,
`slo-definition.md` 변경 없음): `LATENCY_THRESHOLD=0.648s`는
**집계 P95에 대해서만** 공식 정의돼 있다(`docs/design/
slo-definition.md` 63/68행 근방). 개별 요청 단위의 "피해"
임계값은 이 저장소 어디에도 독립적으로 정의돼 있지 않다.

**이번 턴도 이 값을 pilot 데이터나 추정으로 새로 만들지 않았다.**
계속 "미결정"으로 남긴다 - 이 결정은 §156/§159의 NO-GO 판정
근거와 직결되므로, 사용자 승인 없이 암묵적으로 채워 넣으면 §155가
지적한 것과 같은 종류의(판정 기준을 조용히 바꾸는) 오류가 된다.

---

## 5. 포트포워드 프로세스 소유권/종료 확인 방법 검토 (설계 검토만, 라이브 재확인 없음)

지시대로 §162에서 실측한 내용을 **설계 수준에서만** 재검토한다
- 이번 턴은 클러스터 접속이 없으므로 라이브로 재확인하지 않았다.

**§162에서 관찰된 현상**: `kubectl port-forward ... &`를 Bash
도구로 실행하면, `$!`가 보고하는 PID는 **MSYS2/Git Bash가
에뮬레이션하는 POSIX 서브셸의 PID**이지, 실제로 소켓을 열고
있는 Windows 네이티브 `kubectl.exe` 프로세스의 PID가 아니다(Git
Bash는 fork/exec를 POSIX 계층으로 흉내내면서 Windows 프로세스를
별도로 스폰하는 구조이기 때문 - 두 PID 네임스페이스가 다르다).
그 결과 `taskkill //PID <bash가 보고한 PID>`는 "프로세스 없음"
으로 실패하는데, 실제로는 포트포워드가 계속 살아있다(§162에서
`curl`로 직접 확인됨).

**§162가 실측으로 찾은 올바른 방법**: PID를 프로세스 시작 시점의
`$!`가 아니라, **포트 자체의 실제 소유자**로 역산한다 -
`Get-NetTCPConnection -LocalPort <port> -State Listen |
Select-Object OwningProcess`로 실제 Windows PID를 얻은 뒤
`Stop-Process -Force -Id <그 PID>`로 종료. §162는 이 방법으로
실제 종료에 성공했고, 종료 후 `curl`로 연결 자체가 끊겼음을
재확인했다.

**이번 검토에서 추가로 짚어야 할 점(설계 수준, 미검증)**:

1. `Get-NetTCPConnection`은 **리스닝 소켓 기준**이라, 포트포워드
   프로세스가 아직 리스닝을 시작하기 전(기동 직후 수백ms) 또는
   이미 죽어서 리스닝을 놓은 뒤에 조회하면 빈 결과를 준다 - "아직
   못 찾음"과 "이미 안 살아있음"을 이 명령 하나로는 구분 못한다.
   다음에 이 방법을 쓸 때는 조회 결과가 비었을 때 별도로
   `curl`/TCP 연결 시도로 "정말 안 떠 있는지"를 교차 확인하는
   단계를 명시적으로 넣어야 한다(§162는 어쩌다 순서가 맞아
   성공했을 뿐, 이 경합을 구조적으로 막는 절차는 아직 없다).
2. 같은 포트를 다른 프로세스가 먼저 점유하고 있다가 방금 죽어
   `OwningProcess`가 **다른 무관한 프로세스**를 가리킬 여지가
   이론상 있다(포트 재사용 경합) - 실무적으로는 로컬 개발
   환경에서 확률이 낮지만, "그 PID가 진짜 우리가 띄운
   kubectl.exe인지"를 `Get-Process -Id <PID> | Select
   ProcessName, Path`로 한 번 더 대조하는 검증 스텝을 다음
   프로토콜에 추가하는 게 안전하다.
3. 종료 확인은 `Stop-Process` 호출 성공 여부만으로 끝내지 않고,
   §162가 실제로 한 것처럼 종료 **후** `Get-NetTCPConnection`
   재조회(빈 결과여야 함) + `curl` 연결 실패 확인까지 **양쪽 다**
   표준 절차에 넣어야 한다 - 하나만으로는 "종료 신호를 보냄"과
   "실제로 죽음"을 구분 못한다.

**결론**: §162의 방법은 실측으로 동작이 확인됐고 원리도 타당하다.
다만 위 3가지(리스닝 지연 경합, 포트 재사용 오탐, 이중 확인
표준화)는 다음 라이브 smoke/pilot 프로토콜에 **명시적 절차**로
넣을 것을 권고한다 - 이번 턴에는 코드·문서 수준 권고만 남기고
실측 검증은 하지 않았다(지시 준수).

---

## 6. 보존 확인

- §152의 0/40, 후속 5쌍 결과: 파일 미접근(읽기조차 안 함) - 그대로.
- 모델(v3.2b artifacts)·SLO 정의(`slo-definition.md`)·임계값
  (`LATENCY_THRESHOLD`, `FRESHNESS_MAX_AGE_SEC`,
  `CONSECUTIVE_THRESHOLD` 등): 코드에서 값 자체를 읽는 위치만
  거쳤을 뿐 수정 0건(diff로 확인 - 전부 로깅 관련 라인만 변경).
  `evaluate_v32b()`/`advance_streak()`/판정 분기 자체는 단
  한 줄도 안 바뀜.
  `experiments/analyze_precursor_timing_153.py` 및 그 출력
  (`_precursor_timing_analysis_153.json`): 미접근.

---

## 7. 남은 계측 공백 (통과로 처리하지 않는 것들)

1. **§162 §3의 원래 공백은 "부분적으로만" 메워졌다 - "15초가
   해상도의 물리적 바닥"이라는 최초 표현은 부정확했다(정정)**:
   최초 작성 시 이 항목은 "15초 스크레이프 주기 자체가 해상도의
   물리적 바닥"이라고 적었으나, 이는 두 가지를 놓친 부정확한
   서술이었다(Codex 읽기 전용 검토로 확인됨, 2026-09-26). 첫째,
   `query_range` API가 돌려주는 timestamp는 **쿼리 평가 격자
   시각**이지 원본 스크레이프 시각 자체가 아니다 - Prometheus의
   staleness/lookback 처리 때문에 오래된(stale) gauge 값이 최근
   평가 시각에 실린 채로 반환될 수 있고, `cpu` 지표는 애초에
   `rate(...[30s])` 구간 계산값이라 "하나의 원본 표본"과 대응하지도
   않는다. 그래서 §1.1의 상한(`last_sample_ts_utc`)·하한 자체가
   "확실한 물리적 바닥"이 아니라, 평가 격자 시각에서 기계적으로
   유도했을 뿐 **원본 스크레이프 이벤트를 검증하지 않은 참고값**
   이다(guaranteed bound 아님 - §1.1에 lookback/stale marker
   구분과 함께 정정 기록, 2026-09-26 2회차 Codex 검토로 확인).
   둘째(아래 새 항목 참고), 15초라는 숫자 자체가
   네 지표 모두에 근거 있는 게 아니다. 코드(§163이 수정한 범위)는
   그대로 두고 이 문서의 서술만 정정한다 - 판정 로직·상수 값
   자체는 이번 정정에서 변경하지 않았다.
2. **`PROM_SCRAPE_INTERVAL_SEC=15`가 `cpu`/`memory`에는 근거가
   없다(신규 확인, 2026-09-26)**: §1.1에 정정 기록. `queue`/`cache`는
   `servicemonitor.yaml`의 15초 스크레이프로 근거가 있지만,
   `cpu`/`memory`(cAdvisor 출처)의 실제 스크레이프 주기는 이
   저장소 어디에도 정의돼 있지 않다 - `gitops/` 전체에서
   `interval:` 설정을 전수 검색해도 이 ServiceMonitor 한 건뿐이다.
   `_raw_sample_staleness()`가 이 상수를 네 지표 모두에 동일
   적용하므로, `cpu`/`memory`의 `underlying_value_true_lower_
   bound_utc`는 **미확인 숫자로 계산된 값**이다 - 코드를 바꾸지
   않는 한 이 두 지표의 하한은 "확인 불가"로 취급해야 한다(다음
   코드 변경 시 처리할 항목, 이번 문서 정정 범위 밖).
3. **`queue` 지표가 실부하 하에서도 0으로 남는지는 여전히 미확인**
   (§162 §4 그대로 - 이번 턴은 오프라인이라 재확인 불가).
4. **개별 요청 피해 임계값 미결정**(§4) - 다음 측정 pilot을 설계
   하려면 결국 사용자가 명시적으로 결정하거나, 최소한 "집계 P95
   기준으로만 판단한다"는 대안을 명시적으로 채택해야 한다.
5. **포트포워드 종료 절차의 경합 조건 3가지**(§5) - 다음 라이브
   세션 전에 절차화가 필요하다.

---

*작성: 2026-09-26. 이번 턴은 완전히 오프라인(클러스터 접속·본
pilot·장애 주입·promotion·모델/SLO/임계값/판정 로직 변경 전부
0건). §140~§162 전부 보존. 코드 변경은 features.py/
fixed_threshold.py/score_server.py 3개 파일 + 신규/확장 테스트
3개 파일이며, 전체 회귀 스위트(257개) 통과로 판정 로직 불변을
확인했다. 다음 단계는 Codex CLI의 읽기 전용 방향 검토를 거친
뒤 사용자 승인을 기다린다(본 문서 다음 보고 참고).*
