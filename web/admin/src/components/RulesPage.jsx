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
import { ruleZh, RULE_ZH } from '../ruleNames.jsx';
import { L2PromptsSection } from './ModelPolicyPage.jsx';
// ---------------- 5.1/5.2/5.3 规则管理 ----------------
// ---------------- L2 拦截配置（scope 两档 + 判密阈值；持久化 model_policy.yaml，热重载生效） ----------------
const L2_SCOPE_META = [
  { key: 'last_user', label: '最近用户消息', tip: '判定基线，不受预算裁剪' },
  { key: 'tool', label: '工具结果', tip: '最后一条 tool 结果' },
  { key: 'tail', label: '长文尾部', tip: '长粘贴 [chunk, 2×chunk) 段' },
];

function L2RulesCard() {
  const [rm, setRm] = useState(null);
  const [pol, setPol] = useState(null);
  const [saving, setSaving] = useState(false);
  const load = useCallback(async () => {
    try {
      const d = await api('/model-policy');
      setPol(d.policy);
      const r = d.policy.review_model || {};
      setRm({ scopes: new Set(r.scopes || []),
        threshold: r.threshold ?? 0.7 });
    } catch (e) { message.error(errText(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);
  const toggle = (group, key) => setRm((c) => {
    const next = new Set(c[group]);
    if (next.has(key)) next.delete(key); else next.add(key);
    return { ...c, [group]: next };
  });
  const save = async () => {
    setSaving(true);
    try {
      await api('/model-policy', { method: 'PUT', body: JSON.stringify({
        ...pol, review_model: { ...pol.review_model,
          scopes: [...rm.scopes], threshold: rm.threshold },
      }) });
      message.success('已保存到 model_policy.yaml（即时生效，无需重启）');
      load();
    } catch (e) { message.error(errText(e)); }
    setSaving(false);
  };
  if (!rm) return <Card title="L2 拦截配置" loading />;
  return (
    <Card title="L2 拦截配置">
      <div style={{ marginBottom: 12 }}>
        <Text strong style={{ display: 'block', marginBottom: 16 }}>判定档（命中 CONFIDENTIAL → 本地路由拦截）</Text>
        <Space wrap>
          {L2_SCOPE_META.map((m) => (
            <Tooltip key={m.key} title={m.tip}>
              <Checkbox data-testid={`l2scope-scopes-${m.key}`}
                checked={rm.scopes.has(m.key)} onChange={() => toggle('scopes', m.key)}>
                {m.label} <Text type="secondary" code>{m.key}</Text>
              </Checkbox>
            </Tooltip>
          ))}
        </Space>
      </div>
      <Space align="center" wrap>
        <Text>判密阈值：confidence ≥ {rm.threshold.toFixed(2)}</Text>
        <Slider style={{ width: 220 }} min={0} max={1} step={0.05}
          value={rm.threshold} onChange={(v) => setRm((c) => ({ ...c, threshold: v }))} />
      </Space>
      <div style={{ margin: '8px 0 16px' }}>
        <Button type="primary" loading={saving} data-testid="l2rules-save" onClick={save}>保存</Button>
      </div>
      <div style={{ marginTop: 16, paddingTop: 12, borderTop: '1px solid #f0f0f0' }}>
        <L2PromptsSection />
      </div>
    </Card>
  );
}

function whenSummary(when) {
  if (when === 'always' || typeof when === 'string') return String(when);
  return JSON.stringify(when);
}

export default function RulesPage() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState(null); // 规则名或 null=新增
  const [form] = Form.useForm();
  const [aiLoading, setAiLoading] = useState(false);
  const [l1Test, setL1Test] = useState(null);
  const [l2Test, setL2Test] = useState(null);
  const [l2Input, setL2Input] = useState('');
  // rule target dropdowns: providers from routing.yaml, models proxied upstream
  const [providers, setProviders] = useState([]);
  const [modelOptions, setModelOptions] = useState([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsFailed, setModelsFailed] = useState(false);
  const loadProviders = useCallback(async () => {
    try {
      const d = await api('/providers');
      setProviders(Array.isArray(d.providers) ? d.providers : []);
    } catch (e) { setProviders([]); }
  }, []);
  const loadModels = useCallback(async (pname) => {
    if (!pname) { setModelOptions([]); setModelsFailed(false); return; }
    setModelsLoading(true);
    setModelsFailed(false);
    try {
      const d = await api(`/providers/${encodeURIComponent(pname)}/models`);
      const ms = Array.isArray(d.models) ? d.models : [];
      setModelOptions(ms.map((m) => ({ value: m.id, label: m.name || m.id })));
    } catch (e) { setModelOptions([]); setModelsFailed(true); }
    setModelsLoading(false);
  }, []);
  const onProviderChange = (v) => {
    const pname = v || '';
    const cur = form.getFieldValue('target_model') || '';
    const prov = providers.find((p) => p.name === pname);
    form.setFieldsValue({
      target_provider: pname,
      target_model: cur || (prov && prov.default_model) || '',
    });
    loadModels(pname);
  };
  useEffect(() => {
    if (!modalOpen) return;
    loadProviders();
    loadModels(form.getFieldValue('target_provider') || '');
  }, [modalOpen, loadProviders, loadModels, form]);
  // builder auto-sync: builder edits rebuild when_json (debounced 400ms);
  // manual when_json edits set a dirty flag pausing auto-sync until builder changes.
  const progRef = useRef(false);
  const whenDirtyRef = useRef(false);
  const whenSyncTimer = useRef(null);
  const syncWhenFromBuilder = useCallback(async () => {
    if (!modalOpen) return;
    const ct = form.getFieldValue('content_type');
    const mt = form.getFieldValue('match_type');
    let mv = form.getFieldValue('match_value');
    if (mt === 'size') mv = Number(mv || 0);
    else if (mt === 'contains') mv = String(mv || '').split(',').map((x) => x.trim()).filter(Boolean);
    try {
      const d = await api('/policies/build-when', {
        method: 'POST', body: JSON.stringify({ content_type: mt === 'size' ? 'size' : ct, match_type: mt, match_value: mv }),
      });
      progRef.current = true;
      form.setFieldsValue({ when_json: JSON.stringify(d.when, null, 2) });
      progRef.current = false;
    } catch (e) { message.error(errText(e)); }
  }, [modalOpen, form]);
  const onFormValuesChange = (changed) => {
    if (progRef.current) return;
    if (changed.when_json !== undefined) {
      whenDirtyRef.current = true;
      if (whenSyncTimer.current) { clearTimeout(whenSyncTimer.current); whenSyncTimer.current = null; }
      return;
    }
    if (changed.content_type !== undefined || changed.match_type !== undefined || changed.match_value !== undefined) {
      whenDirtyRef.current = false;
      if (whenSyncTimer.current) clearTimeout(whenSyncTimer.current);
      whenSyncTimer.current = setTimeout(() => { syncWhenFromBuilder(); }, 400);
    }
  };

  const load = useCallback(async () => {
    setLoading(true);
    try { setItems((await api('/policies')).items); } catch (e) { message.error(errText(e)); }
    setLoading(false);
  }, []);
  useEffect(() => { load(); }, [load]);

  const openEdit = (rec) => {
    setEditing(rec ? rec.name : null);
    if (rec) {
      progRef.current = true;
      form.setFieldsValue({
        name: rec.name, priority: rec.priority, action: rec.action,
        description: rec.description, when_json: JSON.stringify(rec.when, null, 2),
        target_provider: rec.target?.provider || '', target_model: rec.target?.model || '',
        content_type: 'prompt', match_type: 'regex', match_value: '',
        ai_description: '',
      });
      progRef.current = false;
    } else {
      progRef.current = true;
      form.setFieldsValue({
        name: '', priority: 100, action: 'allow', description: '',
        when_json: '{"text contains_any": ["关键词1", "关键词2"]}',
        target_provider: '', target_model: '',
        content_type: 'prompt', match_type: 'regex', match_value: '',
        ai_description: '',
      });
      progRef.current = false;
    }
    setModalOpen(true);
  };

  const aiGenerate = async () => {
    const desc = form.getFieldValue('ai_description');
    if (!desc) return;
    setAiLoading(true);
    try {
      const d = await api('/l2/generate-rule', { method: 'POST', body: JSON.stringify({ description: desc }) });
      const s = d.suggestion;
      form.setFieldsValue({
        match_type: s.match_type === 'contains' ? 'contains' : 'regex',
        match_value: Array.isArray(s.value) ? s.value.join(', ') : s.value,
      });
      message.success(`已生成（${s.explanation}），请确认后保存`);
    } catch (e) { message.warning(`AI 生成不可用，请手动填写（${errText(e)}）`); }
    setAiLoading(false);
  };

  const submit = async () => {
    try {
      const v = await form.validateFields();
      let when;
      try { when = JSON.parse(v.when_json); } catch { message.error('when JSON 不合法'); return; }
      const body = {
        name: v.name, priority: v.priority, action: v.action, description: v.description, when,
      };
      if (editing && v.name !== editing) body.new_name = v.name;
      if (v.target_provider) body.target = { provider: v.target_provider, model: v.target_model || '' };
      const r = editing
        ? await api(`/policies/${encodeURIComponent(editing)}`, { method: 'PUT', body: JSON.stringify(body) })
        : await api('/policies', { method: 'POST', body: JSON.stringify(body) });
      message.success(r.ok ? '已保存并生效' : '完成');
      setModalOpen(false);
      load();
    } catch (e) { if (e.errorFields) return; message.error(errText(e)); }
  };

  const del = async (name) => {
    try { await api(`/policies/${encodeURIComponent(name)}`, { method: 'DELETE' }); load(); }
    catch (e) { message.error(errText(e)); }
  };

  const runL1Test = async () => {
    try {
      const f = document.getElementById('l1test-text');
      const fn = document.getElementById('l1test-filename');
      const sz = document.getElementById('l1test-size');
      setL1Test(await api('/policies/test', { method: 'POST', body: JSON.stringify({
        text: f?.value || '', filename: fn?.value || '', size: Number(sz?.value || 0) }) }));
    } catch (e) { message.error(errText(e)); }
  };

  const runL2Test = async () => {
    try {
      const d = await api('/l2/test', { method: 'POST', body: JSON.stringify({ text: l2Input }) });
      setL2Test(d.result);
    } catch (e) { message.error(errText(e)); }
  };

  const cols = [
    { title: '优先级', dataIndex: 'priority', width: 80, sorter: (a, b) => a.priority - b.priority },
    { title: '名称', dataIndex: 'name', render: (v) => <Text code title={v}>{ruleZh(v)}</Text> },
    { title: '动作', dataIndex: 'action', width: 110,
      render: (v) => <Tag color={v === 'block' ? 'red' : v === 'route_local' ? 'orange' : 'green'}>{v}</Tag> },
    { title: '条件', dataIndex: 'when', ellipsis: true, render: (v) => <Text type="secondary" style={{ fontSize: 12 }}>{whenSummary(v)}</Text> },
    { title: '说明', dataIndex: 'description', ellipsis: true },
    { title: '操作', width: 150,
      render: (_, rec) => (
        <Space>
          <Button onClick={() => openEdit(rec)}>编辑</Button>
          {rec.name !== 'default_allow' && (
            <Popconfirm title="确认删除该规则？" onConfirm={() => del(rec.name)}>
              <Button data-testid={`rules-del-${rec.name}`} danger>删除</Button>
            </Popconfirm>
          )}
        </Space>
      ) },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card title="L1 规则" extra={
        <Space>
          <Button type="primary" onClick={() => openEdit(null)}>新增规则</Button>
            <Button aria-label="刷新" icon={<ReloadOutlined />} onClick={load} />
        </Space>
      }>
        <Table rowKey="name" columns={cols} dataSource={items} loading={loading} size="small" pagination={{ pageSize: 50, showSizeChanger: false }} />
      </Card>

      <L2RulesCard />

      <Row gutter={[12, 12]}>
        <Col span={12}>
          <Card title="L1 规则测试">
            <Space direction="vertical" style={{ width: '100%' }}>
              <Input.TextArea id="l1test-text" rows={2} placeholder="模拟提示词内容…" />
              <Space wrap>
                <Input id="l1test-filename" style={{ width: 200 }} placeholder="模拟文件名（可选）" />
                <Input id="l1test-size" style={{ width: 130 }} placeholder="文件大小(bytes，可选)" />
                <Button icon={<ExperimentOutlined />} onClick={runL1Test}>测试命中</Button>
              </Space>
              {l1Test && (
                <Alert showIcon type={l1Test.matched ? 'warning' : 'success'}
                  message={l1Test.matched
                    ? `命中规则 ${ruleZh(l1Test.rule)}（${l1Test.rule}）-> ${l1Test.action}`
                    : '未命中任何规则（default_allow）'} />
              )}
            </Space>
          </Card>
        </Col>
        <Col span={12}>
          <Card title="L2 规则测试">
            <Space direction="vertical" style={{ width: '100%' }}>
              <Input.TextArea rows={3} value={l2Input} onChange={(e) => setL2Input(e.target.value)}
                placeholder="粘贴待识别内容，查看 L2 审查判定（label / confidence / reason）…" />
              <Button icon={<ExperimentOutlined />} onClick={runL2Test}>运行 L2 判定</Button>
              {l2Test && (
                <Alert showIcon
                  type={l2Test.label === 'CONFIDENTIAL' ? 'warning' : 'success'}
                  message={`${l2Test.label}  confidence=${l2Test.confidence}`}
                  description={`${l2Test.reason || ''}${l2Test.disabled ? '（L2 未启用，当前为降级结果）' : ''}`} />
              )}
            </Space>
          </Card>
        </Col>
      </Row>

      <Modal title={editing ? `编辑规则：${editing}` : '新增规则'} open={modalOpen}
        destroyOnClose onOk={submit} onCancel={() => setModalOpen(false)} width={680}>
        <Form form={form} layout="vertical" onValuesChange={onFormValuesChange}>
          <Space style={{ width: '100%' }} size="middle">
            <Form.Item name="name" label="规则名" rules={[{ required: true }]}>
              <Input id="rule-name" />
            </Form.Item>
            <Form.Item name="priority" label="优先级（小者先）">
              <InputNumber min={0} max={1000} />
            </Form.Item>
            <Form.Item name="action" label="动作" rules={[{ required: true }]}>
              <Select style={{ width: 140 }} options={[
                { value: 'allow', label: 'allow 放行' },
                { value: 'route_local', label: 'route_local 转内网' },
                { value: 'block', label: 'block 拦截' },
              ]} />
            </Form.Item>
          </Space>
          <Form.Item name="description" label="说明"><Input /></Form.Item>

          <Card title="匹配方式构造器" style={{ marginBottom: 12 }}>
            <Space style={{ width: '100%' }} size="middle" wrap>
              <Form.Item name="content_type" label="内容类型" style={{ marginBottom: 0 }}>
                <Select style={{ width: 130 }} options={[
                  { value: 'prompt', label: '提示词' },
                  { value: 'filename', label: '文件名' },
                  { value: 'file', label: '文件' },
                  { value: 'ocr', label: 'OCR' },
                ]} />
              </Form.Item>
              <Form.Item name="match_type" label="匹配类型" style={{ marginBottom: 0 }}>
                <Select style={{ width: 110 }} options={[
                  { value: 'regex', label: '正则' },
                  { value: 'contains', label: '包含任一' },
                  { value: 'size', label: '文件大小' },
                ]} />
              </Form.Item>
              <Form.Item name="match_value" label="匹配项" style={{ marginBottom: 0, minWidth: 260 }}>
                <Input id="rule-match-value" placeholder="正则 / 关键词1, 关键词2 / 字节数" />
              </Form.Item>
            </Space>
            <Space style={{ marginTop: 8 }} wrap>
              <Input id="ai-desc" style={{ width: 260 }} placeholder="自然语言描述，如：拦截含 AWS 密钥"
                onChange={(e) => form.setFieldValue('ai_description', e.target.value)} />
              <Button icon={<RobotOutlined />} loading={aiLoading}
                onClick={() => aiGenerate()}>AI 生成匹配项</Button>
            </Space>
          </Card>

          <Form.Item name="when_json" label="when 条件（JSON，最终以此保存）"
            rules={[{ required: true }]}>
            <Input.TextArea id="rule-when-json" rows={4} style={{ fontFamily: 'monospace' }} />
          </Form.Item>
          <Space>
            <Form.Item name="target_provider" label="目标 provider（可选）" style={{ marginBottom: 0 }}>
              <Select id="rule-target-provider" showSearch allowClear style={{ width: 200 }} options={providers.map((p) => ({ value: p.name, label: p.local ? `${p.name} (local)` : p.name }))} onChange={onProviderChange} />
            </Form.Item>
            <Form.Item name="target_model" label="目标 model（可选）" style={{ marginBottom: 0 }}>
              {modelsFailed ? (
              <Input id="rule-target-model" />
            ) : (
              <Select id="rule-target-model" showSearch allowClear loading={modelsLoading} style={{ width: 200 }} options={modelOptions} />
            )}
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </Space>
  );
}

