# network_degrade 3-arm 파일럿 - 원본 증거 (2026-09-20)

network_tolerant profile(`timeoutSeconds = 11`)에서 `native -> fixed_threshold -> proposed` 각 1회(`is_pilot=true`)를 돌린 파일럿의 원본 기록이다. **pilot이며 본 분석에서 제외**된다.
규칙·해석은 `../../phase8-blue-green-preflight-incident.md` §46, 계약서 §5.8(`transition_straddling`)에 있다. 결과 JSON·raw CSV의 원래 위치(`experiments/results/`)는 gitignore라 여기에 복사해 보존한다.
텍스트 파일은 **LF로 정규화해 복사**했다(원본은 Windows 텍스트 모드 CRLF) - 아래 sha256은 이 저장소의 파일(= git blob) 기준이다.

## 파일

| 파일 | 무엇 | 크기(byte) | sha256 |
|---|---|---|---|
| `chaos-mesh-events-pilot.json` | 세 trial CR 9개의 Chaos Mesh 이벤트 130건(이벤트 TTL 1시간 때문에 조기 추출) | 28,884 | `c5d7ebf622fe0bd6cb29a43e72e730a9648353f24d834366dcdbc6f338998615` |
| `observer-network_degrade-fixed_threshold-analysis.json` | fixed_threshold 관찰기 분석(§5.8) | 3,859 | `d26a20b9c8ea3faa9dd7818579097bf870b7100726e11af93e929edd14a7d109` |
| `observer-network_degrade-fixed_threshold.jsonl` | fixed_threshold trial 관찰기 기록(header/state/heartbeat/chaos/footer) | 132,583 | `01506b3e23f7672690a189c91afc9a85690f98ef1571ab7207c74a17e2de5564` |
| `observer-network_degrade-fixed_threshold.log` | fixed_threshold trial 관찰기 ALERT 로그 | 6,306 | `a4e06f07e730e548ef107305afda53b4013580a4bc67fa9d3d109b75c970e973` |
| `observer-network_degrade-native-analysis.json` | native 관찰기 분석(§5.8) - **CR 단계 타임라인을 Chaos Mesh 이벤트로 재구성**(관찰기 결함, §46.8) | 2,904 | `ffe90585be64cfdcceb786a70365705a771e9f808e973c09d30fb459df17cdee` |
| `observer-network_degrade-native.jsonl` | native trial 관찰기 기록(header/state/heartbeat/chaos/footer) - **CR 스트림 비어 있음**(결함) | 29,568 | `9cda99c1a2566df89c4b7b2aa914a857906bbed5505f46b1f519f8e3879b4bf6` |
| `observer-network_degrade-native.log` | native trial 관찰기 ALERT 로그 | 1,555 | `2aea6209bcba98e780c09606eb7b0ad11d210027ea1750c3e434132f61a1717d` |
| `observer-network_degrade-proposed-analysis.json` | proposed 관찰기 분석(§5.8) | 1,763 | `424aec874cd953b135bc2b41bdc36a050a73f075c2b86a61e40164f0b3b9dd8d` |
| `observer-network_degrade-proposed.jsonl` | proposed trial 관찰기 기록(header/state/heartbeat/chaos/footer) | 122,050 | `7ee563950d494bf5e1ec9af78e9e36c35c2c498db1e91a068ce2cf579d814953` |
| `observer-network_degrade-proposed.log` | proposed trial 관찰기 ALERT 로그 | 5,427 | `23bf33ecba4bff493951d73b2482644a8f9e2a394a5f043a7eee67ee22bd7acb` |
| `observer-profile-restore-to-default.log` | base profile 복원 구간 observer 로그 | 5,811 | `3703ace88407bad5e47e57d1201e0603ff04ace9fa08fe440a401547ef7e6c62` |
| `observer-profile-switch-to-tolerant.jsonl` | profile 전환용으로 띄운 observer의 전 구간 기록(의도치 않게 16:44까지 실행 - 세 arm 전 구간의 독립 읽기 전용 로그) | 444,411 | `ef956f09bbe1b7dc44335ed6e8fe93f321debb9095ffb19432128d85bbdd5d74` |
| `observer-profile-switch-to-tolerant.log` | 위 observer의 ALERT 로그(port-forward 이상 알림은 arm 사이·전환/복원 구간에 port-forward가 없어서) | 20,148 | `97cae36eec7bc9a03332da184401f2babfd760f6adabd5d3e67fa19dca2cd561` |
| `probe-pilot-network_degrade-fixed_threshold-01-20260919T160306Z-fixed_threshold-1-raw.csv` | fixed_threshold SLO probe 원 표본(slo_judge로 baseline_ready·t_slo·t_recovery 재계산 검증) | 78,005 | `44ce5cdf90a9eac02ebd33d06e248d87cff5f78084277a7df8be96a7ac9f9e0f` |
| `probe-pilot-network_degrade-native-01-20260919T154616Z-native-1-raw.csv` | native SLO probe 원 표본(slo_judge로 baseline_ready·t_slo·t_recovery 재계산 검증) | 71,936 | `aa43a36054769b42b21f34ae5a4259b05bab6c984cea1cb56cde036d3d93b912` |
| `probe-pilot-network_degrade-proposed-01-20260919T162315Z-proposed-1-raw.csv` | proposed SLO probe 원 표본(slo_judge로 baseline_ready·t_slo·t_recovery 재계산 검증) | 35,450 | `201490412a596a2fb3a3fa2356c29bb08064971a63b194cdfc5d84d9c6e38c72` |
| `runner-network_degrade-fixed_threshold.log` | fixed_threshold 러너 콘솔 출력 | 466 | `e27de7c35223d905f9903181073cee3120e7c5c30a3b8fa4fa8ab4b85443d2a1` |
| `runner-network_degrade-native.log` | native 러너 콘솔 출력 | 420 | `3d8b0bd8b007ef0a09e20680c0198ecfee2656f56f066be0f97e91015442f05e` |
| `runner-network_degrade-proposed.log` | proposed 러너 콘솔 출력 | 665 | `eb495bc20d1ea9b0bd4c6a6095dee438de9e99e0877c86d8274b13074a82d140` |
| `trial-pilot-network_degrade-fixed_threshold-01-20260919T160306Z.json` | fixed_threshold trial 결과 | 2,768 | `4ab109dc5842ab6df6c15c78bbaccacdf2cba532969d9b50cc46e1dd043c8507` |
| `trial-pilot-network_degrade-native-01-20260919T154616Z.json` | native trial 결과(TrialResult, 스키마 불변) | 2,279 | `d3b971f0bdbfbd3b73e8c32766efdd9249b4e15e98ea5d856dacb4496e35eeb1` |
| `trial-pilot-network_degrade-proposed-01-20260919T162315Z.json` | proposed trial 결과 | 2,866 | `40478d80efd9f3f37d74847d4be666de6af56fe09f7fcb23d742042c774485fd` |

