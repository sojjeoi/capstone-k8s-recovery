# 재현성·최종 상태 audit (2026-10-10, 읽기 전용)

근거 파일: `experiments/results/final_evidence_audit_20261010/` (`provenance/manifest-check-20261010.jsonl`, `provenance/c1-check-20261010.json`, `tests/TEST-SUMMARY.md`, `ab/`, `bck/`, `n1/`). 기존 해시를 다시 만들거나 덮어쓰지 않았다.

## B1. artifact / provenance

| 대상 | 결과 |
|---|---|
| c1 model/scaler/threshold 등 9개 artifact(`anomaly-detection/v3/model_v33_c1/artifacts/SHA256SUMS.json`) | 9/9 일치 |
| c1 독립 검증 freeze(`experiments/analysis_v1/v33c1-validation-freeze-manifest-20261005.json`) | 46/46 파일 + 계약 해시 일치. threshold −0.07667538512242333, 3연속, 쿨다운 60 s |
| B/C/K 공식 freeze v1.2 | 16/16 일치(v1·v1.1은 superseded - runner만 다름, 예상된 불일치) |
| N1 공식 freeze n1-v1.1 | 31/31 일치(v1 superseded, 예상된 불일치) |
| iof v1.0.1 / nnf v1 | 일치(iof v1·candidate는 v1.0.1 수정으로 예상된 불일치) |
| v2wl-v2 frozen | **1개 불일치**: `docs/design/phase8-aiops-v2-design-draft-20261009.md`(설계 초안 문서). freeze 09:12Z 뒤 12:03Z 수정 - v2wl90·91 실행 뒤의 문서 갱신으로 보임(코드·config·계약은 일치). 판정에 영향 없음, 기록만 |
| A/B 공식 | 별도 freeze manifest 없음(provenance 기록 이전 계열). 원자료 probe CSV sha256 = 분석 JSON 기록값(재계산 확인) |
| Python / sklearn / numpy | 학습 3.14.3 / 1.9.0 / 2.5.1 = 현재 같음 |
| 세션 래퍼 `guarded_pilot_session.ps1` | sha256 68c9349a… = 공식 act run 기준값 |
| 실행 ID 중복 | 코드가 summary·evidence 존재 시 재사용 거부. 사용됨: grn01–02, grp01–02, gap01–02, ab00a, ab01–05 a·b, ab90 a·b, bc01–06 b·c·k, bc90 b·c·k, nb01–08, nb90–91, v2wl90–91, iob90–91, iod90, nnb90, nnd90. **사용 가능한 비공식 act ID는 gap03 하나**(ab06–15는 공식 비교 ID) |
| 원자료 변경 | 이번 audit은 새 폴더에만 썼다. 재계산이 공식 수치와 전부 일치 → 원자료 변조 흔적 없음 |

## B2. tests (클러스터 접근 없는 mock/fake만)

- anomaly-detection 425 passed, 4 xfailed(legacy 경로 known bug 고정 기록).
- experiments 한 프로세스 전체 실행 2188 passed / 76 failed / 3 errors → 실패 파일 17개 단독 실행: 15개 전부 통과(테스트 간 상태 오염), 2건 남음 - "실제 결과 폴더에 아직 공식 쌍·블록이 없어야 한다"는 **공식 실행 이전 작성 테스트**(A/B·B/C/K 공식 run이 생겨 당연히 실패). 연구 로직 결함 아님, frozen이라 수정 안 함.
- dashboard 47/48 → 재실행 48/48 ×3(Windows 로컬 소켓 flake).

## B3. 현재 클러스터 (읽기 전용, 2026-10-10 02:3x KST)

| 항목 | 상태 |
|---|---|
| 노드 | sj-control·sj-worker·sj-worker2 Ready (v1.31.14) |
| active vLLM | `vllm-serving-7976ddcdb6-56xmz`, UID 96d3360a…, sj-worker2, 10.244.224.195, Ready, 재시작 0 |
| preview | 별도 Pod 없음(active = preview = 7976ddcdb6, canonical) |
| Rollout | Healthy, gen 387 = observed, scaleDownDelay 30, autoPromotion false, antiAffinity required(라이브 적용, 저장소 gitops는 미커밋 diff) |
| Service selector / endpoints | vllm-active·vllm-preview → hash 7976ddcdb6 / 10.244.224.195:8000 |
| guarded-recovery-policy | 1 Pod sj-worker, record_only(nnd90 사후 관문 최종 사전검사에서 identity·state 확인: context 없음, unresolved 0, ledger 86) |
| 기존 recovery-policy | 1 Pod Running(복원 상태) |
| 잔여 | stress·chaos·probe·loadgen·burner Pod 0, running marker 0 |
| Prometheus | Running(sj-worker), node-exporter 3개 Running |
| 노드 사용량 | CPU 27–35 %, 메모리 sj-worker2 73 %, sj-worker 53 % |

## B4. 시연 재현

`docs/demo/DEMO-RUNBOOK-20261010.md` 참조.
