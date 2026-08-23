export type Point = { x: number; y: number };
export type Direction = 'north' | 'east' | 'south' | 'west';
export type Rect = { x: number; y: number; width: number; height: number };

export type PlanStep = {
  id: string;
  title: string;
  detail?: string;
  status: 'pending' | 'active' | 'done' | 'blocked';
  tool?: 'whiteboard' | 'computer' | 'meeting-room';
};

export type ToolEvent = {
  id: string;
  at: string;
  tool: string;
  input: string;
  output: string;
  result: 'success' | 'working' | 'blocked';
};

export type Memory = {
  id: string;
  kind: 'episode' | 'lesson';
  at: string;
  summary: string;
  evidence: string;
  confidence: number;
};

export type AgentModel = {
  id: string;
  name: string;
  team: string;
  role: string;
  rank: string;
  tone: string;
  position: Point;
  facing: Direction;
  target: Point;
  activity: string;
  focus: string;
  progress: number;
  score: number;
  visible: boolean;
  currentTool?: 'whiteboard' | 'computer' | 'meeting-room';
  toolStation?: string;
  plan: PlanStep[];
  history: ToolEvent[];
  memories: Memory[];
};

export const WORLD_BOUNDS = { minX: 2, maxX: 98, minY: 3, maxY: 96 } as const;
export const HEARING_RADIUS = 34;
export const VISION_RADIUS = 44;
export const VISION_ANGLE = 100;

const WALL_RECTS: Rect[] = [
  { x: 4, y: 8, width: 34, height: 2 },
  { x: 4, y: 8, width: 2, height: 34 },
  { x: 36, y: 8, width: 2, height: 25 },
  { x: 36, y: 37, width: 2, height: 5 },
  { x: 4, y: 40, width: 24, height: 2 },
  { x: 32, y: 40, width: 6, height: 2 },
  { x: 58, y: 8, width: 38, height: 2 },
  { x: 58, y: 8, width: 2, height: 25 },
  { x: 58, y: 38, width: 2, height: 9 },
  { x: 94, y: 8, width: 2, height: 39 },
  { x: 58, y: 45, width: 17, height: 2 },
  { x: 81, y: 45, width: 15, height: 2 },
];

const FURNITURE_RECTS: Rect[] = [
  { x: 11, y: 17, width: 21, height: 14 },
  { x: 65, y: 14, width: 12, height: 7 },
  { x: 82, y: 14, width: 12, height: 7 },
  { x: 65, y: 33, width: 12, height: 7 },
  { x: 82, y: 33, width: 12, height: 7 },
  { x: 8, y: 68, width: 27, height: 22 },
  { x: 68, y: 73, width: 27, height: 18 },
];

export const COLLISION_RECTS: Rect[] = [...WALL_RECTS, ...FURNITURE_RECTS];

export const VISION_BLOCKERS: Rect[] = COLLISION_RECTS;

export const COMPUTER_STATIONS = [
  { id: 'pc-01', position: { x: 70, y: 24 } },
  { id: 'pc-02', position: { x: 87, y: 24 } },
  { id: 'pc-03', position: { x: 70, y: 41 } },
  { id: 'pc-04', position: { x: 87, y: 41 } },
] as const;

export const MEETING_SEATS = [
  { id: 'seat-minji', position: { x: 8, y: 24 } },
  { id: 'seat-doyun', position: { x: 34, y: 24 } },
  { id: 'seat-harin', position: { x: 18, y: 35 } },
  { id: 'seat-jun', position: { x: 27, y: 35 } },
] as const;

export const TOOL_SPOTS = {
  whiteboard: { x: 80, y: 68 },
  computer: COMPUTER_STATIONS[0].position,
  'meeting-room': MEETING_SEATS[0].position,
} satisfies Record<string, Point>;

const directionVectors: Record<Direction, Point> = {
  north: { x: 0, y: -1 },
  east: { x: 1, y: 0 },
  south: { x: 0, y: 1 },
  west: { x: -1, y: 0 },
};

