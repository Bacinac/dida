import { NoRespondersError, RequestError, TimeoutError } from "@nats-io/transport-node";
import { describe, expect, it } from "vitest";

import { Broker, BrokerError, fernetKey, open, seal } from "./broker.js";

// Python's side, fixed time and IV: adapter_key("test-root-key-for-the-broker-seal",
// "matter-bridge") sealing {"op":"voice","args":{}} at 1790000000 with IV 00..0f.
const KEY = fernetKey("UDYb3rVLRR5PEdRW76Hb_umFfmw807v8LiEXXC5xwlI=");
const PLAIN = Buffer.from('{"op":"voice","args":{}}');
const PYTHON = "gAAAAABqsTuAAAECAwQFBgcICQoLDA0OD7V9IHNvFJ9HvvLjN9YKYwcubWTjKV7LKSWj63ottI5vJV0DZMey4HF7p8dYJHPrpUZtCAyBcjPEZzEX7wLnpxw=";
const IV = Buffer.from([...Array(16).keys()]);

describe("the seal", () => {
    it("is byte for byte what Python's Fernet makes", () => {
        expect(seal(KEY, PLAIN, 1790000000, IV)).toBe(PYTHON);
    });

    it("opens what Python sealed", () => {
        expect(open(KEY, PYTHON)).toEqual(PLAIN);
    });

    it("does not open with another key or after tampering", () => {
        const other = fernetKey(Buffer.alloc(32, 7).toString("base64url"));
        expect(() => open(other, PYTHON)).toThrow();
        const bent = Buffer.from(PYTHON, "base64url");
        bent[30] ^= 1;
        expect(() => open(KEY, bent.toString("base64url"))).toThrow();
    });
});

function nc(...answers: Array<unknown>) {
    const asked: string[] = [];
    return {
        asked,
        async request(subject: string, data: Uint8Array) {
            asked.push(subject);
            open(KEY, Buffer.from(data).toString());
            const a = answers.shift();
            if (a instanceof Error) throw a;
            return { data: Buffer.from(seal(KEY, Buffer.from(JSON.stringify(a)))) };
        },
    };
}

describe("the client", () => {
    it("asks on its own subject and returns the result", async () => {
        const bus = nc({ result: { covers: 1 } });
        expect(await new Broker(bus, "matter-bridge", KEY).call("voice")).toEqual({ covers: 1 });
        expect(bus.asked).toEqual(["dida.cfg.matter-bridge"]);
    });

    it("takes a refusal as an error, not a retry", async () => {
        const bus = nc({ error: "voice is not open to x" });
        await expect(new Broker(bus, "matter-bridge", KEY).call("voice")).rejects.toThrow(BrokerError);
        expect(bus.asked).toHaveLength(1);
    });

    it("waits out an api restart", async () => {
        const nobody = new RequestError("no responders", { cause: new NoRespondersError("dida.cfg.matter-bridge") });
        const bus = nc(nobody, new TimeoutError(), { result: 7 });
        expect(await new Broker(bus, "matter-bridge", KEY).call("voice")).toBe(7);
        expect(bus.asked).toHaveLength(3);
    });

    it("gives up loudly when the api stays away", async () => {
        const bus = nc(new TimeoutError(), new TimeoutError());
        await expect(new Broker(bus, "matter-bridge", KEY).call("voice", {}, 100)).rejects.toThrow(TimeoutError);
    });
});
