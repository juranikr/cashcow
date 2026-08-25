from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cognition import default_cognition, ensure_agent_cognition


PALETTE = ["#f9f4df", "#ef7657", "#e4b34f", "#55a47c", "#4d8fb8", "#745f9d", "#26394f"]
BOARD_HISTORY_LIMIT = 30


def board_meta(board: dict) -> dict:
    return {
        "version": int(board["version"]),
        "authorType": board.get("authorType", "system"),
        "authorId": board.get("authorId", "system"),
        "authorName": board.get("authorName", "시스템"),
        "changeSummary": board.get("changeSummary", "화이트보드 저장"),
        "createdAt": board.get("createdAt") or board.get("updatedAt") or utc_now(),
        "contentAvailable": bool(board.get("contentAvailable", "pixelsBase64" in board)),
    }


def ensure_board_versions(data: dict) -> bool:
    """Migrate metadata-only board histories without losing the current document."""
    changed = False
    board = data["board"]
    versions = data.setdefault("boardVersions", {})
    current_key = str(int(board["version"]))
    if current_key not in versions:
        versions[current_key] = deepcopy(board)
        changed = True

    history_by_version: dict[int, dict] = {}
    for raw in data.get("boardHistory", []):
        if not isinstance(raw, dict) or "version" not in raw:
            continue
        normalized = board_meta(raw)
        history_by_version[normalized["version"]] = normalized
        if raw != normalized:
            changed = True
    for document in versions.values():
        normalized = board_meta(document)
        history_by_version[normalized["version"]] = normalized
    history_by_version[int(board["version"])] = board_meta(board)
    history = sorted(history_by_version.values(), key=lambda item: item["version"], reverse=True)[:BOARD_HISTORY_LIMIT]
    if history != data.get("boardHistory"):
        data["boardHistory"] = history
        changed = True

    retained = {str(item["version"]) for item in history}
    trimmed = {key: value for key, value in versions.items() if key in retained}
    if trimmed != versions:
        data["boardVersions"] = trimmed
        changed = True
    return changed


def ensure_data_shape(data: dict) -> bool:
    changed = ensure_board_versions(data)
    if "revision" not in data:
        data["revision"] = 0
        changed = True
    world = data["world"]
    for key, default in (("players", {}), ("toolOccupancies", {}), ("appliedReviews", {})):
        if key not in world:
            world[key] = deepcopy(default)
            changed = True
    if "knowledge" not in data:
        data["knowledge"] = []
        changed = True
    agent_ids = [str(agent.get("id", "")) for agent in world.get("agents", [])]
    migration_now = utc_now()
    for agent in world.get("agents", []):
        if ensure_agent_cognition(agent, agent_ids, migration_now):
            changed = True
        normalized_memories = []
        for memory in agent.get("memories", []):
            if not isinstance(memory, dict):
                changed = True
                continue
            if memory.get("kind") not in ("episode", "semantic", "procedure", "lesson"):
                memory["kind"] = "lesson"
                changed = True
            normalized_memories.append(memory)
        if normalized_memories != agent.get("memories", []):
            agent["memories"] = normalized_memories
            changed = True
    for job in data.get("jobs", {}).values():
        if not isinstance(job, dict):
            continue
        phase_names = ("contract", "move", "act", "verify") if len(job.get("plan", [])) == 4 else ()
        for index, step in enumerate(job.get("plan", [])):
            if "phase" not in step and index < len(phase_names):
                step["phase"] = phase_names[index]
                changed = True
        if "planRevision" not in job:
            job["planRevision"] = 1
            changed = True
        if "checkpoints" not in job:
            job["checkpoints"] = []
            changed = True
    for block in data["board"].get("textBlocks", []):
        created_at = block.get("createdAt") or data["board"].get("updatedAt") or utc_now()
        defaults = {
            "authorType": data["board"].get("authorType", "system"),
            "authorId": data["board"].get("authorId", "system"),
            "authorName": block.get("authorName") or data["board"].get("authorName", "시스템"),
            "createdAt": created_at,
            "updatedAt": block.get("updatedAt") or created_at,
        }
        for key, value in defaults.items():
            if key not in block:
                block[key] = value
                changed = True
    current_key = str(int(data["board"]["version"]))
    if data.setdefault("boardVersions", {}).get(current_key) != data["board"]:
        data["boardVersions"][current_key] = deepcopy(data["board"])
        changed = True
    return changed


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _agent(agent_id: str, name: str, team: str, role: str, rank: str, tone: str, x: float, y: float, score: int) -> dict:
    now = utc_now()
    return {
        "id": agent_id, "name": name, "team": team, "role": role, "rank": rank, "tone": tone,
        "position": {"x": x, "y": y}, "facing": "south", "target": {"x": x, "y": y},
        "activity": "다음 요청을 기다리는 중", "focus": "공용 오피스 관찰", "progress": 0,
        "score": score, "visible": True, "currentTool": None, "toolStation": None,
        "activeJobId": None, "supportingJobId": None, "plan": [], "events": [], "memories": [],
        "cognition": default_cognition(agent_id, now), "relationships": {},
    }


