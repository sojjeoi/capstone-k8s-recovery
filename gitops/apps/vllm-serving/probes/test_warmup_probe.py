#!/usr/bin/env python3
"""warmup_probe.py 오프라인 테스트 - 실서버 없이 urllib.request.urlopen을
모킹해 5가지 종료 코드 분기와 payload의 max_tokens=1을 검증한다."""
import json
import urllib.error
from unittest.mock import MagicMock, patch

import warmup_probe as wp


def _mock_response(body: dict, status: int = 200):
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = json.dumps(body).encode()
    cm = MagicMock()
    cm.__enter__.return_value = resp
    return cm


def test_normal_completion_success():
    cm = _mock_response({"choices": [{"text": "!"}]})
    with patch("urllib.request.urlopen", return_value=cm):
        code, _ = wp.check_completion()
    assert code == wp.EXIT_OK


def test_connection_failure():
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(ConnectionRefusedError())):
        code, _ = wp.check_completion()
    assert code == wp.EXIT_CONN_FAIL


def test_timeout_raised_bare():
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        code, _ = wp.check_completion()
    assert code == wp.EXIT_TIMEOUT


def test_timeout_wrapped_in_urlerror():
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(TimeoutError("timed out"))):
        code, _ = wp.check_completion()
    assert code == wp.EXIT_TIMEOUT


def test_http_error_status():
    err = urllib.error.HTTPError(wp.URL, 500, "Internal Server Error", hdrs=None, fp=None)
    with patch("urllib.request.urlopen", side_effect=err):
        code, _ = wp.check_completion()
    assert code == wp.EXIT_BAD_STATUS


def test_malformed_response_missing_choices():
    cm = _mock_response({"unexpected": "shape"})
    with patch("urllib.request.urlopen", return_value=cm):
        code, _ = wp.check_completion()
    assert code == wp.EXIT_BAD_FORMAT


def test_malformed_response_invalid_json():
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = b"not json"
    cm = MagicMock()
    cm.__enter__.return_value = resp
    with patch("urllib.request.urlopen", return_value=cm):
        code, _ = wp.check_completion()
    assert code == wp.EXIT_BAD_FORMAT


def test_warmup_payload_uses_max_tokens_1_and_same_model():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _mock_response({"choices": [{"text": "!"}]})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        wp.check_completion()
    assert captured["body"]["max_tokens"] == 1
    assert captured["body"]["model"] == wp.MODEL
