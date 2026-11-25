"use client";

import { useState } from "react";
import {
  AlertOctagon,
  CheckCircle2,
  FileCheck2,
  Landmark,
  Play,
  PlusCircle,
  Scale,
} from "lucide-react";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { Text } from "@astryxdesign/core/Text";

import {
  fetchSettlementReport,
  runSettlementCycle,
  toggleShortfallSimulation,
  topupLiquidity,
} from "@/lib/api";
import { formatMzn, formatTimeShort } from "@/lib/format";
import type { SettlementCycle, SettlementPositionsSummary } from "@/lib/types";

interface SettlementPanelProps {
  positions: SettlementPositionsSummary | null;
  cycles: SettlementCycle[];
  currentRole: string;
  onRefresh: () => void;
}

export function SettlementPanel({
  positions,
  cycles,
  currentRole,
  onRefresh,
}: SettlementPanelProps) {
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ status: "success" | "error" | "warning"; text: string } | null>(
    null
  );
  const [activeReport, setActiveReport] = useState<{
    cycle_number: number;
    signature: string;
    signature_valid: boolean;
    report: Record<string, unknown>;
  } | null>(null);

  const canOperate = currentRole === "MAKER" || currentRole === "CHECKER" || currentRole === "ADMIN";
  const canDirectTopup = currentRole === "CHECKER" || currentRole === "ADMIN";

  async function handleRunCycle(simFailBank?: string) {
    setBusy(true);
    setBanner(null);
    try {
      const res = (await runSettlementCycle(simFailBank)) as {
        cycle_number: number;
        status: string;
        failure_reason?: string | null;
      };
      if (res.status === "FAILED") {
        setBanner({
          status: "error",
          text: `MNS Cycle #${res.cycle_number} FAILED: ${res.failure_reason ?? "Liquidity shortfall"}`,
        });
      } else {
        setBanner({
          status: "success",
          text: `MNS Cycle #${res.cycle_number} SETTLED with Ed25519 cryptographic report signature.`,
        });
      }
      onRefresh();
    } catch (e) {
      setBanner({ status: "error", text: e instanceof Error ? e.message : "Settlement error" });
    } finally {
      setBusy(false);
    }
  }

  async function handleToggleShortfall(code: string, enabled: boolean) {
    setBusy(true);
    setBanner(null);
    try {
      await toggleShortfallSimulation(code, enabled);
      setBanner({
        status: enabled ? "warning" : "success",
        text: `${code} liquidity shortfall simulation ${enabled ? "ENABLED" : "CLEARED"}.`,
      });
      onRefresh();
    } catch (e) {
      setBanner({ status: "error", text: e instanceof Error ? e.message : "Action failed" });
    } finally {
      setBusy(false);
    }
  }

  async function handleTopup(code: string) {
    setBusy(true);
    setBanner(null);
    try {
      const res = (await topupLiquidity({
        institution_code: code,
        amount_centavos: 100_000_000, // +1,000,000.00 MT
        reason: "Intraday BM RTGS prefunding liquidity top-up",
        immediate: canDirectTopup,
      })) as { mode: string };
      setBanner({
        status: "success",
        text:
          res.mode === "DIRECT"
            ? `Credited +1,000,000.00 MT RTGS liquidity to ${code} settlement account.`
            : `Proposed +1,000,000.00 MT liquidity top-up for ${code} (awaiting Checker approval).`,
      });
      onRefresh();
    } catch (e) {
      setBanner({ status: "error", text: e instanceof Error ? e.message : "Top-up failed" });
    } finally {
      setBusy(false);
    }
  }

  async function handleViewReport(cycleId: string) {
    try {
      const rep = await fetchSettlementReport(cycleId);
      setActiveReport(rep);
    } catch (e) {
      setBanner({ status: "error", text: e instanceof Error ? e.message : "Unable to load report" });
    }
  }

  return (
    <div className="space-y-6">
      {banner && (
        <Banner
          status={banner.status}
          title="Interbank Clearing & Settlement"
          description={banner.text}
          isDismissable
          onDismiss={() => setBanner(null)}
        />
      )}

      {/* Live Multilateral Net Positions */}
      <Card padding={5} elevation="low" className="w-full">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
          <div>
            <div className="flex items-center gap-2">
              <Landmark className="w-4 h-4 text-accent" />
              <Heading level={4} weight="semibold">
                Multilateral Net Settlement (MNS) &amp; Intraday Liquidity
              </Heading>
            </div>
            <Text type="supporting" display="block">
              Real-time net debit/credit positions across Banco de Moçambique SPIM settlement
              accounts
            </Text>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              variant={positions?.netting_zero_sum_valid ?? true ? "success" : "error"}
              icon={<Scale className="w-3 h-3" />}
              label={
                positions?.netting_zero_sum_valid ?? true
                  ? "Σ Net Positions = 0.00 MT"
                  : "Netting Imbalance"
              }
            />
            <Button
              label="Execute MNS Cycle"
              variant="primary"
              size="sm"
              icon={<Play className="w-3.5 h-3.5" />}
              isLoading={busy}
              isDisabled={!canOperate}
              onClick={() => handleRunCycle()}
            />
            <Button
              label="Simulate BCI Failure"
              variant="secondary"
              size="sm"
              icon={<AlertOctagon className="w-3.5 h-3.5" />}
              isDisabled={!canOperate || busy}
              onClick={() => handleRunCycle("BCI_MZ")}
            />
          </div>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-4 text-xs">
          <div className="px-3.5 py-2.5 rounded-md bg-muted border border-border">
            <div className="text-secondary">Unsettled Completed Transfers</div>
            <div className="text-lg font-semibold text-primary tabular-nums mt-0.5">
              {positions?.unsettled_tx_count ?? 0}
            </div>
          </div>
          <div className="px-3.5 py-2.5 rounded-md bg-muted border border-border">
            <div className="text-secondary">Gross Clearing Volume</div>
            <div className="text-lg font-semibold text-primary tabular-nums mt-0.5">
              {formatMzn(positions?.total_gross_volume_centavos ?? 0)}
            </div>
          </div>
          <div className="px-3.5 py-2.5 rounded-md bg-muted border border-border">
            <div className="text-secondary">Net Settlement Obligation (Liquidity Saved)</div>
            <div className="text-lg font-semibold text-accent tabular-nums mt-0.5">
              {formatMzn(positions?.total_net_obligation_centavos ?? 0)}
            </div>
          </div>
        </div>

        <div className="overflow-x-auto -mx-5 px-5">
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr className="border-y border-border bg-muted/50 uppercase tracking-wider text-secondary">
                <th className="text-left font-medium py-2.5 px-3">Participant</th>
                <th className="text-right font-medium py-2.5 px-3">Settlement Bal</th>
                <th className="text-right font-medium py-2.5 px-3">Gross Debits</th>
                <th className="text-right font-medium py-2.5 px-3">Gross Credits</th>
                <th className="text-right font-medium py-2.5 px-3">Net Position</th>
                <th className="text-left font-medium py-2.5 px-3 min-w-[160px]">
                  Debit Cap Usage
                </th>
                <th className="text-left font-medium py-2.5 px-3">Status</th>
                <th className="text-right font-medium py-2.5 px-3">Liquidity Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(positions?.positions ?? []).map((p) => (
                <tr key={p.institution_code} className="hover:bg-muted/40">
                  <td className="py-3 px-3">
                    <div className="font-semibold text-primary">{p.institution_code}</div>
                    <div className="font-mono text-[11px] text-secondary">
                      {p.settlement_account_number}
                    </div>
                  </td>
                  <td className="py-3 px-3 text-right font-mono font-semibold text-primary tabular-nums">
                    <div>{formatMzn(p.settlement_balance_centavos)}</div>
                    <div className="text-[10px] text-secondary font-normal">
                      Res {formatMzn(p.minimum_reserve_centavos)}
                    </div>
                  </td>
                  <td className="py-3 px-3 text-right font-mono text-error tabular-nums">
                    {formatMzn(p.gross_debits_centavos)}
                  </td>
                  <td className="py-3 px-3 text-right font-mono text-success tabular-nums">
                    {formatMzn(p.gross_credits_centavos)}
                  </td>
                  <td
                    className={`py-3 px-3 text-right font-mono font-semibold tabular-nums ${
                      p.net_position_centavos > 0
                        ? "text-success"
                        : p.net_position_centavos < 0
                          ? "text-error"
                          : "text-secondary"
                    }`}
                  >
                    {p.net_position_centavos > 0 ? "+" : ""}
                    {formatMzn(p.net_position_centavos)}
                  </td>
                  <td className="py-3 px-3">
                    <ProgressBar
                      label={`${p.institution_code} debit cap`}
                      isLabelHidden
                      value={p.liquidity_utilization_bps}
                      max={10000}
                      variant={
                        p.liquidity_utilization_bps >= 8000
                          ? "error"
                          : p.liquidity_utilization_bps >= 5000
                            ? "warning"
                            : "accent"
                      }
                    />
                    <div className="text-[10px] font-mono text-secondary mt-0.5">
                      {(p.liquidity_utilization_bps / 100).toFixed(1)}% of{" "}
                      {formatMzn(p.net_debit_cap_centavos)}
                    </div>
                  </td>
                  <td className="py-3 px-3">
                    <Badge
                      variant={
                        p.status === "LIQUIDITY_SHORTFALL"
                          ? "error"
                          : p.status === "NET_CREDITOR"
                            ? "success"
                            : p.status === "NET_DEBTOR"
                              ? "warning"
                              : "neutral"
                      }
                      label={p.status}
                    />
                  </td>
                  <td className="py-3 px-3 text-right">
                    <div className="inline-flex items-center gap-1">
                      <Button
                        label={p.simulate_shortfall ? "Clear Fault" : "Fail Liq"}
                        size="sm"
                        variant="ghost"
                        isDisabled={!canOperate || busy}
                        onClick={() =>
                          handleToggleShortfall(p.institution_code, !p.simulate_shortfall)
                        }
                      />
                      <Button
                        label="+1M MT"
                        size="sm"
                        variant="secondary"
                        icon={<PlusCircle className="w-3 h-3" />}
                        isDisabled={!canOperate || busy}
                        onClick={() => handleTopup(p.institution_code)}
                      />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {/* Settlement Cycles & Signed Reports */}
      <Card padding={5} elevation="low" className="w-full">
        <div className="flex items-center justify-between mb-4">
          <div>
            <Heading level={4} weight="semibold">
              Settlement Cycle History &amp; Signed Reports
            </Heading>
            <Text type="supporting">
              Each finalized or failed MNS cycle generates an Ed25519-signed settlement statement
            </Text>
          </div>
          <Badge variant="info" label={`${cycles.length} cycles`} />
        </div>

        {cycles.length === 0 ? (
          <div className="py-8 text-center text-xs text-secondary border border-dashed border-border rounded-md bg-muted/40">
            No MNS settlement cycles executed yet. Complete transfers and click &ldquo;Execute MNS
            Cycle&rdquo;.
          </div>
        ) : (
          <div className="overflow-x-auto -mx-5 px-5">
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="border-y border-border bg-muted/50 uppercase tracking-wider text-secondary">
                  <th className="text-left font-medium py-2 px-3">Cycle</th>
                  <th className="text-left font-medium py-2 px-3">Status</th>
                  <th className="text-right font-medium py-2 px-3">Tx Count</th>
                  <th className="text-right font-medium py-2 px-3">Gross Volume</th>
                  <th className="text-right font-medium py-2 px-3">Net Settled</th>
                  <th className="text-left font-medium py-2 px-3">Operator / Notes</th>
                  <th className="text-right font-medium py-2 px-3">Report</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {cycles.map((c) => (
                  <tr key={c.id} className="hover:bg-muted/40">
                    <td className="py-2.5 px-3 font-mono font-semibold text-primary">
                      #{c.cycle_number} · {formatTimeShort(c.created_at)}
                    </td>
                    <td className="py-2.5 px-3">
                      <Badge
                        variant={c.status === "SETTLED" ? "success" : "error"}
                        label={c.status}
                      />
                    </td>
                    <td className="py-2.5 px-3 text-right font-mono tabular-nums">{c.tx_count}</td>
                    <td className="py-2.5 px-3 text-right font-mono tabular-nums">
                      {formatMzn(c.total_gross_volume_centavos)}
                    </td>
                    <td className="py-2.5 px-3 text-right font-mono font-semibold tabular-nums">
                      {formatMzn(c.total_net_settled_centavos)}
                    </td>
                    <td className="py-2.5 px-3 text-secondary max-w-[260px] truncate">
                      {c.failure_reason || `Initiated by ${c.initiated_by}`}
                    </td>
                    <td className="py-2.5 px-3 text-right">
                      <Button
                        label="Verify Report"
                        size="sm"
                        variant="secondary"
                        icon={<FileCheck2 className="w-3 h-3" />}
                        onClick={() => handleViewReport(c.id)}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {activeReport && (
          <div className="mt-4 p-4 rounded-md bg-muted border border-border space-y-2 text-xs">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 font-semibold text-primary">
                <CheckCircle2 className="w-4 h-4 text-success" />
                <span>MNS Cycle #{activeReport.cycle_number} — Cryptographic Report</span>
              </div>
              <Badge
                variant={activeReport.signature_valid ? "success" : "error"}
                label={
                  activeReport.signature_valid
                    ? "Ed25519 Signature Valid"
                    : "Signature Tampered!"
                }
              />
            </div>
            <div className="font-mono text-[11px] text-secondary break-all">
              Sig: {activeReport.signature}
            </div>
            <pre className="p-3 rounded wire-frame-box text-[11px] overflow-x-auto max-h-56">
              {JSON.stringify(activeReport.report, null, 2)}
            </pre>
          </div>
        )}
      </Card>
    </div>
  );
}