export function distance(a: Point, b: Point): number {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

export function directionFromDelta(dx: number, dy: number, fallback: Direction): Direction {
  if (Math.abs(dx) < 0.01 && Math.abs(dy) < 0.01) return fallback;
  if (Math.abs(dx) > Math.abs(dy)) return dx > 0 ? 'east' : 'west';
  return dy > 0 ? 'south' : 'north';
}

export function isBlocked(point: Point, padding = 1.2): boolean {
  if (point.x < WORLD_BOUNDS.minX || point.x > WORLD_BOUNDS.maxX || point.y < WORLD_BOUNDS.minY || point.y > WORLD_BOUNDS.maxY) return true;
  return COLLISION_RECTS.some((rect) =>
    point.x >= rect.x - padding && point.x <= rect.x + rect.width + padding &&
    point.y >= rect.y - padding && point.y <= rect.y + rect.height + padding,
  );
}

export function movePoint(point: Point, dx: number, dy: number): Point {
  const diagonal = dx !== 0 && dy !== 0 ? Math.SQRT1_2 : 1;
  const nextX = { x: point.x + dx * diagonal, y: point.y };
  const nextY = { x: point.x, y: point.y + dy * diagonal };
  const movedX = isBlocked(nextX) ? point.x : nextX.x;
  const combined = { x: movedX, y: nextY.y };
  return isBlocked(combined) ? { x: movedX, y: point.y } : combined;
}

export function stepToward(point: Point, target: Point, maxStep: number): { point: Point; arrived: boolean; facing: Direction } {
  const gap = distance(point, target);
  if (gap <= maxStep) return { point: target, arrived: true, facing: directionFromDelta(target.x - point.x, target.y - point.y, 'south') };
  const dx = ((target.x - point.x) / gap) * maxStep;
  const dy = ((target.y - point.y) / gap) * maxStep;
  const candidate = movePoint(point, dx, dy);
  const stuck = distance(candidate, point) < 0.001;
  const detour = stuck ? movePoint(point, Math.abs(dx) > Math.abs(dy) ? 0 : maxStep, Math.abs(dx) > Math.abs(dy) ? maxStep : 0) : candidate;
  return { point: detour, arrived: distance(detour, target) <= maxStep, facing: directionFromDelta(detour.x - point.x, detour.y - point.y, 'south') };
}

type PathNode = { x: number; y: number; key: string; g: number; f: number; parent?: string };

function gridKey(x: number, y: number): string { return `${x},${y}`; }

function closestOpenGridPoint(point: Point, gridSize: number): Point {
  const base = { x: Math.round(point.x / gridSize) * gridSize, y: Math.round(point.y / gridSize) * gridSize };
  if (!isBlocked(base, 0.45)) return base;
  for (let radius = 1; radius <= 4; radius += 1) {
    for (let dx = -radius; dx <= radius; dx += 1) {
      for (let dy = -radius; dy <= radius; dy += 1) {
        const candidate = { x: base.x + dx * gridSize, y: base.y + dy * gridSize };
        if (!isBlocked(candidate, 0.45)) return candidate;
      }
    }
  }
  return point;
}

export function findNextPathPoint(start: Point, goal: Point, gridSize = 2): Point | null {
  const gridStart = closestOpenGridPoint(start, gridSize);
  const gridGoal = closestOpenGridPoint(goal, gridSize);
  const startKey = gridKey(gridStart.x, gridStart.y);
  const open: PathNode[] = [{ ...gridStart, key: startKey, g: 0, f: distance(gridStart, gridGoal) }];
  const nodes = new Map<string, PathNode>([[startKey, open[0]]]);
  const closed = new Set<string>();
  const directions = [[gridSize, 0], [-gridSize, 0], [0, gridSize], [0, -gridSize]] as const;
  let found: PathNode | null = null;

  while (open.length && closed.size < 4000) {
    open.sort((a, b) => a.f - b.f);
    const current = open.shift()!;
    if (closed.has(current.key)) continue;
    closed.add(current.key);
    if (distance(current, gridGoal) < gridSize * 0.7) { found = current; break; }

    directions.forEach(([dx, dy]) => {
      const x = current.x + dx;
      const y = current.y + dy;
      const key = gridKey(x, y);
      if (closed.has(key) || isBlocked({ x, y }, 0.45)) return;
      const g = current.g + gridSize;
      const existing = nodes.get(key);
      if (existing && existing.g <= g) return;
      const node: PathNode = { x, y, key, g, f: g + distance({ x, y }, gridGoal), parent: current.key };
      nodes.set(key, node);
      open.push(node);
    });
  }

  if (!found) return null;
  let cursor = found;
  while (cursor.parent && cursor.parent !== startKey) cursor = nodes.get(cursor.parent)!;
  return { x: cursor.x, y: cursor.y };
}

export function navigateToward(point: Point, target: Point, maxStep: number): { point: Point; arrived: boolean; facing: Direction } {
  if (distance(point, target) <= maxStep * 1.5) return stepToward(point, target, maxStep);
  const nextPathPoint = findNextPathPoint(point, target);
  if (!nextPathPoint) return { point, arrived: false, facing: 'south' };
  return stepToward(point, nextPathPoint, maxStep);
}

function pointInside(point: Point, rect: Rect): boolean {
  return point.x >= rect.x && point.x <= rect.x + rect.width && point.y >= rect.y && point.y <= rect.y + rect.height;
}

export function hasLineOfSight(from: Point, to: Point, blockers = VISION_BLOCKERS): boolean {
  const gap = distance(from, to);
  const samples = Math.max(2, Math.ceil(gap * 1.8));
  for (let index = 1; index < samples; index += 1) {
    const t = index / samples;
    const sample = { x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t };
    if (blockers.some((rect) => pointInside(sample, rect))) return false;
  }
  return true;
}

export function canSee(observer: Pick<AgentModel, 'position' | 'facing'>, target: Point, radius = VISION_RADIUS, angle = VISION_ANGLE): boolean {
  const dx = target.x - observer.position.x;
  const dy = target.y - observer.position.y;
  const gap = Math.hypot(dx, dy);
  if (gap === 0) return true;
  if (gap > radius) return false;
  const facing = directionVectors[observer.facing];
  const cosine = (facing.x * dx + facing.y * dy) / gap;
  if (cosine < Math.cos((angle / 2) * (Math.PI / 180))) return false;
  return hasLineOfSight(observer.position, target);
}

export function hearingRecipients(speaker: Point, agents: AgentModel[], radius = HEARING_RADIUS): AgentModel[] {
  return agents.filter((agent) => distance(speaker, agent.position) <= radius);
}

export function canAcquireExclusiveTool(requesterId: string, currentOwnerId?: string): boolean {
  return !currentOwnerId || currentOwnerId === requesterId;
}

export function transitionVisibility(previous: Set<string>, next: Set<string>): Array<{ id: string; type: 'seen' | 'lost_sight' }> {
  const events: Array<{ id: string; type: 'seen' | 'lost_sight' }> = [];
  next.forEach((id) => { if (!previous.has(id)) events.push({ id, type: 'seen' }); });
  previous.forEach((id) => { if (!next.has(id)) events.push({ id, type: 'lost_sight' }); });
  return events;
}

export function targetToolFromCommand(command: string): keyof typeof TOOL_SPOTS {
  if (/회의|같이|협업|모여/.test(command)) return 'meeting-room';
  if (/보드|그림|도트|화이트/.test(command)) return 'whiteboard';
  if (/인터넷|검색|스크립트|공용\s*정보|조사/.test(command)) return 'computer';
  if (/정리|시각화/.test(command)) return 'whiteboard';
  return 'computer';
}

export function buildCommandPlan(command: string, tool = targetToolFromCommand(command)): PlanStep[] {
  const concise = command.replace(/@[가-힣\w-]+/g, '').trim().slice(0, 44) || '대표 요청 수행';
  const toolLabel = tool === 'computer' ? '컴퓨터' : tool === 'whiteboard' ? '화이트보드' : '회의실';
  return [
    { id: crypto.randomUUID(), title: `요청 이해: ${concise}`, detail: '목표와 완료 조건 확인', status: 'active' },
    { id: crypto.randomUUID(), title: `${toolLabel}(으)로 이동`, detail: '물리적 도구에 도착한 뒤 점유 요청', status: 'pending', tool },
    { id: crypto.randomUUID(), title: `${toolLabel}에서 작업 실행`, detail: '입력·출력·결과를 히스토리에 기록', status: 'pending', tool },
    { id: crypto.randomUUID(), title: '결과 검증 및 교훈 저장', detail: '대표에게 결과를 보고하고 다음 행동에 반영', status: 'pending' },
  ];
}

export function kstReviewCycle(now = new Date()): { id: string; startsAt: Date; endsAt: Date; daysLeft: number } {
  const kst = new Date(now.getTime() + 9 * 60 * 60 * 1000);
  const day = kst.getUTCDay();
  const daysSinceMonday = (day + 6) % 7;
  const startUtc = Date.UTC(kst.getUTCFullYear(), kst.getUTCMonth(), kst.getUTCDate() - daysSinceMonday, 0, 0, 0) - 9 * 60 * 60 * 1000;
  const startsAt = new Date(startUtc);
  const endsAt = new Date(startUtc + 7 * 24 * 60 * 60 * 1000);
  const daysLeft = Math.max(0, Math.ceil((endsAt.getTime() - now.getTime()) / (24 * 60 * 60 * 1000)));
  const localStart = new Date(startUtc + 9 * 60 * 60 * 1000);
  return { id: localStart.toISOString().slice(0, 10), startsAt, endsAt, daysLeft };
}

export function redactSecrets(value: string): string {
  return value
    .replace(/gsk_[A-Za-z0-9_-]{16,}/g, '[REDACTED_GROQ_KEY]')
    .replace(/(?:bearer\s+)[A-Za-z0-9._~-]{16,}/gi, 'Bearer [REDACTED]')
    .slice(0, 2000);
}
