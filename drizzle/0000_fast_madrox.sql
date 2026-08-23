CREATE TABLE `agents` (
	`id` text PRIMARY KEY NOT NULL,
	`name` text NOT NULL,
	`team` text NOT NULL,
	`role` text NOT NULL,
	`rank` text NOT NULL,
	`policy_version` integer DEFAULT 1 NOT NULL,
	`score` integer DEFAULT 80 NOT NULL,
	`updated_at` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `audit_log` (
	`id` text PRIMARY KEY NOT NULL,
	`actor_id` text NOT NULL,
	`action` text NOT NULL,
	`entity_type` text NOT NULL,
	`entity_id` text NOT NULL,
	`summary` text NOT NULL,
	`created_at` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `idx_audit_entity_created` ON `audit_log` (`entity_type`,`entity_id`,`created_at`);--> statement-breakpoint
CREATE TABLE `chat_deliveries` (
	`id` text PRIMARY KEY NOT NULL,
	`speaker_id` text NOT NULL,
	`recipient_agent_id` text NOT NULL,
	`message_summary` text NOT NULL,
	`distance` integer NOT NULL,
	`delivered` integer NOT NULL,
	`created_at` text NOT NULL,
	FOREIGN KEY (`recipient_agent_id`) REFERENCES `agents`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE INDEX `idx_chat_deliveries_recipient_created` ON `chat_deliveries` (`recipient_agent_id`,`created_at`);--> statement-breakpoint
CREATE TABLE `memories` (
	`id` text PRIMARY KEY NOT NULL,
	`agent_id` text NOT NULL,
	`kind` text NOT NULL,
	`summary` text NOT NULL,
	`evidence` text NOT NULL,
	`confidence` integer NOT NULL,
	`created_at` text NOT NULL,
	FOREIGN KEY (`agent_id`) REFERENCES `agents`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE INDEX `idx_memories_agent_kind` ON `memories` (`agent_id`,`kind`);--> statement-breakpoint
CREATE TABLE `plans` (
	`id` text PRIMARY KEY NOT NULL,
	`agent_id` text NOT NULL,
	`command` text NOT NULL,
	`steps_json` text NOT NULL,
	`status` text NOT NULL,
	`created_at` text NOT NULL,
	`updated_at` text NOT NULL,
	FOREIGN KEY (`agent_id`) REFERENCES `agents`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE INDEX `idx_plans_agent_status` ON `plans` (`agent_id`,`status`);--> statement-breakpoint
CREATE TABLE `review_cycles` (
	`id` text PRIMARY KEY NOT NULL,
	`starts_at` text NOT NULL,
	`ends_at` text NOT NULL,
	`status` text NOT NULL,
	`created_at` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `idx_review_cycles_status_ends` ON `review_cycles` (`status`,`ends_at`);--> statement-breakpoint
CREATE TABLE `reviews` (
	`id` text PRIMARY KEY NOT NULL,
	`cycle_id` text NOT NULL,
	`reviewer_id` text NOT NULL,
	`individual_score` integer NOT NULL,
	`team_score` integer NOT NULL,
	`comment` text NOT NULL,
	`created_at` text NOT NULL,
	FOREIGN KEY (`cycle_id`) REFERENCES `review_cycles`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE UNIQUE INDEX `idx_reviews_cycle_reviewer` ON `reviews` (`cycle_id`,`reviewer_id`);--> statement-breakpoint
CREATE TABLE `run_events` (
	`id` text PRIMARY KEY NOT NULL,
	`plan_id` text,
	`agent_id` text NOT NULL,
	`event_type` text NOT NULL,
	`tool` text,
	`input_summary` text,
	`output_summary` text,
	`result` text NOT NULL,
	`created_at` text NOT NULL,
	FOREIGN KEY (`plan_id`) REFERENCES `plans`(`id`) ON UPDATE no action ON DELETE no action,
	FOREIGN KEY (`agent_id`) REFERENCES `agents`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE INDEX `idx_run_events_agent_created` ON `run_events` (`agent_id`,`created_at`);--> statement-breakpoint
CREATE TABLE `shared_knowledge` (
	`id` text PRIMARY KEY NOT NULL,
	`title` text NOT NULL,
	`body` text NOT NULL,
	`source_url` text,
	`author_agent_id` text,
	`created_at` text NOT NULL,
	FOREIGN KEY (`author_agent_id`) REFERENCES `agents`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE INDEX `idx_shared_knowledge_created` ON `shared_knowledge` (`created_at`);--> statement-breakpoint
CREATE TABLE `whiteboards` (
	`id` text PRIMARY KEY NOT NULL,
	`title` text NOT NULL,
	`text_content` text NOT NULL,
	`pixels_json` text NOT NULL,
	`version` integer DEFAULT 1 NOT NULL,
	`updated_by` text NOT NULL,
	`updated_at` text NOT NULL
);
--> statement-breakpoint
PRAGMA optimize;
