"""probe_followup.py 오프라인 검증 (§137 이후 지시 §7 필수 테스트).
실제 로컬 aiohttp 서버(mock 아님)를 띄워 정상완료/HTTP오류/timeout 세
경로를 진짜로 통과시켜 확인한다. 클러스터·kubectl 불필요."""
import asyncio
import csv
import json
import sys
from pathlib import Path

import pytest
import yaml
from aiohttp import web

sys.path.insert(0, str(Path(__file__).parent / "loadgen-runner"))
import probe_followup  # noqa: E402


@pytest.fixture
def local_server(unused_tcp_port):
    async def ok_handler(request):
        return web.json_response({"result": "ok"}, status=200)

    async def slow_handler(request):
        await asyncio.sleep(probe_followup.REQUEST_TIMEOUT_SEC + 3)  # probe 자체 30s timeout을 강제로 넘김
        return web.json_response({"result": "too_late"}, status=200)

    async def badstatus_handler(request):
        return web.json_response({"result": "error"}, status=500)

    app = web.Application()
    app.router.add_post("/ok", ok_handler)
    app.router.add_post("/slow", slow_handler)
    app.router.add_post("/badstatus", badstatus_handler)
    return app, unused_tcp_port


async def _run_server(app, port):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner


def _write_config(tmp_path, url, max_tokens=1):
    cfg = {"target": {"url": url, "model": "dummy", "prompt": "hi", "max_tokens": max_tokens}, "rps": 2}
    p = tmp_path / "probe-config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return str(p)


