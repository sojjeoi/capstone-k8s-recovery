"""사전등록 문서 ↔ 코드 상수 일치 시험(2026-10-17, M5, DEVELOPMENT). 문서에 적은 값이 코드와 다르면 여기서 걸린다.
Phase 2 테스트 계약의 "동결 상수가 사전등록과 일치" 시험(test_frozen_constants_match_prereg...)의 네트워크 환경판이다."""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n2_common as nc  # noqa: E402
import n2_pressure as pr  # noqa: E402
import n2_session as ns2  # noqa: E402

pc = nc.pc
DOC = nc.ROOT / nc.PREREG_DOC
TEXT = DOC.read_text(encoding="utf-8")
FORBIDDEN = ("통계적으로 증명", "장애 예측", "AI가 장애를 판단", "IF가 CPU보다 우수", "IF가 CPU보다 우월", "자동 rollback", "공식 비교 8쌍",
             "Kubernetes는 Pod가 살아 있으면 아무것도 하지 않는다", "promotion 성공 = 복구 성공")


def test_document_exists_and_declares_its_status_until_frozen():
    assert DOC.is_file()
    if nc.PREREG_SHA256 is None:
        assert "DRAFT" in TEXT and "미동결" in TEXT                                   # 동결 전에는 초안이라고 밝힌다
    else:
        assert pc.text_sha256(DOC) == nc.PREREG_SHA256                              # 동결 뒤에는 문서가 한 글자도 바뀌지 않았다


def test_thresholds_artifacts_and_net_probe_evidence_in_the_document_match_the_code():
    for v in (repr(pc.CPU_THRESHOLD), repr(pc.IF_THRESHOLD), repr(nc.NET_THRESHOLD), nc.NET_CALIBRATION_SHA256, nc.NET_DEFINITION_SHA256,
              nc.NET_CALIBRATION_REL):
        assert v in TEXT, v
    for d in nc.DETECTOR_IDS:
        assert f"`{d}`" in TEXT, d
    assert "CPU 임계값 > net_probe > c3-A IF" in TEXT and nc.SIMPLICITY_ORDER == (nc.CPU_ID, nc.NET_ID, nc.IF_ID)
    assert nc.NET_THRESHOLD == nc.FROZEN_DETECTORS[nc.NET_ID]["threshold"]


def test_environment_stages_probe_profile_and_counts_match_the_code():
    for s in nc.NETWORK_STAGES:
        assert s["name"] in TEXT and s["latency"].replace("ms", " ms") in TEXT and str(s["duration_sec"]) in TEXT
    assert nc.PROBE_PROFILE == {"name": "network_tolerant", "readiness_timeout_sec": 11, "liveness_timeout_sec": 11}
    assert "`network_tolerant`" in TEXT and "11 s" in TEXT
    assert (nc.N_NORMAL, nc.N_PRESSURE, nc.N_STAGE2) == (4, 5, 3) and nc.MAX_ATTEMPTS == {"normal": 12, "pressure": 7, "stage2": 5}      # rev3: 정상 시도 상한만 6 -> 12
    for row in ("| Stage 1 정상 | record_only, SHADOW 3후보 | **4** | **12**(rev3: 6에서 증가, §9.2) |", "| Stage 1 압박 | record_only, 네트워크 지연 4단계 | **5** | 7 |",
                "| Stage 2 복구 | act, 선택된 후보만 ACTIVE | **3** | 5 |"):
        assert row in TEXT
    for pfx in ("n2n01", "n2p01", "n2s01"):
        assert pfx in TEXT


def test_selection_gate_constants_match_the_phase2_rules_the_document_says_it_keeps():
    assert (nc.GATE2_LIMIT_SEC, nc.GATE2_MIN_PASS, nc.GATE3_MIN_DIFF, nc.TIMING_MIN_DIFF_SEC, nc.DECISION_SEC) == (363.0, 4, 2, 30.0, 3600.0)
    for s in ("363 s", "4개 이상", "3600 s", "min_unnecessary + 2", "best_median + 30 s", "30 s", "3회 연속", "쿨다운 60 s", "평가 간격 15 s", "평가 창 60 s"):
        assert s in TEXT, s
    for verdict in ("ARCHITECTURE_VALIDATED", "SELECTION_VALIDATED_RECOVERY_PENDING", "MIXED", "NOT_VALIDATED"):
        assert verdict in TEXT
    for state in ("INTEGRITY_FAILURE", "SELECTION_INCOMPLETE", "NO_ELIGIBLE_DETECTOR", "SELECTED:cpu-threshold-c",
                  "SELECTED:net-probe-readiness", "SELECTED:c3-a-if"):
        assert state in TEXT, state


