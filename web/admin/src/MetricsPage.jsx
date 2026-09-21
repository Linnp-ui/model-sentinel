import React, { useEffect, useRef, useState } from 'react';
import { Alert, Card, Col, Row, Segmented, Typography } from 'antd';
import UPlotChart from './UPlotChart.jsx';

const { Text } = Typography;

const api = async (path) => {
  const r = await fetch(path);
  if (r.status === 401 || r.redirected) { window.location.href = '/admin/login'; throw new Error('auth'); }
  return r.json();
};
const pct = (v) => v == null ? '-' : (v * 100).toFixed(1) + '%';

// chart tab 入 URL（#/metrics?chart=err）：总览 KPI 卡按图深链，刷新留 tab
const CHART_TABS = ['qps', 'err', 'lat', 'l2'];
const readChartTab = () => {
  const p = new URLSearchParams(window.location.hash.split('?')[1] || '');
  const c = p.get('chart');
  return CHART_TABS.includes(c) ? c : 'qps';
};
const writeChartTab = (c) => history.replaceState(null, '', '#/metrics?chart=' + c);

export default function MetricsPage() {
  const [data, setData] = useState(null);
  const [chartTab, setChartTabState] = useState(readChartTab);
  const setChartTab = (c) => { setChartTabState(c); writeChartTab(c); };
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
  if (err) return <Alert type="error" message={`刷新失败：${err}（显示上次数据）`} />;
  if (!data) return <Card loading />;
  const { ticks, panel } = data;
  const lastT = ticks.length ? ticks[ticks.length - 1].t : Math.floor(Date.now() / 1000);
  const winStart = lastT - 1440 * 60; // fixed 24h window, zoom via wheel/drag
  const w = ticks.filter((t) => t.t >= winStart);
  // gap:true = restart blank tick -> null (stay disconnected).
  // Idle-but-defined (qps, err rates) -> 0 so lines stay continuous.
  const zv = (t, v) => (t.gap ? null : (v ?? 0));
  const series = (key, color) =>
    [{ name: key, color: color || '#1677ff', points: w.map((t) => ({ t: t.t, v: zv(t, t[key]) })) }];
  // QPS 只画总量（不分类型）
  const qpsSeries = series('qps_total', '#1677ff');
  const avg = (points) => {
    const vs = points.filter((p) => p.v != null);
    return vs.length ? vs.reduce((a, p) => a + p.v, 0) / vs.length : null;
  };
  const AvgTags = ({ list, fmt }) => (
    <span style={{ fontSize: 11, fontWeight: 400 }}>
      {list.map((s) => ({ k: s.name, n: avg(s.points) }))
        .map((x) => <Text key={x.k} type="secondary" style={{ marginLeft: 10, fontSize: 11 }}>
          {x.k} 均值 {x.n == null ? '-' : fmt(x.n)}
        </Text>)}
    </span>
  );
  
  const msFmt = (v) => Math.round(v).toLocaleString('en-US');
  const qpsFmt = (v) => v.toFixed(2);
  // y 轴标签 = 数字 + 单位（延迟 ≥1000ms 自动升为 s，避免长数字撑爆轴宽）
  const msAxis = (v) => {
    if (v == null) return '';
    if (v === 0) return '0';
    return v >= 1000
      ? (v / 1000).toFixed(v >= 10000 ? 0 : 1).replace(/\.0$/, '') + 's'
      : Math.round(v) + 'ms';
  };
  const qpsAxis = (v) => (v == null ? '' : (Math.round(v * 100) / 100).toLocaleString('en-US') + ' req/s');
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
  const chartDefs = {
    qps: { tags: qpsSeries, fmt: qpsFmt, yVal: qpsAxis, series: qpsSeries },
    err: { tags: errSeries, fmt: pct, yVal: pct, series: errSeries },
    lat: { tags: latSeries, fmt: msFmt, yVal: msAxis, series: latSeries },
    l2: { tags: l2Series.slice(0, 1), fmt: msFmt, yVal: msAxis, series: l2Series },
  };
  const cur = chartDefs[chartTab] || chartDefs.qps;
  const uptime = `v${panel.version} · uptime ${Math.floor((panel.uptime_s || 0) / 3600)}h${Math.floor((panel.uptime_s || 0) % 3600 / 60)}m`;
  return (
    <Row gutter={[12, 12]}>
      <Col span={24}><Card>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, marginBottom: 8, flexWrap: 'wrap' }}>
          <Segmented value={chartTab} onChange={setChartTab}
            options={[
              { label: 'QPS', value: 'qps' },
              { label: '错误率', value: 'err' },
              { label: '网关延迟分位', value: 'lat' },
              { label: 'L2', value: 'l2' },
            ]} />
          <AvgTags list={cur.tags} fmt={cur.fmt} />
          <Text type="secondary" style={{ fontSize: 11 }}>滚轮缩放 · 拖拽框选放大 · 双击回到全景 · {uptime}</Text>
        </div>
        <UPlotChart height={460} yVal={cur.yVal} series={cur.series} />
      </Card></Col>
    </Row>
  );
}
