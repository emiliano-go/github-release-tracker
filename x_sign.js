// OAuth 1.0a signer, verified against the X/Twitter published test vector.
// The same functions are embedded in the workflow's "Sign X Post" Code node.

function percentEncode(value) {
  return encodeURIComponent(String(value)).replace(/[!'()*]/g, (c) =>
    "%" + c.charCodeAt(0).toString(16).toUpperCase()
  );
}

function rotl(n, s) {
  return ((n << s) | (n >>> (32 - s))) >>> 0;
}

// SHA-1 over a byte array, returning 20 raw bytes.
function sha1Digest(bytes) {
  const bitLen = bytes.length * 8;
  const buf = bytes.slice();
  buf.push(0x80);
  while (buf.length % 64 !== 56) buf.push(0);
  const hi = Math.floor(bitLen / 0x100000000);
  const lo = bitLen >>> 0;
  buf.push((hi >>> 24) & 255, (hi >>> 16) & 255, (hi >>> 8) & 255, hi & 255);
  buf.push((lo >>> 24) & 255, (lo >>> 16) & 255, (lo >>> 8) & 255, lo & 255);

  let h0 = 0x67452301, h1 = 0xefcdab89, h2 = 0x98badcfe, h3 = 0x10325476, h4 = 0xc3d2e1f0;
  const w = new Array(80);

  for (let offset = 0; offset < buf.length; offset += 64) {
    for (let i = 0; i < 16; i++) {
      const j = offset + i * 4;
      w[i] = ((buf[j] << 24) | (buf[j + 1] << 16) | (buf[j + 2] << 8) | buf[j + 3]) >>> 0;
    }
    for (let i = 16; i < 80; i++) w[i] = rotl(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);

    let a = h0, b = h1, c = h2, d = h3, e = h4;
    for (let i = 0; i < 80; i++) {
      let f, k;
      if (i < 20) { f = (b & c) | (~b & d); k = 0x5a827999; }
      else if (i < 40) { f = b ^ c ^ d; k = 0x6ed9eba1; }
      else if (i < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8f1bbcdc; }
      else { f = b ^ c ^ d; k = 0xca62c1d6; }
      const temp = (rotl(a, 5) + (f >>> 0) + e + k + w[i]) >>> 0;
      e = d; d = c; c = rotl(b, 30); b = a; a = temp;
    }
    h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0;
    h3 = (h3 + d) >>> 0; h4 = (h4 + e) >>> 0;
  }

  const out = [];
  for (const h of [h0, h1, h2, h3, h4]) {
    out.push((h >>> 24) & 255, (h >>> 16) & 255, (h >>> 8) & 255, h & 255);
  }
  return out;
}

function hmacSha1Base64(keyString, messageString) {
  const block = 64;
  let key = Array.from(Buffer.from(keyString, "utf8"));
  if (key.length > block) key = sha1Digest(key);
  while (key.length < block) key.push(0);

  const inner = new Array(block);
  const outer = new Array(block);
  for (let i = 0; i < block; i++) {
    inner[i] = key[i] ^ 0x36;
    outer[i] = key[i] ^ 0x5c;
  }

  const innerHash = sha1Digest(inner.concat(Array.from(Buffer.from(messageString, "utf8"))));
  const outerHash = sha1Digest(outer.concat(innerHash));
  return Buffer.from(outerHash).toString("base64");
}

/**
 * Build the OAuth1 Authorization header.
 * @returns {{authorization: string, signatureBase: string, signature: string}}
 */
function signRequest({
  method,
  url,
  consumerKey,
  consumerSecret,
  token,
  tokenSecret,
  nonce,
  timestamp,
  params = {},
}) {
  const oauth = {
    oauth_consumer_key: consumerKey,
    oauth_nonce: nonce,
    oauth_signature_method: "HMAC-SHA1",
    oauth_timestamp: String(timestamp),
    oauth_token: token,
    oauth_version: "1.0",
  };

  const all = { ...oauth, ...params };
  const paramPairs = Object.keys(all)
    .map((k) => [percentEncode(k), percentEncode(all[k])])
    .sort((a, b) => (a[0] === b[0] ? (a[1] < b[1] ? -1 : 1) : a[0] < b[0] ? -1 : 1));

  const paramString = paramPairs.map(([k, v]) => k + "=" + v).join("&");
  const signatureBase = [method.toUpperCase(), percentEncode(url), percentEncode(paramString)].join("&");
  const signingKey = percentEncode(consumerSecret) + "&" + percentEncode(tokenSecret);
  const signature = hmacSha1Base64(signingKey, signatureBase);

  const header = "OAuth " + Object.keys(oauth)
    .map((k) => percentEncode(k) + '="' + percentEncode(oauth[k]) + '"')
    .concat('oauth_signature="' + percentEncode(signature) + '"')
    .join(", ");

  return { authorization: header, signatureBase, signature };
}

module.exports = { percentEncode, sha1Digest, hmacSha1Base64, signRequest };

if (require.main === module) {
  // Published test vector from X's OAuth 1.0a signing documentation.
  const result = signRequest({
    method: "POST",
    url: "https://api.twitter.com/1.1/statuses/update.json",
    consumerKey: "xvz1evFS4wEEPTGEFPHBog",
    consumerSecret: "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
    token: "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
    tokenSecret: "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
    nonce: "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
    timestamp: 1318622958,
    params: {
      status: "Hello Ladies + Gentlemen, a signed OAuth request!",
      include_entities: "true",
    },
  });
  const expected = "hCtSmYh+iHYCEqBWrE7C7hYmtUk=";
  console.log("signature:", result.signature);
  console.log("expected :", expected);
  console.log(result.signature === expected ? "PASS" : "FAIL");
  process.exit(result.signature === expected ? 0 : 1);
}
