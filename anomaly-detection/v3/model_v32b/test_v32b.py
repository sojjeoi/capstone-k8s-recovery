#!/usr/bin/env python3
"""§79 v3.2b 검증 - session 목록 구성과 domain 분리가 지시대로 됐는지
확인. 클러스터 의존 없음."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "model_v32"))
sys.stdout.reconfigure(encoding="utf-8")

from stop_loss import decide_holdout_outcome  # model_v32 - 재사용 확인용
from train_v32b import CALIBRATION_SESSIONS, HOLDOUT_SESSIONS, MODEL_PARAMS, TRAIN_SESSIONS


def test_v32b_training_includes_slo_violating_session():
    """§79.1 핵심 - t_slo가 있는 calib2-low-02도 infrastructure-normal이면
    Training에 포함돼야 한다(v3.2에서는 아예 boundary_challenge_set으로
    제외됐던 것과 대비)."""
    assert "calib2-low-02" in TRAIN_SESSIONS
    print("OK - infrastructure-normal이면서 t_slo가 있는 세션도 v3.2b Training에 포함됨")


def test_v32b_training_has_nine_sessions_correct_regime_split():
    assert len(TRAIN_SESSIONS) == 9
    idle_count = sum(1 for s in TRAIN_SESSIONS if "idle" in s)
    low_load_count = sum(1 for s in TRAIN_SESSIONS if "low" in s and "idle" not in s)
    assert idle_count == 4 and low_load_count == 5
    print("OK - v3.2b Training은 idle 4개+low_load 5개=9개(infrastructure-normal 전부)")


def test_v32b_calibration_and_holdout_are_new_sessions_not_reused():
    assert len(CALIBRATION_SESSIONS) == 6 and len(HOLDOUT_SESSIONS) == 6
    reused_prefixes = ("v31-", "calib2-", "official-", "q3c-")
    assert all(not sid.startswith(reused_prefixes) for sid in CALIBRATION_SESSIONS + HOLDOUT_SESSIONS)
    print("OK - v3.2b calibration/holdout은 전부 신규(calib3-/holdout3-), 기존 재사용 없음")


def test_v32b_splits_are_disjoint():
    train_set, calib_set, holdout_set = set(TRAIN_SESSIONS), set(CALIBRATION_SESSIONS), set(HOLDOUT_SESSIONS)
    assert not (train_set & calib_set)
    assert not (train_set & holdout_set)
    assert not (calib_set & holdout_set)
    print("OK - v3.2b train/calibration/holdout 세션이 서로 겹치지 않음")


def test_v32b_model_params_unchanged():
    assert MODEL_PARAMS["n_estimators"] == 100
    assert MODEL_PARAMS["contamination"] == "auto"
    assert MODEL_PARAMS["random_state"] == 42
    print("OK - v3.2b도 v1/v3.1/v3.2와 동일 하이퍼파라미터")


def test_stop_loss_rule_reused_unchanged_from_v32():
    """§79.8 - stop-loss 판정 함수 자체를 model_v32에서 그대로 재사용
    한다(복제 없음) - 여전히 episode 1건이면 무조건 거부."""
    report = {"overall_false_signal_episodes": 1, "adoption_criteria": {"no_missing_or_nan": True, "schema_and_hash_consistent": True}}
    decision = decide_holdout_outcome(report)
    assert decision["outcome"] == "stop_loss_rejected"
    print("OK - v3.2의 stop_loss 규칙을 그대로 재사용(1건이라도 거부)")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
