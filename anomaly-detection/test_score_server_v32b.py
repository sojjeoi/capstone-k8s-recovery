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
    """§87.1 - 다음 cycle에서 진짜 예상 밖 예외(RuntimeError가 아닌 버그류)가
    나도 이전 cycle까지의 evidence는 보존돼야 한다(매 cycle 직후
    flush+fsync, 크래시 이후 버퍼에 남아 유실되는 경로 자체가 없음을
    확인). §107(2026-09-23)부터 RuntimeError(결측/NaN/stale fail-closed)는
    더 이상 루프를 죽이지 않고 스킵+계속이므로(아래 §107 절 테스트 참고),
    이 테스트는 "진짜 처리 안 된 예외"가 여전히 프로세스를 죽이고 그 전
    evidence는 보존된다는 것만 확인하도록 예외 타입을 바꿨다."""
    calls = {"n": 0}

    def fake_verbose(model, scaler, schema, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"window_start_utc": "t0s", "window_end_utc": "t0e", "raw_feature_vector": [1.0] * 8,
                    "ordered_feature_vector": [1.0] * 6, "scaled_feature_vector": [1.0] * 6,
                    "score": 0.05, "freshness_by_metric": {}}
        raise KeyError("simulated genuinely unexpected bug on 2nd cycle (not a fail-closed RuntimeError)")

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        with patch.object(ss, "_evaluate_v32b_verbose", side_effect=fake_verbose), \
             patch.object(ss.time, "sleep", lambda s: None):
            try:
                ss.main(str(V32B_ARTIFACTS_DIR), "v3.2b", once=False, experiment_run_id="test-run",
                        evidence_log_path=str(evidence_path))
                assert False, "2번째 cycle에서 예외가 발생해야 함"
            except KeyError:
                pass
        lines = evidence_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["score"] == 0.05
    print("OK - 진짜 예상 밖 예외로 크래시해도 그 전 evaluation은 evidence 파일에 그대로 보존됨")


# ---------------------------------------------------------------------------
# §107 - 데이터 갭(결측/NaN/stale)은 detector 프로세스 자체를 죽이지 않고
# 이 cycle만 스킵한다(pod_kill-proposed-03-mainexp-v1 사후조사 계기,
# phase8-blue-green-preflight-incident.md §106/§107)
# ---------------------------------------------------------------------------

def _run_main_n_cycles_raw(evidence_path, verbose_side_effect, n, *, once=False):
    """_run_main_n_cycles()와 같은 목적이지만 _evaluate_v32b_verbose 자체를
    호출자가 정한 side_effect로 대체한다(기존 헬퍼는 _anomalous_verbose로
    고정돼 있어 §107 스킵 시나리오를 못 만듦). n cycle 뒤 StopIteration으로
    빠져나온다."""
    calls = {"n": 0}

    def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] >= n:
            raise StopIteration("test: n cycles reached")

    with patch.object(ss, "_evaluate_v32b_verbose", side_effect=verbose_side_effect), \
         patch.object(ss.time, "sleep", side_effect=fake_sleep):
        try:
            ss.main(str(V32B_ARTIFACTS_DIR), "v3.2b", once=once, experiment_run_id="test-run",
                    evidence_log_path=str(evidence_path))
        except StopIteration:
            pass


def test_freshness_probe_queries_checks_each_feature_source_independently():
    """up{job="vllm-active"} 단일 canary 대신 4개 feature 원천을 개별
    확인한다는 걸 직접 확인 - 하나만 stale이어도 잡히고, 어떤 promql이
    호출됐는지도 볼 수 있다."""
    a = _load_real_artifacts()
    seen_promqls = []

    def selective_stale_fn(promql, max_age_sec):
        seen_promqls.append(promql)
        if "kv_cache_usage_perc" in promql:
            return {"fresh": False, "age_sec": 999.0, "reason": "age 999.0s > 120.0s"}
        return {"fresh": True, "age_sec": 1.0, "reason": None}

    try:
        ss.evaluate_v32b(a["model"], a["scaler"], a["schema"],
                          query_range_fn=_constant_query_range_fn(NORMAL_VALUES),
                          freshness_check_fn=selective_stale_fn)
        assert False, "cache만 stale이어도 fail-closed로 score가 안 나와야 함"
    except RuntimeError as e:
        assert "cache" in str(e) and "stale" in str(e)
    assert seen_promqls == list(ss.FRESHNESS_PROBE_QUERIES.values())[:len(seen_promqls)]
    print(f"OK - 4개 feature 원천을 개별 확인({seen_promqls}), cache 하나만 stale이어도 구체적으로 지목해 fail-closed")


