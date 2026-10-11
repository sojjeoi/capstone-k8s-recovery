# Phase 8 — 계측 코드 정정: 평가 격자 시각 vs 원본 스크레이프 시각 (§164)

§163이 추가한 계측 로그는 판정 로직을 안 건드리는 대신, "원본 표본이
언제 참이 됐는가"를 상한/하한으로 표현하는 방식을 썼다. 이후 Codex
읽기 전용 검토(§163 문서 정정, 3회)로 이 표현 자체가 부정확함이
드러났다 - `query_range`가 돌려주는 timestamp는 Prometheus **평가
격자 시각**이지 원본 스크레이프 시각이 아니고, `PROM_SCRAPE_
INTERVAL_SEC=15`는 `cpu`/`memory`(cAdvisor 출처)에는 근거가 없었다.
이번 §164는 이 문제를 **코드 수준**에서 정정한다(§163은 문서만
고쳤었다). 완전히 오프라인 - 클러스터 접속·라이브 smoke·trial·
promotion·모델/SLO/임계값/판정 기준 변경 전부 0건.

---

## 1. 무엇을 바꿨는가

### 1.1 `features.py`

- `CONFIRMED_SCRAPE_INTERVAL_SEC` dict 신설: `{"cpu": None, "memory":
  None, "queue": 15, "cache": 15}` - `queue`/`cache`는 vLLM 자신의
  `/metrics`를 스크랩하는 `servicemonitor.yaml`(`interval: 15s`)에
  근거가 있고, `cpu`/`memory`(cAdvisor 출처)는 이 저장소 `gitops/`
  전체에 스크레이프 주기 설정이 없다(전수 검색 확인).
- `_raw_sample_staleness()`가 이제 `scrape_interval_sec`을 3번째
  인자로 받는다. `None`이면 하한을 계산하지 않는다(미확인 숫자
  대입 금지) - 필드명도 `last_sample_ts_utc`→`eval_grid_ts_utc`(평가
  격자 시각임을 명시), 중복이었던 `underlying_value_true_upper_
  bound_utc` 제거, `underlying_value_true_lower_bound_utc`→
  `interval_based_lower_bound_utc`+`scrape_interval_used_sec`(어떤
  숫자를 썼는지, 또는 `None`인지 그대로 기록).
- `extract_features_with_provenance()`가 이제 **지표별로** 쿼리
  요청/응답 시각을 따로 캡처한다(이전엔 4개 지표 전체를 감싸는 시각
  1쌍뿐 - 어느 지표가 오래 걸렸는지 구분 불가였음).

### 1.2 `fixed_threshold.py`

위 스키마 변경 반영 - 최상위 `query_sent_at_utc`/`query_received_
at_utc`(이제 지표별로 이미 있어 중복) 제거. 판정 로직(`cpu_mean`
비교, 연속 카운트) 무변경.

### 1.3 `score_server.py` — 실패한 tick의 진단 보존

- `DataGapFailClosed`에 선택적 `partial_provenance` 추가(기본값
  `{}` - 기존 raise 호출부·테스트 100% 호환).
- `_evaluate_v32b_verbose()`의 3개 raise 지점(feature 결측/NaN·Inf/
  metric stale) 전부에서, 그 시점까지 캡처된 지표별 provenance를
  예외에 실어 보낸다. `extract_window_strict()`가 `METRICS` dict
  순서(cpu/memory/queue/cache)대로 **첫 gap에서 short-circuit**
  하므로, 그 뒤 지표는 아직 조회 전이다 - 조용히 생략하지 않고
  `"raw_sample_status": "not_queried"`로 명시한다.
- `main()`의 `evaluation_skipped` evidence record에 `per_metric_
  provenance` 필드 추가 - "이 tick에서 무엇을 확인했고 무엇을
  확인 못 했는지"가 실패 기록에도 남는다(이전엔 `reason` 문자열
  뿐이었음).

---

## 2. Codex 읽기 전용 검토 - 2라운드, 지적 3건 전부 해소

**1라운드**(구현 직후 diff+테스트 261개 통과 상태 검토):

