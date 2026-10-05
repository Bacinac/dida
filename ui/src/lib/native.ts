export type NativeLocationIdentity = {
  provisioned: boolean;
  userId: string | null;
  origin: string | null;
};

export function nativeMatches(
  status: NativeLocationIdentity | null, userId: string | undefined, origin: string,
): boolean {
  return !!status?.provisioned && userId !== undefined && status.userId === userId && status.origin === origin;
}

type NativeTransport = {
  postMessage(message: string): void;
  onmessage: ((event: { data: string }) => void) | null;
};

export type NativeStatus = NativeLocationIdentity & {
  version: string;
  fineLocation: boolean;
  backgroundLocation: boolean;
  batteryExempt: boolean;
  zones: number;
};

export class NativeClient {
  private sequence = 0;
  private pending = new Map<number, { resolve: (value: unknown) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }>();

  constructor(private transport: NativeTransport) {
    transport.onmessage = (event) => {
      try {
        const reply = JSON.parse(event.data);
        const entry = this.pending.get(reply.id);
        if (!entry) return;
        this.pending.delete(reply.id);
        clearTimeout(entry.timer);
        if (reply.error) entry.reject(new Error(reply.error));
        else entry.resolve(reply.result);
      } catch {
        for (const entry of this.pending.values()) {
          clearTimeout(entry.timer);
          entry.reject(new Error("Invalid native response"));
        }
        this.pending.clear();
      }
    };
  }

  private call<T>(method: string, args: Record<string, string> = {}): Promise<T> {
    return new Promise((resolve, reject) => {
      const id = ++this.sequence;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("Native request timed out"));
      }, 10_000);
      this.pending.set(id, { resolve: (value) => resolve(value as T), reject, timer });
      try {
        this.transport.postMessage(JSON.stringify({ id, method, args }));
      } catch (error) {
        this.pending.delete(id);
        clearTimeout(timer);
        reject(error);
      }
    });
  }

  appVersion = () => this.call<string>("appVersion");
  status = () => this.call<NativeStatus>("status");
  startLocationSetup = () => this.call<void>("startLocationSetup");
  scanQr = () => this.call<void>("scanQr");
  checkUpdate = () => this.call<void>("checkUpdate");
  setLocale = (tag: string) => this.call<void>("setLocale", { tag });
  syncIdentity = (userId: string, origin: string) => this.call<void>("syncIdentity", { userId, origin });
}

let client: NativeClient | null = null;

export function nativeBridge(): NativeClient | null {
  if (typeof window === "undefined" || window.top !== window) return null;
  const transport = (window as Window & { DidaNative?: NativeTransport }).DidaNative;
  if (!transport) return null;
  client ??= new NativeClient(transport);
  return client;
}
