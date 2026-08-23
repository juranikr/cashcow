CREATE TABLE `world_controls` (
	`id` text PRIMARY KEY NOT NULL,
	`agents_paused` integer DEFAULT false NOT NULL,
	`updated_by` text NOT NULL,
	`updated_at` text NOT NULL
);
