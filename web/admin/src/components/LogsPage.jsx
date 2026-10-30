import { useState, useEffect, useCallback, useRef } from 'react';
import { Card, Table, Button, Input, Select, Space, Tag, Typography, InputNumber, Popconfirm, message } from 'antd';
import { DatabaseOutlined, SearchOutlined } from '@ant-design/icons';
import { api, errText, RANGES } from '../api.js';
import { wideSticky } from '../utils.js';
import { Tip } from '../terms.jsx';
import { ACTION_INFO } from '../ruleNames.jsx';

const { Text } = Typography;

// 明细库（request_log）：统计/建议引擎的数据源，与审计日志页的 5000 条热窗
// 是**两套存储**——这里查的是 MySQL 明细全量（含延迟/token 分解），无内容字段、不可展开。
// 筛选态入 URL（#/logs?hours=..&key=..&model=..&action=..）：刷新留条件、可分享。
const LOG_ACTIONS = [
  { value: '', label: '全部动作' },
  { value: 'allow', label: '放行' },
  { value: 'route_local', label: '本地路由' },
  { value: 'block', label: '拦截' },
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
    return { hours: p.get('hours') || '168', key: p.get('key') || '',
             model: p.get('model') || '', action: p.get('action') || '' };
  });
  const [pageNo, setPageNo] = useState(1);
  const [cleanDays, setCleanDays] = useState(90);
  const [cleaning, setCleaning] = useState(false);
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

  const setFld = (k) => (e) => { const next = { ...f, [k]: e.target.value }; setF(next); writeUrl(next); };
  const search = () => { setPageNo(1); load(1); };

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
          <Select style={{ width: 120 }} value={f.action} options={LOG_ACTIONS}
            onChange={(v) => { const next = { ...f, action: v || '' }; setF(next); setPageNo(1); writeUrl(next); }} />
          <Input style={{ width: 150 }} placeholder="调用方（精确）" allowClear value={f.key} onChange={setFld('key')} onPressEnter={search} />
          <Input style={{ width: 150 }} placeholder="模型（精确）" allowClear value={f.model} onChange={setFld('model')} onPressEnter={search} />
          <Button type="primary" icon={<SearchOutlined />} onClick={search}>查询</Button>
        </Space>
      </Card>
      <Card title={<><DatabaseOutlined /> 请求明细<Tip text="数据源是 MySQL 明细库，与审计页热窗不同；仅调用元数据，行不可展开。" /></>}
        extra={
          <Space wrap>
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
