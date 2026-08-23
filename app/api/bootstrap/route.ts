import { NextResponse } from 'next/server';
import { getActor } from '@/lib/server/auth';
import { getBootstrap } from '@/lib/server/store';
import { runtimeErrorStatus, runtimeRequest } from '@/lib/server/runtime-client';

export const dynamic = 'force-dynamic';

export async function GET() {
  const actor = await getActor();
  if (!actor) return NextResponse.json({ error: '로그인이 필요합니다.' }, { status: 401 });
  try {
    const runtime = await runtimeRequest<Record<string, unknown>>(`/cashcow/api/bootstrap?actorId=${encodeURIComponent(actor.id)}`);
    const local = await getBootstrap(actor.id).catch((error) => {
      console.error('review_bootstrap_failed', error instanceof Error ? error.message : 'unknown');
      return { reviewSubmitted: false, cycle: null };
    });
    return NextResponse.json({ ...runtime, reviewSubmitted: local.reviewSubmitted, cycle: local.cycle }, { headers: { 'cache-control': 'no-store' } });
  } catch (error) {
    console.error('bootstrap_failed', error instanceof Error ? error.message : 'unknown');
    return NextResponse.json({ error: error instanceof Error ? error.message : '월드 상태를 불러오지 못했습니다.' }, { status: runtimeErrorStatus(error) });
  }
}
