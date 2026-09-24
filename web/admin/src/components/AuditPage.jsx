import React, { useEffect, useState, useCallback, useMemo } from 'react';
import { Table, Button, Input, Select, Tag, Space, message, Typography, Card, Statistic } from 'antd';
import { ReloadOutlined, SearchOutlined, DownloadOutlined, CopyOutlined } from '@ant-design/icons';
import { api, errText, fetchJson, copyText } from '../api.js';

const { Text } = Typography;
import { ruleZh, RULE_ZH } from '../ruleNames.jsx';
// ---------------- 审计面板并入（原 /admin /analytics /metrics/panel HTML 页退役迁入） ----------------

const ACTION_BADGE = (a) => (a === 'block' ? <Tag color="red">阻断</Tag>
  : a === 'route_local' ? <Tag color="orange">本地路由</Tag>
    : a === 'allow' ? <Tag color="green">放行</Tag> : <Tag>{a || '—'}</Tag>);

const isLocal = (v) => v === true || v === 'true' || v === 1 || v === '1';

// L2 展开行：主判定 + laya 影子（shadow_*）。无 L2 的行不可展开。
// l2 可能是对象（新接口）或字符串（容错），统一归一化再渲染。
const normL2 = (l2) => {
  if (!l2) return null;
  if (typeof l2 === 'string') { try { return JSON.parse(l2); } catch { return { raw: l2 }; } }
  return l2;
};
const L2Detail = ({ rec }) => {
  const l2 = normL2(rec.l2);
  if (!l2) return <Text type="secondary">无 L2 判定（L1 直判，未进语义审查）</Text>;
  const hasShadow = l2.shadow_label || l2.shadow_conf !== undefined || l2.shadow_degraded;
  return (
    <Space direction="vertical" size={2} style={{ fontSize: 12 }}>
      <Space wrap>
        <Text strong>主判定：</Text>
        <Tag color={l2.label === 'CONFIDENTIAL' ? 'red' : 'green'}>{l2.label || '?'}</Tag>
        <Text type="secondary">conf={l2.confidence ?? '?'}</Text>
        <Text type="secondary">{l2.reason || ''}</Text>
        {l2.latency_ms !== undefined && <Text type="secondary">{l2.latency_ms}ms</Text>}
        {l2.cached && <Tag>缓存</Tag>}
        {l2.degraded && <Tag color="orange">降级:{l2.degraded_reason || ''}</Tag>}
      </Space>
      <Space wrap>
        <Text strong>影子(laya)：</Text>
        {!hasShadow && <Text type="secondary">无影子数据（影子未开 / backend=laya / 未进 L2）</Text>}
        {l2.shadow_degraded && <Tag color="orange">影子失败:{String(l2.shadow_degraded).slice(0, 80)}</Tag>}
        {l2.shadow_label && (
          <>
            <Tag color={l2.shadow_label === 'CONFIDENTIAL' ? 'red' : 'green'}>{l2.shadow_label}</Tag>
            <Text type="secondary">conf={l2.shadow_conf ?? '?'}</Text>
            <Text type="secondary">{l2.shadow_reason || ''}</Text>
            {String(l2.label || '') !== String(l2.shadow_label || '') && <Tag color="purple">与主判定不一致</Tag>}
          </>
        )}
      </Space>
    </Space>
  );
};

// ---------------- 审计日志（原 /admin 审计页） ----------------
const AUDIT_ACTIONS = [
  { value: '', label: '全部动作' },
  { value: 'allow', label: 'allow 放行' },
  { value: 'route_local', label: 'route_local 本地路由' },
  { value: 'block', label: 'block 拦截' },
];
const AUDIT_RANGES = [
  { value: '', label: '全部时间' },
  { value: '1h', label: '最近 1 小时' },
  { value: '24h', label: '最近 24 小时' },
  { value: '7d', label: '最近 7 天' },
  { value: '30d', label: '最近 30 天' },
];

