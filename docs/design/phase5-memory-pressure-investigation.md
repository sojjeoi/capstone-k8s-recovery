# Phase 5 — 점진적 메모리 압력 시나리오 조사 기록

**일시**: 2026-09-04
**대상**: `chaos/scenario-progressive-memory-pressure.yaml`, `chaos/scenario-progressive-memory-pressure-debug.yaml`
**목적**: "메모리 압박이 실제로 vLLM 서비스를 실패시키는가"를 추측이 아니라 커널 레벨 증거와 실제 요청 테스트로 검증

---

## 1. 시행착오 요약

| 시도 | 설정 | 관측 결과 |
|---|---|---|
| 1차 | 단일 StressChaos, 2GB, 10분 | 목표치로 즉시 점프 후 유지(계단식 아님, 즉시 도달) |
| 2차 | Workflow Serial 4단계(500MB→1GB→1.5GB→2GB) | 단계별로 값이 오르는 것처럼 보였으나(3765→4243→4721Mi), 전환이 0.1초 단위라 실제로는 매 단계 처음부터 새로 시작하는 것이었음(후속 조사로 확인) |
| 3차 | Workflow 5단계 추가(3.5GB, 한도 초과 목표) | `restartCount=0` 유지, 실패 없이 워크플로우 정상 종료(`WorkflowAccomplished`) — 목표만큼 못 오른 것으로 보임 |
| 4차 | 단일 StressChaos 디버그, 5GB→workers 2, 실시간 5초 간격 관찰 | 값이 3200Mi~6038Mi 사이를 반복 순환(오르고 baseline 근처로 떨어지고 다시 오름) |

3~4차까지는 "크기를 더 키우면 되겠지"라는 가정으로 파라미터만 반복 조정했으나 해결되지 않았음. **근본 원인을 확인하지 않고 우회 방법만 바꾸는 접근이었다는 지적을 받고, 커널 로그(dmesg)를 직접 확인하는 것으로 전환.**

---

## 2. 근본 원인 (커널 OOM 로그 기반 확정)

워커 노드(`sj-worker`)의 `dmesg -T`에서 확인한 원문:

```
[Fri Sep  4 11:42:11 2026] oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=cri-containerd-80f7475c8c8014bfeb07a5e6453df2d72cdb94cc3baf8eddeab74e699d207775.scope,mems_allowed=0,oom_memcg=/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod4b5bf7cc_0552_4e08_bf20_995280716dff.slice,task_memcg=/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod4b5bf7cc_0552_4e08_bf20_995280716dff.slice/cri-containerd-80f7475c8c8014bfeb07a5e6453df2d72cdb94cc3baf8eddeab74e699d207775.scope,task=memStress,pid=3839453,uid=0
[Fri Sep  4 11:42:11 2026] Memory cgroup out of memory: Killed process 3839453 (memStress) total-vm:3669180kB, anon-rss:2285812kB, file-rss:2164kB, shmem-rss:0kB, UID:0 pgtables:4564kB oom_score_adj:1000
[Fri Sep  4 11:42:12 2026] memStress invoked oom-killer: gfp_mask=0xcc0(GFP_KERNEL), order=0, oom_score_adj=1000
[Fri Sep  4 11:42:12 2026] memory: usage 6291456kB, limit 6291456kB, failcnt 79403
[Fri Sep  4 11:42:12 2026] memory+swap: usage 0kB, limit 9007199254740988kB, failcnt 0
[Fri Sep  4 11:42:12 2026] Memory cgroup out of memory: Killed process 3839615 (memStress) total-vm:3669180kB, anon-rss:2160472kB, file-rss:2164kB, shmem-rss:0kB, UID:0 pgtables:4320kB oom_score_adj:1000
```

### 해석

