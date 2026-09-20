#!/usr/bin/env python3
"""§76 v3.2 검증 - stop_loss.py의 순수 판정과 train.py의 session 목록
구성(v3.1 재사용/신규 calibration·holdout 분리)을 확인한다. 클러스터
의존 없음."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "model_v31"))
sys.path.insert(0, str(Path(__file__).parent))  # model_v32가 우선하도록 마지막에 삽입(train.py 이름 충돌 방지)
sys.stdout.reconfigure(encoding="utf-8")

from stop_loss import decide_holdout_outcome
from train_v32 import CALIBRATION_SESSIONS, HOLDOUT_SESSIONS, MODEL_PARAMS, TRAIN_SESSIONS

_CLEAN_REPORT = {"overall_false_signal_episodes": 0,
                  "adoption_criteria": {"no_missing_or_nan": True, "schema_and_hash_consistent": True}}


def test_zero_episodes_adopts_model():
    decision = decide_holdout_outcome(_CLEAN_REPORT)
    assert decision["outcome"] == "adopted"
    assert decision["adopt_model"] is True and decision["proceed_to_challenge"] is True
    print("OK - false signal episode 0이면 채택되고 challenge 진행 허용")


def test_one_episode_triggers_stop_loss():
    report = {**_CLEAN_REPORT, "overall_false_signal_episodes": 1}
    decision = decide_holdout_outcome(report)
    assert decision["outcome"] == "stop_loss_rejected"
    assert decision["adopt_model"] is False and decision["proceed_to_challenge"] is False
    print("OK - false signal episode 1건만으로도 즉시 stop-loss(반복 재시도 없음)")


def test_many_episodes_still_stop_loss_not_worse():
    """episode 수가 많다고 다른 결과 카테고리로 안 새며, 여전히 단일
    stop_loss_rejected 결과다(등급을 나누지 않음 - 지시에 없는 구분 추가 안 함)."""
    report = {**_CLEAN_REPORT, "overall_false_signal_episodes": 5}
    decision = decide_holdout_outcome(report)
    assert decision["outcome"] == "stop_loss_rejected"
    print("OK - episode 수와 무관하게 1건 이상이면 동일하게 stop_loss_rejected")


def test_invalid_schema_blocks_adoption_even_with_zero_episodes():
    report = {"overall_false_signal_episodes": 0,
              "adoption_criteria": {"no_missing_or_nan": True, "schema_and_hash_consistent": False}}
    decision = decide_holdout_outcome(report)
    assert decision["outcome"] == "invalid_evaluation"
    assert decision["adopt_model"] is False
    print("OK - episode 0이어도 schema/hash 불일치면 채택 안 함")


def test_v32_training_reuses_all_six_v31_sessions():
    assert len(TRAIN_SESSIONS) == 6
    idle_count = sum(1 for s in TRAIN_SESSIONS if "idle" in s)
    low_load_count = sum(1 for s in TRAIN_SESSIONS if "low_load" in s)
    assert idle_count == 3 and low_load_count == 3
    print("OK - v3.2 Training은 v3.1의 idle 3세션+low_load 3세션 전부(6개)")


def test_v32_calibration_and_holdout_are_new_and_disjoint_from_training():
    assert len(CALIBRATION_SESSIONS) == 6 and len(HOLDOUT_SESSIONS) == 6
    train_set, calib_set, holdout_set = set(TRAIN_SESSIONS), set(CALIBRATION_SESSIONS), set(HOLDOUT_SESSIONS)
    assert not (train_set & calib_set), "v3.2 train/calibration 세션 중복"
    assert not (train_set & holdout_set), "v3.2 train/holdout 세션 중복"
    assert not (calib_set & holdout_set), "v3.2 calibration/holdout 세션 중복"
    # 신규 calibration/holdout은 v3.1 세션 이름 패턴과도 겹치지 않아야 한다(완전히 새 수집)
    assert all(not sid.startswith("v31-") for sid in calib_set | holdout_set)
    print("OK - v3.2 calibration/holdout은 v3.1 세션과 겹치지 않는 신규 수집")


def test_v32_model_params_match_v31_and_v1():
    assert MODEL_PARAMS["n_estimators"] == 100
    assert MODEL_PARAMS["contamination"] == "auto"
    assert MODEL_PARAMS["random_state"] == 42
    print("OK - v3.2도 v1/v3.1과 동일 하이퍼파라미터(탐색 없음)")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
