"use client";

import { useEffect, useMemo, useState } from "react";
import { ArrowDownLeft, ArrowUpRight, Check, Clock, Copy, Layers, Terminal } from "lucide-react";
import { Badge, type BadgeVariant } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Code } from "@astryxdesign/core/Code";
import { Divider } from "@astryxdesign/core/Divider";
import { Heading } from "@astryxdesign/core/Heading";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Text } from "@astryxdesign/core/Text";

import { formatMzn, formatTimeShort } from "@/lib/format";
import type { IsoMessage, SwitchTransaction, TransactionStatus } from "@/lib/types";

interface IsoWireInspectorProps {
  transactions: SwitchTransaction[];
  selectedTxId: string | null;
  onSelectTx: (id: string) => void;
  expanded?: boolean;
}

function statusBadgeVariant(status: TransactionStatus): BadgeVariant {
  switch (status) {
    case "COMPLETED":
      return "success";
    case "ROUTED":
    case "INITIATED":
      return "warning";
    case "REVERSED":
    case "REVERSAL_SENT":
      return "info";
    case "DECLINED":
    case "TIMED_OUT":
    case "RECON_HOLD":
      return "error";
    default:
      return "neutral";
  }
}

function mtiBadgeVariant(mti: string): BadgeVariant {
  switch (mti) {
    case "0200":
      return "info";
    case "0210":
      return "success";
    case "0420":
      return "warning";
    case "0430":
      return "purple";
    default:
      return "neutral";
  }
}

