#!/usr/bin/env python3
"""safety.py의 idempotency 차단과 조치 쿨다운 확인. 매번 상태 파일을 비우고
시작해서 반복 실행해도 결과가 같게 한다(테스트 흔적은 끝에 정리)."""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import safety


def _reset_state():
    if safety.STATE_FILE.exists():
        safety.STATE_FILE.unlink()


def test_check_and_reserve_blocks_duplicate():
    _reset_state()
    key = "dup-test-key"
    assert safety.check_and_reserve(key) is True, "첫 처리는 통과해야 함"
    assert safety.check_and_reserve(key) is False, "같은 key 재처리는 막혀야 함"
    print("OK - check_and_reserve: 첫 신호 통과, 중복 신호 차단")


def test_cooldown_active_right_after_action():
    safety.mark_action_taken()
    assert safety.in_action_cooldown() is True
    print("OK - 조치 직후 쿨다운 활성")


def test_cooldown_not_active_when_stale():
    state = safety._load_state()
    state["last_action_at"] = time.time() - safety.ACTION_COOLDOWN_SEC - 1
    safety._save_state(state)
    assert safety.in_action_cooldown() is False
    print("OK - 쿨다운 만료 후 비활성")


if __name__ == "__main__":
    test_check_and_reserve_blocks_duplicate()
    test_cooldown_active_right_after_action()
    test_cooldown_not_active_when_stale()
    _reset_state()  # 테스트 흔적 정리
    print("모두 통과")
