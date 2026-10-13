import { lazy, Suspense, useEffect, useState, useCallback, useRef } from 'react';
import { Layout, Menu, Button, Tag, Tooltip, Typography, Spin } from 'antd';
import {
  KeyOutlined, StopOutlined, ApiOutlined, LockOutlined, BarChartOutlined,
  FileSearchOutlined, ReloadOutlined, CloudServerOutlined,
  AuditOutlined, DashboardOutlined, SafetyCertificateOutlined,
} from '@ant-design/icons';

// 页面级代码分割：每个页面独立 chunk，首屏只下 shell，打开哪个页才加载哪个页。
// 注意：import 参数必须是字面量（变量路径 Rollup 拆不出 chunk，页面代码会整个消失）。

const { Header, Sider, Content } = Layout;
const { Text } = Typography;

// ---------------- 全局状态条：外网 key / 内网 / L2 / 熔断 / 审计，SSE 推送 ----------------
function StatusBar() {
  const [st, setSt] = useState(null);
  const [busy, setBusy] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const apply = useCallback((raw) => {
    const h = raw.health, c = raw.circuit, l2 = raw.l2 || {}, a = raw.audit;
    if (!h || !c) return;
    const provs = h.providers || {};
    // 外网 provider 全量 key 覆盖（缺的在 tooltip 列名字）
    const exts = Object.entries(provs).filter(([, p]) => !p.local);
    const extMissing = exts.filter(([, p]) => !p.configured).map(([n]) => n);
    const localMissing = Object.entries(provs).filter(([, p]) => p.local && !p.configured).map(([n]) => n);
    // 主动探测存活（provider_probe 后台每 30s 打 /models 的缓存）：
    // 「已配置但连不通」= 失联。首 30s 还没探到（lv[n] 不存在）不当失联，免得首屏误报。
    const lv = h.provider_liveness || {};
    const downOf = (list) => list.filter(([n, p]) => p.configured && lv[n] && lv[n].ok === false).map(([n]) => n);
    const downTip = (list) => list.map(([n]) => { const e = lv[n] || {}; return `${n}${e.error ? `（${e.error}）` : ''}`; });
    const localDown = downOf(Object.entries(provs).filter(([, p]) => p.local));
    const extDown = downOf(Object.entries(provs).filter(([, p]) => !p.local));
    const open = Object.entries(c.providers || {}).filter(([, p]) => p.state === 'open').map(([n]) => n);
    const my = (a && a.mysql) || {};
    const dropped = (my.dropped || {}).total || 0;
    const auditBad = a ? !!(a.redis && a.redis.connected === false)
      || (my.enabled && (my.last_error || (my.err_count || 0) > 0)) : false;
    const auditTip = a ? `审计 ${a.backend}｜mysql err=${my.err_count || 0}${my.last_error ? '：' + my.last_error : ''}｜丢行 ${dropped}` : '审计状态未知';
    setSt({ extTotal: exts.length, extMissing, extDown, extDownTip: downTip(extDown),
            localMissing, localDown, localDownTip: downTip(localDown),
            localName: h.default_local, l2On: !!l2.SMALL_MODEL_ENABLED,
            l2Backend: l2.AI_GATEWAY_L2_BACKEND || 'qwen', l2Shadow: !!l2.AI_GATEWAY_L2_SHADOW,
            open, auditBad, dropped, auditTip,
            updatedAt: new Date() });
  }, []);
  // 服务端每 60s 推一拍；EventSource 断线浏览器自带重连（401 时永远拿不到数据 → 整条不渲染）
  useEffect(() => {
    setBusy(true);
    const es = new EventSource('/admin/api/status/stream');
    es.onopen = () => setBusy(false);
    es.onmessage = (e) => {
      setBusy(false);
      const raw = JSON.parse(e.data);
      if (!raw.error) apply(raw);
    };
    es.onerror = () => setBusy(false);
    return () => es.close();
  }, [apply, reloadKey]);
  if (!st) return null;
  // 流程图：L2 判定 → 路由分支（外网/内网）→ 熔断 → 审计；节点色=状态，tooltip=详情
  const node = (color, critical, tip, children) => (
    <Tooltip title={tip}>
      <Tag color={color} className={critical ? 'status-critical' : undefined}
        style={{ marginInlineEnd: 0, borderRadius: 10, padding: '0 10px' }}>{children}</Tag>
    </Tooltip>
  );
  const arrow = <span style={{ color: '#000', fontSize: 26 }}>→</span>;
  return (
    <div className="app-statusbar" style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 24px',
      background: '#fff', borderBottom: '1px solid #f0f0f0', flexWrap: 'wrap' }}>
      <Text type="secondary" style={{ fontSize: 12 }}>网关运行状态</Text>
      <Button size="small" type="text" aria-label="刷新状态" icon={<ReloadOutlined />} bordered
        loading={busy} onClick={() => setReloadKey((k) => k + 1)} />
      {node(st.l2On ? 'green' : 'red', !st.l2On,
        `权威后端 ${st.l2Backend}｜影子 ${st.l2Shadow ? '开（并行打标，不改路由）' : '关'}｜关闭时 L2 不可用将放行（AI_GATEWAY_L2_FAIL_MODE）`,
        `L2 审查·${st.l2Backend}${st.l2On && st.l2Shadow ? '·影子开' : ''}`)}
      {arrow}
      <span style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        {node((st.extMissing.length || st.extDown.length) ? 'orange' : 'green', false,
          [st.extDown.length
             ? `外网 provider 失联（上游/网络，30s 探测）：${st.extDownTip.join('、')}`
             : '',
           st.extMissing.length
             ? `未配网关 key：${st.extMissing.join(', ')}（Provider 管理页可一键配置）`
             : (!st.extDown.length ? '全部外网 provider 在线且 key 已配置' : ''),
          ].filter(Boolean).join('；'),
          `外网 key ${st.extTotal - st.extMissing.length}/${st.extTotal}${st.extDown.length ? ` 失联:${st.extDown.join(',')}` : ''}`)}
        {node((st.localDown.length || st.localMissing.length) ? 'red' : 'blue',
          st.localDown.length > 0 || st.localMissing.length > 0,
          st.localDown.length
            ? `内网 provider 失联（模型没起来，30s 探测）：${st.localDownTip.join('、')}`
            : st.localMissing.length
              ? `未配置（缺 base_url）的内网 provider：${st.localMissing.join(', ')}`
              : '内网 provider 均已配置且在线',
          `内网 ${st.localName}${st.localDown.length ? ` 失联:${st.localDown.join(',')}` : st.localMissing.length ? ` 缺${st.localMissing.join(',')}` : ''}`)}
      </span>
      {arrow}
      {st.open.length
        ? node('red', true, `熔断 open：${st.open.join(', ')}`, `熔断 ${st.open.length}（${st.open.join(', ')}）`)
        : node('green', false, '全部熔断闭合', '熔断正常')}
      {arrow}
      {node(st.dropped > 0 ? 'orange' : st.auditBad ? 'red' : 'green',
        st.dropped > 0 || st.auditBad, st.auditTip,
        st.dropped > 0 ? `审计丢行 ${st.dropped}` : st.auditBad ? '审计/DB 异常' : '审计/DB 正常')}
      {st.updatedAt && (
        <Text type="secondary" style={{ fontSize: 12, marginLeft: 'auto' }}>
          更新于 {st.updatedAt.toLocaleTimeString('zh-CN', { hour12: false })}
        </Text>
      )}
    </div>
  );
}

