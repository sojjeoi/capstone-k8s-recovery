# 전환/무전환 비교 계약 r2 (guarded-ab-v1-20261007-r2) — 첫 공식 결과 전 개정

작성: 2026-10-07(KST). **실행 승인 문서가 아니다.** 이전 계약 r1 `phase8-v33c1-guarded-ab-comparison-contract-20261007.md`는 수정하지 않고 보존한다.

| | 파일 | SHA-256 |
|---|---|---|
| r1(변경 전·후 동일, 보존) | `phase8-v33c1-guarded-ab-comparison-contract-20261007.md` | `3aeb8ac0e7147d29662f66f830f37c2e31ed88cbd2922c678e2c6e82757e765f` |
| r2(이 파일) | `phase8-v33c1-guarded-ab-comparison-contract-r2-20261007.md` | 별도 기록 `phase8-v33c1-guarded-ab-comparison-contract-r2-20261007.sha256.txt` |

**개정 이유:** 첫 공식 비교 실행 전에 ab00a rehearsal에서 no-promotion A의 D5 abort 이후 Rollout이 Degraded/abort 상태로 남아 B 시작 상태와 비대칭이 생기는 것을 발견했다. 공식 ab01 결과는 아직 하나도 수집되지 않았다. canonical Healthy restore를 별도 사전검증한 뒤 모든 arm을 동일한 canonical start state에서 시작하도록 계약을 개정한다.

**소급 없음:** ab00a는 r1 아래 수행된 개발 rehearsal이며 결과·요약·disposition·원자료·Degraded 종료 기록을 그대로 둔다. canonical restore 1차(원래 사전 기준으로 not validated)·2차(사전 기준 17개 실질 충족, 판정기 결함 별도 기록) 검증 자료도 그대로 보존한다. 이 자료들은 개발 근거이며 공식 비교 횟수에 넣지 않는다. 아래에 적지 않은 r1 조항(질문·arm·모델 동결·실행 조건·순서·반복 수·유효/무효/중단·지표·분석·ID 규칙)은 그대로다.

## 1. Canonical Healthy State (모든 공식 실행·rehearsal의 시작 조건)

실행기 사전검사(`guarded_cleanup_gate.check_canonical`, A/B 실행만)가 아래를 모두 읽어 확인하고, 하나라도 다르거나 읽지 못하면 **시작을 거부**한다. 이 검사는 상태를 고치지 않는다(자동 복원 후 계속 없음).

- Rollout phase = Healthy, abort 없음, `observedGeneration` = `generation`(§5 정규화)
- desired(current) = stable = active = preview hash
- active·preview Service selector = active hash, active endpoint = active Pod IP 1개
- active Pod 1개, Ready, 종료 중 아님, restart 0
- active RS replicas 1/1/1, 그 밖의 RS 0/0/0
- `scaleDownDelaySeconds` = 30, active `/health` 정상
- StressChaos·probe/loadgen Pod 잔여 없음
- 기존 사전검사 그대로: guarded 미해결·차단·context 없음, 기존 recovery-policy 격리 확인, 노드·디스크·PC 조건

## 2. 미승격 arm 정리(A 전체, 실제 승격 없는 B)

1. 기존 D5 조건 확인(정책·원장·탐지기·실제 Rollout으로 미승격·대상 동일성 확인)
2. 기존 `abort_preview`(+ 기존 scale-down 실측 확인)
3. 중단된 preview Pod 종료 대기(읽기, 최대 120초) 뒤 기대 상태 확인: Degraded, abort, active = stable = 시작 active, current = 이번 preview hash
4. stable RS template과 중단 RS template 비교(Argo 주입 anti-affinity·pod-template-hash 라벨 제외, 2차 실측과 같은 기준)
5. 의미 있는 차이가 정확히 `metadata.annotations.experiment-prep-ts` 하나인지, Rollout template metadata = 중단 RS metadata인지 확인
6. stable RS의 실제 값으로 그 annotation 하나만 merge patch 1회(`patch_prep_ts`는 이 key 외에 바꿀 수 없음)
7. canonical 재확인: 3회 연속 canonical(최대 60초) + 20초 뒤 다시 읽기
8. 성공할 때만 정리 완료
9. 실패하면 정리 오류 `canonical_restore_failed` → 실행 중 표시 보존 → 다음 arm 자동 거부. 재시도·다른 복원 방법 없음

patch 전 확인: Rollout 재조회로 generation·현재 값 불변, active Pod 이름·UID = 실행 시작 canonical 검사의 active Pod, active selector·endpoint·health 정상, stable RS 존재, 복원 값 = stable RS 실제 값.
**실제 승격된 B는 D5가 정리를 건너뛰고 기존 D6 정리로만 끝난다(이 복원을 호출하지 않음). D6 알고리즘은 바꾸지 않았다.**
모든 A/B 실행은 끝에서 canonical(유예 30 포함)을 읽기로 다시 확인한다(최대 90초, 옛 Pod 종료 대기용). 아니면 `canonical_end_not_reached` 정리 오류로 다음 arm을 막는다.