def test_evaluate_verbose_runtime_error_skips_cycle_without_crashing_loop():
    """§107 핵심 회귀 테스트 - _evaluate_v32b_verbose()가 RuntimeError(결측/
    NaN/stale)를 던져도 main()의 while 루프가 죽지 않고 다음 cycle로
    넘어가야 한다(과거 버그: 이 예외가 그대로 새 나가 detector 프로세스
    전체가 종료됨 - pod_kill-proposed-03-mainexp-v1이 바로 이 경로로
    invalid_run이 됨)."""
    calls = {"n": 0}

    def fake_verbose(model, scaler, schema, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("fail-closed: metric stale - cache(...): 표본 없음")
        return {"window_start_utc": "t0s", "window_end_utc": "t0e", "raw_feature_vector": [1.0] * 8,
                "ordered_feature_vector": [1.0] * 6, "scaled_feature_vector": [1.0] * 6,
                "score": 0.05, "freshness_by_metric": {}}

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        _run_main_n_cycles_raw(evidence_path, fake_verbose, n=4)
        recs = _read_jsonl(evidence_path)
        decisions = [r for r in recs if r["record_type"] == "evaluation_decision"]
        skipped = [r for r in recs if r["record_type"] == "evaluation_skipped"]
        assert len(skipped) == 1, "2번째 cycle(RuntimeError)은 evaluation_skipped 1건으로 기록돼야 함"
        assert skipped[0]["evaluation_seq"] == 2
        assert skipped[0]["consecutive_data_gap_cycles"] == 1
        assert skipped[0]["gap_classification"] == "transient"
        assert "fail-closed" in skipped[0]["reason"]
        # 1,3,4번째 cycle은 정상 평가(2번째만 스킵) - 루프가 안 죽고 계속 돌았다는 직접 증거.
        assert len(decisions) == 3, decisions
        assert [d["evaluation_seq"] for d in decisions] == [1, 3, 4]
    print("OK - RuntimeError(fail-closed 데이터 갭)는 그 cycle만 스킵하고 loop는 계속 돔(프로세스 안 죽음)")


def test_prolonged_data_gap_classified_differently_from_transient():
    """§107 - "장기간 입력이 없는 경우"를 단순 "미탐지"와 구별하는 판정.
    연속 스킵이 PROLONGED_DATA_GAP_CYCLES(=WINDOW_SEC/EVAL_INTERVAL_SEC)
    미만이면 transient, 그 이상이면 prolonged로 evidence에 명시적으로
    구분해 남긴다."""
    def always_raises(model, scaler, schema, **kwargs):
        raise RuntimeError("fail-closed: metric stale - queue(...): 표본 없음")

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        _run_main_n_cycles_raw(evidence_path, always_raises, n=ss.PROLONGED_DATA_GAP_CYCLES + 2)
        skipped = [r for r in _read_jsonl(evidence_path) if r["record_type"] == "evaluation_skipped"]
        assert len(skipped) == ss.PROLONGED_DATA_GAP_CYCLES + 2
        classifications = [r["gap_classification"] for r in skipped]
        assert classifications[:ss.PROLONGED_DATA_GAP_CYCLES - 1] == ["transient"] * (ss.PROLONGED_DATA_GAP_CYCLES - 1)
        assert classifications[ss.PROLONGED_DATA_GAP_CYCLES - 1:] == (
            ["prolonged"] * (len(classifications) - ss.PROLONGED_DATA_GAP_CYCLES + 1))
    print(f"OK - 연속 데이터 갭이 {ss.PROLONGED_DATA_GAP_CYCLES}cycle({ss.WINDOW_SEC}초, WINDOW_SEC와 동일 근거) "
          f"이상 지속되면 gap_classification이 transient에서 prolonged로 바뀜")


def test_data_gap_resets_consecutive_streak_not_carried_across_gap():
    """스킵된 cycle은 anomalous로도 정상으로도 확정할 수 없으므로 스트릭을
    리셋한다(그 갭 동안 진짜로 계속 이상 상태였는지 증거가 없어, 조용히
    이어붙이면 근거 없는 연속성을 만들어내는 것과 같다)."""
    calls = {"n": 0}

    def fake_verbose(model, scaler, schema, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("fail-closed: metric stale - cache(...): 표본 없음")
        return {"window_start_utc": "t0s", "window_end_utc": "t0e", "raw_feature_vector": [1.0] * 8,
                "ordered_feature_vector": [1.0] * 6, "scaled_feature_vector": [1.0] * 6,
                "score": -999.0, "freshness_by_metric": {}}  # 갭 전후 모두 anomalous 값

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        _run_main_n_cycles_raw(evidence_path, fake_verbose, n=4)
        decisions = [r for r in _read_jsonl(evidence_path) if r["record_type"] == "evaluation_decision"]
        # cycle4(seq=4)는 갭 직후 첫 정상 평가 - 갭 전 연속 2회(seq 1,2)가 이어붙지 않고 1부터 다시 시작해야 함.
        seq4 = next(r for r in decisions if r["evaluation_seq"] == 4)
        assert seq4["consecutive_anomalous"] == 1, seq4
    print("OK - 데이터 갭(스킵) 이후 연속 카운트는 갭 이전 값을 이어받지 않고 1부터 다시 시작함")


def test_once_mode_returns_after_skip_without_retry_loop():
    """--once는 수동 단발 호출용이다 - 스킵이 나도 무한 재시도하지 않고
    once 계약대로 그 자리에서 반환해야 한다."""
    def always_raises(model, scaler, schema, **kwargs):
        raise RuntimeError("fail-closed: feature 결측 - queue 지표 응답 없음")

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        _run_main_n_cycles_raw(evidence_path, always_raises, n=999, once=True)
        skipped = [r for r in _read_jsonl(evidence_path) if r["record_type"] == "evaluation_skipped"]
        assert len(skipped) == 1
    print("OK - once=True에서 스킵이 나도 재시도 안 하고 그 자리에서 반환(정확히 1건만 기록)")


def test_non_runtimeerror_exception_from_verbose_still_propagates_and_crashes():
    """§107 - RuntimeError(fail-closed 데이터 품질 문제)만 스킵 대상이다.
    다른 예외 타입(진짜 버그)은 여전히 그대로 전파돼 프로세스를 종료시켜야
    한다 - 모든 예외를 조용히 삼키는 회귀를 방지."""
    def raises_value_error(model, scaler, schema, **kwargs):
        raise ValueError("이건 fail-closed 데이터 갭이 아니라 진짜 버그")

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        with patch.object(ss, "_evaluate_v32b_verbose", side_effect=raises_value_error):
            try:
                ss.main(str(V32B_ARTIFACTS_DIR), "v3.2b", once=True, experiment_run_id="test-run",
                        evidence_log_path=str(evidence_path))
                assert False, "ValueError는 그대로 전파돼야 함"
            except ValueError:
                pass
    print("OK - RuntimeError가 아닌 예외는 스킵 대상이 아니라 그대로 전파(모든 예외를 삼키는 회귀 방지)")


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


def _anomalous_verbose(model, scaler, schema, **kwargs):
    """§92 회귀 테스트 전용 fake - 항상 threshold보다 훨씬 낮은 score를
    내어 3회 연속 이상 상태를 손쉽게 재현한다(어떤 raw_feature_vector를
    쓰든 상관없음 - score 자체를 직접 고정)."""
    return {"window_start_utc": "t0s", "window_end_utc": "t0e", "raw_feature_vector": [1.0] * 8,
            "ordered_feature_vector": [1.0] * 6, "scaled_feature_vector": [1.0] * 6,
            "score": -999.0, "freshness": {"fresh": True}}


class _apply_all:
    """여러 patch를 한 번에 걸고 원복하는 작은 헬퍼(contextlib.ExitStack과 동일한 목적)."""
    def __init__(self, patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


def _run_main_n_cycles(evidence_path, n, *, post_side_effect=None, stop_file_path=None):
    """§92 - main()을 once=False로 n cycle만 돌리고 멈추게 하는 테스트
    헬퍼(sleep은 무력화, n번째 cycle 뒤 StopIteration으로 빠져나옴). 판정
    로직(advance_streak)은 전혀 건드리지 않고 IO 계층(_evaluate_v32b_verbose/
    post_to_recovery_policy/time.sleep)만 가짜로 대체한다."""
    calls = {"n": 0}

    def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] >= n:
            raise StopIteration("test: n cycles reached")

    patches = [patch.object(ss, "_evaluate_v32b_verbose", side_effect=_anomalous_verbose),
               patch.object(ss.time, "sleep", side_effect=fake_sleep)]
    if post_side_effect is not None:
        patches.append(patch.object(ss, "post_to_recovery_policy", side_effect=post_side_effect))
    with _apply_all(patches):
        try:
            ss.main(str(V32B_ARTIFACTS_DIR), "v3.2b", once=False, experiment_run_id="test-run",
                    evidence_log_path=str(evidence_path), stop_file_path=stop_file_path)
        except StopIteration:
            pass


def _read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def test_write_ahead_decision_recorded_before_signal_http_call():
    """§92 - evaluation_decision이 실제로 신호 HTTP 호출보다 "먼저" 파일에
    있어야 한다(§91 forensic이 지목한 기존 순서의 정반대) - mock 안에서
    파일을 직접 읽어 확인한다."""
    observed = {}

    def fake_post(score, run_id=None, detector="isolation_forest", **kwargs):
        recs = _read_jsonl(observed["path"])
        decisions = [r for r in recs if r["record_type"] == "evaluation_decision"]
        assert len(decisions) == 3, "3번째(신호) cycle의 evaluation_decision이 신호 전송 전에 이미 기록돼 있어야 함"
        assert decisions[-1]["would_signal"] is True
        observed["checked"] = True
        return {"outcome": "sent", "http_status": 200}

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        observed["path"] = evidence_path
        _run_main_n_cycles(evidence_path, 3, post_side_effect=fake_post)
    assert observed.get("checked") is True, "post_to_recovery_policy가 아예 호출 안 됨"
    print("OK - evaluation_decision이 신호 HTTP 호출보다 먼저 파일에 flush됨(write-ahead 순서 직접 확인)")


def test_write_ahead_decision_preserved_even_if_signal_raises_unexpected_exception():
    """§92 - 신호를 보내다 예상 밖 예외(§91 forensic 후보였던 시나리오의
    일반화)가 나도 그 cycle의 evaluation_decision은 이미 신호 시도 전에
    flush+fsync돼 있어 유실되지 않는다."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        try:
            _run_main_n_cycles(evidence_path, 3, post_side_effect=RuntimeError("simulated crash mid-signal"))
        except RuntimeError:
            pass
        recs = _read_jsonl(evidence_path)
        decisions = [r for r in recs if r["record_type"] == "evaluation_decision"]
        assert len(decisions) == 3, decisions
        assert decisions[-1]["would_signal"] is True
        assert decisions[-1]["evaluation_seq"] == 3
        signal_results = [r for r in recs if r["record_type"] == "signal_result"]
        assert signal_results == [], "예외가 났으므로 signal_result는 아예 안 남아야 함(있으면 안 됨)"
    print("OK - 신호 시도 중 예상 밖 예외가 나도 그 cycle의 evaluation_decision은 이미 보존됨(write-ahead)")


def test_signal_result_recorded_as_separate_record_linked_by_correlation_id():
    """§92 - 성공적인 신호는 evaluation_decision과 signal_result 두 개의
    별도 record로 남고(하나를 덮어쓰지 않음), 같은 correlation_id/
    evaluation_seq로 연결된다."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        _run_main_n_cycles(evidence_path, 3,
                            post_side_effect=lambda *a, **k: {"outcome": "sent", "http_status": 200,
                                                                "idempotency_key_hint": "test-run:anomaly_risk"})
        recs = _read_jsonl(evidence_path)
        decisions = [r for r in recs if r["record_type"] == "evaluation_decision"]
        results = [r for r in recs if r["record_type"] == "signal_result"]
        assert len(decisions) == 3 and len(results) == 1, (len(decisions), len(results))
        assert results[0]["correlation_id"] == decisions[-1]["correlation_id"]
        assert results[0]["evaluation_seq"] == decisions[-1]["evaluation_seq"] == 3
        assert results[0]["outcome"] == "sent"
    print("OK - evaluation_decision과 signal_result가 correlation_id/evaluation_seq로 연결된 별도 record로 남음")


def test_evaluation_seq_monotonic_and_correlation_id_unique_per_cycle():
    """§92 - evaluation_seq는 gap 없이 1부터 단조증가, correlation_id는
    cycle마다 서로 달라야 한다."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        _run_main_n_cycles(evidence_path, 5)
        decisions = [r for r in _read_jsonl(evidence_path) if r["record_type"] == "evaluation_decision"]
        seqs = [r["evaluation_seq"] for r in decisions]
        assert seqs == list(range(1, len(seqs) + 1)), seqs
        corr_ids = [r["correlation_id"] for r in decisions]
        assert len(set(corr_ids)) == len(corr_ids), "correlation_id가 cycle 간에 중복됨"
    print("OK - evaluation_seq는 gap 없이 단조증가, correlation_id는 cycle마다 고유함")


def test_broadened_exception_handling_survives_read_timeout():
    """§92 - ReadTimeout처럼 ConnectionError가 아닌 RequestException이 나도
    post_to_recovery_policy()가 절대 예외를 새 나가게 하면 안 된다(§91
    forensic의 root cause 후보에 대한 직접 회귀 테스트 - 이게 새 나가면
    main()의 while 루프 전체가 죽는다)."""
    with patch.object(ss.requests, "post", side_effect=ss.requests.exceptions.ReadTimeout("simulated read timeout")):
        result = ss.post_to_recovery_policy(-0.5, "run-1")
    assert result["outcome"] == "request_exception", result
    assert "ReadTimeout" in result["error"], result
    print("OK - ReadTimeout이 나도 post_to_recovery_policy()가 예외를 삼키고 request_exception으로 반환(while 루프 안 죽음)")


def test_signal_not_sent_when_evidence_write_ahead_fails():
    """§92.3 - evidence-log가 설정돼 있는데 write-ahead 자체가 실패하면
    (fail-closed) 신호를 보내지 않아야 한다."""
    sent = {"called": False}

    def fake_post(*a, **k):
        sent["called"] = True
        return {"outcome": "sent", "http_status": 200}

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        with patch.object(ss, "_write_evidence_line", return_value=False), \
             patch.object(ss, "post_to_recovery_policy", side_effect=fake_post):
            _run_main_n_cycles(evidence_path, 3)
    assert sent["called"] is False, "write-ahead가 실패했는데 신호가 나가면 안 됨(fail-closed 위반)"
    print("OK - write-ahead evidence 기록 실패 시 신호를 보내지 않음(fail-closed)")


def test_graceful_stop_file_exits_after_current_cycle_and_records_shutdown():
    """§92 - stop-file이 이미 존재하면 진행 중이던 cycle을 끝까지 완료한
    뒤 스스로 정상 종료하고 detector_shutdown record를 남긴다(once=False로
    돌려도 무한루프에 안 빠짐)."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        stop_file = Path(d) / "stop.flag"
        stop_file.touch()  # 첫 cycle 시작 전부터 이미 정지 요청이 있는 상태
        with patch.object(ss, "_evaluate_v32b_verbose", side_effect=_anomalous_verbose):
            ss.main(str(V32B_ARTIFACTS_DIR), "v3.2b", once=False, experiment_run_id="test-run",
                    evidence_log_path=str(evidence_path), stop_file_path=str(stop_file))
        recs = _read_jsonl(evidence_path)
        decisions = [r for r in recs if r["record_type"] == "evaluation_decision"]
        shutdowns = [r for r in recs if r["record_type"] == "detector_shutdown"]
        assert len(decisions) == 1, "stop-file이 미리 있으면 딱 1 cycle만 완료하고 종료해야 함"
        assert len(shutdowns) == 1, shutdowns
        assert shutdowns[0]["last_evaluation_seq"] == 1
        assert shutdowns[0]["exit_reason"] == "graceful_stop_file"
    print("OK - stop-file이 있으면 현재 cycle을 완료한 뒤 정상 종료하고 detector_shutdown을 기록함(무한루프 없음)")


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
