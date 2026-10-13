import { useEffect, useMemo, useRef } from 'react';
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

// x 轴时间刻度：本仓所有 x 值都是 unix 秒（环 t / stats_chart / 事件时间线），
// 按北京时间渲染（2026-10-12 起系统展示口径统一北京）；格式化器只建一次（Intl 构造 ms 级）
let _bjAxisFmt = null;
export const bjAxis = (v) => {
  if (typeof v !== 'number' || v <= 0) return '';
  _bjAxisFmt = _bjAxisFmt || new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai',
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  return _bjAxisFmt.format(v * 1000);
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

// uPlot 1.6.32 默认点构建器不支持函数式 points.size：
// rad = (size - width)/2 中 size 是函数 → NaN → arc 不画，且 clip 矩形也 NaN
// → 空 clip 裁掉一切，点**静默消失**（cursor 焦点点会解析函数，悬停图例正常，
// 极易误判成"没有数据"）。2026-10-12 延迟分位散点（点大小∝n）全不可见即此坑。
// 自供构建器：逐点解析 size(u, si, i)。仅横向图（本仓所有图 x=时间）。
function sizeDotPaths(u, si, i0, i1) {
  const pts = u.series[si].points;
  const width = pts.width;
  const fill = new Path2D();
  let dia = 0;
  for (let i = i0; i <= i1; i++) {
    const v = u.data[si][i];
    if (v == null) continue;
    const size = typeof pts.size === 'function' ? pts.size(u, si, i) : pts.size;
    const rad = Math.max(0.5, (size - width) / 2);
    if (size > dia) dia = size;
    const x = u.valToPos(u.data[0][i], 'x', true);
    const y = u.valToPos(v, 'y', si, true);
    fill.moveTo(x + rad, y);
    fill.arc(x, y, rad, 0, Math.PI * 2);
  }
  const b = u.bbox;
  const clip = new Path2D();
  clip.rect(b.left - dia, b.top - dia, b.width + dia * 2, b.height + dia * 2);
  return { stroke: fill, fill, clip, band: null, gaps: null, flags: 3 };
}

export default function UPlotChart({ series, height = 180, yVal = null, scales = null, legend = null }) {
  const host = useRef(null);
  const plot = useRef(null);
  const lastX1 = useRef(0);
  const lastX0 = useRef(0);
  // 散点点大小 ∝ 样本数 n 时，size 函数要读"最新"的 n（图不随数据重建，闭包会旧）；
  // nMax 在 render 期算好缓存，size 回调内只做 O(1)
  const nRef = useRef([]);
  const nMaxRef = useRef([]);
  nRef.current = (series || []).map((s) => s.n);
  nMaxRef.current = nRef.current.map((ns) => (ns ? Math.max(1, ...ns) : 1));
  const namesKey = (series || []).map((s) => s.name + '=' + s.color).join('|');
  // ≥2 柱状系列 = 堆叠：同 x 的柱不堆叠会互相盖住只剩一条（1.6 无原生 stack，
  // 手动累计数据 + bands 填充；上层柱图例经 value 覆盖回原始值，不显示累计值）
  const stacked = (series || []).length >= 2 && series.every((s) => s.bars);
  const data = useMemo(() => {
    if (!series || !series.length) return null;
    // x 取所有系列点并集（多泳道散点：首个系列可为空、其余有值）；
    // 各系列按 t 对齐取值，缺 t 为 null（断线/无点）
    const xset = new Set();
    series.forEach((s) => s.points.forEach((p) => xset.add(p.t)));
    const xs = [...xset].sort((a, b) => a - b);
    if (!xs.length) return null;
    const cols = series.map((s) => {
      const m = new Map();
      s.points.forEach((p) => m.set(p.t, p));
      return xs.map((t) => m.get(t));
    });
    return [
      xs,
      ...cols.map((ps, j) => (stacked && j > 0
        ? ps.map((p, pi) => cols.slice(0, j + 1).reduce((sum, q) => sum + (q[pi]?.v || 0), 0))
        : ps.map((p) => (p ? p.v : null)))),
    ];
  }, [series, stacked]);
  const ready = !!data && data[0].length >= 2; // 1 point = degenerate time scale
  const emptyKey = ready ? 'full' : 'empty';

  useEffect(() => {
    const el = host.current;
    if (!el || !ready) return undefined;
    const wpx = el.clientWidth || 720;
    // 图例不复用轴 values hook：默认 Time 行走 numSeriesVal（裸 unix 秒 "1,760,340,000"）、
    // 系列行走 fmtNum（无单位 "0.35"）→ 图例与轴同格式（悬停读数与轴刻度一致）
    const fmtVal = (u, val) => (val == null ? null : (yVal ? yVal(val) : String(val)));
    const opts = {
      width: wpx,
      height,
      // 默认显图例（系列名）；泳道图系列名=y 轴刻度名，传 { show: false } 去重
      legend: legend || { show: true },
      cursor: { show: true },
      series: [
        { value: (u, val) => (typeof val === 'number' && val > 1e9 ? bjAxis(val) : val) },
        ...series.map((s, j) => (s.dots
          // 散点模式（逐条明细：一次判定一个点，不连线）；
          // 带 n 时点大小 ∝ sqrt(n)（分位散点：小样本桶的点要看得出"只有 1 个请求"）
          ? (() => {
            const o = { label: s.name, stroke: s.color, fill: s.color, width: 0, points: { show: true }, paths: () => null, value: fmtVal };
            if (s.n) {
              o.points.size = (u, si, i) => {
                const n = (nRef.current[si - 1] || [])[i] || 0;
                return 3 + 6 * Math.sqrt(n / (nMaxRef.current[si - 1] || 1));
              };
              // 函数式 size 必须配自定义构建器（见 sizeDotPaths 注释）
              o.points.paths = sizeDotPaths;
            }
            else o.points.size = 6;
            return o;
          })()
          : s.bars
          // 柱状（分桶计数：稀疏尖峰用折线会被 spline 平滑/过冲）：
          // paths.bars() 从 fillTo 基线画矩形、自动柱宽；注意是复数 bars 没有 bar
            ? {
              label: s.name,
              stroke: s.color,
              fill: s.color,
              points: { show: false },
              spanGaps: false,
              paths: uPlot.paths.bars(),
              // 堆叠上层柱数据是累计值，图例显示原始计数（覆盖 fmtVal）
              ...(stacked && j > 0 ? { value: (u, val, si, idx) => (idx == null ? null : (s.points[idx] || {}).v) } : { value: fmtVal }),
            }
            : {
            label: s.name,
            stroke: s.color,
            width: 1.5,
            points: { show: false },
            spanGaps: false,
            paths: uPlot.paths.spline(),
            value: fmtVal,
          })),
      ],
      axes: [
        // x=unix 秒（>1e9）时按北京时间打刻度，非时间轴（数值轴）原样。
        // uPlot 刻度间距只保证 >= space（默认 50px），而 "MM-DD HH:mm" 12px 约 64px 宽，
        // 窗口/宽度组合出间距 < 标签宽时（如 1h 窗）标签重叠 → 按实际间距抽稀标签
        // （刻度/网格不动），保留最右（最新时刻）向左隔 N 取一
        { values: (u, vals, ai, space) => vals.map((v, i) => {
          if (typeof v !== 'number' || v <= 1e9) return v;
          const n = space > 0 ? Math.max(1, Math.ceil(66 / space)) : 1;
          return i % n === (vals.length - 1) % n ? bjAxis(v) : '';
        }) },
        { size: yAxisSize, ...(yVal ? { values: (u, vals) => vals.map((v) => yVal(v)) } : {}) },
      ],
      // 堆叠：band [upper, lower] 让 upper 系列的填充从 lower 值铺到 upper 值（而非从 0）
      ...(stacked ? { bands: series.slice(1).map((_, j) => ({ series: [j + 2, j + 1] })) } : {}),
      // 调用方覆盖（如事件时间线：y 轴固定三泳道 0/1/2）
      ...(scales ? { scales } : {}),
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
  }, [namesKey, height, !!yVal, emptyKey, stacked]);

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
