"use client";

import { useState } from "react";
import { AlertTriangle, CheckCircle2, Clock, Radio, ShieldAlert, WifiOff } from "lucide-react";
import { Badge } from "@astryxdesign/core/Badge";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Text } from "@astryxdesign/core/Text";

import { updateChaos } from "@/lib/api";
import type { ChaosConfig } from "@/lib/types";

interface ChaosControlPanelProps {
  chaos: ChaosConfig | null;
  onChaosUpdated: (next: ChaosConfig) => void;
}

const PRESETS = [
  {
    id: "normal",
    label: "Normal (200ms)",
    tag: "DE 39 = 00",
    description: "Healthy beneficiary PSP responds with 0210 approval in 200ms.",
    icon: CheckCircle2,
    tone: "success" as const,
  },
  {
    id: "slow_15s",
    label: "Slow Network (15s)",
    tag: "Within 25s SLA",
    description: "Injects 15,000ms latency. Still clears before 25s SPIM SLA deadline.",
    icon: Clock,
    tone: "warning" as const,
  },
  {
    id: "drop_0210",
    label: "Drop 0210 ACK (>25s)",
    tag: "0420 Reversal",
    description: "Beneficiary drops 0210 ACK; SLA sweeper triggers DE 39 = 68 & 0420 reversal.",
    icon: WifiOff,
    tone: "error" as const,
  },
  {
    id: "inoperative_91",
    label: "Issuer Inoperative",
    tag: "DE 39 = 91",
    description: "Beneficiary returns DE 39 = 91; clearing hold reverses immediately.",
    icon: AlertTriangle,
    tone: "error" as const,
  },
  {
    id: "recon_hold",
    label: "Drop 0210 + Fail 0430",
    tag: "RECON_HOLD",
    description: "Drops both 0210 and 0430 to test manual reconciliation hold escalation.",
    icon: ShieldAlert,
    tone: "warning" as const,
  },
];

export function ChaosControlPanel({ chaos, onChaosUpdated }: ChaosControlPanelProps) {
  const [updating, setUpdating] = useState<string | null>(null);
  const currentPreset = chaos?.preset ?? "normal";
  const currentTarget = chaos?.target_beneficiary ?? "ALL";

  async function selectPreset(presetId: string) {
    setUpdating(presetId);
    try {
      const updated = await updateChaos({
        preset: presetId,
        target_beneficiary: currentTarget === "ALL" ? null : currentTarget,
      });
      onChaosUpdated(updated);
    } finally {
      setUpdating(null);
    }
  }

  async function selectTarget(nextTarget: string) {
    setUpdating("scope");
    try {
      const updated = await updateChaos({
        preset: currentPreset,
        target_beneficiary: nextTarget === "ALL" ? null : nextTarget,
      });
      onChaosUpdated(updated);
    } finally {
      setUpdating(null);
    }
  }

  return (
    <Card padding={5} elevation="low" className="w-full">
      <div className="flex items-start justify-between gap-3 mb-4">
        <div className="min-w-0">
          <Heading level={4} weight="semibold">
            Beneficiary PSP Chaos Controls
          </Heading>
          <Text type="supporting" display="block">
            Live Redis fault injection across MPESA_MZ &amp; EMOLA_MZ simulators
          </Text>
        </div>
        <div className="shrink-0">
          <Badge
            variant={currentPreset === "normal" ? "success" : "warning"}
            icon={<Radio className="w-3 h-3" />}
            label={currentPreset === "normal" ? "Healthy" : "Fault Active"}
          />
        </div>
      </div>

      <div className="space-y-4">
        <div>
          <Text type="label" display="block" className="mb-1.5">
            Fault Target Scope
          </Text>
          <SegmentedControl
            label="Fault Target Scope"
            value={currentTarget}
            onChange={selectTarget}
            layout="fill"
            size="sm"
            isDisabled={updating !== null}
          >
            <SegmentedControlItem value="ALL" label="All Beneficiaries" />
            <SegmentedControlItem value="MPESA_MZ" label="MPESA_MZ" />
            <SegmentedControlItem value="EMOLA_MZ" label="EMOLA_MZ" />
          </SegmentedControl>
        </div>

        <div className="space-y-2">
          {PRESETS.map((item) => {
            const Icon = item.icon;
            const active = currentPreset === item.id;
            return (
              <button
                key={item.id}
                type="button"
                disabled={updating !== null}
                onClick={() => selectPreset(item.id)}
                className={`w-full text-left px-3.5 py-2.5 rounded-md border transition-all cursor-pointer ${
                  active
                    ? "border-accent bg-accent-muted/45 shadow-xs"
                    : "border-border bg-surface hover:bg-muted"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <Icon
                      className={`w-4 h-4 shrink-0 ${
                        active ? "text-accent" : "text-secondary"
                      }`}
                    />
                    <span className="text-sm font-medium text-primary truncate">
                      {item.label}
                    </span>
                  </div>
                  <Badge variant={active ? item.tone : "neutral"} label={item.tag} />
                </div>
                <p className="mt-1 pl-6 text-xs text-secondary leading-relaxed">
                  {item.description}
                </p>
              </button>
            );
          })}
        </div>

        {chaos && (
          <div className="grid grid-cols-3 gap-2 pt-2 border-t border-border text-xs">
            <div className="px-2.5 py-1.5 rounded bg-muted">
              <div className="text-secondary">Latency</div>
              <div className="font-mono font-semibold text-primary tabular-nums">
                {chaos.latency_ms} ms
              </div>
            </div>
            <div className="px-2.5 py-1.5 rounded bg-muted">
              <div className="text-secondary">Drop 0210</div>
              <div className="font-mono font-semibold text-primary">
                {chaos.drop_0210 ? "TRUE" : "FALSE"}
              </div>
            </div>
            <div className="px-2.5 py-1.5 rounded bg-muted">
              <div className="text-secondary">Force DE 39</div>
              <div className="font-mono font-semibold text-primary">
                {chaos.force_de39 ?? "00"}
              </div>
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}