def initial_world() -> dict:
    now = utc_now()
    world = {
        "id": "main", "version": 1, "paused": False, "updatedAt": now,
        "lastTickAt": now, "players": {}, "toolOccupancies": {}, "appliedReviews": {},
        "agents": [
            _agent("minji", "민지", "전략팀", "프로젝트 매니저", "리드", "coral", 31, 36, 91),
            _agent("doyun", "도윤", "리서치팀", "리서처", "시니어", "blue", 52, 35, 88),
            _agent("harin", "하린", "제품팀", "프로덕트 디자이너", "주니어", "gold", 64, 60, 83),
            _agent("jun", "준", "플랫폼팀", "소프트웨어 엔지니어", "시니어", "mint", 44, 69, 94),
        ],
    }
    agent_ids = [agent["id"] for agent in world["agents"]]
    for agent in world["agents"]:
        ensure_agent_cognition(agent, agent_ids, now)
    return world


def initial_board() -> dict:
    now = utc_now()
    return {
        "title": "SPRINT 08", "width": 256, "height": 256, "palette": PALETTE,
        "pixelsBase64": base64.b64encode(bytes(256 * 256)).decode("ascii"),
        "textBlocks": [{
            "id": "welcome", "x": 14, "y": 14, "width": 150, "height": 42,
            "text": "목표 · 진행 · 배운 점", "color": "#26394f", "background": "#f9f4dfee",
            "fontSize": 12, "authorType": "system", "authorId": "system", "authorName": "시스템",
            "createdAt": now, "updatedAt": now,
        }],
        "version": 1, "authorType": "system", "authorId": "system", "authorName": "시스템",
        "changeSummary": "256×256 공용 보드 생성", "updatedAt": now,
    }


def initial_data() -> dict:
    board = initial_board()
    return {
        "revision": 1, "world": initial_world(), "jobs": {}, "messages": [], "reports": [], "knowledge": [],
        "board": board,
        "boardHistory": [board_meta(board)],
        "boardVersions": {str(board["version"]): deepcopy(board)},
    }


