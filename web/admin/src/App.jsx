import React, { useEffect, useState, useCallback } from 'react';
import { Layout, Menu, Button, Tag, Tooltip, Typography } from 'antd';
import {
  KeyOutlined, StopOutlined, ApiOutlined, LockOutlined, BarChartOutlined,
  FileSearchOutlined, FundOutlined, ReloadOutlined, ExperimentOutlined, CloudServerOutlined,
  AuditOutlined, DashboardOutlined,
} from '@ant-design/icons';
import OverviewPage from './OverviewPage.jsx';
import StatsPage from './StatsPage.jsx';
import MetricsPage from './MetricsPage.jsx';
import KeysPage from './components/KeysPage.jsx';
import KeyRulesPage from './components/KeyRulesPage.jsx';
import PasswordPage from './components/PasswordPage.jsx';
import ModelPolicyPage from './components/ModelPolicyPage.jsx';
import ProviderPage from './components/ProviderPage.jsx';
import DiagnosePage from './components/DiagnosePage.jsx';
import RulesPage from './components/RulesPage.jsx';
import AuditPage from './components/AuditPage.jsx';

const { Header, Sider, Content } = Layout;
const { Text } = Typography;

// ---------------- 全局状态条：外网 key / 内网 / L2 / 熔断 / 策略数，60s 自动刷新 ----------------
function StatusBar() {
  const [st, setSt] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    setBusy(true);
    try {
      const [h, c, l2, a] = await Promise.all([
        fetch('/health').then((r) => r.json()),
        fetch('/admin/api/circuit').then((r) => r.json()),
        fetch('/admin/api/l2-config').then((r) => r.json()),
        fetch('/admin/audit/status').then((r) => r.json()).catch(() => null),
      ]);
      const ext = (h.providers || {})[h.default_external];
      const open = Object.entries(c.providers || {}).filter(([, p]) => p.state === 'open').map(([n]) => n);
      const my = (a && a.mysql) || {};
      const auditBad = a ? !!(a.redis && a.redis.connected === false)
        || (my.enabled && (my.last_error || (my.err_count || 0) > 0)) : false;
      const auditTip = a ? `审计 ${a.backend}｜mysql err=${my.err_count || 0}${my.last_error ? '：' + my.last_error : ''}` : '审计状态未知';
      setSt({ extName: h.default_external, extOk: !!(ext && ext.configured),
              localName: h.default_local, l2On: !!l2.SMALL_MODEL_ENABLED,
              open, policies: h.policy_count, auditBad, auditTip,
              updatedAt: new Date() });
    } catch (e) { setSt(null); }
    setBusy(false);
  }, []);
  useEffect(() => {
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, [load]);
  if (!st) return null;
  const light = (on, tOn, tOff) => on
    ? <Tag color="green" style={{ marginInlineEnd: 0 }}>{tOn}</Tag>
    : <Tag color="red" className="status-critical" style={{ marginInlineEnd: 0 }}>{tOff}</Tag>;
  return (
    <div className="app-statusbar" style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 24px',
      background: '#fff', borderBottom: '1px solid #f0f0f0', flexWrap: 'wrap' }}>
      <Text type="secondary" style={{ fontSize: 12 }}>状态</Text>
      <Tooltip title={st.extOk ? '默认外网 provider 网关 env key 已配置'
        : '默认外网 provider 未配 key：外发将 503（Provider 管理页可一键配置，即时生效）'}>
        {light(st.extOk, `外网 ${st.extName} key已配`, `外网 ${st.extName} 缺key`)}
      </Tooltip>
      <Tag color="blue" style={{ marginInlineEnd: 0 }}>内网 {st.localName}</Tag>
      {light(st.l2On, 'L2 审查开', 'L2 审查关')}
      {st.open.length
        ? <Tag color="red" className="status-critical" style={{ marginInlineEnd: 0 }}>熔断 {st.open.length}（{st.open.join(', ')}）</Tag>
        : <Tag color="green" style={{ marginInlineEnd: 0 }}>熔断正常</Tag>}
      <Tag style={{ marginInlineEnd: 0 }}>策略 {st.policies}</Tag>
      <Tooltip title={st.auditTip}>
        {light(!st.auditBad, '审计/DB 正常', '审计/DB 异常')}
      </Tooltip>
      {st.updatedAt && (
        <Text type="secondary" style={{ fontSize: 12, marginLeft: 'auto' }}>
          更新于 {st.updatedAt.toLocaleTimeString('zh-CN', { hour12: false })}
        </Text>
      )}
      <Button size="small" type="text" aria-label="刷新状态" icon={<ReloadOutlined />}
        loading={busy} onClick={load} />
    </div>
  );
}

const PAGES = {
  overview: <OverviewPage />,
  keys: <KeysPage />,
  keyrules: <KeyRulesPage />,
  modelpolicy: <ModelPolicyPage />,
  provider: <ProviderPage />,
  diagnose: <DiagnosePage />,
  rules: <RulesPage />,
  stats: <StatsPage />,
  audit: <AuditPage />,
  metrics: <MetricsPage />,
  password: <PasswordPage />,
};

const PAGE_TITLES = {
  overview: '总览',
  keys: 'API KEY 管理',
  keyrules: 'KEY 黑白名单',
  modelpolicy: '模型配置',
  provider: 'Provider 管理',
  diagnose: '路由检查',
  rules: '规则管理',
  stats: '统计可视化',
  audit: '审计日志',
  metrics: '运行指标',
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
  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider theme="light" width={200} style={{ position: 'sticky', top: 0,
        height: '100vh', overflow: 'auto' }}>
        <div style={{ padding: 16, fontWeight: 700 }}>网关管理控制台</div>
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
            { key: 'diagnose', icon: <ExperimentOutlined />, label: '路由检查' },
            { key: 'stats', icon: <BarChartOutlined />, label: '统计可视化' },
            { key: 'audit', icon: <FileSearchOutlined />, label: '审计日志' },
            { key: 'metrics', icon: <FundOutlined />, label: '运行指标' },
          ]},
          { type: 'group', label: '系统', children: [
            { key: 'password', icon: <LockOutlined />, label: '管理员密码' },
          ]},
        ]} />
      </Sider>
      <Layout>
        <Header className="app-header" style={{ background: '#fff', borderBottom: '1px solid #f0f0f0', fontSize: 18 }}>
          {PAGE_TITLES[page]}
        </Header>
        <StatusBar />
        <Content style={{ padding: 24 }}>{PAGES[page]}</Content>
      </Layout>
    </Layout>
  );
}
