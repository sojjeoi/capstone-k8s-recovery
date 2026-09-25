"""run_load_ramp_followup_trial.py의 재시도 provenance 최소 구현 오프라인
검증(§143 이후 지시 §2A/§3). --dry-run-contract-only 경로만 쓴다 - 클러스터·
실제 injector/prober/detector 생성 이전에 리턴하므로 완전히 오프라인이다.
FOLLOWUP_RESULTS_DIR은 모듈 상수라 monkeypatch로 tmp_path에 격리한다(실제
results/followup/을 절대 건드리지 않음)."""
import hashlib
import json
import sys

import pytest

import run_load_ramp_followup_trial as rt


def _run_main(monkeypatch, tmp_path, argv):
    monkeypatch.setattr(rt, "FOLLOWUP_RESULTS_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_load_ramp_followup_trial.py"] + argv + ["--dry-run-contract-only"])
    rt.main()


def test_normal_run_no_attempt_of_has_plain_run_id_and_schema(monkeypatch, tmp_path):
    _run_main(monkeypatch, tmp_path, ["--arm", "fixed_threshold", "--rep", "3"])
    contract = json.loads((tmp_path / "contract-load_ramp-fixed_threshold-03-post_hoc_followup-v1.json")
                           .read_text(encoding="utf-8"))
    assert contract["run_id"] == "load_ramp-fixed_threshold-03-post_hoc_followup-v1"
    assert contract["contract_schema_version"] == "followup-v1"
    assert "replacement" not in contract
    print("OK - --attempt-of 없으면 기존과 동일한 run_id/스키마")


def test_attempt_of_inserts_suffix_and_records_replacement_linkage(monkeypatch, tmp_path):
    original_run_id = "load_ramp-fixed_threshold-03-post_hoc_followup-v1"
    original_path = tmp_path / f"trial-{original_run_id}.json"
    original_body = json.dumps({"outcome": "invalid_run", "state": "invalid"}, ensure_ascii=False)
    original_path.write_text(original_body, encoding="utf-8")

    _run_main(monkeypatch, tmp_path, [
        "--arm", "fixed_threshold", "--rep", "3",
        "--attempt-of", original_run_id, "--attempt-suffix", "retry1",
        "--replacement-reason", "WinError 1455 - 로컬 자원 문제로 524초 지점에서 무효화",
    ])

    retry_run_id = "load_ramp-fixed_threshold-03-retry1-post_hoc_followup-v1"
    contract = json.loads((tmp_path / f"contract-{retry_run_id}.json").read_text(encoding="utf-8"))
    assert contract["run_id"] == retry_run_id
    assert contract["contract_schema_version"] == "followup-retry-v1"
    link = contract["replacement"]
    assert link["attempt_of_run_id"] == original_run_id
    assert link["original_result_path"] == str(original_path)
    assert link["same_logical_rep"] == {"scenario": "load_ramp", "arm": "fixed_threshold", "rep": 3,
                                         "plan_id": "post_hoc_followup-v1"}
    assert link["original_result_sha256"] == hashlib.sha256(original_body.encode("utf-8")).hexdigest()

    # 원본 파일은 절대 수정되지 않아야 한다.
    assert original_path.read_text(encoding="utf-8") == original_body
    print("OK - --attempt-of가 run_id 접미사 + 연결 기록을 남기고 원본은 그대로 보존")


def test_attempt_of_without_suffix_or_reason_fails_fast(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, tmp_path, ["--arm", "fixed_threshold", "--rep", "3",
                                           "--attempt-of", "some-original-run-id"])
    print("OK - --attempt-of만 주고 --attempt-suffix/--replacement-reason 누락하면 실패")


def test_attempt_of_missing_original_file_fails_fast(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, tmp_path, [
            "--arm", "fixed_threshold", "--rep", "3",
            "--attempt-of", "no-such-original-run-id", "--attempt-suffix", "retry1",
            "--replacement-reason", "테스트",
        ])
    assert not list(tmp_path.glob("contract-*retry1*"))
    print("OK - 원본 결과 파일이 없으면 contract도 안 만들고 실패")


def test_existing_contract_blocks_overwrite(monkeypatch, tmp_path):
    run_id = "load_ramp-fixed_threshold-03-post_hoc_followup-v1"
    existing_path = tmp_path / f"contract-{run_id}.json"
    existing_path.write_text("이미 있던 원본 계약", encoding="utf-8")

    with pytest.raises(SystemExit):
        _run_main(monkeypatch, tmp_path, ["--arm", "fixed_threshold", "--rep", "3"])
    assert existing_path.read_text(encoding="utf-8") == "이미 있던 원본 계약", "기존 계약 파일을 덮어쓰면 안 됨"
    print("OK - 동일 run_id의 계약이 이미 있으면 덮어쓰지 않고 실패")


def test_existing_retry_attempt_at_same_suffix_blocks_overwrite(monkeypatch, tmp_path):
    original_run_id = "load_ramp-fixed_threshold-03-post_hoc_followup-v1"
    (tmp_path / f"trial-{original_run_id}.json").write_text(
        json.dumps({"outcome": "invalid_run"}), encoding="utf-8")
    retry_run_id = "load_ramp-fixed_threshold-03-retry1-post_hoc_followup-v1"
    existing_retry_trial = tmp_path / f"trial-{retry_run_id}.json"
    existing_retry_trial.write_text(
        json.dumps({"outcome": "recovered", "marker": "먼저 있던 재시도 결과"}, ensure_ascii=False),
        encoding="utf-8")

    with pytest.raises(SystemExit):
        _run_main(monkeypatch, tmp_path, [
            "--arm", "fixed_threshold", "--rep", "3",
            "--attempt-of", original_run_id, "--attempt-suffix", "retry1",
            "--replacement-reason", "동일 접미사 재사용 시도(의도적 충돌 테스트)",
        ])
    assert "먼저 있던 재시도 결과" in existing_retry_trial.read_text(encoding="utf-8")
    print("OK - 같은 -retry1- run_id에 이미 결과가 있으면(자동 접미사 증가 없이) 실패")


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        with tempfile.TemporaryDirectory() as d, pytest.MonkeyPatch.context() as mp:
            t(mp, Path(d))
    print(f"\n전체 {len(tests)}개 오프라인 검증 통과")
