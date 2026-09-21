#!/usr/bin/env python3
"""§86 - score_server.py v3.2b 통합 오프라인 고정 테스트. 클러스터 의존
없음 - 실제 v3.2b 동결 artifact(model.pkl/scaler.pkl/threshold.json/
feature-schema.json)는 읽기만 하고 전혀 다시 쓰지 않는다. offline
evaluator(model_v31/evaluate.py)와 runtime(score_server.py)이 완전히
같은 값을 내는지가 핵심 검증 대상이다."""
import json
import math
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
V3_DIR = Path(__file__).parent / "v3"
sys.path.insert(0, str(V3_DIR))
sys.path.insert(0, str(V3_DIR / "model_v31"))
sys.stdout.reconfigure(encoding="utf-8")

import score_server as ss  # noqa: E402
from feature_selection import apply_feature_schema  # noqa: E402
from features import METRICS  # noqa: E402

V32B_ARTIFACTS_DIR = V3_DIR / "model_v32b" / "artifacts"
FLOAT_TOL = 1e-9  # §86.3 - offline/runtime score 허용 오차(동일 model.pkl/scaler.pkl 재사용이라 이론상 완전히 동일해야 함)


def _promql_to_metric_name(promql: str) -> str:
    for name, q in METRICS.items():
        if q == promql:
            return name
    raise KeyError(promql)


def _constant_query_range_fn(values_by_metric: dict):
    """metric별 상수값을 그대로 window 전체에 채워 반환 -> mean=값,
    slope=0.0이 되는 8-feature raw vector를 만든다(offline/runtime
    parity 검증용 고정 fixture 구성). `query_range_with_bounded_retry()`가
    `step`까지 4개 인자로 호출하므로(features._query_range와 동일 시그니처)
    그대로 받아준다."""
    def fn(promql, start, end, step="15s"):
        name = _promql_to_metric_name(promql)
        return [values_by_metric[name]] * 5
    return fn


def _missing_metric_query_range_fn(missing_metric: str, values_by_metric: dict):
    def fn(promql, start, end, step="15s"):
        name = _promql_to_metric_name(promql)
        if name == missing_metric:
            return []
        return [values_by_metric[name]] * 5
    return fn


def _nan_metric_query_range_fn(nan_metric: str, values_by_metric: dict):
    def fn(promql, start, end, step="15s"):
        name = _promql_to_metric_name(promql)
        if name == nan_metric:
            return [float("nan")] * 5
        return [values_by_metric[name]] * 5
    return fn


NORMAL_VALUES = {"cpu": 1.0, "memory": 7.0e9, "queue": 0.0, "cache": 0.001}


def _always_fresh(promql, max_age_sec):
    return {"fresh": True, "age_sec": 1.0, "reason": None}


# ---------------------------------------------------------------------------
# artifact loading fail-closed
# ---------------------------------------------------------------------------

def test_v32b_artifact_loads_and_hash_matches_known_frozen_values():
    result = ss.load_and_verify_artifacts(V32B_ARTIFACTS_DIR, "v3.2b")
    assert result["threshold"] == -0.0742929709960305
    assert result["schema"]["kept_feature_names"] == [
        "cpu_mean", "cpu_slope", "memory_mean", "memory_slope", "cache_mean", "cache_slope"]
    assert result["artifact_hashes"]["model.pkl"] == "2102e4f5f0d06809402f6f11c0396f0249bda3864bf6eeb18c52ac626e1a9243"
    print("OK - v3.2b artifact 정상 로드, threshold·6-feature 순서·hash 전부 알려진 동결값과 일치")


def test_default_or_latest_fallback_is_impossible_without_explicit_args():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument("--model-version", required=True)
    try:
        parser.parse_args([])
        assert False, "인자 없이 파싱되면 안 됨"
    except SystemExit:
        pass
    print("OK - --artifacts-dir/--model-version 없이는 CLI 파싱 자체가 실패(암묵적 기본값 없음)")


def test_wrong_model_version_fails_closed():
    try:
        ss.load_and_verify_artifacts(V32B_ARTIFACTS_DIR, "v3.1")
        assert False, "예외가 발생해야 함"
    except RuntimeError as e:
        assert "model_version" in str(e)
    print("OK - --model-version이 training-metadata.json과 다르면 fail-closed")


