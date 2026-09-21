import React, { useEffect, useState, useCallback } from 'react';
import { Row, Col, Card, Text, Typography, Alert, Space, Button } from 'antd';
import { errText } from './api.js';
import UPlotChart from './UPlotChart.jsx';
import { ProviderHealthCard } from './components/ProviderPage.jsx';
import { SuggestionCard } from './components/KeyRulesPage.jsx';

const { Text: T } = Typography;

// ---------------- 总览（默认落地页）：每页挑一个最重要信号，同指标不重复 ----------------
// 运行指标 → QPS / 延迟分位（两大图）+ 错误率；审计 → 拦截率 / 本地路由；统计 → Token
// KEY 规则 → 调节策略建议（SuggestionCard 同卡）；熔断/外网key 由全局 StatusBar 灯覆盖，不重复做卡
const get = async (path) => {
  const r = await fetch(path);
  if (r.status === 401 || r.status === 302) {
    window.location.href = '/admin/login?next=/admin/app';
    throw new Error('未登录');
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${r.status}`);
  return body;
};

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

  const load = useCallback(async () => {
    try {
      const [ts, ov] = await Promise.all([
        get('/admin/metrics/timeseries'),
        get('/admin/api/stats/overview?hours=24'),
      ]);
      const w = ts.ticks || [];
      const last = w.slice(-10);
      const qpsNow = avg(last.map((t) => t.qps_total));
      const errNow = avg(last.map((t) => (t.err_5xx_rate || 0) + (t.err_429_rate || 0)));
      setD({
        ticks: w,
        qps: qpsNow,
        err: errNow,
        blocked: ov.blocked || 0,
        routeLocal: ov.local_routed || 0,
        total: ov.total || 0,
        tokens: ov.tokens_total || 0,
      });
      setErr('');
    } catch (e) { setErr(errText(e)); }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(() => { if (!document.hidden) load(); }, 30000);
    return () => clearInterval(t);
  }, [load]);

  if (!d && !err) return <Card loading />;

  const w = d?.ticks || [];
  // 总览只看总量（分类型是运行指标页的活）
  const qpsSeries = w.length
    ? [{ name: 'QPS', color: '#1677ff', points: w.map((t) => ({ t: t.t, v: t.gap ? null : (t.qps_total ?? 0) })) }] : [];
  // 网关延迟分位 P50/P95/P99（与运行指标页 lat 图同口径）
  const latSeries = w.length ? [
    { name: 'P50', color: '#52c41a', points: w.map((t) => ({ t: t.t, v: t.gap ? null : (t.p50 ?? null) })) },
    { name: 'P95', color: '#fa8c16', points: w.map((t) => ({ t: t.t, v: t.gap ? null : (t.p95 ?? null) })) },
    { name: 'P99', color: '#ff4d4f', points: w.map((t) => ({ t: t.t, v: t.gap ? null : (t.p99 ?? null) })) },
  ] : [];
  const qpsAxis = (v) => (v == null ? '' : (Math.round(v * 100) / 100).toLocaleString('en-US') + ' req/s');
  const msAxis = (v) => {
    if (v == null) return '';
    if (v === 0) return '0';
    return v >= 1000
      ? (v / 1000).toFixed(v >= 10000 ? 0 : 1).replace(/\.0$/, '') + 's'
      : Math.round(v) + 'ms';
  };
  const errColor = d && d.err != null
    ? (d.err >= 0.05 ? '#ff4d4f' : d.err > 0 ? '#fa8c16' : '#52c41a') : undefined;

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      {err && <Alert type="error" showIcon message={`总览刷新失败：${err}（显示上次数据）`} />}
      <Row gutter={[12, 12]}>
        <Col flex={1} style={{ minWidth: 0 }}>
          <Kpi testid="ov-qps" title="当前 QPS（近10tick）"
            value={d && d.qps != null ? d.qps.toFixed(1) : '-'} suffix="req/s"
            go="/metrics?chart=qps" goLabel="运行指标 →" />
        </Col>
        <Col flex={1} style={{ minWidth: 0 }}>
          <Kpi testid="ov-err" title="错误率 5xx+429（近10tick）"
            value={d && d.err != null ? (d.err * 100).toFixed(1) + '%' : '-'}
            color={errColor} go="/metrics?chart=err" goLabel="运行指标 →" />
        </Col>
        <Col flex={1} style={{ minWidth: 0 }}>
          <Kpi testid="ov-block" title="拦截率（24h）"
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
          <Kpi testid="ov-tokens" title="Token 用量（24h）"
            value={d && d.tokens ? d.tokens.toLocaleString() : '-'}
            go="/stats" goLabel="统计可视化 →" />
        </Col>
      </Row>
      <Row gutter={[12, 12]}>
        <Col span={12}>
          <ChartPanel title="QPS（近 24h，滚轮缩放 / 双击回全景）" go="/metrics?chart=qps">
            <UPlotChart height={300} yVal={qpsAxis} series={qpsSeries} />
          </ChartPanel>
        </Col>
        <Col span={12}>
          <ChartPanel title="网关延迟分位 P50/P95/P99（近 24h，滚轮缩放 / 双击回全景）" go="/metrics?chart=lat">
            <UPlotChart height={300} yVal={msAxis} series={latSeries} />
          </ChartPanel>
        </Col>
      </Row>
      <Row gutter={[12, 12]}>
        <Col span={12}>
          {/* 总览只要 Provider+状态 两列（明细列去 Provider 管理页看） */}
          <ProviderHealthCard compact />
        </Col>
        <Col span={12}>
          <SuggestionCard />
        </Col>
      </Row>
    </Space>
  );
}
