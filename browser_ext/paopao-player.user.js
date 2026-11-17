// ==UserScript==
// @name         泡泡桌宠 · 播放进度上报（免开发者模式）
// @namespace    https://github.com/paopao
// @version      1.0.0
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
      post({
        url: location.href,
        title: document.title,
        t: v ? v.currentTime : null,
        d: v ? v.duration : null,
        paused: v ? v.paused : null,
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
