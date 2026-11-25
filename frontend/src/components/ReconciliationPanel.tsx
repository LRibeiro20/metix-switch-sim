"use client";

import { useState } from "react";
import {
  AlertTriangle,
  Bug,
  CheckCircle2,
  GitCompare,
  ShieldCheck,
  UserCheck,
  XCircle,
} from "lucide-react";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";

import {
  decideMakerChecker,
  injectReconFault,
  proposeReconAdjustment,
  runReconciliation,
} from "@/lib/api";
import { formatMzn, formatTimeShort } from "@/lib/format";
import type {
  MakerCheckerRequest,
  ReconciliationException,
  SuspenseBreakdown,
} from "@/lib/types";

interface ReconciliationPanelProps {
  exceptions: ReconciliationException[];
  suspense: SuspenseBreakdown | null;
  makerCheckerRequests: MakerCheckerRequest[];
  currentUser: string;
  currentRole: string;
  onRefresh: () => void;
}

export function ReconciliationPanel({
  exceptions,
  suspense,
  makerCheckerRequests,
  currentUser,
  currentRole,
  onRefresh,
}: ReconciliationPanelProps) {
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ status: "success" | "error" | "info"; text: string } | null>(
    null
  );

  const canMake = currentRole === "MAKER" || currentRole === "ADMIN";
  const canCheck = currentRole === "CHECKER" || currentRole === "ADMIN";

  async function handleRunRecon() {
    setBusy(true);
    setBanner(null);
    try {
      const res = (await runReconciliation()) as {
        run: {
          switch_tx_count: number;
          bci_tx_count: number;
          stdb_tx_count: number;
          matched_count: number;
          new_exception_count: number;
        };
      };
      setBanner({
        status: res.run.new_exception_count > 0 ? "info" : "success",
        text: `3-Way Recon completed: Switch (${res.run.switch_tx_count}), BCI CBS (${res.run.bci_tx_count}), STDB CBS (${res.run.stdb_tx_count}) -> ${res.run.matched_count} matched, ${res.run.new_exception_count} new exception(s).`,
      });
      onRefresh();
    } catch (e) {
      setBanner({ status: "error", text: e instanceof Error ? e.message : "Recon failed" });
    } finally {
      setBusy(false);
    }
  }

  async function handleInjectFault(
    faultType: "ORPHAN_CBS_POSTING" | "DUPLICATE_CBS_POSTING" | "STALE_ACTIVE_HOLD"
  ) {
    setBusy(true);
    setBanner(null);
    try {
      await injectReconFault({
        bank_code: faultType === "STALE_ACTIVE_HOLD" ? "BCI_MZ" : "STDB_MZ",
        fault_type: faultType,
        account_number: faultType === "STALE_ACTIVE_HOLD" ? "00010012345678" : "00030055443322",
        amount_centavos: 175_000,
      });
      setBanner({
        status: "info",
        text: `Injected ${faultType} in Core Banking DB. Click "Run 3-Way Recon" to detect it.`,
      });
      onRefresh();
    } catch (e) {
      setBanner({ status: "error", text: e instanceof Error ? e.message : "Fault injection failed" });
    } finally {
      setBusy(false);
    }
  }

  async function handleProposeAdjustment(exc: ReconciliationException) {
    setBusy(true);
    setBanner(null);
    try {
      await proposeReconAdjustment(
        exc.id,
        exc.switch_tx_id ? "RELEASE_SUSPENSE_TO_ORIGINATOR" : "DISMISS_WITH_NOTES",
        `Reconciliation adjustment for ${exc.exception_type} (${exc.bank_code} STAN ${exc.stan ?? "N/A"})`
      );
      setBanner({
        status: "info",
        text: `Adjustment proposed by ${currentUser}. Switch persona to CHECKER to execute 4-eyes approval.`,
      });
      onRefresh();
    } catch (e) {
      setBanner({ status: "error", text: e instanceof Error ? e.message : "Proposal failed" });
    } finally {
      setBusy(false);
    }
  }

  async function handleDecide(req: MakerCheckerRequest, approve: boolean) {
    setBusy(true);
    setBanner(null);
    try {
      await decideMakerChecker(
        req.id,
        approve,
        approve ? "Verified against CBS ledger & approved" : "Rejected by checker"
      );
      setBanner({
        status: approve ? "success" : "info",
        text: `Maker-Checker request ${approve ? "APPROVED & EXECUTED" : "REJECTED"} by ${currentUser}.`,
      });
      onRefresh();
    } catch (e) {
      setBanner({ status: "error", text: e instanceof Error ? e.message : "Approval error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      {banner && (
        <Banner
          status={banner.status}
          title="Financial Reconciliation & Maker-Checker"
          description={banner.text}
          isDismissable
          onDismiss={() => setBanner(null)}
        />
      )}

      {/* 3-Way Recon Controls & Suspense Summary */}
      <Card padding={5} elevation="low" className="w-full">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
          <div>
            <div className="flex items-center gap-2">
              <GitCompare className="w-4 h-4 text-accent" />
              <Heading level={4} weight="semibold">
                3-Way Financial Reconciliation (Switch ↔ BCI CBS ↔ Standard Bank CBS)
              </Heading>
            </div>
            <Text type="supporting" display="block">
              Detects missing CBS postings, orphaned postings, duplicates, incomplete holds &amp;
              aged suspense
            </Text>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              label="Run 3-Way Recon"
              variant="primary"
              size="sm"
              icon={<GitCompare className="w-3.5 h-3.5" />}
              isLoading={busy}
              isDisabled={!canMake && !canCheck}
              onClick={handleRunRecon}
            />
            <Button
              label="Inject Orphan CBS Tx"
              variant="secondary"
              size="sm"
              icon={<Bug className="w-3.5 h-3.5" />}
              isDisabled={!canMake || busy}
              onClick={() => handleInjectFault("ORPHAN_CBS_POSTING")}
            />
            <Button
              label="Inject Stale Hold"
              variant="ghost"
              size="sm"
              isDisabled={!canMake || busy}
              onClick={() => handleInjectFault("STALE_ACTIVE_HOLD")}
            />
          </div>
        </div>

        {/* Suspense Account Breakdown */}
        <div className="p-3.5 rounded-md bg-muted border border-border mb-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-xs">
              <span className="font-semibold text-primary">
                Suspense Clearing Account ({suspense?.suspense_account_number ?? "METIX-SUSPENSE-943"})
              </span>
              <span className="text-secondary ml-2">
                {suspense?.uncleared_transactions.length ?? 0} uncleared transaction(s)
              </span>
            </div>
            <div className="font-mono font-semibold text-sm text-primary tabular-nums">
              Float: {formatMzn(suspense?.balance_centavos ?? 0)}
            </div>
          </div>
          {(suspense?.uncleared_transactions.length ?? 0) > 0 && (
            <div className="mt-2 space-y-1 text-xs">
              {suspense!.uncleared_transactions.map((u) => (
                <div
                  key={u.id}
                  className="flex items-center justify-between px-2.5 py-1.5 rounded bg-surface border border-border"
                >
                  <span className="font-mono">
                    STAN {u.stan} · {u.originator_code} → {u.beneficiary_code}
                  </span>
                  <div className="flex items-center gap-2">
                    <Badge
                      variant={u.status === "RECON_HOLD" ? "error" : "warning"}
                      label={u.status}
                    />
                    <span className="font-mono font-semibold tabular-nums">
                      {formatMzn(u.amount_centavos)}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Exceptions Table */}
        {exceptions.length === 0 ? (
          <div className="py-8 text-center rounded-md border border-dashed border-border bg-muted/40 text-xs text-secondary">
            <CheckCircle2 className="w-5 h-5 mx-auto mb-1.5 text-success" />
            All Switch, BCI CBS, and Standard Bank CBS records are in balance. Run 3-Way Recon after
            transfers or inject a fault to test exception handling.
          </div>
        ) : (
          <div className="overflow-x-auto -mx-5 px-5">
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="border-y border-border bg-muted/50 uppercase tracking-wider text-secondary">
                  <th className="text-left font-medium py-2 px-3">Exception Type</th>
                  <th className="text-left font-medium py-2 px-3">Bank / Ref</th>
                  <th className="text-right font-medium py-2 px-3">Amount</th>
                  <th className="text-left font-medium py-2 px-3">Detail</th>
                  <th className="text-left font-medium py-2 px-3">Status</th>
                  <th className="text-right font-medium py-2 px-3">Maker Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {exceptions.map((exc) => (
                  <tr key={exc.id} className="hover:bg-muted/40">
                    <td className="py-2.5 px-3">
                      <Badge
                        variant={exc.severity === "CRITICAL" ? "error" : "warning"}
                        icon={<AlertTriangle className="w-3 h-3" />}
                        label={exc.exception_type}
                      />
                    </td>
                    <td className="py-2.5 px-3 font-mono">
                      <div className="font-semibold text-primary">{exc.bank_code}</div>
                      <div className="text-[11px] text-secondary">
                        STAN {exc.stan ?? "--"} · {exc.cbs_ref ?? "N/A"}
                      </div>
                    </td>
                    <td className="py-2.5 px-3 text-right font-mono font-semibold tabular-nums">
                      {formatMzn(exc.amount_centavos)}
                    </td>
                    <td className="py-2.5 px-3 text-secondary max-w-[300px]">
                      <div>{exc.description}</div>
                      {exc.resolution_notes && (
                        <div className="text-[11px] text-success mt-0.5">{exc.resolution_notes}</div>
                      )}
                    </td>
                    <td className="py-2.5 px-3">
                      <Badge
                        variant={
                          exc.status === "RESOLVED"
                            ? "success"
                            : exc.status === "PENDING_ADJUSTMENT"
                              ? "info"
                              : "error"
                        }
                        label={exc.status}
                      />
                    </td>
                    <td className="py-2.5 px-3 text-right">
                      {exc.status === "OPEN" && (
                        <Button
                          label="Propose Adj"
                          size="sm"
                          variant="secondary"
                          isDisabled={!canMake || busy}
                          onClick={() => handleProposeAdjustment(exc)}
                        />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* 4-Eyes Maker-Checker Approval Queue */}
      <Card padding={5} elevation="low" className="w-full">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
          <div>
            <div className="flex items-center gap-2">
              <UserCheck className="w-4 h-4 text-accent" />
              <Heading level={4} weight="semibold">
                Maker-Checker 4-Eyes Approval Queue
              </Heading>
            </div>
            <Text type="supporting" display="block">
              Dual-control authorization for reconciliation adjustments, CBS account restrictions,
              and liquidity top-ups (self-approval strictly blocked by DB + API)
            </Text>
          </div>
          <Badge
            variant="purple"
            icon={<ShieldCheck className="w-3 h-3" />}
            label={`Active: ${currentUser} (${currentRole})`}
          />
        </div>

        {makerCheckerRequests.length === 0 ? (
          <div className="py-6 text-center text-xs text-secondary rounded-md border border-dashed border-border bg-muted/40">
            No Maker-Checker approval requests yet. Propose a reconciliation adjustment or CBS
            account restriction as Maker.
          </div>
        ) : (
          <div className="overflow-x-auto -mx-5 px-5">
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="border-y border-border bg-muted/50 uppercase tracking-wider text-secondary">
                  <th className="text-left font-medium py-2 px-3">Action</th>
                  <th className="text-left font-medium py-2 px-3">Resource &amp; Reason</th>
                  <th className="text-left font-medium py-2 px-3">Maker</th>
                  <th className="text-left font-medium py-2 px-3">Checker</th>
                  <th className="text-left font-medium py-2 px-3">Status</th>
                  <th className="text-right font-medium py-2 px-3">4-Eyes Decision</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {makerCheckerRequests.map((req) => {
                  const isSelf = req.maker_id === currentUser;
                  return (
                    <tr key={req.id} className="hover:bg-muted/40">
                      <td className="py-2.5 px-3">
                        <Badge variant="info" label={req.action_type} />
                        <div className="text-[10px] text-secondary font-mono mt-0.5">
                          {formatTimeShort(req.created_at)}
                        </div>
                      </td>
                      <td className="py-2.5 px-3 max-w-[280px]">
                        <div className="font-mono text-[11px] font-semibold text-primary truncate">
                          {req.resource_type}: {req.resource_id}
                        </div>
                        <div className="text-secondary">{req.reason}</div>
                      </td>
                      <td className="py-2.5 px-3 font-mono">
                        <div>{req.maker_id}</div>
                        <div className="text-[10px] text-secondary">{req.maker_role}</div>
                      </td>
                      <td className="py-2.5 px-3 font-mono text-secondary">
                        {req.checker_id ? `${req.checker_id} (${req.checker_role})` : "Pending"}
                      </td>
                      <td className="py-2.5 px-3">
                        <Badge
                          variant={
                            req.status === "APPROVED"
                              ? "success"
                              : req.status === "REJECTED"
                                ? "error"
                                : "warning"
                          }
                          label={req.status}
                        />
                      </td>
                      <td className="py-2.5 px-3 text-right">
                        {req.status === "PENDING" && (
                          <div className="inline-flex items-center gap-1">
                            <Button
                              label={isSelf ? "Self (Blocked)" : "Approve"}
                              size="sm"
                              variant="primary"
                              icon={<CheckCircle2 className="w-3 h-3" />}
                              isDisabled={!canCheck || busy}
                              onClick={() => handleDecide(req, true)}
                            />
                            <Button
                              label="Reject"
                              size="sm"
                              variant="ghost"
                              icon={<XCircle className="w-3 h-3" />}
                              isDisabled={!canCheck || busy}
                              onClick={() => handleDecide(req, false)}
                            />
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
