from __future__ import annotations

import asyncio
import json
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import httpx

from storage import BOARD_HISTORY_LIMIT, PALETTE, board_meta, utc_now
from world import (
    COMPUTER_STATIONS,
    HEARING_RADIUS,
    MEETING_SEATS,
    TOOL_SPOTS,
    distance,
    direction_from_delta,
    is_blocked,
    navigate_toward,
    redact_secrets,
    target_tool_from_command,
)


TOOL_LABELS = {"computer": "컴퓨터", "whiteboard": "화이트보드", "meeting-room": "회의실"}
AGENT_IDS = ("minji", "doyun", "harin", "jun")
MAX_ATTEMPTS = 3


def _event(plan_id: str, event_type: str, title: str, result: str = "success", **values: Any) -> dict:
    return {
        "id": str(uuid.uuid4()), "planId": plan_id, "at": utc_now(), "type": event_type,
        "title": title, "result": result, **values,
    }


def _message(speaker_id: str, speaker: str, body: str, kind: str, heard_by: list[str], plan_id: str | None = None, origin_position: dict | None = None) -> dict:
    return {
        "id": str(uuid.uuid4()), "speakerId": speaker_id, "speaker": speaker,
        "body": redact_secrets(body)[:3000], "at": utc_now(), "kind": kind,
        "heardBy": heard_by, "planId": plan_id, "originPosition": deepcopy(origin_position),
    }


def _ensure_player(world: dict, actor_id: str) -> dict:
    players = world.setdefault("players", {})
    if actor_id not in players:
        now = utc_now()
        players[actor_id] = {"position": {"x": 49.0, "y": 54.0}, "facing": "south", "updatedAt": now}
    return players[actor_id]


def _plan(command: str, tool: str) -> list[dict]:
    concise = command.replace("@", "").strip()[:60] or "대표 요청 수행"
    label = TOOL_LABELS[tool]
    return [
        {"id": str(uuid.uuid4()), "title": f"요청 이해: {concise}", "detail": "목표와 완료 조건을 서버 작업으로 확정", "status": "done"},
        {"id": str(uuid.uuid4()), "title": f"{label}(으)로 이동", "detail": "공유 월드 좌표에서 순간이동 없이 이동", "status": "active", "tool": tool},
        {"id": str(uuid.uuid4()), "title": f"{label}에서 실제 작업 실행", "detail": "도구에 전달한 입력과 반환 출력을 기록", "status": "pending", "tool": tool},
        {"id": str(uuid.uuid4()), "title": "완료 조건 검증·리포트·교훈 저장", "detail": "검증 후에만 완료 처리", "status": "pending"},
    ]


def _public_decision(command: str, tool: str) -> dict:
    return {
        "observations": [f"대표의 명령을 수신함: {redact_secrets(command)[:180]}", f"필요한 물리 도구: {TOOL_LABELS[tool]}"],
        "objective": "검증 가능한 산출물과 최종 리포트를 생성한다.",
        "chosenAction": f"{TOOL_LABELS[tool]}까지 이동한 뒤 작업을 실행한다.",
        "rationale": "사무실 규칙상 물리적 도구에 도착한 에이전트만 해당 작업을 수행할 수 있다.",
        "alternatives": [{"action": "즉시 결과를 작성", "rejectedBecause": "도구 실행과 근거가 없는 결과가 되기 때문"}],
        "evidenceRefs": ["대표 명령", "공유 월드 도구 규칙"],
        "blockers": [], "confidence": 0.92,
    }


def _station_for(agent_id: str) -> dict:
    index = AGENT_IDS.index(agent_id) if agent_id in AGENT_IDS else 0
    return COMPUTER_STATIONS[index]


def _target_for(agent_id: str, tool: str) -> tuple[dict, str | None]:
    if tool == "computer":
        station = _station_for(agent_id)
        return deepcopy(station["position"]), station["id"]
    if tool == "meeting-room":
        seat = next((item for item in MEETING_SEATS if item["id"] == f"seat-{agent_id}"), MEETING_SEATS[0])
        return deepcopy(seat["position"]), seat["id"]
    return deepcopy(TOOL_SPOTS["whiteboard"]), None


def _safe_json(value: str) -> dict | None:
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, json.JSONDecodeError):
        start, end = value.find("{"), value.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(value[start:end + 1])
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
        return None


def _normalize_summary(value: Any, fallback: dict) -> dict:
    item = value if isinstance(value, dict) else {}
    alternatives = item.get("alternatives") if isinstance(item.get("alternatives"), list) else fallback["alternatives"]
    safe_alternatives = []
    for alternative in alternatives[:4]:
        if isinstance(alternative, dict):
            safe_alternatives.append({
                "action": redact_secrets(str(alternative.get("action", "다른 접근")))[:240],
                "rejectedBecause": redact_secrets(str(alternative.get("rejectedBecause", "현재 목표에 덜 적합")))[:360],
            })
    def strings(key: str, default: list[str]) -> list[str]:
        raw = item.get(key)
        return [redact_secrets(str(entry))[:500] for entry in raw[:8]] if isinstance(raw, list) else default
    confidence = item.get("confidence", fallback["confidence"])
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = fallback["confidence"]
    return {
        "observations": strings("observations", fallback["observations"]),
        "objective": redact_secrets(str(item.get("objective", fallback["objective"])))[:700],
        "chosenAction": redact_secrets(str(item.get("chosenAction", fallback["chosenAction"])))[:700],
        "rationale": redact_secrets(str(item.get("rationale", fallback["rationale"])))[:1000],
        "alternatives": safe_alternatives or fallback["alternatives"],
        "evidenceRefs": strings("evidenceRefs", fallback["evidenceRefs"]),
        "blockers": strings("blockers", fallback["blockers"]),
        "confidence": confidence,
    }


