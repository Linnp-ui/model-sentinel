import { useState, useEffect, useCallback, useRef } from 'react';
import { Card, Table, Button, Select, Space, Tag, Typography, InputNumber, Popconfirm, message } from 'antd';
import { DatabaseOutlined, SearchOutlined } from '@ant-design/icons';
import { api, errText, RANGES } from '../api.js';
import { wideSticky } from '../utils.js';
import { Tip } from '../terms.jsx';
import { ACTION_INFO } from '../ruleNames.jsx';

const { Text } = Typography;

// 明细库（request_log）：统计/建议引擎的数据源，与审计日志页的 5000 条热窗
// 是**两套存储**——这里查的是 MySQL 明细全量（含延迟/token 分解），无内容字段、不可展开。
// 筛选态入 URL（#/logs?hours=..&key_name=..&model=..&action=..&ip=..&provider=..&status_code=..）：
// 刷新留条件、可分享。参数名与后端 /logs 端点一致（旧链接的 key= 仍兼容）。
const LOG_ACTIONS = [
  { value: 'allow', label: '放行' },
  { value: 'route_local', label: '本地路由' },
  { value: 'block', label: '拦截' },
];
const LOG_STATUSES = [
  { value: '200', label: '200 成功' },
  { value: '400', label: '400 参数错' },
  { value: '401', label: '401 未鉴权' },
  { value: '403', label: '403 拦截' },
  { value: '404', label: '404 不存在' },
  { value: '413', label: '413 过大' },
  { value: '422', label: '422 不可处理' },
  { value: '429', label: '429 限流' },
  { value: '500', label: '500 内部错' },
  { value: '502', label: '502 上游错' },
  { value: '503', label: '503 不可用' },
];

const cols = [
  { title: '时间', dataIndex: 'ts', width: '12%', align: 'center', ellipsis: true, className: 'cell-center' },
  { title: '调用方', dataIndex: 'key_name', width: '13%', ellipsis: true,
    render: (v) => v || <Text type="secondary">—</Text> },
  { title: '模型', dataIndex: 'model', width: '13%', align: 'center', ellipsis: true, className: 'cell-center',
    render: (v) => v || '—' },
  { title: 'Provider', dataIndex: 'provider', width: '11%', ellipsis: true,
    render: (v) => v || '—' },
  { title: '动作', dataIndex: 'action', width: '10%', align: 'center',
    render: (v) => { const [name, color] = ACTION_INFO[v] || [v || '—', undefined]; return <Tag color={color} style={{ marginInlineEnd: 0 }}>{name}</Tag>; } },
  { title: '状态', dataIndex: 'status_code', width: '7%', align: 'center',
    render: (v) => v || '—' },
  { title: '耗时(ms)', dataIndex: 'duration_ms', width: '11%', align: 'center',
    render: (v, r) => <span title={`网关自身 ${r.gateway_internal_ms ?? 0} / 上游 ${r.upstream_ms ?? 0}`}>{v ?? 0}</span> },
  { title: 'Token（入/出/缓存）', width: '23%', align: 'center',
    render: (_, r) => {
      const p = r.prompt_tokens || 0, c = r.completion_tokens || 0, k = r.cached_tokens || 0;
      if (!p && !c) return <Text type="secondary">—</Text>;
      return <Text>{p} / {c}{k ? <Text type="secondary"> / {k}c</Text> : null}</Text>;
    } },
];

