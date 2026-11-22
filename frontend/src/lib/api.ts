import type {
  AccountInfo,
  ChaosConfig,
  HealthStatus,
  LedgerSummary,
  SwitchTransaction,
  TransferResponse,
} from "./types";

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") || "http://localhost:8000";

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {};
  if (init?.body !== undefined) {
    headers["Content-Type"] = "application/json";
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

export async function fetchHealth(): Promise<HealthStatus> {
  return apiFetch<HealthStatus>("/health");
}

export async function fetchAccounts(): Promise<AccountInfo[]> {
  return apiFetch<AccountInfo[]>("/api/v1/accounts");
}

export async function fetchTransactions(limit = 40): Promise<SwitchTransaction[]> {
  return apiFetch<SwitchTransaction[]>(`/api/v1/transactions?limit=${limit}`);
}

export async function fetchLedger(limit = 40): Promise<LedgerSummary> {
  return apiFetch<LedgerSummary>(`/api/v1/ledger?limit=${limit}`);
}

export async function fetchChaos(): Promise<ChaosConfig> {
  return apiFetch<ChaosConfig>("/sim/beneficiary/chaos");
}

export async function updateChaos(
  payload: Partial<ChaosConfig> & { preset?: string }
): Promise<ChaosConfig> {
  return apiFetch<ChaosConfig>("/sim/beneficiary/chaos", {
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
