"""guarded 정책의 추가 전용 조치 원장(ledger)과 단일 기록자 잠금 - 새 파일.

원장은 상태 파일과 별개로 PVC의 고정 경로에 둔다. 상태 파일을 새로 만들거나 run_id를 바꿔도 이전 조치를 우회하지
못하게 하는 근거 기록이다.
- 기록: intent(승격 명령 전, fsync) → result(명령·검증 결과) → service_result(서비스 확인) → resolution(수동 해결).
- 재구성(reconstruct): target_key별 마지막 상태. intent만 있고 result가 없으면 'unknown_after_restart'(차단).
- 손상(JSON 아닌 줄·필수 필드 없음)·파일 없음은 자동으로 빈 원장으로 만들지 않는다(LedgerUnavailable).
  최초 생성은 create=True로 명시할 때만 한다.
- 원장과 상태 파일이 다르면 원장이 우선이다(원장에 있는 조치는 새 상태 파일에서도 이어진다).

단일 기록자 잠금(SingleWriterLock): 같은 PVC 경로에 O_EXCL로 잠금 파일을 만든다. 이미 있으면(다른 프로세스가 실행 중이거나
이전 프로세스가 비정상 종료) 시작을 거부한다 - 비정상 종료 뒤의 잠금은 사람이 확인하고 지운다(fail-closed).
Kubernetes 단일 복제·Recreate만으로는 두 프로세스 동시 실행을 완전히 배제할 수 없어 이 잠금을 함께 쓴다.
"""
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

TERMINAL_OK = ("verified", "resolved")


class LedgerUnavailable(Exception):
    pass


class LedgerWriteError(Exception):
    pass


class LockHeld(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


STATUSES = {"result": {"verified_service_pending", "verified", "unverified", "unknown"},
            "service_result": {"verified", "verified_service_failed", "verified_service_unknown"},
            "resolution": {"resolved"}, "aborted": {"aborted_adapter_precheck", "aborted_final_check"}}


def _valid(rec) -> bool:
    """사건별 필수 필드·허용 상태. 문법은 맞지만 상태가 빠진 기록이 차단 근거를 지우지 못하게 한다."""
    if not isinstance(rec, dict):
        return False
    ev = rec.get("event")
    if ev == "ledger_created":
        return True
    if ev not in ("intent", *STATUSES) or not isinstance(rec.get("target_key"), str) or not rec["target_key"]:
        return False
    return ev == "intent" or rec.get("status") in STATUSES[ev]


class Ledger:
    def __init__(self, path: Path, *, create: bool = False):
        self.path = Path(path)
        if not self.path.exists():
            if not create:
                raise LedgerUnavailable("ledger_missing")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.append({"event": "ledger_created"})
        self.read_all()  # 손상이면 여기서 거부

    def append(self, rec: dict) -> None:
        line = json.dumps({"at": _now(), **rec}, ensure_ascii=False, default=str)
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as e:
            raise LedgerWriteError(f"{type(e).__name__}: {e}") from e

    def read_all(self) -> list:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as e:
            raise LedgerUnavailable(f"ledger_unreadable: {type(e).__name__}") from e
        out = []
        for i, ln in enumerate(text.splitlines(), 1):
            if not ln.strip():
                continue
            try:
                rec = json.loads(ln)
            except ValueError as e:
                raise LedgerUnavailable(f"ledger_corrupt_line_{i}") from e
            if not _valid(rec):
                raise LedgerUnavailable(f"ledger_bad_record_line_{i}")
            out.append(rec)
        if text and not text.endswith("\n"):
            raise LedgerUnavailable("ledger_partial_last_line")
        if not out or out[0]["event"] != "ledger_created":  # 생성 기록부터 잘린 원장(빈 파일 포함)은 믿지 않는다
            raise LedgerUnavailable("ledger_truncated_or_empty")
        return out

    def reconstruct(self) -> dict:
        """target_key별 마지막 조치 상태."""
        acts = {}
        for r in self.read_all():
            tk = r.get("target_key")
            ev = r["event"]
            if not tk:
                continue
            a = acts.setdefault(tk, {"run_id": r.get("run_id"), "status": None, "events": []})
            a["events"].append(ev)
            if ev == "intent":
                a["status"] = "in_progress"
                a["run_id"] = r.get("run_id")
            elif ev in ("result", "service_result", "resolution", "aborted"):
                a["status"] = r.get("status")
        for a in acts.values():
            if a["status"] == "in_progress":
                a["status"] = "unknown_after_restart"
        return acts


class LockOwnershipError(Exception):
    """잠금 파일의 소유 토큰이 이 프로세스의 것이 아님 - 지우지 않는다."""


class LockReleaseError(Exception):
    """잠금을 풀지 못함(읽기·삭제 실패 등) - 잠금 파일이 남아 다음 시작이 거부될 수 있다(사람 확인)."""


class SingleWriterLock:
    """O_EXCL 잠금 파일 + 소유 토큰. release()는 파일의 토큰이 자신이 쓴 값과 같을 때만 지운다.
    비정상 종료로 남은 잠금은 자동으로 지우지 않는다(다음 acquire가 LockHeld - 사람이 확인하고 지운다)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd = None
        self.token = None
        self.owner = None

    def acquire(self, owner: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as e:
            try:
                held = self.path.read_text(encoding="utf-8")[:300]
            except OSError:
                held = "?"
            raise LockHeld(f"single_writer_lock_held: {held}") from e
        token = uuid.uuid4().hex
        os.write(self.fd, json.dumps({"owner": owner, "pid": os.getpid(), "at": _now(), "token": token}).encode("utf-8"))
        os.fsync(self.fd)
        self.token, self.owner = token, owner

    def held(self) -> bool:
        """이 프로세스가 잠금 파일 핸들을 열고 있음(현재 경로의 잠금이 내 것이라는 뜻은 아님 - owns() 참고)."""
        return self.fd is not None

    def owns(self) -> bool:
        """핸들이 열려 있고, 현재 경로의 잠금 파일 토큰이 내가 쓴 값과 같음. 확인할 수 없으면 False."""
        if self.fd is None:
            return False
        try:
            cur = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(cur, dict) and cur.get("token") == self.token

    def release(self) -> dict:
        """자신이 획득한 잠금만 지운다. 실패는 예외로 올린다(조용히 무시하지 않음)."""
        if self.fd is None:
            raise LockReleaseError("lock_not_held_by_this_process")
        try:
            cur = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise LockReleaseError(f"lock_unreadable - 지우지 않음: {type(e).__name__}: {e}") from e
        if not isinstance(cur, dict) or cur.get("token") != self.token:
            raise LockOwnershipError(f"lock_owned_by_other - 지우지 않음: {str(cur)[:200]}")
        os.close(self.fd)
        self.fd = None
        try:
            self.path.unlink()
        except OSError as e:
            raise LockReleaseError(f"lock_unlink_failed - 잠금 파일이 남음(사람 확인): {type(e).__name__}: {e}") from e
        return {"released": True, "owner": self.owner, "token": self.token}
