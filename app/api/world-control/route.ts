import { NextRequest, NextResponse } from 'next/server';
import { getActor, isOwnerEmail } from '@/lib/server/auth';
import { setAgentsPaused } from '@/lib/server/store';
import { runtimeErrorStatus, runtimeRequest } from '@/lib/server/runtime-client';

const NO_STORE = { 'cache-control': 'no-store' };

export async function GET() {
  const actor = await getActor();
  if (!actor) return NextResponse.json({ error: '로그인이 필요합니다.' }, { status: 401, headers: NO_STORE });
  try {
    const state = await runtimeRequest<{ agentsPaused: boolean }>(`/cashcow/api/bootstrap?actorId=${encodeURIComponent(actor.id)}`);
    return NextResponse.json({ agentsPaused: state.agentsPaused }, { headers: NO_STORE });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : '상태를 불러오지 못했습니다.' }, { status: runtimeErrorStatus(error), headers: NO_STORE });
  }
}
export async function PATCH(request: NextRequest) {
  const actor = await getActor();
  if (!actor) return NextResponse.json({ error: '로그인이 필요합니다.' }, { status: 401, headers: NO_STORE });
  if (!isOwnerEmail(actor.email)) return NextResponse.json({ error: '대표 계정만 에이전트 활동을 제어할 수 있습니다.' }, { status: 403, headers: NO_STORE });
  let body: unknown;
  try { body = await request.json(); } catch { return NextResponse.json({ error: '올바른 JSON이 아닙니다.' }, { status: 400, headers: NO_STORE }); }
  if (!body || typeof body !== 'object' || typeof (body as Record<string, unknown>).agentsPaused !== 'boolean') return NextResponse.json({ error: '정지 상태가 올바르지 않습니다.' }, { status: 400, headers: NO_STORE });
  const agentsPaused = (body as { agentsPaused: boolean }).agentsPaused;
  try {
    const result = await runtimeRequest<{ agentsPaused: boolean; updatedAt: string }>('/cashcow/api/control', { method: 'PATCH', body: JSON.stringify({ agentsPaused, actorId: actor.id }) });
    await setAgentsPaused(result.agentsPaused, actor.id);
    return NextResponse.json(result, { headers: NO_STORE });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : '활동 상태를 저장하지 못했습니다.' }, { status: runtimeErrorStatus(error), headers: NO_STORE });
  }
}
