"""trace_tools 의 처리 Pod 특정 로직 시험(합성 로그, 클러스터 불필요). 실행: python test_trace_tools.py"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trace_tools as tt  # noqa: E402

T = datetime(2026, 10, 23, 3, 0, 0, tzinfo=timezone.utc)
LINE = '2026-10-23T03:00:01.250000000Z (APIServer pid=1) INFO:     10.244.9.9:40001 - "POST /v1/completions HTTP/1.1" 200 OK'


def test_parse():
    a = tt.parse_access_line(LINE)
    assert (a["ip"], a["port"], a["method"], a["status"]) == ("10.244.9.9", 40001, "POST", 200)
    assert a["ts"] == T + timedelta(seconds=1.25)
    assert tt.parse_access_line('2026-10-23T03:00:01Z (APIServer pid=1) INFO:     1.2.3.4:5 - "GET /health HTTP/1.1" 200 OK')["method"] == "GET"
    assert tt.parse_access_line("garbage") is None


def test_attribute():
    l2 = LINE.replace("40001", "40002").replace("03:00:01", "03:00:02")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "podA.log").write_text(LINE + "\n" + l2 + "\n"
                                    '2026-10-23T03:00:01.5Z (APIServer pid=1) INFO:     10.244.9.9:40003 - "GET /metrics HTTP/1.1" 200 OK\n', encoding="utf-8")
        (d / "podB.log").write_text(l2 + "\n", encoding="utf-8")
        acc = tt.load_access(d)
        assert ("10.244.9.9", 40003) not in acc                                  # GET 은 제외
        assert tt.attribute("10.244.9.9", 40001, T, T + timedelta(seconds=2), acc)[0] == "podA"     # podA 에만 있음
        pod, why = tt.attribute("10.244.9.9", 40002, T, T + timedelta(seconds=3), acc)             # 두 Pod 모두 일치 -> 추정하지 않음
        assert pod is None and "여러 Pod" in why
        assert tt.attribute("10.244.9.9", 40001, T + timedelta(seconds=60), T + timedelta(seconds=61), acc)[0] is None   # 시각 밖
        assert tt.attribute(None, None, T, T, acc)[0] is None                                      # 소켓 주소 미상


if __name__ == "__main__":
    test_parse()
    test_attribute()
    print("OK")
