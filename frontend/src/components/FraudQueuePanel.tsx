"use client";

import { useState } from "react";
import { CheckCircle2, ShieldAlert, ShieldCheck, XCircle } from "lucide-react";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { Text } from "@astryxdesign/core/Text";

import { decideFraudCase } from "@/lib/api";
import { formatMzn, formatTimeShort } from "@/lib/format";
import type { FraudCase } from "@/lib/types";

interface FraudQueuePanelProps {
  cases: FraudCase[];
  currentRole: string;
  onRefresh: () => void;
}

export function FraudQueuePanel({ cases, currentRole, onRefresh }: FraudQueuePanelProps) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [banner, setBanner] = useState<{ status: "success" | "error" | "info"; text: string } | null>(
    null
  );

  const canReview =
    currentRole === "FRAUD_ANALYST" || currentRole === "CHECKER" || currentRole === "ADMIN";

  async function handleDecision(fcase: FraudCase, decision: "RELEASE" | "CONFIRM_FRAUD") {
    setBusyId(fcase.id);
    setBanner(null);
    try {
      const res = (await decideFraudCase(
        fcase.id,
        decision,
        decision === "RELEASE"
          ? "Verified legitimate customer activity; released to interbank clearing"
          : "Confirmed suspicious layering/velocity pattern; declined DE 39=59"
      )) as { transaction_status: string; response_code: string | null };
      setBanner({
        status: decision === "RELEASE" ? "success" : "info",
        text: `Fraud case ${decision}: transaction moved to ${res.transaction_status} (DE 39=${res.response_code ?? "--"}).`,
      });
      onRefresh();
    } catch (e) {
      setBanner({ status: "error", text: e instanceof Error ? e.message : "Decision failed" });
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-6">
      {banner && (
        <Banner
          status={banner.status}
          title="Fraud Investigation Disposition"
          description={banner.text}
          isDismissable
          onDismiss={() => setBanner(null)}
        />
      )}

      <Card padding={5} elevation="low" className="w-full">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
          <div>
            <div className="flex items-center gap-2">
              <ShieldAlert className="w-4 h-4 text-accent" />
              <Heading level={4} weight="semibold">
                Fraud Detection Review Queue &amp; Investigation Audit Trail
              </Heading>
            </div>
            <Text type="supporting" display="block">
              Real-time Redis sliding-window velocity, structuring, fan-out &amp; rapid round-trip
              layering detection (Score ≥50 Review · ≥80 Auto-Block DE 39=59)
            </Text>
          </div>
          <Badge
            variant={cases.some((c) => c.status === "PENDING_REVIEW") ? "warning" : "success"}
            label={`${cases.filter((c) => c.status === "PENDING_REVIEW").length} pending review`}
          />
        </div>

        {cases.length === 0 ? (
          <div className="py-10 text-center rounded-md border border-dashed border-border bg-muted/40 text-xs text-secondary">
            <ShieldCheck className="w-6 h-6 mx-auto mb-2 text-success" />
            No flagged or blocked transactions yet. Trigger a rapid round-trip (BCI → STDB → BCI
            for ≥5,000 MT) or high-velocity burst (6+ transfers/min) to observe risk scoring.
          </div>
        ) : (
          <div className="space-y-4">
            {cases.map((c) => (
              <div
                key={c.id}
                className="p-4 rounded-lg border border-border bg-surface space-y-3"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="flex items-center gap-2 flex-wrap">
                      <Badge
                        variant={
                          c.status === "PENDING_REVIEW"
                            ? "warning"
                            : c.status === "RELEASED"
                              ? "success"
                              : "error"
                        }
                        label={c.status}
                      />
                      <span className="font-mono text-xs font-semibold text-primary">
                        {c.originator_code}:{c.originator_account_number} → {c.beneficiary_code}:
                        {c.beneficiary_account_number}
                      </span>
                      <span className="font-mono text-xs font-semibold text-accent tabular-nums">
                        {formatMzn(c.amount_centavos)}
                      </span>
                    </div>
                    <div className="text-[11px] text-secondary mt-1">
                      Tx ID: <span className="font-mono">{c.transaction_id}</span> · Created{" "}
                      {formatTimeShort(c.created_at)}
                    </div>
                  </div>

                  <div className="flex items-center gap-2">
                    <div className="w-32">
                      <div className="flex justify-between text-[11px] font-mono mb-0.5">
                        <span className="text-secondary">Risk Score</span>
                        <span className="font-semibold text-error">{c.risk_score}/100</span>
                      </div>
                      <ProgressBar
                        label="Risk score"
                        isLabelHidden
                        value={c.risk_score}
                        max={100}
                        variant={c.risk_score >= 80 ? "error" : "warning"}
                      />
                    </div>
                    {c.status === "PENDING_REVIEW" && (
                      <div className="flex items-center gap-1.5">
                        <Button
                          label="Release"
                          size="sm"
                          variant="primary"
                          icon={<CheckCircle2 className="w-3.5 h-3.5" />}
                          isLoading={busyId === c.id}
                          isDisabled={!canReview}
                          onClick={() => handleDecision(c, "RELEASE")}
                        />
                        <Button
                          label="Confirm Fraud"
                          size="sm"
                          variant="secondary"
                          icon={<XCircle className="w-3.5 h-3.5" />}
                          isDisabled={!canReview || busyId === c.id}
                          onClick={() => handleDecision(c, "CONFIRM_FRAUD")}
                        />
                      </div>
                    )}
                  </div>
                </div>

                {/* Triggered Rules */}
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  {c.triggered_rules.map((r, i) => (
                    <div
                      key={`${c.id}-rule-${i}`}
                      className="px-3 py-2 rounded bg-muted border border-border text-xs"
                    >
                      <div className="flex items-center justify-between font-mono font-semibold text-primary">
                        <span>{r.rule}</span>
                        <span className="text-error">+{r.weight}</span>
                      </div>
                      <div className="text-secondary mt-0.5">{r.detail}</div>
                    </div>
                  ))}
                </div>

                {/* Investigation Audit Trail */}
                <div className="pt-2 border-t border-border">
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-secondary mb-1.5">
                    Investigation Audit Trail
                  </div>
                  <div className="space-y-1">
                    {c.events.map((ev) => (
                      <div
                        key={ev.id}
                        className="flex items-center justify-between text-xs px-2.5 py-1.5 rounded bg-muted/60"
                      >
                        <div>
                          <span className="font-mono font-semibold text-primary">{ev.action}</span>
                          <span className="text-secondary">
                            {" "}
                            by {ev.actor_id} ({ev.actor_role}) — {ev.notes}
                          </span>
                        </div>
                        <span className="font-mono text-[11px] text-secondary shrink-0 ml-2">
                          {formatTimeShort(ev.created_at)}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
