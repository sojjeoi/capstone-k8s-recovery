# Phase 8 — Session 1: 계측 기술 smoke 결과 (§162)

§140~§161 전부 보존(수정 0건). 승인받은 범위만 실행했다: **계측
기술 smoke 1회**(`--evidence-log` 배선 확인 + 여러 평가 주기 동안의
JSONL 기록 검증). 본 측정 pilot 0건, 공식 trial 0건, 장애 주입 0건,
promotion 0건, 모델·SLO·임계값 변경 0건. **이 smoke 결과를 선제
복구 성능이나 첫 사용자 피해의 증거로 쓰지 않는다** - 순수 계측
인프라 점검이다.

**결론 먼저: PASS(조건부) - 로깅 배선·무결성은 통과, 시각 계측에는
확인된 공백 1건이 남는다(통과로 처리하지 않음).**

---

## 0. 시작 전 읽기 전용 안전 확인 - 전부 충족

- **로컬 자원**: commit-charge 헤드룸 ~22.3GB(§143~146 기준 4GB의
  5배 이상), 최근 재시작 2026-09-25 23:03(위기 징후 없음).
- **git**: `git status`/`git fetch` - working tree clean, HEAD가
  `origin/master`(`ea82045`)와 완전히 일치(+0/-0).
- **`run_all_scenarios.real_safety_checks(trial={}, phase="pre")`**
  (기존 검증된 8항목 스위트를 그대로 호출, 새로 만들지 않음) - 전체
  `ok: true`: `all_nodes_healthy`, `active_endpoint`,
  `rollout_healthy_single_revision`(classification=healthy),
  `active_pod_restart_baseline`, `experiment_context_clear`,
  `quiescent`, `no_leftover_chaos_crs`, `no_leftover_experiment_pods`
  전부 통과.
- **직접 확인**: `kubectl get nodes`(둘 다 Ready, MemoryPressure/
  DiskPressure/PIDPressure 전부 False), `kubectl top nodes`(worker
  CPU 32%/메모리 79% - 새 pod를 안 만드는 이번 smoke엔 영향 없음),
  Rollout 1/1/1/1 단일 리비전, `vllm-active`/`vllm-preview` 둘 다
  같은 pod(`10.244.36.23:8000`)를 가리킴(승격 대기 없음), vllm-serving
  namespace에 최근 이벤트 0건(재시작·OOM 없음), PodChaos/NetworkChaos
  CR 0건, 이상 pod 0건.

8개 항목 전부, 그리고 추가로 확인한 항목들 전부 충족 - 시작을 막을
사유 없음.

---

## 1. `--evidence-log` 배선 확인 (실제 코드 경로로)

`arm_controller._build_detector_command()`를 **직접 호출**해 실제
오케스트레이터가 만들 커맨드를 그대로 얻었다(손으로 만든 커맨드
아님):

- `fixed_threshold`: `[..., "--cpu-limit-cores", "3.0", "--evidence-log", "<경로>"]`
  - **확인됨**: §161에서 추가한 배선이 실제로 인자를 붙인다.
- `proposed`: `[..., "--artifacts-dir", ".../model_v32b/artifacts", "--model-version", "v3.2b", "--evidence-log", "<경로>", "--stop-file", "<경로>.stopfile"]`
  - **확인됨**: 기존(§92) 배선이 그대로 동작한다.

이 커맨드를 그대로(수정 없이) `cwd=anomaly-detection/`에서 실행했다
(오케스트레이터가 실제로 쓰는 것과 동일한 실행 방식).

---

## 2. 여러 평가 주기 - 생성·누락·시각 순서·스키마·민감정보

**공식 결과와 완전히 분리된 경로**에서 실행: `experiments/results/
v2_pilot_smoke/`(신규, gitignore 대상 - `results/*.json`/`*.jsonl`
패턴에 이미 포함돼 git에 안 들어감). 두 detector를 실제 Prometheus
포트포워드(`localhost:9090`) 대상으로 동시에 기동해 **약 95초간**
(EVAL_INTERVAL_SEC=15초 기준 약 6~7 tick) 관찰했다.

