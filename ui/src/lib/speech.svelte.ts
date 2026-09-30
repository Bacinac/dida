// Push-to-talk for the assistant: the browser's own speech recognition in, the
// browser's own synthesis out. No new service, no GPU, no audio leaving the
// device beyond whatever the browser already does for dictation.
//
// It is DELIBERATELY not silent when unavailable. The mic is gated on a secure
// context, and DIDA is reachable two ways: its public https:// address through the
// tunnel (secure — works) and http://<lan-ip>:5273 at home (not secure — the API
// is not even defined). A mic button that simply vanished at home would read as a
// bug in DIDA; `unavailableReason` says which of the two it is so the user, and
// the next reader, knows it is the origin and not the feature.

type Reason = null | "insecure" | "unsupported";

interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((e: { results: { 0: { 0: { transcript: string } } }; }) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
}

function recognitionCtor(): (new () => SpeechRecognitionLike) | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as Record<string, unknown>;
  return (w.SpeechRecognition ?? w.webkitSpeechRecognition) as (new () => SpeechRecognitionLike) | null;
}

const LANG_KEY = "dida.speech.lang";
const TAGS: Record<string, string> = { hr: "hr-HR", en: "en-GB" };

class Speech {
  listening = $state(false);
  /** null = usable. Otherwise WHY not, so the UI can say it instead of hiding. */
  unavailableReason = $state<Reason>(null);
  /** Which language the recogniser listens for — NOT the app's language.
   *
   *  It used to follow the UI locale, and a profile set to English while its owner
   *  speaks Croatian sent every command into an en-GB model, which returned noise.
   *  The two are unrelated now that the assistant replies in the language of the
   *  message: this only decides which acoustic model listens. Default from the
   *  DEVICE (a phone in Croatia is set to Croatian), overridable, remembered. */
  lang = $state("hr");
  private rec: SpeechRecognitionLike | null = null;

  constructor() {
    if (typeof window === "undefined") return;
    // Order matters: an insecure origin is the case we expect at home, and it is
    // the actionable one, so report it ahead of "your browser can't".
    if (!window.isSecureContext) this.unavailableReason = "insecure";
    else if (!recognitionCtor()) this.unavailableReason = "unsupported";

    const saved = localStorage.getItem(LANG_KEY);
    if (saved && saved in TAGS) this.lang = saved;
    else {
      const device = (navigator.language || "").slice(0, 2).toLowerCase();
      this.lang = device in TAGS ? device : "hr";
    }
  }

  setLang(code: string) {
    if (!(code in TAGS)) return;
    this.lang = code;
    try {
      localStorage.setItem(LANG_KEY, code);
    } catch {
      /* private mode — the choice just won't survive the session */
    }
  }

  /** hr -> en -> hr. Two languages, so a toggle beats a menu. */
  nextLang(): string {
    const codes = Object.keys(TAGS);
    return codes[(codes.indexOf(this.lang) + 1) % codes.length];
  }

  get available(): boolean {
    return this.unavailableReason === null;
  }

  /** Listen once and hand the final transcript to `onText`. */
  start(onText: (text: string) => void, onError: (code: string) => void) {
    const Ctor = recognitionCtor();
    if (!Ctor || this.listening) return;
    const rec = new Ctor();
    // The bare "hr" is accepted but recognises poorly; the region tag is what
    // selects the Croatian acoustic model.
    rec.lang = TAGS[this.lang];
    rec.continuous = false;
    rec.interimResults = false;
    rec.onresult = (e) => {
      const text = e.results[0][0].transcript.trim();
      if (text) onText(text);
    };
    rec.onerror = (e) => {
      // "aborted" is the user's own cancel via stop(), not a failure. "no-speech"
      // IS reported: swallowing it made silence look exactly like a dead button.
      if (e.error !== "aborted") onError(e.error);
    };
    rec.onend = () => {
      this.listening = false;
      this.rec = null;
    };
    this.rec = rec;
    this.listening = true;
    rec.start();
  }

  stop() {
    this.rec?.stop();
  }

  /** Speak a reply. Best-effort: a device with no Croatian voice simply stays
   *  quiet rather than reading it in the wrong accent. */
  say(text: string, lang?: string) {
    if (typeof window === "undefined" || !("speechSynthesis" in window)) return;
    const want = lang ?? this.lang;
    const voices = window.speechSynthesis.getVoices();
    const voice = voices.find((v) => v.lang.toLowerCase().startsWith(want));
    if (!voice) return;
    const u = new SpeechSynthesisUtterance(text);
    u.voice = voice;
    u.lang = voice.lang;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(u);
  }

  silence() {
    if (typeof window !== "undefined" && "speechSynthesis" in window) {
      window.speechSynthesis.cancel();
    }
  }
}

export const speech = new Speech();
