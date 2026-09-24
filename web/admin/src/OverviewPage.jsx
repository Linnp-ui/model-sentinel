import React, { useEffect, useState, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { Row, Col, Card, Typography, Alert, Space, Button, Select } from 'antd';
import { errText, fetchJson as get, RANGES } from './api.js';
import UPlotChart, { msAxis, qpsAxis } from './UPlotChart.jsx';
import { ProviderHealthCard } from './components/ProviderPage.jsx';
import { SuggestionCard } from './components/KeyRulesPage.jsx';

const { Text: T } = Typography;

// ---------------- 总览（默认落地页）：每页挑一个最重要信号，同指标不重复 ----------------
// 运行指标 → QPS / 延迟分位（两大图）+ 错误率；审计 → 拦截率 / 本地路由；统计 → Token
// KEY 规则 → 调节策略建议（SuggestionCard 同卡）；熔断/外网key 由全局 StatusBar 灯覆盖，不重复做卡

const avg = (vs) => {
  const v = vs.filter((x) => x != null);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
};

// KPI tile：固定 104px 等高（不带 Card head，避免 56px head 撑出参差）
function Kpi({ testid, title, value, suffix, color, go, goLabel }) {
  return (
    <Card data-testid={testid} hoverable onClick={() => { window.location.hash = go; }}
      styles={{ body: { height: 112, padding: '12px 16px', display: 'flex', flexDirection: 'column' } }}>
      <T type="secondary" style={{ fontSize: 12 }}>{title}</T>
      <T strong style={{ fontSize: 24, lineHeight: 1.3, color, whiteSpace: 'nowrap' }}>{value}</T>
      {suffix && <T type="secondary" style={{ fontSize: 12 }}>{suffix}</T>}
      <div style={{ marginTop: 'auto', fontSize: 11, color: '#bbb', textAlign: 'right' }}>{goLabel || '详情 →'}</div>
    </Card>
  );
}

function ChartPanel({ title, go, children, right }) {
  return (
    <Card title={title} extra={
      <Space size="small">
        {right}
        <Button type="link" size="small" style={{ padding: 0 }} onClick={() => { window.location.hash = go; }}>详情 →</Button>
      </Space>}>
      {children}
    </Card>
  );
}

export default function OverviewPage() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState('');
  // 跨度管拦截率/本地路由率/Token（stats/overview 口径）+ 两大折线图（stats/chart 按窗口重分桶）；
  // QPS/错误率 KPI 是实时信号（环近 10 tick），不随跨度变
  const [hours, setHours] = useState(24);

  const load = useCallback(async (h = hours) => {
    try {
      const [ts, ov, ch] = await Promise.all([
        get('/admin/metrics/timeseries'),
        get(`/admin/api/stats/overview?hours=${h}`),
        get(`/admin/api/stats/chart?hours=${h}`),
      ]);
      const w = ts.ticks || [];
      const last = w.slice(-10);
      const qpsNow = avg(last.map((t) => t.qps_total));
      const errNow = avg(last.map((t) => (t.err_5xx_rate || 0) + (t.err_429_rate || 0)));
      setD({
        chart: ch,
        qps: qpsNow,
        err: errNow,
        blocked: ov.blocked || 0,
        routeLocal: ov.local_routed || 0,
        total: ov.total || 0,
        tokens: ov.tokens_total || 0,
      });
      setErr('');
    } catch (e) { setErr(errText(e)); }
  }, [hours]);

  useEffect(() => {
    load();
    const t = setInterval(() => { if (!document.hidden) load(); }, 30000);
    return () => clearInterval(t);
  }, [load]);

  if (!d && !err) return <Card loading />;

  const ch = d?.chart || { ts: [], qps: [], p50: [], p95: [], p99: [] };
  const pts = (arr, zero) => (ch.ts || []).map((t, i) => ({
    t, v: !arr || arr[i] == null ? null : (arr[i] ?? (zero ? 0 : null)) }));
  // 总览只看总量（分类型是运行指标页的活）；数据源自 stats/chart（request_log 按窗口分桶）
  // 峰值保持：QPS 峰值=桶内 10s 子槽最大速率，延迟 Max/Min=桶内极值，毛刺不被桶均值吃掉
  const qpsSeries = ch.ts?.length
    ? [{ name: 'QPS', color: '#1677ff', points: pts(ch.qps, true) },
       { name: 'QPS 峰值', color: '#722ed1', points: pts(ch.qps_max, true) }] : [];
  // 网关延迟分位 P50/P95/P99（分位口径同 latency_percentiles，空桶断线）
  const latSeries = ch.ts?.length ? [
    { name: 'P50', color: '#52c41a', points: pts(ch.p50, false) },
    { name: 'P95', color: '#fa8c16', points: pts(ch.p95, false) },
    { name: 'P99', color: '#ff4d4f', points: pts(ch.p99, false) },
    { name: 'Min', color: '#b7eb8f', points: pts(ch.lat_min, false) },
  ] : [];
  const errColor = d && d.err != null
    ? (d.err >= 0.05 ? '#ff4d4f' : d.err > 0 ? '#fa8c16' : '#52c41a') : undefined;

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      {err && <Alert type="error" showIcon message={`总览刷新失败：${err}（显示上次数据）`} />}
      {/* 时间窗：Portal 进标题右侧（#page-toolbar-slot），标题栏本就常驻；切页卸载自动清空 */}
      {typeof document !== 'undefined' && document.getElementById('page-toolbar-slot')
        ? createPortal((
          <Select size="small" value={hours} onChange={(v) => setHours(v)} style={{ width: 130 }}
            options={RANGES.map((r) => ({ value: r.hours, label: r.label }))} />
        ), document.getElementById('page-toolbar-slot'))
        : null}
      <Row gutter={[12, 12]}>
        <Col flex={1} style={{ minWidth: 0 }}>
          <Kpi testid="ov-qps" title="当前 QPS"
            value={d && d.qps != null ? d.qps.toFixed(1) : '-'} suffix="req/s"
            go="/stats" goLabel="统计 →" />
        </Col>
        <Col flex={1} style={{ minWidth: 0 }}>
          <Kpi testid="ov-err" title="错误率 5xx+429（近10tick）"
            value={d && d.err != null ? (d.err * 100).toFixed(1) + '%' : '-'}
            color={errColor} go="/stats" goLabel="统计 →" />
        </Col>
        <Col flex={1} style={{ minWidth: 0 }}>
          <Kpi testid="ov-block" title="拦截率"
            value={d && d.total > 0 ? (100 * d.blocked / d.total).toFixed(1) + '%' : '-'}
            suffix={d ? `${d.blocked} 次 / 总 ${d.total}` : ''}
            color={d && d.blocked > 0 ? '#ff4d4f' : '#52c41a'}
            go="/audit?action=block" goLabel="审计：拦截 →" />
        </Col>
        <Col flex={1} style={{ minWidth: 0 }}>
          <Kpi testid="ov-route" title="本地路由率"
            value={d && d.total > 0 ? (100 * d.routeLocal / d.total).toFixed(1) + '%' : d ? '0.0%' : '-'}
            suffix={d ? `${d.routeLocal} 次 / 总 ${d.total}` : ''}
            color={d && d.routeLocal > 0 ? '#fa8c16' : undefined}
            go="/audit?action=route_local" goLabel="审计：本地路由 →" />
        </Col>
        <Col flex={1} style={{ minWidth: 0 }}>
          <Kpi testid="ov-tokens" title="Token 用量"
            value={d && d.tokens ? d.tokens.toLocaleString() : '-'}
            go="/stats" goLabel="统计可视化 →" />
        </Col>
      </Row>
      <Row gutter={[12, 12]}>
        <Col span={12}>
          <ChartPanel title="QPS" go="/stats">
            <UPlotChart height={300} yVal={qpsAxis} series={qpsSeries} />
          </ChartPanel>
        </Col>
        <Col span={12}>
          <ChartPanel title="网关延迟分位 P50/P95/P99" go="/stats">
            <UPlotChart height={300} yVal={msAxis} series={latSeries} />
          </ChartPanel>
        </Col>
      </Row>
      <Row gutter={[12, 12]}>
        <Col span={12}>
          <SuggestionCard />
        </Col>
        <Col span={12}>
          {/* 总览只要 Provider+状态 两列（明细列去 Provider 管理页看） */}
          <ProviderHealthCard compact />
        </Col>
      </Row>
    </Space>
  );
}