| 항목 | fixed_threshold | proposed |
|---|---|---|
| 기록 생성 | 확인(JSONL 파일 생성됨) | 확인 |
| 총 tick 수 | 10 | 8(+정상 종료 레코드 1) |
| 누락 | **없음** - `evaluation_seq` 1~10 연속 | **없음** - 1~8 연속 |
| 시각 순서 | `wall_clock_utc` 단조 증가, 약 15~16초 간격 | `wall_clock_before/after_utc` 단조 증가, 약 15~18초 간격(evaluation 자체 처리에 0.7~2.3초 소요 - 모델 로딩된 프로세스라 예상 범위) |
| 스키마 | §161에서 고정한 15개 키와 일치 | 기존 §92 포맷과 일치(`window_start/end_utc`, `raw/ordered/scaled_feature_vector`, `score`, `is_anomalous` 등) |
| 민감정보 | 없음(요청 본문·자격증명 필드 자체가 코드에 없음, §161에서 이미 회귀 테스트로 확인) | `target_signal_url: "http://localhost:8080/signal"` 필드 존재 - 로컬 포트포워드 주소일 뿐 자격증명 아님. 요청 본문 없음. `artifact_hashes`는 SHA256뿐(비밀 아님) |
| 판정 로직 영향 | `is_anomalous=false`, `consecutive_anomalous=0`, `would_signal=false` **전체 tick에서 일관** - 신호·promotion 경로 전혀 발동 안 함 | 동일(`score`가 `threshold`보다 항상 높아 정상 판정, `would_signal=false` 전체 tick) |

**종료**: `proposed`는 stop-file로 정상 종료(`detector_shutdown` 레코드
확인, `exit_reason=graceful_stop_file`) - 마지막 진행 중이던 cycle을
끝까지 완료한 뒤 종료함. `fixed_threshold`는 stop-file이 없어(§161의
의도된 설계) 강제종료했고, **강제종료에도 evidence 파일에 잘린/깨진
줄이 0건**임을 직접 파싱으로 확인(각 줄이 쓰기 직후 flush+fsync되기
때문 - 설계대로 동작).

이 항목은 **PASS**.

---

## 3. 평가 시각 vs Prometheus 원본 표본 시각 - 확인된 계측 공백 (통과 처리 안 함)

**지시대로, 확인 안 되면 통과로 처리하지 않는다 - 이 항목은 공백으로
보고한다.**

현재 로그(§2)는 두 시각만 남긴다: (a) `window_start_utc`/`window_
end_utc` - **평가 시점에 어떤 구간을 Prometheus에 물어봤는가**, (b)
`wall_clock_utc`(또는 `wall_clock_before/after_utc`) - **그 tick이
실제로 언제 실행됐는가**. 이번에 라이브로 원본 응답까지 직접 확인
했다(§4의 같은 쿼리로 재확인) - Prometheus의 `query_range` 응답은
`[[epoch_ts, value], ...]` 형태로 **개별 표본마다 자기 시각**을
갖고 있는데, `features.py`의 `_query_range()`가 이 표본 시각을
정렬·합산에만 쓰고 **버린 뒤 값 리스트만 반환**한다(코드 233~236행
근방, 최종 반환값에 timestamp 없음). 그래서 **지금 로그만으로는
"이 tick이 실제로 평가한 원본 표본이 정확히 언제 찍혔는가"를
복원할 수 없다** - `window_start/end_utc`는 "요청한 구간"이지
"그 구간 안에서 표본이 정확히 언제 존재했는가"가 아니다.

**판정: 계측 공백 확인됨(통과 아님).** §160/§161이 설계한 로깅은
"탐지기가 어떤 구간을 봤는지·언제 tick을 돌렸는지"까지는 복원하지만,
"신호가 실제로 이용 가능해진 정확한 시각"(원본 표본 시각)까지는
아직 복원할 수 없다. 이 공백을 메우려면 `_query_range()`가 표본
시각도 함께 반환하도록 바꿔야 하는데, 이건 §160 §2가 이미 "판정
로직 자체는 안 바꾼다"는 원칙 안에서도 **추가 코드 변경**이 필요한
항목이라 이번 smoke 승인 범위 밖이다 - 다음 승인이 필요한 항목으로
남긴다.

---

## 4. `queue` - Prometheus 원본 응답으로 확인 (가공된 feature 아님)

Prometheus에 직접 `vllm:num_requests_waiting` 쿼리를 보내(포트포워드
경유, 라이브) **원본 JSON 응답**을 확인했다(가공 전 `_query_range()`
호출 전, `requests.get()` 결과를 직접 봄):