## 3. 복원 뒤 허용 bookkeeping / 실패

- **허용:** 같은 active RS UID·hash 안의 revision 증가, `revision-history` 갱신, patch 전에 desired·current·ready 0·active 아님·stable 아님·Pod 없음이 확인된 old RS의 `revisionHistoryLimit` 삭제, preview selector → active hash, generation/observedGeneration 증가.
- **실패:** 새 RS, active RS UID/hash 변경, active Pod 이름/UID 변경, restart 증가, active selector·endpoint 변경, traffic 전환, active RS scale down, 다른 RS replica>0, active/stable RS 삭제, 허용 밖 RS 삭제, health 이상, Healthy·abort 해제·hash 일치 실패, preview Pod 재생성, 읽기 실패(evidence 부족).

## 4. 실행 ID — rehearsal

- `ab90a`/`ab90b` = r2 개발 rehearsal(집계 제외 `counted_in_comparison=false`, 같은 쌍 A→B, B는 `runs=["ab90b"]` 승인만). `ab00`(r1)은 보존·재사용 금지. 공식은 `ab01`~`ab15`만. 같은 결과 폴더, 실행 ID·probe 반복 번호(980/981)로 분리, 재사용 금지 그대로.
- **ab90a lifecycle:** canonical 사전검사 → preview 준비 → record_only A → CPU 압박·600초 관찰 → 승격 0 → D5 abort → canonical 복원 → canonical 확인 → 정리 완료.
- **ab90b lifecycle:** 같은 canonical 시작 조건·A와 같은 시작 노드 → preview 준비 → act B → IF 조건 충족 시 최대 1회 승격 → 서비스 verified → 600초 관찰 → D6(구 RS 0 실측 뒤 30 원복) → canonical 확인 → 정리 완료. 승격이 없으면 D5 abort → canonical 복원. 수동 승격 없음.

## 5. 판정기 generation 정규화 (2차 검증 결함 수정)

`guarded_cleanup_gate.norm_generation`: 입력 계약 = `int`(bool 제외) 또는 ASCII 10진 문자열(앞자리 0 없음, `"0"`만 허용), 범위 0 ~ 2^63−1. `"274"`·`274` → 274. `"0274"`·`"274.0"`·`"abc"`·`None`·음수·범위 초과·bool·float·전각 숫자·공백 포함·빈 문자열 → 오류(판정 불명 = 실패). 이후 모든 generation 비교는 이 함수를 거친다(특정 실행 예외 없음). 2차 검증 원본 `verdict.json`·결과 파일은 수정·재생성하지 않는다.

## 6. 자동 판정 correction 규칙 (공식 비교 전 고정)

자동 판정 결과는 원본 그대로 보존한다. 자동 판정과 사전 계약 기준이 충돌할 때 재판정은 다음을 **모두** 만족할 때만 한다.
1. 사전 기준 문구가 명확하다.
2. 오류가 결과값이나 arm에 의존하지 않는 결정적 구현 결함이다.
3. 원본 evidence만으로 기준을 다시 판정할 수 있다.
4. 원래 verdict를 수정·삭제하지 않는다.
5. 별도 correction/review record를 만든다.
6. 고친 판정 로직은 특정 실행이 아니라 이후 모든 실행에 똑같이 적용한다.
7. 수정 코드와 테스트는 공식 ab01 전에 동결한다.
결과가 마음에 들지 않는다는 이유의 재판정은 금지한다.

## 7. A/B 동등성 기록(실행 계약 `conditions`, A·B 같은 값이어야 유효)

probe 설정 경로·sha256(1 RPS, max_tokens=1), probe 이미지·ID, 압박 단계(1·2·3·4 workers × 90초), 관찰 600초, 모델 아티팩트 해시·threshold, 탐지 규칙(3연속·쿨다운 60초), 시작 상태 규칙(`canonical_healthy_r2`), 시작 canonical 상태(사전검사 기록), 시작 active 노드, preview 준비 방식(같은 wrapper·함수). 서비스 확인 기준은 guarded 정책 이미지 상수(A는 승격이 없어 해당 없음). arm 차이는 정책 모드(record_only/act)와 그에 따른 실제 조치·B 유예 420뿐이다.

## 8. ab01 전 남은 조건(변경 없음 + 추가)

1. ab90a·ab90b rehearsal 완료·보고(효과 해석 없음).
2. 분석기(r1 §9) 구현·테스트·Ponytail·sha256 동결 — generation 비교는 §5 함수 사용.
3. 공식 B 승인 기록(`runs` = 승인 범위) — 사용자 채팅 승인.
