import { useCallback, useEffect, useRef, useState } from "react";

/** Push-to-talk speech-to-text via the Web Speech API (Chrome/Edge/Safari). */
export function usePushToTalk(onFinal: (text: string) => void) {
  const SR: any = (window as any).SpeechRecognition ?? (window as any).webkitSpeechRecognition;
  const supported = !!SR;
  const rec = useRef<any>(null);
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const buf = useRef("");

  useEffect(() => () => rec.current?.abort?.(), []);

  const start = useCallback(() => {
    if (!supported || listening) return;
    const r = new SR();
    r.lang = "en-US";
    r.interimResults = true;
    r.continuous = true;
    buf.current = "";
    r.onresult = (e: any) => {
      let fin = "", tmp = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) fin += t; else tmp += t;
      }
      buf.current += fin;
      setInterim(buf.current + tmp);
    };
    r.onend = () => {
      setListening(false);
      if (buf.current.trim()) onFinal(buf.current.trim());
      setInterim("");
    };
    rec.current = r;
    r.start();
    setListening(true);
  }, [SR, supported, listening, onFinal]);

  const stop = useCallback(() => rec.current?.stop?.(), []);
  return { supported, listening, interim, start, stop };
}

export function speak(text: string) {
  try {
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 0.9;
    window.speechSynthesis.speak(u);
  } catch { /* TTS unavailable */ }
}

const SPOKEN: Record<string, string> = { K: "king", Q: "queen", R: "rook", B: "bishop", N: "knight" };
export function sanToSpeech(san: string): string {
  if (san.startsWith("O-O-O")) return "castles queenside";
  if (san.startsWith("O-O")) return "castles kingside";
  let s = san.replace(/[+#]/g, "");
  const piece = SPOKEN[s[0]] ?? "";
  if (piece) s = s.slice(1);
  s = s.replace("x", " takes ").replace("=", " promotes to ");
  return `${piece} ${s.split("").join(" ")}${san.includes("+") ? ", check" : ""}`.replace(/\s+/g, " ").trim();
}
