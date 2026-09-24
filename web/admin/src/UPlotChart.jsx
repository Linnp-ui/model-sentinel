import React, { useEffect, useMemo, useRef } from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';

// uPlot wrapper for metrics timeseries (fixed 24h window, wheel/drag zoom).
// series: [{ name, color, points: [{ t (unix s), v }] }]
// Gap semantics: v == null/undefined is a TRUE gap (restart blank tick,
// no latency/L2 samples) and stays disconnected (spanGaps false).
// Callers zero-fill what is DEFINED-but-idle (qps, err rates).
// Zoom state is INFERRED, not event-tracked: uPlot has no setZoom hook and
// drag-zoom runs through an internal closure we cannot intercept. On each
// live refresh we compare the visible span to the data span: full-width
// view follows the window; a narrower view keeps its zoom level, gliding
// right only when pinned to the newest data edge. Double-click resets.
const MIN_SPAN_S = 30;

// 坐标轴标签格式化（OverviewPage / MetricsPage 共用）
export const qpsAxis = (v) => (v == null ? '' : (Math.round(v * 100) / 100).toLocaleString('en-US') + ' req/s');
export const msAxis = (v) => {
  if (v == null) return '';
  if (v === 0) return '0';
  return v >= 1000
    ? (v / 1000).toFixed(v >= 10000 ? 0 : 1).replace(/\.0$/, '') + 's'
    : Math.round(v) + 'ms';
};

// uPlot y 轴标签区默认固定 50px（yAxisOpts.size=50），标签右对齐、距轴线
// 15px（tick 10 + gap 5）向左排布 —— 标签超 ~35px 即在画布左缘被裁掉
// （延迟到万 ms 量级时 "15,000" 已裁）。这里实测最长标签宽度让轴宽自适应。
// 字体须与 uPlot 默认轴字体一致（未覆盖 axis.font）。
const AXIS_FONT = '12px system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", sans-serif';
let _measureCtx = null;
function yAxisSize(u, values) {
  if (!values || !values.length) return 50;
  _measureCtx = _measureCtx || document.createElement('canvas').getContext('2d');
  _measureCtx.font = AXIS_FONT;
  let w = 0;
  for (const s of values) w = Math.max(w, _measureCtx.measureText(String(s)).width);
  return Math.ceil(w) + 18; // 15px(tick+gap) + 3px 缓冲
}

export default function UPlotChart({ series, height = 180, yVal = null }) {
  const host = useRef(null);
  const plot = useRef(null);
  const lastX1 = useRef(0);
  const lastX0 = useRef(0);
  const namesKey = (series || []).map((s) => s.name + '=' + s.color).join('|');
  const data = useMemo(() => {
    if (!series || !series.length || !series[0].points.length) return null;
    return [
      series[0].points.map((p) => p.t),
      ...series.map((s) => s.points.map((p) => p.v)),
    ];
  }, [series]);
  const ready = !!data && data[0].length >= 2; // 1 point = degenerate time scale
  const emptyKey = ready ? 'full' : 'empty';

  useEffect(() => {
    const el = host.current;
    if (!el || !ready) return undefined;
    const wpx = el.clientWidth || 720;
    const opts = {
      width: wpx,
      height,
      legend: { show: true },
      cursor: { show: true },
      series: [
        {},
        ...series.map((s) => ({
          label: s.name,
          stroke: s.color,
          width: 1.5,
          points: { show: false },
          spanGaps: false,
          paths: uPlot.paths.spline(),
        })),
      ],
      axes: [
        {},
        { size: yAxisSize, ...(yVal ? { values: (u, vals) => vals.map((v) => yVal(v)) } : {}) },
      ],
    };
    const u = new uPlot(opts, data, el);
    plot.current = u;
    u.root._uplot = u;
    lastX1.current = data[0][data[0].length - 1];
    const over = el.querySelector('.u-over') || el;
    const onWheel = (e) => {
      e.preventDefault();
      const xs = u.data[0];
      if (!xs || xs.length < 2) return;
      const fullMin = xs[0], fullMax = xs[xs.length - 1];
      const sc = u.scales.x;
      const cx = u.cursor.left != null
        ? u.posToVal(u.cursor.left, 'x')
        : (sc.min + sc.max) / 2;
      const f = e.deltaY > 0 ? 1.3 : 1 / 1.3;
      let nMin = cx - (cx - sc.min) * f;
      let nMax = cx + (sc.max - cx) * f;
      if (nMax - nMin > fullMax - fullMin) { nMin = fullMin; nMax = fullMax; }
      if (nMax - nMin < MIN_SPAN_S) {
        const m = (nMin + nMax) / 2;
        nMin = m - MIN_SPAN_S / 2;
        nMax = m + MIN_SPAN_S / 2;
      }
      if (nMin < fullMin) { nMax += fullMin - nMin; nMin = fullMin; }
      if (nMax > fullMax) { nMin -= nMax - fullMax; nMax = fullMax; }
      u.setScale('x', { min: nMin, max: nMax });
    };
    const onReset = () => {
      const xs = u.data[0];
      if (xs && xs.length >= 2) u.setScale('x', { min: xs[0], max: xs[xs.length - 1] });
    };
    over.addEventListener('wheel', onWheel, { passive: false });
    over.addEventListener('dblclick', onReset);
    const ro = new ResizeObserver(() => {
      const w = el.clientWidth;
      if (w) u.setSize({ width: w, height });
    });
    ro.observe(el);
    return () => {
      over.removeEventListener('wheel', onWheel);
      over.removeEventListener('dblclick', onReset);
      ro.disconnect();
      u.destroy();
      plot.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [namesKey, height, !!yVal, emptyKey]);

  useEffect(() => {
    const u = plot.current;
    if (!u || !data) return;
    const xs = data[0];
    const sc = u.scales.x;
    const x0 = xs[0];
    const x1 = xs[xs.length - 1];
    const prevX1 = lastX1.current || x1;
    const prevX0 = lastX0.current || x0;
    lastX1.current = x1;
    lastX0.current = x0;
    // 时间窗切换（x0 跳变远超实时漂移）→ 回全景，否则图还卡在旧窗口切片、看起来像没同步
    if (Math.abs(x0 - prevX0) > Math.max(300, (sc.max - sc.min) * 0.05)) {
      u.setData(data, true);
      return;
    }
    const drift = Math.max(0, x1 - prevX1);
    const tol = Math.max(2, 2 * drift);
    const span = sc.max - sc.min;
    // heal stale views (visible range older than the whole data window)
    if (sc.max < x0 || sc.min > x1) { u.setData(data, true); return; }
    if (span >= x1 - x0 - tol) { u.setData(data, true); return; }
    u.setData(data, false);
    // pinned to the newest edge -> glide right at the same zoom level
    if (drift > 0 && sc.max >= prevX1 - tol) {
      const nMax = x1;
      const nMin = Math.max(x0, nMax - span);
      u.setScale('x', { min: nMin, max: nMax });
    }
  }, [data]);

  if (!ready) {
    return (
      <div style={{ height, display: 'flex', alignItems: 'center',
        justifyContent: 'center', color: '#bbb', fontSize: 12, background: '#fafafa' }}
        data-testid="metrics-chart">
        暂无数据
      </div>
    );
  }
  return <div ref={host} style={{ width: '100%' }} data-testid="metrics-chart" />;
}
