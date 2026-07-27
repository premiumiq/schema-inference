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
