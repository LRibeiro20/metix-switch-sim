"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  BookOpen,
  LayoutDashboard,
  RefreshCw,
  ShieldCheck,
  Terminal,
  TimerReset,
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
import { IsoWireInspector } from "@/components/IsoWireInspector";
import { LedgerBalancesPanel } from "@/components/LedgerBalancesPanel";
import { TransferComposer } from "@/components/TransferComposer";
import {
  fetchAccounts,
  fetchChaos,
  fetchHealth,
  fetchLedger,
  fetchTransactions,
  runSweeperNow,
} from "@/lib/api";
import { formatMzn } from "@/lib/format";
import type {
  AccountInfo,
  ChaosConfig,
  HealthStatus,
  LedgerSummary,
  SwitchTransaction,
} from "@/lib/types";

type NavSection = "workbench" | "wire" | "ledger";

export function SwitchDashboard() {
  const [activeNav, setActiveNav] = useState<NavSection>("workbench");
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [accounts, setAccounts] = useState<AccountInfo[]>([]);
  const [transactions, setTransactions] = useState<SwitchTransaction[]>([]);
  const [ledger, setLedger] = useState<LedgerSummary | null>(null);
  const [chaos, setChaos] = useState<ChaosConfig | null>(null);
  const [selectedTxId, setSelectedTxId] = useState<string | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isSweeping, setIsSweeping] = useState(false);

  const loadAll = useCallback(async (manual = false) => {
    if (manual) setIsRefreshing(true);
    try {
      const [h, accts, txs, led, ch] = await Promise.all([
        fetchHealth(),
        fetchAccounts(),
        fetchTransactions(40),
        fetchLedger(50),
        fetchChaos(),
      ]);
      setHealth(h);
      setAccounts(accts);
      setTransactions(txs);
      setLedger(led);
      setChaos(ch);
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
  }, []);

  useEffect(() => {
    void loadAll();
    const interval = setInterval(() => {
      void loadAll(false);
    }, 1500);
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
    const reversedCount = transactions.filter((t) => t.status === "REVERSED").length;
    const declinedCount = transactions.filter((t) => t.status === "DECLINED").length;
    const suspenseCentavos = ledger?.suspense_balance_centavos ?? 0;

    return {
      settledCentavos,
      completedCount: completed.length,
      routedCount,
      reversedCount,
      declinedCount,
      suspenseCentavos,
    };
  }, [transactions, ledger]);

  const navItems: Array<{ id: NavSection; label: string; icon: typeof LayoutDashboard }> = [
    { id: "workbench", label: "Switch Workbench", icon: LayoutDashboard },
    { id: "wire", label: "ISO 8583 Wire Log", icon: Terminal },
    { id: "ledger", label: "Double-Entry Ledger", icon: BookOpen },
  ];

  return (
    <div className="min-h-screen flex bg-body text-primary">
      {/* Stripe-style Fixed Left Rail */}
      <aside className="hidden lg:flex lg:flex-col sticky top-0 h-screen w-64 shrink-0 bg-surface border-r border-border">
        <div className="h-14 px-5 flex items-center gap-2.5 border-b border-border">
          <div className="w-7 h-7 rounded-md bg-accent flex items-center justify-center text-on-accent shadow-xs">
            <Activity className="w-4 h-4" />
          </div>
          <div className="min-w-0">
            <div className="text-sm font-semibold tracking-tight text-primary leading-tight">
              METIX Switch
            </div>
            <div className="text-[11px] text-secondary truncate">SIMOrede · 1/GBM/2026</div>
          </div>
        </div>

        <nav className="p-3 space-y-1 flex-1">
          <div className="px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-secondary">
            Operations
          </div>
          {navItems.map((item) => {
            const Icon = item.icon;
            const active = activeNav === item.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setActiveNav(item.id)}
                className={`w-full flex items-center gap-2.5 px-2.5 py-2 rounded-md text-sm font-medium transition-colors cursor-pointer ${
                  active
                    ? "bg-accent-muted text-accent"
                    : "text-secondary hover:bg-muted hover:text-primary"
                }`}
              >
                <Icon className="w-4 h-4 shrink-0" />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        <div className="p-4 border-t border-border space-y-2.5 text-xs">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-secondary">
            Infrastructure Telemetry
          </div>
          <div className="flex items-center justify-between">
            <span className="text-secondary">PostgreSQL 16</span>
            <span className="inline-flex items-center gap-1.5 font-medium text-primary">
              <StatusDot
                variant={health?.postgres === "connected" ? "success" : "error"}
                label="PostgreSQL status"
              />
              {health?.postgres === "connected" ? "Healthy" : "Offline"}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-secondary">Redis 7 Guard</span>
            <span className="inline-flex items-center gap-1.5 font-medium text-primary">
              <StatusDot
                variant={health?.redis === "connected" ? "success" : "error"}
                label="Redis status"
              />
              {health?.redis === "connected" ? "Healthy" : "Offline"}
            </span>
          </div>
          <Divider />
          <div className="flex items-center justify-between text-secondary">
            <span>SPIM SLA</span>
            <span className="font-mono font-semibold text-primary">25s · DE 39=68</span>
          </div>
          <div className="flex items-center justify-between text-secondary">
            <span>Currency</span>
            <span className="font-mono font-semibold text-primary">943 (MZN)</span>
          </div>
        </div>
      </aside>

      {/* Main Content Shell */}
      <div className="flex-1 min-w-0 flex flex-col">
        {/* Sticky Stripe-grade Header */}
        <header className="sticky top-0 z-20 h-14 bg-surface/95 backdrop-blur border-b border-border px-4 sm:px-6 flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <Heading level={4} weight="semibold" className="truncate">
              {activeNav === "workbench"
                ? "Instant Payment Switch Workbench"
                : activeNav === "wire"
                  ? "ISO 8583 Wire Trace Inspector"
                  : "Double-Entry Clearing Ledger"}
            </Heading>
            <span className="hidden md:inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-muted border border-border text-xs text-secondary">
              <StatusDot variant={apiError ? "error" : "success"} label="Live sync" isPulsing={!apiError} />
              {apiError ? "Disconnected" : "Live · 1.5s sync"}
            </span>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            <div className="flex lg:hidden items-center gap-1">
              {navItems.map((item) => (
                <Button
                  key={item.id}
                  label={item.label.split(" ")[0]}
                  size="sm"
                  variant={activeNav === item.id ? "primary" : "ghost"}
                  onClick={() => setActiveNav(item.id)}
                />
              ))}
            </div>
            <Button
              label="Sweep SLA"
              variant="secondary"
              size="sm"
              icon={<TimerReset className="w-3.5 h-3.5" />}
              isLoading={isSweeping}
              tooltip="Run one immediate pass of the 25s SPIM SLA sweeper"
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

        {/* Scrollable Workspace */}
        <main className="flex-1 max-w-[1520px] w-full mx-auto px-4 sm:px-6 py-6 space-y-6">
          {apiError && (
            <Banner
              status="error"
              title="Switch API unreachable"
              description={`${apiError}. Ensure the FastAPI switch service is running on http://localhost:8000.`}
            />
          )}

          {/* Top KPI Strip (4 Stripe-style metric cards) */}
          <section className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
            <Card padding={4} elevation="low">
              <Text type="supporting" display="block">
                Settled Volume (0210 · DE 39=00)
              </Text>
              <div className="mt-1.5 text-2xl font-semibold tracking-tight text-primary tabular-nums">
                {formatMzn(metrics.settledCentavos)}
              </div>
              <div className="mt-1.5 flex items-center gap-2 text-xs text-secondary">
                <Badge variant="success" label={`${metrics.completedCount} settled`} />
                <span>MZN 943 minor units</span>
              </div>
            </Card>

            <Card padding={4} elevation="low">
              <Text type="supporting" display="block">
                METIX Clearing Suspense (In-Flight)
              </Text>
              <div
                className={`mt-1.5 text-2xl font-semibold tracking-tight tabular-nums ${
                  metrics.suspenseCentavos > 0 ? "text-warning" : "text-primary"
                }`}
              >
                {formatMzn(metrics.suspenseCentavos)}
              </div>
              <div className="mt-1.5 flex items-center gap-2 text-xs text-secondary">
                <Badge
                  variant={metrics.routedCount > 0 ? "warning" : "neutral"}
                  label={`${metrics.routedCount} ROUTED`}
                />
                <span>{metrics.suspenseCentavos === 0 ? "Zero uncleared float" : "Held pending 0210/0420"}</span>
              </div>
            </Card>

            <Card padding={4} elevation="low">
              <Text type="supporting" display="block">
                Double-Entry Ledger Invariant
              </Text>
              <div className="mt-1.5 flex items-center gap-2">
                <span className="text-2xl font-semibold tracking-tight text-primary">
                  {ledger?.is_balanced ?? true ? "Balanced" : "Mismatch"}
                </span>
                <ShieldCheck className="w-5 h-5 text-success" />
              </div>
              <div className="mt-1.5 text-xs text-secondary font-mono tabular-nums">
                DR = CR = {formatMzn(ledger?.total_debits_centavos ?? 0)}
              </div>
            </Card>

            <Card padding={4} elevation="low">
              <Text type="supporting" display="block">
                SPIM 25s SLA &amp; Risk Outcomes
              </Text>
              <div className="mt-1.5 flex items-baseline gap-3 tabular-nums">
                <span className="text-2xl font-semibold text-primary">
                  {metrics.reversedCount}
                </span>
                <span className="text-xs text-secondary">0420 reversed</span>
                <span className="text-2xl font-semibold text-primary">
                  {metrics.declinedCount}
                </span>
                <span className="text-xs text-secondary">declined</span>
              </div>
              <div className="mt-1.5 flex items-center gap-2 text-xs text-secondary">
                <Badge
                  variant={chaos?.preset === "normal" ? "success" : "warning"}
                  label={`Chaos: ${chaos?.preset ?? "normal"}`}
                />
                <span>Cap 200k / 500k MT</span>
              </div>
            </Card>
          </section>

          {activeNav === "workbench" && (
            <>
              <section className="grid grid-cols-1 xl:grid-cols-12 gap-6 items-start">
                <div className="xl:col-span-5 space-y-6">
                  <TransferComposer
                    accounts={accounts}
                    onTransferSettled={(tx) => {
                      setSelectedTxId(tx.id);
                      void loadAll(false);
                    }}
                  />
                  <ChaosControlPanel
                    chaos={chaos}
                    onChaosUpdated={(next) => {
                      setChaos(next);
                    }}
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

              <LedgerBalancesPanel accounts={accounts} ledger={ledger} />
            </>
          )}

          {activeNav === "wire" && (
            <IsoWireInspector
              transactions={transactions}
              selectedTxId={selectedTxId}
              onSelectTx={setSelectedTxId}
              expanded
            />
          )}

          {activeNav === "ledger" && (
            <LedgerBalancesPanel accounts={accounts} ledger={ledger} />
          )}
        </main>
      </div>
    </div>
  );
}
