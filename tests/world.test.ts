import { describe, expect, it } from 'vitest';
import {
  AgentModel,
  AGENT_CLEARANCE,
  COMPUTER_STATIONS,
  HEARING_RADIUS,
  MEETING_SEATS,
  TOOL_SPOTS,
  buildCommandPlan,
  canAcquireExclusiveTool,
  canSee,
  distance,
  findNextPathPoint,
  hasLineOfSight,
  hearingRecipients,
  isBlocked,
  kstReviewCycle,
  movePoint,
  navigateToward,
  redactSecrets,
  reachedAssignedTool,
  stepToward,
  targetToolFromCommand,
  transitionVisibility,
} from '../lib/world';

function walkUntilNear(start: { x: number; y: number }, goal: { x: number; y: number }, maxTicks = 600) {
  let point = start;
  let stalledTicks = 0;
  for (let tick = 0; tick < maxTicks && distance(point, goal) > 2.4; tick += 1) {
    const next = navigateToward(point, goal, .72);
    expect(distance(point, next.point)).toBeLessThanOrEqual(.721);
    expect(isBlocked(next.point)).toBe(false);
    stalledTicks = distance(point, next.point) < .001 ? stalledTicks + 1 : 0;
    expect(stalledTicks).toBeLessThan(12);
    point = next.point;
  }
  return point;
}

function agent(id: string, x: number, y: number): AgentModel {
  return {
    id, name: id, team: 'test', role: 'test', rank: 'test', tone: 'blue',
    position: { x, y }, facing: 'east', target: { x, y }, activity: 'idle', focus: 'test',
    progress: 0, score: 80, visible: true, plan: [], history: [], memories: [],
  };
}

