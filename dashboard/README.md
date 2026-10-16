# AIOps 시연 대시보드 (READ-ONLY)

교수님 시연에서 "지금 시스템에서 무슨 일이 일어나고 있는가"를 한 화면(1920x1080)으로 보여 주는 관찰 전용 화면이다.
대시보드는 클러스터·정책·탐지기를 바꾸지 않는다. 실제 시연 절차(부하·탐지·전환)는 기존 실행기/터미널에서 따로 돌린다.

## 실행

```bash
# 연습(MOCK DATA 각본, 클러스터 접속 없음) - 화면 상단에 MOCK DATA 표시
python dashboard/server.py --mock
# 실제 시연(LIVE) - 실행기 세션이 연 포트포워드(9090, 18090)와 kubeconfig를 읽기만 한다
python dashboard/server.py
```

브라우저에서 `http://127.0.0.1:8765/`를 연다(F11 전체 화면). Python은 `pythoncore-3.14-64/python.exe`를 쓴다.

LIVE 옵션:
- `GUARDED_ADMIN_TOKEN` 환경변수: 정책 `/admin/state`를 GET으로 읽을 때만 쓴다(화면·응답에 남기지 않음). 없으면 Traffic Verification이 UNKNOWN이고 VERIFIED가 표시되지 않는다.
- `--since-minutes N`: 대시보드를 실행 도중에 다시 켰을 때, N분 전에 시작한 실행까지 이번 세션으로 본다(기본 0 = 대시보드 시작 이후 실행만).
- `--policy-url`, `--results-dir`, `--port`: 기본값 `http://127.0.0.1:18090`, `experiments/results`, `8765`.

## 화면이 읽는 실제 데이터

| 화면 | 출처(읽기 전용) |
|---|---|
| Active / Preview Pod, Service Flow | `vllm-active`·`vllm-preview` Service selector, `vllm-active` Endpoints(targetRef), Pod 목록, Rollout status |
| AI Anomaly Detection | 실행기가 쓰는 `experiments/results/**/c1-live-evidence-<run>.jsonl` (평가 15초 주기, 줄바꿈으로 끝난 줄만) |
| Recovery | 위 evidence의 정책 전송 결과 + 정책 `GET /identity`, `GET /admin/state` |
| 복구 조건 검증 | evidence의 정책 응답 `policy_outcome`·`reasons`(`guarded_policy.check_target` + 신호 시각 검사). 정책은 거부 사유만 돌려주므로 통과 결과 + 사유 없음 = 전 항목 ✓, 사유가 있으면 해당 항목만 ✕, 검사 전에 멈췄으면 – |
| Traffic Verification | 정책 `service_check`(각 Pod `/metrics` 요청 카운터를 직접 읽은 검증 결과: new/old 증가량) |
| User Service | `experiments/results/probe-<run>-*-raw.csv` 직전 60초를 `slo_judge`(SLO v3)로 평가 |

## 상태 판정 규칙

- **SWITCHED**: 대시보드가 직접 읽은 Service selector(Endpoints 읽기 앞뒤 2회 일치)와 ready Endpoints의 Pod가 전부 이번 실행 조치의 새 hash·UID일 때만. 승격 API 성공만으로는 PROMOTING.
- **VERIFIED**: SWITCHED + 같은 실행의 정책 조치 `verified`, `service_check.ok=true`, 새 Pod 요청 증가 > 0 + 승격 이후로만 채워진 60초 사용자 요청 창이 SLO v3 정상(표본 20개 이상).
- **record_only**: 전환하지 않는 모드라 RECORD ONLY로 표시(PROMOTING 아님).
- 조회 실패·오래된 값은 UNKNOWN / STALE / NO DATA로 표시한다. 0이나 HEALTHY로 채우지 않는다.
- 전환 뒤 탐지기는 active=preview라 평가를 보류한다. 전환이 실제로 확인된 경우에만 화면에 POST-SWITCH로, 그 밖의 건너뜀·공백은 UNKNOWN으로 표시한다.
- 화면은 `state.present()`가 만든 단순 표시값(view["ui"])만 크게 보여 준다. hash·selector·IF 점수/임계값 등은 오른쪽 아래 Details에만 있다.
- 타임라인 5단계는 증거가 있을 때만 체크한다: 정상 서비스(대시보드가 HEALTHY 관측) → 이상 감지(IF 이탈 1회째) → 복구 시작(실제 승격 호출) → 서버 전환(Service/Endpoints 확인) → 검증 완료(VERIFIED).

## 테스트

```bash
python -m pytest dashboard -q
```
