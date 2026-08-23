import { env } from 'cloudflare:workers';
import { NextRequest, NextResponse } from 'next/server';
import { getActor } from '@/lib/server/auth';
import { commandRateAllowed, recordAgentCommand } from '@/lib/server/store';
import { redactSecrets, targetToolFromCommand } from '@/lib/world';

const VALID_AGENTS = new Set(['minji', 'doyun', 'harin', 'jun']);

type GroqPayload = {
  reply: string;
  plan: string[];
  lesson?: string;
};

function fallbackPlan(agentName: string, command: string): GroqPayload {
  const tool = targetToolFromCommand(command);
  const label = tool === 'computer' ? '컴퓨터' : tool === 'whiteboard' ? '화이트보드' : '회의실';
  return {
    reply: `${agentName}입니다. 요청을 들었어요. ${label}(으)로 이동해 완료 조건부터 확인하겠습니다.`,
    plan: ['요청의 목표와 완료 조건 확인', `${label}(으)로 이동해 사용 권한 확보`, '도구로 작업하고 결과 검증', '결과 보고와 근거 기반 교훈 저장'],
    lesson: '도구 실행 전에 완료 조건과 검증 방법을 명시한다.',
  };
}

function safeGroqResult(value: unknown, fallback: GroqPayload): GroqPayload {
  if (!value || typeof value !== 'object') return fallback;
  const item = value as Record<string, unknown>;
  const reply = typeof item.reply === 'string' ? redactSecrets(item.reply).slice(0, 500) : fallback.reply;
  const plan = Array.isArray(item.plan)
    ? item.plan.filter((step): step is string => typeof step === 'string' && step.trim().length > 0).slice(0, 5).map((step) => redactSecrets(step).slice(0, 120))
    : fallback.plan;
  const lesson = typeof item.lesson === 'string' ? redactSecrets(item.lesson).slice(0, 300) : fallback.lesson;
  return { reply, plan: plan.length >= 2 ? plan : fallback.plan, lesson };
}

async function askGroq(input: { agentName: string; role: string; rank: string; command: string; nearbyAgents: string[]; memories: unknown[] }): Promise<{ value: GroqPayload; engine: 'groq' | 'deterministic' }> {
  const fallback = fallbackPlan(input.agentName, input.command);
  const apiKey = env.GROQ_API_KEY || process.env.GROQ_API_KEY;
  if (!apiKey) return { value: fallback, engine: 'deterministic' };

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12_000);
  try {
    const response = await fetch('https://api.groq.com/openai/v1/chat/completions', {
      method: 'POST',
      signal: controller.signal,
      headers: { authorization: `Bearer ${apiKey}`, 'content-type': 'application/json' },
      body: JSON.stringify({
        model: env.GROQ_MODEL || process.env.GROQ_MODEL || 'openai/gpt-oss-120b',
        temperature: 0.2,
        max_completion_tokens: 550,
        response_format: { type: 'json_object' },
        messages: [
          {
            role: 'system',
            content: `당신은 픽셀 오피스의 ${input.role} ${input.agentName}(${input.rank})입니다. 대표의 근거리 명령에 응답하세요. 순간이동하지 말고 화이트보드·컴퓨터·회의실이라는 물리적 도구에 도착한 뒤에만 작업할 수 있습니다. 컴퓨터는 1인 점유입니다. 숨은 사고과정은 출력하지 말고, 관찰 가능한 짧은 답변과 2~5개 플랜, 재사용할 교훈 한 문장만 한국어 JSON으로 반환하세요. JSON 키는 reply, plan, lesson입니다. 도구 결과를 지어내지 말고 지금은 계획만 세우세요.`,
          },
          {
            role: 'user',
            content: JSON.stringify({ command: redactSecrets(input.command), nearbyAgents: input.nearbyAgents.slice(0, 8), priorLessons: input.memories.slice(0, 3) }),
          },
        ],
      }),
    });
    if (!response.ok) return { value: fallback, engine: 'deterministic' };
    const data = await response.json() as { choices?: Array<{ message?: { content?: string } }> };
    const content = data.choices?.[0]?.message?.content;
    if (!content) return { value: fallback, engine: 'deterministic' };
    return { value: safeGroqResult(JSON.parse(content), fallback), engine: 'groq' };
  } catch {
    return { value: fallback, engine: 'deterministic' };
  } finally {
    clearTimeout(timeout);
  }
}

export async function POST(request: NextRequest) {
  const actor = await getActor();
  if (!actor) return NextResponse.json({ error: '로그인이 필요합니다.' }, { status: 401 });
  let body: unknown;
  try { body = await request.json(); } catch { return NextResponse.json({ error: '올바른 JSON이 아닙니다.' }, { status: 400 }); }
  if (!body || typeof body !== 'object') return NextResponse.json({ error: '입력이 비어 있습니다.' }, { status: 400 });
  const value = body as Record<string, unknown>;
  if (typeof value.agentId !== 'string' || !VALID_AGENTS.has(value.agentId)) return NextResponse.json({ error: '알 수 없는 에이전트입니다.' }, { status: 400 });
  if (typeof value.command !== 'string' || !value.command.trim() || value.command.length > 800) return NextResponse.json({ error: '명령은 1~800자여야 합니다.' }, { status: 400 });
  if (!(await commandRateAllowed(value.agentId))) return NextResponse.json({ error: '명령이 너무 빠릅니다. 잠시 후 다시 시도해 주세요.' }, { status: 429 });

  const input = {
    agentName: typeof value.agentName === 'string' ? value.agentName.slice(0, 20) : value.agentId,
    role: typeof value.role === 'string' ? value.role.slice(0, 60) : '에이전트',
    rank: typeof value.rank === 'string' ? value.rank.slice(0, 30) : '멤버',
    command: value.command.trim(),
    nearbyAgents: Array.isArray(value.nearbyAgents) ? value.nearbyAgents.filter((name): name is string => typeof name === 'string').slice(0, 8) : [],
    memories: Array.isArray(value.memories) ? value.memories.slice(0, 3) : [],
  };
  const result = await askGroq(input);
  const tool = targetToolFromCommand(input.command);
  await recordAgentCommand({ agentId: value.agentId, command: input.command, plan: result.value.plan, reply: result.value.reply, tool, lesson: result.value.lesson });
  return NextResponse.json({ ...result.value, engine: result.engine });
}
