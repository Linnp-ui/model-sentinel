import { useEffect, useState, useCallback, useRef } from 'react';
import { Table, Button, Input, InputNumber, Modal, Form, Select, Tag, Space, message, Popconfirm, Typography, Alert, Card, Slider, Checkbox, Tooltip, Row, Col } from 'antd';
import { ReloadOutlined, RobotOutlined, ExperimentOutlined } from '@ant-design/icons';
import { api, errText } from '../api.js';

const { Text } = Typography;
import { ruleZh } from '../ruleNames.jsx';
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
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
          <Text strong>判定档（命中 CONFIDENTIAL → 本地路由拦截）</Text>
          <Button type="primary" loading={saving} data-testid="l2rules-save" onClick={save}>保存</Button>
        </div>
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
  const [prioSort, setPrioSort] = useState('ascend'); // 优先级仅升/降两态，点击直接切换
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
  const [versions, setVersions] = useState([]);
  const [choices, setChoices] = useState(null);
  const [choicesLoading, setChoicesLoading] = useState(false);
  const [matchType, setMatchType] = useState('');
  const [verView, setVerView] = useState(null);
  const [hits, setHits] = useState({});  // rule 名 -> {count, actions, last_seen}（168h）
  // 构造器可选项来自后端 policy_admin.MATCH_CHOICES（唯一真源），不在前端抄一份
  const loadChoices = useCallback(async () => {
    setChoicesLoading(true);
    try {
      const d = await api('/policies/match-choices');
      setChoices([...(d.choices || []), ...(d.self_field || [])]);
    } catch (e) { setChoices([]); }
    setChoicesLoading(false);
  }, []);
  const VALUE_HINT = {
    regex: '正则表达式，如 AWS_SECRET|akia',
    contains: '关键词1, 关键词2',
    size: '字节数，如 1048576',
    in: '枚举值1, 枚举值2',
    not_in: '枚举值1, 枚举值2（都不匹配才算命中）',
    bool: 'true / false（是 / 否 也认）',
  };
  const valueHint = VALUE_HINT[matchType] || '先选匹配条件';
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
    loadChoices();
    loadModels(form.getFieldValue('target_provider') || '');
  }, [modalOpen, loadProviders, loadChoices, loadModels, form]);
  // builder auto-sync: builder edits rebuild when_json (debounced 400ms);
  // manual when_json edits set a dirty flag pausing auto-sync until builder changes.
  const progRef = useRef(false);
  const whenDirtyRef = useRef(false);
  const whenSyncTimer = useRef(null);
  const syncWhenFromBuilder = useCallback(async () => {
    if (!modalOpen) return;
    const key = form.getFieldValue('match_cond');
    if (!key) return;
    const [ct, mt] = key.split('|');
    const rawMv = form.getFieldValue('match_value');
    // 匹配项还没填就切换条件时，别去 build-when 校验——否则空值必然 422
    // （contains/in/not_in 要求至少一个值）弹出误报。留空=不同步，
    // when_json 保持原样，等填了值再同步。
    if (String(rawMv || '').trim() === '') return;
    let mv = rawMv;
    if (mt === 'size') mv = Number(mv || 0);
    else if (mt === 'contains') mv = String(mv || '').split(',').map((x) => x.trim()).filter(Boolean);
    try {
      const d = await api('/policies/build-when', {
        method: 'POST', body: JSON.stringify({ content_type: ct, match_type: mt, match_value: mv }),
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
    if (changed.match_cond !== undefined || changed.match_value !== undefined) {
      if (changed.match_cond !== undefined) setMatchType(String(changed.match_cond).split('|')[1] || '');
      whenDirtyRef.current = false;
      if (whenSyncTimer.current) clearTimeout(whenSyncTimer.current);
      whenSyncTimer.current = setTimeout(() => { syncWhenFromBuilder(); }, 400);
    }
  };

  const loadVersions = useCallback(async () => {
    try { setVersions((await api('/policy/versions?limit=50')).items || []); } catch { setVersions([]); }
  }, []);
  const load = useCallback(async () => {
    setLoading(true);
    try { setItems((await api('/policies')).items); } catch (e) { message.error(errText(e)); }
    setLoading(false);
    loadVersions();
    api('/rule-hits?hours=168').then((d) => {
      setHits(Object.fromEntries((d.items || []).map((i) => [i.rule, i])));
    }).catch(() => {});
  }, [loadVersions]);
  useEffect(() => { load(); }, [load]);

  const viewVersion = async (vid) => {
    try { setVerView((await api(`/policy/versions/${vid}`)).yaml_text); } catch (e) { message.error(errText(e)); }
  };
  const rollback = async (vid) => {
    try {
      await api(`/policy/versions/${vid}/rollback`, { method: 'POST' });
      message.success('已回滚并即时生效（现状已留版本，可再反悔）');
      load();
    } catch (e) { message.error(errText(e)); }
  };

  // setFieldsValue 不触发 onValuesChange，matchType（hint 用）得手动同步
  const applyBuilderPrefill = (ct, mt, mv) => {
    setMatchType(mt || '');
    form.setFieldsValue({ match_cond: ct ? `${ct}|${mt}` : null, match_value: mv ?? '' });
  };
  const openEdit = async (rec) => {
    setEditing(rec ? rec.name : null);
    progRef.current = true;
    if (rec) {
      form.setFieldsValue({
        name: rec.name, priority: rec.priority, action: rec.action,
        description: rec.description, when_json: JSON.stringify(rec.when, null, 2),
        target_provider: rec.target?.provider || '', target_model: rec.target?.model || '',
        match_cond: null, match_value: '',
        ai_description: '',
      });
      setMatchType('');
      // 已有规则：先尝试把 when 逆解析回构造器控件（开关/单条件规则直接
      // 在构造器里改，而不是当作新增）。认不出的形状保持空白，绝不预填
      // 默认值——空白构造器不会去覆盖下方 when_json。
      try {
        const d = await api('/policies/parse-when', {
          method: 'POST', body: JSON.stringify({ when: rec.when }),
        });
        if (d.parsed) applyBuilderPrefill(d.parsed.content_type, d.parsed.match_type, d.parsed.match_value);
      } catch { /* 保持空白，手动改 when_json */ }
    } else {
      form.setFieldsValue({
        name: '', priority: 100, action: 'allow', description: '',
        when_json: '{"text contains_any": ["关键词1", "关键词2"]}',
        target_provider: '', target_model: '',
        ai_description: '',
      });
      applyBuilderPrefill('prompt', 'contains', '');
    }
    progRef.current = false;
    setModalOpen(true);
  };

  const aiGenerate = async () => {
    const desc = form.getFieldValue('ai_description');
    if (!desc) return;
    setAiLoading(true);
    try {
      const d = await api('/l2/generate-rule', { method: 'POST', body: JSON.stringify({ description: desc }) });
      const s = d.suggestion;
      const mt = s.match_type === 'contains' ? 'contains' : 'regex';
      setMatchType(mt);
      form.setFieldsValue({
        match_cond: `prompt|${mt}`,
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
      const md = document.getElementById('l1test-model');
      const ck = document.getElementById('l1test-client');
      const hr = document.getElementById('l1test-hour');
      setL1Test(await api('/policies/test', { method: 'POST', body: JSON.stringify({
        text: f?.value || '', filename: fn?.value || '', size: Number(sz?.value || 0),
        model: md?.value || '', client_key: ck?.value || '',
        // 空串 = 不传（后端回落到当前小时），别传 0 把「凌晨」规则测成假阴性
        hour: hr?.value === '' ? null : Number(hr?.value) }) }));
    } catch (e) { message.error(errText(e)); }
  };

  const runL2Test = async () => {
    try {
      const d = await api('/l2/test', { method: 'POST', body: JSON.stringify({ text: l2Input }) });
      setL2Test(d.result);
    } catch (e) { message.error(errText(e)); }
  };

  const cols = [
    { title: '优先级', dataIndex: 'priority', width: 80, sorter: (a, b) => a.priority - b.priority, sortOrder: prioSort },
    { title: '名称', dataIndex: 'name', width: 190, ellipsis: true, render: (v) => <Text code title={v}>{ruleZh(v)}</Text> },
    { title: '动作', dataIndex: 'action', width: 110,
      render: (v) => <Tag color={v === 'block' ? 'red' : v === 'route_local' ? 'orange' : 'green'}>{v}</Tag> },
    { title: '条件', dataIndex: 'when', ellipsis: true, render: (v) => <Text type="secondary" style={{ fontSize: 12 }}>{whenSummary(v)}</Text> },
    { title: <Tooltip title="近 7 天命中次数（审计库口径，排除 default_allow）">7d 命中</Tooltip>, width: 100,
      render: (_, r) => {
        const h = hits[r.name];
        if (!h) return <Text type="secondary">0</Text>;
        return (
          <Tooltip title={`动作分布：${Object.entries(h.actions).map(([k, v]) => `${k}×${v}`).join(' ')}；最后命中 ${h.last_seen} UTC`}>
            <Text strong={h.count >= 50} style={{ color: h.count >= 50 ? '#fa8c16' : undefined }}>{h.count}</Text>
          </Tooltip>
        );
      } },
    { title: '说明', dataIndex: 'description', width: 150, ellipsis: true },
    // 操作列要装下 编辑+删除 两个默认尺寸按钮 + 24/24 cell padding，
    // 150 会撑出 wrapper 右缘（2026-10 实测）
    { title: '操作', width: 180,
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
        <Table rowKey="name" columns={cols} dataSource={items} loading={loading} size="small"
          tableLayout="fixed" scroll={{ x: 1070 }} pagination={{ pageSize: 50, showSizeChanger: false }}
          onChange={(_p, _f, sorter, extra) => {
            // antd 默认三态 升→降→取消(null)；把 null 映射回升序 ⇒ 只有升/降两态，点击直接切换
            if (extra.action === 'sort') setPrioSort(sorter.order === 'descend' ? 'descend' : 'ascend');
          }} />
      </Card>

      <L2RulesCard />

      <Row gutter={[12, 12]}>
        <Col span={12}>
          <Card title="L1 规则测试" style={{ height: '100%' }}>
            <Space direction="vertical" style={{ width: '100%' }}>
              <Input.TextArea id="l1test-text" rows={3} placeholder="模拟提示词内容…" />
              <Space wrap>
                <Input id="l1test-filename" style={{ width: 200 }} placeholder="模拟文件名（可选）" />
                <Input id="l1test-size" style={{ width: 130 }} placeholder="文件大小(bytes，可选)" />
                <Input id="l1test-model" style={{ width: 150 }} placeholder="模型名(可选)" />
                <Input id="l1test-client" style={{ width: 150 }} placeholder="客户端登记名(可选)" />
                <Input id="l1test-hour" style={{ width: 110 }} placeholder="小时0-23(空=当前)" />
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
          <Card title="L2 规则测试" style={{ height: '100%' }}>
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

      <Card title="历史版本（每次保存前快照；回滚走校验+原子落盘）" style={{ marginTop: 12 }}
        extra={<Button size="small" type="text" onClick={loadVersions}>刷新</Button>}>
        {versions.length ? (
          <Table rowKey="id" size="small" pagination={false} dataSource={versions} columns={[
            { title: '时间', dataIndex: 'ts', width: 170 },
            { title: '操作者', dataIndex: 'operator', width: 140, ellipsis: true },
            { title: '来源', dataIndex: 'source', width: 90,
              render: (s) => (s === 'rollback' ? <Tag color="orange">rollback</Tag> : <Tag>save</Tag>) },
            { title: '规则数', dataIndex: 'rule_count', width: 80 },
            { title: '预览', dataIndex: 'yaml_text', ellipsis: true },
            { title: '操作', width: 150, render: (_, r) => (
              <Space size={4}>
                <Button size="small" type="link" onClick={() => viewVersion(r.id)}>查看</Button>
                <Popconfirm title={`回滚到该版本（${r.rule_count} 条规则）？现状也会存一版，可再反悔。`}
                  onConfirm={() => rollback(r.id)}>
                  <Button size="small" type="link" danger>回滚</Button>
                </Popconfirm>
              </Space>
            ) },
          ]} />
        ) : (
          <Alert type="info" showIcon message="暂无历史版本（第一次保存规则时自动生成）" />
        )}
      </Card>

      <Modal title="版本内容" open={verView != null} onCancel={() => setVerView(null)}
        footer={null} width={720}>
        <pre style={{ maxHeight: 480, overflow: 'auto', fontSize: 12 }}>{verView || ''}</pre>
      </Modal>

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
              <Form.Item name="match_cond" label="匹配条件" style={{ marginBottom: 0 }}>
                <Select showSearch style={{ width: 260 }} loading={choicesLoading}
                  placeholder="选字段 + 匹配方式"
                  options={(choices || []).filter((c) => editing || c.match_type !== 'bool').map((c) => ({ value: c.key, label: c.label }))} />
              </Form.Item>
              <Form.Item name="match_value" label="匹配项" style={{ marginBottom: 0, minWidth: 260 }}>
                <Input id="rule-match-value" placeholder={valueHint} />
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

