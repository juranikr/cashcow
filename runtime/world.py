from __future__ import annotations

import heapq
import math
from dataclasses import dataclass


WORLD_BOUNDS = {"min_x": 2.0, "max_x": 98.0, "min_y": 3.0, "max_y": 96.0}
AGENT_CLEARANCE = 0.45
HEARING_RADIUS = 34.0

WALL_RECTS = [
    (4, 8, 34, 2), (4, 8, 2, 34), (36, 8, 2, 34),
    (4, 40, 14, 2), (24, 40, 14, 2),
    (58, 8, 38, 2), (58, 8, 2, 39), (94, 8, 2, 39),
    (58, 45, 16, 2), (82, 45, 14, 2),
]

FURNITURE_RECTS = [
    (11, 17, 21, 14),
    (61.5, 15, 12.5, 7), (80, 15, 12.5, 7),
    (61.5, 34.5, 12.5, 7), (80, 34.5, 12.5, 7),
    (10, 69, 9.5, 6.5), (24.5, 81.5, 9.5, 6.5),
    (20, 74.5, 6.5, 8.5), (65, 70, 28, 22),
]

COLLISION_RECTS = WALL_RECTS + FURNITURE_RECTS

COMPUTER_STATIONS = [
    {"id": "pc-01", "position": {"x": 68.0, "y": 24.0}},
    {"id": "pc-02", "position": {"x": 86.0, "y": 24.0}},
    {"id": "pc-03", "position": {"x": 68.0, "y": 43.0}},
    {"id": "pc-04", "position": {"x": 86.0, "y": 43.0}},
]

MEETING_SEATS = [
    {"id": "seat-minji", "position": {"x": 8.0, "y": 24.0}},
    {"id": "seat-doyun", "position": {"x": 34.0, "y": 24.0}},
    {"id": "seat-harin", "position": {"x": 18.0, "y": 35.0}},
    {"id": "seat-jun", "position": {"x": 27.0, "y": 35.0}},
]

TOOL_SPOTS = {
    "whiteboard": {"x": 80.0, "y": 68.0},
    "computer": COMPUTER_STATIONS[0]["position"],
    "meeting-room": MEETING_SEATS[0]["position"],
}


def distance(a: dict, b: dict) -> float:
    return math.hypot(float(a["x"]) - float(b["x"]), float(a["y"]) - float(b["y"]))


def is_blocked(point: dict, padding: float = AGENT_CLEARANCE) -> bool:
    x, y = float(point["x"]), float(point["y"])
    if x < WORLD_BOUNDS["min_x"] or x > WORLD_BOUNDS["max_x"] or y < WORLD_BOUNDS["min_y"] or y > WORLD_BOUNDS["max_y"]:
        return True
    return any(
        x >= rx - padding and x <= rx + width + padding
        and y >= ry - padding and y <= ry + height + padding
        for rx, ry, width, height in COLLISION_RECTS
    )


def _move_vector(point: dict, dx: float, dy: float) -> dict:
    next_x = {"x": point["x"] + dx, "y": point["y"]}
    moved_x = point["x"] if is_blocked(next_x) else next_x["x"]
    combined = {"x": moved_x, "y": point["y"] + dy}
    return {"x": moved_x, "y": point["y"]} if is_blocked(combined) else combined


def direction_from_delta(dx: float, dy: float, fallback: str = "south") -> str:
    if abs(dx) < 0.01 and abs(dy) < 0.01:
        return fallback
    if abs(dx) > abs(dy):
        return "east" if dx > 0 else "west"
    return "south" if dy > 0 else "north"


def _closest_open_grid_point(point: dict, grid_size: float) -> dict:
    base = {"x": round(point["x"] / grid_size) * grid_size, "y": round(point["y"] / grid_size) * grid_size}
    if not is_blocked(base):
        return base
    for radius in range(1, 5):
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                candidate = {"x": base["x"] + dx * grid_size, "y": base["y"] + dy * grid_size}
                if not is_blocked(candidate):
                    return candidate
    return point


