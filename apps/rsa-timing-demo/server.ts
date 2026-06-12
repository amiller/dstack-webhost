// rsa-sign — a tiny signing service.
// Holds a private exponent d and modulus n. Clients submit a message m and
// get back m^d mod n. Implementation: textbook square-and-multiply.

export default async function handler(
  req: Request,
  ctx: { env: Record<string, string> },
) {
  const url = new URL(req.url);
  const d = BigInt("0x" + (ctx.env.D_HEX || ""));
  const n = BigInt("0x" + (ctx.env.N_HEX || ""));

  if (url.pathname === "/sign") {
    const mHex = url.searchParams.get("m") ?? "1";
    const m = BigInt("0x" + mHex.replace(/^0x/, ""));
    const sig = modPow(m, d, n);
    return Response.json({ sig: sig.toString(16) });
  }
  if (url.pathname === "/info") {
    return Response.json({
      modulus_bits: bitLength(n),
      exponent_bits: bitLength(d),
      hint: "GET /sign?m=<hex>",
    });
  }
  return new Response("rsa-sign\n");
}

// Textbook modular exponentiation via square-and-multiply.
// One squaring per bit of the exponent; one extra multiply per *set* bit.
// This is the canonical implementation. It is correct. It is not constant-time.
function modPow(base: bigint, exp: bigint, mod: bigint): bigint {
  let result = 1n;
  let b = base % mod;
  let e = exp;
  while (e > 0n) {
    if (e & 1n) {
      result = (result * b) % mod;
    }
    b = (b * b) % mod;
    e >>= 1n;
  }
  return result;
}

function bitLength(x: bigint): number {
  let n = 0;
  let y = x;
  while (y > 0n) { n++; y >>= 1n; }
  return n;
}
