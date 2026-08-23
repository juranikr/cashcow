import { NextRequest, NextResponse } from 'next/server';
import { getActor, isOwnerEmail } from '@/lib/server/auth';
import { saveReview } from '@/lib/server/store';
import { runtimeErrorStatus, runtimeRequest } from '@/lib/server/runtime-client';

const AGENT_IDS = new Set(['minji', 'doyun', 'harin', 'jun']);

export async function POST(request: NextRequest) {
  const actor = await getActor();
  if (!actor) return NextResponse.json({ error: '대표 로그인이 필요합니다.' }, { status: 401 });
  if (!isOwnerEmail(actor.email)) return NextResponse.json({ error: '대표 계정만 주간 평가를 제출할 수 있습니다.' }, { status: 403 });
  try {
    const runtime = await runtimeRequest<{ agentsPaused: boolean }>(`/cashcow/api/bootstrap?actorId=${encodeURIComponent(actor.id)}`);
    if (runtime.agentsPaused) return NextResponse.json({ error: '에이전트가 일시정지되어 평가를 반영할 수 없습니다.' }, { status: 423 });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : '영속 런타임을 확인하지 못했습니다.' }, { status: runtimeErrorStatus(error) });
  }
  let body: unknown;
  try { body = await request.json(); } catch { return NextResponse.json({ error: '올바른 JSON이 아닙니다.' }, { status: 400 }); }
  if (!body || typeof body !== 'object') return NextResponse.json({ error: '입력이 비어 있습니다.' }, { status: 400 });
  const value = body as Record<string, unknown>;
  const individual = Number(value.individual);
  const team = Number(value.team);
  const agentIds = Array.isArray(value.agentIds) ? value.agentIds.filter((id): id is string => typeof id === 'string' && AGENT_IDS.has(id)) : [];
  if (typeof value.cycleId !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value.cycleId)) return NextResponse.json({ error: '평가 주기가 올바르지 않습니다.' }, { status: 400 });
  if (!Number.isInteger(individual) || individual < 1 || individual > 5 || !Number.isInteger(team) || team < 1 || team > 5) return NextResponse.json({ error: '평가는 1~5점이어야 합니다.' }, { status: 400 });
  if (typeof value.comment !== 'string' || value.comment.length > 1200) return NextResponse.json({ error: '피드백은 1,200자 이하여야 합니다.' }, { status: 400 });
  if (!agentIds.length) return NextResponse.json({ error: '평가 대상 에이전트가 없습니다.' }, { status: 400 });
  if (request.signal.aborted) return NextResponse.json({ error: '평가 반영이 취소됐습니다.' }, { status: 499 });
  try {
    const runtime = await runtimeRequest<{ reflectedAgents: number }>('/cashcow/api/reviews', {
      method: 'POST',
      body: JSON.stringify({ cycleId: value.cycleId, individual, team, comment: value.comment, actorId: actor.id }),
    });
    const local = await saveReview({ cycleId: value.cycleId, individual, team, comment: value.comment, agentIds }, actor.id);
    return NextResponse.json({ ...local, ...runtime });
  } catch (error) {
    const message = error instanceof Error ? error.message : '평가를 저장하지 못했습니다.';
    return NextResponse.json({ error: message }, { status: message.includes('일시정지') ? 423 : message.includes('현재') ? 409 : runtimeErrorStatus(error) });
  }
}
