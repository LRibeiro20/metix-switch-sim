export function formatMzn(centavos: number): string {
  const negative = centavos < 0;
  const abs = Math.abs(centavos);
  const whole = Math.floor(abs / 100);
  const frac = abs % 100;
  const formattedWhole = whole.toLocaleString("en-US");
  const formattedFrac = String(frac).padStart(2, "0");
  return `${negative ? "-" : ""}${formattedWhole}.${formattedFrac} MT`;
}

export function parseMznToCentavos(raw: string): number | null {
  const cleaned = raw.replace(/,/g, "").trim();
  if (!/^\d+(\.\d{1,2})?$/.test(cleaned)) {
    return null;
  }
  const [wholePart, fracPart = ""] = cleaned.split(".");
  const paddedFrac = fracPart.padEnd(2, "0");
  const centavos = Number.parseInt(wholePart, 10) * 100 + Number.parseInt(paddedFrac, 10);
  return Number.isSafeInteger(centavos) && centavos > 0 ? centavos : null;
}

export function formatTimeShort(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function generateIdempotencyKey(prefix = "metix"): string {
  const rand = Math.random().toString(36).slice(2, 10);
  return `${prefix}-${Date.now().toString(36)}-${rand}`;
}