export default function LogsPage() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [f, setF] = useState(() => {
    const p = new URLSearchParams(window.location.hash.split('?')[1] || '');
    return { hours: p.get('hours') || '168', key_name: p.get('key_name') || p.get('key') || '',
             model: p.get('model') || '', action: p.get('action') || '',
             ip: p.get('ip') || '', provider: p.get('provider') || '',
             status_code: p.get('status_code') || '' };
  });
  const [pageNo, setPageNo] = useState(1);
  const [cleanDays, setCleanDays] = useState(90);
  const [cleaning, setCleaning] = useState(false);
  // 下拉选项 = 时间窗内 distinct 值（/logs/facets）；换时间窗重新拉
  const [facets, setFacets] = useState({ key_name: [], model: [], provider: [], client_ip: [] });
  const loadFacets = useCallback(async (hours) => {
    try { setFacets(await api(`/logs/facets?hours=${hours}`)); } catch { /* 选项拉取失败不阻塞列表 */ }
  }, []);
  useEffect(() => { loadFacets(f.hours); }, [f.hours, loadFacets]);
  const writeUrl = (next) => {
    const p = new URLSearchParams();
    Object.entries(next).forEach(([k, v]) => { if (v) p.set(k, v); });
    const qs = p.toString();
    history.replaceState(null, '', '#/logs' + (qs ? '?' + qs : ''));
  };
  const fRef = useRef(f);
  fRef.current = f;

  const load = useCallback(async (page = pageNo) => {
    setLoading(true);
    try {
      const p = new URLSearchParams();
      Object.entries(fRef.current).forEach(([k, v]) => { if (v) p.set(k, v); });
      p.set('limit', '50');
      p.set('offset', String((page - 1) * 50));
      setData(await api('/logs?' + p.toString()));
    } catch (e) { message.error(errText(e)); }
    finally { setLoading(false); }
  }, [pageNo]);

  useEffect(() => { load(); }, [load, f.hours, f.action]);

  const search = () => { setPageNo(1); load(1); };
  // 窗口 distinct 值下拉（内置搜索）：选中即查，与动作/时间窗同口径即时生效
  const facetSel = (k, label, vals, w) => (
    <Select style={{ width: w }} showSearch allowClear optionFilterProp="label"
      placeholder={label} value={f[k] || undefined}
      options={vals.map((v) => ({ value: v, label: v }))}
      onChange={(v) => { const next = { ...f, [k]: v || '' }; setF(next); writeUrl(next); setPageNo(1); load(1); }} />
  );

  const cleanup = async () => {
    setCleaning(true);
    try {
      const d = await api(`/logs/cleanup?days=${cleanDays}`, { method: 'POST' });
      message.success(`已清理 ${d.deleted} 行`);
      load(pageNo);
    } catch (e) { message.error(errText(e)); }
    finally { setCleaning(false); }
  };

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card>
        <Space wrap>
          <Select style={{ width: 140 }} value={f.hours}
            options={RANGES.map((r) => ({ value: String(r.hours), label: r.label }))}
            onChange={(v) => { const next = { ...f, hours: v }; setF(next); setPageNo(1); writeUrl(next); }} />
          <Select style={{ width: 120 }} value={f.action || undefined} options={LOG_ACTIONS} allowClear placeholder="动作"
            onChange={(v) => { const next = { ...f, action: v || '' }; setF(next); setPageNo(1); writeUrl(next); }} />
          {facetSel('key_name', '调用方', facets.key_name, 180)}
          {facetSel('model', '模型', facets.model, 170)}
          {facetSel('ip', '客户端 IP', facets.client_ip, 140)}
          {facetSel('provider', 'Provider', facets.provider, 150)}
          <Select style={{ width: 120 }} value={f.status_code || undefined} options={LOG_STATUSES} allowClear placeholder="状态码"
            onChange={(v) => { const next = { ...f, status_code: v || '' }; setF(next); writeUrl(next); setPageNo(1); load(1); }} />
          <Button type="primary" icon={<SearchOutlined />} onClick={search}>查询</Button>
        </Space>
      </Card>
      <Card title={<><DatabaseOutlined /> 请求明细<Tip text="数据源是 MySQL 明细库，与审计页热窗不同；仅调用元数据，行不可展开。" /></>}
        extra={
          <Space wrap>
            <Text type="secondary" style={{ fontSize: 'var(--gw-fs-12)' }}>查调用耗时 / token / 成本（90 天全量）；查为什么拦/转内网去 <a onClick={() => { window.location.hash = '/audit'; }}>审计日志</a></Text>
            <InputNumber min={1} max={365} value={cleanDays} onChange={(v) => setCleanDays(v || 90)} style={{ width: 90 }} addonAfter="天前" />
            <Popconfirm title={`清理 ${cleanDays} 天前的全部明细？不可恢复（统计/建议引擎的历史数据会减少）。`}
              okText="确认清理" okButtonProps={{ danger: true }} onConfirm={cleanup}>
              <Button danger loading={cleaning}>清理旧明细</Button>
            </Popconfirm>
          </Space>
        }>
        <Table className="tbl-logs" rowKey="id" columns={cols} dataSource={data?.items || []} loading={loading} size="small"
          sticky={wideSticky()}
          tableLayout="fixed"
          locale={{ emptyText: '当前筛选无明细记录：可扩大时间范围或清空筛选条件' }}
          pagination={{
            current: pageNo, pageSize: 50, total: data?.total || 0,
            onChange: (pg) => setPageNo(pg), showSizeChanger: false,
            showTotal: (t, rg) => `第 ${rg[0]}-${rg[1]} 条 / 共 ${t} 条`,
          }} />
      </Card>
    </Space>
  );
}