| 지적 | 내용 | 조치 |
|---|---|---|
| [P2] | `extract_features_with_provenance()` docstring이 여전히 "원본 표본의 시각/나이"라고 서술(정정 안 된 표현 잔존) | docstring을 평가 격자 시각 기반으로 통일, `raw_sample_status="ok"`가 유효성을 보장 안 한다는 문장 추가 |
| [P2] | NaN/Inf 경로의 partial_provenance 미검증, evaluation_skipped record까지 실제 전달되는지 미검증 | 신규 테스트 3개 추가(NaN 경로, end-to-end 전달, provenance 생략 시 하위호환) |
| [P3] | test fixture 버그 - 날짜 산술 오류(하한이 상한보다 미래), cpu/memory에 15초 대입 | `_FAKE_PROVENANCE_OK` 하나를 interval 유무로 2개로 분리, 잘못된 날짜 계산 자체를 제거 |

**2라운드**(정정 후 diff+테스트 264개 통과 상태 재검토): **"지적
1~3 전부 해소됨"** - 추가 필수 지적 없음. score_server의 on/off
직접 비교 테스트를 안 만든 판단(구조적으로 같은 `_query_range_
with_timestamps` 호출을 재사용할 뿐이라 안전하다는 근거)에 Codex도
동의했으나, "기존 커스텀 테스트는 빈 provenance만 확인하고 feats
동일성 자체는 직접 비교 안 함"이라는 관찰을 남겼다 - 차단 사유는
아니라고 명시했고, 이번 라운드에서 추가 조치는 안 했다(§4에 남은
공백으로 기록).

두 라운드 모두 코드 수정·클러스터 접속·라이브 실행 없이 읽기
전용으로 진행됐다. Codex의 동의는 사용자 승인이 아니다.

---

## 3. 테스트 결과

| 시점 | anomaly-detection/ 전체 |
|---|---|
| §163 기준(직전 turn) | 257개 통과 |
| §164 구현 직후 | 261개 통과(+4) |
| Codex 1라운드 반영 후(최종) | **264개 통과(+3)** |

`test_score_server_data_gap_subprocess.py`(요청-횟수 기반 cycle
카운팅에 민감한 결정론적 subprocess 테스트) 포함 - 0 실패, 매
단계 직접 실행 확인(날조 없음).

지시받은 (a)~(f) 확인:
- **(a) 평가 격자/scrape 시각 오인 정정**: 필드명 변경 + docstring
  정정(§1.1, 2라운드 검토로 확인).
- **(b) 지표별 요청/응답·판단 시각 보존**: `extract_features_with_
  provenance()`/`_evaluate_v32b_verbose()` 둘 다 지표별 캡처(§1.1/
  §1.3), `feature_computed_at_utc`는 판단 시각으로 유지.
- **(c) 실패 tick 진단 보존**: `DataGapFailClosed.partial_
  provenance` + `evaluation_skipped`에 반영, end-to-end 테스트로
  확인(§1.3, §2).
- **(d) cpu/memory에 15초 미대입**: `CONFIRMED_SCRAPE_INTERVAL_SEC`
  + 전용 테스트로 확인.
- **(e) feature·신호·모델·SLO·임계값·연속판정 무변경**: diff에
  `apply_feature_schema`/`scaler.transform`/`decision_function`/
  `advance_streak`/신호 발행 로직 라인이 안 나타남(Codex도 diff
  기준으로 확인).
- **(f) on/off·지연·stale·실패 오프라인 검증**: 대부분 커버(§2),
  score_server의 명시적 on/off 직접 비교 1건은 남은 공백(§4).

---

## 4. 남은 계측 공백 (통과로 처리하지 않는 것)

1. **score_server의 on/off 직접 비교 테스트 부재** - 구조적으로
   안전하다고 판단했고 Codex도 차단 사유로 보지 않았지만, 명시적
   테스트는 아직 없다.
2. **cpu/memory의 실제 cAdvisor 스크레이프 주기는 여전히 미확인**
   (코드가 `None`으로 정직하게 표시하도록 고쳤을 뿐 - 실제 숫자를
   알아내려면 라이브 관찰이 필요하다, §5의 다음 프롬프트 참고).
3. **queue가 실부하 하에서도 0으로 남는지 여전히 미확인**(§162 §4
   그대로, 이번 턴도 오프라인).
4. **포트포워드 종료 절차의 경합 조건 3가지**(§163 문서 §5) -
   다음 라이브 세션 전 절차화 필요.
5. **이 정정 자체는 §156의 NO-GO를 뒤집는 근거가 아니다** - 아래
   §5 참고.

---

## 5. 이제 라이브에서 확인할 수 있는 것 / 아직 주장할 수 없는 것

