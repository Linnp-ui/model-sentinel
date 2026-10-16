import { useRef, useEffect, useState, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { Space, Tag, message, Card, Statistic, Select, Alert, Tooltip, Typography, Row, Col, Spin } from 'antd';
import { ruleZh } from './ruleNames.jsx';
import MetricsSection from './components/MetricsSection.jsx';
import { api, errText, RANGES, usePoll } from './api.js';

const { Text } = Typography;

const DONUT_COLORS = ['#1677ff', '#52c41a', '#faad14', '#ff4d4f', '#722ed1',
  '#13c2c2', '#eb2f96', '#fa8c16', '#2f54eb', '#a0d911'];

// 横向条形列表：低基数排名（2~10 类）。环图"一大扇区+一条缝"不可读（实测 2 provider
// 97.7%/2.3%、3 KEY 99.4%/0.4%/0.2%），条形长度+数值标签可直接比较
function HBarList({ items, testid, renderVal = (x) => String(x.count) }) {
  if (!items || !items.length) return null;
  const total = items.reduce((s, x) => s + (x.count || 0), 0) || 1;
  const maxV = Math.max(...items.map((x) => x.count || 0), 1);
  return (
    <div data-testid={testid}>
      {items.map((x, i) => (
        <div key={x.label} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, fontSize: 14 }}>
          <span style={{ width: 180, flex: '0 0 auto', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={x.label}>{x.label}</span>
          <div style={{ flex: 1, background: '#f5f5f5', borderRadius: 3, height: 14 }}>
            <div style={{ width: `${((x.count || 0) / maxV) * 100}%`, background: x.color || DONUT_COLORS[i % DONUT_COLORS.length], height: '100%', borderRadius: 3 }} />
          </div>
          <span style={{ width: 170, flex: '0 0 auto', textAlign: 'right', color: '#666' }} title={x.title}>
            {renderVal(x)}（{((x.count || 0) / total * 100).toFixed(1)}%）
          </span>
        </div>
      ))}
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
              <title>{`${d}：¥${v.toFixed(v < 1 ? 2 : 0)}`}</title>
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

// 每 KEY 一行：行宽=调用量、段=放行/本地路由/拦截（blocked 恒 0 的段自然消失）
// + token 横向条。实测 3 个 KEY 全放得下，"选一个 KEY 看一个"的下拉交互不必要
function KeyCallBars({ keyModels, keyCalls, ov, testid }) {
  const keyTokens = {};
  (keyModels || []).forEach((r) => { keyTokens[r.key] = (keyTokens[r.key] || 0) + (r.tokens || 0); });
  const keyActions = {};
  (keyCalls || []).forEach((r) => {
    const k = r.label || '(unknown)';
    const calls = r.calls || 0, blocked = r.blocked || 0, local = r.local_routed || 0;
    keyActions[k] = { calls, blocked, local, allow: Math.max(0, calls - blocked - local) };
  });
  const keys = Array.from(new Set([...Object.keys(keyTokens), ...Object.keys(keyActions)]))
    .sort((a, b) => (keyActions[b]?.calls || 0) - (keyActions[a]?.calls || 0));
  if (!keys.length) return <Alert type="info" showIcon message="所选时间范围内暂无调用数据" />;
  const maxCalls = Math.max(1, ...keys.map((k) => keyActions[k]?.calls || 0));
  const maxTok = Math.max(1, ...keys.map((k) => keyTokens[k] || 0));
  const totalTokens = ov?.tokens_total || 0;
  const SEG = [['allow', '#1677ff', '放行'], ['local', '#faad14', '本地路由'], ['blocked', '#ff4d4f', '拦截']];
  return (
    <div data-testid={testid}>
      <Space size={4} wrap style={{ marginBottom: 12 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>行宽 ∝ 调用量 · 段色：</Text>
        {SEG.map(([key, color, name]) => (
          <Tag key={key} color={color === '#1677ff' ? 'blue' : color === '#faad14' ? 'orange' : 'red'}>{name}</Tag>
        ))}
      </Space>
      {keys.map((k) => {
        const g = keyActions[k] || { calls: 0, blocked: 0, local: 0, allow: 0 };
        return (
          <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8, fontSize: 14 }}>
            <span style={{ width: 160, flex: '0 0 auto', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={k}>{k}</span>
            <div style={{ flex: 1, display: 'flex', height: 18, background: '#f5f5f5', borderRadius: 3, overflow: 'hidden' }}>
              <div style={{ width: `${(g.calls / maxCalls) * 100}%`, display: 'flex', height: '100%' }}>
                {SEG.map(([key, color, name]) => (
                  <div key={key} title={`${name} ${g[key]} 次`}
                    style={{ width: g.calls > 0 ? `${(g[key] / g.calls) * 100}%` : 0, background: color }} />
                ))}
              </div>
            </div>
            <span style={{ width: 190, flex: '0 0 auto', textAlign: 'right', color: '#666' }}>
              {g.calls} 次
            </span>
          </div>
        );
      })}
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 6 }}>
        Token 用量（总计 {totalTokens ? totalTokens.toLocaleString() : 0}）
      </Text>
      {keys.map((k) => (
        <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, fontSize: 14 }}>
          <span style={{ width: 160, flex: '0 0 auto', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={k}>{k}</span>
          <div style={{ flex: 1, background: '#f5f5f5', borderRadius: 3, height: 14 }}>
            <div style={{ width: `${((keyTokens[k] || 0) / maxTok) * 100}%`, background: '#1677ff', height: '100%', borderRadius: 3 }} />
          </div>
          <span style={{ width: 190, flex: '0 0 auto', textAlign: 'right', color: '#666' }}>
            {(keyTokens[k] || 0).toLocaleString()}{totalTokens > 0 ? `（${((keyTokens[k] || 0) / totalTokens * 100).toFixed(1)}%）` : ''}
          </span>
        </div>
      ))}
    </div>
  );
}


export default function StatsPage() {
  const billingRef = useRef(null);
  const focus = new URLSearchParams((window.location.hash.split('?')[1]||'')).get('focus');
  const [hours, setHours] = useState(168);
  const [ov, setOv] = useState(null);
  const [provTop, setProvTop] = useState([]);
  const [loading, setLoading] = useState(false);
  const [keyModels, setKeyModels] = useState([]);
  const [keyCalls, setKeyCalls] = useState([]);
  const [billing, setBilling] = useState(null);
  useEffect(() => { if (focus === 'billing') billingRef.current?.scrollIntoView({behavior:'smooth',block:'start'}); }, [billing]);
  // 规则命中（audit_logs 窗口聚合，排除 默认放行）：比 group_rule 的 26 个 label
  // （top=「(unknown)」57.5%）信息量大——只有真实 firing 的规则 + 次数 + 最后命中
  const [ruleHits, setRuleHits] = useState(undefined);

  // 单一数据源 request_log：KPI 卡 / 趋势 / Top 榜同窗口同口径，页面数字不再打架
  // 单端点 /stats/summary 单遍聚合（后端 7 端点合并，口径与各自端点逐字段一致）
  // silent=true：30s 轮询用，不点 loading（不闪 Spin）、不弹错误（后台刷新失败静默）
  const loadAll = useCallback(async (h = hours, silent = false) => {
    if (!silent) setLoading(true);
    try {
      const [d, rh] = await Promise.all([
        api(`/stats/summary?hours=${h}&top=20`),
        api(`/rule-hits?hours=${h}`).catch(() => null), // 审计后端不可用时不拖垮整页
      ]);
      const [o, m, k, pg, b] = [d.overview, { items: d.key_model }, { items: d.group_key_name }, { items: d.group_provider }, d.billing];
      setOv(o);
      setProvTop((pg.items || []).slice(0, 8).map((g) => ({ label: g.label, count: g.calls, tokens: g.tokens })));
      setRuleHits(rh);
      setKeyModels(m.items || []); setKeyCalls(k.items || []); setBilling(b);
    } catch (e) { if (!silent) message.error(errText(e)); }
    finally { if (!silent) setLoading(false); }
  }, [hours]);

  useEffect(() => { loadAll(); }, [loadAll]);
  usePoll(() => loadAll(hours, true)); // 替代原刷新按钮

  // KEY × 金额占比环图数据（仅已定价金额>0 的 KEY，降序）
  const billKeyCosts = (() => {
    const byKey = {};
    (billing?.items || []).forEach((it) => { byKey[it.key] = (byKey[it.key] || 0) + it.cost; });
    return Object.entries(byKey).filter(([, c]) => c > 0)
      .map(([label, count]) => ({ label, count: Math.round(count * 10000) / 10000 }))
      .sort((a, b) => b.count - a.count);
  })();














  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {/* 工具栏：Portal 进标题右侧（#page-toolbar-slot），标题栏本就常驻，无需吸顶；切页卸载自动清空 */}
      {typeof document !== 'undefined' && document.getElementById('page-toolbar-slot')
        ? createPortal((
          <Space size="middle">
            <Select size="small" value={hours} onChange={(v) => setHours(v)} style={{ width: 130 }}
              options={RANGES.map((r) => ({ value: r.hours, label: r.label }))} />
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
          <MetricsSection hours={hours} />
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'stretch', marginTop: 12 }}>
            <Card title="Provider 调用分布"
              style={{ minWidth: 380, flex: 1, display: 'flex', flexDirection: 'column' }}
              styles={{ body: { flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center' } }}>
              {provTop.length
                ? <HBarList testid="stats-provider-bars" items={provTop}
                    renderVal={(x) => `${x.count} 次 · ${(x.tokens || 0).toLocaleString()} tok`} />
                : <Alert type="info" showIcon message="窗口内无调用" />}
            </Card>
            <Card title="规则命中 Top10"
              style={{ minWidth: 380, flex: 1, display: 'flex', flexDirection: 'column' }}
              styles={{ body: { flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center' } }}>
              {ruleHits === null
                ? <Alert type="warning" showIcon message="规则命中统计拉取失败（审计后端不可用？）" />
                : (ruleHits?.items || []).length
                  ? <HBarList testid="stats-rule-bars"
                      items={(ruleHits.items).slice(0, 10).map((it) => ({
                        label: ruleZh(it.rule), count: it.count,
                        title: `最后命中 ${String(it.last_seen || '').slice(5, 16)}`,
                      }))}
                      renderVal={(x) => `${x.count} 次`} />
                  : <Alert type="info" showIcon message="窗口内无规则命中（全部放行且无规则触发）" />}
            </Card>
          </div>
          <Card data-testid="stats-keymodelkeycalls-card" title="KEY × 调用量 / Token"
            style={{ marginTop: 12 }}
            styles={{ body: { flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center' } }}>
            <KeyCallBars keyModels={keyModels} keyCalls={keyCalls} ov={ov} testid="stats-key-bars" />
          </Card>
          <Card ref={billingRef} title={`Token 计费${billing ? `（合计 ¥${billing.total_cost}）` : ''}`} style={{ marginTop: 12, minHeight: 220 }}
            extra={<Text type="secondary" style={{ fontSize: 12 }}>内部分摊口径≠上游账单；缓存单价未配按全价（高估）；上游不回用量记0（少计）</Text>}>
            <div style={{ marginBottom: 12, display: 'flex', gap: 24, flexWrap: 'wrap' }}>
              <div style={{ flex: '0 0 420px' }}>
                <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>KEY × 金额（仅已定价）</Text>
                {billKeyCosts.length
                  ? <HBarList testid="billing-key-bars" items={billKeyCosts}
                      renderVal={(x) => `¥${x.count.toFixed(2)}`} />
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
