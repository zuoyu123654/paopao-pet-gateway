// ==UserScript==
// @name         泡泡桌宠 · 播放进度 + 标签页音轨
// @namespace    https://github.com/paopao
// @version      1.2.2
// @updateURL    http://127.0.0.1:9888/ext/paopao-player.user.js
// @downloadURL  http://127.0.0.1:9888/ext/paopao-player.user.js
// @description  把当前网页视频的地址/标题/进度 POST 给本机网关（127.0.0.1:9888），并把 <video> 的音轨切成 8 秒一段推上去（蓝牙耳机没有环回时靠它转写字幕）。
// @author       paopao
// @match        *://*.bilibili.com/*
// @match        *://*.youtube.com/*
// @match        *://*.iqiyi.com/*
// @match        *://*.youku.com/*
// @match        *://*.mgtv.com/*
// @match        *://v.qq.com/*
// @match        *://*.douyin.com/*
// @match        *://*.icourse163.org/*
// @match        *://*.xuetangx.com/*
// @match        *://*.chaoxing.com/*
// @match        *://*.zhihuishu.com/*
// @match        *://live.bilibili.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// ==/UserScript==

/*
 * 为什么用它：unpacked 的 browser_ext 是开发人员模式扩展，Edge 更新/策略会把它清掉；
 * Tampermonkey / Violentmonkey / ScriptCat 是商店扩展，不会被那样清。
 * 装好脚本管理器后把这个文件拖进浏览器（或新建脚本粘贴），它就能替代 browser_ext 的
 * content script。
 *
 * 1.1.0：每秒把 {url,title,t,d,paused,fullscreen,visible,<video> 矩形} 发给 /player。
 *
 * 1.2.0（2026-11-18）：顺手把「标签页音轨」也抓了，推给 /tabaudio。
 *   为什么：蓝牙耳机的 WASAPI 环回是哑的（驱动不给数据，实测 RMS 0.0004），
 *   直播/网课就没法转写，只能退回截屏 OCR。而网页音轨是从播放器解码之后直接接出来的，
 *   跟输出设备（有线/蓝牙/HDMI）完全无关，也不用装虚拟声卡。
 *   做法：AudioContext.createMediaElementSource(<video>) → ScriptProcessor 收 PCM
 *   → 降采样到 16k 单声道 → 8 秒一个 WAV → base64 塞 JSON → POST 127.0.0.1:9888/tabaudio。
 *   原声照常从 ac.destination 出来，不会把声音弄哑。
 *   跨域/DRM 的片子 createMediaElementSource 会抛，那就只上报进度（不影响原来的功能）。
 *
 * 想临时关掉音轨抓取：控制台执行 window.__paopaoTabAudio = false
 */

