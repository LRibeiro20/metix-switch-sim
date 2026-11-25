"use client";

import { useState } from "react";
import {
  Activity,
  Bell,
  CheckCircle2,
  KeyRound,
  Lock,
  ShieldCheck,
} from "lucide-react";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";

import { acknowledgeAlert, verifyAuditChain } from "@/lib/api";
import { formatMzn, formatTimeShort } from "@/lib/format";
import type { AuditLogEntry, OpsTelemetry, SecurityStatus, SystemAlert } from "@/lib/types";

interface SecurityTelemetryPanelProps {
  security: SecurityStatus | null;
  auditLogs: AuditLogEntry[];
  telemetry: OpsTelemetry | null;
  alerts: SystemAlert[];
  currentRole: string;
  onRefresh: () => void;
}

export function SecurityTelemetryPanel({
  security,
  auditLogs,
  telemetry,
  alerts,
  currentRole,
  onRefresh,
}: SecurityTelemetryPanelProps) {
  const [verifying, setVerifying] = useState(false);
  const [verifyBanner, setVerifyBanner] = useState<{
    status: "success" | "error";
    text: string;
  } | null>(null);

  const canAck = currentRole !== "OPS_VIEWER";

  async function handleVerifyChain() {
    setVerifying(true);
    setVerifyBanner(null);
    try {
      const res = await verifyAuditChain();
      setVerifyBanner({
        status: res.valid ? "success" : "error",
        text: res.valid
          ? `Cryptographic SHA-256 chain VERIFIED across ${res.checked_entries} immutable entries (head ${res.head_hash?.slice(0, 16)}...).`
          : `TAMPERING DETECTED at sequence #${res.broken_at_sequence}: ${res.reason}`,
      });
      onRefresh();
    } catch (e) {
      setVerifyBanner({
        status: "error",
        text: e instanceof Error ? e.message : "Chain verification failed",
      });
    } finally {
      setVerifying(false);
    }
  }

  async function handleAckAlert(id: string) {
    await acknowledgeAlert(id);
    onRefresh();
  }

  return (
    <div className="space-y-6">
      {verifyBanner && (
        <Banner
          status={verifyBanner.status}
          title="Immutable Audit Log Hash Chain"
          description={verifyBanner.text}
          isDismissable
          onDismiss={() => setVerifyBanner(null)}
        />
      )}

      {/* Latency, Throughput & Corridor Telemetry */}
      <Card padding={5} elevation="low" className="w-full">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Activity className="w-4 h-4 text-accent" />
            <Heading level={4} weight="semibold">
              Transaction Latency, Throughput &amp; Corridor Analytics
            </Heading>
          </div>
          <Badge variant="info" label={`Sample: ${telemetry?.sample_size ?? 0} tx`} />
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 text-xs mb-4">
          <div className="p-3 rounded-md bg-muted border border-border">
            <div className="text-secondary">Throughput (1m)</div>
            <div className="text-lg font-semibold text-primary font-mono tabular-nums">
              {telemetry?.tps_1m ?? 0} TPS
            </div>
            <div className="text-[11px] text-secondary">{telemetry?.tx_last_1m ?? 0} tx / min</div>
          </div>
          <div className="p-3 rounded-md bg-muted border border-border">
            <div className="text-secondary">p50 Latency</div>
            <div className="text-lg font-semibold text-primary font-mono tabular-nums">
              {telemetry?.latency_p50_ms ?? 0} ms
            </div>
          </div>
          <div className="p-3 rounded-md bg-muted border border-border">
            <div className="text-secondary">p95 Latency</div>
            <div className="text-lg font-semibold text-primary font-mono tabular-nums">
              {telemetry?.latency_p95_ms ?? 0} ms
            </div>
          </div>
          <div className="p-3 rounded-md bg-muted border border-border">
            <div className="text-secondary">p99 Latency</div>
            <div className="text-lg font-semibold text-primary font-mono tabular-nums">
              {telemetry?.latency_p99_ms ?? 0} ms
            </div>
          </div>
          <div className="p-3 rounded-md bg-muted border border-border">
            <div className="text-secondary">Completion Rate</div>
            <div className="text-lg font-semibold text-success font-mono tabular-nums">
              {telemetry?.success_rate_pct ?? 100}%
            </div>
          </div>
        </div>

        {(telemetry?.corridor_breakdown.length ?? 0) > 0 && (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2 text-xs">
            {telemetry!.corridor_breakdown.map((c) => (
              <div
                key={c.corridor}
                className="flex items-center justify-between px-3 py-2 rounded bg-surface border border-border"
              >
                <span className="font-mono font-semibold text-primary">{c.corridor}</span>
                <span className="font-mono text-secondary tabular-nums">
                  {c.count} tx · {formatMzn(c.volume_centavos)}
                </span>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Live System Health & Alerts */}
      <Card padding={5} elevation="low" className="w-full">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Bell className="w-4 h-4 text-accent" />
            <Heading level={4} weight="semibold">
              System Health &amp; Operational Alerts
            </Heading>
          </div>
          <Badge
            variant={(telemetry?.unacknowledged_alerts ?? 0) > 0 ? "error" : "success"}
            label={`${telemetry?.unacknowledged_alerts ?? 0} unacknowledged`}
          />
        </div>

        {alerts.length === 0 ? (
          <div className="py-6 text-center text-xs text-secondary rounded-md border border-dashed border-border bg-muted/40">
            <CheckCircle2 className="w-5 h-5 mx-auto mb-1 text-success" />
            No active system alerts. Fraud blocks, SLA RECON_HOLD escalations, settlement failures,
            and recon exceptions stream here automatically.
          </div>
        ) : (
          <div className="space-y-2">
            {alerts.map((a) => (
              <div
                key={a.id}
                className="flex flex-wrap items-center justify-between gap-2 px-3.5 py-2.5 rounded-md border border-border bg-surface text-xs"
              >
                <div className="space-y-0.5 min-w-0">
                  <div className="flex items-center gap-2">
                    <Badge
                      variant={
                        a.severity === "CRITICAL"
                          ? "error"
                          : a.severity === "WARNING"
                            ? "warning"
                            : "info"
                      }
                      label={`${a.category} · ${a.severity}`}
                    />
                    <span className="font-semibold text-primary">{a.title}</span>
                    <span className="font-mono text-[11px] text-secondary">
                      {formatTimeShort(a.created_at)}
                    </span>
                  </div>
                  <div className="text-secondary">{a.message}</div>
                </div>
                <div>
                  {a.acknowledged ? (
                    <Badge variant="neutral" label={`Ack by ${a.acknowledged_by}`} />
                  ) : (
                    <Button
                      label="Acknowledge"
                      size="sm"
                      variant="secondary"
                      isDisabled={!canAck}
                      onClick={() => handleAckAlert(a.id)}
                    />
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Enterprise Security: mTLS PKI, OAuth 2.0, Ed25519 & Encryption */}
      <Card padding={5} elevation="low" className="w-full">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
          <div>
            <div className="flex items-center gap-2">
              <KeyRound className="w-4 h-4 text-accent" />
              <Heading level={4} weight="semibold">
                Enterprise Security — mTLS X.509 PKI, OAuth 2.0, Ed25519 &amp; AES-256-GCM
              </Heading>
            </div>
            <Text type="supporting" display="block">
              Mutual TLS certificate trust chain, asymmetric payload signing, and encrypted CBS PII
            </Text>
          </div>
          <div className="flex items-center gap-1.5">
            <Badge variant="success" icon={<Lock className="w-3 h-3" />} label="OAuth 2.0 + RBAC" />
            <Badge variant="info" label="Ed25519 Signed" />
          </div>
        </div>

        <div className="overflow-x-auto -mx-5 px-5">
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr className="border-y border-border bg-muted/50 uppercase tracking-wider text-secondary">
                <th className="text-left font-medium py-2 px-3">Service Identity</th>
                <th className="text-left font-medium py-2 px-3">Issuer CA</th>
                <th className="text-left font-medium py-2 px-3">SHA-256 Fingerprint</th>
                <th className="text-left font-medium py-2 px-3">Valid Until</th>
                <th className="text-left font-medium py-2 px-3">Chain Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(security?.certificates ?? []).map((cert) => (
                <tr key={cert.service_name} className="hover:bg-muted/40">
                  <td className="py-2 px-3 font-mono font-semibold text-primary">
                    {cert.subject_cn}
                  </td>
                  <td className="py-2 px-3 font-mono text-secondary">{cert.issuer_cn}</td>
                  <td className="py-2 px-3 font-mono text-[11px] text-secondary">
                    {cert.sha256_fingerprint.slice(0, 35)}...
                  </td>
                  <td className="py-2 px-3 font-mono text-secondary">
                    {cert.not_after.slice(0, 10)}
                  </td>
                  <td className="py-2 px-3">
                    <Badge
                      variant={cert.is_valid_now && cert.ca_chain_verified ? "success" : "error"}
                      label={cert.ca_chain_verified ? "X.509 CA Verified" : "Invalid"}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {/* Hash-Chained Immutable Audit Log */}
      <Card padding={5} elevation="low" className="w-full">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
          <div>
            <div className="flex items-center gap-2">
              <ShieldCheck className="w-4 h-4 text-accent" />
              <Heading level={4} weight="semibold">
                Immutable Hash-Chained Audit Log
              </Heading>
            </div>
            <Text type="supporting" display="block">
              Append-only SHA-256 chained audit trail across OAuth 2.0, settlement, recon, fraud,
              and Maker-Checker decisions
            </Text>
          </div>
          <Button
            label="Verify SHA-256 Chain"
            variant="primary"
            size="sm"
            icon={<ShieldCheck className="w-3.5 h-3.5" />}
            isLoading={verifying}
            onClick={handleVerifyChain}
          />
        </div>

        <div className="overflow-x-auto -mx-5 px-5">
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr className="border-y border-border bg-muted/50 uppercase tracking-wider text-secondary">
                <th className="text-left font-medium py-2 px-3">Seq</th>
                <th className="text-left font-medium py-2 px-3">Time</th>
                <th className="text-left font-medium py-2 px-3">Actor &amp; Role</th>
                <th className="text-left font-medium py-2 px-3">Action</th>
                <th className="text-left font-medium py-2 px-3">Resource</th>
                <th className="text-left font-medium py-2 px-3">SHA-256 Hash</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {auditLogs.map((log) => (
                <tr key={log.id} className="hover:bg-muted/40">
                  <td className="py-2 px-3 font-mono font-semibold text-primary">
                    #{log.sequence_no}
                  </td>
                  <td className="py-2 px-3 font-mono text-secondary whitespace-nowrap">
                    {formatTimeShort(log.created_at)}
                  </td>
                  <td className="py-2 px-3 font-mono">
                    <span className="font-semibold text-primary">{log.actor_id}</span>{" "}
                    <span className="text-secondary">({log.actor_role})</span>
                  </td>
                  <td className="py-2 px-3">
                    <Badge variant="neutral" label={log.action} />
                  </td>
                  <td className="py-2 px-3 font-mono text-secondary max-w-[200px] truncate">
                    {log.resource_type}:{log.resource_id}
                  </td>
                  <td className="py-2 px-3 font-mono text-[11px] text-accent">
                    {log.entry_hash.slice(0, 18)}...
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
