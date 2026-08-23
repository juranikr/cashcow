import { env } from 'cloudflare:workers';
import { kstReviewCycle, redactSecrets } from '@/lib/world';

const CREATE_STATEMENTS = [
  `CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    team TEXT NOT NULL,
    role TEXT NOT NULL,
    rank TEXT NOT NULL,
    policy_version INTEGER NOT NULL DEFAULT 1,
    score INTEGER NOT NULL DEFAULT 80,
    updated_at TEXT NOT NULL
  )`,
  `CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    command TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
  )`,
  `CREATE INDEX IF NOT EXISTS idx_plans_agent_status ON plans(agent_id, status)`,
  `CREATE TABLE IF NOT EXISTS run_events (
    id TEXT PRIMARY KEY,
    plan_id TEXT REFERENCES plans(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    event_type TEXT NOT NULL,
    tool TEXT,
    input_summary TEXT,
    output_summary TEXT,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
  )`,
  `CREATE INDEX IF NOT EXISTS idx_run_events_agent_created ON run_events(agent_id, created_at)`,
  `CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence TEXT NOT NULL,
    confidence INTEGER NOT NULL,
    created_at TEXT NOT NULL
  )`,
  `CREATE INDEX IF NOT EXISTS idx_memories_agent_kind ON memories(agent_id, kind)`,
  `CREATE TABLE IF NOT EXISTS whiteboards (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    text_content TEXT NOT NULL,
    pixels_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
  )`,
  `CREATE TABLE IF NOT EXISTS world_controls (
    id TEXT PRIMARY KEY,
    agents_paused INTEGER NOT NULL DEFAULT 0,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
  )`,
  `CREATE TABLE IF NOT EXISTS review_cycles (
    id TEXT PRIMARY KEY,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
  )`,
  `CREATE INDEX IF NOT EXISTS idx_review_cycles_status_ends ON review_cycles(status, ends_at)`,
  `CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL REFERENCES review_cycles(id),
    reviewer_id TEXT NOT NULL,
    individual_score INTEGER NOT NULL,
    team_score INTEGER NOT NULL,
    comment TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(cycle_id, reviewer_id)
  )`,
  `CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
  )`,
  `CREATE INDEX IF NOT EXISTS idx_audit_entity_created ON audit_log(entity_type, entity_id, created_at)`,
  `CREATE TABLE IF NOT EXISTS chat_deliveries (
    id TEXT PRIMARY KEY,
    speaker_id TEXT NOT NULL,
    recipient_agent_id TEXT NOT NULL REFERENCES agents(id),
    message_summary TEXT NOT NULL,
    distance INTEGER NOT NULL,
    delivered INTEGER NOT NULL,
    created_at TEXT NOT NULL
  )`,
  `CREATE INDEX IF NOT EXISTS idx_chat_deliveries_recipient_created ON chat_deliveries(recipient_agent_id, created_at)`,
  `CREATE TABLE IF NOT EXISTS shared_knowledge (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    source_url TEXT,
    author_agent_id TEXT REFERENCES agents(id),
    created_at TEXT NOT NULL
  )`,
  `CREATE INDEX IF NOT EXISTS idx_shared_knowledge_created ON shared_knowledge(created_at)`,
];

const DEFAULT_PIXELS = Array.from({ length: 160 }, () => '#f9f4df');
const AGENTS = [
  ['minji', '민지', '전략팀', '프로젝트 매니저', '리드', 91],
  ['doyun', '도윤', '리서치팀', '리서처', '시니어', 88],
  ['harin', '하린', '제품팀', '프로덕트 디자이너', '주니어', 83],
  ['jun', '준', '플랫폼팀', '소프트웨어 엔지니어', '시니어', 94],
] as const;

let initialized = false;

function db(): D1Database {
  if (!env.DB) throw new Error('D1 database binding is unavailable.');
  return env.DB;
}

export async function ensureDatabase(actorId = 'system') {
  if (!initialized) {
    await db().batch(CREATE_STATEMENTS.map((statement) => db().prepare(statement)));
    await db().prepare('PRAGMA optimize').run();
    initialized = true;
  }

  const now = new Date();
  const stamp = now.toISOString();
  const cycle = kstReviewCycle(now);
  const seeds = AGENTS.map((agent) => db().prepare(
    `INSERT OR IGNORE INTO agents (id, name, team, role, rank, policy_version, score, updated_at)
     VALUES (?, ?, ?, ?, ?, 1, ?, ?)`,
  ).bind(...agent, stamp));
  seeds.push(db().prepare(
    `INSERT OR IGNORE INTO whiteboards (id, title, text_content, pixels_json, version, updated_by, updated_at)
     VALUES ('main', 'SPRINT 08', '목표 · 진행 · 배운 점', ?, 1, ?, ?)`,
  ).bind(JSON.stringify(DEFAULT_PIXELS), actorId, stamp));
  seeds.push(db().prepare(
    `INSERT OR IGNORE INTO world_controls (id, agents_paused, updated_by, updated_at)
     VALUES ('main', 0, ?, ?)`,
  ).bind(actorId, stamp));
  seeds.push(db().prepare(
    `INSERT OR IGNORE INTO review_cycles (id, starts_at, ends_at, status, created_at)
     VALUES (?, ?, ?, 'open', ?)`,
  ).bind(cycle.id, cycle.startsAt.toISOString(), cycle.endsAt.toISOString(), stamp));
  seeds.push(db().prepare(
    `UPDATE review_cycles
     SET status = 'no_review'
     WHERE status = 'open' AND ends_at <= ?
       AND NOT EXISTS (SELECT 1 FROM reviews WHERE reviews.cycle_id = review_cycles.id)`,
  ).bind(stamp));
  await db().batch(seeds);
  return cycle;
}

