import { NextRequest, NextResponse } from 'next/server';
import { getActor } from '@/lib/server/auth';
import { runtimeErrorStatus, runtimeRequest } from '@/lib/server/runtime-client';

export async function GET(_request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const actor = await getActor();
  if (!actor) return NextResponse.json({ error: '로그인이 필요합니다.' }, { status: 401 });
  const { id } = await context.params;
  if (!/^[a-f0-9-]{20,50}$/i.test(id)) return NextResponse.json({ error: '작업 ID가 올바르지 않습니다.' }, { status: 400 });
  try {
    const detail = await runtimeRequest<Record<string, unknown>>(`/cashcow/api/jobs/${encodeURIComponent(id)}`);
    return NextResponse.json(detail, { headers: { 'cache-control': 'no-store' } });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : '작업 감사 로그를 불러오지 못했습니다.' }, { status: runtimeErrorStatus(error) });
  }
}
