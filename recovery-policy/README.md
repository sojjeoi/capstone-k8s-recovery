# recovery-policy - Recovery Layer

detector 신호를 받아 조치 여부를 결정하고 Argo Rollouts 승격까지 수행하는 정책 서비스. detector 종류와 무관하게 같은 경로를 쓴다.

- `guarded_policy.py`, `guarded_state.py`, `guarded_ledger.py`, `guarded_schema.py`, `guarded_k8s.py`, `main_guarded.py`: 공식 실험에 쓴 guarded 정책(record_only / act, 중복 조치 방지·쿨다운·감사 기록, 승격 후 서비스 확인)
- `main.py`, `policy.py`, `rollouts_client.py`, `safety.py`, `decision_log.py`, `git_client.py`: 초기 정책 서비스와 감사 기록(Git) 경로 - `audit-log/`의 bot commit을 남긴 구성 요소
- 테스트: `test_*.py`

결과 요약: [../docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md](../docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md)
