import { lazy, Suspense, useEffect, useState, useCallback, useRef, Component } from 'react';
import { Layout, Menu, Tag, Tooltip, Typography, Spin, Button, Alert, Space } from 'antd';
import { ThemeToggle, useTheme } from './theme.jsx';
import {
  KeyOutlined, StopOutlined, ApiOutlined, LockOutlined, BarChartOutlined,
  FileSearchOutlined, CloudServerOutlined,
  AuditOutlined, DashboardOutlined, SafetyCertificateOutlined, ExperimentOutlined,
  MenuFoldOutlined, MenuUnfoldOutlined, FileProtectOutlined,
} from '@ant-design/icons';

// 页面级代码分割：每个页面独立 chunk，首屏只下 shell，打开哪个页才加载哪个页。
// 注意：import 参数必须是字面量（变量路径 Rollup 拆不出 chunk，页面代码会整个消失）。

const { Header, Sider, Content } = Layout;
const { Text } = Typography;

// ---------------- 全局状态条：外网 key / 内网 / L2 / 熔断 / 审计，SSE 推送 ----------------
function StatusBar() {
  const [st, setSt] = useState(null);
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
    const open = Object.entries(c.providers || {}).filter(([, p]) => p.state && p.state !== 'closed').map(([n]) => n);
    const my = (a && a.mysql) || {};
    const dropped = (my.dropped || {}).total || 0;
    const auditBad = a ? !!(a.redis && a.redis.connected === false)
      || (my.enabled && (my.last_error || (my.err_count || 0) > 0)) : false;
    const auditTip = a ? `审计 ${a.backend}｜mysql err=${my.err_count || 0}${my.last_error ? '：' + my.last_error : ''}｜丢行 ${dropped}` : '审计状态未知';
    setSt({ extTotal: exts.length, extMissing, extDown, extDownTip: downTip(extDown),
            localMissing, localDown, localDownTip: downTip(localDown),
            localName: h.default_local, l2On: !!l2.SMALL_MODEL_ENABLED,
            l2Backend: l2.AI_GATEWAY_L2_BACKEND || 'qwen', l2Shadow: !!l2.AI_GATEWAY_L2_SHADOW,
             open, auditBad, dropped, auditTip });
  }, []);
  // 服务端每 60s 推一拍；EventSource 断线浏览器自带重连（401 时永远拿不到数据 → 整条不渲染）
  // 刷新靠 SSE 自动推送，无手动刷新按钮
  useEffect(() => {
    const es = new EventSource('/admin/api/status/stream');
    es.onmessage = (e) => {
      const raw = JSON.parse(e.data);
      if (!raw.error) apply(raw);
    };
    return () => es.close();
  }, [apply]);
  if (!st) return null;
  // 单行状态：L2 / 外网 / 内网 / 熔断 / 审计 并排，无流程图；节点色=状态，tooltip=详情
  const node = (color, critical, tip, children) => (
    <Tooltip title={tip}>
      <Tag color={color} className={critical ? 'status-critical' : undefined}
        style={{ marginInlineEnd: 0, borderRadius: 10, padding: '0 var(--gw-sp-8)', fontSize: 'var(--gw-fs-13)' }}>{children}</Tag>
    </Tooltip>
  );
  return (
    <div className="app-statusbar" style={{ display: 'flex', alignItems: 'center', gap: 'var(--gw-sp-8)', padding: 'var(--gw-sp-6) var(--gw-sp-24)',
      background: 'var(--gw-surface)', borderBottom: '1px solid var(--gw-border)', flexWrap: 'wrap' }}>
      <span style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 'var(--gw-sp-8)', flexWrap: 'wrap' }}>
      <Text type="secondary" style={{ fontSize: 'var(--gw-fs-12)' }}>网关运行状态</Text>
      {node(st.l2On ? 'green' : 'red', !st.l2On,
        `内容审查小模型（L2：判断内容是不是机密）｜当前用 ${st.l2Backend}｜影子 ${st.l2Shadow ? '开（并行打标，不改路由）' : '关'}｜审查小模型不可用时按配置放行或转内网`,
        `L2 审查·${st.l2Backend}${st.l2On && st.l2Shadow ? '·影子开' : ''}`)}
      {node((st.extMissing.length || st.extDown.length) ? 'orange' : 'green', false,
        [st.extDown.length
          ? `外网模型服务连不上（上游或网络问题，30s 探测一次）：${st.extDownTip.join('、')}`
          : '',
          st.extMissing.length
            ? `这几个外网服务还没配网关用的 key：${st.extMissing.join(', ')}（到「Provider 管理」页可一键配置）`
            : (!st.extDown.length ? '全部外网 provider 在线且 key 已配置' : ''),
        ].filter(Boolean).join('；'),
        `外网 key ${st.extTotal - st.extMissing.length}/${st.extTotal}${st.extDown.length ? ` 失联:${st.extDown.join(',')}` : ''}`)}
      {node((st.localDown.length || st.localMissing.length) ? 'red' : 'blue',
        st.localDown.length > 0 || st.localMissing.length > 0,
        st.localDown.length
          ? `内网模型服务连不上（模型没起来，30s 探测一次）：${st.localDownTip.join('、')}`
          : st.localMissing.length
            ? `这几个内网服务还没填地址（缺 base_url）：${st.localMissing.join(', ')}`
            : '内网 provider 均已配置且在线',
        `内网 ${st.localName}${st.localDown.length ? ` 失联:${st.localDown.join(',')}` : st.localMissing.length ? ` 缺${st.localMissing.join(',')}` : ''}`)}
      {st.open.length
        ? node('red', true, `熔断中：${st.open.join(', ')}`, `熔断/恢复中 ${st.open.length}（${st.open.join(', ')}）`)
        : node('green', false, '无熔断中', '无熔断')}
      {node(st.dropped > 0 ? 'orange' : st.auditBad ? 'red' : 'green',
        st.dropped > 0 || st.auditBad, `审计日志（每次调用的记录）写入情况：${st.auditTip}`,
        st.dropped > 0 ? `审计丢行 ${st.dropped}` : st.auditBad ? '审计/DB 异常' : '审计/DB 正常')}
      </span>
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
  review: lazy(() => import('./components/ReviewPage.jsx')),
  stats: lazy(() => import('./StatsPage.jsx')),
  l2: lazy(() => import('./components/L2Page.jsx')),
  audit: lazy(() => import('./components/AuditPage.jsx')),
  password: lazy(() => import('./components/PasswordPage.jsx')),
  cert: lazy(() => import('./components/CertGuidePage.jsx')),
};

