import { NextRequest, NextResponse } from 'next/server';
import { getActor } from '@/lib/server/auth';
import { runtimeErrorStatus, runtimeRequest } from '@/lib/server/runtime-client';

const VALID_AGENTS = new Set(['minji', 'doyun', 'harin', 'jun']);

export async function POST(request: NextRequest) {
  const actor = await getActor();
  if (!actor) return NextResponse.json({ error: '로그인이 필요합니다.' }, { status: 401 });
  let body: unknown;
  try { body = await request.json(); } catch { return NextResponse.json({ error: '올바른 JSON이 아닙니다.' }, { status: 400 }); }
  if (!body || typeof body !== 'object') return NextResponse.json({ error: '입력이 비어 있습니다.' }, { status: 400 });
  const value = body as Record<string, unknown>;
  if (typeof value.agentId !== 'string' || !VALID_AGENTS.has(value.agentId)) return NextResponse.json({ error: '알 수 없는 에이전트입니다.' }, { status: 400 });
  if (typeof value.command !== 'string' || !value.command.trim() || value.command.length > 800) return NextResponse.json({ error: '명령은 1~800자여야 합니다.' }, { status: 400 });
  if (typeof value.idempotencyKey !== 'string' || !/^[a-zA-Z0-9_-]{8,100}$/.test(value.idempotencyKey)) return NextResponse.json({ error: '명령 식별자가 올바르지 않습니다.' }, { status: 400 });
  try {
    const result = await runtimeRequest<{ jobId: string; status: string; heardBy: string[]; agentName: string }>('/cashcow/api/commands', {
      method: 'POST',
      body: JSON.stringify({ agentId: value.agentId, command: value.command.trim(), actorId: actor.id, idempotencyKey: value.idempotencyKey }),
    });
    return NextResponse.json({ ...result, reply: `${result.agentName}에게 명령을 영속 작업 큐로 전달했습니다. 브라우저를 닫아도 서버에서 계속 실행됩니다.` }, { status: 202 });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : '명령을 접수하지 못했습니다.' }, { status: runtimeErrorStatus(error) });
  }
}