export async function getBootstrap(actorId: string) {
  const cycle = await ensureDatabase(actorId);
  const board = await db().prepare(
    `SELECT title, text_content, pixels_json, version FROM whiteboards WHERE id = 'main'`,
  ).first<{ title: string; text_content: string; pixels_json: string; version: number }>();
  const review = await db().prepare(
    `SELECT id FROM reviews WHERE cycle_id = ? AND reviewer_id = ? LIMIT 1`,
  ).bind(cycle.id, actorId).first<{ id: string }>();
  const controls = await db().prepare(
    `SELECT agents_paused FROM world_controls WHERE id = 'main'`,
  ).first<{ agents_paused: number }>();
  return {
    whiteboard: board ? { title: board.title, text: board.text_content, pixels: JSON.parse(board.pixels_json) as string[], version: board.version } : null,
    reviewSubmitted: Boolean(review),
    agentsPaused: Boolean(controls?.agents_paused),
    cycle: { id: cycle.id, startsAt: cycle.startsAt.toISOString(), endsAt: cycle.endsAt.toISOString() },
  };
}

export async function getAgentsPaused() {
  if (!initialized) await ensureDatabase();
  const controls = await db().prepare(
    `SELECT agents_paused FROM world_controls WHERE id = 'main'`,
  ).first<{ agents_paused: number }>();
  return Boolean(controls?.agents_paused);
}

export async function setAgentsPaused(agentsPaused: boolean, actorId: string) {
  await ensureDatabase(actorId);
  const updatedAt = new Date().toISOString();
  await db().batch([
    db().prepare(
      `INSERT INTO world_controls (id, agents_paused, updated_by, updated_at)
       VALUES ('main', ?, ?, ?)
       ON CONFLICT(id) DO UPDATE SET
         agents_paused = excluded.agents_paused,
         updated_by = excluded.updated_by,
         updated_at = excluded.updated_at`,
    ).bind(agentsPaused ? 1 : 0, actorId, updatedAt),
    db().prepare(
      `INSERT INTO audit_log (id, actor_id, action, entity_type, entity_id, summary, created_at)
       VALUES (?, ?, ?, 'world_control', 'main', ?, ?)`,
    ).bind(
      crypto.randomUUID(),
      actorId,
      agentsPaused ? 'agents_paused' : 'agents_resumed',
      agentsPaused ? '에이전트 활동 정지' : '에이전트 활동 재개',
      updatedAt,
    ),
  ]);
  return { agentsPaused, updatedAt };
}

export async function updateWhiteboard(input: { title: string; text: string; pixels: string[]; expectedVersion: number }, actorId: string) {
  await ensureDatabase(actorId);
  const nextVersion = input.expectedVersion + 1;
  const result = await db().prepare(
    `UPDATE whiteboards
     SET title = ?, text_content = ?, pixels_json = ?, version = ?, updated_by = ?, updated_at = ?
     WHERE id = 'main' AND version = ?`,
  ).bind(
    redactSecrets(input.title).slice(0, 30),
    redactSecrets(input.text).slice(0, 400),
    JSON.stringify(input.pixels),
    nextVersion,
    actorId,
    new Date().toISOString(),
    input.expectedVersion,
  ).run();
  if (!result.meta.changes) return null;
  return { title: input.title.slice(0, 30), text: input.text.slice(0, 400), pixels: input.pixels, version: nextVersion };
}

export async function commandRateAllowed(agentId: string): Promise<boolean> {
  await ensureDatabase();
  const since = new Date(Date.now() - 60_000).toISOString();
  const row = await db().prepare(
    `SELECT COUNT(*) AS count FROM run_events WHERE agent_id = ? AND event_type = 'command_received' AND created_at >= ?`,
  ).bind(agentId, since).first<{ count: number }>();
  return Number(row?.count ?? 0) < 12;
}

