/** D-07: amounts are major units; currency must come from the data contract. */
export function formatMoney(amount: unknown, currency: unknown, decimals: number): string {
  if (decimals !== 0 && decimals !== 2) throw new Error("Zecimale: doar 0 sau 2");
  if (typeof currency !== "string" || !/^[A-Z]{3}$/.test(currency)) throw new Error("Valută neconfigurată");
  if (typeof amount !== "string" && typeof amount !== "number") throw new Error("Sumă invalidă");
  // Decimal strings avoid loss of precision for large values received from APIs.
  let raw = String(amount);
  if (typeof amount === "number" && /e/i.test(raw) && Number.isFinite(amount)) {
    const [coefficient, exponent] = raw.toLowerCase().split("e");
    const negative = coefficient.startsWith("-");
    const unsigned = coefficient.replace("-", "");
    const digits = unsigned.replace(".", "");
    const point = (unsigned.indexOf(".") < 0 ? unsigned.length : unsigned.indexOf(".")) + Number(exponent);
    raw = (negative ? "-" : "") + (point <= 0 ? "0." + "0".repeat(-point) + digits
      : point >= digits.length ? digits + "0".repeat(point - digits.length)
      : digits.slice(0, point) + "." + digits.slice(point));
  }
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(raw);
  if (!match) throw new Error("Sumă invalidă");
  const fraction = match[3] ?? "";
  const scale = BigInt(10) ** BigInt(decimals);
  let units = BigInt(match[2]) * scale + BigInt((fraction + "00").slice(0, decimals) || "0");
  if (Number(fraction[decimals] ?? "0") >= 5) units += BigInt(1);
  const integer = (units / scale).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  const tail = decimals ? "," + (units % scale).toString().padStart(2, "0") : "";
  const label = currency === "RON" ? "lei" : currency === "EUR" ? "€" : currency;
  return `${match[1] && units !== BigInt(0) ? "-" : ""}${integer}${tail} ${label}`;
}

/** Romanian editor input is major units; never round the editable value. */
export function parseMoneyInput(input: string): string | null {
  const raw = input.trim();
  if (!raw) return null;
  if (!/^-?(?:\d+|\d{1,3}(?:\.\d{3})+)(?:,\d+)?$/.test(raw)) {
    throw new Error("Sumă invalidă. Folosește formatul 6.000,25, fără simbol de valută.");
  }
  const canonical = raw.replace(/\./g, "").replace(",", ".");
  const [integer, fraction] = canonical.split(".");
  const negative = integer.startsWith("-");
  const digits = integer.replace("-", "").replace(/^0+(?=\d)/, "");
  return (negative ? "-" : "") + digits + (fraction === undefined ? "" : "." + fraction);
}
export function moneyInputValue(amount: unknown): string {
  if (amount == null || amount === "") return "";
  let raw = String(amount);
  if (typeof amount === "number" && Number.isFinite(amount) && /e/i.test(raw)) {
    const [coefficient, exponent] = raw.toLowerCase().split("e");
    const negative = coefficient.startsWith("-");
    const unsigned = coefficient.replace("-", "");
    const digits = unsigned.replace(".", "");
    const point = (unsigned.indexOf(".") < 0 ? unsigned.length : unsigned.indexOf(".")) + Number(exponent);
    raw = (negative ? "-" : "") + (point <= 0 ? "0." + "0".repeat(-point) + digits
      : point >= digits.length ? digits + "0".repeat(point - digits.length)
      : digits.slice(0, point) + "." + digits.slice(point));
  }
  if (!/^-?\d+(?:\.\d+)?$/.test(raw)) throw new Error("Sumă invalidă");
  const [integer, fraction] = raw.split(".");
  return integer.replace(/\B(?=(\d{3})+(?!\d))/g, ".") + (fraction === undefined ? "" : "," + fraction);
}
/** Legacy JSON-number fields reject precision loss instead of silently changing amounts. */
export function parseMoneyNumber(input: string): number | null {
  const canonical = parseMoneyInput(input);
  if (canonical == null) return null;
  const value = Number(canonical);
  const normalized = (s: string) => s.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "").replace(/^-0$/, "0");
  if (!Number.isFinite(value) || normalized(parseMoneyInput(moneyInputValue(value))!) !== normalized(canonical)) {
    throw new Error("Suma depășește precizia acceptată de acest câmp; nu a fost salvată.");
  }
  return value;
}

/** Empty currency means unknown. Validation is syntactic, not an ISO registry lookup. */
export function moneyCurrency(input: string): string | null {
  const value = input.trim();
  if (!value) return null;
  if (!/^[A-Z]{3}$/.test(value)) throw new Error("Valută invalidă. Folosește un cod de trei litere mari, de exemplu EUR.");
  return value;
}
export function retainerMoneyPatch(amount: string, currency: string, existingStructured = false) {
  const value = parseMoneyInput(amount);
  const code = moneyCurrency(currency);
  if ((value === null) !== (code === null)) throw new Error("Completează atât suma, cât și valuta onorariului, sau golește ambele câmpuri.");
  if (value === null && existingStructured) throw new Error("Ștergerea onorariului structurat nu este disponibilă prin API. Datele existente au fost păstrate.");
  if (value !== null && (value.length > 32 || !/^\d+(?:\.\d{1,6})?$/.test(value))) throw new Error("Onorariul cere o sumă pozitivă sau zero, de maximum 32 caractere și 6 zecimale. Nu a fost salvat.");
  return { retainer_amount: value, retainer_currency: code };
}
