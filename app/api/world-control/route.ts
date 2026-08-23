import { NextRequest, NextResponse } from 'next/server';
import { getActor, isOwnerEmail } from '@/lib/server/auth';
import { getAgentsPaused, setAgentsPaused } from '@/lib/server/store';

const NO_STORE = { 'cache-control': 'no-store' };

export async function GET() {
  const actor = await getActor();
  if (!actor) return NextResponse.json({ error: '로그인이 필요합니다.' }, { status: 401, headers: NO_STORE });
  return NextResponse.json({ agentsPaused: await getAgentsPaused() }, { headers: NO_STORE });
}

export async function PATCH(request: NextRequest) {
  const actor = await getActor();
  if (!actor) return NextResponse.json({ error: '로그인이 필요합니다.' }, { status: 401, headers: NO_STORE });
  if (!isOwnerEmail(actor.email)) return NextResponse.json({ error: '대표 계정만 에이전트 활동을 제어할 수 있습니다.' }, { status: 403, headers: NO_STORE });

  let body: unknown;
  try { body = await request.json(); } catch { return NextResponse.json({ error: '올바른 JSON이 아닙니다.' }, { status: 400, headers: NO_STORE }); }
  if (!body || typeof body !== 'object' || typeof (body as Record<string, unknown>).agentsPaused !== 'boolean') {
    return NextResponse.json({ error: '정지 상태가 올바르지 않습니다.' }, { status: 400, headers: NO_STORE });
  }

  const agentsPaused = (body as { agentsPaused: boolean }).agentsPaused;
  return NextResponse.json(await setAgentsPaused(agentsPaused, actor.id), { headers: NO_STORE });
}
