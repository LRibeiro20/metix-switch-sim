"use client";

import { useState } from "react";
import { Building2, Lock, ShieldCheck, Snowflake, Unlock } from "lucide-react";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";

import { LedgerBalancesPanel } from "@/components/LedgerBalancesPanel";
import { proposeAccountRestriction } from "@/lib/api";
import { formatMzn } from "@/lib/format";
import type { AccountInfo, BankCustomerAccount, LedgerSummary } from "@/lib/types";

interface CoreBankingPanelProps {
  bankAccounts: BankCustomerAccount[];
  switchAccounts: AccountInfo[];
  ledger: LedgerSummary | null;
  currentRole: string;
  onRefresh: () => void;
}

export function CoreBankingPanel({
  bankAccounts,
  switchAccounts,
  ledger,
  currentRole,
  onRefresh,
}: CoreBankingPanelProps) {
  const [busyAcct, setBusyAcct] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ status: "success" | "error" | "info"; text: string } | null>(
    null
  );

  const bciAccounts = bankAccounts.filter((a) => a.bank_code === "BCI_MZ");
  const stdbAccounts = bankAccounts.filter((a) => a.bank_code === "STDB_MZ");
  const walletAccounts = bankAccounts.filter(
    (a) => a.bank_code === "MPESA_MZ" || a.bank_code === "EMOLA_MZ"
  );

  const canPropose = currentRole === "MAKER" || currentRole === "ADMIN";

  async function handleStatusAction(acct: BankCustomerAccount, nextStatus: string) {
    setBusyAcct(acct.account_number);
    setNotice(null);
    try {
      await proposeAccountRestriction(acct.bank_code, acct.account_number, {
        status: nextStatus,
        reason: `Core Banking restriction change to ${nextStatus} via Operations Console`,
        immediate: false,
      });
      setNotice({
        status: "info",
        text: `Maker-Checker request submitted to set ${acct.bank_code} ${acct.account_number} -> ${nextStatus}. Switch persona to CHECKER to approve.`,
      });
      onRefresh();
    } catch (e) {
      setNotice({
        status: "error",
        text: e instanceof Error ? e.message : "Failed submitting restriction request",
      });
    } finally {
      setBusyAcct(null);
    }
  }

  function renderBankTable(title: string, subtitle: string, dbTag: string, rows: BankCustomerAccount[]) {
    return (
      <Card padding={5} elevation="low" className="w-full">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
          <div>
            <div className="flex items-center gap-2">
              <Building2 className="w-4 h-4 text-accent" />
              <Heading level={4} weight="semibold">
                {title}
              </Heading>
            </div>
            <Text type="supporting" display="block">
              {subtitle}
            </Text>
          </div>
          <div className="flex items-center gap-1.5">
            <Badge variant="info" label={dbTag} />
            <Badge variant="success" icon={<Lock className="w-3 h-3" />} label="AES-256-GCM PII" />
          </div>
        </div>

        <div className="overflow-x-auto -mx-5 px-5">
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr className="border-y border-border bg-muted/50 uppercase tracking-wider text-secondary">
                <th className="text-left font-medium py-2 px-3">Account &amp; Holder</th>
                <th className="text-left font-medium py-2 px-3">Status</th>
                <th className="text-right font-medium py-2 px-3">Available</th>
                <th className="text-right font-medium py-2 px-3">Ledger</th>
                <th className="text-right font-medium py-2 px-3">Earmarked</th>
                <th className="text-right font-medium py-2 px-3">Per-Tx / Daily Cap</th>
                <th className="text-right font-medium py-2 px-3">4-Eyes Controls</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((acct) => (
                <tr key={acct.id} className="hover:bg-muted/40">
                  <td className="py-2.5 px-3">
                    <div className="font-semibold text-primary">{acct.holder_name}</div>
                    <div className="font-mono text-[11px] text-secondary">
                      {acct.account_number} · {acct.account_tier} · v{acct.version}
                    </div>
                  </td>
                  <td className="py-2.5 px-3">
                    <Badge
                      variant={
                        acct.status === "ACTIVE"
                          ? "success"
                          : acct.status === "PND" || acct.status === "PNC"
                            ? "warning"
                            : "error"
                      }
                      label={acct.status}
                    />
                  </td>
                  <td className="py-2.5 px-3 text-right font-mono font-semibold text-primary tabular-nums">
                    {formatMzn(acct.available_balance_centavos)}
                  </td>
                  <td className="py-2.5 px-3 text-right font-mono text-secondary tabular-nums">
                    {formatMzn(acct.ledger_balance_centavos)}
                  </td>
                  <td className="py-2.5 px-3 text-right font-mono tabular-nums">
                    <span
                      className={
                        acct.earmarked_centavos > 0 ? "text-warning font-semibold" : "text-secondary"
                      }
                    >
                      {formatMzn(acct.earmarked_centavos)}
                    </span>
                  </td>
                  <td className="py-2.5 px-3 text-right font-mono text-secondary tabular-nums">
                    <div>{formatMzn(acct.per_tx_limit_centavos)}</div>
                    <div className="text-[10px]">
                      Used {formatMzn(acct.daily_used_centavos)} / {formatMzn(acct.daily_limit_centavos)}
                    </div>
                  </td>
                  <td className="py-2.5 px-3 text-right">
                    <div className="inline-flex items-center gap-1">
                      {acct.status === "ACTIVE" ? (
                        <>
                          <Button
                            label="PND"
                            size="sm"
                            variant="secondary"
                            isDisabled={!canPropose || busyAcct === acct.account_number}
                            onClick={() => handleStatusAction(acct, "PND")}
                          />
                          <Button
                            label="Freeze"
                            size="sm"
                            variant="ghost"
                            icon={<Snowflake className="w-3 h-3" />}
                            isDisabled={!canPropose || busyAcct === acct.account_number}
                            onClick={() => handleStatusAction(acct, "FROZEN")}
                          />
                        </>
                      ) : (
                        <Button
                          label="Restore"
                          size="sm"
                          variant="secondary"
                          icon={<Unlock className="w-3 h-3" />}
                          isDisabled={!canPropose || busyAcct === acct.account_number}
                          onClick={() => handleStatusAction(acct, "ACTIVE")}
                        />
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      {notice && (
        <Banner
          status={notice.status}
          title="Core Banking Restriction Workflow"
          description={notice.text}
          isDismissable
          onDismiss={() => setNotice(null)}
        />
      )}

      {renderBankTable(
        "BCI Core Banking Simulator (BCI_MZ)",
        "Independent FastAPI CBS + PostgreSQL bci_cbs · Available vs Ledger balances & earmarks",
        "DB: bci_cbs (:5434)",
        bciAccounts
      )}

      {renderBankTable(
        "Standard Bank Core Banking Simulator (STDB_MZ)",
        "Independent FastAPI CBS + PostgreSQL stdb_cbs · Available vs Ledger balances & earmarks",
        "DB: stdb_cbs (:5435)",
        stdbAccounts
      )}

      {walletAccounts.length > 0 && (
        <Card padding={4} elevation="low" className="w-full">
          <div className="flex items-center justify-between mb-3">
            <div>
              <Heading level={4} weight="semibold">
                External Mobile Wallet Adapters (MPESA_MZ &amp; EMOLA_MZ)
              </Heading>
              <Text type="supporting">
                Isolated wallet-sim service (zero shared tables with metix_switch)
              </Text>
            </div>
            <Badge variant="neutral" icon={<ShieldCheck className="w-3 h-3" />} label="wallet-sim" />
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {walletAccounts.map((w) => (
              <div
                key={w.id}
                className="flex items-center justify-between px-3.5 py-2.5 rounded-md bg-muted border border-border text-xs"
              >
                <div>
                  <div className="font-semibold text-primary">
                    {w.bank_code} · {w.holder_name}
                  </div>
                  <div className="font-mono text-secondary">{w.account_number}</div>
                </div>
                <div className="text-right font-mono font-semibold text-primary tabular-nums">
                  {formatMzn(w.available_balance_centavos)}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <LedgerBalancesPanel accounts={switchAccounts} ledger={ledger} />
    </div>
  );
}