def _load_evidence(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_completed_request_has_sent_and_completed_events(tmp_path, local_server):
    app, port = local_server
    runner = await _run_server(app, port)
    try:
        config = _write_config(tmp_path, f"http://127.0.0.1:{port}/ok")
        out_csv = tmp_path / "raw.csv"
        out_evidence = tmp_path / "evidence.jsonl"
        await probe_followup.main(config, "test-run", "unit", "arm", 1, str(out_csv), str(out_evidence), duration_sec=1.5)

        rows = list(csv.DictReader(open(out_csv, encoding="utf-8")))
        events = _load_evidence(out_evidence)
        sent_ids = {e["request_id"] for e in events if e["event"] == "sent"}
        completed_ids = {e["request_id"] for e in events if e["event"] == "completed"}

        assert len(rows) >= 2, "1.5초 x 2rps면 최소 2건은 완료돼야 함"
        assert sent_ids == completed_ids, "정상 완료된 모든 요청은 sent와 completed 이벤트가 같은 request_id로 쌍을 이뤄야 함"
        for e in events:
            if e["event"] == "completed":
                assert e["http_status"] == 200 and e["success"] is True
                assert e["sent_at"] <= e["completed_at"], "완료 시각이 발신 시각보다 빨라선 안 됨(timestamp 역전 탐지)"
        print("OK - 정상 완료 요청의 sent/completed 이벤트 쌍 일치")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_error_recorded_as_failure_not_dropped(tmp_path, local_server):
    app, port = local_server
    runner = await _run_server(app, port)
    try:
        config = _write_config(tmp_path, f"http://127.0.0.1:{port}/badstatus")
        out_csv = tmp_path / "raw.csv"
        out_evidence = tmp_path / "evidence.jsonl"
        await probe_followup.main(config, "test-run", "unit", "arm", 1, str(out_csv), str(out_evidence), duration_sec=1.0)

        rows = list(csv.DictReader(open(out_csv, encoding="utf-8")))
        assert len(rows) >= 1
        assert all(r["success"] == "False" and r["status"] == "500" for r in rows), \
            "HTTP 500은 success=False, status=500으로 남아야 함(조용히 사라지면 안 됨)"
        print("OK - HTTP 오류 응답이 실패로 정확히 기록됨(누락 아님)")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_timeout_recorded_explicitly_not_silently_dropped(tmp_path, local_server, monkeypatch):
    # 실제 30초를 기다리면 테스트가 느려지므로, REQUEST_TIMEOUT_SEC 자체를
    # 테스트 동안만 짧게 줄인다(운영 코드의 상수 자체를 바꾸는 게 아니라
    # 모듈 속성을 monkeypatch - _fire_and_log는 매 호출마다 이 모듈 속성을
    # 참조하므로 그대로 반영된다).
    monkeypatch.setattr(probe_followup, "REQUEST_TIMEOUT_SEC", 1)
    app, port = local_server
    runner = await _run_server(app, port)
    try:
        config = _write_config(tmp_path, f"http://127.0.0.1:{port}/slow")
        out_csv = tmp_path / "raw.csv"
        out_evidence = tmp_path / "evidence.jsonl"
        await probe_followup.main(config, "test-run", "unit", "arm", 1, str(out_csv), str(out_evidence), duration_sec=1.0)

        rows = list(csv.DictReader(open(out_csv, encoding="utf-8")))
        events = _load_evidence(out_evidence)
        timeout_events = [e for e in events if e["event"] == "timeout"]
        assert len(rows) >= 1
        assert all(r["success"] == "False" and r["status"] == "" for r in rows), \
            "timeout은 success=False, status=None(빈 값)으로 남아야 함"
        assert len(timeout_events) >= 1, "timeout 이벤트가 evidence에 명시적으로 남아야 함(조용히 사라지면 안 됨)"
        print("OK - timeout이 명시적으로 기록됨(latency 분석에서 조용히 제거되지 않음)")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_unresolved_after_grace_is_flagged_not_faked(tmp_path, local_server, monkeypatch):
    # grace 자체도 짧게 줄여서(원래 60초는 테스트에 너무 김) "grace 안에도
    # 안 끝난 요청"을 실제로 만든다 - REQUEST_TIMEOUT_SEC은 넉넉히 둬서
    # (grace보다 길게) probe 자체 30초 timeout보다 grace가 먼저 끝나는
    # 상황을 흉내낸다(실제 그리드에서는 grace(60s) < REQUEST_TIMEOUT_SEC(30s)
    # 아니지만, 이 테스트는 "grace 만료 시점에 여전히 pending인 요청"이라는
    # 상황 자체를 보는 것이므로 상대적 크기만 맞추면 된다).
    monkeypatch.setattr(probe_followup, "REQUEST_TIMEOUT_SEC", 10)
    monkeypatch.setattr(probe_followup, "GRACE_SEC", 0.3)
    app, port = local_server
    runner = await _run_server(app, port)
    try:
        config = _write_config(tmp_path, f"http://127.0.0.1:{port}/slow")
        out_csv = tmp_path / "raw.csv"
        out_evidence = tmp_path / "evidence.jsonl"
        await probe_followup.main(config, "test-run", "unit", "arm", 1, str(out_csv), str(out_evidence), duration_sec=0.6)

        rows = list(csv.DictReader(open(out_csv, encoding="utf-8")))
        events = _load_evidence(out_evidence)
        sent_ids = {e["request_id"] for e in events if e["event"] == "sent"}
        completed_ids = {e["request_id"] for e in events
                          if e["event"] in ("completed", "timeout", "error")}
        summaries = [e for e in events if e["event"] == "unresolved_summary"]

        assert sent_ids and not completed_ids, "이 시나리오는 전부 미완료여야 함(느린 엔드포인트 + 짧은 grace)"
        assert len(rows) == 0, "미완료 요청을 성공/실패/가짜 latency로 CSV에 채워 넣으면 안 됨"
        assert summaries and summaries[0]["n_unresolved_after_grace"] == len(sent_ids), \
            "미완료 건수가 grace 만료 후 summary 이벤트에 정확히 남아야 함(숨기지 않음)"
        print(f"OK - grace 만료 후 미완료 {len(sent_ids)}건이 CSV엔 안 남고 evidence엔 명시적으로 남음")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_stop_file_halts_new_sends_but_drains_in_flight(tmp_path, local_server):
    """§138 이후 지시 §3 - stop-file을 만들면 (a) 새 요청 발신은 즉시(~1초
    이내) 멈추고 (b) 이미 발신된 요청은 정상적으로 drain(대기+기록)돼야
    한다 - stop() 쪽이 프로세스를 강제로 죽이지 않고 이 자연 종료를 기다릴
    수 있어야 §3의 경합이 실제로 해소된다."""
    app, port = local_server
    runner = await _run_server(app, port)
    try:
        config = _write_config(tmp_path, f"http://127.0.0.1:{port}/ok")
        out_csv = tmp_path / "raw.csv"
        out_evidence = tmp_path / "evidence.jsonl"
        stop_file = tmp_path / "probe.stopfile"

        async def _touch_stop_file_soon():
            await asyncio.sleep(1.2)  # 요청 1~2건은 나간 뒤에 중단 신호
            stop_file.write_text("stop")

        await asyncio.gather(
            probe_followup.main(config, "test-run", "unit", "arm", 1, str(out_csv), str(out_evidence),
                                 duration_sec=10.0, stop_file_path=str(stop_file)),
            _touch_stop_file_soon(),
        )

        events = _load_evidence(out_evidence)
        sent_events = [e for e in events if e["event"] == "sent"]
        send_stopped = [e for e in events if e["event"] == "send_stopped"]
        assert send_stopped and send_stopped[0]["reason"] == "stop_file", "stop-file로 중단됐음이 명시돼야 함"
        # duration_sec=10인데 stop-file이 ~1.2초에 생겼으므로, 그 근처에서 멈춰야 한다
        # (10초 전체를 다 채우면 stop-file이 무시된 것 - 버그).
        assert send_stopped[0]["elapsed_sec"] < 5.0, \
            f"stop-file 신호를 못 받고 계속 발신함(elapsed={send_stopped[0]['elapsed_sec']}s, duration_sec=10.0)"
        assert 1 <= len(sent_events) <= 4, f"stop-file 근처에서 발신이 멈춰야 하는데 {len(sent_events)}건 발신됨"
        completed_ids = {e["request_id"] for e in events if e["event"] == "completed"}
        sent_ids = {e["request_id"] for e in sent_events}
        assert sent_ids == completed_ids, "이미 발신된 요청은 stop-file 이후에도 정상 drain(완료 기록)돼야 함"
        print(f"OK - stop-file로 {send_stopped[0]['elapsed_sec']}s만에 발신 중단, "
              f"이미 발신된 {len(sent_ids)}건은 전부 정상 drain됨")
    finally:
        await runner.cleanup()


def test_duplicate_request_id_is_detected():
    # 네트워크 없이 순수 로직만 - seen_ids 가드가 실제로 예외를 던지는지 확인.
    # _fire_and_log는 코루틴이라 asyncio로 직접 두 번 호출하되, 두 번째 호출
    # 전에 seen_ids에 강제로 첫 번째 request_id를 미리 넣어 충돌을 흉내낸다.
    import uuid
    from unittest.mock import patch

    seen_ids = set()
    fixed_id = uuid.uuid4().hex
    seen_ids.add(fixed_id)

    async def _trigger():
        with patch("probe_followup.uuid.uuid4") as mock_uuid:
            mock_uuid.return_value.hex = fixed_id
            # session/csv_writer 등은 이 경로(중복 감지)에서 전혀 안 쓰이므로 None으로 충분
            await probe_followup._fire_and_log(None, None, None, None, None, lambda e: None,
                                                {"experiment_run_id": "x"}, seen_ids)

    with pytest.raises(RuntimeError, match="request_id 중복"):
        asyncio.run(_trigger())
    print("OK - request_id 중복 생성 시 예외로 명시 탐지")
