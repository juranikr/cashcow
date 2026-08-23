import { NextRequest, NextResponse } from 'next/server';
import { getActor } from '@/lib/server/auth';
import { runtimeErrorStatus, runtimeRequest } from '@/lib/server/runtime-client';

export async function GET(request: NextRequest) {
  const actor = await getActor();
  if (!actor) return NextResponse.json({ error: '로그인이 필요합니다.' }, { status: 401 });
  const rawVersion = request.nextUrl.searchParams.get('version');
  const version = Number(rawVersion);
  if (!Number.isInteger(version) || version < 1) return NextResponse.json({ error: '올바른 보드 버전이 필요합니다.' }, { status: 400 });
  try {
    const document = await runtimeRequest<Record<string, unknown>>(`/cashcow/api/whiteboard/${version}`);
    return NextResponse.json(document, { headers: { 'cache-control': 'no-store' } });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : '보드 버전을 불러오지 못했습니다.' }, { status: runtimeErrorStatus(error) });
  }
}

export async function PATCH(request: NextRequest) {
  const actor = await getActor();
  if (!actor) return NextResponse.json({ error: '로그인이 필요합니다.' }, { status: 401 });
  let body: unknown;
  try { body = await request.json(); } catch { return NextResponse.json({ error: '올바른 JSON이 아닙니다.' }, { status: 400 }); }
  if (!body || typeof body !== 'object') return NextResponse.json({ error: '입력이 비어 있습니다.' }, { status: 400 });
  try {
    const saved = await runtimeRequest<Record<string, unknown>>('/cashcow/api/whiteboard', {
      method: 'PATCH',
      body: JSON.stringify({ ...(body as Record<string, unknown>), actorId: actor.id, actorName: actor.displayName }),
    });
    return NextResponse.json(saved);
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : '화이트보드를 저장하지 못했습니다.' }, { status: runtimeErrorStatus(error) });
  }
}