export default function AuditPage() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  // 筛选态入 URL（#/audit?since=..&action=..&rule=..&q=..）：刷新留条件、可分享
  const [f, setF] = useState(() => {
    const p = new URLSearchParams(window.location.hash.split('?')[1] || '');
    return { action: p.get('action') || '', since: p.get('since') || '',
             rule: p.get('rule') || '', q: p.get('q') || '', shadow: p.get('shadow') || '' };
  });
  const [pageNo, setPageNo] = useState(1);
  const writeUrl = (next) => {
    const p = new URLSearchParams();
    Object.entries(next).forEach(([k, v]) => { if (v) p.set(k, v); });
    const qs = p.toString();
    history.replaceState(null, '', '#/audit' + (qs ? '?' + qs : ''));
  };
  // 规则名下拉来源：与「规则管理」页同源（/admin/api/policies），避免前端硬编码漂移
  const [ruleNames, setRuleNames] = useState([]);

  const load = useCallback(async (page = pageNo) => {
    setLoading(true);
    try {
      const p = new URLSearchParams();
      Object.entries(f).forEach(([k, v]) => { if (v) p.set(k, v); });
      if (page > 1) p.set('page', String(page));
      setData(await fetchJson('/admin/audit/entries?' + p.toString()));
    } catch (e) { message.error(errText(e)); }
    setLoading(false);
  }, [f, pageNo]);

  useEffect(() => { load(); }, [load]);

  // 规则名清单拉取：失败时静默回退到内置中文映射（RULE_ZH），不影响筛选可用性
  useEffect(() => {
    api('/policies')
      .then((r) => setRuleNames((r.items || []).map((x) => x.name).filter(Boolean)))
      .catch(() => {});
  }, []);

  const ruleOptions = useMemo(() => {
    const names = ruleNames.length ? ruleNames : Object.keys(RULE_ZH);
    return names.map((v) => ({ value: v, label: RULE_ZH[v] ? RULE_ZH[v] + '（' + v + '）' : v }));
  }, [ruleNames]);

  const search = () => { writeUrl(f); setPageNo(1); load(1); };
  const exportCsv = () => {
    const p = new URLSearchParams();
    Object.entries(f).forEach(([k, v]) => { if (v) p.set(k, v); });
    window.open('/admin/csv?' + p.toString(), '_blank');
  };

  const setFld = (k) => (e) => { const next = { ...f, [k]: e.target.value }; setF(next); writeUrl(next); };

  const cols = [
    { title: '时间', dataIndex: 'time', width: 165 },
    { title: '动作', dataIndex: 'action', width: 130,
      render: (v, rec) => (
        <Space size={4}>
          {ACTION_BADGE(v)}
          {v === 'allow' && isLocal(rec.local) && <Tag color="blue">local</Tag>}
        </Space>
      ) },
    { title: '规则', dataIndex: 'rule', width: 170, ellipsis: true, render: (v) => <Text code title={v || ''}>{ruleZh(v)}</Text> },
    { title: 'Provider', dataIndex: 'provider', width: 110, render: (v) => v || '—' },
    { title: '模型', dataIndex: 'model', width: 150, ellipsis: true, render: (v) => v || '—' },
    { title: '调用方名称', dataIndex: 'key_name', width: 130,
      render: (v, r) => { const name = v || r.token_masked || ''; return name ? <Text code title={r.token_masked || name}>{name}</Text> : '—'; } },
    { title: '客户端 IP', dataIndex: 'client_ip', width: 120, render: (v) => v || '—' },
    { title: '文件名', dataIndex: 'filename', width: 140, ellipsis: true, render: (v) => v || '—' },
    { title: '内容摘要', dataIndex: 'text_preview',
      render: (v) => (
        <span style={{ display: 'flex', alignItems: 'center', gap: 4, maxWidth: '100%' }}>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0, flex: 1 }}
            title={v}>{v || '—'}</span>
          {v && <Button type="text" size="small" icon={<CopyOutlined />} aria-label="复制内容摘要"
            onClick={() => copyText(v)} />}
        </span>
      ) },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card>
        <Space wrap>
          <Select style={{ width: 140 }} value={f.since} options={AUDIT_RANGES}
            onChange={(v) => { const next = { ...f, since: v }; setF(next); setPageNo(1); writeUrl(next); }} />
          <Select style={{ width: 165 }} value={f.action} options={AUDIT_ACTIONS}
            onChange={(v) => { const next = { ...f, action: v }; setF(next); setPageNo(1); writeUrl(next); }} />
          <Select style={{ width: 190 }} value={f.rule || undefined} options={ruleOptions} allowClear showSearch
            optionFilterProp="label" placeholder="规则名"
            onChange={(v) => { const next = { ...f, rule: v || '' }; setF(next); setPageNo(1); writeUrl(next); }} />
          <Select style={{ width: 150 }} value={f.shadow}
            options={[{ value: '', label: '影子：全部' }, { value: 'mismatch', label: '主影不一致' }]}
            onChange={(v) => { const next = { ...f, shadow: v || '' }; setF(next); setPageNo(1); writeUrl(next); }} />
          <Input style={{ width: 210 }} placeholder="全文搜索（内容/IP/调用方/模型/L2/影子…）" allowClear value={f.q} onChange={setFld('q')} onPressEnter={search} />
          <Button type="primary" icon={<SearchOutlined />} onClick={search}>查询</Button>
          <Button icon={<DownloadOutlined />} onClick={exportCsv}>导出 CSV</Button>
          <Button aria-label="刷新" icon={<ReloadOutlined />} onClick={() => load()} />
        </Space>
      </Card>
      {data && (
        <Space size="large" wrap>
          <Statistic data-testid="audit-stat-total" title="命中总数" value={data.total} />
          <Statistic data-testid="audit-stat-allow" title="放行" value={data.c_allow} valueStyle={{ color: '#52c41a' }} />
          <Statistic data-testid="audit-stat-route" title="本地路由" value={data.c_route} valueStyle={{ color: '#faad14' }} />
          <Statistic data-testid="audit-stat-block" title="阻断" value={data.c_block} valueStyle={{ color: '#ff4d4f' }} />
          <Statistic data-testid="audit-stat-local" title="本地处理" value={data.c_local} />
          <Statistic data-testid="audit-stat-ext" title="外部处理" value={data.c_ext} />
        </Space>
      )}
      <Table rowKey={(_, i) => i} columns={cols} dataSource={data?.entries || []} loading={loading} size="small"
        locale={{ emptyText: '当前筛选无审计记录：可扩大时间范围、清空筛选条件，或换关键词试试' }}
        expandable={{ expandedRowRender: (rec) => <L2Detail rec={rec} />,
          rowExpandable: (rec) => !!normL2(rec.l2) }} 
        pagination={{
          current: data?.page || 1, pageSize: data?.limit || 50, total: data?.total || 0,
          onChange: (pg) => setPageNo(pg), showSizeChanger: false,
          showTotal: (t, rg) => `第 ${rg[0]}-${rg[1]} 条 / 共 ${t} 条`,
        }} />
    </Space>
  );
}

