#!/usr/bin/env python3
"""v3 정상 데이터 후보 세션 - Isolation Forest 감사(2026-09-20) 이후 지시.
v1(data/regimes.jsonl)은 이력으로 그대로 두고 절대 덮어쓰지 않는다.

각 세션은 이미 완료된 파일럿·calibration·탐색 실행의 JSON에 남은 실측
타임스탬프(t_preview_ready/t_baseline_ready/t_run_start/t_injection 등)에서
"주입도, promotion도, 아직 안 일어난 안정 구간"만 골라 손으로 정의했다 -
자동 추출이 아니다(경계 판단은 코드가 아니라 사람이 각 trial의 실제 의미를
보고 확정해야 한다는 원칙, guideline.md 감사 지시와 동일).

**cutover 기준**: gitops/apps/vllm-serving/rollout.yaml의 CPU limit
4->3코어 변경 커밋(a479f842, 2026-09-18T14:16:13+09:00=05:16:13Z)과 그 뒤
콜드스타트 안정화 커밋들(마지막 17:47+09:00=08:47Z) 이후로 안전하게
잡기 위해 **2026-09-18T09:00:00Z**를 컷오버로 쓴다(보수적 여유) - 이 이전
데이터는 전부 제외.

**시작 여유(margin)**: `active_plus_preview` 세션은 preview가 Ready로
확인된 시각(t_preview_ready)에서 30초를 더한 시점부터 쓴다(readiness
프로브는 통과했어도 preview pod 자체의 초기 안정화 잔재가 남아있을 수
있다는 보수적 가정 - 이 저장소의 기존 "HEADROOM-COLDSTART" 재작업들과
같은 우려). `active_only` 세션(native, preview 없음)은 t_run_start(또는
t_round_start)에서 30~90초를 더한 시점부터 쓴다(PREPARING 자체가 짧아
preview 세션보다 여유를 적게 잡아도 됨).

**끝 경계**: 전부 `t_injection`(또는 그에 준하는 실제 주입 요청 시각) *이전*
까지만 - 계약서·이 프로젝트의 기존 관례대로 주입·promotion·recovery 구간은
전부 배제한다. `t_cleanup_done` 이후 구간(recovery/drain)은 "잔여 영향이
불명확"하므로 이번 목록에 포함하지 않았다(§ 감사 지시 - 배제 대상)."""
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

