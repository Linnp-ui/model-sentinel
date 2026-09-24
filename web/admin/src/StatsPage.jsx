import React, { useEffect, useState, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { Button, Space, message, Card, Statistic, Select, Popconfirm, Alert, Tooltip, Typography, Row, Col, Spin } from 'antd';
import { ReloadOutlined, DeleteOutlined } from '@ant-design/icons';
import { ruleZh } from './ruleNames.jsx';
import MetricsSection from './components/MetricsSection.jsx';
import { api, errText, RANGES } from './api.js';

const { Text } = Typography;

// 规则展示名：request_log.blocked_reason 带 l1:/l2:[scope:] 前缀，ruleZh 表无前缀键
const ruleLabel = (r) => {
  if (!r || r === '(unknown)') return '放行（无规则命中）';
  const m2 = String(r).match(/^l2:[^:]+:(.+)$/);
  if (m2) return `L2 判定：${m2[1]}`;
  return ruleZh(String(r).replace(/^l1:/, '').replace(/^l2:/, ''));
};

// 环形图（占比，中间显示总量）
const DONUT_COLORS = ['#1677ff', '#52c41a', '#faad14', '#ff4d4f', '#722ed1',
  '#13c2c2', '#eb2f96', '#fa8c16', '#2f54eb', '#a0d911'];

// fmt：数值格式化（金额传 v => `¥${v.toFixed(2)}`；缺省原样显示）
function DonutChart({ items, labelKey, testid, fmt }) {
  if (!items || !items.length) return <div style={{ height: 180, display: 'flex', alignItems: 'center', justifyContent: 'center' }}><Text type="secondary">暂无数据</Text></div>;
  const total = items.reduce((s, x) => s + (x.count || 0), 0) || 1;
  const f = fmt || ((v) => String(v));
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
        <text x="90" y="86" textAnchor="middle" fontSize="22" fontWeight="700">{f(total)}</text>
        <text x="90" y="106" textAnchor="middle" fontSize="12" fill="#999">总量</text>
      </svg>
      <div style={{ flex: 1, minWidth: 200, height: 180, overflowY: 'auto' }}>
        {segs.map((s) => (
          <div key={String(s.label)} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4, fontSize: 12 }}>
            <span style={{ width: 10, height: 10, borderRadius: 2, background: s.color }} />
            <span style={{ flex: '0 1 auto', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={s.label}>{s.label || '—'}</span>
            <Text type="secondary" style={{ fontSize: 12 }}>{f(s.count)}（{(s.f * 100).toFixed(1)}%）</Text>
          </div>
        ))}
      </div>
    </div>
  );
}

