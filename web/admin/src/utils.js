const _bjFmt = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai',
  dateStyle: 'short', timeStyle: 'medium', hour12: false });

export const bj = (ts) => {
  const t = Date.parse(String(ts).replace(' ', 'T').slice(0, 19) + 'Z');
  if (Number.isNaN(t)) return String(ts || '');
  return _bjFmt.format(t);
};

export const avg = (vs) => {
  const v = vs.filter((x) => x != null);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
};

/** 表头冻结只在 ≥1200px 开：rc-table 的 sticky 把表头/表体拆成两个容器，且仅在设了
    scroll.x 时才联动横向滚动 ⇒ 窄屏（实测 ≤1150）表体横滚时表头不动、列错位。
    窄屏返回 false 退回单表结构（表头跟表体一起滚），行为与加 sticky 之前一致。
    shortcut: 非响应式读取（无 hook），拖窗跨 1200 时下一次渲染/轮询（各页 ≤30s）才生效。 */
const _wideMq = typeof window === 'undefined' ? null : window.matchMedia('(min-width: 1200px)');
export const wideSticky = () => (_wideMq ? _wideMq.matches : true);
