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
import ExternalCandidateModal, { ExternalCandidateFormItems } from './ExternalCandidateBits.jsx';
function AliasGroupsCard() {
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState(null);
  const [extCands, setExtCands] = useState([]);
  const [extLoading, setExtLoading] = useState(false);
  const [extModal, setExtModal] = useState(null); // null | "new"
  const [addForm] = Form.useForm();

  const loadExt = useCallback(async () => {
    setExtLoading(true);
    try {
      const d = await api('/model-policy');
      setExtCands(d.policy.external_candidates || []);
    } catch (e) { message.error(errText(e)); }
    setExtLoading(false);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api('/aliases');
      setGroups(d.groups || []);
    } catch (e) { message.error(errText(e)); }
    setLoading(false);
    loadExt();
  }, [loadExt]);
  useEffect(() => { load(); }, [load]);

  const save = async () => {
    const gname = (editing.name || '').trim();
    if (!gname) { message.error('请填写别名'); return; }
    if (!editing.members.length) { message.error('至少添加一个候选'); return; }
    try {
      const members = editing.members.map((m, i) => ({ ...m, priority: (i + 1) * 10 }));
      await api(`/aliases/${encodeURIComponent(gname)}`, {
        method: 'PUT',
        body: JSON.stringify({ description: editing.description, members }),
      });
      message.success('已保存，路由即时生效（5s 内刷新）');
      setEditing(null);
      load();
    } catch (e) { message.error(errText(e)); }
  };

  const remove = async (gname) => {
    try {
      await api(`/aliases/${encodeURIComponent(gname)}`, { method: 'DELETE' });
      message.success(`已删除别名组 ${gname}`);
      load();
    } catch (e) { message.error(errText(e)); }
  };

  return (
    <Card title="对外模型别名（ext-flash / ext-pro）" size="small" style={{ marginBottom: 16 }}
          extra={<Space>
            <Button type="primary"
                    onClick={() => { addForm.resetFields(); setEditing({ isNew: true, name: '', description: '', members: [] }); }}>新建别名组</Button>
            <Button onClick={load}>刷新</Button>
          </Space>}>
      <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
        /v1/models 仅展示以下别名；请求 model=别名 时按候选顺序路由（第一个为默认），上游失败自动切下一候选，连续失败触发熔断跳过。
        候选只能从外部候选中选择；如下拉没有，先点“＋新增外部候选”（与下方外部候选同一张表）。
      </Typography.Text>
      <Table
        loading={loading}
        rowKey={(g) => g.name}
        dataSource={groups}
        pagination={false}
        size="small"
        columns={[
          { title: '别名', dataIndex: 'name', width: 120, render: (v) => <Tag color="blue">{v}</Tag> },
          { title: '候选链（顺序即优先级，第一个为默认）', render: (_, g) => (
              <Space direction="vertical" size={2}>
                {(g.members || []).map((m, i) => (
                  <Typography.Text key={i} type={m.enabled ? undefined : 'secondary'}>
                    {i === 0 && m.enabled ? <Tag color="green">默认</Tag> : null}
                    {m.provider}/{m.model}{m.enabled ? '' : '（停用）'}
                  </Typography.Text>
                ))}
              </Space>
          )},
          { title: '操作', width: 160, render: (_, g) => (
              <Space size={4}>
                <Button onClick={() => { addForm.resetFields(); setEditing({
                  name: g.name, description: g.description || '',
                  members: (g.members || []).map((m) => ({ provider: m.provider, model: m.model, priority: m.priority, enabled: !!m.enabled })),
                }); }}>编辑</Button>
                <Popconfirm title={`删除别名组 ${g.name}？`}
                            description="整组及候选链一并删除，/v1/models 立即不再展示，且不可恢复。"
                            okText="删除" okButtonProps={{ danger: true }} cancelText="取消"
                            onConfirm={() => remove(g.name)}>
                  <Button danger>删除</Button>
                </Popconfirm>
              </Space>
          )},
        ]}
      />
      <Modal title={editing && editing.isNew ? '新建别名组' : `编辑别名组：${editing ? editing.name : ''}`} open={!!editing}
             onOk={save} onCancel={() => setEditing(null)} width={760}
             okText="保存" cancelText="取消">
        {editing && (
          <>
            {editing.isNew && (
              <Form layout="inline" style={{ marginBottom: 8 }}>
                <Form.Item label="别名" required>
                  <Input value={editing.name} style={{ width: 220 }}
                         onChange={(e) => setEditing({ ...editing, name: e.target.value })}
                         placeholder="如 ext-flash" />
                </Form.Item>
              </Form>
            )}
            <Form layout="inline" style={{ marginBottom: 8 }} form={addForm}
                  onFinish={(v) => {
                    const key = (v.cand || '').trim();
                    if (!key) return;
                    const hit = extCands.find((c) => `${c.provider}/${c.model}` === key);
                    if (!hit) { message.error('所选候选不在外部候选表中，请先新增外部候选'); return; }
                    if (editing.members.some((m) => m.provider === hit.provider && m.model === hit.model)) {
                      message.warning('该候选已在链中'); return;
                    }
                    setEditing({ ...editing, members: [...editing.members, { provider: hit.provider, model: hit.model, priority: (editing.members.length + 1) * 10, enabled: true }] });
                    addForm.resetFields();
                  }}>
              <Form.Item name="cand" rules={[{ required: true, message: '请从外部候选中选择' }]}>
                <Select showSearch placeholder="从外部候选中选择（provider/model）" style={{ width: 340 }}
                        loading={extLoading}
                        options={extCands.map((c) => ({
                          value: `${c.provider}/${c.model}`,
                          label: `${c.provider}/${c.model}${c.enabled ? '' : '（停用）'}${c.note ? ` — ${c.note}` : ''}`,
                        }))}
                        filterOption={(inp, opt) => String((opt && (opt.value || opt.label)) || '').toLowerCase().includes(inp.toLowerCase())} />
              </Form.Item>
              <Button htmlType="submit">添加候选</Button>
              <Button onClick={() => setExtModal("new")}>＋新增外部候选</Button>
            </Form>
            <Table rowKey={(_, i) => 'm' + i} size="small" pagination={false}
                   dataSource={editing.members}
                   columns={[
                     { title: '#', width: 60, render: (_, __, i) => (i === 0 ? <Tag color="green">默认</Tag> : i + 1) },
                     { title: 'Provider', dataIndex: 'provider' },
                     { title: 'Model', dataIndex: 'model' },
                     { title: '启用', dataIndex: 'enabled', width: 80, render: (v, _, i) => (
                         <Switch size="small" checked={v} onChange={(ck) => {
                           const ms = [...editing.members]; ms[i] = { ...ms[i], enabled: ck }; setEditing({ ...editing, members: ms });
                         }} /> )},
                     { title: '操作', width: 170, render: (_, __, i) => (
                         <Space>
                           <Button disabled={i === 0} onClick={() => {
                             const ms = [...editing.members]; const t = ms[i - 1]; ms[i - 1] = ms[i]; ms[i] = t; setEditing({ ...editing, members: ms });
                           }}>上移</Button>
                           <Popconfirm title="删除该候选？" onConfirm={() => setEditing({ ...editing, members: editing.members.filter((_, k) => k !== i) })}>
                             <Button danger>删除</Button>
                           </Popconfirm>
                         </Space>
                     )},
                    ]} />
            <ExternalCandidateModal
              open={extModal === "new"}
              title="新增外部候选"
              okText="保存并加入别名"
              onCancel={() => setExtModal(null)}
              onOk={async (v) => {
                const provider = (v.provider || '').trim();
                const model = (v.model || '').trim();
                if (!provider || !model) { message.error('provider/模型必填'); throw new Error('missing'); }
                if (extCands.some((c) => c.provider === provider && c.model === model)) {
                  message.error('该 provider/model 已在外部候选中'); throw new Error('dup');
                }
                const entry = {
                  provider, model,
                  price_per_1m_in: v.price_per_1m_in ?? 0,
                  price_per_1m_out: v.price_per_1m_out ?? 0,
                  context_length: v.context_length ?? 65536,
                  rate: v.rate ?? 1.0,
                  note: v.note || '', enabled: true, rank: v.rank ?? 100,
                };
                const d = await api('/model-policy');
                await api('/model-policy', { method: 'PUT', body: JSON.stringify({
                  external_candidates: [...(d.policy.external_candidates || []), entry],
                  internal_models: d.policy.internal_models || [],
                  model_limits: d.policy.model_limits || [],
                  load_balance_mode: d.policy.load_balance_mode || 'none',
                  review_model: d.policy.review_model || {},
                })});
                setExtCands((prev) => [...prev, entry]);
                if (!editing.members.some((m) => m.provider === provider && m.model === model)) {
                  setEditing({ ...editing, members: [...editing.members, { provider, model, priority: (editing.members.length + 1) * 10, enabled: true }] });
                }
                message.success('已加入外部候选并加入别名链');
              }}
            />
          </>
        )}
      </Modal>
    </Card>
  );
}


