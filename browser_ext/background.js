// 后台只做一件事：把 content.js 报上来的播放进度 POST 给本机网关。
// 放在后台而不是 content script 里，是为了绕开跨域（本机 9888 ++host_permissions 已授权）。
const GATEWAY = 'http://127.0.0.1:9888/player';

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || msg.type !== 'paopao-player' || !msg.data) return;
  fetch(GATEWAY, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(msg.data)
  }).catch(() => {});
});
