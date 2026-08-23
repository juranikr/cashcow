import { env } from 'cloudflare:workers';

type RuntimeErrorBody = { detail?: string; error?: string };

function runtimeBaseUrl() {
  const value = (env.RUNTIME_BASE_URL || process.env.RUNTIME_BASE_URL || '').trim().replace(/\/$/, '');
  if (!value) throw new Error('영속 월드 런타임 주소가 설정되지 않았습니다.');
  if (process.env.NODE_ENV === 'production' && !value.startsWith('https://')) throw new Error('운영 런타임은 HTTPS 주소여야 합니다.');
  return value;
}

function runtimeToken() {
  const value = (env.RUNTIME_SERVICE_TOKEN || process.env.RUNTIME_SERVICE_TOKEN || '').trim();
  if (!value) throw new Error('영속 월드 런타임 인증이 설정되지 않았습니다.');
  return value;
}

export class RuntimeResponseError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'RuntimeResponseError';
    this.status = status;
  }
}

export async function runtimeRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 125_000);
  try {
    const response = await fetch(`${runtimeBaseUrl()}${path}`, {
      ...init,
      cache: 'no-store',
      signal: controller.signal,
      headers: {
        'content-type': 'application/json',
        'x-cashcow-service-token': runtimeToken(),
        ...(init.headers ?? {}),
      },
    });
    let body: unknown = null;
    try { body = await response.json(); } catch { body = null; }
    if (!response.ok) {
      const value = body as RuntimeErrorBody | null;
      throw new RuntimeResponseError(response.status, value?.detail ?? value?.error ?? `영속 런타임 오류 (${response.status})`);
    }
    return body as T;
  } catch (error) {
    if (error instanceof RuntimeResponseError) throw error;
    if (controller.signal.aborted) throw new RuntimeResponseError(504, '영속 월드 런타임 응답 시간이 초과됐습니다.');
    if (error instanceof Error && error.message.startsWith('영속 월드 런타임')) throw new RuntimeResponseError(503, error.message);
    throw new RuntimeResponseError(503, '영속 월드 런타임에 연결하지 못했습니다.');
  } finally {
    clearTimeout(timeout);
  }
}

export function runtimeErrorStatus(error: unknown) {
  return error instanceof RuntimeResponseError ? error.status : 503;
}