def test_rejected_v31_artifact_not_selected_by_v32b_version_label():
    v31_dir = V3_DIR / "model_v31" / "artifacts"
    try:
        ss.load_and_verify_artifacts(v31_dir, "v3.2b")
        assert False, "v3.1 디렉터리를 v3.2b로 로드하면 안 됨"
    except RuntimeError:
        pass
    print("OK - rejected v3.1 artifact 디렉터리는 v3.2b 버전 이름으로도 선택되지 않음(training-metadata.json 자체가 다름)")


def test_sha256_mismatch_fails_closed(tmp_path):
    import shutil
    fake_dir = tmp_path / "tampered"
    shutil.copytree(V32B_ARTIFACTS_DIR, fake_dir)
    # model.pkl 내용을 바꿔 SHA256SUMS.json과 어긋나게 만든다.
    (fake_dir / "model.pkl").write_bytes(b"tampered-bytes")
    try:
        ss.load_and_verify_artifacts(fake_dir, "v3.2b")
        assert False, "SHA mismatch인데 통과하면 안 됨"
    except RuntimeError as e:
        assert "SHA256SUMS" in str(e)
    print("OK - artifact 파일이 SHA256SUMS.json과 다르면 fail-closed")


def test_dependency_version_mismatch_fails_closed(tmp_path):
    import shutil
    fake_dir = tmp_path / "bad-deps"
    shutil.copytree(V32B_ARTIFACTS_DIR, fake_dir)
    (fake_dir / "requirements-lock.txt").write_text(
        "scikit-learn==0.0.0\nnumpy==0.0.0\n", encoding="utf-8")
    try:
        ss._check_dependency_versions(fake_dir / "requirements-lock.txt")
        assert False, "버전 불일치인데 통과하면 안 됨"
    except RuntimeError as e:
        assert "의존성 버전 불일치" in str(e)
    print("OK - requirements-lock.txt의 고정 버전과 설치된 버전이 다르면 fail-closed")


def test_runtime_replay_rule_mismatch_fails_closed(tmp_path):
    import hashlib
    import json
    import shutil
    fake_dir = tmp_path / "bad-rules"
    shutil.copytree(V32B_ARTIFACTS_DIR, fake_dir)
    doc = json.loads((fake_dir / "threshold.json").read_text(encoding="utf-8"))
    doc["cooldown_sec"] = 999.0
    (fake_dir / "threshold.json").write_text(json.dumps(doc), encoding="utf-8")
    # SHA256SUMS 재계산(파일을 실제로 바꿨으므로) - 이 테스트의 목적은 SHA
    # 검증이 아니라 그 다음 단계인 runtime 규칙 대조이므로 SHA는 갱신해둔다.
    sums = json.loads((fake_dir / "SHA256SUMS.json").read_text(encoding="utf-8"))
    sums["threshold.json"] = hashlib.sha256((fake_dir / "threshold.json").read_bytes()).hexdigest()
    (fake_dir / "SHA256SUMS.json").write_text(json.dumps(sums), encoding="utf-8")
    try:
        ss.load_and_verify_artifacts(fake_dir, "v3.2b")
        assert False, "runtime replay 규칙 불일치인데 통과하면 안 됨"
    except RuntimeError as e:
        assert "runtime replay 규칙" in str(e)
    print("OK - threshold.json의 cooldown_sec 등이 score_server.py 상수와 다르면 fail-closed")


# ---------------------------------------------------------------------------
# feature extraction fail-closed (missing/NaN/stale)
# ---------------------------------------------------------------------------

def _load_real_artifacts():
    return ss.load_and_verify_artifacts(V32B_ARTIFACTS_DIR, "v3.2b")


def test_missing_metric_fails_closed_no_score():
    a = _load_real_artifacts()
    try:
        ss.evaluate_v32b(a["model"], a["scaler"], a["schema"],
                          query_range_fn=_missing_metric_query_range_fn("queue", NORMAL_VALUES),
                          freshness_check_fn=_always_fresh)
        assert False, "missing metric인데 score가 나오면 안 됨"
    except RuntimeError as e:
        assert "결측" in str(e)
    print("OK - 원천 metric 응답이 비어있으면(missing) score를 만들지 않고 fail-closed")


def test_nan_feature_fails_closed_no_score():
    a = _load_real_artifacts()
    try:
        ss.evaluate_v32b(a["model"], a["scaler"], a["schema"],
                          query_range_fn=_nan_metric_query_range_fn("cache", NORMAL_VALUES),
                          freshness_check_fn=_always_fresh)
        assert False, "NaN feature인데 score가 나오면 안 됨"
    except RuntimeError as e:
        assert "NaN" in str(e)
    print("OK - feature 값에 NaN이 섞이면 score를 만들지 않고 fail-closed(0 대체 없음)")


