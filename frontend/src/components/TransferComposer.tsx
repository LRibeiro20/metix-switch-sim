"use client";

import { useEffect, useMemo, useState } from "react";
import { ArrowRight, RefreshCw, Send, ShieldAlert, Zap } from "lucide-react";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Divider } from "@astryxdesign/core/Divider";
import { Heading } from "@astryxdesign/core/Heading";
import { Selector } from "@astryxdesign/core/Selector";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";

import { submitTransfer } from "@/lib/api";
import { formatMzn, generateIdempotencyKey, parseMznToCentavos } from "@/lib/format";
import type { BankCustomerAccount, SwitchTransaction } from "@/lib/types";

interface TransferComposerProps {
  bankAccounts: BankCustomerAccount[];
  canInitiate: boolean;
  onTransferSettled: (tx: SwitchTransaction | null) => void;
}

const QUICK_AMOUNTS = [
  { label: "5,000 MT", value: "5000.00" },
  { label: "50,000 MT", value: "50000.00" },
  { label: "150,000 MT", value: "150000.00" },
  { label: "210,000 MT (Cap Test)", value: "210000.00" },
];

export function TransferComposer({
  bankAccounts,
  canInitiate,
  onTransferSettled,
}: TransferComposerProps) {
  // BCI and Standard Bank can initiate bidirectional interbank transfers
  const originatorAccounts = useMemo(
    () => bankAccounts.filter((a) => a.bank_code === "BCI_MZ" || a.bank_code === "STDB_MZ"),
    [bankAccounts]
  );

  const [originatorAcct, setOriginatorAcct] = useState<string>("");
  const [beneficiaryAcct, setBeneficiaryAcct] = useState<string>("");
  const [amountInput, setAmountInput] = useState<string>("5000.00");
  const [idempotencyKey, setIdempotencyKey] = useState<string>(() => generateIdempotencyKey());
  const [isSending, setIsSending] = useState(false);
  const [isStorming, setIsStorming] = useState(false);
  const [feedback, setFeedback] = useState<{
    status: "success" | "warning" | "error" | "info";
    title: string;
    description: string;
  } | null>(null);

  const beneficiaryAccounts = useMemo(
    () => bankAccounts.filter((a) => a.account_number !== originatorAcct),
    [bankAccounts, originatorAcct]
  );

  useEffect(() => {
    if (!originatorAcct && originatorAccounts.length > 0) {
      setOriginatorAcct(originatorAccounts[0].account_number);
    }
  }, [originatorAccounts, originatorAcct]);

  useEffect(() => {
    if (
      (!beneficiaryAcct || beneficiaryAcct === originatorAcct) &&
      beneficiaryAccounts.length > 0
    ) {
      // Default to opposite bank (Standard Bank if originator is BCI)
      const crossBank = beneficiaryAccounts.find(
        (a) => a.bank_code === "STDB_MZ" || a.bank_code === "BCI_MZ"
      );
      setBeneficiaryAcct((crossBank ?? beneficiaryAccounts[0]).account_number);
    }
  }, [beneficiaryAccounts, beneficiaryAcct, originatorAcct]);

  const selectedOriginator = originatorAccounts.find((a) => a.account_number === originatorAcct);
  const selectedBeneficiary = beneficiaryAccounts.find((a) => a.account_number === beneficiaryAcct);
  const isSameBank = Boolean(
    selectedOriginator &&
      selectedBeneficiary &&
      selectedOriginator.bank_code === selectedBeneficiary.bank_code
  );

  const originatorOptions = originatorAccounts.map((a) => ({
    value: a.account_number,
    label: `${a.bank_code} · ${a.holder_name}`,
    description: `${a.account_number} · Avail ${formatMzn(a.available_balance_centavos)} / Ledger ${formatMzn(a.ledger_balance_centavos)}`,
  }));

  const beneficiaryOptions = beneficiaryAccounts.map((a) => ({
    value: a.account_number,
    label: `${a.bank_code} · ${a.holder_name}`,
    description: `${a.account_number} · Avail ${formatMzn(a.available_balance_centavos)} · ${a.status}`,
  }));

  async function handleSingleTransfer() {
    const centavos = parseMznToCentavos(amountInput);
    if (!centavos) {
      setFeedback({
        status: "error",
        title: "Invalid MZN amount",
        description: "Enter a positive amount with up to 2 decimal places (centavos).",
      });
      return;
    }
    if (!originatorAcct || !beneficiaryAcct) return;

    setIsSending(true);
    setFeedback(null);
    try {
      const res = await submitTransfer({
        idempotency_key: idempotencyKey.trim(),
        originator_account_number: originatorAcct,
        beneficiary_account_number: beneficiaryAcct,
        amount_centavos: centavos,
      });

      if (res.routing_rail === "CBS_ON_US" && res.on_us_transfer) {
        const onUs = res.on_us_transfer;
        if (res.idempotent_replay) {
          setFeedback({
            status: "info",
            title: `Idempotent On-Us Replay · ${onUs.bank_code} CBS (${onUs.cbs_ref ?? "DIRECT"})`,
            description: `Direct CBS lock prevented double-debit. Returned original ${onUs.approved ? "POSTED" : "DECLINED"} outcome without METIX/SPIM.`,
          });
        } else if (onUs.approved) {
          setFeedback({
            status: "success",
            title: `Direct CBS Settled · ${onUs.bank_code} On-Us · Auth ${onUs.auth_id ?? "OK"} (${onUs.processing_latency_ms}ms)`,
            description: `${formatMzn(onUs.amount_centavos)} booked internally inside ${onUs.bank_code} (${onUs.cbs_ref}). Bypassed METIX/SPIM, ISO 8583, Suspense & MNS.`,
          });
          setIdempotencyKey(generateIdempotencyKey());
        } else {
          setFeedback({
            status: "error",
            title: `CBS On-Us Declined · Code ${onUs.response_code}`,
            description:
              onUs.reason ||
              `Internal book transfer declined by ${onUs.bank_code} CBS (no METIX/SPIM involved).`,
          });
          setIdempotencyKey(generateIdempotencyKey());
        }
        onTransferSettled(null);
        return;
      }

      const tx = res.transaction;
      if (!tx) {
        throw new Error("Switch returned an empty transaction payload");
      }
      if (res.idempotent_replay) {
        setFeedback({
          status: "info",
          title: `Idempotent replay — STAN ${tx.stan}`,
          description: `Redis lock prevented double-debit. Returned original ${tx.status} outcome for key "${tx.idempotency_key}".`,
        });
      } else if (tx.status === "COMPLETED") {
        setFeedback({
          status: "success",
          title: `Settled · DE 39 = 00 · Auth ${tx.auth_id ?? "OK"} (${tx.processing_latency_ms ?? 0}ms)`,
          description: `${formatMzn(tx.amount_centavos)} cleared ${tx.originator_code} -> ${tx.beneficiary_code} (CBS hold captured, STAN ${tx.stan}).`,
        });
        setIdempotencyKey(generateIdempotencyKey());
      } else if (tx.status === "INITIATED" && tx.fraud_status === "PENDING_REVIEW") {
        setFeedback({
          status: "warning",
          title: `Held in Fraud Review Queue · Risk Score ${tx.fraud_score}/100`,
          description: `STAN ${tx.stan} paused prior to routing. Open Fraud & Risk Monitoring as a Fraud Analyst to Release or Block.`,
        });
        setIdempotencyKey(generateIdempotencyKey());
      } else if (tx.status === "ROUTED") {
        setFeedback({
          status: "warning",
          title: `In-Flight (ROUTED) · Funds Earmarked in ${tx.originator_code} & Suspense`,
          description: `Available balance held (${tx.originator_hold_ref ?? "CBS Hold"}). If no 0210 arrives within 25s SPIM SLA, MTI 0420 releases the hold.`,
        });
        setIdempotencyKey(generateIdempotencyKey());
      } else {
        setFeedback({
          status: "error",
          title: `${tx.status} · DE 39 = ${tx.response_code ?? "--"}`,
          description:
            tx.failure_reason ||
            tx.response_description ||
            "Transaction declined by switch rules, CBS restriction, or beneficiary.",
        });
        setIdempotencyKey(generateIdempotencyKey());
      }

      onTransferSettled(tx);
    } catch (err) {
      setFeedback({
        status: "error",
        title: "Switch dispatch error",
        description: err instanceof Error ? err.message : "Failed to reach METIX switch API.",
      });
    } finally {
      setIsSending(false);
    }
  }

  async function handleDuplicateStorm() {
    const centavos = parseMznToCentavos(amountInput);
    if (!centavos || !originatorAcct || !beneficiaryAcct) return;

    const stormKey = generateIdempotencyKey("storm");
    setIdempotencyKey(stormKey);
    setIsStorming(true);
    setFeedback(null);

    try {
      const requests = Array.from({ length: 5 }, () =>
        submitTransfer({
          idempotency_key: stormKey,
          originator_account_number: originatorAcct,
          beneficiary_account_number: beneficiaryAcct,
          amount_centavos: centavos,
        })
      );
      const settled = await Promise.all(requests);
      const freshCount = settled.filter((r) => !r.idempotent_replay).length;
      const replayCount = settled.filter((r) => r.idempotent_replay).length;

      if (settled[0].routing_rail === "CBS_ON_US" && settled[0].on_us_transfer) {
        const uniqueRefs = new Set(settled.map((r) => r.on_us_transfer?.cbs_ref ?? "DECLINED"));
        const firstOnUs = settled[0].on_us_transfer;
        setFeedback({
          status: uniqueRefs.size === 1 ? "success" : "error",
          title: `On-Us Retry Storm Absorbed: 5 concurrent requests -> ${uniqueRefs.size} Direct CBS execution`,
          description: `${freshCount} fresh execution, ${replayCount} CBS idempotent replays (${firstOnUs.cbs_ref ?? firstOnUs.response_code}, bypassed METIX/SPIM).`,
        });
        setIdempotencyKey(generateIdempotencyKey());
        onTransferSettled(null);
        return;
      }

      const uniqueTxIds = new Set(settled.map((r) => r.transaction?.id).filter(Boolean));
      const firstTx = settled[0].transaction;
      if (!firstTx) {
        throw new Error("Switch returned an empty transaction payload during storm");
      }

      setFeedback({
        status: uniqueTxIds.size === 1 ? "success" : "error",
        title: `Retry Storm Absorbed: 5 concurrent requests -> ${uniqueTxIds.size} CBS & Switch execution`,
        description: `${freshCount} fresh execution, ${replayCount} Redis SET NX idempotent replays (STAN ${firstTx.stan}, status ${firstTx.status}).`,
      });
      setIdempotencyKey(generateIdempotencyKey());
      onTransferSettled(firstTx);
    } catch (err) {
      setFeedback({
        status: "error",
        title: "Duplicate storm error",
        description: err instanceof Error ? err.message : "Storm execution failed.",
      });
    } finally {
      setIsStorming(false);
    }
  }

  const parsedPreview = parseMznToCentavos(amountInput);

  return (
    <Card padding={5} elevation="low" className="w-full">
      <div className="flex items-start justify-between gap-3 mb-4">
        <div className="min-w-0">
          <Heading level={4} weight="semibold">
            {isSameBank ? "Initiate Direct On-Us Bank Transfer" : "Initiate Interbank ISO 8583 Transfer"}
          </Heading>
          <Text type="supporting" display="block">
            {isSameBank
              ? `Same-bank book transfer inside ${selectedOriginator?.bank_code} CBS · Bypasses METIX/SPIM`
              : "Bidirectional BCI ↔ Standard Bank CBS + Optional Wallets · Ed25519 Signed"}
          </Text>
        </div>
        <div className="shrink-0">
          <Badge variant={isSameBank ? "success" : "info"} label={isSameBank ? "ON-US CBS" : "MTI 0200"} />
        </div>
      </div>

      <div className="space-y-4">
        <div className="grid grid-cols-1 gap-3">
          <Selector
            label="Originator CBS Account (DE 102)"
            options={originatorOptions}
            value={originatorAcct}
            onChange={setOriginatorAcct}
            placeholder="Select originator CBS account"
            width="100%"
          />

          {selectedOriginator && (
            <div className="px-3 py-2 rounded-md bg-muted border border-border text-xs space-y-1.5">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-1.5">
                  <Badge
                    variant={selectedOriginator.account_tier === "BUSINESS" ? "purple" : "neutral"}
                    label={selectedOriginator.account_tier}
                  />
                  <Badge
                    variant={selectedOriginator.status === "ACTIVE" ? "success" : "error"}
                    label={selectedOriginator.status}
                  />
                </div>
                <span className="text-secondary tabular-nums">
                  Cap:{" "}
                  <strong className="text-primary">
                    {formatMzn(selectedOriginator.switch_daily_used_centavos)} /{" "}
                    {formatMzn(selectedOriginator.daily_limit_centavos)}
                  </strong>
                </span>
              </div>
              <div className="flex items-center justify-between font-mono text-[11px] text-secondary tabular-nums">
                <span>
                  Available:{" "}
                  <strong className="text-primary">
                    {formatMzn(selectedOriginator.available_balance_centavos)}
                  </strong>
                </span>
                <span>
                  Ledger:{" "}
                  <strong className="text-primary">
                    {formatMzn(selectedOriginator.ledger_balance_centavos)}
                  </strong>
                </span>
                {selectedOriginator.earmarked_centavos > 0 && (
                  <span className="text-warning font-semibold">
                    Held: {formatMzn(selectedOriginator.earmarked_centavos)}
                  </span>
                )}
              </div>
            </div>
          )}

          <div className="flex items-center justify-center py-0.5 text-secondary">
            <div className="flex items-center gap-2 text-xs font-medium px-2.5 py-0.5 rounded-full bg-muted border border-border">
              <span>{selectedOriginator?.bank_code ?? "ORIGINATOR"}</span>
              <ArrowRight className="w-3.5 h-3.5 text-accent" />
              <span className={isSameBank ? "text-success font-semibold" : undefined}>
                {isSameBank ? "DIRECT CBS (ON-US)" : "METIX_SWITCH"}
              </span>
              <ArrowRight className="w-3.5 h-3.5 text-accent" />
              <span>{selectedBeneficiary?.bank_code ?? "BENEFICIARY"}</span>
            </div>
          </div>

          {isSameBank && (
            <Banner
              status="info"
              title="On-Us Intrabank Transfer — METIX / SPIM Not Required"
              description={`Both accounts belong to ${selectedOriginator?.bank_code}. This transfer executes atomically inside ${selectedOriginator?.bank_code}'s Core Banking System with no ISO 8583 messages, no METIX Suspense clearing, no 25s SPIM SLA, and no MNS settlement impact.`}
            />
          )}

          <Selector
            label="Beneficiary CBS / Wallet Account (DE 103)"
            options={beneficiaryOptions}
            value={beneficiaryAcct}
            onChange={setBeneficiaryAcct}
            placeholder="Select beneficiary account"
            width="100%"
          />
        </div>

        <div className="space-y-2">
          <TextInput
            label={isSameBank ? "Amount in MZN (integer centavos)" : "Amount in MZN (DE 4 — integer centavos)"}
            value={amountInput}
            onChange={setAmountInput}
            placeholder="5000.00"
            width="100%"
            description={
              parsedPreview
                ? isSameBank
                  ? `Direct CBS minor units: ${parsedPreview.toLocaleString()} centavos (no ISO 8583 DE 4)`
                  : `Wire DE 4 minor units: ${String(parsedPreview).padStart(12, "0")} (${parsedPreview.toLocaleString()} centavos)`
                : "Enter a valid positive MZN amount"
            }
          />
          <div className="flex flex-wrap gap-1.5">
            {QUICK_AMOUNTS.map((preset) => (
              <button
                key={preset.value}
                type="button"
                onClick={() => setAmountInput(preset.value)}
                className="px-2.5 py-1 text-xs font-medium rounded-md border border-border bg-surface hover:bg-muted text-secondary hover:text-primary transition-colors cursor-pointer tabular-nums"
              >
                {preset.label}
              </button>
            ))}
          </div>
        </div>

        <div className="flex items-end gap-2">
          <div className="flex-1">
            <TextInput
              label="Idempotency Key (Redis SET NX)"
              value={idempotencyKey}
              onChange={setIdempotencyKey}
              width="100%"
              placeholder="metix-..."
            />
          </div>
          <Button
            label="Regenerate key"
            variant="secondary"
            isIconOnly
            icon={<RefreshCw className="w-3.5 h-3.5" />}
            tooltip="Generate fresh idempotency key"
            onClick={() => setIdempotencyKey(generateIdempotencyKey())}
          />
        </div>

        {!canInitiate && (
          <div className="flex items-center gap-2 text-xs text-warning bg-warning-muted px-3 py-2 rounded-md">
            <ShieldAlert className="w-4 h-4 shrink-0" />
            <span>
              Current RBAC persona is read-only for transfer initiation. Switch to Maker or Admin.
            </span>
          </div>
        )}

        <Divider />

        <div className="flex flex-col sm:flex-row gap-2.5">
          <Button
            label={isSameBank ? "Send Direct CBS Transfer" : "Send 0200 Transfer"}
            variant="primary"
            size="lg"
            width="100%"
            icon={<Send className="w-4 h-4" />}
            isLoading={isSending}
            isDisabled={!canInitiate || isStorming || !parsedPreview}
            onClick={handleSingleTransfer}
          />
          <Button
            label="Duplicate Storm (5×)"
            variant="secondary"
            size="lg"
            width="100%"
            icon={<Zap className="w-4 h-4" />}
            isLoading={isStorming}
            isDisabled={!canInitiate || isSending || !parsedPreview}
            tooltip="Fires 5 concurrent POST /api/v1/transfers with identical idempotency_key"
            onClick={handleDuplicateStorm}
          />
        </div>

        {feedback && (
          <Banner
            status={feedback.status}
            title={feedback.title}
            description={feedback.description}
            isDismissable
            onDismiss={() => setFeedback(null)}
          />
        )}
      </div>
    </Card>
  );
}
