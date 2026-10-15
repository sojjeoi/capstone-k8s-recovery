# audit-log - 초기 Recovery Policy의 결정 기록 (historical)

이 폴더의 `*.jsonl` 파일은 **초기 Recovery Policy**([recovery-policy/git_client.py](../recovery-policy/git_client.py))가 결정할 때마다 남기고 `recovery-policy-bot` 이름으로 커밋한 감사 기록이다(2026-09-15 ~ 2026-10-06).

- 파일 이름 = 실험 실행 ID(예: `load_ramp-proposed-…`, `pod_kill-fixed_threshold-…`). `adhoc.jsonl`은 실행 ID 없이 들어온 기록.
- 한 줄 = 결정 1건: `decided_at`, `signal_source`, `signal_type`, `action`, `outcome`, `reasoning` 등.
- 초기·중간 단계 실험(3-arm 비교 설계, v33c1 검증 등)의 기록이며, **최종 결과(Phase 1 A/B, Phase 2)의 근거가 아니다.**

최종 guarded 정책([recovery-policy/main_guarded.py](../recovery-policy/main_guarded.py))은 이 폴더에 쓰지 않는다. 조치 기록은 정책의 상태 파일·조치 원장에 남고, 실험별 근거는 [experiments/results/](../experiments/README.md)의 summary·분석 파일에 정리돼 있다.

처음 보는 사람은 이 폴더를 해석할 필요가 없다. 최종 결과는 [docs/final/](../docs/README.md)에서 본다.