class WorldEngine:
    def __init__(self, store):
        self.store = store
        self.lock = asyncio.Lock()
        self.runner_task: asyncio.Task | None = None
        self.executions: dict[str, asyncio.Task] = {}
        self.closed = False

    async def start(self) -> None:
        await self.store.initialize()
        await self._recover_executions()
        self.runner_task = asyncio.create_task(self._run_loop(), name="cashcow-world-loop")

    async def stop(self) -> None:
        self.closed = True
        if self.runner_task:
            self.runner_task.cancel()
        for task in self.executions.values():
            task.cancel()
        await asyncio.gather(*(list(self.executions.values()) + ([self.runner_task] if self.runner_task else [])), return_exceptions=True)

    async def _recover_executions(self) -> None:
        async with self.lock:
            def recover(data: dict):
                for job in data["jobs"].values():
                    if job.get("status") == "active" and job.get("phase") == "executing":
                        job["phase"] = "working"
                        job["updatedAt"] = utc_now()
                        job["events"].append(_event(job["id"], "status", "서버 재시작 후 안전한 실행 단계 복구", "working", output="검색·격리 코드 실행처럼 재실행 가능한 단계만 다시 검증합니다."))
                return True
            await self.store.mutate(recover)

    async def snapshot(self, actor_id: str | None = None) -> dict:
        async with self.lock:
            if actor_id:
                await self.store.mutate(lambda data: _ensure_player(data["world"], actor_id))
            data = await self.store.snapshot()
            return self._public_snapshot(data, actor_id)

    async def board_revision(self, version: int) -> dict:
        async with self.lock:
            data = await self.store.snapshot()
            document = data.get("boardVersions", {}).get(str(version))
            if not document:
                raise KeyError("요청한 화이트보드 버전을 찾을 수 없습니다.")
            return deepcopy(document) | {"history": deepcopy(data["boardHistory"][:BOARD_HISTORY_LIMIT])}

    async def job_detail(self, job_id: str) -> dict:
        async with self.lock:
            data = await self.store.snapshot()
            job = data["jobs"].get(job_id)
            if not job:
                raise KeyError("요청한 작업 감사 로그를 찾을 수 없습니다.")
            report = next((item for item in data["reports"] if item["planId"] == job_id), None)
            return {"job": deepcopy(job), "report": deepcopy(report)}

    def _public_snapshot(self, data: dict, actor_id: str | None = None) -> dict:
        world = deepcopy(data["world"])
        jobs = data["jobs"]
        reports_by_plan = {item["planId"]: item for item in data["reports"]}
        for agent in world["agents"]:
            relevant = sorted([job for job in jobs.values() if agent["id"] in job.get("participantIds", [job["agentId"]])], key=lambda item: item["createdAt"], reverse=True)
            assigned_id = agent.get("activeJobId") or agent.get("supportingJobId")
            current = jobs.get(assigned_id) if assigned_id else None
            if not current:
                current = next((job for job in reversed(relevant) if job["status"] == "queued"), relevant[0] if relevant else None)
            if current:
                agent["plan"] = deepcopy(current["plan"])
                agent["events"] = deepcopy(current["events"][-40:])
                agent["activeJob"] = {
                    "id": current["id"], "command": current["command"], "status": current["status"],
                    "phase": current["phase"], "createdAt": current["createdAt"],
                    "assignment": "owner" if current["agentId"] == agent["id"] else "supporter",
                }
                if current["id"] in reports_by_plan:
                    agent["report"] = deepcopy(reports_by_plan[current["id"]])
            agent["memories"] = agent.get("memories", [])[-20:][::-1]
        board = deepcopy(data["board"])
        board["history"] = deepcopy(data["boardHistory"][:30])
        player = deepcopy(world.get("players", {}).get(actor_id)) if actor_id else None
        messages = deepcopy(data["messages"][-60:])
        if player:
            messages = [message for message in messages if message.get("kind") == "system" or message.get("speakerId") == actor_id or (message.get("originPosition") and distance(player["position"], message["originPosition"]) <= HEARING_RADIUS)]
        return {
            "worldVersion": world["version"], "serverTime": utc_now(), "agentsPaused": world["paused"],
            "agents": world["agents"], "messages": messages, "player": player,
            "reports": deepcopy(sorted(data["reports"], key=lambda item: item["createdAt"], reverse=True)[:30]),
            "whiteboard": board,
        }

    async def enqueue(self, agent_id: str, command: str, actor_id: str, idempotency_key: str) -> dict:
        command = redact_secrets(command.strip())[:800]
        if not command:
            raise ValueError("명령은 비어 있을 수 없습니다.")
        async with self.lock:
            def create(data: dict):
                world = data["world"]
                if world["paused"]:
                    raise RuntimeError("대표가 에이전트 활동을 일시정지했습니다.")
                agent = next((item for item in world["agents"] if item["id"] == agent_id), None)
                if not agent:
                    raise ValueError("알 수 없는 에이전트입니다.")
                existing = next((job for job in data["jobs"].values() if job.get("actorId") == actor_id and job.get("idempotencyKey") == idempotency_key), None)
                if existing:
                    return {"jobId": existing["id"], "heardBy": existing.get("heardBy", []), "agentName": agent["name"], "status": existing["status"]}
                speaker_position = _ensure_player(world, actor_id)["position"]
                heard_by = [item["name"] for item in world["agents"] if distance(speaker_position, item["position"]) <= HEARING_RADIUS]
                if agent["name"] not in heard_by:
                    raise PermissionError(f"{agent['name']}은(는) 들을 수 있는 거리 밖에 있습니다.")
                now = utc_now()
                job_id = str(uuid.uuid4())
                tool = target_tool_from_command(command)
                participants = list(AGENT_IDS) if tool == "meeting-room" else [agent_id]
                job = {
                    "id": job_id, "actorId": actor_id, "idempotencyKey": idempotency_key, "heardBy": heard_by, "agentId": agent_id, "command": command,
                    "tool": tool, "participantIds": participants, "status": "queued", "phase": "queued",
                    "plan": _plan(command, tool), "events": [], "attempt": 0, "attemptResults": [],
                    "createdAt": now, "updatedAt": now, "completedAt": None,
                }
                decision = _public_decision(command, tool)
                job["events"].extend([
                    _event(job_id, "command", "대표 명령 수신", input=command, output=f"{TOOL_LABELS[tool]} 작업 큐에 영속 저장"),
                    _event(job_id, "decision", "초기 판단 요약", summary=decision, input=command, output=decision["chosenAction"]),
                ])
                data["jobs"][job_id] = job
                data["messages"].append(_message(actor_id, "대표님", command, "user", heard_by, job_id, speaker_position))
                data["messages"] = data["messages"][-60:]
                self._activate_waiting(data)
                world["version"] += 1
                world["updatedAt"] = now
                return {"jobId": job_id, "heardBy": heard_by, "agentName": agent["name"], "status": job["status"]}
            return await self.store.mutate(create)

    async def move_player(self, actor_id: str, target: dict, facing: str) -> dict:
        async with self.lock:
            def update(data: dict):
                world = data["world"]
                player = _ensure_player(world, actor_id)
                current = player["position"]
                desired = {"x": float(target["x"]), "y": float(target["y"])}
                gap = distance(current, desired)
                if gap > 1.6:
                    scale = 1.6 / gap
                    desired = {"x": current["x"] + (desired["x"] - current["x"]) * scale, "y": current["y"] + (desired["y"] - current["y"]) * scale}
                if is_blocked(desired):
                    desired = current
                safe_facing = facing if facing in ("north", "east", "south", "west") else direction_from_delta(desired["x"] - current["x"], desired["y"] - current["y"], player["facing"])
                if distance(current, desired) < 0.001 and safe_facing == player["facing"]:
                    return deepcopy(player)
                player.update({"position": desired, "facing": safe_facing, "updatedAt": utc_now()})
                world["version"] += 1
                world["updatedAt"] = player["updatedAt"]
                return deepcopy(player)
            return await self.store.mutate(update)

    def _activate_waiting(self, data: dict) -> None:
        world = data["world"]
        occupancies = world.setdefault("toolOccupancies", {})
        queued = sorted((job for job in data["jobs"].values() if job["status"] == "queued"), key=lambda item: item["createdAt"])
        for job in queued:
            participants = [next(item for item in world["agents"] if item["id"] == participant_id) for participant_id in job["participantIds"]]
            if any(agent.get("activeJobId") or agent.get("supportingJobId") for agent in participants):
                continue
            claims = []
            for participant in participants:
                _target, station = _target_for(participant["id"], job["tool"])
                claims.append(station or "whiteboard-main")
            if any(occupancies.get(station, {}).get("jobId") not in (None, job["id"]) for station in claims):
                continue
            now = utc_now()
            for station, participant in zip(claims, participants):
                occupancies[station] = {"jobId": job["id"], "agentId": participant["id"], "claimedAt": now}
            job["claimedStations"] = claims
            for participant in participants:
                target, station = _target_for(participant["id"], job["tool"])
                participant["target"] = target
                participant["toolStation"] = station
                participant["currentTool"] = None
                participant["focus"] = job["command"][:80]
                participant["activity"] = f"{TOOL_LABELS[job['tool']]}(으)로 이동 중"
                participant["progress"] = 8
                if participant["id"] == job["agentId"]:
                    participant["activeJobId"] = job["id"]
                else:
                    participant["supportingJobId"] = job["id"]
            job["status"] = "active"
            job["phase"] = "moving"
            job["updatedAt"] = now
            job["events"].append(_event(job["id"], "status", "공유 월드에서 이동 시작", "working", output=f"참여자 {', '.join(agent['name'] for agent in participants)}"))

    async def set_paused(self, paused: bool, actor_id: str) -> dict:
        if paused:
            for task in list(self.executions.values()):
                task.cancel()
        async with self.lock:
            def update(data: dict):
                world = data["world"]
                world["paused"] = paused
                world["version"] += 1
                world["updatedAt"] = utc_now()
                if paused:
                    for job in data["jobs"].values():
                        if job["status"] == "active" and job["phase"] == "executing":
                            job["phase"] = "working"
                            job["events"].append(_event(job["id"], "status", "대표가 실행을 일시정지", "blocked", output="현재 체크포인트를 저장하고 외부 호출을 중단했습니다."))
                data["messages"].append(_message("system", "시스템", "에이전트 활동이 일시정지됐습니다." if paused else "에이전트 활동이 재개됐습니다.", "system", [], None))
                return {"agentsPaused": paused, "updatedAt": world["updatedAt"], "updatedBy": actor_id}
            return await self.store.mutate(update)

    async def save_board(self, value: dict, actor_id: str, actor_name: str) -> dict:
        async with self.lock:
            def update(data: dict):
                board = data["board"]
                if int(value["expectedVersion"]) != int(board["version"]):
                    raise FileExistsError("다른 사용자가 먼저 수정했습니다. 최신 보드를 다시 불러와 주세요.")
                now = utc_now()
                existing_blocks = {block["id"]: block for block in board.get("textBlocks", [])}
                text_blocks = []
                for block in value["textBlocks"]:
                    previous = existing_blocks.get(block["id"])
                    if previous:
                        provenance = {key: previous.get(key) for key in ("authorType", "authorId", "authorName", "createdAt")}
                    else:
                        provenance = {"authorType": "user", "authorId": actor_id, "authorName": actor_name, "createdAt": now}
                    text_blocks.append(block | provenance | {"updatedAt": now, "lastEditorId": actor_id, "lastEditorName": actor_name})
                next_board = {
                    "title": redact_secrets(value["title"].strip())[:40], "width": 256, "height": 256,
                    "palette": value["palette"], "pixelsBase64": value["pixelsBase64"],
                    "textBlocks": text_blocks, "version": board["version"] + 1,
                    "authorType": "user", "authorId": actor_id, "authorName": actor_name,
                    "changeSummary": redact_secrets(value.get("changeSummary") or "사용자가 그림과 텍스트 영역을 편집")[:180],
                    "updatedAt": now,
                }
                data["board"] = next_board
                data.setdefault("boardVersions", {})[str(next_board["version"])] = deepcopy(next_board)
                data["boardHistory"].insert(0, board_meta(next_board))
                data["boardHistory"] = data["boardHistory"][:BOARD_HISTORY_LIMIT]
                retained = {str(item["version"]) for item in data["boardHistory"]}
                data["boardVersions"] = {key: document for key, document in data["boardVersions"].items() if key in retained}
                data["world"]["version"] += 1
                data["world"]["updatedAt"] = now
                return deepcopy(next_board) | {"history": deepcopy(data["boardHistory"])}
            return await self.store.mutate(update)

    async def apply_review(self, scores: dict, actor_id: str) -> dict:
        async with self.lock:
            def update(data: dict):
                now = utc_now()
                cycle_id = scores.get("cycleId", "current")
                applied = data["world"].setdefault("appliedReviews", {})
                if cycle_id in applied:
                    return {"reflectedAgents": 0, "actorId": actor_id, "alreadyApplied": True}
                lesson = redact_secrets(scores.get("comment") or f"개인 {scores['individual']}점·팀 {scores['team']}점 평가를 다음 행동에 반영한다.")[:1000]
                for agent in data["world"]["agents"]:
                    agent["score"] = round(((scores["individual"] + scores["team"]) / 10) * 100)
                    agent.setdefault("memories", []).append({
                        "id": str(uuid.uuid4()), "kind": "lesson", "at": now, "summary": lesson,
                        "evidence": f"weekly_review_{cycle_id}", "confidence": 0.95,
                    })
                    agent["memories"] = agent["memories"][-20:]
                data["world"]["version"] += 1
                data["world"]["updatedAt"] = now
                applied[cycle_id] = {"actorId": actor_id, "appliedAt": now, "individual": scores["individual"], "team": scores["team"]}
                return {"reflectedAgents": len(data["world"]["agents"]), "actorId": actor_id, "alreadyApplied": False}
            return await self.store.mutate(update)

    async def _run_loop(self) -> None:
        while not self.closed:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as error:
                print("world_tick_failed", type(error).__name__, str(error)[:300], flush=True)
            await asyncio.sleep(0.4)

    async def _tick(self) -> None:
        jobs_to_execute: list[str] = []
        async with self.lock:
            def advance(data: dict):
                world = data["world"]
                if world["paused"]:
                    return []
                changed = False
                now = datetime.now(timezone.utc)
                moving_jobs = [job for job in data["jobs"].values() if job["status"] == "active" and job["phase"] == "moving"]
                elapsed = 0.4
                if moving_jobs:
                    try:
                        previous = datetime.fromisoformat(world["lastTickAt"].replace("Z", "+00:00"))
                        elapsed = max(0.05, min(1.0, (now - previous).total_seconds()))
                    except (KeyError, ValueError):
                        elapsed = 0.4
                    world["lastTickAt"] = now.isoformat().replace("+00:00", "Z")
                for job in moving_jobs:
                    participants = [agent for agent in world["agents"] if agent["id"] in job["participantIds"]]
                    all_arrived = True
                    for agent in participants:
                        if distance(agent["position"], agent["target"]) <= 2.4:
                            continue
                        all_arrived = False
                        point, facing = navigate_toward(agent["position"], agent["target"], 2.15 * elapsed, agent["facing"])
                        agent["position"] = point
                        agent["facing"] = facing
                        agent["activity"] = f"{TOOL_LABELS[job['tool']]}(으)로 이동 중"
                        agent["progress"] = min(38, agent["progress"] + elapsed * 2)
                        changed = True
                    if all_arrived:
                        for agent in participants:
                            agent["currentTool"] = job["tool"]
                            agent["activity"] = f"{TOOL_LABELS[job['tool']]}에서 작업 준비"
                            agent["progress"] = 42
                        job["phase"] = "working"
                        job["plan"][1]["status"] = "done"
                        job["plan"][2]["status"] = "active"
                        job["events"].append(_event(job["id"], "status", f"{TOOL_LABELS[job['tool']]} 도착·점유 확인", "working", output="물리적 도착 좌표와 도구 점유를 검증했습니다."))
                        job["updatedAt"] = utc_now()
                        changed = True
                for job in data["jobs"].values():
                    if job["status"] == "active" and job["phase"] == "working" and job["id"] not in self.executions:
                        job["phase"] = "executing"
                        job["attempt"] += 1
                        job["updatedAt"] = utc_now()
                        jobs_to_execute.append(job["id"])
                        for agent in world["agents"]:
                            if agent["id"] in job["participantIds"]:
                                agent["activity"] = f"{TOOL_LABELS[job['tool']]}에서 실제 작업 중"
                                agent["progress"] = 55
                        changed = True
                if changed:
                    world["version"] += 1
                    world["updatedAt"] = utc_now()
                return jobs_to_execute
            jobs_to_execute = await self.store.mutate(advance)
        for job_id in jobs_to_execute:
            task = asyncio.create_task(self._execute_job(job_id), name=f"cashcow-job-{job_id}")
            self.executions[job_id] = task
            task.add_done_callback(lambda _task, identifier=job_id: self.executions.pop(identifier, None))

    async def _execute_job(self, job_id: str) -> None:
        try:
            data = await self.store.snapshot()
            job = deepcopy(data["jobs"].get(job_id))
            if not job:
                return
            agent = next(item for item in data["world"]["agents"] if item["id"] == job["agentId"])
            participants = [item for item in data["world"]["agents"] if item["id"] in job["participantIds"]]
            result = await self._groq_work(job, agent, participants, data.get("knowledge", []), data.get("reports", []))
            await self._complete_job(job_id, result)
        except asyncio.CancelledError:
            return
        except Exception as error:
            await self._fail_or_retry(job_id, error)

    async def _groq_work(self, job: dict, agent: dict, participants: list[dict], knowledge: list[dict], reports: list[dict]) -> dict:
        key = os.getenv("GROQ_API_KEY", "").strip()
        fallback_decision = _public_decision(job["command"], job["tool"])
        if not key:
            return {
                "reply": "요청은 실행했지만 Groq 키가 없어 모델 산출물을 만들 수 없었습니다.",
                "publicDecision": fallback_decision, "toolRuns": [], "collaboration": [],
                "report": {
                    "title": f"{agent['name']}의 작업 결과", "outcome": "partial",
                    "summary": "물리적 이동과 도구 점유는 완료했지만 모델 실행 키가 없어 산출물 생성이 제한됐습니다.",
                    "body": "## 실행 결과\n\n공유 월드 이동과 도구 점유를 검증했습니다. GROQ_API_KEY 설정 후 같은 명령을 다시 실행해야 합니다.",
                    "limitations": ["Groq 실행 키가 설정되지 않음"], "lessons": ["실행 전 외부 도구 자격 증명을 확인한다."],
                },
            }
        needs_code = any(word in job["command"] for word in ("코드", "스크립트", "계산", "분석", "자동화"))
        needs_search = any(word in job["command"] for word in ("인터넷", "검색", "최신", "출처", "조사", "웹"))
        needs_knowledge = "공용 정보" in job["command"] or "공유 정보" in job["command"]
        built_in_tool = "code_interpreter" if needs_code else "browser_search" if needs_search else None
        knowledge_matches = deepcopy(knowledge[:20]) if needs_knowledge else []
        work_prompt = {
            "agent": {key: agent[key] for key in ("name", "team", "role", "rank")},
            "command": job["command"], "physicalTool": TOOL_LABELS[job["tool"]],
            "participants": [{"id": item["id"], "name": item["name"], "role": item["role"]} for item in participants],
            "priorLessons": agent.get("memories", [])[-5:],
            "priorAttempts": job.get("attemptResults", [])[-2:],
            "sharedKnowledgeMatches": knowledge_matches,
            "recentVerifiedReports": [{key: item.get(key) for key in ("id", "planId", "agentId", "title", "outcome", "summary", "limitations", "createdAt")} for item in reports[-10:]],
            "sharedKnowledgeCapability": {
                "readAuthorized": True, "writeAuthorized": True,
                "behavior": "서버가 위 검색 결과를 이미 읽었고, completed 리포트는 같은 원자 트랜잭션으로 공용 정보에 등록한다. 결과가 비어 있어도 권한 오류가 아니며 대표 명령을 출처로 새 기록을 만들 수 있다.",
            } if needs_knowledge else None,
            "requirements": [
                "명령을 실제로 끝낼 수 있을 만큼 조사·계산·코드 실행을 수행한다.",
                "최신 정보에는 근거 URL을 포함하고, 확인하지 못한 사실은 만들지 않는다.",
                "실행 결과와 한계를 명확히 구분한다.",
                "sharedKnowledgeCapability이 있으면 권한이 이미 부여된 것이므로 권한 부족을 추측하지 않는다.",
                "명령·priorAttempts·sharedKnowledgeMatches·recentVerifiedReports·실제 도구 출력에 없는 수치, 취약점, 파일명, 테스트 결과를 만들지 않는다.",
            ],
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(110.0, connect=15.0)) as client:
            first_payload: dict[str, Any] = {
                "model": os.getenv("GROQ_WORK_MODEL", os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")),
                "temperature": 0.2, "max_completion_tokens": 3500, "reasoning_effort": "low",
                "messages": [
                    {"role": "system", "content": "당신은 지속 실행되는 오피스 에이전트의 실제 작업 엔진입니다. 숨은 사고과정은 출력하지 마세요. 제공된 근거와 허용된 도구만 사용해 작업을 수행하고, 근거에 없는 수치·취약점·파일·테스트 결과는 절대 만들지 마세요. 검증 가능한 결과·출처·한계를 한국어로 작성하세요."},
                    {"role": "user", "content": json.dumps(work_prompt, ensure_ascii=False)},
                ],
            }
            if built_in_tool:
                first_payload["tools"] = [{"type": built_in_tool}]
                first_payload["tool_choice"] = "required"
            first = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=first_payload,
            )
            first.raise_for_status()
            first_data = first.json()
            message = ((first_data.get("choices") or [{}])[0].get("message") or {})
            raw_content = redact_secrets(str(message.get("content") or ""))[:12000]
            executed_tools = message.get("executed_tools") if isinstance(message.get("executed_tools"), list) else []
            safe_tools = []
            if needs_knowledge:
                safe_tools.append({
                    "tool": "shared_knowledge.search", "input": job["command"],
                    "output": json.dumps(knowledge_matches, ensure_ascii=False)[:9000] if knowledge_matches else "일치하는 기존 공용 정보가 없습니다.",
                })
            for entry in executed_tools[:12]:
                if not isinstance(entry, dict):
                    continue
                safe_tools.append({
                    "tool": redact_secrets(str(entry.get("type") or "built_in_tool"))[:80],
                    "input": redact_secrets(str(entry.get("arguments") or ""))[:6000],
                    "output": redact_secrets(str(entry.get("output") or entry.get("search_results") or ""))[:9000],
                })
            actual_collaboration = []
            for collaborator in participants:
                if collaborator["id"] == agent["id"]:
                    continue
                collaboration_input = {
                    "command": job["command"], "primaryAgent": agent["name"],
                    "yourIdentity": {key: collaborator[key] for key in ("name", "team", "role", "rank")},
                    "primaryResult": raw_content,
                    "request": "독립적으로 결과를 검토하고 빠진 근거·위험·다음 플랜을 한국어로 답하세요. 숨은 사고과정은 쓰지 마세요.",
                }
                collaborator_response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": os.getenv("GROQ_STRUCTURED_MODEL", "openai/gpt-oss-120b"), "temperature": 0.2,
                        "max_completion_tokens": 1000,
                        "messages": [
                            {"role": "system", "content": f"당신은 {collaborator['name']}이며 {collaborator['role']}입니다. 전달받은 작업을 독립적으로 검토해 공개 가능한 응답만 작성하세요."},
                            {"role": "user", "content": json.dumps(collaboration_input, ensure_ascii=False)},
                        ],
                    },
                )
                collaborator_response.raise_for_status()
                response_text = redact_secrets(str((((collaborator_response.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "")))[:4000]
                actual_collaboration.append({
                    "toAgentId": collaborator["id"], "purpose": "독립 검토와 플랜 조정",
                    "message": json.dumps(collaboration_input, ensure_ascii=False), "response": response_text,
                })
            structure_prompt = {
                "command": job["command"], "agent": agent["name"], "participants": [item["name"] for item in participants],
                "rawVerifiedResult": raw_content, "actualToolRuns": safe_tools, "actualCollaborationTurns": actual_collaboration,
            }
            second = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": os.getenv("GROQ_STRUCTURED_MODEL", "openai/gpt-oss-120b"), "temperature": 0.1,
                    "max_completion_tokens": 2600, "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": "검증된 작업 결과를 한국어 공개 감사 JSON으로 구조화하세요. 숨은 사고과정이나 비공개 chain-of-thought는 쓰지 마세요. 키: reply, publicDecision(observations[],objective,chosenAction,rationale,alternatives[{action,rejectedBecause}],evidenceRefs[],blockers[],confidence 0..1), report(title,outcome completed|partial|failed,summary,body,limitations[],lessons[]). actualCollaborationTurns의 실제 응답을 반영하되 rawVerifiedResult에 없는 사실은 추가하지 마세요."},
                        {"role": "user", "content": json.dumps(structure_prompt, ensure_ascii=False)},
                    ],
                },
            )
            second.raise_for_status()
            structured_content = (((second.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "{}")
        structured = _safe_json(structured_content) or {}
        report = structured.get("report") if isinstance(structured.get("report"), dict) else {}
        outcome = report.get("outcome") if report.get("outcome") in ("completed", "partial", "failed") else ("completed" if raw_content else "partial")
        normalized = {
            "reply": redact_secrets(str(structured.get("reply") or f"{agent['name']}입니다. 작업을 마치고 리포트를 저장했습니다."))[:1000],
            "publicDecision": _normalize_summary(structured.get("publicDecision"), fallback_decision),
            "toolRuns": safe_tools or [{"tool": TOOL_LABELS[job["tool"]], "input": job["command"], "output": raw_content[:9000]}],
            "collaboration": actual_collaboration,
            "registerKnowledge": needs_knowledge,
            "report": {
                "title": redact_secrets(str(report.get("title") or f"{agent['name']}의 {TOOL_LABELS[job['tool']]} 작업 리포트"))[:240],
                "outcome": outcome,
                "summary": redact_secrets(str(report.get("summary") or raw_content[:700] or "작업 결과가 제한적입니다."))[:1800],
                "body": redact_secrets(str(report.get("body") or raw_content or "산출물이 없습니다."))[:12000],
                "limitations": [redact_secrets(str(item))[:700] for item in (report.get("limitations") if isinstance(report.get("limitations"), list) else [])[:8]],
                "lessons": [redact_secrets(str(item))[:700] for item in (report.get("lessons") if isinstance(report.get("lessons"), list) else [])[:8]],
            },
        }
        return normalized

    async def _complete_job(self, job_id: str, result: dict) -> None:
        async with self.lock:
            def complete(data: dict):
                job = data["jobs"].get(job_id)
                if not job or job["status"] != "active":
                    return False
                world = data["world"]
                agent = next(item for item in world["agents"] if item["id"] == job["agentId"])
                job["events"].append(_event(job_id, "decision", "실행 후 판단 요약", summary=result["publicDecision"], input="검증된 도구 결과", output=result["publicDecision"]["chosenAction"]))
                for tool_run in result["toolRuns"]:
                    job["events"].append(_event(job_id, "tool_request", f"{tool_run['tool']}에 전달", tool=tool_run["tool"], input=tool_run["input"], output="도구 실행 요청 전달"))
                    job["events"].append(_event(job_id, "tool_result", f"{tool_run['tool']} 반환", tool=tool_run["tool"], input=tool_run["input"], output=tool_run["output"]))
                participant_names = {item["id"]: item["name"] for item in world["agents"]}
                participant_positions = {item["id"]: item["position"] for item in world["agents"]}
                for raw in result["collaboration"][:12]:
                    if not isinstance(raw, dict):
                        continue
                    recipient_id = str(raw.get("toAgentId") or "")
                    if recipient_id not in participant_names or recipient_id == agent["id"]:
                        continue
                    message = redact_secrets(str(raw.get("message") or "진행 결과 검토를 요청합니다."))[:1500]
                    response = redact_secrets(str(raw.get("response") or "검토 결과를 공유했습니다."))[:1500]
                    purpose = redact_secrets(str(raw.get("purpose") or "협업 검토"))[:240]
                    job["events"].append(_event(job_id, "agent_message", purpose, recipientAgentId=recipient_id, recipientName=participant_names[recipient_id], input=message, output=response))
                    data["messages"].append(_message(agent["id"], agent["name"], message, "agent", [participant_names[recipient_id]], job_id, agent["position"]))
                    data["messages"].append(_message(recipient_id, participant_names[recipient_id], response, "agent", [agent["name"]], job_id, participant_positions[recipient_id]))
                now = utc_now()
                report_value = result["report"]
                if report_value["outcome"] != "completed" and int(job.get("attempt", 0)) < MAX_ATTEMPTS:
                    job.setdefault("attemptResults", []).append({
                        "attempt": job["attempt"], "outcome": report_value["outcome"],
                        "summary": report_value["summary"], "limitations": report_value["limitations"], "at": now,
                    })
                    job["attemptResults"] = job["attemptResults"][-MAX_ATTEMPTS:]
                    job["events"].append(_event(job_id, "verification", f"{job['attempt']}차 결과 미충족 · 자동 재계획", "blocked", output=report_value["summary"]))
                    job["phase"] = "working"
                    job["updatedAt"] = now
                    job["plan"][2]["status"] = "active"
                    job["plan"][3]["status"] = "blocked"
                    job["plan"][3]["detail"] = f"미충족 사유를 반영해 {job['attempt'] + 1}차 실행을 준비"
                    for participant in world["agents"]:
                        if participant["id"] in job["participantIds"]:
                            participant["activity"] = "결과 미충족 · 다음 행동 재계획"
                            participant["progress"] = min(82, 56 + job["attempt"] * 8)
                    world["version"] += 1
                    world["updatedAt"] = now
                    return True
                report = {
                    "id": str(uuid.uuid4()), "planId": job_id, "agentId": agent["id"],
                    "title": report_value["title"], "outcome": report_value["outcome"],
                    "summary": report_value["summary"], "body": report_value["body"],
                    "limitations": report_value["limitations"], "lessons": report_value["lessons"], "createdAt": now,
                }
                job["events"].append(_event(job_id, "verification", "완료 조건 검증", "success" if report["outcome"] == "completed" else "blocked", output=f"결과: {report['outcome']} · 실제 도구 기록 {len(result['toolRuns'])}건"))
                job["events"].append(_event(job_id, "report", "최종 리포트 발행", output=report["summary"]))
                job["status"] = "completed" if report["outcome"] == "completed" else "failed"
                job["phase"] = "completed" if report["outcome"] == "completed" else "failed"
                job["completedAt"] = now
                job["updatedAt"] = now
                for index, step in enumerate(job["plan"]):
                    step["status"] = "done" if report["outcome"] == "completed" or index < len(job["plan"]) - 1 else "blocked"
                data["reports"].append(report)
                data["reports"] = data["reports"][-30:]
                data["messages"].append(_message(agent["id"], agent["name"], result["reply"], "agent", ["대표님"], job_id, agent["position"]))
                data["messages"] = data["messages"][-60:]
                lesson_items = report["lessons"] or ["실제 도구 결과를 검증한 뒤 완료 리포트를 발행한다."]
                for lesson in lesson_items[:3]:
                    agent.setdefault("memories", []).append({
                        "id": str(uuid.uuid4()), "kind": "lesson", "at": now, "summary": lesson,
                        "evidence": f"report_{report['id']}", "confidence": 0.88,
                    })
                agent["memories"] = agent["memories"][-20:]
                agent["score"] = min(100, agent["score"] + (1 if report["outcome"] == "completed" else 0))
                if result.get("registerKnowledge") and report["outcome"] == "completed":
                    knowledge = {
                        "id": str(uuid.uuid4()), "title": report["title"], "body": report["summary"],
                        "sourceJobId": job_id, "authorAgentId": agent["id"], "authorName": agent["name"], "createdAt": now,
                    }
                    data.setdefault("knowledge", []).insert(0, knowledge)
                    data["knowledge"] = data["knowledge"][:200]
                    job["events"].append(_event(job_id, "tool_result", "공용 정보 등록 완료", tool="shared_knowledge.register", input=report["summary"], output=f"공용 정보 {knowledge['id']} 저장"))
                for participant in world["agents"]:
                    if participant["id"] not in job["participantIds"]:
                        continue
                    participant["activeJobId"] = None if participant.get("activeJobId") == job_id else participant.get("activeJobId")
                    participant["supportingJobId"] = None if participant.get("supportingJobId") == job_id else participant.get("supportingJobId")
                    participant["currentTool"] = None
                    participant["toolStation"] = None
                    participant["activity"] = ("리포트 저장 완료" if report["outcome"] == "completed" else "실패 리포트 저장") + " · 다음 요청 대기"
                    participant["focus"] = report["title"][:80]
                    participant["progress"] = 100
                for station in job.get("claimedStations", []):
                    if world.setdefault("toolOccupancies", {}).get(station, {}).get("jobId") == job_id:
                        world["toolOccupancies"].pop(station, None)
                if job["tool"] == "whiteboard" and report["outcome"] == "completed":
                    self._write_agent_board(data, agent, job, report)
                self._activate_waiting(data)
                world["version"] += 1
                world["updatedAt"] = now
                return True
            await self.store.mutate(complete)

    def _write_agent_board(self, data: dict, agent: dict, job: dict, report: dict) -> None:
        board = data["board"]
        now = utc_now()
        text_blocks = list(board.get("textBlocks") or [])
        candidates = [(8, 58), (132, 58), (8, 124), (132, 124), (8, 190), (132, 190)]
        def overlaps(x: int, y: int) -> bool:
            return any(x < int(item["x"]) + int(item["width"]) and x + 116 > int(item["x"]) and y < int(item["y"]) + int(item["height"]) and y + 58 > int(item["y"]) for item in text_blocks)
        position = next(((x, y) for x, y in candidates if not overlaps(x, y)), None)
        started_new_page = position is None
        if started_new_page:
            text_blocks = []
            position = candidates[0]
        x, y = position
        block = {
            "id": str(uuid.uuid4()), "x": x, "y": y,
            "width": 116, "height": 58, "text": f"{report['title']}\n\n{report['summary'][:240]}",
            "color": "#26394f", "background": "#f9f4dfef", "fontSize": 8,
            "authorType": "agent", "authorId": agent["id"], "authorName": agent["name"],
            "createdAt": now, "updatedAt": now,
        }
        text_blocks.append(block)
        next_board = deepcopy(board)
        next_board.update({
            "title": report["title"][:40], "textBlocks": text_blocks, "version": board["version"] + 1,
            "authorType": "agent", "authorId": agent["id"], "authorName": agent["name"],
            "changeSummary": ("이전 본문을 버전 보관함에 보존하고 새 페이지 생성 · " if started_new_page else "") + f"작업 {job['id'][:8]} 리포트 배치", "updatedAt": now,
        })
        data["board"] = next_board
        data.setdefault("boardVersions", {})[str(next_board["version"])] = deepcopy(next_board)
        data["boardHistory"].insert(0, board_meta(next_board))
        data["boardHistory"] = data["boardHistory"][:BOARD_HISTORY_LIMIT]
        retained = {str(item["version"]) for item in data["boardHistory"]}
        data["boardVersions"] = {key: document for key, document in data["boardVersions"].items() if key in retained}
        job["events"].append(_event(job["id"], "tool_result", f"화이트보드 v{next_board['version']} 저장", tool="whiteboard", input=report["summary"], output=f"텍스트 블록 {block['id']} · 작성자 {agent['name']}"))

    async def _fail_or_retry(self, job_id: str, error: Exception) -> None:
        async with self.lock:
            def update(data: dict):
                job = data["jobs"].get(job_id)
                if not job or job["status"] != "active":
                    return False
                message = redact_secrets(f"{type(error).__name__}: {error}")[:900]
                if job["attempt"] < 3 and not data["world"]["paused"]:
                    job["phase"] = "working"
                    job["events"].append(_event(job_id, "verification", "실행 오류 후 재계획", "blocked", output=message))
                    job["updatedAt"] = utc_now()
                    return True
                fallback = {
                    "reply": "작업을 완료하지 못해 실패 리포트를 남겼습니다.",
                    "publicDecision": _public_decision(job["command"], job["tool"]),
                    "toolRuns": [], "collaboration": [],
                    "report": {
                        "title": "작업 실패 리포트", "outcome": "failed", "summary": "세 차례 실행 후에도 외부 도구 오류가 계속됐습니다.",
                        "body": f"실행 오류: {message}", "limitations": [message], "lessons": ["외부 도구 실패를 재시도 한도와 함께 기록한다."],
                    },
                }
                asyncio.create_task(self._complete_job(job_id, fallback))
                return True
            await self.store.mutate(update)
