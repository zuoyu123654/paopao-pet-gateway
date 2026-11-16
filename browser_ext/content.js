// 每秒读一次页面里的 <video>，把「放到第几秒」丢给后台，再由后台转发给本机网关。
// 只读这几项：地址、标题、进度、总长、暂停、全屏、可见。不动页面、不读内容。
(() => {
  const send = () => {
    try {
      const v = document.querySelector('video');
      const data = {
        url: location.href,
        title: document.title,
        t: v ? v.currentTime : null,
        d: v ? v.duration : null,
        paused: v ? v.paused : null,
        fullscreen: !!document.fullscreenElement,
        visible: document.visibilityState === 'visible',
        at: Date.now() / 1000
      };
      chrome.runtime.sendMessage({ type: 'paopao-player', data });
    } catch (e) {
      /* 扩展被卸载/页面被冻结时静默 */
    }
  };
  send();
  setInterval(send, 1000);
})();