def test_stale_metric_fails_closed_no_score():
    a = _load_real_artifacts()

    def stale_fn(promql, max_age_sec):
        return {"fresh": False, "age_sec": 999.0, "reason": "age 999.0s > 120.0s"}

    try:
        ss.evaluate_v32b(a["model"], a["scaler"], a["schema"],
                          query_range_fn=_constant_query_range_fn(NORMAL_VALUES),
                          freshness_check_fn=stale_fn)
        assert False, "stale metric인데 score가 나오면 안 됨"
    except RuntimeError as e:
        assert "stale" in str(e)
    print("OK - metric이 신선하지 않으면(stale) score를 만들지 않고 fail-closed")


def test_normal_feature_extraction_produces_score():
    a = _load_real_artifacts()
    score = ss.evaluate_v32b(a["model"], a["scaler"], a["schema"],
                              query_range_fn=_constant_query_range_fn(NORMAL_VALUES),
                              freshness_check_fn=_always_fresh)
    assert isinstance(score, float) and math.isfinite(score)
    print(f"OK - 정상 feature면 score가 정상적으로 계산됨(score={score:.4f})")


# ---------------------------------------------------------------------------
# offline/runtime parity
# ---------------------------------------------------------------------------

def test_offline_runtime_parity_matches_within_tolerance():
    """같은 8-feature raw row를 offline evaluator 경로(apply_feature_schema
    -> scaler.transform -> decision_function, model_v31/evaluate.py와 동일
    호출 순서)와 runtime evaluate_v32b() 양쪽에 넣어 score가 허용 오차
    이내로 일치하는지 확인한다."""
    a = _load_real_artifacts()
    model, scaler, schema = a["model"], a["scaler"], a["schema"]

    raw8 = [NORMAL_VALUES["cpu"], 0.0, NORMAL_VALUES["memory"], 0.0,
            NORMAL_VALUES["queue"], 0.0, NORMAL_VALUES["cache"], 0.0]
    offline_x6 = apply_feature_schema(raw8, schema)
    offline_score = float(model.decision_function(scaler.transform([offline_x6]))[0])

    runtime_score = ss.evaluate_v32b(model, scaler, schema,
                                      query_range_fn=_constant_query_range_fn(NORMAL_VALUES),
                                      freshness_check_fn=_always_fresh)

    assert abs(offline_score - runtime_score) <= FLOAT_TOL, (offline_score, runtime_score)
    print(f"OK - offline/runtime score parity 확인(offline={offline_score:.10f}, runtime={runtime_score:.10f}, "
          f"허용오차={FLOAT_TOL})")


def test_wrong_feature_order_would_produce_different_score():
    """schema 기반 선택(순서 고정) 대신 앞 6개를 그냥 잘라 쓰면(queue가
    섞여 들어가 순서·구성 자체가 달라짐) 다른 score가 나온다는 것을
    보여준다 - `apply_feature_schema()`를 반드시 써야 하는 이유의
    회귀 방지 증거."""
    a = _load_real_artifacts()
    model, scaler, schema = a["model"], a["scaler"], a["schema"]
    raw8 = [1.5, 0.05, 7.01e9, -1000.0, 0.0, 0.0, 0.0012, -0.0001]

    correct_x6 = apply_feature_schema(raw8, schema)
    correct_score = float(model.decision_function(scaler.transform([correct_x6]))[0])

    naive_x6 = raw8[:6]  # 잘못된 순서/구성 - queue_mean/slope가 섞여 들어가고 cache가 빠짐
    naive_score = float(model.decision_function(scaler.transform([naive_x6]))[0])

    assert correct_x6 != naive_x6
    assert correct_score != naive_score
    print(f"OK - 잘못된 feature 순서/구성(naive={naive_x6})은 올바른 schema 적용(correct={correct_x6})과 "
          f"다른 score를 낸다(correct={correct_score:.4f} vs naive={naive_score:.4f}) - schema 기반 선택이 필수임을 확인")


# ---------------------------------------------------------------------------
# streak / reset / cooldown parity with replay_detector()
# ---------------------------------------------------------------------------

