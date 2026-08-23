import { NextRequest, NextResponse } from 'next/server';
import { getActor } from '@/lib/server/auth';
import { updateWhiteboard } from '@/lib/server/store';

const HEX_COLOR = /^#[0-9a-f]{6}$/i;

export async function PATCH(request: NextRequest) {
  const actor = await getActor();
  if (!actor) return NextResponse.json({ error: '로그인이 필요합니다.' }, { status: 401 });
  let body: unknown;
  try { body = await request.json(); } catch { return NextResponse.json({ error: '올바른 JSON이 아닙니다.' }, { status: 400 }); }
  if (!body || typeof body !== 'object') return NextResponse.json({ error: '입력이 비어 있습니다.' }, { status: 400 });
  const value = body as Record<string, unknown>;
  if (typeof value.title !== 'string' || !value.title.trim() || value.title.length > 30) return NextResponse.json({ error: '보드 제목은 1~30자여야 합니다.' }, { status: 400 });
  if (typeof value.text !== 'string' || value.text.length > 400) return NextResponse.json({ error: '글 영역은 400자 이하여야 합니다.' }, { status: 400 });
  if (!Array.isArray(value.pixels) || value.pixels.length !== 160 || value.pixels.some((pixel) => typeof pixel !== 'string' || !HEX_COLOR.test(pixel))) return NextResponse.json({ error: '도트 영역은 16×10 HEX 색상이어야 합니다.' }, { status: 400 });
  if (!Number.isInteger(value.expectedVersion) || Number(value.expectedVersion) < 1) return NextResponse.json({ error: '보드 버전이 올바르지 않습니다.' }, { status: 400 });
  try {
    const saved = await updateWhiteboard({ title: value.title.trim(), text: value.text, pixels: value.pixels as string[], expectedVersion: Number(value.expectedVersion) }, actor.id);
    if (!saved) return NextResponse.json({ error: '다른 에이전트가 먼저 수정했습니다. 보드를 다시 열어 주세요.' }, { status: 409 });
    return NextResponse.json(saved);
  } catch (error) {
    console.error('whiteboard_update_failed', error instanceof Error ? error.message : 'unknown');
    return NextResponse.json({ error: '화이트보드를 저장하지 못했습니다.' }, { status: 503 });
  }
}