const PAGE_TITLES = {
  overview: '总览',
  keys: 'API KEY 管理',
  keyrules: 'KEY 黑白名单',
  modelpolicy: '模型配置',
  provider: 'Provider 管理',
  rules: '规则管理',
  review: '规则测试与检查',
  stats: '统计可视化',
  l2: 'L2 影子',
  audit: '审计日志',
  password: '管理员密码',
  cert: '证书安装',
};

// ---------------- 懒加载失败兜底：防「偶发空白页」 ----------------
// 新构建会删掉旧 chunk 文件，而长开标签页（bfcache 回退/未刷新）里跑的还是旧 bundle
// ⇒ 切页时动态 import 指向已删哈希文件，浏览器只给 "Failed to fetch dynamically imported
// module"。Suspense 不接异常：没有 boundary 时 React 会把整棵树卸载 = 白屏。
// 识别出 chunk 加载失败就自动刷新一次（30s 内只刷一次，防真缺文件时死循环），
// 刷不动则给可点刷新的提示，页面其余部分照常渲染。
const CHUNK_RELOAD_KEY = 'gw-chunk-reload-at';
const isChunkLoadError = (err) => /dynamically imported module|Importing a module script failed|Failed to fetch/i
  .test(String((err && err.message) || err || ''));

class PageBoundary extends Component {
  state = { err: null };
  static getDerivedStateFromError(err) { return { err }; }
  componentDidCatch(err) {
    if (!isChunkLoadError(err)) return;
    const last = Number(sessionStorage.getItem(CHUNK_RELOAD_KEY) || 0);
    if (Date.now() - last > 30000) {
      sessionStorage.setItem(CHUNK_RELOAD_KEY, String(Date.now()));
      window.location.reload();
    }
  }
  render() {
    if (!this.state.err) return this.props.children;
    return (
      <div style={{ padding: 'var(--gw-sp-24)' }}>
        <Alert showIcon type="warning"
          message="页面加载失败：网关刚更新过，这个标签页还挂着旧版本"
          description="刷新即恢复（旧标签页里的代码指向了已被新构建删除的资源文件）。"
          action={<Space><Button size="small" type="primary"
            onClick={() => window.location.reload()}>刷新</Button></Space>} />
      </div>
    );
  }
}

// 侧边栏菜单项（单一真源）；PAGE_ICONS 从它派生 页→图标，供标题栏标题左侧显示当前页图标
const MENU_ITEMS = [
  { key: 'overview', icon: <DashboardOutlined />, label: '总览' },
  { type: 'group', label: '接入', children: [
    { key: 'keys', icon: <KeyOutlined />, label: 'API KEY 管理' },
  ]},
  { type: 'group', label: '安全策略', children: [
    { key: 'rules', icon: <AuditOutlined />, label: '规则管理' },
    { key: 'review', icon: <ExperimentOutlined />, label: '规则测试与检查' },
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
    { key: 'cert', icon: <FileProtectOutlined />, label: '证书安装' },
  ]},
];
const PAGE_ICONS = Object.fromEntries(
  MENU_ITEMS.flatMap((it) => it.children ?? [it]).map((it) => [it.key, it.icon]),
);

