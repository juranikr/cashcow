import { index, integer, sqliteTable, text, uniqueIndex } from 'drizzle-orm/sqlite-core';

export const agents = sqliteTable('agents', {
  id: text('id').primaryKey(),
  name: text('name').notNull(),
  team: text('team').notNull(),
  role: text('role').notNull(),
  rank: text('rank').notNull(),
  policyVersion: integer('policy_version').notNull().default(1),
  score: integer('score').notNull().default(80),
  updatedAt: text('updated_at').notNull(),
});

export const plans = sqliteTable('plans', {
  id: text('id').primaryKey(),
  agentId: text('agent_id').notNull().references(() => agents.id),
  command: text('command').notNull(),
  stepsJson: text('steps_json').notNull(),
  status: text('status').notNull(),
  createdAt: text('created_at').notNull(),
  updatedAt: text('updated_at').notNull(),
}, (table) => [index('idx_plans_agent_status').on(table.agentId, table.status)]);

export const runEvents = sqliteTable('run_events', {
  id: text('id').primaryKey(),
  planId: text('plan_id').references(() => plans.id),
  agentId: text('agent_id').notNull().references(() => agents.id),
  eventType: text('event_type').notNull(),
  tool: text('tool'),
  inputSummary: text('input_summary'),
  outputSummary: text('output_summary'),
  result: text('result').notNull(),
  createdAt: text('created_at').notNull(),
}, (table) => [index('idx_run_events_agent_created').on(table.agentId, table.createdAt)]);

export const memories = sqliteTable('memories', {
  id: text('id').primaryKey(),
  agentId: text('agent_id').notNull().references(() => agents.id),
  kind: text('kind').notNull(),
  summary: text('summary').notNull(),
  evidence: text('evidence').notNull(),
  confidence: integer('confidence').notNull(),
  createdAt: text('created_at').notNull(),
}, (table) => [index('idx_memories_agent_kind').on(table.agentId, table.kind)]);

export const whiteboards = sqliteTable('whiteboards', {
  id: text('id').primaryKey(),
  title: text('title').notNull(),
  textContent: text('text_content').notNull(),
  pixelsJson: text('pixels_json').notNull(),
  version: integer('version').notNull().default(1),
  updatedBy: text('updated_by').notNull(),
  updatedAt: text('updated_at').notNull(),
});

export const worldControls = sqliteTable('world_controls', {
  id: text('id').primaryKey(),
  agentsPaused: integer('agents_paused', { mode: 'boolean' }).notNull().default(false),
  updatedBy: text('updated_by').notNull(),
  updatedAt: text('updated_at').notNull(),
});

export const reviewCycles = sqliteTable('review_cycles', {
  id: text('id').primaryKey(),
  startsAt: text('starts_at').notNull(),
  endsAt: text('ends_at').notNull(),
  status: text('status').notNull(),
  createdAt: text('created_at').notNull(),
}, (table) => [index('idx_review_cycles_status_ends').on(table.status, table.endsAt)]);

export const reviews = sqliteTable('reviews', {
  id: text('id').primaryKey(),
  cycleId: text('cycle_id').notNull().references(() => reviewCycles.id),
  reviewerId: text('reviewer_id').notNull(),
  individualScore: integer('individual_score').notNull(),
  teamScore: integer('team_score').notNull(),
  comment: text('comment').notNull(),
  createdAt: text('created_at').notNull(),
}, (table) => [uniqueIndex('idx_reviews_cycle_reviewer').on(table.cycleId, table.reviewerId)]);

export const chatDeliveries = sqliteTable('chat_deliveries', {
  id: text('id').primaryKey(),
  speakerId: text('speaker_id').notNull(),
  recipientAgentId: text('recipient_agent_id').notNull().references(() => agents.id),
  messageSummary: text('message_summary').notNull(),
  distance: integer('distance').notNull(),
  delivered: integer('delivered', { mode: 'boolean' }).notNull(),
  createdAt: text('created_at').notNull(),
}, (table) => [index('idx_chat_deliveries_recipient_created').on(table.recipientAgentId, table.createdAt)]);

export const sharedKnowledge = sqliteTable('shared_knowledge', {
  id: text('id').primaryKey(),
  title: text('title').notNull(),
  body: text('body').notNull(),
  sourceUrl: text('source_url'),
  authorAgentId: text('author_agent_id').references(() => agents.id),
  createdAt: text('created_at').notNull(),
}, (table) => [index('idx_shared_knowledge_created').on(table.createdAt)]);

export const auditLog = sqliteTable('audit_log', {
  id: text('id').primaryKey(),
  actorId: text('actor_id').notNull(),
  action: text('action').notNull(),
  entityType: text('entity_type').notNull(),
  entityId: text('entity_id').notNull(),
  summary: text('summary').notNull(),
  createdAt: text('created_at').notNull(),
}, (table) => [index('idx_audit_entity_created').on(table.entityType, table.entityId, table.createdAt)]);
