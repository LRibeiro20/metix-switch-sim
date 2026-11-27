"use client";

import { useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  PowerOff,
  Radio,
  RotateCcw,
  ShieldAlert,
  WifiOff,
} from "lucide-react";
import { Badge } from "@astryxdesign/core/Badge";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Text } from "@astryxdesign/core/Text";

import { fetchBankChaos, updateBankChaos } from "@/lib/api";
import type { BankChaosConfig } from "@/lib/types";

interface ChaosControlPanelProps {
  canMutate: boolean;
  onChaosUpdated?: () => void;
}

const PRESETS = [
  {
    id: "normal",
    label: "Normal (60ms)",
    tag: "DE 39 = 00",
    description: "Healthy CBS / Wallet responds with signed 0210 approval.",
    icon: CheckCircle2,
    tone: "success" as const,
  },
  {
    id: "slow_15s",
    label: "Slow CBS (15s)",
    tag: "Within 25s SLA",
    description: "Injects 15s latency inside target CBS; still clears before 25s SLA.",
    icon: Clock,
    tone: "warning" as const,
  },
  {
    id: "drop_0210",
    label: "Drop 0210 ACK (>25s)",
    tag: "0420 Reversal",
    description: "Drops 0210 ACK before CBS credit; SLA sweeper sends 0420 & releases hold.",
    icon: WifiOff,
    tone: "error" as const,
  },
  {
    id: "commit_then_drop_0210",
    label: "Commit Credit + Drop 0210",
    tag: "Lost ACK -> 0420",
    description:
      "Beneficiary CBS commits credit & builds 0210, then drops ACK; 0420 compensates credit once.",
    icon: RotateCcw,
    tone: "error" as const,
  },
  {
    id: "inoperative_91",
    label: "CBS Inoperative",
    tag: "DE 39 = 91",
    description: "Target bank returns DE 39 = 91; clearing & CBS earmark reverse immediately.",
    icon: AlertTriangle,
    tone: "error" as const,
  },
  {
    id: "recon_hold",
    label: "Drop 0210 + Fail 0430",
    tag: "RECON_HOLD",
    description: "Drops both 0210 and 0430 to test RECON_HOLD & Maker-Checker adjustment.",
    icon: ShieldAlert,
    tone: "warning" as const,
  },
  {
    id: "offline",
    label: "CBS Offline Outage",
    tag: "HTTP 503",
    description: "Simulates complete bank simulator downtime for connectivity alerts.",
    icon: PowerOff,
    tone: "error" as const,
  },
];

export function ChaosControlPanel({ canMutate, onChaosUpdated }: ChaosControlPanelProps) {
  const [targetBank, setTargetBank] = useState<string>("STDB_MZ");
  const [chaos, setChaos] = useState<BankChaosConfig | null>(null);
  const [updating, setUpdating] = useState<string | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    fetchBankChaos(targetBank)
      .then((cfg) => {
        if (active) {
          setChaos(cfg);
          setErrMsg(null);
        }
      })
      .catch((e) => {
        if (active) setErrMsg(e instanceof Error ? e.message : "Unable to load CBS chaos state");
      });
    return () => {
      active = false;
    };
  }, [targetBank]);

  async function selectPreset(presetId: string) {
    if (!canMutate) return;
    setUpdating(presetId);
    setErrMsg(null);
    try {
      const updated = await updateBankChaos(targetBank, { preset: presetId });
      setChaos(updated);
      onChaosUpdated?.();
    } catch (e) {
      setErrMsg(e instanceof Error ? e.message : "Failed updating chaos state");
    } finally {
      setUpdating(null);
    }
  }

  const currentPreset = chaos?.preset ?? "normal";

  return (
    <Card padding={5} elevation="low" className="w-full">
      <div className="flex items-start justify-between gap-3 mb-4">
        <div className="min-w-0">
          <Heading level={4} weight="semibold">
            Per-Bank CBS Chaos Injection
          </Heading>
          <Text type="supporting" display="block">
            Signed adapter fault controls across independent CBS &amp; wallet services
          </Text>
        </div>
        <div className="shrink-0">
          <Badge
            variant={currentPreset === "normal" ? "success" : "warning"}
            icon={<Radio className="w-3 h-3" />}
            label={currentPreset === "normal" ? "Healthy" : `${targetBank}: ${currentPreset}`}
          />
        </div>
      </div>

      <div className="space-y-4">
        <div>
          <Text type="label" display="block" className="mb-1.5">
            Target Participant Simulator
          </Text>
          <SegmentedControl
            label="Target Participant Simulator"
            value={targetBank}
            onChange={setTargetBank}
            layout="fill"
            size="sm"
            isDisabled={updating !== null}
          >
            <SegmentedControlItem value="BCI_MZ" label="BCI CBS" />
            <SegmentedControlItem value="STDB_MZ" label="STDB CBS" />
            <SegmentedControlItem value="MPESA_MZ" label="M-Pesa" />
            <SegmentedControlItem value="EMOLA_MZ" label="e-Mola" />
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
                disabled={!canMutate || updating !== null}
                onClick={() => selectPreset(item.id)}
                className={`w-full text-left px-3.5 py-2.5 rounded-md border transition-all ${
                  canMutate ? "cursor-pointer" : "cursor-not-allowed opacity-70"
                } ${
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
                    <span className="text-sm font-medium text-primary truncate">{item.label}</span>
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

        {errMsg && <div className="text-xs text-error font-medium">{errMsg}</div>}
      </div>
    </Card>
  );
}
