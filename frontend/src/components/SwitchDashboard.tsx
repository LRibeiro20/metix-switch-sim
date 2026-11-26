"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  Building2,
  GitCompare,
  Landmark,
  LayoutDashboard,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Terminal,
  TimerReset,
  UserCircle2,
} from "lucide-react";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Divider } from "@astryxdesign/core/Divider";
import { Heading } from "@astryxdesign/core/Heading";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Text } from "@astryxdesign/core/Text";

import { ChaosControlPanel } from "@/components/ChaosControlPanel";
import { CoreBankingPanel } from "@/components/CoreBankingPanel";
import { FraudQueuePanel } from "@/components/FraudQueuePanel";
import { IsoWireInspector } from "@/components/IsoWireInspector";
import { ReconciliationPanel } from "@/components/ReconciliationPanel";
import { SecurityTelemetryPanel } from "@/components/SecurityTelemetryPanel";
import { SettlementPanel } from "@/components/SettlementPanel";
import { TransferComposer } from "@/components/TransferComposer";
import {
  OPERATOR_PERSONAS,
  fetchAuditLogs,
  fetchBankConnectivity,
  fetchBankCustomerAccounts,
  fetchFraudCases,
  fetchHealth,
  fetchLedger,
  fetchMakerCheckerRequests,
  fetchOpsAlerts,
  fetchOpsTelemetry,
  fetchReconciliationExceptions,
  fetchSecurityStatus,
  fetchSettlementAccounts,
  fetchSettlementCycles,
  fetchSettlementPositions,
  fetchSuspenseBreakdown,
  fetchTransactions,
  loginPersona,
  runSweeperNow,
} from "@/lib/api";
import { formatMzn } from "@/lib/format";
import type {
  AccountInfo,
  AuditLogEntry,
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
} from "@/lib/types";

type NavSection =
  | "workbench"
  | "core-banking"
  | "settlement"
  | "reconciliation"
  | "fraud"
  | "security"
  | "wire";

