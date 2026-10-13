import { useEffect, useRef, useState } from 'react';
import { Alert, Card, Segmented, Space, Typography } from 'antd';
import UPlotChart, { msAxis, qpsAxis } from '../UPlotChart.jsx';
import DailyHeatmap, { dailyPointsFromChart } from './DailyHeatmap.jsx';
import { fetchJson as api } from '../api.js';

const { Text } = Typography;
const pct = (v) => v == null ? '-' : (v * 100).toFixed(1) + '%';

// 运行指标段（统计页内嵌，原 MetricsPage 页已删）：时间窗用统计页共有的 hours。
// 双源：24h 内 QPS/错误率/L2 走环（10s 自刷新）；超 24h 三者走 request_log 分桶快照（stats/chart）。
// 延迟分位 tab 恒走 stats/chart 分桶（与总览图同源同口径，10s 刷新）——
// 环分位是 5 分钟滑窗（PCT_WINDOW_S），与总览分桶口径对不上，不再用于展示。
export default function MetricsSection({ hours = 24 }) {
  const [data, setData] = useState(null);   // 环（常驻：L2 tab + 版本/uptime）
  const [chart, setChart] = useState(null); // request_log 分桶（延迟分位固定数据源）
  const [chartTab, setChartTab] = useState('qps');
  const bigWin = (hours || 24) > 24;
  const winHours = Math.min(hours || 24, 24);
  const [err, setErr] = useState('');
  const timer = useRef(null);
  useEffect(() => {
    const load = () => api('/admin/metrics/timeseries')
      .then((d) => { setData(d); setErr(''); }).catch((e) => setErr(String(e.message || e)));
    load();
    timer.current = setInterval(() => { if (!document.hidden) load(); }, 10000);
    const onVis = () => { if (!document.hidden) load(); };
    document.addEventListener('visibilitychange', onVis);
    return () => { clearInterval(timer.current); document.removeEventListener('visibilitychange', onVis); };
  }, []);
  useEffect(() => {
    let alive = true;
    const load = () => api(`/admin/api/stats/chart?hours=${hours}`)
      .then((c) => { if (alive) setChart(c); }).catch(() => {});
    load();
    const t = setInterval(() => { if (!document.hidden) load(); }, 10000);
    return () => { alive = false; clearInterval(t); };
  }, [hours]);
  const CHART_TABS = ['qps', 'err', 'lat', 'l2'];
  if (err) return <Alert type="error" message={`运行指标刷新失败：${err}（显示上次数据）`} style={{ marginTop: 12 }} />;
  if (!data) return <Card title="运行指标" style={{ marginTop: 12 }} loading />;
  const { ticks, panel } = data;

  const avg = (points) => {
    const vs = points.filter((p) => p.v != null);
    return vs.length ? vs.reduce((a, p) => a + p.v, 0) / vs.length : null;
  };
  const msFmt = (v) => Math.round(v).toLocaleString('en-US');
  const qpsFmt = (v) => v.toFixed(2);

  // 延迟分位固定走 request_log 分桶（与总览图同源同口径）：环分位是 5 分钟滑窗，
  // 与总览分桶对不上，已退下展示（环 tick 里仍带 p50/p95/p99，API 不变）
  const ch = chart || { ts: [] };
  const cpts = (arr, zero) => (ch.ts || []).map((t, i) => ({
    t, v: !arr || arr[i] == null ? null : (arr[i] ?? (zero ? 0 : null)) }));
  // 桶内样本数 n（散点尺寸用）：生产是低流量，桶多为空，n 直接决定分位点可信度
  const nArr = (ch.counts || []).map((v) => v || 0);
  // 延迟分位改散点：每桶一点、点大小 ∝ n，不连线（小样本桶的分位数≈单请求值，连线是误导）
  const chartLatSeries = [
    { name: 'P50', color: '#52c41a', dots: true, n: nArr, points: cpts(ch.p50, false) },
    { name: 'P95', color: '#fa8c16', dots: true, n: nArr, points: cpts(ch.p95, false) },
    { name: 'P99', color: '#ff4d4f', dots: true, n: nArr, points: cpts(ch.p99, false) },
    // Min 用中性灰：#b7eb8f 浅绿与 P50 #52c41a 同色系难分辨，白底上对比度仅 ~1.9:1
    { name: 'Min', color: '#8c8c8c', dots: true, n: nArr, points: cpts(ch.lat_min, false) },
  ];
  // 空态：窗口内真没数据时画平 0 线会读成"正常运行"，显式空态替代
  const winQps = bigWin ? (ch.counts || []).reduce((a, b) => a + (b || 0), 0) : null; // 小窗空态在环分支算
  const hasErrWin = bigWin
    ? (ch.err_5xx || []).some((v) => v) || (ch.err_403 || []).some((v) => v) || (ch.err_429 || []).some((v) => v)
    : null;
  const hasLatWin = (ch.p50 || []).some((v) => v != null);
  const dailyCounts = dailyPointsFromChart(ch);
  const nzBuckets = (ch.counts || []).filter((v) => v > 0).length;
  let chartDefs;
  if (!bigWin) {
    const lastT = ticks.length ? ticks[ticks.length - 1].t : Math.floor(Date.now() / 1000);
    const w = ticks.filter((t) => t.t >= lastT - winHours * 3600); // 环内窗口，zoom via wheel/drag
    // gap:true = restart blank tick -> null (stay disconnected).
    // Idle-but-defined (qps, err rates) -> 0 so lines stay continuous.
    const zv = (t, v) => (t.gap ? null : (v ?? 0));
    const series = (key, color) =>
      [{ name: key, color: color || '#1677ff', points: w.map((t) => ({ t: t.t, v: zv(t, t[key]) })) }];
    // QPS 只画总量（不分类型）
    const qpsSeries = series('qps_total', '#1677ff');
    // y 轴标签 = 数字 + 单位（延迟 ≥1000ms 自动升为 s，避免长数字撑爆轴宽）
    const errSeries = [
      { name: '5xx', color: '#ff4d4f', points: w.map((t) => ({ t: t.t, v: zv(t, t.err_5xx_rate) })) },
      { name: '403', color: '#fa8c16', points: w.map((t) => ({ t: t.t, v: zv(t, t.err_403_rate) })) },
      { name: '429', color: '#faad14', points: w.map((t) => ({ t: t.t, v: zv(t, t.err_429_rate) })) },
    ];
    const l2LatSeries = [
      { name: 'L2 avg', color: '#722ed1', points: w.map((t) => ({ t: t.t, v: t.l2_avg_ms })) },
    ];
    // skipped/sampled 是逐 tick 计数，不能与 ms 同轴（量纲混合相对高度无意义）：
    // 合成 L2 触发占比 % 序列单画（白名单 key 95% 跳 L2，占比随流量构成波动）
    const l2RateSeries = [
      { name: 'L2 占比', color: '#1677ff', fmt: pct, points: w.map((t) => {
        const s = t.l2_sampled ?? 0, k = t.l2_skipped ?? 0;
        return { t: t.t, v: (s + k) > 0 ? s / (s + k) : null };
      }) },
    ];
    const qpsEmpty = w.length && !w.some((t) => (t.qps_total ?? 0) > 0)
      ? '窗口内 0 次调用（外发链路空闲，不是"正常运行"）' : null;
    const errEmpty = w.length && !w.some((t) => ((t.err_5xx_rate || 0) + (t.err_403_rate || 0) + (t.err_429_rate || 0)) > 0)
      ? '窗口内 0 错误（5xx/403/429 全 0）' : null;
    const l2Empty = w.length && !w.some((t) => ((t.l2_sampled ?? 0) + (t.l2_skipped ?? 0)) > 0)
      ? '窗口内无 L2 触发请求（sampled=skipped=0，无灰区流量）' : null;
    const latEmpty = !hasLatWin ? '窗口内无延迟样本（无经网关的请求）' : null;
    chartDefs = {
      qps: { tags: qpsSeries, fmt: qpsFmt, yVal: qpsAxis, series: qpsSeries, empty: qpsEmpty },
      err: { tags: errSeries, fmt: pct, yVal: pct, series: errSeries, empty: errEmpty },
      lat: { tags: chartLatSeries, fmt: msFmt, yVal: msAxis, series: chartLatSeries, empty: latEmpty,
            hint: '散点：点大小 ∝ 桶内样本数 n（小样本桶的分位数≈单请求值）' },
      l2: { tags: [...l2LatSeries, ...l2RateSeries], fmt: msFmt, yVal: msAxis, series: l2LatSeries, empty: l2Empty,
            second: { label: 'L2 触发占比 sampled/(sampled+skipped)：白名单 key 95% 跳 L2，占比随流量构成波动', yVal: pct, series: l2RateSeries } },
    };
  } else {
    // 大窗口：QPS/错误率也切 request_log 分桶（快照，10s 刷新）
    // 低流量下 8640 桶只有个位数/十几桶有流量：QPS 改日粒度热力，折线只是平 0 线
    const qpsEmpty = (winQps || 0) === 0 ? `窗口内 0 次调用（${hours}h）` : null;
    const errEmpty = !hasErrWin ? '窗口内 0 错误（5xx/403/429 全 0）' : null;
    const latEmpty = !hasLatWin ? '窗口内无延迟样本（无经网关的请求）' : null;
    const errSeries = [
      { name: '5xx', color: '#ff4d4f', points: cpts(ch.err_5xx, true) },
      { name: '403', color: '#fa8c16', points: cpts(ch.err_403, true) },
      { name: '429', color: '#faad14', points: cpts(ch.err_429, true) },
    ];
    chartDefs = {
      qps: { heatmap: winQps ? dailyCounts : null, heatmapNote: winQps
              ? `${(ch.ts || []).length} 桶仅 ${nzBuckets} 桶有流量 → 日粒度（北京时区）` : null, empty: qpsEmpty },
      err: { tags: errSeries, fmt: pct, yVal: pct, series: errSeries, empty: errEmpty },
      lat: { tags: chartLatSeries, fmt: msFmt, yVal: msAxis, series: chartLatSeries, empty: latEmpty,
            hint: '散点：点大小 ∝ 桶内样本数 n（小样本桶的分位数≈单请求值）' },
      l2: null, // request_log 未记逐请求 L2 数据，大窗口无此 tab
    };
  }
  const cur = (bigWin && chartTab === 'l2') ? null : (chartDefs[CHART_TABS.includes(chartTab) ? chartTab : 'qps']);
  const uptime = `v${panel.version} · uptime ${Math.floor((panel.uptime_s || 0) / 3600)}h${Math.floor((panel.uptime_s || 0) % 3600 / 60)}m`;
  return (
    <Card title="运行指标" style={{ marginTop: 12 }}
      extra={<Space>
        {bigWin && <Text type="secondary" style={{ fontSize: 11 }}>超 24h 走 request_log 分桶快照（L2 序列仅环内）</Text>}
        <Segmented size="small" value={chartTab} onChange={setChartTab}
          options={[
            { label: 'QPS', value: 'qps' },
            { label: '错误率', value: 'err' },
            { label: '网关延迟分位', value: 'lat' },
            ...(bigWin ? [] : [{ label: 'L2', value: 'l2' }]),
          ]} />
      </Space>}>
      {!cur ? (
        <Alert type="info" showIcon style={{ margin: '12px 0' }}
          message="L2 序列只有环里有（request_log 未记逐请求 L2 数据），切回 24h 窗口查看" />
      ) : cur.empty ? (
        <Alert type="info" showIcon style={{ margin: '12px 0' }} message={cur.empty} />
      ) : (
        <>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, marginBottom: 8, flexWrap: 'wrap' }}>
            {cur.tags && (
              <span style={{ fontSize: 11, fontWeight: 400 }}>
                {cur.tags.map((s) => { const n = avg(s.points); return (
                  <Text key={s.name} type="secondary" style={{ marginLeft: 10, fontSize: 11 }}>
                    {s.name} 均值 {n == null ? '-' : (s.fmt || cur.fmt)(n)}
                  </Text>); })}
              </span>
            )}
            <Text type="secondary" style={{ fontSize: 11 }}>
              {cur.hint ? `${cur.hint} · ` : ''}{bigWin ? 'request_log 分桶快照' : '环数据（延迟分位走分桶）'} · 10s 刷新 · 滚轮缩放 · 拖拽框选放大 · 双击回到全景 · {uptime}
            </Text>
          </div>
          {cur.heatmap ? (
            <div>
              {cur.heatmapNote && <Text type="secondary" style={{ fontSize: 11, display: 'block', marginBottom: 8 }}>{cur.heatmapNote}</Text>}
              <DailyHeatmap points={cur.heatmap} testid="metrics-qps-daily" />
            </div>
          ) : (
            <UPlotChart height={380} yVal={cur.yVal} series={cur.series} />
          )}
          {cur.second && (
            <div style={{ marginTop: 12 }}>
              <Text type="secondary" style={{ fontSize: 11 }}>{cur.second.label}</Text>
              <UPlotChart height={180} yVal={cur.second.yVal} series={cur.second.series} />
            </div>
          )}
        </>
      )}
    </Card>
  );
}
