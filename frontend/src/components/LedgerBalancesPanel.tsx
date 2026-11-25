"use client";

import { CheckCircle2, Lock, Scale, ShieldCheck } from "lucide-react";
import { Badge } from "@astryxdesign/core/Badge";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { Text } from "@astryxdesign/core/Text";

import { formatMzn, formatTimeShort } from "@/lib/format";
import type { AccountInfo, LedgerSummary } from "@/lib/types";

interface LedgerBalancesPanelProps {
  accounts: AccountInfo[];
  ledger: LedgerSummary | null;
}

export function LedgerBalancesPanel({ accounts, ledger }: LedgerBalancesPanelProps) {
  return (
    <div className="space-y-6">
      {/* Live Account Balances & Notice 1/GBM/2026 Regulatory Caps */}
      <Card padding={5} elevation="low" className="w-full">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
          <div>
            <Heading level={4} weight="semibold">
              METIX Switch Clearing Suspense &amp; Participant Settlement Accounts
            </Heading>
            <Text type="supporting" display="block">
              Switch-owned PostgreSQL settlement ledger (BIGINT centavos, optimistic locking) —
              customer accounts live exclusively inside BCI &amp; Standard Bank CBS databases
            </Text>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant="neutral" icon={<Lock className="w-3 h-3" />} label="Optimistic Lock" />
            <Badge variant="info" label="ISO 4217 · 943 MZN" />
          </div>
        </div>

        <div className="overflow-x-auto -mx-5 px-5">
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="border-y border-border bg-muted/50 text-xs uppercase tracking-wider text-secondary">
                <th className="text-left font-medium py-2.5 px-3">Institution</th>
                <th className="text-left font-medium py-2.5 px-3">Account &amp; Holder</th>
                <th className="text-left font-medium py-2.5 px-3">Tier</th>
                <th className="text-right font-medium py-2.5 px-3">Balance (MZN)</th>
                <th className="text-left font-medium py-2.5 px-3 min-w-[220px]">
                  Daily Limit Utilization
                </th>
                <th className="text-right font-medium py-2.5 px-3">Lock Ver</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {accounts.map((acct) => {
                const isSuspense = acct.account_tier === "SUSPENSE";
                const cap = acct.daily_limit_centavos ?? 0;
                const pct = cap > 0 ? Math.min(100, Math.round((acct.daily_used_centavos / cap) * 100)) : 0;
                const barVariant =
                  pct >= 90 ? "error" : pct >= 70 ? "warning" : "accent";

                return (
                  <tr
                    key={acct.id}
                    className={`transition-colors ${
                      isSuspense ? "bg-indigo-50/45" : "hover:bg-muted/40"
                    }`}
                  >
                    <td className="py-3 px-3 align-middle">
                      <div className="font-semibold text-primary text-xs">
                        {acct.institution_code}
                      </div>
                      <div className="text-[11px] text-secondary font-mono">{acct.bic_code}</div>
                    </td>
                    <td className="py-3 px-3 align-middle">
                      <div className="font-medium text-primary">{acct.holder_name}</div>
                      <div className="text-xs font-mono text-secondary">{acct.account_number}</div>
                    </td>
                    <td className="py-3 px-3 align-middle">
                      <Badge
                        variant={
                          isSuspense
                            ? "purple"
                            : acct.account_tier === "BUSINESS"
                              ? "info"
                              : "neutral"
                        }
                        label={acct.account_tier}
                      />
                    </td>
                    <td className="py-3 px-3 text-right align-middle">
                      <div
                        className={`font-mono font-semibold tabular-nums ${
                          isSuspense && acct.balance_centavos > 0
                            ? "text-warning"
                            : "text-primary"
                        }`}
                      >
                        {formatMzn(acct.balance_centavos)}
                      </div>
                      <div className="text-[11px] font-mono text-secondary tabular-nums">
                        {acct.balance_centavos.toLocaleString()} ct
                      </div>
                    </td>
                    <td className="py-3 px-3 align-middle">
                      {cap > 0 && acct.institution_role === "ORIGINATOR" ? (
                        <div className="space-y-1">
                          <ProgressBar
                            label={`${acct.account_number} daily limit`}
                            isLabelHidden
                            value={acct.daily_used_centavos}
                            max={cap}
                            variant={barVariant}
                          />
                          <div className="flex items-center justify-between text-[11px] font-mono text-secondary tabular-nums">
                            <span>{formatMzn(acct.daily_used_centavos)}</span>
                            <span>cap {formatMzn(cap)}</span>
                          </div>
                        </div>
                      ) : (
                        <span className="text-xs text-secondary">
                          {isSuspense
                            ? acct.balance_centavos === 0
                              ? "Cleared (0.00 MT in-flight)"
                              : "Holding in-flight ROUTED funds"
                            : "Beneficiary receiver"}
                        </span>
                      )}
                    </td>
                    <td className="py-3 px-3 text-right align-middle">
                      <span className="inline-flex items-center px-2 py-0.5 rounded bg-muted border border-border font-mono text-xs text-secondary tabular-nums">
                        v{acct.version}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      {/* Immutable Double-Entry Journal */}
      <Card padding={5} elevation="low" className="w-full">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
          <div>
            <Heading level={4} weight="semibold">
              Immutable Double-Entry Journal Postings
            </Heading>
            <Text type="supporting" display="block">
              Every balance write creates balanced DEBIT and CREDIT lines — never mutated in place
            </Text>
          </div>
          {ledger && (
            <div className="flex flex-wrap items-center gap-2">
              <Badge
                variant={ledger.is_balanced ? "success" : "error"}
                icon={
                  ledger.is_balanced ? (
                    <ShieldCheck className="w-3 h-3" />
                  ) : (
                    <Scale className="w-3 h-3" />
                  )
                }
                label={
                  ledger.is_balanced
                    ? `SUM(DR) == SUM(CR) · ${formatMzn(ledger.total_debits_centavos)}`
                    : "LEDGER IMBALANCE DETECTED"
                }
              />
            </div>
          )}
        </div>

        {!ledger || ledger.entries.length === 0 ? (
          <div className="py-10 text-center rounded-lg border border-dashed border-border bg-muted/40">
            <CheckCircle2 className="w-6 h-6 mx-auto mb-2 text-secondary" />
            <Text type="supporting">
              No journal entries posted yet. Initiate a transfer to observe clearing hold &amp;
              settlement legs.
            </Text>
          </div>
        ) : (
          <div className="overflow-x-auto -mx-5 px-5">
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="border-y border-border bg-muted/50 uppercase tracking-wider text-secondary">
                  <th className="text-left font-medium py-2.5 px-3">Time</th>
                  <th className="text-left font-medium py-2.5 px-3">Entry Type</th>
                  <th className="text-left font-medium py-2.5 px-3">Description</th>
                  <th className="text-left font-medium py-2.5 px-3">Account</th>
                  <th className="text-right font-medium py-2.5 px-3">DEBIT (MZN)</th>
                  <th className="text-right font-medium py-2.5 px-3">CREDIT (MZN)</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {ledger.entries.flatMap((entry) =>
                  entry.postings.map((posting, idx) => (
                    <tr key={posting.id} className="hover:bg-muted/40">
                      <td className="py-2.5 px-3 font-mono text-secondary tabular-nums whitespace-nowrap">
                        {idx === 0 ? formatTimeShort(entry.created_at) : ""}
                      </td>
                      <td className="py-2.5 px-3 whitespace-nowrap">
                        {idx === 0 && (
                          <Badge
                            variant={
                              entry.entry_type === "SETTLEMENT"
                                ? "success"
                                : entry.entry_type === "REVERSAL"
                                  ? "info"
                                  : "warning"
                            }
                            label={entry.entry_type}
                          />
                        )}
                      </td>
                      <td className="py-2.5 px-3 text-primary max-w-[280px] truncate">
                        {idx === 0 ? entry.description : ""}
                      </td>
                      <td className="py-2.5 px-3 font-mono whitespace-nowrap">
                        <span className="font-semibold text-primary">
                          {posting.institution_code ?? "--"}
                        </span>{" "}
                        <span className="text-secondary">
                          {posting.account_number ?? posting.account_id}
                        </span>
                      </td>
                      <td className="py-2.5 px-3 text-right font-mono tabular-nums text-error font-medium">
                        {posting.direction === "DEBIT" ? formatMzn(posting.amount_centavos) : "—"}
                      </td>
                      <td className="py-2.5 px-3 text-right font-mono tabular-nums text-success font-medium">
                        {posting.direction === "CREDIT" ? formatMzn(posting.amount_centavos) : "—"}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
