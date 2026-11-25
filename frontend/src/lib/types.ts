export interface IsoMessage {
  id: string;
  mti: "0200" | "0210" | "0420" | "0430" | string;
  direction: "INBOUND" | "OUTBOUND" | string;
  stan: string;
  rrn: string;
  response_code: string | null;
  payload_json: {
    mti: string;
    de2: string;
    de3: string;
    de4: number;
    de7: string;
    de11: string;
    de37: string;
    de38?: string | null;
    de39?: string | null;
    de49: string;
    de90?: string | null;
    de102: string;
    de103: string;
  };
  raw_iso_snippet: string;
  signature_key_id?: string | null;
  signature_verified?: boolean;
  created_at: string;
}

export interface JournalPosting {
  id: string;
  account_id: string;
  account_number: string | null;
  institution_code: string | null;
  direction: "DEBIT" | "CREDIT";
  amount_centavos: number;
  created_at: string;
}

export interface JournalEntry {
  id: string;
  transaction_id: string | null;
  entry_type:
    | "CLEARING_HOLD"
    | "SETTLEMENT"
    | "REVERSAL"
    | "RECON_ADJUSTMENT"
    | "LIQUIDITY_TOPUP"
    | string;
  description: string;
  created_at: string;
  postings: JournalPosting[];
}

export type TransactionStatus =
  | "INITIATED"
  | "ROUTED"
  | "COMPLETED"
  | "DECLINED"
  | "TIMED_OUT"
  | "REVERSAL_SENT"
  | "REVERSED"
  | "RECON_HOLD";

export interface SwitchTransaction {
  id: string;
  idempotency_key: string;
  stan: string;
  rrn: string;
  transmission_datetime: string;
  originator_code: string;
  beneficiary_code: string;
  originator_account_id: string;
  beneficiary_account_id: string;
  originator_account_number: string;
  beneficiary_account_number: string;
  originator_hold_ref?: string | null;
  amount_centavos: number;
  currency: string;
  status: TransactionStatus;
  response_code: string | null;
  response_description: string | null;
  auth_id: string | null;
  failure_reason: string | null;
  fraud_score: number;
  fraud_status: string;
  processing_latency_ms?: number | null;
  signature_verified: boolean;
  settlement_cycle_id?: string | null;
  sla_deadline_at: string;
  created_at: string;
  updated_at: string;
  iso_messages: IsoMessage[];
  journal_entries: JournalEntry[];
}

export interface TransferResponse {
  idempotent_replay: boolean;
  transaction: SwitchTransaction;
}

export interface AccountInfo {
  id: string;
  institution_code: string;
  institution_name: string;
  institution_role: string;
  bic_code: string;
  account_number: string;
  holder_name: string;
  account_tier: string;
  currency: string;
  balance_centavos: number;
  version: number;
  daily_limit_centavos: number | null;
  daily_used_centavos: number;
  updated_at: string;
}

export interface BankCustomerAccount {
  id: string;
  bank_code: string;
  bank_name: string;
  bic_code: string;
  account_number: string;
  holder_name: string;
  holder_name_masked: string;
  account_tier: string;
  currency: string;
  ledger_balance_centavos: number;
  available_balance_centavos: number;
  earmarked_centavos: number;
  status: "ACTIVE" | "PND" | "PNC" | "FROZEN" | "CLOSED" | string;
  per_tx_limit_centavos: number;
  daily_limit_centavos: number;
  daily_used_centavos: number;
  switch_daily_used_centavos: number;
  switch_daily_limit_centavos: number | null;
  version: number;
  pii_encrypted_at_rest: boolean;
  updated_at: string;
}

export interface LedgerSummary {
  total_debits_centavos: number;
  total_credits_centavos: number;
  is_balanced: boolean;
  suspense_balance_centavos: number;
  entries: JournalEntry[];
}

export interface BankChaosConfig {
  preset: string;
  latency_ms: number;
  drop_0210: boolean;
  force_de39: string | null;
  fail_0430: boolean;
  offline: boolean;
}

export interface BankConnectivity {
  bank_code: string;
  bank_name: string;
  adapter_type: string;
  endpoint_url: string;
  status: "HEALTHY" | "DEGRADED" | "OFFLINE" | string;
  latency_ms: number;
  mtls_enabled: boolean;
  oauth2_authenticated: boolean;
  signature_algorithm: string;
  circuit_state: string;
  cert_cn: string | null;
  cert_fingerprint: string | null;
  cert_valid_until: string | null;
  chaos_preset: string;
  error: string | null;
}

export interface HealthStatus {
  status: string;
  postgres: string;
  redis: string;
}

export interface OAuthSession {
  access_token: string;
  token_type: string;
  expires_in: number;
  subject: string;
  display_name: string;
  role: "OPS_VIEWER" | "MAKER" | "CHECKER" | "FRAUD_ANALYST" | "ADMIN" | string;
  scopes: string[];
}

