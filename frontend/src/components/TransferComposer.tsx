"use client";

import { useEffect, useMemo, useState } from "react";
import { ArrowRight, RefreshCw, Send, Zap } from "lucide-react";
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
import type { AccountInfo, SwitchTransaction } from "@/lib/types";

interface TransferComposerProps {
  accounts: AccountInfo[];
  onTransferSettled: (tx: SwitchTransaction, notice?: string) => void;
}

const QUICK_AMOUNTS = [
  { label: "5,000 MT", value: "5000.00" },
  { label: "50,000 MT", value: "50000.00" },
  { label: "150,000 MT", value: "150000.00" },
  { label: "210,000 MT (Cap Test)", value: "210000.00" },
];

export function TransferComposer({ accounts, onTransferSettled }: TransferComposerProps) {
  const originatorAccounts = useMemo(
    () => accounts.filter((a) => a.institution_role === "ORIGINATOR"),
    [accounts]
  );
  const beneficiaryAccounts = useMemo(
    () => accounts.filter((a) => a.institution_role === "BENEFICIARY"),
    [accounts]
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

  useEffect(() => {
    if (!originatorAcct && originatorAccounts.length > 0) {
      setOriginatorAcct(originatorAccounts[0].account_number);
    }
    if (!beneficiaryAcct && beneficiaryAccounts.length > 0) {
      setBeneficiaryAcct(beneficiaryAccounts[0].account_number);
    }
  }, [originatorAccounts, beneficiaryAccounts, originatorAcct, beneficiaryAcct]);

  const selectedOriginator = originatorAccounts.find((a) => a.account_number === originatorAcct);
  const selectedBeneficiary = beneficiaryAccounts.find((a) => a.account_number === beneficiaryAcct);

  const originatorOptions = originatorAccounts.map((a) => ({
    value: a.account_number,
    label: `${a.institution_code} · ${a.holder_name}`,
    description: `${a.account_number} · Bal ${formatMzn(a.balance_centavos)}`,
  }));

  const beneficiaryOptions = beneficiaryAccounts.map((a) => ({
    value: a.account_number,
    label: `${a.institution_code} · ${a.holder_name}`,
    description: `${a.account_number} · Bal ${formatMzn(a.balance_centavos)}`,
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

      const tx = res.transaction;
      if (res.idempotent_replay) {
        setFeedback({
          status: "info",
          title: `Idempotent replay — STAN ${tx.stan}`,
          description: `Redis lock prevented double-debit. Returned original ${tx.status} outcome for key "${tx.idempotency_key}".`,
        });
      } else if (tx.status === "COMPLETED") {
        setFeedback({
          status: "success",
          title: `Settled · DE 39 = 00 · Auth ${tx.auth_id ?? "OK"}`,
          description: `${formatMzn(tx.amount_centavos)} cleared from ${tx.originator_code} to ${tx.beneficiary_code} (STAN ${tx.stan}).`,
        });
        setIdempotencyKey(generateIdempotencyKey());
      } else if (tx.status === "ROUTED") {
        setFeedback({
          status: "warning",
          title: `In-Flight (ROUTED) · Awaiting 0210 ACK`,
          description: `Funds held in METIX_SUSPENSE. If no 0210 arrives within 25s SPIM SLA, the sweeper will dispatch MTI 0420 Reversal Advice.`,
        });
        setIdempotencyKey(generateIdempotencyKey());
      } else {
        setFeedback({
          status: "error",
          title: `${tx.status} · DE 39 = ${tx.response_code ?? "--"}`,
          description:
            tx.failure_reason ||
            tx.response_description ||
            "Transaction declined by switch rules or beneficiary.",
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
      const uniqueTxIds = new Set(settled.map((r) => r.transaction.id));
      const firstTx = settled[0].transaction;

      setFeedback({
        status: uniqueTxIds.size === 1 ? "success" : "error",
        title: `Retry Storm Absorbed: 5 concurrent requests -> ${uniqueTxIds.size} ledger execution`,
        description: `${freshCount} fresh execution, ${replayCount} Redis SET NX idempotent replays on key "${stormKey}" (STAN ${firstTx.stan}, status ${firstTx.status}).`,
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
            Initiate ISO 8583 Transfer
          </Heading>
          <Text type="supporting" display="block">
            Originator PSP dispatch · MTI 0200 · ISO 4217 Currency 943 (MZN)
          </Text>
        </div>
        <div className="shrink-0">
          <Badge variant="info" label="MTI 0200" />
        </div>
      </div>

      <div className="space-y-4">
        <div className="grid grid-cols-1 gap-3">
          <Selector
            label="Originator PSP Account (DE 102)"
            options={originatorOptions}
            value={originatorAcct}
            onChange={setOriginatorAcct}
            placeholder="Select originator account"
            width="100%"
          />

          {selectedOriginator && (
            <div className="flex items-center justify-between px-3 py-2 rounded-md bg-muted border border-border text-xs">
              <div className="flex items-center gap-2">
                <Badge
                  variant={selectedOriginator.account_tier === "BUSINESS" ? "purple" : "neutral"}
                  label={selectedOriginator.account_tier}
                />
                <span className="text-secondary">
                  Daily cap:{" "}
                  <strong className="text-primary tabular-nums">
                    {selectedOriginator.daily_limit_centavos
                      ? formatMzn(selectedOriginator.daily_limit_centavos)
                      : "Unlimited"}
                  </strong>
                </span>
              </div>
              <span className="text-secondary tabular-nums">
                Used today:{" "}
                <strong className="text-primary">
                  {formatMzn(selectedOriginator.daily_used_centavos)}
                </strong>
              </span>
            </div>
          )}

          <div className="flex items-center justify-center py-0.5 text-secondary">
            <div className="flex items-center gap-2 text-xs font-medium px-2.5 py-0.5 rounded-full bg-muted border border-border">
              <span>{selectedOriginator?.institution_code ?? "ORIGINATOR"}</span>
              <ArrowRight className="w-3.5 h-3.5 text-accent" />
              <span>METIX_SWITCH</span>
              <ArrowRight className="w-3.5 h-3.5 text-accent" />
              <span>{selectedBeneficiary?.institution_code ?? "BENEFICIARY"}</span>
            </div>
          </div>

          <Selector
            label="Beneficiary PSP / Mobile Wallet (DE 103)"
            options={beneficiaryOptions}
            value={beneficiaryAcct}
            onChange={setBeneficiaryAcct}
            placeholder="Select beneficiary wallet"
            width="100%"
          />
        </div>

        <div className="space-y-2">
          <TextInput
            label="Amount in MZN (DE 4 — converted to integer centavos)"
            value={amountInput}
            onChange={setAmountInput}
            placeholder="5000.00"
            width="100%"
            description={
              parsedPreview
                ? `Wire DE 4 minor units: ${String(parsedPreview).padStart(12, "0")} (${parsedPreview.toLocaleString()} centavos)`
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

        <Divider />

        <div className="flex flex-col sm:flex-row gap-2.5">
          <Button
            label="Send 0200 Transfer"
            variant="primary"
            size="lg"
            width="100%"
            icon={<Send className="w-4 h-4" />}
            isLoading={isSending}
            isDisabled={isStorming || !parsedPreview}
            onClick={handleSingleTransfer}
          />
          <Button
            label="Duplicate Storm (5×)"
            variant="secondary"
            size="lg"
            width="100%"
            icon={<Zap className="w-4 h-4" />}
            isLoading={isStorming}
            isDisabled={isSending || !parsedPreview}
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
