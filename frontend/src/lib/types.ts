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
  transaction_id: string;
  entry_type: "CLEARING_HOLD" | "SETTLEMENT" | "REVERSAL" | string;
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
  amount_centavos: number;
  currency: string;
  status: TransactionStatus;
  response_code: string | null;
  response_description: string | null;
  auth_id: string | null;
  failure_reason: string | null;
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
  institution_role: "ORIGINATOR" | "BENEFICIARY" | "SWITCH" | string;
  bic_code: string;
  account_number: string;
  holder_name: string;
  account_tier: "INDIVIDUAL" | "BUSINESS" | "SUSPENSE" | string;
  currency: string;
  balance_centavos: number;
  version: number;
  daily_limit_centavos: number | null;
  daily_used_centavos: number;
  updated_at: string;
}

export interface LedgerSummary {
  total_debits_centavos: number;
  total_credits_centavos: number;
  is_balanced: boolean;
  suspense_balance_centavos: number;
  entries: JournalEntry[];
}

export interface ChaosConfig {
  preset: string;
  latency_ms: number;
  drop_0210: boolean;
  force_de39: string | null;
  fail_0430: boolean;
  target_beneficiary: string | null;
}

export interface HealthStatus {
  status: string;
  postgres: string;
  redis: string;
}