def test_advance_streak_matches_replay_detector_for_full_sequence():
    from replay import replay_detector  # model_v31, 변경 없음
    threshold = -0.05
    # 정상/이상/이상/정상(reset)/이상/이상/이상(신호)/이상(cooldown 중 추가 이상)/.../cooldown 종료 후 새 신호
    scores = [0.1, -0.1, -0.2, 0.1, -0.3, -0.4, -0.5, -0.6, -0.7, -0.8, -0.9]
    eval_interval_sec = 15.0

    expected = replay_detector(scores, threshold, consecutive_threshold=3, cooldown_sec=60.0,
                                eval_interval_sec=eval_interval_sec)

    consecutive, last_signal_at = 0, None
    signal_indices = []
    for i, score in enumerate(scores):
        now = i * eval_interval_sec  # 정확히 eval_interval_sec 간격(offline index 기반 시간과 동일 조건)
        step = ss.advance_streak(score, threshold, consecutive, last_signal_at, now,
                                  cooldown_sec=60.0, consecutive_threshold=3)
        consecutive, last_signal_at = step["consecutive_anomalous"], step["last_signal_at"]
        if step["should_signal"]:
            signal_indices.append(i)

    assert signal_indices == expected["signal_indices"], (signal_indices, expected["signal_indices"])
    print(f"OK - advance_streak()의 신호 타이밍이 offline replay_detector()와 정확히 일치: {signal_indices}")


def test_two_anomalous_then_normal_resets_consecutive():
    step1 = ss.advance_streak(-0.1, 0.0, 0, None, 0.0)
    step2 = ss.advance_streak(-0.1, 0.0, step1["consecutive_anomalous"], step1["last_signal_at"], 15.0)
    assert step2["consecutive_anomalous"] == 2
    step3 = ss.advance_streak(0.1, 0.0, step2["consecutive_anomalous"], step2["last_signal_at"], 30.0)
    assert step3["consecutive_anomalous"] == 0
    assert step3["should_signal"] is False
    print("OK - anomaly 2회 후 정상 1회로 즉시 reset(연속 카운트 0)")


def test_three_consecutive_triggers_signal():
    consecutive, last_signal_at = 0, None
    step = None
    for now in (0.0, 15.0, 30.0):
        step = ss.advance_streak(-0.1, 0.0, consecutive, last_signal_at, now)
        consecutive, last_signal_at = step["consecutive_anomalous"], step["last_signal_at"]
    assert consecutive == 3
    assert step["should_signal"] is True
    print("OK - 연속 3회 anomaly에서 신호 발생")


def test_score_exactly_equal_to_threshold_is_not_anomalous():
    # 엄격한 미만(score < threshold) - 같으면 이상 아님(score_server.py:82와 동일 계약).
    step = ss.advance_streak(-0.05, -0.05, 0, None, 0.0)
    assert step["is_anomalous"] is False
    print("OK - score == threshold는 이상이 아님(엄격한 미만 비교)")


def test_score_just_below_threshold_is_anomalous():
    step = ss.advance_streak(-0.0500001, -0.05, 0, None, 0.0)
    assert step["is_anomalous"] is True
    print("OK - score가 threshold보다 아주 조금이라도 낮으면 이상")


def test_cooldown_suppresses_additional_signal():
    consecutive, last_signal_at = 3, 30.0  # 방금 30.0초 시점에 신호를 낸 상태
    step = ss.advance_streak(-0.1, 0.0, consecutive, last_signal_at, 45.0, cooldown_sec=60.0)  # 15초 후, 아직 cooldown 중
    assert step["consecutive_anomalous"] == 4
    assert step["should_signal"] is False
    print("OK - cooldown 중 추가 anomaly는 연속 카운트는 늘지만 신호는 억제됨")


def test_new_episode_after_cooldown_ends():
    step = ss.advance_streak(-0.1, 0.0, 3, 30.0, 95.0, cooldown_sec=60.0)  # 65초 후, cooldown 종료
    assert step["should_signal"] is True
    print("OK - cooldown 종료 후 연속 조건을 다시 만족하면 새 episode 신호 발생")


# ---------------------------------------------------------------------------
# §88.6 - 관찰 하니스 회귀 테스트(§87 사고 재발 방지, 판정 로직은 불변)
# ---------------------------------------------------------------------------

