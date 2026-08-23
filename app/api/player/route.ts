import { NextRequest, NextResponse } from 'next/server';
import { getActor } from '@/lib/server/auth';
import { runtimeErrorStatus, runtimeRequest } from '@/lib/server/runtime-client';

const FACINGS = new Set(['north', 'east', 'south', 'west']);

export async function PATCH(request: NextRequest) {
  const actor = await getActor();
  if (!actor) return NextResponse.json({ error: '로그인이 필요합니다.' }, { status: 401 });
  let body: unknown;
  try { body = await request.json(); } catch { return NextResponse.json({ error: '올바른 JSON이 아닙니다.' }, { status: 400 }); }
  if (!body || typeof body !== 'object') return NextResponse.json({ error: '입력이 비어 있습니다.' }, { status: 400 });
  const value = body as Record<string, unknown>;
  const x = Number(value.x);
  const y = Number(value.y);
  if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || x > 100 || y < 0 || y > 100 || typeof value.facing !== 'string' || !FACINGS.has(value.facing)) {
    return NextResponse.json({ error: '이동 좌표가 올바르지 않습니다.' }, { status: 400 });
  }
  try {
    const player = await runtimeRequest<Record<string, unknown>>('/cashcow/api/player', {
      method: 'PATCH', body: JSON.stringify({ x, y, facing: value.facing, actorId: actor.id }),
    });
    return NextResponse.json(player, { headers: { 'cache-control': 'no-store' } });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : '대표 위치를 동기화하지 못했습니다.' }, { status: runtimeErrorStatus(error) });
  }
}