1. **`usage 6291456kB, limit 6291456kB`** — cgroup 사용량이 한도(6144Mi = rollout.yaml의 `memory: 6Gi`)와 정확히 일치하는 순간 커널이 개입함. 한도 설정과 실제 강제(enforcement)가 정확히 일치함을 확인.
2. **`Killed process ... (memStress) ... oom_score_adj:1000`** — 죽은 프로세스는 vLLM이 아니라 Chaos Mesh가 주입한 stress 프로세스(`memStress`) 자신. `oom_score_adj:1000`(가능한 최댓값)은 Chaos Mesh가 자기 stress 프로세스에 "OOM 발생 시 내가 최우선으로 죽어라"는 안전장치를 심어둔 것 — 카오스 도구가 실수로 테스트 대상(vLLM)을 죽이는 사고를 막기 위한 설계.
3. 이 때문에 "값이 올랐다 baseline 근처로 떨어졌다"를 반복한 패턴이 설명됨: 한도 도달 → memStress 자폭 → 메모리 해제 → chaos-daemon이 새 memStress 재시작 → 다시 상승, 의 반복.
4. **vLLM 컨테이너의 `restartCount`는 이 조사 전체에서 한 번도 증가하지 않음** — vLLM 프로세스 자체는 이 메커니즘으로 인해 생존이 보장됨.

### 이전 가설 정정

조사 중간에 "vLLM 자체가 압박 시 캐시를 줄이는 적응 동작을 하는 것 아니냐"는 가설을 세웠으나, 이는 틀린 추측이었음. 실제로는 vLLM은 이 과정에 전혀 관여하지 않고, Chaos Mesh의 자기 프로세스 보호 로직이 원인이었음. **추측을 검증 없이 결론으로 삼았던 것 자체가 이번 조사의 교훈.**

---

## 3. 서비스 영향 검증 (실제 요청 테스트)

메모리 압박이 진행 중인 상태(`stresschaos` 시작 후 3분 58초 경과, 당시 `kubectl top` 기준 메모리 5149Mi, CPU 1787m)에서 실제 추론 요청 전송:

```powershell
Invoke-WebRequest -Uri "http://localhost:8000/v1/completions" -Method Post `
  -ContentType "application/json" `
  -Body '{"model":"Qwen/Qwen2.5-0.5B-Instruct","prompt":"Hello","max_tokens":10}' `
  -TimeoutSec 15
```

**결과**: `FAIL time=15.02s error=작업 시간이 초과되었습니다` (타임아웃)

같은 시점에 `kubectl get pods`는 `1/1 Running`, `RESTARTS=0`으로 정상 표시됨 — **Pod 상태만 보면 멀쩡하지만, 실제 사용자 요청은 처리되지 않는 상태(silent degradation)** 를 직접 재현·확인.

---

## 4. Phase 8 실험 설계에 대한 함의

- 이 시나리오는 **무개입 대조군**(아무 개입 없이 두었을 때 `t_SLO_counterfactual`을 측정하는 조건)으로 유효함이 실측으로 확인됨 — 방금 수행한 테스트 자체가 그 리허설.
- Chaos Mesh의 `memStress`는 대상 애플리케이션(vLLM)을 절대 죽이지 않도록 설계되어 있음 — **"vLLM 프로세스 자체의 크래시"를 재현하고 싶다면 이 도구로는 불가능**하며, 별도 메커니즘(예: 컨테이너 한도를 일시적으로 낮추거나, Chaos Mesh 대신 직접 stress 도구를 주입)이 필요함. 현재 시나리오는 "죽지는 않지만 요청을 처리 못 하는 심각한 성능 저하"를 재현하는 것으로 범위를 명확히 함.
- 논문/보고서용 증거는 터미널 캡처보다 **Prometheus/Grafana로 메모리 사용량과 요청 실패율(또는 지연시간)을 같은 시간축에 그래프로 겹쳐서** 제시하는 것이 더 설득력 있음 — Phase 8 반복 실험 시 이 방식으로 캡처할 것.
- dmesg 커널 로그는 노드의 링버퍼에 있어 시간이 지나면 유실되므로, 위 원문을 이 문서에 영구 보존함.