function ExternalCandidatesCard() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [modal, setModal] = useState(null);
  const [envFlag, setEnvFlag] = useState(null);
  const [fbSaving, setFbSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api('/model-policy');
      setRows(d.policy.external_candidates || []);
      setEnvFlag(d.env_flag || {});
    } catch (e) { message.error(errText(e)); }
    setLoading(false);
  }, []);

  const setFallback = async (on) => {
    setFbSaving(true);
    try {
      const r = await api('/model-policy/fallback', { method: 'POST', body: JSON.stringify({ enabled: on }) });
      message.success(r.enabled ? '自动切换已开启' : '自动切换已关闭（重启后恢复 env 配置）');
      load();
    } catch (e) { message.error(errText(e)); }
    setFbSaving(false);
  };
  useEffect(() => { load(); }, [load]);

  const saveAll = async (next) => {
    const d = await api('/model-policy');
    await api('/model-policy', { method: 'PUT', body: JSON.stringify({
      external_candidates: next,
      internal_models: d.policy.internal_models || [],
      model_limits: d.policy.model_limits || [],
      load_balance_mode: d.policy.load_balance_mode || 'none',
      review_model: d.policy.review_model || {},
    })});
    message.success('已保存，热重载即时生效');
    load();
  };

  return (
    <Card title="外部候选（失败切换：rank→价格；溢出按上下文升序试）"
      extra={<Button type="primary"
        onClick={() => setModal({ mode: 'new' })}>新增候选</Button>}>
      <Space align="center" wrap style={{ display: 'flex', marginBottom: 12 }}>
        <Text>外网候选自动切换（fallback）：</Text>
        <Switch size="small" loading={fbSaving}
          checked={!['0', 'false', 'no', 'off'].includes(String(envFlag?.AI_GATEWAY_MODEL_FALLBACK ?? 'true').toLowerCase())}
          onChange={setFallback} />
        <Text type="secondary">关闭后报错/溢出不再自动切换；重启后恢复 env 配置</Text>
      </Space>
      <Table rowKey={(r) => `${r.provider}/${r.model}`} size="small" pagination={false}
        loading={loading} dataSource={rows}
        locale={{ emptyText: '暂无外部候选，点「新增候选」添加' }}
        columns={[
          { title: 'Provider', dataIndex: 'provider' },
          { title: '模型', dataIndex: 'model' },
          { title: '价格入($/1M)', dataIndex: 'price_per_1m_in' },
          { title: '价格出($/1M)', dataIndex: 'price_per_1m_out' },
          { title: '上下文', dataIndex: 'context_length' },
          { title: 'Rank', dataIndex: 'rank' },
          { title: '启用', dataIndex: 'enabled', render: (v, _, i) => (
            <Switch size="small" checked={!!v} onChange={(on) => {
              const next = rows.slice(); next[i] = { ...next[i], enabled: on };
              saveAll(next).catch((e) => message.error(errText(e)));
            }} />) },
          { title: '操作', render: (_, r, i) => (
            <Space>
              <Button type="link" onClick={() => setModal({ mode: 'edit', row: r, idx: i })}>编辑</Button>
              <Popconfirm title={`删除 ${r.provider}/${r.model}？`} onConfirm={() => {
                const next = rows.slice(); next.splice(i, 1);
                saveAll(next).catch((e) => message.error(errText(e)));
              }}><Button type="link" danger>删除</Button></Popconfirm>
            </Space>) },
        ]} />
      <ExternalCandidateModal
        open={!!modal}
        title={modal?.mode === 'edit' ? '编辑候选' : '新增候选'}
        initial={modal?.row}
        onCancel={() => setModal(null)}
        onOk={async (v) => {
          const next = rows.slice();
          if (modal.mode === 'edit') next[modal.idx] = { ...next[modal.idx], ...v, enabled: v.enabled !== false };
          else next.push({ ...v, enabled: v.enabled !== false });
          await saveAll(next);
          setModal(null);
          // 保存后路由检查器干跑一次：验证候选可解析 + 网关侧 key 可用
          try {
            const r = await api('/route-inspect', { method: 'POST', body: JSON.stringify({ model: `${v.provider}/${v.model}`, text: '你好' }) });
            const rt = r.routing || {};
            if (rt.error) message.warning(`候选验证：解析失败 ${rt.error}`);
            else if (rt.gateway_key && !rt.gateway_key.configured && rt.gateway_key.note) message.warning(`候选验证：${rt.gateway_key.note}`);
            else message.success(`候选验证：${v.provider}/${v.model} → ${rt.provider}（${rt.local ? '内网' : '外网'}）OK`);
          } catch (e) { /* 验证失败不阻塞保存 */ }
        }}
      />
    </Card>
  );
}
function L2RuntimeSection() {
  const [cfg, setCfg] = useState(null);
  const [saving, setSaving] = useState(false);
  const [loadedAt, setLoadedAt] = useState(null);
  // dirty：表单有未保存草稿（UI 展示用）；loadedRef：上次 GET 到的快照 JSON（脏判定基准）。
  const [dirty, setDirty] = useState(false);
  const loadedRef = useRef(null);
  const load = useCallback(async () => {
    try {
      const next = await api('/l2-config');
      loadedRef.current = JSON.stringify(next);
      setDirty(false);
      setCfg(next);
      setLoadedAt(new Date());
    } catch (e) { message.error(errText(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);
  // 切回标签页 / 窗口重新聚焦时拉一次（GET 读到的是运行时真值，非缓存）。
  // 刻意不做定时轮询：配置变更是低频事件，轮询只会在用户编辑途中冲掉草稿。
  useEffect(() => {
    const onBack = () => {
      if (document.hidden || dirty) return;
      load();
    };
    document.addEventListener('visibilitychange', onBack);
    window.addEventListener('focus', onBack);
    return () => {
      document.removeEventListener('visibilitychange', onBack);
      window.removeEventListener('focus', onBack);
    };
  }, [load, dirty]);
  const set = (k, v) => setCfg((c) => {
    const next = { ...c, [k]: v };
    setDirty(JSON.stringify(next) !== loadedRef.current);
    return next;
  });
  const save = async () => {
    setSaving(true);
    try {
      await api('/l2-config', { method: 'PUT', body: JSON.stringify({
        SMALL_MODEL_ENABLED: cfg.SMALL_MODEL_ENABLED,
        SMALL_MODEL_TIMEOUT: cfg.SMALL_MODEL_TIMEOUT,
        SMALL_MODEL_URL: cfg.SMALL_MODEL_URL,
        SMALL_MODEL_NAME: cfg.SMALL_MODEL_NAME,
        SMALL_MODEL_MAX_TOKENS: cfg.SMALL_MODEL_MAX_TOKENS,
        AI_GATEWAY_WL_L2_SAMPLE: cfg.AI_GATEWAY_WL_L2_SAMPLE,
        AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD: cfg.AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD,
        AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD: cfg.AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD,
        AI_GATEWAY_SUGGEST_ERROR_RATE: cfg.AI_GATEWAY_SUGGEST_ERROR_RATE,
      }) });
      message.success('已保存并生效（重启后仍生效）'); load();
    } catch (e) { message.error(errText(e)); }
    setSaving(false);
  };
  if (!cfg) return <Text type="secondary">加载中…</Text>;
  return (
    <div data-testid="l2cfg-section">
      <Space align="center" wrap>
        <Text strong style={{ fontSize: 14 }}>L2 运行配置</Text>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {loadedAt ? `数据取自 ${loadedAt.toLocaleTimeString('zh-CN', { hour12: false })}` : ''}
          {dirty ? '（有未保存改动）' : ''}
        </Text>
        <Button icon={<ReloadOutlined />} data-testid="l2cfg-reload"
          onClick={load}>刷新</Button>
      </Space>
      <Space align="center" wrap style={{ marginLeft: 8 }}>
        <Text>L2 总开关：</Text>
        <Switch size="small" checked={!!cfg.SMALL_MODEL_ENABLED}
          onChange={(v) => set('SMALL_MODEL_ENABLED', v)} />
        <Text>超时(s)：</Text>
        <Slider style={{ width: 160 }} min={0.5} max={60} step={0.5}
          value={cfg.SMALL_MODEL_TIMEOUT} onChange={(v) => set('SMALL_MODEL_TIMEOUT', v)} />
        <Text>白名单抽样：</Text>
        <Slider style={{ width: 160 }} min={0} max={1} step={0.05}
          value={cfg.AI_GATEWAY_WL_L2_SAMPLE} onChange={(v) => set('AI_GATEWAY_WL_L2_SAMPLE', v)} />
      </Space>
      <Space align="center" wrap style={{ marginTop: 12 }}>
        <Text>端点：</Text>
        <Input size="small" style={{ width: 320 }} value={cfg.SMALL_MODEL_URL || ''}
          onChange={(e) => set('SMALL_MODEL_URL', e.target.value)} />
        <Text>模型：</Text>
        <Input size="small" style={{ width: 200 }} value={cfg.SMALL_MODEL_NAME || ''}
          onChange={(e) => set('SMALL_MODEL_NAME', e.target.value)} />
        <Text>max_tokens：</Text>
        <InputNumber size="small" min={8} max={512} value={cfg.SMALL_MODEL_MAX_TOKENS}
          onChange={(v) => set('SMALL_MODEL_MAX_TOKENS', v)} />
      </Space>
      <Space align="center" wrap style={{ marginTop: 12, marginLeft: 8 }}>
        <Text>建议阈值 触发/量/误报率：</Text>
        <InputNumber size="small" min={1} max={1000} value={cfg.AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD}
          onChange={(v) => set('AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD', v)} />
        <InputNumber size="small" min={1} max={100000} value={cfg.AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD}
          onChange={(v) => set('AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD', v)} />
        <InputNumber size="small" min={0} max={1} step={0.01} value={cfg.AI_GATEWAY_SUGGEST_ERROR_RATE}
          onChange={(v) => set('AI_GATEWAY_SUGGEST_ERROR_RATE', v)} />
        <Button type="primary" loading={saving} onClick={save}>保存生效</Button>
      </Space>
    </div>
  );
}

// L2 提示词（system + user 模板）在线编辑：热生效 + 重启后仍生效（落盘 l2_overrides.yaml）。
// 占位符：{filename} {sheet_names} {headers} {preview}。空串=还原默认。
// 唯一使用方是 RulesPage（L2 拦截配置卡），故 export。
export function L2PromptsSection() {
  const [data, setData] = useState(null);
  const [draft, setDraft] = useState({ system: '', template: '' });
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const dirty = !!data && (draft.system !== data.system_prompt || draft.template !== data.user_prompt_template);

  const load = useCallback(async () => {
    try {
      const d = await api('/l2-prompts');
      setData(d);
      setDraft({ system: d.system_prompt, template: d.user_prompt_template });
    } catch (e) { message.error(errText(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setSaving(true);
    try {
      const r = await api('/l2-prompts', { method: 'PUT', body: JSON.stringify({
        AI_GATEWAY_L2_SYSTEM_PROMPT: draft.system,
        AI_GATEWAY_L2_USER_PROMPT_TEMPLATE: draft.template,
      })});
      message.success('已保存，下次 L2 判定即时生效');
      setData(r);
      setDraft({ system: r.system_prompt, template: r.user_prompt_template });
    } catch (e) { message.error(errText(e)); }
    setSaving(false);
  };
  const reset = async (which) => {
    const d = data?.defaults || {};
    const next = { ...draft, [which === 'system' ? 'system' : 'template']: which === 'system' ? d.system_prompt : d.user_prompt_template };
    setDraft(next);
  };
  if (!data) return <Card loading />;
  const placeholders = data.placeholders || [];
  return (
    <div data-testid="l2prompts-section">
      <Space align="center" style={{ marginBottom: 4 }}>
        <Text strong>L2 提示词</Text>
        <Button type="link" onClick={() => setOpen(true)}>查看/编辑</Button>
        <Text type="secondary" style={{ fontSize: 12 }}>{dirty ? '（有未保存改动）' : ''}</Text>
        <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
      </Space>
      <Row gutter={8}>
        <Col span={12}><Text strong style={{ display: 'block', marginBottom: 12 }}>SYSTEM</Text><Typography.Paragraph style={{ maxHeight: 200, overflow: 'auto', marginBottom: 0, whiteSpace: 'pre-wrap' }}>{data.system_prompt.slice(0, 200)}{data.system_prompt.length > 200 ? '…' : ''}</Typography.Paragraph></Col>
        <Col span={12}><Text strong style={{ display: 'block', marginBottom: 12 }}>USER 模板</Text><Typography.Paragraph style={{ maxHeight: 200, overflow: 'auto', marginBottom: 0, whiteSpace: 'pre-wrap' }}>{data.user_prompt_template}</Typography.Paragraph></Col>
      </Row>
      <Modal title="编辑 L2 提示词" open={open} onCancel={() => setOpen(false)} destroyOnClose
             okText="保存" cancelText="取消" confirmLoading={saving} width={760}
             onOk={async () => { await save(); setOpen(false); }}>
        <Space direction="vertical" style={{ width: '100%' }} size="small">
          <Text>system prompt（角色/判定口径）</Text>
          <Input.TextArea rows={10} value={draft.system} onChange={(e) => setDraft({ ...draft, system: e.target.value })} />
          <Space>
            <Button onClick={() => reset('system')}>还原默认</Button>
            <Text type="secondary">字数：{draft.system.length}/8000</Text>
          </Space>
          <Text>user prompt 模板（占位符：{placeholders.join(' / ')}）</Text>
          <Input.TextArea rows={6} value={draft.template} onChange={(e) => setDraft({ ...draft, template: e.target.value })} />
          <Space>
            <Button onClick={() => reset('template')}>还原默认</Button>
            <Text type="secondary">字数：{draft.template.length}/2000</Text>
          </Space>
        </Space>
      </Modal>
    </div>
  );
}


// 内部模型（route_local/block 降级目标）：登记可增删改，保存走 PUT /model-policy（热重载），
// 保存后 route-inspect 干跑验证目标可解析且落在内网。
function InternalModelsCard() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [mode, setMode] = useState('none');
  const [modal, setModal] = useState(null);
  const [form] = Form.useForm();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api('/model-policy');
      setRows(d.policy.internal_models || []);
      setMode(d.policy.load_balance_mode || 'none');
    } catch (e) { message.error(errText(e)); }
    setLoading(false);
  }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (modal) {
      form.resetFields();
      form.setFieldsValue({ provider: '', model: '', note: '', enabled: true, ...(modal.row || {}) });
    }
  }, [modal]); // eslint-disable-line react-hooks/exhaustive-deps

  const saveAll = async (next) => {
    const d = await api('/model-policy');
    await api('/model-policy', { method: 'PUT', body: JSON.stringify({
      external_candidates: d.policy.external_candidates || [],
      internal_models: next,
      model_limits: d.policy.model_limits || [],
      load_balance_mode: d.policy.load_balance_mode || 'none',
      review_model: d.policy.review_model || {},
    })});
    message.success('已保存，热重载即时生效');
    load();
  };

  return (
    <Card title="内部模型（降级目标登记）"
      extra={<Button type="primary" onClick={() => setModal({ mode: 'new' })}>新增内部模型</Button>}>
      <Table data-testid="mp-internal-table" rowKey={(r) => `${r.provider}/${r.model}`} size="small" pagination={false}
        loading={loading} dataSource={rows}
        locale={{ emptyText: '暂无内部模型，点「新增内部模型」添加' }}
        columns={[
          { title: 'Provider', dataIndex: 'provider' },
          { title: '模型', dataIndex: 'model' },
          { title: '备注', dataIndex: 'note' },
          { title: '启用', dataIndex: 'enabled', render: (v, _, i) => (
            <Switch size="small" checked={!!v} onChange={(on) => {
              const next = rows.slice(); next[i] = { ...next[i], enabled: on };
              saveAll(next).catch((e) => message.error(errText(e)));
            }} />) },
          { title: '操作', render: (_, r, i) => (
            <Space>
              <Button type="link" size="small" onClick={() => setModal({ mode: 'edit', row: r, idx: i })}>编辑</Button>
              <Popconfirm title={`删除 ${r.provider}/${r.model}？`} onConfirm={() => {
                const next = rows.slice(); next.splice(i, 1);
                saveAll(next).catch((e) => message.error(errText(e)));
              }}><Button type="link" size="small" danger>删除</Button></Popconfirm>
            </Space>) },
        ]} />
      <Space style={{ marginTop: 12 }} align="center">
        <Text>分流模式：</Text>
        <Select size="small" style={{ width: 180 }} value={mode} onChange={setMode} disabled
          options={[
            { value: 'none', label: '不分流（单模型）' },
            { value: 'user_hash', label: '用户均衡（预留）' },
            { value: 'load', label: '负载均衡（预留）' },
          ]} />
        <Text type="secondary">当前仅一个内部模型，分流功能暂不实现</Text>
      </Space>
      <Modal title={modal?.mode === 'edit' ? `编辑内部模型 ${modal.row?.model || ''}` : '新增内部模型'}
        open={!!modal} destroyOnClose onCancel={() => setModal(null)}
        onOk={async () => {
          const v = await form.validateFields();
          const next = rows.slice();
          if (modal.mode === 'edit') next[modal.idx] = { ...next[modal.idx], ...v };
          else next.push(v);
          try { await saveAll(next); } catch (e) { message.error(errText(e)); return; }
          setModal(null);
          // 保存后干跑验证：目标可解析 + 必须落在内网 provider（内部模型指外网是配置错误）
          try {
            const r = await api('/route-inspect', { method: 'POST', body: JSON.stringify({ model: `${v.provider}/${v.model}`, text: '你好' }) });
            const rt = r.routing || {};
            if (rt.error) message.warning(`验证：解析失败 ${rt.error}`);
            else if (!rt.local) message.warning(`验证：${v.provider}/${v.model} 解析到外网 provider，内部模型应选内网 provider`);
            else message.success(`验证：${v.provider}/${v.model} → ${rt.provider}（内网）OK`);
          } catch (e) { /* 验证失败不阻塞保存 */ }
        }}>
        <Form form={form} layout="vertical" preserve={false}>
          <ExternalCandidateFormItems form={form} />
          <Form.Item name="note" label="备注"><Input /></Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}

// 模型限流：最终解析到该模型的全局 RPM（全客户端共享桶），超限 429。
// model 支持裸模型名（任意 provider）或 provider/model（双匹配）。
function ModelLimitsCard() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [modal, setModal] = useState(null);
  const [form] = Form.useForm();
  const [knownModels, setKnownModels] = useState([]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api('/model-policy');
      setRows(d.policy.model_limits || []);
      const cands = (d.policy.external_candidates || []).map((c) => `${c.provider}/${c.model}`);
      const interns = (d.policy.internal_models || []).map((m) => `${m.provider}/${m.model}`);
      let defaults = [];
      try {
        const p = await api('/providers');
        defaults = (p.providers || []).map((x) => (x.default_model ? `${x.name}/${x.default_model}` : '')).filter(Boolean);
      } catch (e) { /* 下拉缺省模型可手输 */ }
      setKnownModels([...new Set([...cands, ...interns, ...defaults])].sort());
    } catch (e) { message.error(errText(e)); }
    setLoading(false);
  }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (modal) {
      form.resetFields();
      form.setFieldsValue({ model: '', rpm: 0, note: '', enabled: true, ...(modal.row || {}) });
    }
  }, [modal]); // eslint-disable-line react-hooks/exhaustive-deps

  const saveAll = async (next) => {
    const d = await api('/model-policy');
    await api('/model-policy', { method: 'PUT', body: JSON.stringify({
      external_candidates: d.policy.external_candidates || [],
      internal_models: d.policy.internal_models || [],
      model_limits: next,
      load_balance_mode: d.policy.load_balance_mode || 'none',
      review_model: d.policy.review_model || {},
    })});
    message.success('已保存，热重载即时生效');
    load();
  };

  return (
    <Card title="模型限流（全局，超限 429）"
      extra={<Button type="primary" onClick={() => setModal({ mode: 'new' })}>新增限流</Button>}>
      <Table rowKey="model" size="small" pagination={false}
        loading={loading} dataSource={rows}
        locale={{ emptyText: '未配置限流（全部不限）' }}
        columns={[
          { title: '模型', dataIndex: 'model', render: (v) => <Text code>{v}</Text> },
          { title: 'RPM', dataIndex: 'rpm', render: (v) => (v > 0 ? v : <Tag>不限</Tag>) },
          { title: '备注', dataIndex: 'note' },
          { title: '启用', dataIndex: 'enabled', render: (v, _, i) => (
            <Switch size="small" checked={!!v} onChange={(on) => {
              const next = rows.slice(); next[i] = { ...next[i], enabled: on };
              saveAll(next).catch((e) => message.error(errText(e)));
            }} />) },
          { title: '操作', render: (_, r, i) => (
            <Space>
              <Button type="link" size="small" onClick={() => setModal({ mode: 'edit', row: r, idx: i })}>编辑</Button>
              <Popconfirm title={`删除 ${r.model} 的限流？`} onConfirm={() => {
                const next = rows.slice(); next.splice(i, 1);
                saveAll(next).catch((e) => message.error(errText(e)));
              }}><Button type="link" size="small" danger>删除</Button></Popconfirm>
            </Space>) },
        ]} />
      <Text type="secondary">按最终路由到的模型计（别名/前缀重写后不逃限）；全客户端共享一个桶，保护 vLLM 容量与外网额度。</Text>
      <Modal title={modal?.mode === 'edit' ? `编辑限流 ${modal.row?.model || ''}` : '新增模型限流'}
        open={!!modal} destroyOnClose onCancel={() => setModal(null)}
        onOk={async () => {
          const v = await form.validateFields();
          const next = rows.slice();
          if (modal.mode === 'edit') next[modal.idx] = { ...next[modal.idx], ...v };
          else next.push(v);
          try { await saveAll(next); } catch (e) { message.error(errText(e)); return; }
          setModal(null);
        }}>
        <Form form={form} layout="vertical" preserve={false}>
          <Form.Item name="model" label="模型" rules={[{ required: true }]}
            tooltip="裸模型名 = 任意 provider 下该模型；provider/model = 仅匹配该 provider">
            <AutoComplete style={{ width: '100%' }}
              options={knownModels.map((v) => ({ value: v, label: v }))}
              filterOption={(inp, opt) => String(opt.value).toLowerCase().includes(String(inp).toLowerCase())}
              placeholder="下拉选候选/内部/默认模型，或手输（支持裸模型名）" />
          </Form.Item>
          <Form.Item name="rpm" label="每分钟上限（RPM，0=不限）">
            <InputNumber min={0} max={100000} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="note" label="备注"><Input /></Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}

export default function ModelPolicyPage() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api('/model-policy');
      setData(d);
    } catch (e) { message.error(errText(e)); }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading || !data) return <Card loading />;

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <AliasGroupsCard />
      <ExternalCandidatesCard />
      <InternalModelsCard />
      <ModelLimitsCard />
      <Card title="审查模型">
        <L2RuntimeSection />
      </Card>
    </Space>
  );
}


