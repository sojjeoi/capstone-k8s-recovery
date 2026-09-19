# network_tolerant probe timeout calibration - 원본 증거

`network_degrade`의 readiness/liveness `timeoutSeconds` 후보를 **격리 calibration pod**에서 실측한 결과의 원본 JSON이다.
pilot이며 본 분석에서 제외된다(`collect_metrics`의 `trial-*.json` 집계 대상이 아니다). 규칙·해석은
`../../phase8-blue-green-preflight-incident.md`의 §42(절차)·§43(v1 결과)·§44(v2 사전 등록)·§45(v2 결과)에 있다.
결과 JSON의 원래 위치(`experiments/results/pilot/`)는 gitignore라 여기에 복사해 보존한다.

| 파일 | 무엇 | 도구 커밋 | 판정 |
|---|---|---|---|
| `calibration-network-tolerant-calib-net-tolerant-20260919t103257z.json` | §43 v1 1회(후보 10초, active와 무관한 격리 pod) | `a6cfabb` | v1 규칙 `MARGINAL` / 권고 `NONE` |
| `calibration-network-tolerant-calib-net-tolerant-20260919t135919z.json` | §44 v2 **1회차**(후보 11초) | `72f8b6c` | `PASS` |
| `calibration-network-tolerant-calib-net-tolerant-20260919t142045z.json` | §44 v2 **2회차**(후보 11초, 새 pod·새 CR) | `72f8b6c` | `PASS` |
| `calib-v2-run1-console.log`, `calib-v2-run2-console.log` | v2 두 회차의 콘솔 출력 | `72f8b6c` | - |

### 무결성 (sha256)

도구가 Windows에서 텍스트 모드로 기록해 **원본 파일은 CRLF**다. git은 커밋할 때 LF로 정규화하므로 저장소의 blob 해시는 원본과 다르다(줄바꿈 외 내용은 동일 -
`blob == 원본의 CRLF->LF`를 확인했다). `git show HEAD:<경로> | sha256sum`이 blob(LF) 해시이고, Windows에서 `core.autocrlf=true`로 체크아웃하면 원본(CRLF) 해시와 같아진다.

| 파일 | sha256 - 원본(기록 당시 CRLF) | sha256 - git blob(LF) |
|---|---|---|
| v1 `...t103257z.json` | `21b9c5d2c8fc3ac111775d0138f969b7b2c600ace0c49fdca914e767bc633fb5` | `292d2f1de6d0dc4889cea07b2fce02b0864c2934760a82481b3c41cbacb03965` |
| v2 1회차 `...t135919z.json` | `a240a62dad60c090f5166a791359d8068e151e28dda3228b24be60eec3401628` | `750acf5aeddbea114c515b9a6ce3cbd3d47da32e505f2d37962cbf6fa3cd7b78` |
| v2 2회차 `...t142045z.json` | `4002567e94ace553ab99bd90e38184c6fb1a1d85740ef42c6b261258d09233ad` | `d8ac0d31c79cd6fd613aded9d954774d278986f2b10669454cc11ee0decfc77d` |
| `calib-v2-run1-console.log` | `0cf6d4cf39baaa35a2d09e8bd11e29bce46e240dc037ff917f6ccfa083bbe0f6` | `16933b52eee0d4c0d72abba6874a56556af3cd271559203c71bdc29775201743` |
| `calib-v2-run2-console.log` | `21dfce8932d3887897f8c8cd9843dbbd960403e63c06076e028b251451c559d8` | `c8031f6d350f4888313f5b8055e0a11efd8a212075cb758661466f4ba1d3b2f7` |

v2 콘솔 로그의 `V3_probe_counter_crosscheck: 교차검증 미수행` 표시는 **문구 버그**다 - 교차검증은 수행돼 통과했고(`crosscheck.ok = true`,
아래 표·JSON의 `crosscheck.detail`), 판정 로직은 `crosscheck.ok`만 본다. 두 회차 뒤 도구에서 문구만 고쳤다(판정·측정 코드 불변).

## v2 JSON을 읽는 법

- `windows[]`: 창별(baseline·stage-1~4·recovery-stage-1~4) probe 동등 `/health`·completion 요약과 원본 표본 `samples`
  (`[seq, 창 시작 대비 발행 시각(초), 지연(초), HTTP status(, 오류)]`).