export function SwitchDashboard() {
  const [activeNav, setActiveNav] = useState<NavSection>("workbench");
  const [session, setSession] = useState<OAuthSession | null>(null);
  const [selectedPersona, setSelectedPersona] = useState<string>(OPERATOR_PERSONAS[0].username);

  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [connectivity, setConnectivity] = useState<BankConnectivity[]>([]);
  const [switchAccounts, setSwitchAccounts] = useState<AccountInfo[]>([]);
  const [bankAccounts, setBankAccounts] = useState<BankCustomerAccount[]>([]);
  const [transactions, setTransactions] = useState<SwitchTransaction[]>([]);
  const [ledger, setLedger] = useState<LedgerSummary | null>(null);
  const [settlementPositions, setSettlementPositions] =
    useState<SettlementPositionsSummary | null>(null);
  const [settlementCycles, setSettlementCycles] = useState<SettlementCycle[]>([]);
  const [reconExceptions, setReconExceptions] = useState<ReconciliationException[]>([]);
  const [suspenseBreakdown, setSuspenseBreakdown] = useState<SuspenseBreakdown | null>(null);
  const [makerCheckerReqs, setMakerCheckerReqs] = useState<MakerCheckerRequest[]>([]);
  const [fraudCases, setFraudCases] = useState<FraudCase[]>([]);
  const [securityStatus, setSecurityStatus] = useState<SecurityStatus | null>(null);
  const [auditLogs, setAuditLogs] = useState<AuditLogEntry[]>([]);
  const [telemetry, setTelemetry] = useState<OpsTelemetry | null>(null);
  const [alerts, setAlerts] = useState<SystemAlert[]>([]);

  const [selectedTxId, setSelectedTxId] = useState<string | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isSweeping, setIsSweeping] = useState(false);

  const switchOperatorPersona = useCallback(async (username: string) => {
    const persona = OPERATOR_PERSONAS.find((p) => p.username === username) ?? OPERATOR_PERSONAS[0];
    try {
      const oauth = await loginPersona(persona.username, persona.password);
      setSession(oauth);
      setSelectedPersona(persona.username);
      setApiError(null);
    } catch (err) {
      setApiError(err instanceof Error ? err.message : "OAuth 2.0 login failed");
    }
  }, []);

  const loadAll = useCallback(
    async (manual = false) => {
      if (manual) setIsRefreshing(true);
      try {
        const [
          h,
          conn,
          swAccts,
          cbsAccts,
          txs,
          led,
          settlePos,
          cycles,
          exc,
          susp,
          fraud,
          tel,
          alrt,
        ] = await Promise.all([
          fetchHealth(),
          fetchBankConnectivity(),
          fetchSettlementAccounts(),
          fetchBankCustomerAccounts(),
          fetchTransactions(40),
          fetchLedger(50),
          fetchSettlementPositions(),
          fetchSettlementCycles(),
          fetchReconciliationExceptions(),
          fetchSuspenseBreakdown(),
          fetchFraudCases(),
          fetchOpsTelemetry(),
          fetchOpsAlerts(),
        ]);

        setHealth(h);
        setConnectivity(conn);
        setSwitchAccounts(swAccts);
        setBankAccounts(cbsAccts);
        setTransactions(txs);
        setLedger(led);
        setSettlementPositions(settlePos);
        setSettlementCycles(cycles);
        setReconExceptions(exc);
        setSuspenseBreakdown(susp);
        setFraudCases(fraud);
        setTelemetry(tel);
        setAlerts(alrt);

        if (session) {
          const [mc, sec, logs] = await Promise.all([
            fetchMakerCheckerRequests(),
            fetchSecurityStatus(),
            fetchAuditLogs(40),
          ]);
          setMakerCheckerReqs(mc);
          setSecurityStatus(sec);
          setAuditLogs(logs);
        }
        setApiError(null);
      } catch (err) {
        setApiError(
          err instanceof Error
            ? err.message
            : "Unable to connect to METIX Switch API on localhost:8000"
        );
      } finally {
        if (manual) setIsRefreshing(false);
      }
    },
    [session]
  );

  useEffect(() => {
    void switchOperatorPersona(OPERATOR_PERSONAS[0].username);
  }, [switchOperatorPersona]);

  useEffect(() => {
    void loadAll();
    const interval = setInterval(() => {
      void loadAll(false);
    }, 2500);
    return () => clearInterval(interval);
  }, [loadAll]);

  async function handleRunSweeper() {
    setIsSweeping(true);
    try {
      await runSweeperNow();
      await loadAll(false);
    } finally {
      setIsSweeping(false);
    }
  }

  const metrics = useMemo(() => {
    const completed = transactions.filter((t) => t.status === "COMPLETED");
    const settledCentavos = completed.reduce((sum, t) => sum + t.amount_centavos, 0);
    const routedCount = transactions.filter((t) => t.status === "ROUTED").length;
    const failedCount = transactions.filter(
      (t) => t.status === "DECLINED" || t.status === "RECON_HOLD" || t.status === "REVERSED"
    ).length;
    const suspenseCentavos = ledger?.suspense_balance_centavos ?? 0;

    return {
      settledCentavos,
      completedCount: completed.length,
      routedCount,
      failedCount,
      suspenseCentavos,
    };
  }, [transactions, ledger]);

  const currentRole = session?.role ?? "OPS_VIEWER";
  const currentUser = session?.subject ?? "ops.viewer";
  const canInitiateTransfers = currentRole === "MAKER" || currentRole === "ADMIN";

  const navItems: Array<{
    id: NavSection;
    label: string;
    icon: typeof LayoutDashboard;
    badgeCount?: number;
  }> = [
    { id: "workbench", label: "Operations Workbench", icon: LayoutDashboard },
    { id: "core-banking", label: "Core Banking & Ledger", icon: Building2 },
    { id: "settlement", label: "Clearing & Settlement", icon: Landmark },
    {
      id: "reconciliation",
      label: "Reconciliation",
      icon: GitCompare,
      badgeCount: (telemetry?.open_recon_exceptions ?? 0) + (telemetry?.pending_maker_checker ?? 0),
    },
    {
      id: "fraud",
      label: "Fraud & Risk Queue",
      icon: ShieldAlert,
      badgeCount: telemetry?.pending_fraud_reviews ?? 0,
    },
    {
      id: "security",
      label: "Security & Telemetry",
      icon: ShieldCheck,
      badgeCount: telemetry?.unacknowledged_alerts ?? 0,
    },
    { id: "wire", label: "ISO 8583 Wire Log", icon: Terminal },
  ];

  const pageTitle: Record<NavSection, string> = {
    workbench: "Banking Operations Workbench",
    "core-banking": "Core Banking Simulators (BCI & Standard Bank) & Switch Ledger",
    settlement: "Interbank Multilateral Net Settlement & Liquidity",
    reconciliation: "3-Way Financial Reconciliation & Maker-Checker Queue",
    fraud: "Fraud Detection, Velocity Monitoring & Review Queue",
    security: "Enterprise Security (mTLS, OAuth 2.0, Audit Chain) & Alerts",
    wire: "ISO 8583 Wire Trace & Failure Investigation",
  };

  return (
    <div className="min-h-screen flex bg-body text-primary">
      {/* Fixed Left Operations Rail */}
      <aside className="hidden lg:flex lg:flex-col sticky top-0 h-screen w-64 shrink-0 bg-surface border-r border-border">
        <div className="h-14 px-5 flex items-center gap-2.5 border-b border-border">
          <div className="w-7 h-7 rounded-md bg-accent flex items-center justify-center text-on-accent shadow-xs">
            <Activity className="w-4 h-4" />
          </div>
          <div className="min-w-0">
            <div className="text-sm font-semibold tracking-tight text-primary leading-tight">
              METIX Operations
            </div>
            <div className="text-[11px] text-secondary truncate">SIMOrede · 1/GBM/2026</div>
          </div>
        </div>

        <nav className="p-3 space-y-1 flex-1 overflow-y-auto">
          <div className="px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-secondary">
            Banking Ops
          </div>
          {navItems.map((item) => {
            const Icon = item.icon;
            const active = activeNav === item.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setActiveNav(item.id)}
                className={`w-full flex items-center justify-between gap-2 px-2.5 py-2 rounded-md text-sm font-medium transition-colors cursor-pointer ${
                  active
                    ? "bg-accent-muted text-accent"
                    : "text-secondary hover:bg-muted hover:text-primary"
                }`}
              >
                <span className="flex items-center gap-2.5 min-w-0">
                  <Icon className="w-4 h-4 shrink-0" />
                  <span className="truncate">{item.label}</span>
                </span>
                {item.badgeCount !== undefined && item.badgeCount > 0 && (
                  <span className="px-1.5 py-0.5 text-[10px] font-mono font-semibold rounded-full bg-error-muted text-error">
                    {item.badgeCount}
                  </span>
                )}
              </button>
            );
          })}
        </nav>

        <div className="p-4 border-t border-border space-y-2 text-xs">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-secondary">
            Core Infrastructure
          </div>
          <div className="flex items-center justify-between">
            <span className="text-secondary">Switch DB (PG16)</span>
            <span className="inline-flex items-center gap-1.5 font-medium text-primary">
              <StatusDot
                variant={health?.postgres === "connected" ? "success" : "error"}
                label="PostgreSQL"
              />
              {health?.postgres === "connected" ? "Healthy" : "Offline"}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-secondary">Redis 7 Guard</span>
            <span className="inline-flex items-center gap-1.5 font-medium text-primary">
              <StatusDot
                variant={health?.redis === "connected" ? "success" : "error"}
                label="Redis"
              />
              {health?.redis === "connected" ? "Healthy" : "Offline"}
            </span>
          </div>
          {connectivity.slice(0, 2).map((c) => (
            <div key={c.bank_code} className="flex items-center justify-between">
              <span className="text-secondary">{c.bank_code} CBS</span>
              <span className="inline-flex items-center gap-1.5 font-medium text-primary">
                <StatusDot
                  variant={
                    c.status === "HEALTHY"
                      ? "success"
                      : c.status === "DEGRADED"
                        ? "warning"
                        : "error"
                  }
                  label={c.bank_code}
                />
                {c.latency_ms}ms
              </span>
            </div>
          ))}
          <Divider />
          <div className="flex items-center justify-between text-secondary">
            <span>Audit Chain</span>
            <span className="font-mono font-semibold text-success">
              {securityStatus?.audit_chain.valid ?? true ? "SHA-256 OK" : "BROKEN"}
            </span>
          </div>
        </div>
      </aside>

      {/* Main Operations Shell */}
      <div className="flex-1 min-w-0 flex flex-col">
        <header className="sticky top-0 z-20 min-h-14 py-2 bg-surface/95 backdrop-blur border-b border-border px-4 sm:px-6 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <Heading level={4} weight="semibold" className="truncate">
              {pageTitle[activeNav]}
            </Heading>
          </div>

          <div className="flex flex-wrap items-center gap-2 shrink-0">
            {/* Real OAuth 2.0 Operator RBAC Persona Switcher */}
            <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-muted border border-border text-xs">
              <UserCircle2 className="w-4 h-4 text-accent shrink-0" />
              <select
                aria-label="OAuth 2.0 Operator Persona"
                value={selectedPersona}
                onChange={(e) => void switchOperatorPersona(e.target.value)}
                className="bg-transparent font-medium text-primary focus:outline-none cursor-pointer"
              >
                {OPERATOR_PERSONAS.map((p) => (
                  <option key={p.username} value={p.username}>
                    {p.label}
                  </option>
                ))}
              </select>
            </div>

            <Button
              label="Sweep SLA"
              variant="secondary"
              size="sm"
              icon={<TimerReset className="w-3.5 h-3.5" />}
              isLoading={isSweeping}
              onClick={handleRunSweeper}
            />
            <Button
              label="Refresh"
              variant="ghost"
              size="sm"
              icon={<RefreshCw className="w-3.5 h-3.5" />}
              isLoading={isRefreshing}
              onClick={() => void loadAll(true)}
            />
          </div>
        </header>

        <main className="flex-1 max-w-[1520px] w-full mx-auto px-4 sm:px-6 py-6 space-y-6">
          {apiError && (
            <Banner
              status="error"
              title="Switch API Error"
              description={apiError}
            />
          )}

          {/* Bank Connectivity & Security Telemetry Strip */}
          <section className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
            {connectivity.map((bank) => (
              <div
                key={bank.bank_code}
                className="px-3.5 py-2.5 rounded-lg bg-surface border border-border flex items-center justify-between gap-2 text-xs shadow-xs"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5 font-semibold text-primary">
                    <StatusDot
                      variant={
                        bank.status === "HEALTHY"
                          ? "success"
                          : bank.status === "DEGRADED"
                            ? "warning"
                            : "error"
                      }
                      label={bank.bank_code}
                    />
                    <span className="truncate">{bank.bank_code}</span>
                    <span className="font-mono text-[10px] text-secondary">
                      {bank.latency_ms}ms
                    </span>
                  </div>
                  <div className="text-[11px] text-secondary font-mono truncate mt-0.5">
                    {bank.mtls_enabled ? "mTLS" : "TLS-Ready"} · OAuth2 · {bank.signature_algorithm}
                  </div>
                </div>
                <Badge
                  variant={
                    bank.status === "HEALTHY"
                      ? "success"
                      : bank.status === "DEGRADED"
                        ? "warning"
                        : "error"
                  }
                  label={bank.chaos_preset !== "normal" ? bank.chaos_preset : bank.status}
                />
              </div>
            ))}
          </section>

          {/* Top Financial Operations KPI Strip */}
          <section className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
            <Card padding={4} elevation="low">
              <Text type="supporting" display="block">
                Settled Interbank Volume (0210 · 00)
              </Text>
              <div className="mt-1.5 text-2xl font-semibold tracking-tight text-primary tabular-nums">
                {formatMzn(metrics.settledCentavos)}
              </div>
              <div className="mt-1.5 flex items-center gap-2 text-xs text-secondary">
                <Badge variant="success" label={`${metrics.completedCount} settled`} />
                <span>{telemetry?.success_rate_pct ?? 100}% completion</span>
              </div>
            </Card>

            <Card padding={4} elevation="low">
              <Text type="supporting" display="block">
                Latency &amp; Throughput (SPIM 25s SLA)
              </Text>
              <div className="mt-1.5 flex items-baseline gap-2 font-mono tabular-nums">
                <span className="text-2xl font-semibold text-primary">
                  {telemetry?.latency_p50_ms ?? 0}ms
                </span>
                <span className="text-xs text-secondary">
                  p50 · p95 {telemetry?.latency_p95_ms ?? 0}ms
                </span>
              </div>
              <div className="mt-1.5 flex items-center gap-2 text-xs text-secondary">
                <Badge variant="info" label={`${telemetry?.tps_1m ?? 0} TPS`} />
                <span>{metrics.routedCount} in-flight ROUTED</span>
              </div>
            </Card>

            <Card padding={4} elevation="low">
              <Text type="supporting" display="block">
                Clearing Suspense &amp; Ledger Invariant
              </Text>
              <div className="mt-1.5 flex items-center gap-2">
                <span className="text-2xl font-semibold tracking-tight text-primary tabular-nums">
                  {formatMzn(metrics.suspenseCentavos)}
                </span>
                <ShieldCheck className="w-5 h-5 text-success" />
              </div>
              <div className="mt-1.5 text-xs text-secondary font-mono tabular-nums">
                {ledger?.is_balanced ?? true ? "DR = CR" : "IMBALANCE"} ·{" "}
                {formatMzn(ledger?.total_debits_centavos ?? 0)}
              </div>
            </Card>

            <Card padding={4} elevation="low">
              <Text type="supporting" display="block">
                Exceptions, Fraud Queue &amp; Alerts
              </Text>
              <div className="mt-1.5 flex items-baseline gap-3 tabular-nums">
                <span className="text-2xl font-semibold text-primary">
                  {telemetry?.open_recon_exceptions ?? 0}
                </span>
                <span className="text-xs text-secondary">recon</span>
                <span className="text-2xl font-semibold text-primary">
                  {telemetry?.pending_fraud_reviews ?? 0}
                </span>
                <span className="text-xs text-secondary">fraud</span>
                <span className="text-2xl font-semibold text-primary">
                  {telemetry?.unacknowledged_alerts ?? 0}
                </span>
                <span className="text-xs text-secondary">alerts</span>
              </div>
              <div className="mt-1.5 flex items-center gap-2 text-xs text-secondary">
                <Badge
                  variant={(telemetry?.pending_maker_checker ?? 0) > 0 ? "warning" : "neutral"}
                  label={`${telemetry?.pending_maker_checker ?? 0} 4-eyes pending`}
                />
                <span>{metrics.failedCount} failed/rev</span>
              </div>
            </Card>
          </section>

          {activeNav === "workbench" && (
            <section className="grid grid-cols-1 xl:grid-cols-12 gap-6 items-start">
              <div className="xl:col-span-5 space-y-6">
                <TransferComposer
                  bankAccounts={bankAccounts}
                  canInitiate={canInitiateTransfers}
                  onTransferSettled={(tx) => {
                    if (tx) {
                      setSelectedTxId(tx.id);
                    }
                    void loadAll(false);
                  }}
                />
                <ChaosControlPanel
                  canMutate={canInitiateTransfers}
                  onChaosUpdated={() => void loadAll(false)}
                />
              </div>
              <div className="xl:col-span-7">
                <IsoWireInspector
                  transactions={transactions}
                  selectedTxId={selectedTxId}
                  onSelectTx={setSelectedTxId}
                />
              </div>
            </section>
          )}

          {activeNav === "core-banking" && (
            <CoreBankingPanel
              bankAccounts={bankAccounts}
              switchAccounts={switchAccounts}
              ledger={ledger}
              currentRole={currentRole}
              onRefresh={() => void loadAll(false)}
            />
          )}

          {activeNav === "settlement" && (
            <SettlementPanel
              positions={settlementPositions}
              cycles={settlementCycles}
              currentRole={currentRole}
              onRefresh={() => void loadAll(false)}
            />
          )}

          {activeNav === "reconciliation" && (
            <ReconciliationPanel
              exceptions={reconExceptions}
              suspense={suspenseBreakdown}
              makerCheckerRequests={makerCheckerReqs}
              currentUser={currentUser}
              currentRole={currentRole}
              onRefresh={() => void loadAll(false)}
            />
          )}

          {activeNav === "fraud" && (
            <FraudQueuePanel
              cases={fraudCases}
              currentRole={currentRole}
              onRefresh={() => void loadAll(false)}
            />
          )}

          {activeNav === "security" && (
            <SecurityTelemetryPanel
              security={securityStatus}
              auditLogs={auditLogs}
              telemetry={telemetry}
              alerts={alerts}
              currentRole={currentRole}
              onRefresh={() => void loadAll(false)}
            />
          )}

          {activeNav === "wire" && (
            <IsoWireInspector
              transactions={transactions}
              selectedTxId={selectedTxId}
              onSelectTx={setSelectedTxId}
              expanded
            />
          )}
        </main>
      </div>
    </div>
  );
}