**정정(2026-09-26, 사용자 지적으로 확인됨) - 철회하는 주장**: 이
섹션의 최초 버전은 "`cpu`/`memory`의 연속된 `eval_grid_ts_utc`
간격을 관찰해 cAdvisor의 실제 스크레이프 주기를 추정할 수 있다"고
썼는데, 이는 **순환 논리였다.** `eval_grid_ts_utc`의 간격은
`query_range` 호출에 우리가 직접 넘기는 `step` 파라미터(코드에서
`"15s"`로 고정)가 만드는 **평가 격자**다 - 우리 자신이 정한 쿼리
설정의 산물일 뿐, cAdvisor가 실제로 언제 스크레이프하는지와는
무관하다. 우리가 이미 알고 있는 값(우리가 설정한 step)을 관찰해서
우리가 모르는 값(cAdvisor의 실제 주기)을 "추정"할 수는 없다. 이
문장은 철회한다 - 아래 표로 대체.

**네 가지 시각을 각각 무엇으로 확인할 수 있는가**:

| 시각 종류 | 확인 방법 | 확인 가능 여부 |
|---|---|---|
| 타깃의 설정된 `scrapeInterval`(cAdvisor 포함) | `query_range`가 아니라 Prometheus의 **타깃 메타데이터** API(`/api/v1/targets`)가 `activeTargets[].scrapeInterval`로 각 타깃에 적용된 설정 주기를 보고한다 - query_range의 격자 간격과 무관 | **라이브에서 확인 가능**(§6 프롬프트 대상, 아직 미실행) |
| 실제 원본 표본 시각(그 표본이 정확히 언제 스크레이프됐는가) | **현재 계측(query_range의 bare selector)만으로는** 복원 불가(§163/§164에서 이미 정정) - 단, 이게 절대적 한계는 아니다: PromQL `timestamp()` 함수는 lookback으로 선택된 표본 자신의 저장 timestamp를 값으로 반환할 수 있다(2026-09-26 Codex 검토로 지적됨, [공식 함수 명세](https://prometheus.io/docs/prometheus/latest/querying/functions/#timestamp)) - 이 기법은 아직 시도·구현 안 됐고, 반영하려면 별도 설계·구현·승인이 필요하다. `/api/v1/targets`의 `lastScrape`는 "타깃 전체의 가장 최근 스크레이프 시각"일 뿐 특정 과거 tick의 특정 시계열 표본과는 별개다 | **현재 계측·조회 범위로는 확인 불가**(`timestamp()` 기법은 미탐구 - §5/§6 어느 쪽도 이번 승인 범위에 포함 안 함) |
| 쿼리 평가 시각(`eval_grid_ts_utc`) | 우리 자신이 `query_range`에 넘긴 `step` 파라미터가 만드는 평가 격자 - 코드 자체가 그 값 | **이미 확인됨**(코드로 이미 기록) |
| detector가 응답을 받은 시각(`query_received_at_utc`) | 우리 코드가 HTTP 응답 직후 찍는 wall clock | **이미 확인됨**(코드로 이미 기록) |

**이제 라이브에서 확인 가능해진 것**(위 표 기준으로 정정): (1)
`/api/v1/targets`를 직접 조회해 cAdvisor(cpu/memory) 타깃의
**설정된** `scrapeInterval`을 확인할 수 있다 - 이 값 자체를
`CONFIRMED_SCRAPE_INTERVAL_SEC`에 반영할지는 별도 승인 대상(§6).
(2) fail-closed가 **실제로 발생하면** `not_queried`/`no_samples`가
의미 있게 기록되는지 관찰할 수 있다 - 단, 무주입 상태라 발생 안 할
수도 있고, 그러면 이 경로는 "미검증"으로 명시해야 한다(§6, 강제
유도 안 함). (3) `queue`가 무주입 상태에서도 실환경 Prometheus로
계속 0인지 재확인 가능.

**아직 주장할 수 없는 것**: 이 계측 정정은 §156의 NO-GO(현재
load_ramp + 동일 용량 BlueGreen 조합에서 탐지 floor를 못 이김)를
뒤집을 근거가 **아니다** - 시각을 더 정직하게 "기록"하게 됐을 뿐,
탐지기가 더 **일찍** 신호를 낼 수 있게 된 게 아니다. `queue`가
실부하 하에서 전조를 보이는지, 개별 요청 피해 임계값(§4 §159 참고
- 여전히 "미결정"), 첫 사용자 피해 전 실제 BlueGreen 전환 완료 -
이 중 어느 것도 이번 턴에 새로 입증되지 않았다(라이브 신호 관찰
0건, 순수 오프라인 코드 변경). **정정(2026-09-26, Codex 검토로
확인)**: 실제 원본 표본 시각(개별 과거 tick의 정확한 스크레이프
순간)은 §6에서 계획한 조회 범위(query_range bare selector,
`/api/v1/targets`)로는 다음 라이브 세션 이후에도 확인 불가로
남는다 - 다만 이것이 스크레이프 아키텍처의 절대적 한계라는 최초
서술은 과도했다. PromQL `timestamp()` 함수로 lookback 선택된
표본 자신의 저장 timestamp를 값으로 조회하는 별도 기법이
있으나(위 표 참고), 이번 §164/§6 범위 밖이라 시도하지 않았다 -
"현재 계측 범위에서 확인 불가"로 한정한다.

---

## 6. 다음 라이브 검증 프롬프트 (승인 필요 - 실행하지 않음, 정정됨)

> §163 §5(포트포워드 프로세스 소유권/이중 종료 확인)의 권고를
> 적용해 무주입 라이브 계측 확인 세션을 1회만 수행하라. §5의 표에
> 따라 **네 가지 시각을 서로 다른 방법으로, 서로 섞지 않고**
> 확인한다:
> (1) cpu/memory(cAdvisor) 타깃의 **설정된** scrapeInterval은
> Prometheus `/api/v1/targets`를 직접 조회해 확인한다 -
> `query_range`의 평가 격자 간격으로 추정하지 않는다(그건 우리
> 자신이 정한 step 파라미터일 뿐이다, §5 정정 참고). 확인된 값은
> 관찰 기록만 남기고, `CONFIRMED_SCRAPE_INTERVAL_SEC`에 반영할지는
> 이번 세션에서 결정하지 않는다(코드 변경은 별도 승인 대상).
> (2) 실제 원본 표본 시각은 이번 세션이 조회하는 범위(query_range
> bare selector, `/api/v1/targets`)로는 복원 불가하므로 "확인
> 불가"로 남긴다(PromQL `timestamp()` 함수로 저장 timestamp를
> 직접 조회하는 별도 기법이 있으나 이번 승인 범위 밖 - §5 정정
> 참고, 절대적 한계라고 서술하지 않는다). `/api/v1/targets`의
> `lastScrape`로 "지금 막 스크레이프됐는가" 정도의 근사적 교차
> 참고만 기록하고, 이를 특정 과거 tick의 확정된 표본 시각인 것처럼
> 서술하지 않는다.
> (3) 쿼리 평가 시각(`eval_grid_ts_utc`)과 (4) detector 응답 수신
> 시각(`query_received_at_utc`)은 이미 코드가 기록하므로 그대로
> 관찰·기록한다.
>
> 추가로: `queue`가 무주입 상태에서도 여전히 0인지 재확인한다
> (§162 §4). 무주입 중 fail-closed가 **자연 발생하면** 그 tick의
> `not_queried`/`partial_provenance`가 의미 있게 기록되는지
> 관찰한다(강제로 유도하지 않는다) - **발생하지 않으면 이 경로는
> "미검증"이라고 결과에 명시적으로 기록한다**(관찰 부재를 통과로
> 처리하지 않는다).
>
> 본 pilot·장애 주입·promotion·모델/SLO/임계값/판정 기준 변경은
> 이번 승인 범위에 포함하지 않는다. 예상 시간: 30~45분(정리 포함
> 최대 60분 - `/api/v1/targets` 조회 1회 추가로 인한 증가는 미미).

이 프롬프트는 **실행하지 않았다** - 클러스터 접속이 필요하므로
사용자 승인을 기다린다.

---

*작성: 2026-09-26. §140~§163 전부 보존(§163 문서의 자체 정정
3회는 별도 turn에서 이미 커밋 전 상태로 처리 중). 이번 §164는
features.py/fixed_threshold.py/score_server.py 3개 파일 + 테스트
3개 파일을 수정했고, 전체 회귀 스위트(264개) 통과 + Codex 읽기
전용 검토 2라운드(지적 3건 전부 해소)로 확인했다. §5/§6은 작성
직후 사용자가 지적한 순환 논리(eval_grid_ts_utc 간격으로 cAdvisor
실제 주기를 추정할 수 있다는 주장)를 발견 당일 철회하고 4가지
시각 종류를 구분하는 표로 대체했다(코드·테스트·회귀 결과는
이 정정의 영향을 받지 않음 - 문서 서술만 수정).*
