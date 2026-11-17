// ==UserScript==
// @name         泡泡桌宠 · 播放进度上报（免开发者模式）
// @namespace    https://github.com/paopao
// @version      1.0.2
// @description  把当前网页视频的地址/标题/进度 POST 给本机网关（127.0.0.1:9888），只读进度、不碰页面内容。
// @author       paopao
// @match        *://*.bilibili.com/*
// @match        *://*.youtube.com/*
// @match        *://*.iqiyi.com/*
// @match        *://*.youku.com/*
// @match        *://*.mgtv.com/*
// @match        *://v.qq.com/*
// @match        *://*.douyin.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// ==/UserScript==

/*
 * 为什么用它：unpacked 的 browser_ext 是开发人员模式扩展，Edge 更新/策略会把它清掉；
 * Tampermonkey / Violentmonkey / ScriptCat 是商店扩展，不会被那样清。
 * 装好脚本管理器后把这个文件拖进浏览器（或新建脚本粘贴），它就能替代 browser_ext 的
 * content script：每秒把 {url,title,t,d,paused,fullscreen,visible} 发给网关 /player。
 *
 * 网关不需要改：/player 收到后 PlayerWatch 会把它当成 source=ext 的状态。
 */

(() => {
  // 不再只限顶层：B站直播/番剧的播放器可能在 iframe 里。
  // 只上报「真的有 <video> 且 currentTime 是数字」的帧，隐藏 iframe（没有 video）自然被挡掉。
  if (window.__paopaoPlayerReporter) return;
  window.__paopaoPlayerReporter = true;

  const ENDPOINT = 'http://127.0.0.1:9888/player';
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

  const send = () => {
    try {
      const v = document.querySelector('video');
      // 没视频就不报：否则 B站隐藏 iframe / 首页会把「最后一次状态」污染成 header sync
      if (!v || !Number.isFinite(v.currentTime)) return;
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
      });
    } catch (e) {
      /* 页面切换/播放器还没出来时静默，下一秒再试 */
    }
  };

  send();
  setInterval(send, 1000);
})();
