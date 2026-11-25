import type {
  AccountInfo,
  AuditLogEntry,
  BankChaosConfig,
  BankConnectivity,
  BankCustomerAccount,
  FraudCase,
  HealthStatus,
  LedgerSummary,
  MakerCheckerRequest,
  OAuthSession,
  OpsTelemetry,
  ReconciliationException,
  SecurityStatus,
  SettlementCycle,
  SettlementPositionsSummary,
  SuspenseBreakdown,
  SwitchTransaction,
  SystemAlert,
  TransferResponse,
} from "./types";

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") || "http://localhost:8000";

let activeAccessToken: string | null = null;

export function setAuthToken(token: string | null) {
  activeAccessToken = token;
}

export const OPERATOR_PERSONAS = [
  { username: "sec.admin", password: "admin-mz-2026", label: "Switch Admin (ADMIN)" },
  { username: "ops.maker", password: "maker-mz-2026", label: "Clearing Maker (MAKER)" },
  { username: "ops.checker", password: "checker-mz-2026", label: "Clearing Checker (CHECKER)" },
  { username: "fraud.analyst", password: "fraud-mz-2026", label: "Fraud Analyst (FRAUD_ANALYST)" },
  { username: "ops.viewer", password: "viewer-mz-2026", label: "Ops Viewer (OPS_VIEWER)" },
] as const;

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {};
  if (init?.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (activeAccessToken) {
    headers.Authorization = `Bearer ${activeAccessToken}`;
  }
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...headers,
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const errBody = (await res.json()) as { detail?: string };
      if (errBody.detail) detail = errBody.detail;
    } catch {
      // ignore non-json error body
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export async function loginPersona(username: string, password: string): Promise<OAuthSession> {
  const session = await apiFetch<OAuthSession>("/oauth/token", {
    method: "POST",
    body: JSON.stringify({ grant_type: "password", username, password }),
  });
  setAuthToken(session.access_token);
  return session;
}

export async function fetchHealth(): Promise<HealthStatus> {
  return apiFetch<HealthStatus>("/health");
}

export async function fetchSettlementAccounts(): Promise<AccountInfo[]> {
  return apiFetch<AccountInfo[]>("/api/v1/accounts");
}

export async function fetchBankCustomerAccounts(): Promise<BankCustomerAccount[]> {
  return apiFetch<BankCustomerAccount[]>("/api/v1/banks/accounts");
}

export async function fetchBankConnectivity(): Promise<BankConnectivity[]> {
  return apiFetch<BankConnectivity[]>("/api/v1/banks/connectivity");
}

export async function fetchTransactions(limit = 40): Promise<SwitchTransaction[]> {
  return apiFetch<SwitchTransaction[]>(`/api/v1/transactions?limit=${limit}`);
}

export async function fetchLedger(limit = 50): Promise<LedgerSummary> {
  return apiFetch<LedgerSummary>(`/api/v1/ledger?limit=${limit}`);
}

export async function fetchBankChaos(bankCode: string): Promise<BankChaosConfig> {
  return apiFetch<BankChaosConfig>(`/api/v1/banks/${bankCode}/chaos`);
}

export async function updateBankChaos(
  bankCode: string,
  payload: Partial<BankChaosConfig> & { preset?: string }
): Promise<BankChaosConfig> {
  return apiFetch<BankChaosConfig>(`/api/v1/banks/${bankCode}/chaos`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function submitTransfer(payload: {
  idempotency_key: string;
  originator_account_number: string;
  beneficiary_account_number: string;
  amount_centavos: number;
}): Promise<TransferResponse> {
  return apiFetch<TransferResponse>("/api/v1/transfers", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function runSweeperNow(): Promise<{ processed_transaction_ids: string[] }> {
  return apiFetch<{ processed_transaction_ids: string[] }>("/api/v1/sweeper/run", {
    method: "POST",
  });
}

export async function proposeAccountRestriction(
  bankCode: string,
  accountNumber: string,
  payload: {
    status?: string;
    per_tx_limit_centavos?: number;
    daily_limit_centavos?: number;
    reason: string;
    immediate?: boolean;
  }
) {
  return apiFetch(`/api/v1/banks/${bankCode}/accounts/${accountNumber}/restrictions`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// Settlement
export async function fetchSettlementPositions(): Promise<SettlementPositionsSummary> {
  return apiFetch<SettlementPositionsSummary>("/api/v1/settlement/positions");
}

export async function fetchSettlementCycles(): Promise<SettlementCycle[]> {
  return apiFetch<SettlementCycle[]>("/api/v1/settlement/cycles");
}

export async function runSettlementCycle(simulateFailureInstitution?: string | null) {
  return apiFetch("/api/v1/settlement/cycles/run", {
    method: "POST",
    body: JSON.stringify({
      simulate_failure_institution: simulateFailureInstitution || null,
    }),
  });
}

export async function toggleShortfallSimulation(institution_code: string, enabled: boolean) {
  return apiFetch("/api/v1/settlement/simulate-shortfall", {
    method: "POST",
    body: JSON.stringify({ institution_code, enabled }),
  });
}

export async function topupLiquidity(payload: {
  institution_code: string;
  amount_centavos: number;
  reason: string;
  immediate?: boolean;
}) {
  return apiFetch("/api/v1/settlement/liquidity/topup", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function fetchSettlementReport(cycleId: string) {
  return apiFetch<{
    cycle_id: string;
    cycle_number: number;
    report: Record<string, unknown>;
    signature_algorithm: string;
    signing_key_id: string;
    signature: string;
    signature_valid: boolean;
  }>(`/api/v1/settlement/cycles/${cycleId}/report`);
}

// Reconciliation
export async function runReconciliation() {
  return apiFetch("/api/v1/reconciliation/run", { method: "POST" });
}

export async function fetchReconciliationExceptions(): Promise<ReconciliationException[]> {
  return apiFetch<ReconciliationException[]>("/api/v1/reconciliation/exceptions");
}

export async function fetchSuspenseBreakdown(): Promise<SuspenseBreakdown> {
  return apiFetch<SuspenseBreakdown>("/api/v1/reconciliation/suspense");
}

export async function proposeReconAdjustment(
  exceptionId: string,
  adjustment_mode: string,
  reason: string
) {
  return apiFetch(`/api/v1/reconciliation/exceptions/${exceptionId}/adjust`, {
    method: "POST",
    body: JSON.stringify({ adjustment_mode, reason }),
  });
}

export async function injectReconFault(payload: {
  bank_code: "BCI_MZ" | "STDB_MZ";
  fault_type: "ORPHAN_CBS_POSTING" | "DUPLICATE_CBS_POSTING" | "STALE_ACTIVE_HOLD";
  account_number: string;
  amount_centavos: number;
}) {
  return apiFetch("/api/v1/reconciliation/inject-fault", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// Fraud
export async function fetchFraudCases(): Promise<FraudCase[]> {
  return apiFetch<FraudCase[]>("/api/v1/fraud/cases");
}

export async function decideFraudCase(
  caseId: string,
  decision: "RELEASE" | "CONFIRM_FRAUD",
  notes: string
) {
  return apiFetch(`/api/v1/fraud/cases/${caseId}/decision`, {
    method: "POST",
    body: JSON.stringify({ decision, notes }),
  });
}

// Security, Maker-Checker & Audit
export async function fetchSecurityStatus(): Promise<SecurityStatus> {
  return apiFetch<SecurityStatus>("/api/v1/security/status");
}

export async function fetchAuditLogs(limit = 50): Promise<AuditLogEntry[]> {
  return apiFetch<AuditLogEntry[]>(`/api/v1/security/audit-logs?limit=${limit}`);
}

export async function verifyAuditChain() {
  return apiFetch<{
    valid: boolean;
    checked_entries: number;
    total_entries: number;
    head_hash?: string;
    broken_at_sequence: number | null;
    reason: string | null;
  }>("/api/v1/security/audit-logs/verify", { method: "POST" });
}

export async function fetchMakerCheckerRequests(): Promise<MakerCheckerRequest[]> {
  return apiFetch<MakerCheckerRequest[]>("/api/v1/security/maker-checker");
}

export async function decideMakerChecker(requestId: string, approve: boolean, notes: string) {
  return apiFetch(`/api/v1/security/maker-checker/${requestId}/decide`, {
    method: "POST",
    body: JSON.stringify({ approve, notes }),
  });
}

// Ops Telemetry & Alerts
export async function fetchOpsTelemetry(): Promise<OpsTelemetry> {
  return apiFetch<OpsTelemetry>("/api/v1/ops/telemetry");
}

export async function fetchOpsAlerts(): Promise<SystemAlert[]> {
  return apiFetch<SystemAlert[]>("/api/v1/ops/alerts");
}

export async function acknowledgeAlert(alertId: string) {
  return apiFetch(`/api/v1/ops/alerts/${alertId}/ack`, { method: "POST" });
}
