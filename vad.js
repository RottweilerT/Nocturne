/* Détecteur de voix humaine pour Nocturne.
   Réimplémentation en JavaScript pur du modèle Silero VAD v5 (16 kHz), licence MIT,
   © Silero Team — https://github.com/snakers4/silero-vad
   Poids : silero_vad_16k.safetensors (≈ 300 000 paramètres), lus une fois puis gardés en mémoire.
   Fonctionne hors ligne : aucun service externe, tout reste sur le téléphone. */
(function (global) {
  "use strict";
  let W = null, loading = null;

  async function load(url) {
    if (W) return W;
    if (loading) return loading;
    loading = (async () => {
      const buf = await (await fetch(url || "silero_vad_16k.safetensors")).arrayBuffer();
      const dv = new DataView(buf);
      const n = Number(dv.getBigUint64(0, true));
      const head = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 8, n)));
      const base = 8 + n, w = {};
      for (const [k, v] of Object.entries(head)) {
        if (k === "__metadata__") continue;
        const [a, b] = v.data_offsets;
        w[k] = new Float32Array(buf.slice(base + a, base + b));
      }
      W = w; return w;
    })();
    try { return await loading; } finally { loading = null; }
  }

  // Conv1d : entrée [cin][L], poids [cout][cin][k], padding p, pas s
  function conv1d(x, cin, L, w, b, cout, k, s, p) {
    const Lout = Math.floor((L + 2 * p - k) / s) + 1, y = new Float32Array(cout * Lout);
    for (let o = 0; o < cout; o++) for (let t = 0; t < Lout; t++) {
      let acc = b ? b[o] : 0; const t0 = t * s - p;
      for (let c = 0; c < cin; c++) {
        const wo = (o * cin + c) * k, xo = c * L;
        for (let j = 0; j < k; j++) { const ti = t0 + j; if (ti >= 0 && ti < L) acc += w[wo + j] * x[xo + ti]; }
      }
      y[o * Lout + t] = acc;
    }
    return { y, L: Lout };
  }
  const relu = a => { for (let i = 0; i < a.length; i++) if (a[i] < 0) a[i] = 0; return a; };
  const sig = v => 1 / (1 + Math.exp(-v));

  // Un pas : 64 échantillons de contexte + 512 nouveaux (32 ms à 16 kHz) → probabilité de voix
  function step(chunk, st) {
    const w = W, N = chunk.length, x = new Float32Array(N + 64);
    x.set(chunk);
    for (let i = 0; i < 64; i++) x[N + i] = chunk[N - 2 - i];          // pad « reflect » à droite
    const st1 = conv1d(x, 1, x.length, w["stft_conv.weight"], null, 258, 256, 128, 0);
    const T = st1.L, mag = new Float32Array(129 * T);
    for (let f = 0; f < 129; f++) for (let t = 0; t < T; t++) {
      const re = st1.y[f * T + t], im = st1.y[(f + 129) * T + t]; mag[f * T + t] = Math.sqrt(re * re + im * im);
    }
    let r = conv1d(mag, 129, T, w["conv1.weight"], w["conv1.bias"], 128, 3, 1, 1); relu(r.y);
    r = conv1d(r.y, 128, r.L, w["conv2.weight"], w["conv2.bias"], 64, 3, 2, 1); relu(r.y);
    r = conv1d(r.y, 64, r.L, w["conv3.weight"], w["conv3.bias"], 64, 3, 2, 1); relu(r.y);
    r = conv1d(r.y, 64, r.L, w["conv4.weight"], w["conv4.bias"], 128, 3, 1, 1); relu(r.y);
    const inp = r.y;                                                     // 128 × 1
    const Wi = w["lstm_cell.weight_ih"], Wh = w["lstm_cell.weight_hh"], bi = w["lstm_cell.bias_ih"], bh = w["lstm_cell.bias_hh"];
    const g = new Float32Array(512);
    for (let o = 0; o < 512; o++) {
      let a = bi[o] + bh[o]; const oi = o * 128;
      for (let j = 0; j < 128; j++) a += Wi[oi + j] * inp[j] + Wh[oi + j] * st.h[j];
      g[o] = a;
    }
    const h = new Float32Array(128), c = new Float32Array(128);
    for (let j = 0; j < 128; j++) {
      const ig = sig(g[j]), fg = sig(g[128 + j]), gg = Math.tanh(g[256 + j]), og = sig(g[384 + j]);
      c[j] = fg * st.c[j] + ig * gg; h[j] = og * Math.tanh(c[j]);
    }
    st.h = h; st.c = c;
    const fw = w["final_conv.weight"]; let o = w["final_conv.bias"][0];
    for (let j = 0; j < 128; j++) o += fw[j] * (h[j] > 0 ? h[j] : 0);
    return sig(o);
  }

  // État du modèle, à conserver d'un morceau à l'autre pour analyser un long signal par petits bouts
  const newState = () => ({ h: new Float32Array(128), c: new Float32Array(128), ctx: new Float32Array(64), rest: new Float32Array(0) });

  // Probabilités de voix toutes les 32 ms pour un signal mono à 16 kHz.
  // Avec un état « st », plusieurs appels successifs équivalent à un seul appel sur le signal complet.
  function probs(x16, st) {
    st = st || newState();
    let x = x16;
    if (st.rest.length) { x = new Float32Array(st.rest.length + x16.length); x.set(st.rest); x.set(x16, st.rest.length); }
    const out = []; let i = 0;
    for (; i + 512 <= x.length; i += 512) {
      const chunk = new Float32Array(576); chunk.set(st.ctx); chunk.set(x.subarray(i, i + 512), 64);
      out.push(step(chunk, st)); st.ctx = x.slice(i + 448, i + 512);
    }
    st.rest = x.slice(i);
    return out;
  }

  // Résumé : durée de voix (avec hystérésis comme l'outil officiel), probabilité max, plus long passage
  function summarize(p, thr = 0.5) {
    const neg = thr - 0.15, fr = 0.032; let on = false, run = 0, best = 0, total = 0, mx = 0;
    for (const v of p) {
      if (v > mx) mx = v;
      if (!on && v >= thr) on = true; else if (on && v < neg) on = false;
      if (on) { run++; total++; if (run > best) best = run; } else run = 0;
    }
    return { speechSec: +(total * fr).toFixed(2), longestSec: +(best * fr).toFixed(2), maxProb: +mx.toFixed(3) };
  }

  global.NocturneVAD = { load, probs, newState, summarize, ready: () => !!W };
})(typeof window !== "undefined" ? window : globalThis);