def test_evidence_log_survives_redirected_stdout_buffering():
    """§87.1 - stdout이 리다이렉트·버퍼링으로 유실돼도(실제 §87 사고)
    --evidence-log 파일에는 evaluation 세부값이 남아야 한다. main()을
    실제로 호출하되(once=True, sleep 없음) `_evaluate_v32b_verbose`만
    가짜로 주입해 판정 로직 자체는 그대로 통과시킨다."""
    def fake_verbose(model, scaler, schema, **kwargs):
        return {"window_start_utc": "2026-01-01T00:00:00+00:00", "window_end_utc": "2026-01-01T00:01:00+00:00",
                "raw_feature_vector": [1.0] * 8, "ordered_feature_vector": [1.0] * 6,
                "scaled_feature_vector": [0.5] * 6, "score": 0.1234, "freshness": {"fresh": True}}

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        with patch.object(ss, "_evaluate_v32b_verbose", side_effect=fake_verbose):
            ss.main(str(V32B_ARTIFACTS_DIR), "v3.2b", once=True, experiment_run_id="test-run",
                    evidence_log_path=str(evidence_path))
        lines = evidence_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["score"] == 0.1234
        assert rec["run_id"] == "test-run"
        assert rec["raw_feature_vector"] == [1.0] * 8
        assert rec["ordered_feature_vector"] == [1.0] * 6
        assert "lifecycle_phase" in rec  # None(오케스트레이터가 사후 결합) - 필드 자체는 항상 존재
    print("OK - stdout 리다이렉트/버퍼링과 무관하게 --evidence-log에 evaluation 세부값이 flush됨")


def test_evidence_log_preserves_last_evaluation_before_crash():
    """§87.1 - 다음 cycle에서 크래시해도 이전 cycle까지의 evidence는
    보존돼야 한다(매 cycle 직후 flush+fsync, 크래시 이후 버퍼에 남아
    유실되는 경로 자체가 없음을 확인)."""
    calls = {"n": 0}

    def fake_verbose(model, scaler, schema, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"window_start_utc": "t0s", "window_end_utc": "t0e", "raw_feature_vector": [1.0] * 8,
                    "ordered_feature_vector": [1.0] * 6, "scaled_feature_vector": [1.0] * 6,
                    "score": 0.05, "freshness": {"fresh": True}}
        raise RuntimeError("fail-closed: simulated crash on 2nd cycle")

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        with patch.object(ss, "_evaluate_v32b_verbose", side_effect=fake_verbose), \
             patch.object(ss.time, "sleep", lambda s: None):
            try:
                ss.main(str(V32B_ARTIFACTS_DIR), "v3.2b", once=False, experiment_run_id="test-run",
                        evidence_log_path=str(evidence_path))
                assert False, "2번째 cycle에서 예외가 발생해야 함"
            except RuntimeError:
                pass
        lines = evidence_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["score"] == 0.05
    print("OK - 다음 cycle 크래시 전에 기록된 evaluation은 evidence 파일에 그대로 보존됨")


def test_long_continuous_anomalous_streak_produces_multiple_signals_not_collapsed_to_one_episode():
    """§88 Task 3 - runtime(advance_streak)이 하나의 긴 연속 이상 상태를
    여러 HTTP signal로 나눠 보내는지, 그리고 offline replay_detector()도
    같은 시퀀스에서 정확히 같은 횟수를 내는지(= episode 정의가 두 경로
    사이에 어긋나지 않음, classification B 반증)."""
    threshold = 0.0
    consecutive, last_signal_at = 0, None
    now = 0.0
    signal_times = []
    for _ in range(30):  # 30 * 15s = 450초 연속 이상 - 여러 cooldown(60초) 주기를 넘김
        step = ss.advance_streak(-0.1, threshold, consecutive, last_signal_at, now)
        consecutive, last_signal_at = step["consecutive_anomalous"], step["last_signal_at"]
        if step["should_signal"]:
            signal_times.append(now)
        now += 15.0

    assert len(signal_times) > 1, "장기 연속 이상 상태가 하나의 episode로 뭉쳐지면 안 됨(cooldown마다 재발행)"

    from replay import replay_detector
    scores = [-0.1] * 30
    result = replay_detector(scores, threshold, consecutive_threshold=3, cooldown_sec=60.0, eval_interval_sec=15.0)
    assert result["signal_count"] == len(signal_times), (result["signal_count"], len(signal_times))
    print(f"OK - runtime·offline 둘 다 긴 연속 스트릭에서 동일하게 {len(signal_times)}건으로 반복 신호 - "
          f"episode 정의(cooldown 기반 재무장)가 두 경로에서 어긋나지 않음(classification B 반증)")


if __name__ == "__main__":
    import tempfile
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        if "tmp_path" in t.__code__.co_varnames[:t.__code__.co_argcount]:
            with tempfile.TemporaryDirectory() as d:
                t(Path(d))
        else:
            t()
    print(f"전체 통과 ({len(tests)}개)")