- `timeline`: 구간 경계 시각(UTC ISO) - `pod_created`, `ready`, 각 stage의 `create`·`allinjected`·`delete_request`·`gone`·`teardown_end`, `pod_delete_request`.
- `probe_events[]`: kubelet `Unhealthy` 이벤트를 `count` 증가분으로 푼 발생 목록. `segment`(§44.1 구간)·`ambiguous`(steady 경계 +-1초)·
  `worker_ts`(이벤트 `lastTimestamp`, worker 시계, 초 단위)·`t_pc_iso`(= `worker_ts` - 오프셋 + 0.5초, PC 시계).
- `pod_events`: K8s가 남긴 원본 pod 이벤트(`count`·`first`·`last` 포함). `clock_offset`: worker-PC 오프셋(시작·종료·사용값).
- `crosscheck`·`prometheus`: kubelet probe 카운터(`prober_probe_total`)와 이벤트 집계의 교차검증, kubelet이 잰 소요시간 히스토그램(증거용).
- `analysis`: `judge_v2` 판정 - 조건별 `ok`·`detail`, `run_outcome`, `L_max`·`T_required`·`T_min`.

## 두 회차 요약 (후보 11초)

| | 1회차 | 2회차 |
|---|---|---|
| run_id | `calib-net-tolerant-20260919t135919z` | `calib-net-tolerant-20260919t142045z` |
| 판정 | `PASS` | `PASS` |
| stage-4 `/health` 지연 p50 / p95 / **max(L_max)** | 7.949 / 8.511 / **8.724 s** | 7.956 / 8.456 / **8.642 s** |
| `T_required` -> `T_min` (<= 11) | 10.905 -> **11** | 10.803 -> **11** |
| completion 성공률 | 9개 창 모두 100% | 9개 창 모두 100% |
| steady 구간 probe 실패 | 0 | 0 |
| liveness 실패(shutdown 제외) | 0 | 0 |
| Ready 전이·restart·UID 변경·OOM·eviction·Node 이상 | 0 | 0 |
| teardown readiness 실패 | `teardown_4` 1건 | `teardown_4` 1건 |
| Prometheus 교차검증 (Readiness 이벤트 E1 / 카운터 C / E2, Liveness) | 1 / 1 / 1, 0 / 0 / 0 | 1 / 1 / 1, 0 / 0 / 0 |
| kubelet 소요시간 히스토그램 Readiness (`le=10` / `+Inf`) | 127 / 127 | 124 / 124 |
| 시계 오프셋 시작 / 종료 / 사용 | +0.281 / +0.307 / +0.294 s | +0.320 / +0.366 / +0.343 s |
| cleanup | 완전 성공 | 완전 성공 |

## 구간별 probe 이벤트 (v2)

| 회차 | 이벤트 | 구간 | PC 시각 | 비고 |
|---|---|---|---|---|
| 1 | Startup probe 실패 x19 | `startup` | 13:59:37 ~ 14:02:37 | 콜드스타트(연결 거부) - 판정 대상 아님 |
| 1 | **Readiness 실패 x1** (`context deadline exceeded (Client.Timeout exceeded while awaiting headers)`) | `teardown_4` | 14:13:33.2 | stage-4 CR 삭제 요청(14:13:27.996) **+5.21초**, 소멸 확인(14:13:30.339) +2.87초. 11초 timeout으로 역산한 probe 시작은 삭제 요청 **-5.8초**(steady) |
| 1 | Readiness 실패 x3 (연결 거부·`invalid argument`) | `shutdown` | 14:14:52 ~ 14:15:02 | pod 삭제 뒤 종료 아티팩트 - 판정 제외 |
| 2 | Startup probe 실패 x19 | `startup` | 콜드스타트 | 판정 대상 아님 |
| 2 | **Readiness 실패 x1** (같은 메시지) | `teardown_4` | 14:34:47.2 | stage-4 CR 삭제 요청 **+3.41초**, 소멸 확인 +1.07초. 역산한 probe 시작은 삭제 요청 **-7.6초**(steady) |
| 2 | Readiness 실패 x3 (연결 거부·`invalid argument`) | `shutdown` | 14:36:06 ~ 14:36:16 | 종료 아티팩트 - 판정 제외 |

두 회차 모두 `ambiguous = true`인 이벤트는 없다. Liveness 실패 이벤트는 두 회차 모두 0건이다.