export interface SettlementLivePosition {
  institution_code: string;
  settlement_account_number: string;
  settlement_balance_centavos: number;
  prefunded_limit_centavos: number;
  minimum_reserve_centavos: number;
  collateral_centavos: number;
  net_debit_cap_centavos: number;
  available_liquidity_centavos: number;
  gross_debits_centavos: number;
  gross_credits_centavos: number;
  net_position_centavos: number;
  tx_count: number;
  liquidity_utilization_bps: number;
  simulate_shortfall: boolean;
  status: string;
}

export interface SettlementPositionsSummary {
  unsettled_tx_count: number;
  total_gross_volume_centavos: number;
  total_net_obligation_centavos: number;
  netting_zero_sum_valid: boolean;
  positions: SettlementLivePosition[];
}

export interface SettlementCycle {
  id: string;
  cycle_number: number;
  status: string;
  total_gross_volume_centavos: number;
  total_net_settled_centavos: number;
  tx_count: number;
  failure_reason: string | null;
  initiated_by: string;
  report_signature: string | null;
  created_at: string;
  settled_at: string | null;
  positions: Array<{
    institution_code: string;
    gross_debits_centavos: number;
    gross_credits_centavos: number;
    net_position_centavos: number;
    tx_count: number;
    opening_balance_centavos: number;
    closing_balance_centavos: number;
    available_liquidity_centavos: number;
    liquidity_utilization_bps: number;
    status: string;
  }>;
}

export interface ReconciliationException {
  id: string;
  run_id: string;
  exception_type: string;
  severity: string;
  bank_code: string;
  switch_tx_id: string | null;
  cbs_ref: string | null;
  stan: string | null;
  rrn: string | null;
  amount_centavos: number;
  description: string;
  status: string;
  resolution_notes: string | null;
  resolved_at: string | null;
  created_at: string;
}

export interface SuspenseBreakdown {
  suspense_account_number: string;
  balance_centavos: number;
  version: number;
  uncleared_transactions: Array<{
    id: string;
    stan: string;
    rrn: string;
    originator_code: string;
    beneficiary_code: string;
    amount_centavos: number;
    status: string;
    failure_reason: string | null;
    created_at: string;
  }>;
}

export interface MakerCheckerRequest {
  id: string;
  action_type: string;
  resource_type: string;
  resource_id: string;
  payload_json: Record<string, unknown>;
  reason: string;
  status: "PENDING" | "APPROVED" | "REJECTED" | string;
  maker_id: string;
  maker_role: string;
  checker_id: string | null;
  checker_role: string | null;
  decision_notes: string | null;
  created_at: string;
  decided_at: string | null;
}

export interface FraudCase {
  id: string;
  transaction_id: string;
  originator_code: string;
  originator_account_number: string;
  beneficiary_code: string;
  beneficiary_account_number: string;
  amount_centavos: number;
  risk_score: number;
  triggered_rules: Array<{ rule: string; weight: number; detail: string }>;
  status: string;
  assigned_to: string | null;
  decision_notes: string | null;
  created_at: string;
  updated_at: string;
  events: Array<{
    id: string;
    actor_id: string;
    actor_role: string;
    action: string;
    notes: string;
    created_at: string;
  }>;
}

export interface AuditLogEntry {
  id: string;
  sequence_no: number;
  actor_id: string;
  actor_role: string;
  action: string;
  resource_type: string;
  resource_id: string;
  payload_json: Record<string, unknown>;
  prev_hash: string;
  entry_hash: string;
  created_at: string;
}

export interface SecurityStatus {
  mtls_enabled: boolean;
  oauth2_issuer: string;
  signature_algorithm: string;
  encryption_at_rest: string;
  rbac_enforced: boolean;
  maker_checker_enforced: boolean;
  audit_chain: {
    valid: boolean;
    checked_entries: number;
    total_entries: number;
    head_hash?: string;
    broken_at_sequence: number | null;
    reason: string | null;
  };
  certificates: Array<{
    service_name: string;
    subject_cn: string;
    issuer_cn: string;
    serial_hex: string;
    sha256_fingerprint: string;
    not_before: string;
    not_after: string;
    is_valid_now: boolean;
    ca_chain_verified: boolean;
  }>;
}

export interface OpsTelemetry {
  sample_size: number;
  tx_last_1m: number;
  tx_last_5m: number;
  tps_1m: number;
  success_rate_pct: number;
  latency_p50_ms: number;
  latency_p95_ms: number;
  latency_p99_ms: number;
  latency_avg_ms: number;
  status_breakdown: Record<string, number>;
  corridor_breakdown: Array<{ corridor: string; count: number; volume_centavos: number }>;
  open_recon_exceptions: number;
  pending_fraud_reviews: number;
  pending_maker_checker: number;
  unacknowledged_alerts: number;
  computed_at: string;
}

export interface SystemAlert {
  id: string;
  category: string;
  severity: "INFO" | "WARNING" | "CRITICAL" | string;
  title: string;
  message: string;
  source_service: string;
  resource_id: string | null;
  acknowledged: boolean;
  acknowledged_by: string | null;
  created_at: string;
}