def test_delivery_check_constants_in_the_document_match_the_code():
    assert pr.PROTECTED_MAX_PROBE_SEC == 0.1 and ns2.STAGE_MIN_POINTS == 3 and ns2.MAX_GAP_SEC == 45.0 and ns2.GRACE_SEC == 30.0
    assert ns2.STAGE_TOLERANCE_SEC == 2.0 and ns2.CR_DURATION_MARGIN_SEC == 60 and ns2.OBS_BEFORE_SEC == 60.0
    assert ns2.QUERY_PREROLL_SEC == 30.0 and ns2.QUERY_TAIL_SEC == 45.0 and ns2.PROTECTED_RECHECK_SEC == 30.0 and ns2.OBSERVATION_SEC == 600.0
    for s in ("0.1 s", "3개 이상", "45 s", "삭제 + 30 s", "-2 s", "단계 시간 + 60 s", "`[첫 적용 - 60 s, 마지막 삭제 + 30 s]`",
              "`[첫 생성 - 30 s, 마지막 삭제 + 45 s]`", "30 s 간격", "600 s"):
        assert s in TEXT, s
    assert f"{int(nc.NETWORK_STAGES[0]['duration_sec'] + ns2.CR_DURATION_MARGIN_SEC)} s" in TEXT                # 150 s


def test_live_rules_stated_by_the_user_are_in_the_document():
    for s in ("session-end-check", "전체 시연 상태 원복", "밤새 두지 않는다", "블록 시작", "세션 정리", "전체 원복이 아니다", "INVALID로 표시한다",
              "추정치를 적지 않으며", "10/21", "10/24", "10/25 이후 live 금지", "세션 수를 줄이지 않고", "capture-baseline", "restore-check", "restore-demo-state.ps1",
              "측정한 적이 없", "force push·history 변경 금지", "별도 추가 환경 검증", "합치지 않는"):
        assert s in TEXT, s


def test_rev2_resume_rule_in_the_document_matches_the_code():
    import n2_resume as rn
    for s in ("### 9.1 호스트 중단(PC 절전) 뒤 재개", "authorize-resume", "n2_resume.py", "Kernel-Power 42 또는 506", "`n2n01`", "`n2n02`",
              "policy_state_after_failed", "preview_cleanup_held", "canonical_end_not_reached", "사후", "안전 확인용 집계",
              "rev2", "supersedes", "절전 방지(rev2)", "규칙을 사고 뒤에 추가했다"):
        assert s in TEXT, s
    assert rn.HOST_SLEEP_EVENT_IDS == (42, 506)
    assert all(p in TEXT for p in rn.RESUMABLE_PREFIXES)


def test_rev3_attempt_cap_and_reuse_rules_in_the_document_match_the_code():
    for s in ("### 9.2 정상 시도 상한과 수집 종료", "### 9.3 기존 정상 시도의 처리", "6 → **12**", "유효 정상 4개가 모이기 전에는 압박 세션을 시작하지 않는다",
              "`n2n03`은 재사용한다", "삭제하거나 숨기지 않고 모두 기록한다", "`SELECTION_INCOMPLETE`로 보고한다", "사후 변경임을 밝힌다",
              "rev3의 지위(예외를 밝힌다)", "규칙을 사고 뒤에 바꿨다(rev3)", "정상 시도 상한도 고정"):
        assert s in TEXT, s
    for run in ("`n2n01`", "`n2n02`", "`n2n03`", "`n2n04`"):
        assert run in TEXT
    assert (nc.N_NORMAL, nc.N_PRESSURE, nc.N_STAGE2) == (4, 5, 3)                       # 유효 목표 수는 그대로
    assert nc.MAX_ATTEMPTS["pressure"] == 7 and nc.MAX_ATTEMPTS["stage2"] == 5           # 압박·Stage 2 상한도 그대로


