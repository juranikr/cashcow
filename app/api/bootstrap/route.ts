import { NextResponse } from 'next/server';
import { getActor } from '@/lib/server/auth';
import { getBootstrap } from '@/lib/server/store';

export const dynamic = 'force-dynamic';

export async function GET() {
  const actor = await getActor();
  if (!actor) return NextResponse.json({ error: '로그인이 필요합니다.' }, { status: 401 });
  try {
    return NextResponse.json(await getBootstrap(actor.id), { headers: { 'cache-control': 'no-store' } });
  } catch (error) {
    console.error('bootstrap_failed', error instanceof Error ? error.message : 'unknown');
    return NextResponse.json({ error: '월드 상태를 불러오지 못했습니다.' }, { status: 503 });
  }
}
