import React, { useEffect, useRef, useState } from 'react';
import { Alert, Card, Segmented, Space, Typography } from 'antd';
import UPlotChart, { msAxis, qpsAxis } from '../UPlotChart.jsx';
import { fetchJson as api } from '../api.js';

const { Text } = Typography;
const pct = (v) => v == null ? '-' : (v * 100).toFixed(1) + '%';

// 运行指标段（统计页内嵌，原 MetricsPage 页已删）：时间窗用统计页共有的 hours。
// 双源：24h 内走环（10s 自刷新，与原来一致）；超 24h 走 request_log 分桶快照
// （stats/chart，QPS/错误率/延迟分位；L2 无逐请求数据，大窗口下该 tab 挂牌说明）。
export default function MetricsSection({ hours = 24 }) {
  const [data, setData] = useState(null);   // 环（常驻：L2 tab + 版本/uptime）
  const [chart, setChart] = useState(null); // 大窗口 request_log 分桶
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
    if (!bigWin) return undefined;
    let alive = true;
    api(`/admin/api/stats/chart?hours=${hours}`)
      .then((c) => { if (alive) setChart(c); }).catch(() => {});
    return () => { alive = false; };
  }, [bigWin, hours]);
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
    const latSeries = [
      { name: 'P50', color: '#52c41a', points: w.map((t) => ({ t: t.t, v: t.p50 })) },
      { name: 'P95', color: '#fa8c16', points: w.map((t) => ({ t: t.t, v: t.p95 })) },
      { name: 'P99', color: '#ff4d4f', points: w.map((t) => ({ t: t.t, v: t.p99 })) },
    ];
    const l2Series = [
      { name: 'L2 avg', color: '#722ed1', points: w.map((t) => ({ t: t.t, v: t.l2_avg_ms })) },
      { name: 'skipped', color: '#52c41a', points: w.map((t) => ({ t: t.t, v: t.l2_skipped })) },
      { name: 'sampled', color: '#1677ff', points: w.map((t) => ({ t: t.t, v: t.l2_sampled })) },
    ];
    chartDefs = {
      qps: { tags: qpsSeries, fmt: qpsFmt, yVal: qpsAxis, series: qpsSeries },
      err: { tags: errSeries, fmt: pct, yVal: pct, series: errSeries },
      lat: { tags: latSeries, fmt: msFmt, yVal: msAxis, series: latSeries },
      l2: { tags: l2Series.slice(0, 1), fmt: msFmt, yVal: msAxis, series: l2Series },
    };
  } else {
    // 大窗口：request_log 分桶（快照，随时间窗切换刷新）；口径与总览图一致
    const ch = chart || { ts: [] };
    const cpts = (arr, zero) => (ch.ts || []).map((t, i) => ({
      t, v: !arr || arr[i] == null ? null : (arr[i] ?? (zero ? 0 : null)) }));
    const qpsSeries = [
      { name: 'QPS', color: '#1677ff', points: cpts(ch.qps, true) },
      { name: 'QPS 峰值', color: '#722ed1', points: cpts(ch.qps_max, true) },
    ];
    const errSeries = [
      { name: '5xx', color: '#ff4d4f', points: cpts(ch.err_5xx, true) },
      { name: '403', color: '#fa8c16', points: cpts(ch.err_403, true) },
      { name: '429', color: '#faad14', points: cpts(ch.err_429, true) },
    ];
    const latSeries = [
      { name: 'P50', color: '#52c41a', points: cpts(ch.p50, false) },
      { name: 'P95', color: '#fa8c16', points: cpts(ch.p95, false) },
      { name: 'P99', color: '#ff4d4f', points: cpts(ch.p99, false) },
      { name: 'Min', color: '#b7eb8f', points: cpts(ch.lat_min, false) },
    ];
    chartDefs = {
      qps: { tags: qpsSeries, fmt: qpsFmt, yVal: qpsAxis, series: qpsSeries },
      err: { tags: errSeries, fmt: pct, yVal: pct, series: errSeries },
      lat: { tags: latSeries, fmt: msFmt, yVal: msAxis, series: latSeries },
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
      ) : (
        <>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, marginBottom: 8, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 11, fontWeight: 400 }}>
              {cur.tags.map((s) => { const n = avg(s.points); return (
                <Text key={s.name} type="secondary" style={{ marginLeft: 10, fontSize: 11 }}>
                  {s.name} 均值 {n == null ? '-' : cur.fmt(n)}
                </Text>); })}
            </span>
            <Text type="secondary" style={{ fontSize: 11 }}>
              {bigWin ? '分桶快照 · 随时间窗刷新' : '环数据 10s 刷新'} · 滚轮缩放 · 拖拽框选放大 · 双击回到全景 · {uptime}
            </Text>
          </div>
          <UPlotChart height={380} yVal={cur.yVal} series={cur.series} />
        </>
      )}
    </Card>
  );
}