const PAGES = {
  overview: lazy(() => import('./OverviewPage.jsx')),
  keys: lazy(() => import('./components/KeysPage.jsx')),
  keyrules: lazy(() => import('./components/KeyRulesPage.jsx')),
  modelpolicy: lazy(() => import('./components/ModelPolicyPage.jsx')),
  provider: lazy(() => import('./components/ProviderPage.jsx')),
  rules: lazy(() => import('./components/RulesPage.jsx')),
  stats: lazy(() => import('./StatsPage.jsx')),
  l2: lazy(() => import('./components/L2Page.jsx')),
  audit: lazy(() => import('./components/AuditPage.jsx')),
  password: lazy(() => import('./components/PasswordPage.jsx')),
};

const PAGE_TITLES = {
  overview: '总览',
  keys: 'API KEY 管理',
  keyrules: 'KEY 黑白名单',
  modelpolicy: '模型配置',
  provider: 'Provider 管理',
  rules: '规则管理',
  stats: '统计可视化',
  l2: 'L2 影子',
  audit: '审计日志',
  password: '管理员密码',
};

const readPage = () => {
  // hash 可带页内 query（如 #/audit?q=..），取 ? 前的段做页面路由
  const h = window.location.hash.replace(/^#\/?/, '').split('?')[0];
  return h in PAGES ? h : 'overview';
};

export default function App() {
  const [page, setPage] = useState(readPage);
  useEffect(() => {
    const onHash = () => setPage(readPage());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  const nav = (key) => { window.location.hash = `/${key}`; };
  const Page = PAGES[page];
  // 顶栏真 fixed（sticky 每帧重算会抖，见侧边栏同因）：高度实测，内容区 paddingTop 让位，换行增高自动跟
  const topRef = useRef(null);
  const [topH, setTopH] = useState(0);
  useEffect(() => {
    if (!topRef.current) return undefined;
    const measure = () => setTopH(topRef.current ? topRef.current.offsetHeight : 0);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(topRef.current);
    return () => ro.disconnect();
  }, []);
  return (
    <Layout style={{ minHeight: '100vh' }}>
      {/* 真 fixed：sticky 在 body 滚动 + flex 下每帧重算会抖，fixed 一次定位不再动 */}
      <Sider theme="light" width={200} style={{ position: 'fixed', left: 0, top: 0, bottom: 0,
        height: '100vh', overflowY: 'auto', overflowX: 'hidden', zIndex: 11 }}>
          <div style={{ padding: 16, fontWeight: 700, fontSize: 20, textAlign: 'center' }}>网关管理控制台</div>
        <Menu mode="inline" selectedKeys={[page]} onClick={(e) => nav(e.key)} items={[
          { key: 'overview', icon: <DashboardOutlined />, label: '总览' },
          { type: 'group', label: '接入', children: [
            { key: 'keys', icon: <KeyOutlined />, label: 'API KEY 管理' },
          ]},
          { type: 'group', label: '安全策略', children: [
            { key: 'rules', icon: <AuditOutlined />, label: '规则管理' },
            { key: 'keyrules', icon: <StopOutlined />, label: 'KEY 黑白名单' },
          ]},
          { type: 'group', label: '路由与模型', children: [
            { key: 'modelpolicy', icon: <ApiOutlined />, label: '模型配置' },
            { key: 'provider', icon: <CloudServerOutlined />, label: 'Provider 管理' },
          ]},
          { type: 'group', label: '观测', children: [
            { key: 'stats', icon: <BarChartOutlined />, label: '统计可视化' },
            { key: 'l2', icon: <SafetyCertificateOutlined />, label: 'L2 影子' },
            { key: 'audit', icon: <FileSearchOutlined />, label: '审计日志' },
          ]},
          { type: 'group', label: '系统', children: [
            { key: 'password', icon: <LockOutlined />, label: '管理员密码' },
          ]},
        ]} />
      </Sider>
      <Layout style={{ marginLeft: 200, paddingTop: topH }}>
        {/* 顶部常驻块：状态条在上、标题在下。真 fixed（sticky 滚动抖），高度实测后内容区让位 */}
        <div className="app-topbar" ref={topRef}>
        <StatusBar />
        <Header className="app-header" style={{ background: '#fff', borderBottom: '1px solid #f0f0f0',
          fontSize: 18, height: 48, lineHeight: '48px', display: 'flex', alignItems: 'center', padding: '0px 24px 0px 36px' }}>
          <span>{PAGE_TITLES[page]}</span>
          {/* 各页工具栏插槽（StatsPage 用 Portal 把控件挂到标题右边，随页卸载自动清空） */}
          <div id="page-toolbar-slot" style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center' }} />
        </Header>
        </div>
        <Content style={{ padding: 16 }}>
          <Suspense fallback={<div style={{ padding: 64, textAlign: 'center' }}><Spin /></div>}>
            <Page />
          </Suspense>
        </Content>
      </Layout>
    </Layout>
  );
}