const readPage = () => {
  // hash 可带页内 query（如 #/audit?q=..），取 ? 前的段做页面路由
  const h = window.location.hash.replace(/^#\/?/, '').split('?')[0];
  return h in PAGES ? h : 'overview';
};

export default function App() {
  const [page, setPage] = useState(readPage);
  const { isDark } = useTheme();
  // 侧栏收起/展开（Sider 底部触发条切换）；收起 64 只留图标
  const [collapsed, setCollapsed] = useState(false);
  // 展开=折叠倒放：先保持图标模式扩宽度（200ms=Sider 宽度过渡），到位后菜单/标题才出现；
  // 收起反之：立即翻图标再缩宽。menuTimer 清掉未到的展开定时器，防快速连点串帧
  const [menuCollapsed, setMenuCollapsed] = useState(false);
  const menuTimer = useRef(null);
  const onSiderCollapse = (c) => {
    setCollapsed(c);
    if (menuTimer.current) { clearTimeout(menuTimer.current); menuTimer.current = null; }
    if (c) setMenuCollapsed(true);
    else {
      setMenuCollapsed(true);
      menuTimer.current = setTimeout(() => { setMenuCollapsed(false); menuTimer.current = null; }, 200);
    }
  };
  const siderW = collapsed ? 64 : 180;
  useEffect(() => {
    const onHash = () => setPage(readPage());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  // 路由切换后 content 懒加载/布局异步渲染，浏览器默认不滚动到顶部，导致定位错位：
  // 切页统一回顶，避免停留在上一页滚动高度
  useEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
  }, [page]);
  const nav = (key) => { window.location.hash = `/${key}`; };
  const Page = PAGES[page];
  // 顶栏真 fixed（sticky 每帧重算会抖，见侧边栏同因）：高度实测，内容区 paddingTop 让位，换行增高自动跟
  const topRef = useRef(null);
  const [topH, setTopH] = useState(0);
  useEffect(() => {
    if (!topRef.current) return undefined;
    const measure = () => {
      const h = topRef.current ? topRef.current.offsetHeight : 0;
      setTopH(h);
      // 表头 sticky 吸附位（theme.css .ant-table-sticky-holder）
      document.documentElement.style.setProperty('--app-topbar-h', `${h}px`);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(topRef.current);
    return () => ro.disconnect();
  }, []);
  return (
    <Layout style={{ minHeight: '100vh' }}>
      {/* 真 fixed：sticky 在 body 滚动 + flex 下每帧重算会抖，fixed 一次定位不再动 */}
       {/* Sider 主题跟当前深浅：深色下仍用 theme="light"，继承文字色是黑色（深底上不可见） */}
       <Sider theme={isDark ? 'dark' : 'light'} width={180} collapsedWidth={64} collapsible collapsed={collapsed}
        trigger={null}
        onCollapse={onSiderCollapse} style={{ position: 'fixed', left: 0, top: 0, bottom: 0,
        height: '100vh', overflowY: 'auto', overflowX: 'hidden', zIndex: 11 }}>
          <div style={{ height: 63, display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 var(--gw-sp-12)', fontWeight: 700, fontSize: 20 }}>
            {!menuCollapsed && '网关控制台'}
            <Button type="text" size="large" aria-label={collapsed ? '展开侧栏' : '收起侧栏'} icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />} onClick={() => onSiderCollapse(!collapsed)} />
          </div>
        <Menu mode="inline" inlineCollapsed={menuCollapsed} selectedKeys={[page]} onClick={(e) => nav(e.key)} items={MENU_ITEMS} />
      </Sider>
      <Layout style={{ marginLeft: siderW, paddingTop: topH, transition: 'margin-left 0.2s' }}>
        {/* 顶部常驻块：标题在上、状态条在下。真 fixed（sticky 滚动抖），高度实测后内容区让位。
            left 随侧栏宽度联动（CSS 里 180 只是兜底） */}
        <div className="app-topbar" ref={topRef} style={{ left: siderW, transition: 'left 0.2s' }}>
        <Header className="app-header" style={{ background: 'var(--gw-surface)', borderBottom: '1px solid var(--gw-border)',
          fontSize: 'var(--gw-fs-18)', height: 48, lineHeight: '48px', display: 'flex', alignItems: 'center', padding: '0px var(--gw-sp-24) 0px 36px' }}>
          {/* 当前页图标（派生自侧边栏菜单）在标题左侧 */}
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--gw-sp-8)' }}>
            {PAGE_ICONS[page]}{PAGE_TITLES[page]}
          </span>
          {/* 各页工具栏插槽（StatsPage 用 Portal 把控件挂到标题右边，随页卸载自动清空） */}
          <div id="page-toolbar-slot" style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center' }} />
          {/* 主题切换（跟随系统 → 浅色 → 深色 三态循环） */}
          <ThemeToggle />
        </Header>
        <StatusBar />
        </div>
        <Content style={{ padding: 'var(--gw-sp-8) 0' }}>
          <Suspense fallback={<div style={{ padding: 64, textAlign: 'center' }}><Spin /></div>}>
            <PageBoundary key={page}>
              <div key={page} className="page-enter"><Page /></div>
            </PageBoundary>
          </Suspense>
        </Content>
      </Layout>
    </Layout>
  );
}
