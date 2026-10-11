# S3 KV pressure feasibility pilot — 사전등록 초안 (DRAFT, 동결 아님, 2026-10-11)

> 상태: **DRAFT**. 실행 권고 아님 - 계획 문서 판정은 S3_PILOT_DESIGN_NOT_FEASIBLE(`S3-KV-PRESSURE-ENVIRONMENT-PLAN.md` 7절).
> 이 초안은 사용자가 그 판단을 직접 확인하고 싶을 때 쓸 수 있도록 조건을 결과 보기 전에 적어 둔 것이다. 실행·동결은 별도 사용자 승인 필요.
> detector(c3-A·CPU 임계값·새 모델) 점수는 이 pilot에서 계산하지도 보지도 않는다. 결과는 공식 증거가 아니다(탐색용).

## 1. 질문
현재 CPU-vLLM의 KV 예산만 Mild로 줄였을 때, SLO v3가 깨지기 전에 KV 압박 상태(선점·용량 부족 대기, KV 사용률은 서술용)가 재현 가능하게 관찰되는가?

## 2. 고정 환경 변경 (승인 시 1개만)
- Rollout 인자에 `--kv-cache-memory-bytes=94371840` 추가(60 블록 × 128 토큰 = 7,680 토큰). 그 밖의 인자·자원·이미지·scrape 설정 불변.
- 적용 확인: 시작 로그 "Explicitly set (…) GiB for KV cache", `vllm:cache_config_info`의 num_gpu_blocks·kv_cache_size_tokens 기록.
- pilot 종료 후 원래 인자로 복원하고 복원 확인(정상 Rollout·hash·delay 30).

## 3. 단계
1. NEW-ENV NORMAL BASELINE 2 세션(기존 정상 계약: probe 1 RPS·max_tokens=1·4200 s·record_only).
2. pressure rep 2회: probe 그대로 + 별도 Pod의 pressure 생성기.
   - 요청: `/v1/completions`, 결정적 고유 prompt 약 512 토큰(seed 고정 생성, 첫 128 토큰부터 서로 다르고 전체 pilot에서 한 번씩만 사용) + max_tokens 128, `ignore_eos=true`(생성 길이 고정). 요청당 실제 prompt 토큰 수를 counter 증가분으로 기록.
   - 동시 수 단계 {1, 2, 4, 8, 13}(closed-loop: 끝나면 다음 요청; 13은 60블록을 넘는 첫 동시 수 - 선점이 가능한 유일한 단계), 단계당 600 s, 단계 사이 120 s 휴지.
   - 단계 중 SLO v3 episode가 시작되면 그 단계 끝까지 기록하고 다음 단계는 실행하지 않는다.

## 4. 사전 선언 지표 (입력 후보 상태값만)
KV 압박 판정 지표: `vllm:num_preemptions_total` rate, `vllm:num_requests_waiting_by_reason{reason="capacity"}`. 서술용: `vllm:kv_cache_usage_perc`, waiting(전체·deferred), running, CPU·memory, 토큰·iteration rate. 모든 질의 `job="vllm-active"`. outcome 전용: SLO v3, probe 지연, 요청 실패. 보조: vLLM 로그 10 s 주기 줄.

## 5. 성공·중단 기준
`S3-KV-PRESSURE-ENVIRONMENT-PLAN.md` 10절(성공 5개 모두)·11절(중단) 그대로. 실패 시 예산을 더 줄이지 않는다.

## 6. 안전
- 클러스터 변경은 2절의 인자 1개와 그 복원뿐. promotion·recovery·Service 변경 없음, guarded 정책 record_only, 기존 recovery-policy 격리 유지 여부는 실행 승인 시 사용자가 정함.
- 정상 기준선에서 SLO 위반 또는 요청 실패가 나오면 즉시 중단하고 원래 인자로 복원.