## 읽는 법

- `trial-*.json`: `TrialResult` 그대로(새 필드 없음). `probe-*-raw.csv`: SLO probe의 요청별 `sent_at`·`latency`·`success`.
- `observer-*.jsonl`(한 줄 = 한 레코드, 시각은 PC 시계 epoch 초): `header`(worker 시계 오프셋) / `state`(변경이 있을 때만 전체 상태: pods·services·endpoints·rollout·chaos·nodes·kubelet Unhealthy/Killing 이벤트·로컬 port-forward 응답) /
  `heartbeat` / `chaos`(NetworkChaos CR watch 스트림 - 단계별 생성·AllInjected·삭제 요청·소멸의 수신 시각) / `footer`.
- `observer-*-analysis.json`: `trial_observer.py analyze --probe-timeout 11` 결과 - 단계 타임라인, 분류된 probe 실패(`steady`/`transition_straddling`/순수 `teardown`/`shutdown`), Ready·Endpoint·restart, 중단 조건, **최종 행**.
  native는 관찰기 결함으로 CR 스트림이 비어 Chaos Mesh 이벤트(`chaos-mesh-events-pilot.json`, 초 단위 컨트롤러 시계)로 단계 타임라인을 재구성해 같은 분석을 돌렸다(결과의 `stage_timeline_source` 참고).
- 재현: `python experiments/trial_observer.py analyze <jsonl> --probe-timeout 11 --offset-sec 0.32`(오프셋 생략 시 header/footer의 측정값 평균).

## 최종 행(§5.8) 요약

| trial | steady | transition_straddling | 순수 teardown | shutdown | Ready 전이 | Endpoint 영향 | restart·UID | 판정 |
|---|---|---|---|---|---|---|---|---|
| native | 0 | 0 | 0 | 0 | 없음 | 없음 | 없음 | PASS |
| fixed_threshold | 0 | 0 | 0 | 3 | 없음 | 없음 | 없음 | PASS |
| proposed | 0 | 0 | 0 | 1 | 없음 | 없음 | 없음 | PASS |
