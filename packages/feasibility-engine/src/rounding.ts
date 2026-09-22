/**
 * Rounding shared with the Python engine.
 *
 * Both engines compute in IEEE-754 doubles in the same operation order, so the raw results are
 * bit-identical. Outputs are then rounded *half away from zero on the shortest decimal
 * representation of the double* (`Number#toString` here, `repr()` in Python), which is what a
 * person reproducing the figures by hand from the decimal inputs expects (1.005 → 1.01), and is
 * identical on both sides. Rounding is applied to outputs only, never to intermediate values.
 */

export const DECIMALS = { m2: 2, EUR: 2, "%": 2 } as const;

interface DecimalParts {
  negative: boolean;
  /** Significant digits without a decimal point, no leading zeros (may be "0"). */
  digits: bigint;
  /** value = digits × 10^exponent */
  exponent: number;
}

/** Parse the shortest round-trip representation, including exponent forms such as 1.5e-7. */
function decimalParts(value: number): DecimalParts {
  const text = value.toString();
  const negative = text.startsWith("-");
  const unsigned = negative ? text.slice(1) : text;
  const [mantissa, exponentText] = unsigned.split("e");
  const [integerPart, fractionPart = ""] = (mantissa as string).split(".");
  const digits = BigInt(`${integerPart}${fractionPart}`);
  const exponent = (exponentText === undefined ? 0 : Number(exponentText)) - fractionPart.length;
  return { negative, digits, exponent };
}

/** Round `value` to `decimals` places, half away from zero on its decimal representation. */
export function roundHalfAwayFromZero(value: number, decimals: number): number {
  if (!Number.isFinite(value)) {
    throw new RangeError(`cannot round a non-finite number: ${String(value)}`);
  }
  const { negative, digits, exponent } = decimalParts(value);
  if (digits === 0n) {
    return 0; // also normalises -0
  }
  const drop = -exponent - decimals; // digits to remove from the right
  let kept: bigint;
  let keptExponent: number;
  if (drop <= 0) {
    kept = digits; // already at most `decimals` places: exact
    keptExponent = exponent;
  } else {
    const divisor = 10n ** BigInt(drop);
    const quotient = digits / divisor;
    const remainder = digits % divisor;
    kept = remainder * 2n >= divisor ? quotient + 1n : quotient;
    keptExponent = -decimals;
  }
  if (kept === 0n) {
    return 0;
  }
  // Build the decimal string and let the parser produce the correctly rounded double.
  return Number(`${negative ? "-" : ""}${kept.toString()}e${keptExponent}`);
}