CORE3_CUTOVER_UTC = datetime(2026, 9, 18, 9, 0, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class CandidateSession:
    session_id: str
    regime: str
    topology: str  # "active_only" | "active_plus_preview" | "post_promotion_single_active"
    start_utc: datetime
    end_utc: datetime
    source_run_id: str
    notes: str = ""


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


# regime="probe_baseline" - 이 세션들은 v1의 low_load/sustained_load/burst
# regime_configs(YAML)로 만든 게 아니라, load_ramp/pod_kill/network_degrade/
# memory_pressure 각 시나리오의 **기본 probe profile** baseline 구간이다.
# v1 taxonomy와 강도가 다를 수 있어 같은 이름을 억지로 붙이지 않았다 -
# §5(목표 데이터 기준)에서 저강도/버스트 regime_configs 기반의 진짜
# low_load/sustained_load/burst는 별도 live 수집이 필요하다고 본다.

# --- active_plus_preview (최우선 topology - proposed/fixed_threshold가
# 실제로 감시하는 상태와 일치, §2 topology 조사 결론) ---
ACTIVE_PLUS_PREVIEW_SESSIONS = [
    CandidateSession(
        session_id="pk-ft-20260919-baseline", regime="probe_baseline", topology="active_plus_preview",
        start_utc=_t("2026-09-19T05:52:44.13+00:00"), end_utc=_t("2026-09-19T05:54:41.82+00:00"),
        source_run_id="pilot-pod_kill-fixed_threshold-01-20260919T054802Z",
        notes="t_preview_ready(05:52:14.13)+30s ~ t_injection(05:54:41.82)",
    ),
    CandidateSession(
        session_id="pk-proposed-20260919-baseline", regime="probe_baseline", topology="active_plus_preview",
        start_utc=_t("2026-09-19T06:26:20.89+00:00"), end_utc=_t("2026-09-19T06:28:13.33+00:00"),
        source_run_id="pilot-pod_kill-proposed-01-20260919T062317Z",
        notes="t_preview_ready(06:25:50.89)+30s ~ t_injection(06:28:13.33)",
    ),
    CandidateSession(
        session_id="nd-ft-20260919-baseline", regime="probe_baseline", topology="active_plus_preview",
        start_utc=_t("2026-09-19T16:07:32.67+00:00"), end_utc=_t("2026-09-19T16:09:20.91+00:00"),
        source_run_id="pilot-network_degrade-fixed_threshold-01-20260919T160306Z",
        notes="t_preview_ready(16:07:02.67)+30s ~ t_injection(16:09:20.91)",
    ),
    CandidateSession(
        session_id="nd-proposed-20260919-baseline", regime="probe_baseline", topology="active_plus_preview",
        start_utc=_t("2026-09-19T16:26:59.67+00:00"), end_utc=_t("2026-09-19T16:28:49.18+00:00"),
        source_run_id="pilot-network_degrade-proposed-01-20260919T162315Z",
        notes=("t_preview_ready(16:26:29.67)+30s ~ t_injection(16:28:49.18). "
               "이 trial은 이후(주입 이후) target_replaced=True(promotion) - "
               "이 세션 구간(주입 전)에는 영향 없음, 참고용으로만 남김."),
    ),
    CandidateSession(
        session_id="lr-ft-20260918-baseline", regime="probe_baseline", topology="active_plus_preview",
        start_utc=_t("2026-09-18T17:46:34.53+00:00"), end_utc=_t("2026-09-18T17:49:28.59+00:00"),
        source_run_id="pilot-load_ramp-fixed_threshold-01-20260918T174240Z",
        notes="t_preview_ready(17:46:04.53)+30s ~ t_injection(17:49:28.59)",
    ),
    CandidateSession(
        session_id="lr-proposed-20260918-baseline", regime="probe_baseline", topology="active_plus_preview",
        start_utc=_t("2026-09-18T18:18:58.77+00:00"), end_utc=_t("2026-09-18T18:21:57.16+00:00"),
        source_run_id="pilot-load_ramp-proposed-01-20260918T181524Z",
        notes="t_preview_ready(18:18:28.77)+30s ~ t_injection(18:21:57.16)",
    ),
]

# --- active_only (보조 topology - native만의 baseline, preview 없음) ---
ACTIVE_ONLY_SESSIONS = [
    CandidateSession(
        session_id="lr-native-20260918-1301-baseline", regime="probe_baseline", topology="active_only",
        start_utc=_t("2026-09-18T13:01:41.69+00:00"), end_utc=_t("2026-09-18T13:03:27.04+00:00"),
        source_run_id="pilot-load_ramp-native-01-20260918T130111Z",
        notes="t_run_start(13:01:11.69)+30s ~ t_injection(13:03:27.04)",
    ),
    CandidateSession(
        session_id="lr-native-20260918-1414-baseline", regime="probe_baseline", topology="active_only",
        start_utc=_t("2026-09-18T14:14:50.76+00:00"), end_utc=_t("2026-09-18T14:17:32.90+00:00"),
        source_run_id="pilot-load_ramp-native-01-20260918T141420Z",
        notes="t_run_start(14:14:20.76)+30s ~ t_injection(14:17:32.90)",
    ),
    CandidateSession(
        session_id="lr-native-20260918-1637-baseline", regime="probe_baseline", topology="active_only",
        start_utc=_t("2026-09-18T16:37:32.61+00:00"), end_utc=_t("2026-09-18T16:40:16.01+00:00"),
        source_run_id="pilot-load_ramp-native-01-20260918T163702Z",
        notes="t_run_start(16:37:02.61)+30s ~ t_injection(16:40:16.01)",
    ),
    CandidateSession(
        session_id="mp-direct-rep1-20260920-baseline", regime="probe_baseline", topology="active_only",
        start_utc=_t("2026-09-20T04:56:24.61+00:00"), end_utc=_t("2026-09-20T04:57:56.17+00:00"),
        source_run_id="explore-memory_pressure-native-1500mb-120s-20260920T045524Z",
        notes=("t_round_start(04:55:24.61)+60s(=PROBE_STARTUP_TIMEOUT_SEC 상한) ~ t_injection(04:57:56.17) - "
               "이번 세션 §56/57에서 이미 검증된 파일. 처음엔 90초 여유를 뒀더니 rep3가 0개 윈도우가 돼 60초로 낮췄다"),
    ),
    CandidateSession(
        session_id="mp-direct-rep2-20260920-baseline", regime="probe_baseline", topology="active_only",
        start_utc=_t("2026-09-20T05:04:34.56+00:00"), end_utc=_t("2026-09-20T05:06:15.62+00:00"),
        source_run_id="explore-memory_pressure-native-1500mb-120s-20260920T050334Z",
        notes="t_round_start(05:03:34.56)+60s ~ t_injection(05:06:15.62)",
    ),
    CandidateSession(
        session_id="mp-direct-rep3-20260920-baseline", regime="probe_baseline", topology="active_only",
        start_utc=_t("2026-09-20T05:12:42.19+00:00"), end_utc=_t("2026-09-20T05:14:05.54+00:00"),
        source_run_id="explore-memory_pressure-native-1500mb-120s-20260920T051142Z",
        notes="t_round_start(05:11:42.19)+60s ~ t_injection(05:14:05.54)",
    ),
]

ALL_CANDIDATE_SESSIONS = ACTIVE_PLUS_PREVIEW_SESSIONS + ACTIVE_ONLY_SESSIONS


def validate_sessions(sessions=ALL_CANDIDATE_SESSIONS) -> list:
    """순수 함수 - 사전 등록된 배제 규칙을 세션 정의 자체에 기계적으로
    적용한다(컷오버 이전·역전된 구간 등). 위반이 있으면 문자열 목록을
    반환(비어있으면 전부 통과)."""
    problems = []
    seen_ids = set()
    for s in sessions:
        if s.session_id in seen_ids:
            problems.append(f"{s.session_id}: session_id 중복")
        seen_ids.add(s.session_id)
        if s.start_utc < CORE3_CUTOVER_UTC:
            problems.append(f"{s.session_id}: 3코어 전환({CORE3_CUTOVER_UTC.isoformat()}) 이전 - 제외 대상인데 목록에 있음")
        if s.end_utc <= s.start_utc:
            problems.append(f"{s.session_id}: end_utc가 start_utc보다 앞서거나 같음")
        if s.topology not in ("active_only", "active_plus_preview", "post_promotion_single_active"):
            problems.append(f"{s.session_id}: 알 수 없는 topology '{s.topology}'")
    return problems
