import React, { useEffect, useState, useCallback } from 'react';
import {
  Table, Button, Tag, Space, message, Card, Statistic, Select,
  Popconfirm, Alert, Tooltip, Typography, Row, Col,
} from 'antd';
import { ReloadOutlined, DeleteOutlined } from '@ant-design/icons';
import { ruleZh } from './ruleNames.jsx';

const { Text } = Typography;

const api = async (path, opts = {}) => {
  const r = await fetch(`/admin/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (r.status === 401 || r.status === 302) {
    window.location.href = '/admin/login?next=/admin/app';
    throw new Error('未登录');
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail) || `HTTP ${r.status}`);
  return body;
};

const apiAbs = async (path, opts = {}) => {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (r.status === 401 || r.status === 302) {
    window.location.href = '/admin/login?next=/admin/app';
    throw new Error('未登录');
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail) || `HTTP ${r.status}`);
  return body;
};

// 环形图（占比，中间显示总量）
const DONUT_COLORS = ['#1677ff', '#52c41a', '#faad14', '#ff4d4f', '#722ed1',
  '#13c2c2', '#eb2f96', '#fa8c16', '#2f54eb', '#a0d911'];

function DonutChart({ items, labelKey, testid }) {
  if (!items || !items.length) return <div style={{ height: 180, display: 'flex', alignItems: 'center', justifyContent: 'center' }}><Text type="secondary">暂无数据</Text></div>;
  const total = items.reduce((s, x) => s + (x.count || 0), 0) || 1;
  const R = 70, C = 2 * Math.PI * R;
  let acc = 0;
  const segs = items.map((x, i) => {
    const f = (x.count || 0) / total;
    const s = { label: x[labelKey], count: x.count || 0, f, off: acc, color: x.color || DONUT_COLORS[i % DONUT_COLORS.length] };
    acc += f;
    return s;
  });
  return (
    <div style={{ display: 'flex', gap: 16, alignItems: 'center', flexWrap: 'wrap' }}>
      <svg data-testid={testid} width="180" height="180" viewBox="0 0 180 180">
        <circle cx="90" cy="90" r={R} fill="none" stroke="#f0f0f0" strokeWidth="26" />
        {segs.map((s) => (
          <circle key={String(s.label)} cx="90" cy="90" r={R} fill="none"
            stroke={s.color} strokeWidth="26"
            strokeDasharray={`${Math.max(s.f * C - 2, 0)} ${C}`}
            strokeDashoffset={-s.off * C}
            transform="rotate(-90 90 90)">
            <title>{`${s.label}：${s.count}（${(s.f * 100).toFixed(1)}%）`}</title>
          </circle>
        ))}
        <text x="90" y="86" textAnchor="middle" fontSize="22" fontWeight="700">{total}</text>
        <text x="90" y="106" textAnchor="middle" fontSize="12" fill="#999">总量</text>
      </svg>
      <div style={{ flex: 1, minWidth: 200, height: 180, overflowY: 'auto' }}>
        {segs.map((s) => (
          <div key={String(s.label)} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4, fontSize: 12 }}>
            <span style={{ width: 10, height: 10, borderRadius: 2, background: s.color }} />
            <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={s.label}>{s.label || '—'}</span>
            <Text type="secondary" style={{ fontSize: 12 }}>{s.count}（{(s.f * 100).toFixed(1)}%）</Text>
          </div>
        ))}
      </div>
    </div>
  );
}

// 60桶趋势（蓝=放行 橙=本地路由 红=拦截；蓝条 = 调用 - 本地路由 - 拦截下限0）
function TrendBars({ slots, counts, routes, blocks }) {
  if (!slots || !slots.length) return <Alert type="info" showIcon message="暂无趋势数据" />;
  const W = 900, H = 360, pad = 40; // H 加大：整图更高，柱子纵向空间更足
  const allows = slots.map((_, i) => Math.max(0, (counts[i] || 0) - (routes[i] || 0) - (blocks[i] || 0)));
  // 自适应：最高的柱占绘图区 90%（留 10% 顶空）；网格线按同一比例对齐
  const maxV = Math.max(1, ...slots.map((_, i) => Math.max(allows[i], routes[i] || 0, blocks[i] || 0)));
  const plotH = H - pad * 2;
  const HEAD = 0.9;
  const bw = Math.max(3, Math.min(22, (W - pad * 2) / slots.length - 2));
  const scale = (v) => plotH * HEAD * (v / maxV);
  const x = (i) => pad + 6 + i * ((W - pad * 2) / slots.length);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%' }}>
      {[0, 0.5, 1].map((fr) => (
        <g key={fr}>
          <line x1={pad} x2={W - pad / 2} y1={H - pad - scale(maxV * fr)}
            y2={H - pad - scale(maxV * fr)} stroke="#e8e8e8" strokeDasharray="3,3" />
          <text x={pad - 6} y={H - pad - scale(maxV * fr) + 4} fontSize="10" fill="#999" textAnchor="end">{Math.round(maxV * fr)}</text>
        </g>
      ))}
      {slots.map((s, i) => (
        <g key={s + i}>
          <title>{`${s}: 放行 ${allows[i]}，本地路由 ${routes[i] || 0}，拦截 ${blocks[i] || 0}（含 403）`}</title>
          <rect x={x(i)} y={H - pad - scale(allows[i])} width={bw} height={scale(allows[i])} fill="#1677ff" rx="1" />
          {(routes[i] || 0) > 0 && <rect x={x(i)} y={H - pad - scale(routes[i])} width={bw} height={scale(routes[i])} fill="#faad14" rx="1" />}
          {(blocks[i] || 0) > 0 && <rect x={x(i)} y={H - pad - scale(blocks[i])} width={bw} height={scale(blocks[i])} fill="#ff4d4f" rx="1" />}
          {i % 10 === 0 && <text x={x(i) + bw / 2} y={H - pad + 12} fontSize="9" fill="#666" textAnchor="middle">{s}</text>}
        </g>
      ))}
    </svg>
  );
}

const errText = (e) => (Array.isArray(e) ? e.map(x => `${(x.loc || []).join('.')}: ${x.msg}`).join('; ') : String(e.message || e));

const RANGES = [
  { hours: 24, label: '最近 24 小时' },
  { hours: 168, label: '最近 7 天' },
  { hours: 720, label: '最近 30 天' },
];

// 建议卡片标题：明示「规则 1/2/3 用面板窗口、KEY 分级另用独立窗口」这个双窗口事实
// （2026-09-16 B 案：触发口径改为「异常处置 = 拦截 ∪ 本地路由」）。
// KEY×调用量占比：KEY 下拉（按调用量排序）+ 该 KEY 下放行/本地路由/拦截环图
// 数据源 stats/group?dim=key_name（request_log，与 Token 占比同源；全量聚合不限 5000 条）
function KeyActionDonut({ rows }) {
  const byKey = {};
  (rows || []).forEach((r) => {
    const k = r.label || '(unknown)';
    const calls = r.calls || 0, blocked = r.blocked || 0, local = r.local_routed || 0;
    byKey[k] = { calls, blocked, local, allow: Math.max(0, calls - blocked - local) };
  });
  const keys = Object.keys(byKey).sort((a, b) => byKey[b].calls - byKey[a].calls);
  const [sel, setSel] = useState(null);
  const cur = sel && byKey[sel] ? sel : keys[0];
  const g = cur ? byKey[cur] : null;
  const items = g ? [
    { label: '放行', count: g.allow, color: '#1677ff' },
    { label: '本地路由', count: g.local, color: '#faad14' },
    { label: '拦截', count: g.blocked, color: '#ff4d4f' },
  ] : [];
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="small">
      <Select data-testid="stats-keycalls-select" style={{ width: 260 }} value={cur} placeholder="选择人员/KEY（按调用量排序）"
        onChange={setSel} options={keys.map((v) => ({ value: v, label: `${v}（${byKey[v].calls} 次）` }))} />
      {g
        ? <DonutChart items={items} labelKey="label" testid="stats-keycalls-donut" />
        : <Alert type="info" showIcon message="所选时间范围内暂无调用数据" />}
    </Space>
  );
}

// KEY×模型 token 占比：KEY 下拉（按 token 总量排序）+ 该 KEY 下各模型 token 环图
function KeyModelDonut({ rows }) {
  const tot = {};
  (rows || []).forEach((r) => { tot[r.key] = (tot[r.key] || 0) + (r.tokens || 0); });
  const keys = Object.keys(tot).sort((a, b) => tot[b] - tot[a]);
  const [sel, setSel] = useState(null);
  const cur = sel && tot[sel] !== undefined ? sel : keys[0];
  const items = (rows || []).filter((r) => r.key === cur)
    .map((r) => ({ model: r.model, count: r.tokens || 0 }));
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="small">
      <Select data-testid="stats-keymodel-select" style={{ width: 260 }} value={cur} placeholder="选择人员/KEY（按 token 总量排序）"
        onChange={setSel} options={keys.map((v) => ({ value: v, label: `${v}（${tot[v]} tokens）` }))} />
      {cur
        ? <DonutChart items={items} labelKey="model" testid="stats-keymodel-donut" />
        : <Alert type="info" showIcon message="所选时间范围内暂无 token 数据" />}
    </Space>
  );
}


export default function StatsPage() {
  const [hours, setHours] = useState(168);
  const [ov, setOv] = useState(null);
  const [ana, setAna] = useState(null);
  const [loading, setLoading] = useState(false);
  const [keyModels, setKeyModels] = useState([]);
  const [keyCalls, setKeyCalls] = useState([]);

  const loadAll = useCallback(async (h = hours) => {
    setLoading(true);
    try {
      const [o, a, m, k] = await Promise.all([
        api(`/stats/overview?hours=${h}`),
        apiAbs(`/admin/analytics/data?limit=5000&since=${encodeURIComponent(new Date(Date.now() - h * 3600 * 1000).toISOString())}`),
        api(`/stats/key-model?hours=${h}&top=20`),
        api(`/stats/group?dim=key_name&hours=${h}`),
      ]);
      setOv(o); setAna(a); setKeyModels(m.items || []); setKeyCalls(k.items || []);
    } catch (e) { message.error(errText(e)); }
    finally { setLoading(false); }
  }, [hours]);

  useEffect(() => { loadAll(); }, [loadAll]);





  const cleanup = async () => {
    try {
      const r = await api('/logs/cleanup', { method: 'POST', body: JSON.stringify({}) });
      message.success(`已清理 ${r.deleted} 条过期明细（默认保留 90 天）`);
      loadAll();
    } catch (e) { message.error(errText(e)); }
  };







  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Space size="middle" wrap>
        <Select value={hours} onChange={(v) => setHours(v)} style={{ width: 150 }}
          options={RANGES.map(r => ({ value: r.hours, label: r.label }))} />
        <Button icon={<ReloadOutlined />} onClick={() => loadAll()} loading={loading}>刷新</Button>
        <Popconfirm title="清理 90 天前的明细？" onConfirm={cleanup}>
          <Button icon={<DeleteOutlined />}>清理过期明细</Button>
        </Popconfirm>
      </Space>

      <Row gutter={[12, 12]}>
        <Col flex={1}>
          <Card data-testid="stats-ov-total" styles={{ body: { padding: '12px 16px', height: 104 } }}>
            <Statistic title="总调用量" value={ov ? ov.total : '-'} valueStyle={{ fontSize: 24 }} />
          </Card>
        </Col>
        <Col flex={1}>
          <Card data-testid="stats-ov-nonallow" styles={{ body: { padding: '12px 16px', height: 104 } }}>
            <Statistic title={<Tooltip title="口径：非放行 = 拦截（action=block* 或 HTTP 403）+ 本地路由（route_local 机密降级）。生产 on_block=fallback_local，敏感内容多落本地路由，只看拦截会漏。来源 request_log，仅 /v1/* 的 POST">非放行率</Tooltip>}
              value={ov && ov.total ? ((ov.blocked + ov.local_routed) / ov.total * 100).toFixed(1) : '-'} suffix="%"
              valueStyle={{ fontSize: 24, color: ov && ov.blocked > 0 ? '#cf1322' : ov && ov.local_routed > 0 ? '#fa8c16' : undefined }} />
            <Text type="secondary" style={{ fontSize: 12 }}>拦截 {ov ? ov.blocked : '-'} / 本地路由 {ov ? ov.local_routed : '-'}</Text>
          </Card>
        </Col>
        <Col flex={1}>
          <Card data-testid="stats-ov-ips" styles={{ body: { padding: '12px 16px', height: 104 } }}>
            <Statistic title="活跃 IP" value={ov ? ov.active_ips : '-'} valueStyle={{ fontSize: 24 }} />
          </Card>
        </Col>
        <Col flex={1}>
          <Card data-testid="stats-ov-avgms" styles={{ body: { padding: '12px 16px', height: 104 } }}>
            <Statistic title={<Tooltip title="口径：request_log.duration_ms 均值，端到端总耗时（含上游生成）；网关自身耗时看观测页 P50/P95/P99">平均总耗时</Tooltip>}
              value={ov ? ov.avg_ms : '-'} suffix="ms" valueStyle={{ fontSize: 24 }} />
          </Card>
        </Col>
        <Col flex={1}>
          <Card data-testid="stats-ov-tokens" styles={{ body: { padding: '12px 16px', height: 104 } }}>
            <Statistic title="Token 用量" value={ov && ov.tokens_total != null ? ov.tokens_total.toLocaleString() : '-'} valueStyle={{ fontSize: 24 }} />
          </Card>
        </Col>
      </Row>

      {ana && (
        <>
          <Card title="时间趋势（蓝=放行 橙=本地路由 红=拦截）"
            extra={<Text type="secondary" style={{ fontSize: 12 }}>
              本窗口 {ana.c_allow} 放行 / {ana.c_route} 本地路由 / {ana.c_block} 拦截（含 403）
            </Text>}>
            <TrendBars slots={ana.time_slots} counts={ana.time_counts} routes={ana.time_route} blocks={ana.time_block} />
          </Card>
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'stretch' }}>
            <Card title="Provider Top8"
              style={{ minWidth: 380, flex: 1, display: 'flex', flexDirection: 'column' }}
              styles={{ body: { flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center' } }}>
              <DonutChart items={ana.provider_top} labelKey="provider" />
            </Card>
            <Card title="规则 Top10"
              style={{ minWidth: 380, flex: 1, display: 'flex', flexDirection: 'column' }}
              styles={{ body: { flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center' } }}>
              <DonutChart items={(ana.rule_top || []).map((x) => ({ ...x, rule: ruleZh(x.rule) }))} labelKey="rule" />
            </Card>
            <Card data-testid="stats-keymodel-card" title="KEY × 模型 Token 占比"
              style={{ minWidth: 380, flex: 1, display: 'flex', flexDirection: 'column' }}
              styles={{ body: { flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center' } }}>
              <KeyModelDonut rows={keyModels} />
            </Card>
            <Card data-testid="stats-keycalls-card" title="KEY × 调用量占比（蓝=放行 橙=本地路由 红=拦截）"
              style={{ minWidth: 380, flex: 1, display: 'flex', flexDirection: 'column' }}
              styles={{ body: { flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center' } }}>
              <KeyActionDonut rows={keyCalls} />
            </Card>
          </div>
        </>
      )}

    </Space>
  );
}