function SlaCountdown({ deadlineIso }: { deadlineIso: string }) {
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNowMs(Date.now()), 250);
    return () => clearInterval(timer);
  }, []);

  const remainingMs = new Date(deadlineIso).getTime() - nowMs;
  const remainingSec = Math.max(0, remainingMs / 1000);
  const expired = remainingMs <= 0;

  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-mono font-semibold tabular-nums ${
        expired
          ? "bg-error-muted text-error"
          : remainingSec <= 8
            ? "bg-warning-muted text-warning"
            : "bg-info-muted text-info"
      }`}
    >
      <Clock className="w-3 h-3" />
      {expired ? "SLA EXPIRED · SWEEPING" : `SLA ${remainingSec.toFixed(1)}s`}
    </span>
  );
}

function CopyRawButton({ raw }: { raw: string }) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(raw);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard unavailable
    }
  }

  return (
    <Button
      label={copied ? "Copied" : "Copy wire"}
      variant="ghost"
      size="sm"
      icon={copied ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
      onClick={handleCopy}
    />
  );
}

function IsoFrameCard({ msg }: { msg: IsoMessage }) {
  const p = msg.payload_json;
  const mtiPart = msg.raw_iso_snippet.slice(0, 4);
  const bitmapPart = msg.raw_iso_snippet.slice(4, 36);
  const fieldsPart = msg.raw_iso_snippet.slice(36);

  const elements: Array<{ de: string; name: string; value: string }> = [
    { de: "DE 2", name: "PAN / Originator", value: p.de2 },
    { de: "DE 3", name: "Processing Code", value: p.de3 },
    {
      de: "DE 4",
      name: "Amount (Centavos)",
      value: `${String(p.de4).padStart(12, "0")} (${formatMzn(p.de4)})`,
    },
    { de: "DE 7", name: "Transmission UTC", value: p.de7 },
    { de: "DE 11", name: "STAN", value: p.de11 },
    { de: "DE 37", name: "RRN", value: p.de37 },
    { de: "DE 49", name: "Currency (MZN)", value: p.de49 },
    { de: "DE 102", name: "Originator ID", value: p.de102 },
    { de: "DE 103", name: "Beneficiary ID", value: p.de103 },
  ];

  if (p.de38) elements.splice(6, 0, { de: "DE 38", name: "Auth ID", value: p.de38 });
  if (p.de39) elements.splice(7, 0, { de: "DE 39", name: "Response Code", value: p.de39 });
  if (p.de90) elements.push({ de: "DE 90", name: "Original Data Elements", value: p.de90 });

  const isInbound = msg.direction === "INBOUND";

  return (
    <div className="rounded-lg border border-border bg-surface overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 px-3.5 py-2.5 bg-muted/70 border-b border-border">
        <div className="flex items-center gap-2">
          <Badge variant={mtiBadgeVariant(msg.mti)} label={`MTI ${msg.mti}`} />
          <span className="inline-flex items-center gap-1 text-xs font-medium text-secondary">
            {isInbound ? (
              <ArrowDownLeft className="w-3.5 h-3.5 text-info" />
            ) : (
              <ArrowUpRight className="w-3.5 h-3.5 text-accent" />
            )}
            {msg.direction}
          </span>
          {msg.response_code && (
            <Badge
              variant={msg.response_code === "00" ? "success" : "error"}
              label={`DE 39 = ${msg.response_code}`}
            />
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs font-mono text-secondary tabular-nums">
            {formatTimeShort(msg.created_at)}
          </span>
          <CopyRawButton raw={msg.raw_iso_snippet} />
        </div>
      </div>

      <div className="p-3.5 space-y-3">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          {elements.map((el) => (
            <div
              key={el.de}
              className={`px-2.5 py-1.5 rounded border border-border/80 bg-muted/40 ${
                el.de === "DE 90" ? "sm:col-span-2" : ""
              }`}
            >
              <div className="flex items-center justify-between gap-2 text-[11px] text-secondary">
                <span className="font-mono font-semibold text-accent">{el.de}</span>
                <span className="truncate">{el.name}</span>
              </div>
              <div className="mt-0.5 font-mono text-xs text-primary break-all tabular-nums">
                {el.value}
              </div>
            </div>
          ))}
        </div>

        <div className="wire-frame-box px-3 py-2.5 text-xs overflow-x-auto leading-relaxed">
          <div className="text-[10px] uppercase tracking-wider text-slate-400 mb-1">
            Raw ASCII ISO 8583 Wire Frame (MTI + 128-bit Hex Bitmap + Packed Fields)
          </div>
          <div className="break-all">
            <span className="text-indigo-300 font-semibold">{mtiPart}</span>
            <span className="text-emerald-300">{bitmapPart}</span>
            <span className="text-slate-200">{fieldsPart}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

export function IsoWireInspector({
  transactions,
  selectedTxId,
  onSelectTx,
  expanded = false,
}: IsoWireInspectorProps) {
  const [inspectorTab, setInspectorTab] = useState<string>("iso");

  const activeTx = useMemo(() => {
    if (transactions.length === 0) return null;
    if (selectedTxId) {
      const found = transactions.find((t) => t.id === selectedTxId);
      if (found) return found;
    }
    return transactions[0];
  }, [transactions, selectedTxId]);

  return (
    <Card padding={5} elevation="low" className="w-full">
      <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
        <div>
          <Heading level={4} weight="semibold">
            Real-Time ISO 8583 Wire Trace &amp; Switch State
          </Heading>
          <Text type="supporting" display="block">
            Live protocol frames (0200 → 0210 / 0420 → 0430) and state machine audit
          </Text>
        </div>
        <div className="flex items-center gap-2">
          <StatusDot variant="success" label="Live wire feed" isPulsing />
          <Text type="supporting" className="tabular-nums">
            {transactions.length} transactions
          </Text>
        </div>
      </div>

      {transactions.length === 0 ? (
        <div className="py-12 px-6 text-center rounded-lg border border-dashed border-border bg-muted/40">
          <Terminal className="w-7 h-7 mx-auto mb-2.5 text-secondary" />
          <Heading level={5} weight="semibold">
            No ISO 8583 traffic recorded yet
          </Heading>
          <Text type="supporting" display="block" className="mt-1">
            Dispatch a transfer on the left to inspect canonical MTI 0200 / 0210 / 0420 / 0430 wire
            frames and double-entry clearing legs.
          </Text>
        </div>
      ) : (
        <div className={`grid grid-cols-1 ${expanded ? "xl:grid-cols-12" : "lg:grid-cols-12"} gap-4`}>
          {/* Transaction List */}
          <div
            className={`${expanded ? "xl:col-span-4" : "lg:col-span-5"} space-y-2 max-h-[620px] overflow-y-auto pr-1`}
          >
            {transactions.map((tx) => {
              const isSelected = activeTx?.id === tx.id;
              const uniqueMtis = Array.from(new Set(tx.iso_messages.map((m) => m.mti)));
              return (
                <button
                  key={tx.id}
                  type="button"
                  onClick={() => onSelectTx(tx.id)}
                  className={`w-full text-left p-3 rounded-lg border transition-all cursor-pointer ${
                    isSelected
                      ? "border-accent bg-accent-muted/40 shadow-xs"
                      : "border-border bg-surface hover:bg-muted/70"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className="font-mono text-xs font-semibold text-primary shrink-0">
                        STAN {tx.stan}
                      </span>
                      <span className="text-xs text-secondary truncate">
                        {tx.originator_code} → {tx.beneficiary_code}
                      </span>
                    </div>
                    <div className="shrink-0">
                      <Badge variant={statusBadgeVariant(tx.status)} label={tx.status} />
                    </div>
                  </div>

                  <div className="mt-2 flex items-center justify-between gap-2">
                    <span className="text-sm font-semibold text-primary tabular-nums">
                      {formatMzn(tx.amount_centavos)}
                    </span>
                    <div className="flex items-center gap-1.5">
                      {tx.status === "ROUTED" ? (
                        <SlaCountdown deadlineIso={tx.sla_deadline_at} />
                      ) : (
                        tx.response_code && (
                          <span className="font-mono text-[11px] px-1.5 py-0.5 rounded bg-muted border border-border text-secondary">
                            DE39={tx.response_code}
                          </span>
                        )
                      )}
                    </div>
                  </div>

                  <div className="mt-2 flex items-center justify-between gap-2 text-[11px] text-secondary">
                    <span className="font-mono truncate">
                      {uniqueMtis.join(" → ") || "INIT"}
                    </span>
                    <span className="font-mono tabular-nums shrink-0">
                      {formatTimeShort(tx.created_at)}
                    </span>
                  </div>
                </button>
              );
            })}
          </div>

          {/* Selected Transaction Inspector */}
          <div className={`${expanded ? "xl:col-span-8" : "lg:col-span-7"} min-w-0`}>
            {activeTx && (
              <div className="rounded-lg border border-border bg-muted/25 p-4 space-y-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <Heading level={5} weight="semibold" className="tabular-nums">
                        {formatMzn(activeTx.amount_centavos)}
                      </Heading>
                      <Badge
                        variant={statusBadgeVariant(activeTx.status)}
                        label={activeTx.status}
                      />
                      {activeTx.response_code && (
                        <Badge
                          variant={activeTx.response_code === "00" ? "success" : "error"}
                          label={`DE 39 = ${activeTx.response_code}`}
                        />
                      )}
                      {activeTx.status === "ROUTED" && (
                        <SlaCountdown deadlineIso={activeTx.sla_deadline_at} />
                      )}
                    </div>
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-secondary">
                      <span>
                        STAN <Code>{activeTx.stan}</Code>
                      </span>
                      <span>
                        RRN <Code>{activeTx.rrn}</Code>
                      </span>
                      {activeTx.auth_id && (
                        <span>
                          Auth <Code>{activeTx.auth_id}</Code>
                        </span>
                      )}
                      {activeTx.originator_hold_ref && (
                        <span>
                          CBS Hold <Code>{activeTx.originator_hold_ref}</Code>
                        </span>
                      )}
                      {activeTx.fraud_score > 0 && (
                        <span>
                          Risk <Code>{activeTx.fraud_score}/100</Code>
                        </span>
                      )}
                      {activeTx.processing_latency_ms != null && (
                        <span>
                          RTT <Code>{activeTx.processing_latency_ms}ms</Code>
                        </span>
                      )}
                      <span className="truncate max-w-[260px]">
                        Key <Code>{activeTx.idempotency_key}</Code>
                      </span>
                    </div>
                    {activeTx.failure_reason && (
                      <div className="text-xs text-error font-medium pt-0.5">
                        {activeTx.failure_reason}
                      </div>
                    )}
                  </div>

                  <SegmentedControl
                    label="Inspector view"
                    value={inspectorTab}
                    onChange={setInspectorTab}
                    size="sm"
                  >
                    <SegmentedControlItem
                      value="iso"
                      label={`ISO Frames (${activeTx.iso_messages.length})`}
                    />
                    <SegmentedControlItem
                      value="ledger"
                      label={`Journal (${activeTx.journal_entries.length})`}
                    />
                  </SegmentedControl>
                </div>

                <Divider />

                {inspectorTab === "iso" ? (
                  <div className="space-y-3 max-h-[500px] overflow-y-auto pr-1">
                    {activeTx.iso_messages.map((msg) => (
                      <IsoFrameCard key={msg.id} msg={msg} />
                    ))}
                  </div>
                ) : (
                  <div className="space-y-3">
                    {activeTx.journal_entries.length === 0 ? (
                      <div className="p-6 text-center rounded-md border border-dashed border-border bg-surface">
                        <Layers className="w-5 h-5 mx-auto mb-1.5 text-secondary" />
                        <Text type="supporting">
                          No journal postings recorded (transaction declined before ledger clearing
                          hold).
                        </Text>
                      </div>
                    ) : (
                      activeTx.journal_entries.map((entry) => {
                        const totalDr = entry.postings
                          .filter((p) => p.direction === "DEBIT")
                          .reduce((acc, p) => acc + p.amount_centavos, 0);
                        const totalCr = entry.postings
                          .filter((p) => p.direction === "CREDIT")
                          .reduce((acc, p) => acc + p.amount_centavos, 0);

                        return (
                          <div
                            key={entry.id}
                            className="rounded-lg border border-border bg-surface overflow-hidden"
                          >
                            <div className="flex items-center justify-between gap-2 px-3.5 py-2 bg-muted/70 border-b border-border">
                              <div className="flex items-center gap-2">
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
                                <span className="text-xs font-medium text-primary">
                                  {entry.description}
                                </span>
                              </div>
                              <Badge
                                variant={totalDr === totalCr ? "success" : "error"}
                                label={totalDr === totalCr ? "DR == CR" : "UNBALANCED"}
                              />
                            </div>
                            <table className="w-full text-xs">
                              <thead>
                                <tr className="border-b border-border text-secondary bg-muted/30">
                                  <th className="text-left font-medium px-3.5 py-2">Account</th>
                                  <th className="text-left font-medium px-3.5 py-2">Institution</th>
                                  <th className="text-right font-medium px-3.5 py-2">DEBIT</th>
                                  <th className="text-right font-medium px-3.5 py-2">CREDIT</th>
                                </tr>
                              </thead>
                              <tbody className="divide-y divide-border/70">
                                {entry.postings.map((p) => (
                                  <tr key={p.id}>
                                    <td className="px-3.5 py-2 font-mono text-primary">
                                      {p.account_number ?? p.account_id}
                                    </td>
                                    <td className="px-3.5 py-2 text-secondary">
                                      {p.institution_code ?? "--"}
                                    </td>
                                    <td className="px-3.5 py-2 text-right font-mono tabular-nums text-error">
                                      {p.direction === "DEBIT"
                                        ? formatMzn(p.amount_centavos)
                                        : "—"}
                                    </td>
                                    <td className="px-3.5 py-2 text-right font-mono tabular-nums text-success">
                                      {p.direction === "CREDIT"
                                        ? formatMzn(p.amount_centavos)
                                        : "—"}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        );
                      })
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}