export async function recordAgentCommand(input: { agentId: string; command: string; plan: string[]; reply: string; tool: string; lesson?: string }) {
  await ensureDatabase();
  const now = new Date().toISOString();
  const planId = crypto.randomUUID();
  const statements = [
    db().prepare(
      `INSERT INTO plans (id, agent_id, command, steps_json, status, created_at, updated_at)
       SELECT ?, ?, ?, ?, 'active', ?, ?
       WHERE EXISTS (SELECT 1 FROM world_controls WHERE id = 'main' AND agents_paused = 0)`,
    ).bind(planId, input.agentId, redactSecrets(input.command), JSON.stringify(input.plan), now, now),
    db().prepare(
      `INSERT INTO run_events (id, plan_id, agent_id, event_type, tool, input_summary, output_summary, result, created_at)
       SELECT ?, ?, ?, 'command_received', 'nearby_chat', ?, ?, 'success', ?
       WHERE EXISTS (SELECT 1 FROM plans WHERE id = ?)
         AND EXISTS (SELECT 1 FROM world_controls WHERE id = 'main' AND agents_paused = 0)`,
    ).bind(crypto.randomUUID(), planId, input.agentId, redactSecrets(input.command), redactSecrets(input.reply), now, planId),
  ];
  if (input.lesson) {
    statements.push(db().prepare(
      `INSERT INTO memories (id, agent_id, kind, summary, evidence, confidence, created_at)
       SELECT ?, ?, 'lesson', ?, ?, 75, ?
       WHERE EXISTS (SELECT 1 FROM plans WHERE id = ?)
         AND EXISTS (SELECT 1 FROM world_controls WHERE id = 'main' AND agents_paused = 0)`,
    ).bind(crypto.randomUUID(), input.agentId, redactSecrets(input.lesson), `plan_${planId}`, now, planId));
  }
  const results = await db().batch(statements);
  if (!Number(results[0].meta.changes ?? 0)) return null;
  return planId;
}

export async function saveReview(input: { cycleId: string; individual: number; team: number; comment: string; agentIds: string[] }, actorId: string) {
  const cycle = await ensureDatabase(actorId);
  if (input.cycleId !== cycle.id) throw new Error('현재 평가 주기만 제출할 수 있습니다.');
  const now = new Date().toISOString();
  const reviewId = crypto.randomUUID();
  const comment = redactSecrets(input.comment).slice(0, 1200);
  const statements = [
    db().prepare(
      `INSERT INTO reviews (id, cycle_id, reviewer_id, individual_score, team_score, comment, created_at)
       SELECT ?, ?, ?, ?, ?, ?, ?
       WHERE EXISTS (SELECT 1 FROM world_controls WHERE id = 'main' AND agents_paused = 0)
       ON CONFLICT(cycle_id, reviewer_id) DO UPDATE SET
         individual_score = excluded.individual_score,
         team_score = excluded.team_score,
         comment = excluded.comment,
         created_at = excluded.created_at`,
    ).bind(reviewId, input.cycleId, actorId, input.individual, input.team, comment, now),
    db().prepare(
      `UPDATE review_cycles SET status = 'reviewed'
       WHERE id = ? AND EXISTS (SELECT 1 FROM world_controls WHERE id = 'main' AND agents_paused = 0)`,
    ).bind(input.cycleId),
    db().prepare(
      `INSERT INTO audit_log (id, actor_id, action, entity_type, entity_id, summary, created_at)
       SELECT ?, ?, 'weekly_review_submitted', 'review_cycle', ?, ?, ?
       WHERE EXISTS (SELECT 1 FROM world_controls WHERE id = 'main' AND agents_paused = 0)`,
    ).bind(crypto.randomUUID(), actorId, input.cycleId, `개인 ${input.individual}/5 · 팀 ${input.team}/5`, now),
  ];
  input.agentIds.forEach((agentId) => {
    statements.push(db().prepare(
      `INSERT INTO memories (id, agent_id, kind, summary, evidence, confidence, created_at)
       SELECT ?, ?, 'lesson', ?, ?, 95, ?
       WHERE EXISTS (SELECT 1 FROM world_controls WHERE id = 'main' AND agents_paused = 0)`,
    ).bind(crypto.randomUUID(), agentId, comment || `개인 ${input.individual}점과 팀 ${input.team}점 평가를 다음 행동에 반영한다.`, `weekly_review_${input.cycleId}`, now));
    statements.push(db().prepare(
      `UPDATE agents SET policy_version = policy_version + 1, score = ?, updated_at = ?
       WHERE id = ? AND EXISTS (SELECT 1 FROM world_controls WHERE id = 'main' AND agents_paused = 0)`,
    ).bind(Math.round(((input.individual + input.team) / 10) * 100), now, agentId));
  });
  const results = await db().batch(statements);
  if (!Number(results[0].meta.changes ?? 0)) throw new Error('에이전트가 일시정지되어 평가를 반영할 수 없습니다.');
  return { reviewId, reflectedAgents: input.agentIds.length };
}
