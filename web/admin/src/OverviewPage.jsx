import { useEffect, useState, useCallback, useRef, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { Row, Col, Card, Typography, Alert, Space, Button, Select, Table, Tag, Tooltip, Spin } from 'antd';
import { errText, fetchJson as get, RANGES } from './api.js';
import { bj, avg } from './utils.js';
import UPlotChart, { msAxis, qpsAxis } from './UPlotChart.jsx';
import DailyHeatmap, { dailyPointsFromChart } from './components/DailyHeatmap.jsx';
import { ProviderHealthCard } from './components/ProviderPage.jsx';
import { SuggestionCard } from './components/KeyRulesPage.jsx';
import WheelGate from './components/WheelGate.jsx';

// ---------------- 降级事件流：流式降级 / 本地路由 / L2 不可用（数据源 audit_logs） ----------------
const EV_META = [
  ['degrade', '流式降级', '#ff4d4f'],
  ['route_local', '本地路由', '#fa8c16'],
  ['l2_unavailable', 'L2 不可用', '#faad14'],
];

function EventStreamCard({ events }) {
  // 三计数（金丝雀：今天外发链路/安全降级正不正常）+ 明细表（预筛的最近事件）。
  // 时间线散点已删：~5 条/天频率下近乎恒空，信息量≈0，表格已能看时间分布（2026-10-12 拍板）
  const s = events.summary || {};
  const cols = [
    /* 列宽用 % 自适应（灌数轮次表口径）；时间为纯文本，窄列溢出需 ellipsis 裁切 */
    { title: '时间 (北京时间)', dataIndex: 'ts', width: '16%', ellipsis: true, render: (v) => bj(v) },
    { title: '事件', dataIndex: 'kinds', width: '20%',
      render: (ks) => (ks || []).map((k) => {
        const m = EV_META.find(([id]) => id === k);
        return <Tag key={k} color={m ? m[2] : 'default'} style={{ marginInlineEnd: 4 }}>{m ? m[1] : k}</Tag>;
      }) },
    { title: '明细', width: '22%',
      render: (_, r) => (
        <Tooltip title={`${r.rule || '-'} / ${r.downgraded_from || '-'} / ${r.provider || '-'}${r.model ? ':' + r.model : ''}`}>
          <span>{[r.downgraded_from, r.rule].filter(Boolean).join(' · ') || '-'}</span>
        </Tooltip>
      ) },
    { title: '内容', dataIndex: 'text_preview', width: '42%', ellipsis: true },
  ];
  return (
    <Card title="降级事件流"
      extra={
        <Space size="large">
          {EV_META.map(([id, label, color]) => (
            <span key={id}><Typography.Text style={{ color, fontWeight: 600 }}>{s[id] ?? 0}</Typography.Text>
              <Typography.Text type="secondary"> {label}</Typography.Text></span>
          ))}
        </Space>
      }>
      {/* 明细表是总览页唯一嵌套滚动容器：滚轮让位页面（表仍可拖细滚动条翻），见 WheelGate */}
      <WheelGate>
      {(events.recent || []).length === 0 ? (
        <Alert type="success" showIcon message="本窗口无降级事件（外发链路全绿：无流式降级、无本地路由、L2 正常）" />
      ) : (
        <Table rowKey={(r) => `${r.ts}-${r.rule}`} size="small" tableLayout="fixed" columns={cols} dataSource={events.recent}
          pagination={false} scroll={{ y: 300 }} />
      )}
      </WheelGate>
    </Card>
  );
}

const { Text: T } = Typography;

// ---------------- 总览（默认落地页）：一个页面看到尽量多信息 ----------------
// KPI 5 卡 + QPS/延迟分位两大图 + 降级事件流 + 建议/Provider 健康全在页上；
// 与统计页 MetricsSection 的重复是有意保留（落地页密度优先，详情点「详情 →」跳统计页）
// KEY 规则 → 调节策略建议（SuggestionCard 同卡）；熔断/外网key 由全局 StatusBar 灯覆盖，不重复做卡

// KPI tile：固定 104px 等高（不带 Card head，避免 56px head 撑出参差）
function Kpi({ testid, title, value, suffix, color, bar, go, hint }) {
  const card = (
    <Card data-testid={testid} hoverable={!!go} onClick={go ? () => { window.location.hash = go; } : undefined}
      style={{ borderLeft: `3px solid ${bar ?? color ?? '#e2e4ea'}`, cursor: go ? 'pointer' : 'default' }}
      styles={{ body: { height: 96, padding: '12px 16px', display: 'flex', flexDirection: 'column' } }}>
      <T type="secondary" style={{ fontSize: 14, height: 20, display: 'flex', alignItems: 'center' }}>{title}{go && <span style={{ marginLeft: 4, fontSize: 20, lineHeight: 1, position: 'relative', top: -1, color: '#1677ff' }}>→</span>}</T>
      {/* 24→32：5 个 KPI 是总览页唯一的量化入口，字号小要眯眼看 */}
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
        <T strong style={{ fontSize: 32, lineHeight: 1.25, color, whiteSpace: 'nowrap' }}>{value}</T>
        {suffix && <T type="secondary" style={{ fontSize: 12 }}>{suffix}</T>}
      </div>
    </Card>
  );
  // 可跳的卡：hover 明示跳转（光标+阴影只是暗示，Tooltip 说清点哪去哪）；每卡口径不同，文案按卡给
  return go ? <Tooltip title={hint}>{card}</Tooltip> : card;
}

function ChartPanel({ title, go, children }) {
  return (
    <Card title={title} style={{ height: '100%' }} extra={go ? (
      <Button type="link" size="small" style={{ padding: 0 }} onClick={() => { window.location.hash = go; }}>详情 →</Button>) : null}>
      {children}
    </Card>
  );
}

export default function OverviewPage() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState('');
  // 初次使用指引：落地页顶部引导。X = 本次会话收起；「不再提醒」= 记 localStorage 永久不再出现
  const [guideHidden, setGuideHidden] = useState(() => localStorage.getItem('gw-firstguide-dismissed') === '1');
  const [guideSessionHidden, setGuideSessionHidden] = useState(false);
  const dismissGuide = () => { localStorage.setItem('gw-firstguide-dismissed', '1'); setGuideHidden(true); };
  // 跨度管拦截率/本地路由率/Token（stats/overview 口径）+ 两大折线图（stats/chart 按窗口重分桶）；
  // QPS/错误率 KPI 是实时信号（环近 10 tick），不随跨度变
  const [hours, setHours] = useState(24);
  // 切窗反馈：只有手动切窗显加载动画，30s 轮询刷新不闪 Spin
  const [loading, setLoading] = useState(false);
  const hoursRef = useRef(hours);
  hoursRef.current = hours;
  const seqRef = useRef(0);

  const load = useCallback(async (h = hours) => {
    // 只有最新一次 load 有权摘遮罩：在飞的旧窗请求（30s 轮询）后落地时
    // 不能把刚切窗接上的遮罩立刻拆掉
    const seq = ++seqRef.current;
    try {
      const [ts, ov, ch, ev] = await Promise.all([
        get('/admin/metrics/timeseries?recent=20'),
        get(`/admin/api/stats/overview?hours=${h}`),
        get(`/admin/api/stats/chart?hours=${h}`),
        get(`/admin/api/events?hours=${h}`).catch(() => null),
      ]);
      // 切窗后旧窗请求才落地 → 丢弃，防止旧窗数据覆盖新窗（30s 轮询与切窗竞态）
      if (h !== hoursRef.current) return;
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
        events: ev,
      });
      setErr('');
    } catch (e) { if (h === hoursRef.current) setErr(errText(e)); }
    // 摘遮罩双守卫：seq 防同窗双击刷新互拆；hoursRef 防切窗后、新 load 启动前
    // 旧窗请求先落地把刚接上的遮罩拆掉
    finally { if (seq === seqRef.current && h === hoursRef.current) setLoading(false); }
  }, [hours]);

  useEffect(() => {
    load();
    const t = setInterval(() => { if (!document.hidden) load(); }, 30000);
    return () => clearInterval(t);
  }, [load]);

  // hooks 必须在早退 return 之前（eslint react-hooks/rules-of-hooks）
  const ch = d?.chart || { ts: [], qps: [], qps_max: [], counts: [], p50: [], p95: [], p99: [], lat_min: [] };
  // useMemo：数据不变不重算（30s 轮询/切窗点击渲染都会重跑本组件）；
  // ≤24h 不渲染热力图，跳过计算
  const dailyCounts = useMemo(() => (hours > 24 ? dailyPointsFromChart(ch) : null), [ch, hours]);

  if (!d && !err) return <Card loading />;

  const pts = (arr, zero) => (ch.ts || []).map((t, i) => ({
    t, v: !arr || arr[i] == null ? null : (arr[i] ?? (zero ? 0 : null)) }));
  // 两大图数据源自 stats/chart（request_log 按窗口分桶，与统计页延迟分位同源同口径）；
  // 峰值保持：QPS 峰值=桶内 10s 子槽最大速率，延迟 Min=桶内极值，毛刺不被桶均值吃掉
  const nArr = (ch.counts || []).map((v) => v || 0);
  const winCalls = nArr.reduce((a, b) => a + b, 0);
  const nzBuckets = nArr.filter((v) => v > 0).length;
  // QPS：窗口 0 调用 → 显式空态（平 0 线会读成"正常运行"）；>24h 窗稀疏流量 → 日粒度热力
  const qpsEmpty = winCalls === 0 ? `近 ${hours} 小时 0 次调用（外发链路空闲，不是"正常运行"）` : null;
  const qpsSeries = ch.ts?.length
    ? [{ name: 'QPS', color: '#1677ff', points: pts(ch.qps, true) },
       { name: 'QPS 峰值', color: '#722ed1', points: pts(ch.qps_max, true) }] : [];
  // 网关延迟分位 P50/P95/P99 改散点：生产是低流量，桶内样本 1~36，
  // 小样本桶的分位数≈单请求值，连线是误导；点大小 ∝ 桶内样本数 n
  const hasLat = (ch.p50 || []).some((v) => v != null);
  const latEmpty = !hasLat ? '窗口内无延迟样本（无经网关的请求）' : null;
  const latSeries = ch.ts?.length ? [
    { name: 'P50', color: '#52c41a', dots: true, n: nArr, points: pts(ch.p50, false) },
    { name: 'P95', color: '#fa8c16', dots: true, n: nArr, points: pts(ch.p95, false) },
    { name: 'P99', color: '#ff4d4f', dots: true, n: nArr, points: pts(ch.p99, false) },
  ] : [];
  const errColor = d && d.err != null
    ? (d.err >= 0.05 ? '#ff4d4f' : d.err > 0 ? '#fa8c16' : '#52c41a') : undefined;

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      {err && <Alert type="error" showIcon message={`总览刷新失败：${err}（显示上次数据）`} />}
      {!guideHidden && !guideSessionHidden && (
        <Alert type="info" showIcon message="初次使用指引"
          action={<Button size="small" type="link" style={{ padding: 0 }} onClick={dismissGuide}>不再提醒</Button>}
          description={
            <ul style={{ margin: '4px 0 0', paddingLeft: 18, lineHeight: 1.8 }}>
              <li>先在「API KEY 管理」登记客户端 KEY —— 未登记的 key 调 /v1/* 直接 401</li>
              <li>快速验证：「路由检查」页干跑（文本 / 文件都支持，不落审计），或 README 的三行 curl</li>
              <li>改规则 / 路由后先到「路由检查」页干跑验证（生产同款决策链，不产生真实流量）</li>
            </ul>
          }
          onClose={() => setGuideSessionHidden(true)} />
      )}
      {/* 时间窗：Portal 进标题右侧（#page-toolbar-slot），标题栏本就常驻；切页卸载自动清空 */}
      {typeof document !== 'undefined' && document.getElementById('page-toolbar-slot')
        ? createPortal((
          <Select size="small" value={hours} onChange={(v) => { setLoading(true); setHours(v); }} style={{ width: 130 }}
            options={RANGES.map((r) => ({ value: r.hours, label: r.label }))} />
        ), document.getElementById('page-toolbar-slot'))
        : null}
      {/* 切窗期间旧窗数据仍在（不清空），Spin 遮罩给出"在换"的反馈；
          KPI/图表/事件流三块各自是外层 Space 的 item（vertical middle=16px 块间 gap） */}
      <Spin spinning={loading}>
      <Row gutter={[16, 16]}>
        <Col flex={1} style={{ minWidth: 0 }}>
          <Kpi testid="ov-qps" title="当前 QPS" bar="#1677ff"
            value={d && d.qps != null ? d.qps.toFixed(1) : '-'} suffix="req/s"
            go="/stats" hint="跳统计页：QPS 趋势 / 分位延迟 / 运行指标" />
        </Col>
        <Col flex={1} style={{ minWidth: 0 }}>
          <Kpi testid="ov-err" title="错误率 5xx+429（近10tick）"
            value={d && d.err != null ? (d.err * 100).toFixed(1) + '%' : '-'}
            color={errColor} bar="#722ed1" go="/stats"
            hint="跳统计页：错误率趋势（5xx+429 近 10 tick 口径）" />
        </Col>
        <Col flex={1} style={{ minWidth: 0 }}>
          <Kpi testid="ov-block" title="拦截率"
            value={d && d.total > 0 ? (100 * d.blocked / d.total).toFixed(1) + '%' : '-'}
            color="#ff4d4f"
            go="/audit?action=block" hint="跳审计页：拦截明细（动作=block）" />
        </Col>
        <Col flex={1} style={{ minWidth: 0 }}>
          <Kpi testid="ov-route" title="本地路由率"
            value={d && d.total > 0 ? (100 * d.routeLocal / d.total).toFixed(1) + '%' : d ? '0.0%' : '-'}
            color={d && d.routeLocal > 0 ? '#fa8c16' : undefined}
            go="/audit?action=route_local" hint="跳审计页：本地路由明细（机密内容降级，不出境）" />
        </Col>
        <Col flex={1} style={{ minWidth: 0 }}>
          <Kpi testid="ov-tokens" title="Token 用量" bar="#13c2c2"
            value={d && d.tokens ? d.tokens.toLocaleString() : '-'} go="/stats?focus=billing"
            hint="跳统计页并定位：Token 计费（KEY × 金额 / 每日金额）" />
        </Col>
      </Row>
      </Spin>
      <Spin spinning={loading}>
      <Row gutter={[16, 16]}>
        <Col span={12} style={{ minWidth: 0 }}>
          <ChartPanel title="QPS">
            {qpsEmpty ? (
              <Alert type="info" showIcon message={qpsEmpty} />
            ) : hours <= 24 ? (
              <UPlotChart height={300} yVal={qpsAxis} series={qpsSeries} />
            ) : (
              <>
                <T type="secondary" style={{ fontSize: 11, display: 'block', marginBottom: 8 }}>
                  窗口 {winCalls} 次调用 · {(ch.ts || []).length} 桶仅 {nzBuckets} 桶有流量 → 日粒度（北京时区）
                </T>
                <DailyHeatmap points={dailyCounts} testid="ov-qps-daily" />
              </>
            )}
          </ChartPanel>
        </Col>
        <Col span={12} style={{ minWidth: 0 }}>
          <ChartPanel title="网关延迟分位 P50/P95/P99">
            {latEmpty ? (
              <Alert type="info" showIcon message={latEmpty} />
            ) : (
              <>
                {/* 与统计页同图同编码：点大小含义只在那里有提示，落地页补同款 */}
                <T type="secondary" style={{ fontSize: 11, display: 'block', marginBottom: 4 }}>
                  散点：点大小 ∝ 桶内样本数 n（小样本桶的分位数≈单请求值）
                </T>
                <UPlotChart height={300} yVal={msAxis} series={latSeries} />
              </>
            )}
          </ChartPanel>
        </Col>
      </Row>
      </Spin>
      {d.events && (
        <Spin spinning={loading}>
        <EventStreamCard events={d.events} />
        </Spin>
      )}
      <Row gutter={[16, 16]}>
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