describe('world geometry', () => {
  it('uses Euclidean distance and includes the exact hearing boundary', () => {
    const speaker = { x: 10, y: 10 };
    const inside = agent('inside', 10 + HEARING_RADIUS, 10);
    const outside = agent('outside', 10 + HEARING_RADIUS + .01, 10);
    expect(distance(speaker, inside.position)).toBe(HEARING_RADIUS);
    expect(hearingRecipients(speaker, [inside, outside])).toEqual([inside]);
  });

  it('enforces facing angle and opaque-wall line of sight', () => {
    const observer = agent('observer', 40, 24);
    observer.facing = 'east';
    expect(canSee(observer, { x: 50, y: 24 })).toBe(true);
    expect(canSee(observer, { x: 30, y: 24 })).toBe(false);
    expect(hasLineOfSight({ x: 52, y: 24 }, { x: 70, y: 24 })).toBe(false);
  });

  it('emits seen and lost_sight exactly once per set transition', () => {
    expect(transitionVisibility(new Set(['a']), new Set(['b']))).toEqual([
      { id: 'b', type: 'seen' },
      { id: 'a', type: 'lost_sight' },
    ]);
    expect(transitionVisibility(new Set(['b']), new Set(['b']))).toEqual([]);
  });

  it('does not move through furniture and path movement never teleports', () => {
    const blockedStart = { x: 8.5, y: 18 };
    const attempted = movePoint(blockedStart, 2, 0);
    expect(isBlocked(attempted)).toBe(false);
    const start = { x: 50, y: 52 };
    const next = navigateToward(start, { x: 31, y: 36 }, .72);
    expect(distance(start, next.point)).toBeLessThanOrEqual(.721);
    expect(isBlocked(next.point)).toBe(false);
  });

  it('uses the same collision clearance for planned waypoints and actual movement', () => {
    expect(AGENT_CLEARANCE).toBeGreaterThan(0);
    const waypoint = findNextPathPoint({ x: 63.75, y: 32.1 }, COMPUTER_STATIONS[1].position);
    expect(waypoint).not.toBeNull();
    expect(isBlocked(waypoint!)).toBe(false);
  });

  it('keeps autonomous diagonal movement at the configured walking speed', () => {
    const start = { x: 45, y: 55 };
    const next = stepToward(start, { x: 50, y: 60 }, .72);
    expect(distance(start, next.point)).toBeCloseTo(.72, 5);
  });

  it.each(COMPUTER_STATIONS)('enters the focus lab and reaches $id without getting stuck', (station) => {
    expect(isBlocked(station.position)).toBe(false);
    const end = walkUntilNear({ x: 78, y: 55 }, station.position);
    expect(distance(end, station.position)).toBeLessThanOrEqual(2.4);
  });

  it('keeps every tool approach point reachable from the central office', () => {
    const destinations = [...MEETING_SEATS, { id: 'whiteboard', position: TOOL_SPOTS.whiteboard }];
    destinations.forEach((destination) => {
      expect(isBlocked(destination.position)).toBe(false);
      const end = walkUntilNear({ x: 49, y: 54 }, destination.position);
      expect(distance(end, destination.position), destination.id).toBeLessThanOrEqual(2.4);
    });
  });

  it('reaches all physical tools from every initial agent area', () => {
    const starts = [
      { x: 31, y: 36 },
      { x: 52, y: 35 },
      { x: 64, y: 60 },
      { x: 44, y: 69 },
    ];
    const destinations = [
      ...COMPUTER_STATIONS,
      ...MEETING_SEATS,
      { id: 'whiteboard', position: TOOL_SPOTS.whiteboard },
    ];
    starts.forEach((start, startIndex) => destinations.forEach((destination) => {
      const end = walkUntilNear(start, destination.position);
      expect(distance(end, destination.position), `start-${startIndex} to ${destination.id}`).toBeLessThanOrEqual(2.4);
    }));
  }, 15_000);

  it('only starts the tool and station the agent was assigned', () => {
    const meetingAgent = agent('minji', 20, 36);
    meetingAgent.target = MEETING_SEATS[0].position;
    meetingAgent.toolStation = MEETING_SEATS[0].id;
    meetingAgent.focus = '회의실에서 협업';
    expect(reachedAssignedTool(meetingAgent, MEETING_SEATS[2].position)).toBeUndefined();
    expect(reachedAssignedTool(meetingAgent, MEETING_SEATS[0].position)).toEqual({
      tool: 'meeting-room',
      stationId: MEETING_SEATS[0].id,
    });

    const computerAgent = agent('doyun', 78, 48);
    computerAgent.target = COMPUTER_STATIONS[1].position;
    computerAgent.toolStation = COMPUTER_STATIONS[1].id;
    computerAgent.focus = '인터넷 검색';
    expect(reachedAssignedTool(computerAgent, COMPUTER_STATIONS[0].position)).toBeUndefined();
    expect(reachedAssignedTool(computerAgent, COMPUTER_STATIONS[1].position)).toEqual({
      tool: 'computer',
      stationId: COMPUTER_STATIONS[1].id,
    });
  });
});

describe('agent rules', () => {
  it('gives an exclusive computer lease to only one agent', () => {
    expect(canAcquireExclusiveTool('minji')).toBe(true);
    expect(canAcquireExclusiveTool('minji', 'minji')).toBe(true);
    expect(canAcquireExclusiveTool('doyun', 'minji')).toBe(false);
  });

  it('routes commands to physical tools and creates observable plans', () => {
    expect(targetToolFromCommand('화이트보드에 도트로 그려줘')).toBe('whiteboard');
    expect(targetToolFromCommand('다 같이 회의하자')).toBe('meeting-room');
    expect(targetToolFromCommand('인터넷에서 조사해줘')).toBe('computer');
    const plan = buildCommandPlan('검색 결과를 정리해줘');
    expect(plan).toHaveLength(4);
    expect(plan.some((step) => step.tool === 'computer')).toBe(true);
  });

  it('rolls weekly review cycles at Monday midnight in Asia/Seoul', () => {
    const before = kstReviewCycle(new Date('2026-08-23T14:59:59.000Z'));
    const after = kstReviewCycle(new Date('2026-08-23T15:00:00.000Z'));
    expect(before.id).toBe('2026-08-17');
    expect(after.id).toBe('2026-08-24');
  });

  it('redacts Groq and bearer secrets before trace persistence', () => {
    const value = redactSecrets('gsk_abcdefghijklmnopqrstuvwxyz123456 Bearer abcdefghijklmnopqrstuvwxyz');
    expect(value).not.toContain('gsk_');
    expect(value).toContain('[REDACTED_GROQ_KEY]');
    expect(value).toContain('Bearer [REDACTED]');
  });
});