def test_rev4_pre_injection_defect_rule_in_the_document_matches_the_code():
    import n2_resume as rn
    for s in ("### 9.4 실제 압박 주입 이전의 구현 결함 뒤 재개", "**NetworkChaos 0회:**", "**신호 0건:**", "**수정 뒤 새 구현 동결:**", "**깨끗한 상태:**",
              "압박 시도**(`n2p`)", "정상 시도와 Stage 2에는 적용하지 않는다", "`n2p01`은 **무효이며 시도 1회 소진 상태로 그대로 보존**하고 Gate 계산에 포함하지 않는다",
              "**이 규칙은 사후 규칙이다.**", "규칙을 사고 뒤에 추가했다(rev4)", "rev4의 지위(예외를 밝힌다)", "`n2p02`", "압박 시도 상한 7은 그대로다",
              "trial_cleanup_incomplete", "harness_error", "network_session_end: running_marker_left", "HTTP 422"):
        assert s in TEXT, s
    assert rn.CLASSIFICATION_DEFECT == "pre_injection_implementation_defect"
    assert rn.DEFECT_PREFIXES == ("trial_cleanup_incomplete", "harness_error", "network_session_end: running_marker_left")
    assert nc.MAX_ATTEMPTS == {"normal": 12, "pressure": 7, "stage2": 5} and (nc.N_NORMAL, nc.N_PRESSURE, nc.N_STAGE2) == (4, 5, 3)       # 상한·목표 불변


def test_rev5_stage2_n3_early_recovery_rule_in_the_document_matches_the_code():
    import n2_stage2 as s2m
    for s in ("### 6.2.1 Stage 2의 N3: 승격이 확인되면 이후 지연 단계는 수행하지 않아도 된다", "`n2s02`부터", "`n2s01`은 소급해서 유효로 바꾸지 않는다",
              "승격·Service 전환 확인 시각까지 유지", "앞부분 1개 이상", "마지막으로 주입한 단계만", "승격이 없었거나 확인되지 않은 세션",
              "기존 규칙 그대로 **주입 스케줄 4단계 전부**", "새 Pod로 지연이 새지 않았다", "`n3_rule`", "`early_recovery_rev5`", "`strict`",
              "이 규칙은 사후 규칙이다.", "규칙을 성공한 세션을 본 뒤에 고쳤다(rev5)", "rev5의 지위(예외를 밝힌다)", "`EARLY_RECOVERY_FROM = 2`",
              "감지기 임계값, Stage 1 선택 결과, Recovery Policy 코드, 트래픽 검증 기준", "`n2s01` (Stage 2)", "새 Pod +63"):
        assert s in TEXT, s
    assert s2m.EARLY_RECOVERY_FROM == 2 and not s2m.early_recovery_applies("n2s01") and s2m.early_recovery_applies("n2s02")
    assert nc.MAX_ATTEMPTS == {"normal": 12, "pressure": 7, "stage2": 5} and (nc.N_NORMAL, nc.N_PRESSURE, nc.N_STAGE2) == (4, 5, 3)       # 상한·목표 불변


def test_document_does_not_use_any_forbidden_claim_phrase_and_states_its_limits():
    for phrase in FORBIDDEN:
        assert phrase not in TEXT, phrase
    for s in ("## 14. 주장 경계", "## 18. 알려진 한계", "측정 위치", "보정 조건 차이", "표본 크기", "개발 자료"):
        assert s in TEXT, s
    for s in ("probe timeout 설정이", "확인되지 않았다", "재보정하지 않는다", "새 보정·새 사전등록 revision", "일반적으로 더 낫다는 주장",
              "SESSION INVALID — target isolation / new-Pod leakage exclusion not established", "감지기 실패가 아니다", "NO_SELECTION"):
        assert s in TEXT, s
    assert "공식 세션" in TEXT and "**0개.**" in TEXT                                  # 아직 공식 세션이 없다고 밝힌다


def test_section_numbers_cross_referenced_inside_the_document_exist():
    sections = set(re.findall(r"^#{2,3} (\d+(?:\.\d+)?)\.? ", TEXT, flags=re.M))
    own = [ln for ln in TEXT.splitlines() if not re.search(r"Phase 2|개발 초안|rev6|PHASE2-DETECTOR", ln)]       # 다른 문서의 절을 가리키는 줄은 제외
    for ref in set(re.findall(r"(?<![A-Za-z0-9])§(\d+(?:\.\d+)?)", chr(10).join(own))):
        assert ref in sections or ref.split(".")[0] in sections, ref


def test_phase2_preregistration_is_not_modified_by_this_environment():
    p2 = nc.ROOT / pc.PREREG_DOC
    assert p2.is_file() and pc.text_sha256(p2) == pc.PREREG_SHA256                  # Phase 2 사전등록은 동결 해시 그대로
