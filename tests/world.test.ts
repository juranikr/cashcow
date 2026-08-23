import { describe, expect, it } from 'vitest';
import {
  AgentModel,
  HEARING_RADIUS,
  buildCommandPlan,
  canAcquireExclusiveTool,
  canSee,
  distance,
  hasLineOfSight,
  hearingRecipients,
  isBlocked,
  kstReviewCycle,
  movePoint,
  navigateToward,
  redactSecrets,
  targetToolFromCommand,
  transitionVisibility,
} from '../lib/world';

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