- HTTP 200, Prometheus `status: "success"`.
- **`result` 배열에 시계열 2개**(`job=vllm-active`, `job=vllm-preview`
  - 지금은 두 Service가 같은 pod를 가리키므로 같은 pod의 지표가
  두 라벨로 중복 노출됨) - **시계열 자체는 실재한다.**
- 각 시계열 9개 표본, 값은 전부 문자열 `"0"`.

**결론(사실만, 규칙 변경 없음)**: `queue`는 "쿼리가 아무 시계열도
못 받는" 상태가 **아니다** - 지표가 실제로 노출·스크레이프되고 있고,
지금(무주입, 유휴 상태) 실제 값이 0이라는 뜻이다. 기존 자료(§161의
`v3-inventory.json` 100% 0)와 정합적이다. **다만 이건 "무주입 유휴
상태"에서 확인한 것**이고, `load_ramp` 같은 실제 부하 아래서도
계속 0인지는 이번 smoke 범위(장애 주입 금지) 밖이라 여전히 확인
안 됨 - §160의 원래 계획대로 이 질문은 본 측정 단계로 남는다. 결과를
보고 다른 신호로 바꾸거나 판정 규칙을 조정하지 않았다.

---

## 5. 종료 후 클러스터 상태 - 시작 전과 동일 확인

`real_safety_checks(trial={}, phase="post")` 재실행 - **전체 `ok:
true`, 8항목 전부 통과**(시작 전과 동일). 추가로 직접 대조:

| | 시작 전 | 종료 후 |
|---|---|---|
| pods(`vllm-serving` ns) | `recovery-policy`(0 restart, 4d17h), `vllm-serving`(0 restart, 15h) | **동일**(restart 0/0, age 변화 없음 - 재시작·재생성 없었음) |
| endpoints | active/preview 둘 다 `10.244.36.23:8000` | **동일** |
| rollout | 1/1/1/1, 단일 리비전 | **동일** |
| Chaos CR | 0 | 0(동일) |

**임시 프로세스 정리 확인**: `fixed_threshold`(강제종료, 확인됨),
`proposed`(정상 종료, `detector_shutdown` 레코드로 확인됨), 두
port-forward(`kubectl.exe`, 실제 Windows PID를 `Get-NetTCPConnection`
으로 재확인해 강제종료 - 최초 bash 잡 PID와 실제 Windows PID가
달라 첫 시도는 "프로세스 없음"으로 실패했고, 이 불일치를 찾아
올바른 PID로 재시도해 완료) - 종료 후 `curl`로 8080/9090 둘 다
연결 자체가 안 됨을 재확인. 로컬에 남은 관련 python 프로세스 0건,
commit-charge 헤드룸 21.2GB(시작 전 22.3GB에서 정상 범위 내 변화).

---

## 6. 종합 - PASS/FAIL, 남은 계측 공백, 소요 시간

**PASS**: `--evidence-log` 배선(양 arm), 여러 평가 주기 동안의 기록
생성·무누락·시각순서·스키마·민감정보 비노출, 판정 로직 불변(신호·
promotion 미발동), 클러스터 원상 복구.

**공백(통과 아님, 확인됨)**: 평가 tick 시각과 Prometheus 원본 표본
시각의 구분 - 현재 로그만으로 복원 불가(§3). 다음 코드 변경(및
승인) 대상.

**미확인(이번 smoke 범위 밖, 의도적)**: `queue`가 실제 부하 하에서도
0으로 남는지(§4) - 무주입 상태에서만 확인, 장애 주입은 이번에 승인
범위 밖.

**소요 시간**: 안전 확인+포트포워드 설정+라이브 관찰(~95초)+정상/
강제 종료+클러스터 원상 확인+정리까지 **약 25~30분**(정확한 초 단위
기록은 없음 - 대략치). 승인된 예상(30~45분, 정리 포함 최대 60분)
안에서 끝났다. 연결·자원·안전 문제는 발생하지 않아 중단 사유
없었다.

본 측정 pilot은 시작하지 않았다. §160 §9의 다음 승인 지점(§3-정상
대조 사전 분석, 또는 §3의 새 계측 공백을 메울 코드 변경)이 남아
있다.

---

*작성: 2026-09-26. Session 1 기술 smoke 1회만 실행(승인 범위).
공식 trial·장애 주입·promotion·모델/SLO/임계값 변경 0건. §140~§161
전부 보존. 이 문서의 관측은 계측 인프라 검증용이며 선제 복구 성능·
피해 감소 증거로 사용하지 않는다.*
