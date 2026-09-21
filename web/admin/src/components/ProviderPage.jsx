import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import {
  Layout, Menu, Table, Button, AutoComplete, Input, InputNumber, Modal, Form, Select, Tag, Space,
  message, Popconfirm, Typography, Alert, Card, Statistic, Switch, Slider, Tabs,
  Progress, Checkbox, Tooltip, Row, Col,
} from 'antd';
import {
  ImportOutlined, KeyOutlined, StopOutlined, LockOutlined, ReloadOutlined, BarChartOutlined,
  ApiOutlined, AuditOutlined, RobotOutlined, ExperimentOutlined,
  FileSearchOutlined, FundOutlined, SearchOutlined,
  DownloadOutlined, ClearOutlined, WarningOutlined, ThunderboltOutlined,
  CopyOutlined,
} from '@ant-design/icons';
import { api, errText } from '../api.js';

const { Text } = Typography;
// ---------------- Provider 注册表（routing.yaml 读写；密钥仍只走 env） ----------------
function ProviderRegistryCard() {
  const [data, setData] = useState(null);
  const [modal, setModal] = useState(null);
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try { setData(await api('/providers')); } catch (e) { message.error(errText(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const openModal = (row) => {
    setModal(row ? { row } : {});
    form.setFieldsValue(row ? {
      name: row.name, base_url: row.base_url || '', api_key_env: row.api_key_env || '',
      api_mode: row.api_mode || 'openai', kind: row.kind || 'chat',
      default_model: row.default_model || '', embedding_model: row.embedding_model || '',
      timeout: row.timeout ?? 60, local: !!row.local,
    } : { api_mode: 'openai', kind: 'chat', timeout: 60 });
  };

  const save = async () => {
    const v = await form.validateFields();
    setSaving(true);
    try {
      if (modal?.row) await api(`/providers/${encodeURIComponent(modal.row.name)}`, { method: 'PUT', body: JSON.stringify(v) });
      else await api('/providers', { method: 'POST', body: JSON.stringify(v) });
      message.success('已保存，热重载即时生效');
      setModal(null);
      load();
    } catch (e) { message.error(errText(e)); }
    setSaving(false);
  };

  const [keyModal, setKeyModal] = useState(null);
  const [keyVal, setKeyVal] = useState('');
  const [keyBusy, setKeyBusy] = useState(false);
  const saveKey = async () => {
    const r = keyModal.row;
    setKeyBusy(true);
    try {
      await api(`/providers/${encodeURIComponent(r.name)}/key`, { method: 'PUT', body: JSON.stringify({ value: keyVal }) });
      message.success(keyVal.trim() ? `${r.api_key_env} 已保存，即时生效（免重启）` : `${r.api_key_env} 已清除`);
      setKeyModal(null);
      setKeyVal('');
      load();
      // 存完顺手健康检查，闭环验证 key 是否真的可用
      if (keyVal.trim()) {
        try {
          const d = await api('/providers/check', { method: 'POST', body: JSON.stringify({ name: r.name }) });
          const one = (d.results || [])[0] || {};
          if (one.ok) message.success(`健康检查：${r.name} 可达（${one.latency_ms}ms，${one.model_count} 个模型）`);
          else message.warning(`健康检查：${r.name} 仍失败 — ${one.error || 'HTTP ' + one.status}（key 可能无效）`);
        } catch (e) { /* 验证失败不阻塞保存结果 */ }
      }
    } catch (e) { message.error(errText(e)); }
    setKeyBusy(false);
  };

  const rows = data?.providers || [];
  return (
    <Card title="注册表"
      extra={<Button type="primary" size="small" onClick={() => openModal(null)}>新增 provider</Button>}>
      <Table size="small" rowKey="name" pagination={false} dataSource={rows} columns={[
        { title: '名称', dataIndex: 'name' },
        { title: 'base_url', dataIndex: 'base_url', ellipsis: true },
        { title: 'api_mode', dataIndex: 'api_mode' },
        { title: '类型', render: (_, r) => (
          <Space size={4}>{r.local ? <Tag color="blue">内网</Tag> : <Tag>外网</Tag>}{r.kind === 'embedding' && <Tag color="purple">向量</Tag>}</Space>) },
        { title: '默认模型', dataIndex: 'default_model' },
        { title: 'Key', dataIndex: 'api_key_env', width: 300, render: (v, r) => v ? (
          <Space size={4}><code>{v}</code>{r.configured ? <Tag color="green">已配</Tag> : <Tag color="red">缺</Tag>}
            <Button type="link" size="small" onClick={() => { setKeyVal(''); setKeyModal({ row: r }); }}>{r.configured ? '改' : '配'}</Button>
          </Space>
        ) : <Tag>-</Tag> },
        { title: '超时(s)', dataIndex: 'timeout' },
        { title: '操作', render: (_, r) => (
          <Space>
            <Button type="link" size="small" onClick={() => openModal(r)}>编辑</Button>
            <Popconfirm title={`删除 ${r.name}？被默认路由/候选/别名/策略引用时会拒绝`} onConfirm={async () => {
              try { await api(`/providers/${encodeURIComponent(r.name)}`, { method: 'DELETE' }); message.success('已删除'); load(); }
              catch (e) { message.error(errText(e)); }
            }}><Button type="link" size="small" danger>删除</Button></Popconfirm>
          </Space>) },
      ]} />
      <Text type="secondary">base_url 支持 {'${ENV_VAR}'} / {'${ENV_VAR:默认值}'} 展开；删除受引用保护（默认路由/候选/别名组/策略规则）</Text>
      <Modal open={!!modal} title={modal?.row ? `编辑 provider ${modal.row.name}` : '新增 provider'}
        onOk={save} confirmLoading={saving} onCancel={() => setModal(null)} destroyOnClose width={560}>
        <Form form={form} layout="vertical" size="small">
          <Form.Item name="name" label="名称" rules={[{ required: true, pattern: /^[A-Za-z0-9_-]+$/, message: '仅字母/数字/_/-' }]}>
            <Input disabled={!!modal?.row} placeholder="如 oneapi" />
          </Form.Item>
          <Form.Item name="base_url" label="base_url" rules={[{ required: true }]}>
            <Input placeholder="https://api.example.com/v1" />
          </Form.Item>
          <Row gutter={8}>
            <Col span={12}>
              <Form.Item name="api_mode" label="api_mode">
                <Select options={(data?.api_modes || ['openai', 'anthropic']).map((m) => ({ value: m, label: m }))} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="kind" label="类型">
                <Select options={[{ value: 'chat', label: 'chat' }, { value: 'embedding', label: 'embedding' }]} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="api_key_env" label="api_key_env（env 变量名）"
            tooltip="只填变量名；值在列表的「配/改」按钮里填（即时生效，免重启）；内网 provider 可留空"
            extra="外网 provider 对应 env 未配值时真实转发会 503（保存后自动健康检查验证）">
            <Input placeholder="如 OPENAI_API_KEY；内网留空" />
          </Form.Item>
          <Form.Item name="default_model" label="默认模型"><Input placeholder="如 gpt-4o-mini" /></Form.Item>
          <Form.Item name="embedding_model" label="向量模型（可选）"><Input /></Form.Item>
          <Form.Item name="timeout" label="超时（秒）" rules={[{ required: true }]}>
            <InputNumber min={1} max={600} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="local" label="内网 provider（local: true，内容不出境）" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
      <Modal open={!!keyModal} title={keyModal ? `配置 ${keyModal.row.api_key_env}（${keyModal.row.name}）` : ''}
        onOk={saveKey} confirmLoading={keyBusy} okText="保存"
        onCancel={() => { setKeyModal(null); setKeyVal(''); }} destroyOnClose>
        <Input.Password value={keyVal} onChange={(e) => setKeyVal(e.target.value)}
          placeholder={keyModal?.row?.configured ? '留空保存 = 清除该 key' : 'key 值（至少 8 位）'}
          autoComplete="new-password" onPressEnter={saveKey} />
        <Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
          即时生效（免重启）并写入 .env 持久化；保存后自动跑健康检查验证；
          值只写不读，任何端点不返回明文
        </Text>
      </Modal>
    </Card>
  );
}

// ---------------- 运维诊断：provider 健康检查 ----------------
// 总览页也内嵌这张卡（手动触发，不自动轮询：检查打上游真实 HTTP）
export function ProviderHealthCard({ compact = false }) {
  const [rows, setRows] = useState(null);
  const [checking, setChecking] = useState(false);
  const check = async (name) => {
    setChecking(true);
    try {
      const d = await api('/providers/check', { method: 'POST', body: JSON.stringify({ name: name || '' }) });
      setRows(d.results);
      const bad = (d.results || []).filter((r) => !r.ok).length;
      if (bad) message.warning(`健康检查完成：${bad} 个异常`);
      else message.success('健康检查完成：全部正常');
    } catch (e) { message.error(errText(e)); }
    setChecking(false);
  };
  return (
    <Card title="Provider 健康检查"
      extra={<Space>
        {compact && <Button type="link" size="small" style={{ padding: 0 }} onClick={() => { window.location.hash = '/provider'; }}>Provider 管理 →</Button>}
        <Button size="small" loading={checking} onClick={() => check('')}>全部检查</Button>
      </Space>}>
      <Table size="small" rowKey="name" pagination={false} dataSource={rows || []}
        locale={{ emptyText: '尚未检查，点右上角「全部检查」开始' }}
        columns={[
          { title: 'Provider', dataIndex: 'name' },
          { title: '状态', render: (_, r) => r.ok ? <Tag color="green">正常</Tag> : <Tag color="red">异常</Tag> },
          ...(!compact && [
            { title: 'HTTP', dataIndex: 'status' },
            { title: '延迟(ms)', dataIndex: 'latency_ms' },
            { title: '模型数', dataIndex: 'model_count' },
            { title: 'Key来源', dataIndex: 'key_source', render: (v) => (
              <Tooltip title="header=请求头 / models_list_env=专用拉列表 env / provider_env=provider env key / none=无 key"><Tag>{v}</Tag></Tooltip>) },
            { title: '说明', dataIndex: 'error', ellipsis: true },
            { title: '操作', render: (_, r) => (
              <Button type="link" size="small" loading={checking} onClick={() => check(r.name)}>检查</Button>) },
          ] || []),
        ]} />
      {!compact && (
        <Text type="secondary">打 GET {`{base_url}`}/models 测连通+鉴权+延迟，不发真实模型调用（不耗 token）</Text>
      )}
    </Card>
  );
}

// ---------------- 运维诊断：熔断器（逐 provider 明细 + 手动置位） ----------------
function CircuitCard() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState('');
  const load = useCallback(async () => {
    try { setData(await api('/circuit')); } catch (e) { message.error(errText(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);
  const setState = async (name, state) => {
    setBusy(name);
    try {
      await api(`/circuit/${encodeURIComponent(name)}`, { method: 'POST', body: JSON.stringify({ state }) });
      message.success(`${name} 已置为 ${state}`);
      load();
    } catch (e) { message.error(errText(e)); }
    setBusy('');
  };
  const rows = Object.entries(data?.providers || {}).map(([name, p]) => ({ name, ...p }));
  return (
    <Card title="熔断器"
      extra={<Button size="small" icon={<ReloadOutlined />} onClick={load}>刷新</Button>}>
      <Table size="small" rowKey="name" pagination={false} dataSource={rows}
        columns={[
          { title: 'Provider', dataIndex: 'name' },
          { title: '状态', dataIndex: 'state', render: (v) => v === 'open'
            ? <Tag color="red">OPEN</Tag> : v === 'half_open' ? <Tag color="orange">HALF_OPEN</Tag> : <Tag color="green">CLOSED</Tag> },
          { title: '窗口内失败', dataIndex: 'failure_count_window' },
          { title: '冷却剩余(s)', dataIndex: 'cooldown_remaining_s' },
          { title: '操作', render: (_, r) => (
            <Space>
              <Popconfirm title={`手动打开 ${r.name} 熔断？该 provider 上游请求将立即失败并回落本地`}
                onConfirm={() => setState(r.name, 'open')}>
                <Button type="link" size="small" danger loading={busy === r.name}>打开</Button>
              </Popconfirm>
              <Button type="link" size="small" loading={busy === r.name} onClick={() => setState(r.name, 'closed')}>关闭</Button>
            </Space>) },
        ]} />
      {data && <Text type="secondary">
        参数：{data.config.window_seconds}s 内连续 {data.config.failure_threshold} 次失败自动 OPEN；OPEN {data.config.recovery_seconds}s 后转 HALF_OPEN 探测，连续 {data.config.success_threshold} 次成功恢复
      </Text>}
    </Card>
  );
}


export default function ProviderPage() {
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <ProviderRegistryCard />
      <ProviderHealthCard />
      <CircuitCard />
    </Space>
  );
}
