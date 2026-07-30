/**
 * Mirrors the subset of schema_inference/models.py used by the extension.
 * Hand-maintained for now -- no shared codegen between the Pydantic models
 * and these interfaces yet. Keep in sync manually; a generator (e.g.
 * pydantic's JSON schema export -> quicktype) is a reasonable follow-up once
 * the wire shape stabilizes, not before.
 */

export interface ColumnMapping {
  source_column: string;
  source_table: string;
  target_field: string | null;
  confidence: number;
  method: 'rule' | 'llm' | 'manual' | 'critic';
  sql_expression: string;
  notes: string;
  name_similarity: number;
  type_compatibility: number;
  pattern_bonus: number;
}

export interface MappingProposal {
  source_name: string;
  table_name: string;
  mappings: ColumnMapping[];
  unmapped_columns: string[];
  missing_standard_fields: string[];
  contested_mappings: Array<{
    target_field: string;
    competing_columns: string[];
    confidences?: Record<string, number>;
    provisional_winner?: string;
  }>;
  excluded_metadata_columns: string[];
  row_shape: RowShapeProposal | null;
  run_id: string | null;
}

export interface RowShapeProposal {
  source_name: string;
  table_name: string;
  natural_key: string[];
  recency_column: string | null;
  dedup_strategy: 'row_number' | 'cdc_latest' | 'none';
  dedup_pattern: string | null;
  confidence: number;
  reasoning: string;
  run_id: string | null;
}

export interface ProfileRunResult {
  profile_path: string;
  source_name: string;
  table_name: string;
  row_count: number;
  column_count: number;
  delimiter: string;
}

export interface MapRunResult {
  proposal: MappingProposal;
  run_id: string | null;
  proposal_path?: string;
}

export interface ColumnChange {
  change_type: 'added' | 'removed' | 'renamed' | 'type_changed';
  column_name: string;
  old_value: string | null;
  new_value: string | null;
  rename_similarity: number | null;
  is_breaking: boolean;
}

export interface SchemaChangeReport {
  source_name: string;
  from_version: number;
  to_version: number;
  changes: ColumnChange[];
  has_breaking_changes: boolean;
  new_columns_for_mapping: string[];
}

export interface TrackerCheckResult {
  version: Record<string, unknown> | null;
  report: SchemaChangeReport | null;
  breaking: boolean;
}

export interface ReviewStatus {
  total_columns: number;
  decided_columns: number;
  pending_columns: string[];
  missing_standard_fields: string[];
  missing_fields_resolved: string[];
  contested_targets: string[];
  contests_resolved: string[];
}

export interface ReviewStartResult {
  session_id: string;
  source_name: string;
  table_name: string;
  mappings: ColumnMapping[];
  unmapped_columns: string[];
  missing_standard_fields: string[];
  contested_mappings: MappingProposal['contested_mappings'];
  excluded_metadata_columns: string[];
  row_shape: RowShapeProposal | null;
  status: ReviewStatus;
}

// ─── Self-tuning (Layer 0/1/2) ─────────────────────────────────────────────

export interface RuleWeights {
  name_sim: number;
  type_compat: number;
  pattern_bonus: number;
}

export interface RuleMetrics {
  mean_loss: number;
  f1: number;
  hard_f1: number;
}

export interface Layer0StatusResult {
  active_weights: RuleWeights;
  recent_runs: Array<{ run_id: string; table_name: string; recorded_at: string; config_snapshot: Record<string, unknown> }>;
  metamodel_available: boolean;
}

export interface RunLayer0Result {
  source_name: string;
  table_name: string;
  baseline_weights: RuleWeights;
  baseline_metrics: RuleMetrics;
  best_weights: RuleWeights;
  best_metrics: RuleMetrics;
  applied: boolean;
  top_candidates: Array<{ weights: RuleWeights } & RuleMetrics>;
}

export interface FewShotExample {
  example_id: string;
  source_name: string;
  source_column: string;
  target_field: string | null;
  sql_expression: string;
  reasoning: string;
  origin: string;
  status: string;
  added_at: string;
}

export interface FewShotStatsResult {
  active: FewShotExample[];
  active_count: number;
  retired_count: number;
  by_origin: Record<string, number>;
  metamodel_available: boolean;
}

export interface RunLayer1CurationResult {
  hard_tp_inserted: number;
  critic_inserted: number;
  skipped_existing: number;
  skipped_no_signature: number;
}

export interface PromptVersion {
  version_id: string;
  agent_name: string;
  prompt_text: string;
  parent_version_id: string | null;
  loss_before: number | null;
  loss_after: number | null;
  diagnosis: string | null;
  accepted: number;
  created_at: string;
  accepted_at: string | null;
}

export interface PromptVersionsResult {
  versions: PromptVersion[];
  active_prompt: string | null;
  metamodel_available: boolean;
}

export interface Layer2Round {
  round: number;
  version_id: string | null;
  loss_before: number;
  loss_after: number;
  improved: boolean;
  regressed: string[];
}

export interface Layer2SessionResult {
  baseline_loss: number;
  best_loss: number;
  best_version_id: string | null;
  rounds: Layer2Round[];
  determinism: { losses: number[]; mean: number; stdev: number } | null;
}

// ─── Snowflake as a mapping target ──────────────────────────────────────────

export interface DraftCanonicalField {
  name: string;
  target_type: string;
  required: boolean;
  description: string;
  aliases: string[];
  secondary_target: string | null;
}

export interface ExtractSnowflakeSchemaResult {
  schema_key: string;
  fields: DraftCanonicalField[];
}

export interface RegisterDynamicSchemaResult {
  registered: boolean;
  schema_key: string;
  table_names: string[];
}
