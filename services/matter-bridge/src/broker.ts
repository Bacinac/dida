// The bridge's side of the broker (dida_core.broker is the adapters'): it asks the
// api on `dida.cfg.matter-bridge`, both ways sealed with its own key from the keys
// service, so it needs neither the database nor the root key.
import { createCipheriv, createDecipheriv, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";

import { RequestError, TimeoutError } from "@nats-io/transport-node";

export const PATIENCE_MS = 30_000;

export class BrokerError extends Error {}

export type FernetKey = { sign: Buffer; enc: Buffer };

export function fernetKey(b64: string): FernetKey {
    const raw = Buffer.from(b64.trim(), "base64url");
    if (raw.length !== 32) throw new Error("a Fernet key is 32 bytes");
    return { sign: raw.subarray(0, 16), enc: raw.subarray(16) };
}

// Fernet as the Python side speaks it (cryptography.fernet).
export function seal(key: FernetKey, plain: Buffer, now = Math.floor(Date.now() / 1000), iv = randomBytes(16)): string {
    const ts = Buffer.alloc(8);
    ts.writeBigUInt64BE(BigInt(now));
    const cipher = createCipheriv("aes-128-cbc", key.enc, iv);
    const body = Buffer.concat([Buffer.from([0x80]), ts, iv, cipher.update(plain), cipher.final()]);
    const token = Buffer.concat([body, createHmac("sha256", key.sign).update(body).digest()]);
    // Python's urlsafe_b64decode insists on the padding that Node's base64url drops.
    return token.toString("base64").replace(/\+/g, "-").replace(/\//g, "_");
}

export function open(key: FernetKey, token: string): Buffer {
    const raw = Buffer.from(token, "base64url");
    if (raw.length < 1 + 8 + 16 + 16 + 32 || raw[0] !== 0x80) throw new Error("not a Fernet token");
    const body = raw.subarray(0, raw.length - 32);
    if (!timingSafeEqual(createHmac("sha256", key.sign).update(body).digest(), raw.subarray(raw.length - 32))) {
        throw new Error("the token does not open with this key");
    }
    const decipher = createDecipheriv("aes-128-cbc", key.enc, body.subarray(9, 25));
    return Buffer.concat([decipher.update(body.subarray(25)), decipher.final()]);
}

export function ownKey(path = process.env.DIDA_KEY_FILE ?? "/run/dida-key/key"): FernetKey {
    try {
        return fernetKey(readFileSync(path, "utf8"));
    } catch (e) {
        throw new Error(`client key not readable at ${path} — is the keys service up? (${(e as Error).message})`);
    }
}

type Requester = { request(subject: string, data: Uint8Array, opts: { timeout: number }): Promise<{ data: Uint8Array }> };

// An api restart (deploy) leaves nobody answering for a few seconds: waited out.
function waitable(e: unknown): boolean {
    return e instanceof TimeoutError || (e instanceof RequestError && e.isNoResponders());
}

export class Broker {
    constructor(private nc: Requester, private client: string, private key: FernetKey = ownKey()) {}

    async call<T>(op: string, args: Record<string, unknown> = {}, patience = PATIENCE_MS): Promise<T> {
        const deadline = Date.now() + patience;
        let delay = 500;
        let msg: { data: Uint8Array };
        for (;;) {
            try {
                // Sealed afresh on every try: the api refuses a seal older than 60 s.
                const body = Buffer.from(seal(this.key, Buffer.from(JSON.stringify({ op, args }))));
                msg = await this.nc.request(`dida.cfg.${this.client}`, body, { timeout: 10_000 });
                break;
            } catch (e) {
                if (!waitable(e) || Date.now() + delay > deadline) throw e;
                await new Promise((r) => setTimeout(r, delay));
                delay = Math.min(delay * 2, 5_000);
            }
        }
        const reply = JSON.parse(open(this.key, Buffer.from(msg.data).toString()).toString());
        if ("error" in reply) throw new BrokerError(`${op}: ${reply.error}`);
        return reply.result as T;
    }
}