// 按天计费竖向柱状图（北京日 × 金额，仅已定价行，与 daily_total 同口径）
function BillDailyBars({ daily }) {
  if (!daily || !daily.length) return <div style={{ height: 160, display: 'flex', alignItems: 'center', justifyContent: 'center' }}><Text type="secondary">暂无数据</Text></div>;
  const byDay = {};
  daily.forEach((r) => { byDay[r.day] = (byDay[r.day] || 0) + (r.cost || 0); });
  const days = Object.keys(byDay).sort();
  const vals = days.map((d) => byDay[d]);
  const W = 900, H = 220, padL = 48, padR = 10, padT = 24, padB = 26;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const maxV = Math.max(0.0001, ...vals);
  const slot = plotW / days.length;
  const bw = Math.min(42, slot * 0.7);
  const step = Math.ceil(days.length / 15);
  const y = (v) => padT + plotH * (1 - v / maxV);
  return (
    <div style={{ overflowX: 'auto' }}>
    <svg data-testid="billing-daily-bars" viewBox={`0 0 ${W} ${H}`}
      style={{ display: 'block', width: '100%', minWidth: days.length > 10 ? days.length * 38 : undefined }}>
      {[0, 0.5, 1].map((g) => (
        <g key={g}>
          <line x1={padL} x2={W - padR} y1={y(maxV * g)} y2={y(maxV * g)} stroke="#f0f0f0" />
          <text x={padL - 6} y={y(maxV * g) + 4} textAnchor="end" fontSize="10" fill="#999">¥{(maxV * g).toFixed(maxV < 1 ? 2 : 0)}</text>
        </g>
      ))}
      {days.map((d, i) => {
        const v = vals[i];
        const x = padL + slot * i + (slot - bw) / 2;
        return (
          <g key={d}>
            <rect x={x} y={y(v)} width={bw} height={Math.max(padT + plotH - y(v), v > 0 ? 2 : 0)} fill="#1677ff" rx="2">
              <title>{`${d}：¥${v.toFixed(4)}`}</title>
            </rect>
            {days.length <= 16 && v > 0 && (
              <text x={x + bw / 2} y={y(v) - 4} textAnchor="middle" fontSize="10" fill="#666">¥{v.toFixed(v < 1 ? 2 : 0)}</text>
            )}
            {i % step === 0 && (
              <text x={x + bw / 2} y={H - padB + 15} textAnchor="middle" fontSize="10" fill="#999">{d.slice(5)}</text>
            )}
          </g>
        );
      })}
    </svg>
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
  const [ts, setTs] = useState(null);
  const [provTop, setProvTop] = useState([]);
  const [ruleTop, setRuleTop] = useState([]);
  const [loading, setLoading] = useState(false);
  const [keyModels, setKeyModels] = useState([]);
  const [keyCalls, setKeyCalls] = useState([]);
  const [billing, setBilling] = useState(null);
  // 影子（laya）汇总：独立数据源（审计热缓存），失败不拖累主统计
  const [shadow, setShadow] = useState(null);

  // 单一数据源 request_log：KPI 卡 / 趋势 / Top 榜同窗口同口径，页面数字不再打架
  // 单端点 /stats/summary 单遍聚合（后端 7 端点合并，口径与各自端点逐字段一致）
  const loadAll = useCallback(async (h = hours) => {
    setLoading(true);
    try {
      const d = await api(`/stats/summary?hours=${h}&top=20`);
      const [o, m, k, t, pg, rg, b] = [
        d.overview,
        { items: d.key_model },
        { items: d.group_key_name },
        d.timeseries,
        { items: d.group_provider },
        { items: d.group_rule },
        d.billing,
      ];
      setOv(o);
      setTs(t || { slots: [], counts: [], routes: [], blocks: [] });
      setProvTop((pg.items || []).slice(0, 8).map((g) => ({ label: g.label, count: g.calls })));
      setRuleTop((rg.items || []).slice(0, 10).map((g) => ({ label: ruleLabel(g.label), count: g.calls })));
      setKeyModels(m.items || []); setKeyCalls(k.items || []); setBilling(b);
    } catch (e) { message.error(errText(e)); }
    finally { setLoading(false); }
    try {
      setShadow(await api(`/l2-shadow-stats?hours=${h}`));
    } catch (e) { setShadow(null); }
  }, [hours]);

  useEffect(() => { loadAll(); }, [loadAll]);

  // KEY × 金额占比环图数据（仅已定价金额>0 的 KEY，降序）
  const billKeyCosts = (() => {
    const byKey = {};
    (billing?.items || []).forEach((it) => { byKey[it.key] = (byKey[it.key] || 0) + it.cost; });
    return Object.entries(byKey).filter(([, c]) => c > 0)
      .map(([label, count]) => ({ label, count: Math.round(count * 10000) / 10000 }))
      .sort((a, b) => b.count - a.count);
  })();






  const cleanup = async () => {
    try {
      const r = await api('/logs/cleanup', { method: 'POST', body: JSON.stringify({}) });
      message.success(`已清理 ${r.deleted} 条过期明细（默认保留 90 天）`);
      loadAll();
    } catch (e) { message.error(errText(e)); }
  };







  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {/* 工具栏：Portal 进标题右侧（#page-toolbar-slot），标题栏本就常驻，无需吸顶；切页卸载自动清空 */}
      {typeof document !== 'undefined' && document.getElementById('page-toolbar-slot')
        ? createPortal((
          <Space size="middle">
            <Select size="small" value={hours} onChange={(v) => setHours(v)} style={{ width: 130 }}
              options={RANGES.map(r => ({ value: r.hours, label: r.label }))} />
            <Button size="small" icon={<ReloadOutlined />} onClick={() => loadAll()} loading={loading}>刷新</Button>
            <Popconfirm title="清理 90 天前的明细？" onConfirm={cleanup}>
              <Button size="small" icon={<DeleteOutlined />}>清理过期明细</Button>
            </Popconfirm>
          </Space>
        ), document.getElementById('page-toolbar-slot'))
        : null}

      <Spin spinning={loading || !ov} tip="加载中…" size="large">
      <div style={{ width: '100%' }}>
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
            <Statistic title={<Tooltip title="口径：request_log.duration_ms 均值，端到端总耗时（含上游生成）；网关自身耗时看下方运行指标 P50/P95/P99">平均总耗时</Tooltip>}
              value={ov ? ov.avg_ms : '-'} suffix="ms" valueStyle={{ fontSize: 24 }} />
          </Card>
        </Col>
        <Col flex={1}>
          <Card data-testid="stats-ov-tokens" styles={{ body: { padding: '12px 16px', height: 104 } }}>
            <Statistic title="Token 用量" value={ov && ov.tokens_total != null ? ov.tokens_total.toLocaleString() : '-'} valueStyle={{ fontSize: 24 }} />
          </Card>
        </Col>
      </Row>

      {ov && (
        <>
          <Card title="时间趋势（蓝=放行 橙=本地路由 红=拦截）" style={{ marginTop: 12 }}
            extra={<Text type="secondary" style={{ fontSize: 12 }}>
              本窗口 {ov.total - ov.blocked - ov.local_routed} 放行 / {ov.local_routed} 本地路由 / {ov.blocked} 拦截
            </Text>}>
            <TrendBars slots={ts.slots} counts={ts.counts} routes={ts.routes} blocks={ts.blocks} />
          </Card>
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'stretch', marginTop: 12 }}>
            <Card title="Provider Top8"
              style={{ minWidth: 380, flex: 1, display: 'flex', flexDirection: 'column' }}
              styles={{ body: { flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center' } }}>
              <DonutChart items={provTop} labelKey="label" />
            </Card>
            <Card title="规则 Top10"
              style={{ minWidth: 380, flex: 1, display: 'flex', flexDirection: 'column' }}
              styles={{ body: { flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center' } }}>
              <DonutChart items={ruleTop} labelKey="label" />
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
          <MetricsSection hours={hours} />
          <Card title="影子（laya）汇总" style={{ marginTop: 12 }}
            extra={<Text type="secondary" style={{ fontSize: 12 }}>
              数据源审计热缓存（近 5000 条）；只有进 L2 的灰区流量才有影子
            </Text>}>
            {!shadow || !shadow.shadowed ? (
              <Alert type="info" showIcon
                message="本窗口暂无影子数据（影子只在进 L2 的灰区流量上产生；backend=laya 时影子自动停）" />
            ) : (
              <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', alignItems: 'center' }}>
                <Space size="large" wrap>
                  <Statistic title={<Tooltip title="口径：主影 label 相同 / 可比条数（主判定降级的兜底项不计入）">主影一致率</Tooltip>}
                    value={shadow.agree_rate == null ? '-' : (shadow.agree_rate * 100).toFixed(1)} suffix="%"
                    valueStyle={{ fontSize: 24 }} />
                  <Statistic title="影子样本" value={`${shadow.agree}/${shadow.compared}`}
                    valueStyle={{ fontSize: 24 }} />
                  <Statistic title={<Tooltip title="口径：有影子打标 / 进 L2 的条数">影子覆盖率</Tooltip>}
                    value={shadow.l2_total ? (shadow.shadowed / shadow.l2_total * 100).toFixed(1) : '-'} suffix="%"
                    valueStyle={{ fontSize: 24 }} />
                  <Statistic title="影子失败" value={shadow.shadow_degraded} valueStyle={{ fontSize: 24 }} />
                </Space>
                <div style={{ flex: 1, minWidth: 380 }}>
                  <DonutChart
                    items={[
                      { label: '主密·影密', count: shadow.cells.cc, color: '#52c41a' },
                      { label: '主放·影放', count: shadow.cells.nn, color: '#1677ff' },
                      { label: '主密·影放（影子漏报嫌疑）', count: shadow.cells.cn, color: '#faad14' },
                      { label: '主放·影密（影子误报嫌疑）', count: shadow.cells.nc, color: '#ff4d4f' },
                    ]}
                    labelKey="label" testid="shadow-cells-donut" />
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    主判定降级 {shadow.main_degraded} 条未计入一致率；影子样本主判定平均延迟 {shadow.avg_main_latency_ms ?? '?'}ms
                  </Text>
                </div>
              </div>
            )}
          </Card>
          <Card title={`Token 计费${billing ? `（合计 ¥${billing.total_cost}）` : ''}`} style={{ marginTop: 12 }}
            extra={<Text type="secondary" style={{ fontSize: 12 }}>内部分摊口径≠上游账单；缓存单价未配按全价（高估）；上游不回用量记0（少计）</Text>}>
            <div style={{ marginBottom: 12, display: 'flex', gap: 24, flexWrap: 'wrap' }}>
              <div style={{ flex: '0 0 420px' }}>
                <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>KEY × 金额占比（仅已定价）</Text>
                {billKeyCosts.length
                  ? <DonutChart items={billKeyCosts} labelKey="label" testid="billing-key-donut" fmt={(v) => `¥${v.toFixed(2)}`} />
                  : <Text type="secondary" style={{ fontSize: 12 }}>本窗口暂无计费金额（全部未定价或无调用）</Text>}
              </div>
              <div style={{ flex: 1, minWidth: 380 }}>
                <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>每日金额（北京时间，仅已定价）</Text>
                <BillDailyBars daily={billing ? billing.daily : []} />
              </div>
            </div>
          </Card>
        </>
      )}
      </div>
      </Spin>

    </Space>
  );
}