(() => {
  // 不再只限顶层：B站直播/番剧的播放器可能在 iframe 里。
  // 只上报「真的有 <video> 且 currentTime 是数字」的帧，隐藏 iframe（没有 video）自然被挡掉。
  if (window.__paopaoPlayerReporter) return;
  window.__paopaoPlayerReporter = true;

  const ENDPOINT = 'http://127.0.0.1:9888/player';
  const AUDIO_ENDPOINT = 'http://127.0.0.1:9888/tabaudio';
  const TARGET_RATE = 16000;      // ASR 要 16k 单声道
  const CHUNK_SECONDS = 8;        // 和环回那路（asr_chunk_seconds）统一成 8 秒一批
  let busy = false;

  const post = (data) => {
    if (busy) return;
    busy = true;
    const done = () => { busy = false; };
    try {
      GM_xmlhttpRequest({
        method: 'POST',
        url: ENDPOINT,
        headers: { 'Content-Type': 'application/json' },
        data: JSON.stringify(data),
        timeout: 1200,
        onload: done,
        onerror: done,
        ontimeout: done,
      });
    } catch (e) {
      done();
    }
  };

  // ------------------------------------------------------------ 音轨抓取
  let ac = null;
  let srcNode = null;
  let procNode = null;
  let boundVideo = null;
  let acc = [];
  let accLen = 0;
  let sending = false;
  let mutedUntil = 0;   // 网关说「这个视频有 CC，音轨用不上」时安静一会儿
  const bound = new WeakSet();

  const tabOn = () => window.__paopaoTabAudio !== false;

  const bytesToB64 = (bytes) => {
    let s = '';
    const step = 0x8000;
    for (let i = 0; i < bytes.length; i += step) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
    }
    return btoa(s);
  };

  const wavFromFloat = (f32, inRate) => {
    const ratio = (inRate || TARGET_RATE) / TARGET_RATE;
    const outLen = Math.max(1, Math.floor(f32.length / ratio));
    const out = new Int16Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const start = Math.floor(i * ratio);
      const end = Math.min(f32.length, Math.floor((i + 1) * ratio));
      let s = 0;
      for (let j = start; j < end; j++) s += f32[j];
      const n = Math.max(1, end - start);
      let v = s / n;
      if (v > 1) v = 1; else if (v < -1) v = -1;
      out[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }
    const buf = new ArrayBuffer(44 + out.length * 2);
    const dv = new DataView(buf);
    const ws = (o, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
    ws(0, 'RIFF'); dv.setUint32(4, 36 + out.length * 2, true); ws(8, 'WAVEfmt ');
    dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
    dv.setUint32(24, TARGET_RATE, true); dv.setUint32(28, TARGET_RATE * 2, true);
    dv.setUint16(32, 2, true); dv.setUint16(34, 16, true); ws(36, 'data');
    dv.setUint32(40, out.length * 2, true);
    new Uint8Array(buf, 44).set(new Uint8Array(out.buffer));
    return buf;
  };

  const flushAudio = (v) => {
    if (!accLen || sending || !ac) return;
    if (Date.now() < mutedUntil) {   // 网关说这个视频有 CC 字幕，别录了
      acc = [];
      accLen = 0;
      return;
    }
    const total = new Float32Array(accLen);
    let o = 0;
    for (let i = 0; i < acc.length; i++) { total.set(acc[i], o); o += acc[i].length; }
    acc = [];
    accLen = 0;
    // 整段几乎没声（纯音乐过场/静音）就别送 ASR 了：whisper 跑一段要吃掉两个多核，
    // 空跑出来的还都是「谢谢观看」这种幻觉。0.004 ≈ -48dBFS，正常说话远高于它。
    let sumSq = 0;
    for (let i = 0; i < total.length; i++) sumSq += total[i] * total[i];
    const rms = Math.sqrt(sumSq / Math.max(1, total.length));
    if (rms < 0.004) return;
    let wav;
    try { wav = wavFromFloat(total, ac.sampleRate); } catch (e) { return; }
    const t = Number.isFinite(v.currentTime) ? v.currentTime : 0;
    sending = true;
    const done = () => { sending = false; };
    try {
      GM_xmlhttpRequest({
        method: 'POST',
        url: AUDIO_ENDPOINT,
        headers: { 'Content-Type': 'application/json' },
        data: JSON.stringify({
          url: location.href,
          title: document.title,
          t: t,
          at: Date.now() / 1000,
          b64: bytesToB64(new Uint8Array(wav)),
        }),
        timeout: 20000,
        onload: (r) => {
          // 有 CC 字幕的 B站 视频：网关回 skip，45 秒内别再录（省 CPU）
          try {
            const j = JSON.parse(r.responseText || '{}');
            if (j && j.state === 'skip') mutedUntil = Date.now() + 45000;
            else if (j && j.state === 'queued') mutedUntil = 0;
          } catch (e) { /* 回包坏了不影响上报 */ }
          done();
        },
        onerror: done,
        ontimeout: done,
      });
    } catch (e) {
      done();
    }
  };

  const ensureGraph = (v) => {
    if (!v || !tabOn() || bound.has(v)) return;
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      if (!ac) ac = new AC();
      if (ac.state === 'suspended') ac.resume().catch(() => {});
      srcNode = ac.createMediaElementSource(v);
      bound.add(v);
      boundVideo = v;
      // 原声照常放：不接 destination 的话这段音轨会被我们截走，老大就听不见了
      srcNode.connect(ac.destination);
      procNode = ac.createScriptProcessor(4096, 1, 1);
      procNode.onaudioprocess = (e) => {
        try {
          if (v.paused || v.ended) return;
          if (document.visibilityState !== 'visible') return;
          const ch = e.inputBuffer.getChannelData(0);
          acc.push(new Float32Array(ch));
          accLen += ch.length;
          if (accLen >= ac.sampleRate * CHUNK_SECONDS) flushAudio(v);
        } catch (err) { /* 音频线程里别抛 */ }
      };
      srcNode.connect(procNode);
      // ScriptProcessor 必须接到 destination 才会被拉起来；用 0 增益，别再出一份声
      const mute = ac.createGain();
      mute.gain.value = 0;
      procNode.connect(mute);
      mute.connect(ac.destination);
    } catch (e) {
      // 跨域媒体/DRM：拿不到音轨，保持只报进度
      window.__paopaoTabAudio = false;
    }
  };

  const send = () => {
    try {
      const v = document.querySelector('video');
      // 没视频就不报：否则 B站隐藏 iframe / 首页会把「最后一次状态」污染成 header sync
      if (!v || !Number.isFinite(v.currentTime)) return;
      ensureGraph(v);
      const r = v.getBoundingClientRect();
      post({
        url: location.href,
        title: document.title,
        referrer: document.referrer || "",
        t: v.currentTime,
        d: Number.isFinite(v.duration) ? v.duration : null,
        paused: v.paused,
        fullscreen: !!document.fullscreenElement,
        visible: document.visibilityState === 'visible',
        at: Date.now() / 1000,
        // 1.1.0：把 <video> 在屏幕上的位置也报上去 —— 网关拿它裁「字幕条」做 OCR，
        // 不然只能截整屏下 1/3，B站侧栏推荐/任务栏全混进来（番剧又没 CC，只能靠 OCR）。
        dpr: window.devicePixelRatio || 1,
        vx: r.left, vy: r.top, vw: r.width, vh: r.height,
        sx: window.screenX || 0, sy: window.screenY || 0,
        iw: window.innerWidth, ih: window.innerHeight,
        ow: window.outerWidth, oh: window.outerHeight,
        // 1.2.0：音轨抓到了没有（没抓到就说明是跨域/DRM，网关那边别等它）
        audio: !!(boundVideo && boundVideo === v && ac && ac.state === 'running'),
      });
    } catch (e) {
      /* 页面切换/播放器还没出来时静默，下一秒再试 */
    }
  };

  send();
  setInterval(send, 1000);
})();