def find_next_path_point(start: dict, goal: dict, grid_size: float = 2.0) -> dict | None:
    grid_start = _closest_open_grid_point(start, grid_size)
    grid_goal = _closest_open_grid_point(goal, grid_size)
    start_key = (grid_start["x"], grid_start["y"])
    counter = 0
    open_heap = [(distance(grid_start, grid_goal), counter, start_key)]
    nodes = {start_key: {"g": 0.0, "parent": None}}
    closed: set[tuple[float, float]] = set()
    found = None
    directions = ((grid_size, 0), (-grid_size, 0), (0, grid_size), (0, -grid_size))
    while open_heap and len(closed) < 4000:
        _, _, current_key = heapq.heappop(open_heap)
        if current_key in closed:
            continue
        closed.add(current_key)
        current = {"x": current_key[0], "y": current_key[1]}
        if distance(current, grid_goal) < grid_size * 0.7:
            found = current_key
            break
        for dx, dy in directions:
            candidate = {"x": current["x"] + dx, "y": current["y"] + dy}
            key = (candidate["x"], candidate["y"])
            if key in closed or is_blocked(candidate):
                continue
            g = nodes[current_key]["g"] + grid_size
            if key in nodes and nodes[key]["g"] <= g:
                continue
            nodes[key] = {"g": g, "parent": current_key}
            counter += 1
            heapq.heappush(open_heap, (g + distance(candidate, grid_goal), counter, key))
    if found is None:
        return None
    cursor = found
    while nodes[cursor]["parent"] is not None and nodes[cursor]["parent"] != start_key:
        cursor = nodes[cursor]["parent"]
    return {"x": cursor[0], "y": cursor[1]}


def _step_toward(point: dict, target: dict, max_step: float, fallback_facing: str) -> tuple[dict, str]:
    gap = distance(point, target)
    facing = direction_from_delta(target["x"] - point["x"], target["y"] - point["y"], fallback_facing)
    if gap <= max_step and not is_blocked(target):
        return {"x": float(target["x"]), "y": float(target["y"])}, facing
    if gap < 0.001:
        return point, facing
    step = min(gap, max_step)
    dx = (target["x"] - point["x"]) / gap * step
    dy = (target["y"] - point["y"]) / gap * step
    candidate = _move_vector(point, dx, dy)
    if distance(candidate, point) < 0.001:
        options = [
            _move_vector(point, max_step, 0), _move_vector(point, -max_step, 0),
            _move_vector(point, 0, max_step), _move_vector(point, 0, -max_step),
        ]
        options = [item for item in options if distance(item, point) > 0.001]
        candidate = min(options, key=lambda item: distance(item, target), default=point)
    return candidate, direction_from_delta(candidate["x"] - point["x"], candidate["y"] - point["y"], facing)


def navigate_toward(point: dict, target: dict, max_step: float, facing: str = "south") -> tuple[dict, str]:
    if distance(point, target) <= max_step * 1.5:
        return _step_toward(point, target, max_step, facing)
    waypoint = find_next_path_point(point, target)
    if waypoint is None:
        return point, facing
    return _step_toward(point, waypoint, max_step, facing)


def target_tool_from_command(command: str) -> str:
    if any(word in command for word in ("회의", "같이", "협업", "모여")):
        return "meeting-room"
    if any(word in command for word in ("보드", "그림", "도트", "화이트", "시각화")):
        return "whiteboard"
    return "computer"


def redact_secrets(value: str) -> str:
    import re
    value = re.sub(r"gsk_[A-Za-z0-9_-]{16,}", "[REDACTED_GROQ_KEY]", value)
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~-]{16,}", "Bearer [REDACTED]", value)
    value = re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "[REDACTED_AWS_ACCESS_KEY]", value)
    value = re.sub(r"(?i)((?:api[_-]?key|secret|token|password|authorization|cookie|x-api-key)\s*[=:]\s*[\"']?)[^\s,;\"'}]{8,}", r"\1[REDACTED]", value)
    value = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", "[REDACTED_PRIVATE_KEY]", value)
    return value[:12000]