class FileStore:
    def __init__(self, path: str):
        self.path = Path(path).resolve()
        self._file_lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._file_lock:
            if not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(self._write, initial_data())
                return
            data = await asyncio.to_thread(self._read)
            if ensure_data_shape(data):
                data["revision"] = int(data.get("revision", 0)) + 1
                await asyncio.shield(asyncio.to_thread(self._write, data))

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(prefix="cashcow-state-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    async def mutate(self, callback):
        async with self._file_lock:
            data = await asyncio.to_thread(self._read)
            before = deepcopy(data)
            result = callback(data)
            if data == before:
                return deepcopy(result)
            data["revision"] = int(before.get("revision", 0)) + 1
            write = asyncio.create_task(asyncio.to_thread(self._write, data))
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                await asyncio.shield(write)
                raise
            return deepcopy(result)

    async def snapshot(self) -> dict:
        async with self._file_lock:
            return deepcopy(await asyncio.to_thread(self._read))


class DynamoStore:
    def __init__(self, table_name: str, region: str):
        import boto3
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        self._mutation_lock = asyncio.Lock()
        self._data: dict | None = None

    async def initialize(self) -> None:
        existing = await asyncio.to_thread(self._snapshot_sync)
        if existing.get("world"):
            before = deepcopy(existing)
            changed = ensure_data_shape(existing)
            if changed or int(existing.get("revision", 0)) == 0:
                existing["revision"] = int(before.get("revision", 0)) + 1
                migration_before = deepcopy(before)
                migration_before["world"] = {}
                migration_before["board"] = {}
                migration_before["boardVersions"] = {}
                await asyncio.to_thread(self._persist_delta, migration_before, existing, int(before.get("revision", 0)))
            self._data = existing
            return
        self._data = initial_data()
        await asyncio.to_thread(self._persist_delta, {}, self._data, 0)

    def _put_json(self, pk: str, sk: str, entity: str, payload: dict, **extra: Any) -> None:
        item = {"pk": pk, "sk": sk, "entity": entity, "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")), **extra}
        self.table.put_item(Item=item)

    def _scan_all(self) -> list[dict]:
        items: list[dict] = []
        kwargs: dict[str, Any] = {"ConsistentRead": True}
        while True:
            response = self.table.scan(**kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                return items
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def _snapshot_sync(self) -> dict:
        data = initial_data()
        data["revision"] = 0
        data["jobs"] = {}
        data["messages"] = []
        data["reports"] = []
        data["knowledge"] = []
        data["boardHistory"] = []
        data["boardVersions"] = {}
        found_world = False
        for item in self._scan_all():
            payload = json.loads(item.get("payload", "{}"))
            entity = item.get("entity")
            if entity == "world":
                data["world"] = payload
                found_world = True
            elif entity == "job":
                data["jobs"][payload["id"]] = payload
            elif entity == "message":
                data["messages"].append(payload)
            elif entity == "report":
                data["reports"].append(payload)
            elif entity == "board":
                data["board"] = payload
            elif entity == "board_version":
                if "pixelsBase64" in payload:
                    data["boardVersions"][str(int(payload["version"]))] = payload
                data["boardHistory"].append(board_meta(payload))
            elif entity == "knowledge":
                data["knowledge"].append(payload)
            elif entity == "commit":
                data["revision"] = int(payload.get("revision", item.get("revision", 0)))
        if not found_world:
            return {}
        data["messages"] = sorted(data["messages"], key=lambda item: item["at"])[-60:]
        data["reports"] = sorted(data["reports"], key=lambda item: item["createdAt"], reverse=True)[:30]
        data["knowledge"] = sorted(data["knowledge"], key=lambda item: item["createdAt"], reverse=True)[:200]
        return data

    async def snapshot(self) -> dict:
        async with self._mutation_lock:
            if self._data is None:
                self._data = await asyncio.to_thread(self._snapshot_sync)
            return deepcopy(self._data)

    def _json_item(self, pk: str, sk: str, entity: str, payload: dict, **extra: Any) -> dict:
        return {"pk": pk, "sk": sk, "entity": entity, "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")), **extra}

    def _persist_delta(self, before: dict, after: dict, expected_revision: int) -> None:
        from boto3.dynamodb.types import TypeSerializer

        items: list[dict] = []
        if before.get("world") != after.get("world"):
            items.append(self._json_item("WORLD#main", "STATE", "world", after["world"]))
        if before.get("board") != after.get("board"):
            items.append(self._json_item("BOARD#main", "CURRENT", "board", after["board"]))
        before_jobs = before.get("jobs", {})
        for job_id, job in after.get("jobs", {}).items():
            if before_jobs.get(job_id) != job:
                items.append(self._json_item("JOB#" + job_id, "META", "job", job, status=job.get("status", ""), updatedAt=job.get("updatedAt", "")))
        before_message_ids = {item["id"] for item in before.get("messages", [])}
        for message in after.get("messages", []):
            if message["id"] not in before_message_ids:
                items.append(self._json_item("FEED#messages", f"{message['at']}#{message['id']}", "message", message))
        before_reports = {item["id"]: item for item in before.get("reports", [])}
        for report in after.get("reports", []):
            if before_reports.get(report["id"]) != report:
                items.append(self._json_item("REPORTS", f"{report['createdAt']}#{report['id']}", "report", report))
        before_versions = before.get("boardVersions", {})
        for version, document in after.get("boardVersions", {}).items():
            if before_versions.get(version) != document:
                items.append(self._json_item("BOARD#main", f"VERSION#{int(version):08d}", "board_version", document))
        before_knowledge = {item["id"]: item for item in before.get("knowledge", [])}
        for knowledge in after.get("knowledge", []):
            if before_knowledge.get(knowledge["id"]) != knowledge:
                items.append(self._json_item("KNOWLEDGE", f"{knowledge['createdAt']}#{knowledge['id']}", "knowledge", knowledge))

        serializer = TypeSerializer()

        def serialize_item(item: dict) -> dict:
            return {key: serializer.serialize(value) for key, value in item.items()}

        commit = self._json_item("META#world", "COMMIT", "commit", {"revision": int(after["revision"])}, revision=int(after["revision"]))
        commit_put = {
            "TableName": self.table.name,
            "Item": serialize_item(commit),
            "ConditionExpression": "attribute_not_exists(#revision) OR #revision = :expected",
            "ExpressionAttributeNames": {"#revision": "revision"},
            "ExpressionAttributeValues": {":expected": serializer.serialize(int(expected_revision))},
        }
        actions = [{"Put": {"TableName": self.table.name, "Item": serialize_item(item)}} for item in items]
        actions.append({"Put": commit_put})
        if len(actions) > 100:
            raise RuntimeError("한 번의 월드 트랜잭션이 DynamoDB 100개 작업 한도를 초과했습니다.")
        self.table.meta.client.transact_write_items(TransactItems=actions)

    async def mutate(self, callback):
        async with self._mutation_lock:
            if self._data is None:
                self._data = await asyncio.to_thread(self._snapshot_sync)
            before = deepcopy(self._data or initial_data())
            after = deepcopy(before)
            result = callback(after)
            if after == before:
                return deepcopy(result)
            expected_revision = int(before.get("revision", 0))
            after["revision"] = expected_revision + 1
            persistence = asyncio.create_task(asyncio.to_thread(self._persist_delta, before, after, expected_revision))
            try:
                await asyncio.shield(persistence)
            except asyncio.CancelledError:
                await asyncio.shield(persistence)
                self._data = after
                raise
            except Exception:
                self._data = await asyncio.to_thread(self._snapshot_sync)
                ensure_data_shape(self._data)
                raise
            self._data = after
            return deepcopy(result)


def create_store():
    local_path = os.getenv("LOCAL_STATE_PATH", "").strip()
    if local_path:
        return FileStore(local_path)
    table_name = os.getenv("DYNAMODB_TABLE", "").strip()
    if not table_name:
        raise RuntimeError("DYNAMODB_TABLE or LOCAL_STATE_PATH is required")
    return DynamoStore(table_name, os.getenv("AWS_REGION", "ap-northeast-2"))
