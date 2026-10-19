import { useEffect, useState, useCallback, useRef } from 'react';
import { Table, Button, AutoComplete, Input, InputNumber, Modal, Form, Select, Tag, Space, message, Popconfirm, Typography, Card, Switch, Slider, Row, Col, Tooltip } from 'antd';
import { api, errText, usePoll } from '../api.js';
import { wideSticky } from '../utils.js';

const { Text } = Typography;
import ExternalCandidateModal, { ExternalCandidateFormItems } from './ExternalCandidateBits.jsx';
import WheelGate from './WheelGate.jsx';

// model_policy 无局部保存接口，整文档按段保存：GET 现值 → 换目标段 → PUT 全文。
// 候选/内部模型/限流三卡 + 别名弹窗内"新增候选"共用，防漏带其他段（曾漏 model_limits）。
// rows 可为数组或 (当前段现值) => 新数组（别名场景基于服务端现值追加，防本地态陈旧出重复）。
const savePolicySection = async (section, rows) => {
  const d = await api('/model-policy');
  const pol = d.policy;
  const cur = pol[section] || [];
  const next = typeof rows === 'function' ? rows(cur) : rows;
  await api('/model-policy', { method: 'PUT', body: JSON.stringify({
    external_candidates: section === 'external_candidates' ? next : pol.external_candidates || [],
    internal_models: section === 'internal_models' ? next : pol.internal_models || [],
    model_limits: section === 'model_limits' ? next : pol.model_limits || [],
    load_balance_mode: pol.load_balance_mode || 'none',
    review_model: pol.review_model || {},
  })});
};

function AliasGroupsCard() {
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState(null);
  const [extCands, setExtCands] = useState([]);
  const [intModels, setIntModels] = useState([]);
  const [extLoading, setExtLoading] = useState(false);
  const [extModal, setExtModal] = useState(null); // null | "new"
  const [addForm] = Form.useForm();

  // silent=true：30s 轮询用（不点 loading 不闪 Spin、后台失败不弹错）
  const loadExt = useCallback(async (silent = false) => {
    if (!silent) setExtLoading(true);
    try {
      const d = await api('/model-policy');
      setExtCands(d.policy.external_candidates || []);
      setIntModels(d.policy.internal_models || []);
    } catch (e) { if (!silent) message.error(errText(e)); }
    finally { if (!silent) setExtLoading(false); }
  }, []);

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const d = await api('/aliases');
      setGroups(d.groups || []);
    } catch (e) { if (!silent) message.error(errText(e)); }
    finally { if (!silent) setLoading(false); }
    loadExt(silent);
  }, [loadExt]);
  useEffect(() => { load(); }, [load]);
  usePoll(() => load(true)); // 替代原刷新按钮（别名+外部候选一起刷）

  const save = async () => {
    const gname = (editing.name || '').trim();
    if (!gname) { message.error('请填写别名'); return; }
    if (!editing.members.length) { message.error('至少添加一个候选'); return; }
    try {
      const members = editing.members.map((m, i) => ({ ...m, priority: (i + 1) * 10 }));
      // URL 恒为原组名；改组名走 new_name（新建时两者相同，后端视为 no-op）
      await api(`/aliases/${encodeURIComponent(editing.origName || gname)}`, {
        method: 'PUT',
        body: JSON.stringify({ description: editing.description, members, new_name: gname }),
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
    <Card title="对外模型别名组"
          size="small" style={{ marginBottom: 16 }}
           extra={<Button type="primary"
                     onClick={() => { addForm.resetFields(); setEditing({ isNew: true, name: '', description: '', members: [] }); }}>新建别名组</Button>}>
      <Table
        sticky={wideSticky()}
        loading={loading}
        rowKey={(g) => g.name}
        dataSource={groups}
        pagination={false}
        size="small"
        tableLayout="fixed"
        columns={[
          /* 列宽全 %  + tableLayout=fixed：表跟随容器宽度，列按比例分配；操作列装两个链接按钮，给足 14% */
          { title: '别名组', dataIndex: 'name', width: '12%', render: (v) => <Tag color="blue">{v}</Tag> },
          { title: '候选链（顺序即优先级，第一个为默认）', width: '74%', render: (_, g) => (
              <Space direction="vertical" size={2}>
                {(g.members || []).map((m, i) => (
                  <Typography.Text key={i} type={m.enabled ? undefined : 'secondary'}>
                    {i === 0 && m.enabled ? <Tag color="green">默认</Tag> : null}
                    {m.provider}/{m.model}{m.enabled ? '' : '（停用）'}
                  </Typography.Text>
                ))}
              </Space>
          )},
          { title: '操作', width: '14%', render: (_, g) => (
              <Space size={4}>
                <Button type="link" onClick={() => { addForm.resetFields(); setEditing({
                  name: g.name, origName: g.name, description: g.description || '',
                  members: (g.members || []).map((m) => ({ provider: m.provider, model: m.model, priority: m.priority, enabled: !!m.enabled })),
                }); }}>编辑</Button>
                <Popconfirm title={`删除别名组 ${g.name}？`}
                            description="整组及候选链一并删除，/v1/models 立即不再展示，且不可恢复。"
                            okText="删除" okButtonProps={{ danger: true }} cancelText="取消"
                            onConfirm={() => remove(g.name)}>
                  <Button type="link" danger>删除</Button>
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
            <Form layout="inline" style={{ marginBottom: 8 }}>
              <Form.Item label="别名" required>
                <Input value={editing.name} style={{ width: 220 }}
                       onChange={(e) => setEditing({ ...editing, name: e.target.value })}
                       placeholder="如 AB-flash" />
              </Form.Item>
            </Form>
            <Form layout="inline" style={{ marginBottom: 8 }} form={addForm}
                  onFinish={(v) => {
                    const key = (v.cand || '').trim();
                    if (!key) return;
                    const hit = [...extCands, ...intModels].find((c) => `${c.provider}/${c.model}` === key);
                    if (!hit) { message.error('所选候选不在外部候选/内网模型表中，请先新增'); return; }
                    if (editing.members.some((m) => m.provider === hit.provider && m.model === hit.model)) {
                      message.warning('该候选已在链中'); return;
                    }
                    setEditing({ ...editing, members: [...editing.members, { provider: hit.provider, model: hit.model, priority: (editing.members.length + 1) * 10, enabled: true }] });
                    addForm.resetFields();
                  }}>
              <Form.Item name="cand" rules={[{ required: true, message: '请从外部候选/内网模型中选择' }]}>
                <Select showSearch placeholder="从外部候选/内网模型中选择（provider/model）" style={{ width: 340 }}
                        loading={extLoading}
                        options={[
                          ...extCands.map((c) => ({
                            value: `${c.provider}/${c.model}`,
                            label: `${c.provider}/${c.model}${c.enabled ? '' : '（停用）'}${c.note ? ` — ${c.note}` : ''}`,
                          })),
                          ...intModels.map((m) => ({
                            value: `${m.provider}/${m.model}`,
                            label: `${m.provider}/${m.model}（内网）${m.enabled ? '' : '·停用'}${m.note ? ` — ${m.note}` : ''}`,
                          })),
                        ]}
                        filterOption={(inp, opt) => String((opt && (opt.value || opt.label)) || '').toLowerCase().includes(inp.toLowerCase())} />
              </Form.Item>
              <Button htmlType="submit">添加候选</Button>
              <Button onClick={() => setExtModal("new")}>＋新增外部候选</Button>
            </Form>
            <Table rowKey={(_, i) => 'm' + i} size="small" sticky={wideSticky()} pagination={false} tableLayout="fixed"
                   dataSource={editing.members}
                   columns={[
                     { title: '#', width: '6%', render: (_, __, i) => (i === 0 ? <Tag color="green">默认</Tag> : i + 1) },
                     { title: 'Provider', dataIndex: 'provider', width: '30%' },
                     { title: 'Model', dataIndex: 'model', width: '32%' },
                     { title: '启用', dataIndex: 'enabled', width: '10%', render: (v, _, i) => (
                         <Switch size="small" aria-label="启用该候选" checked={v} onChange={(ck) => {
                           const ms = [...editing.members]; ms[i] = { ...ms[i], enabled: ck }; setEditing({ ...editing, members: ms });
                         }} /> )},
                     { title: '操作', width: '22%', render: (_, __, i) => (
                         <Space>
                           <Button disabled={i === 0} onClick={() => {
                             const ms = [...editing.members]; const t = ms[i - 1]; ms[i - 1] = ms[i]; ms[i] = t; setEditing({ ...editing, members: ms });
                           }}>上移</Button>
                           <Popconfirm title="删除该候选？" onConfirm={() => setEditing({ ...editing, members: editing.members.filter((_, k) => k !== i) })}>
                             <Button type="link" danger>删除</Button>
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
                  note: v.note || '', enabled: true, rank: v.rank ?? 100,
                };
                await savePolicySection('external_candidates', (cur) => [...cur, entry]);
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
  // "provider/model" -> {cost, tokens, rate_per_1m}：近 7d 实际花费（计费口径）
  const [spend, setSpend] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api('/model-policy');
      setRows(d.policy.external_candidates || []);
      setEnvFlag(d.env_flag || {});
      const sp = await api('/model-policy/spend').catch(() => null);
      setSpend((sp && sp.models) || {});
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
    await savePolicySection('external_candidates', next);
    message.success('已保存，热重载即时生效');
    load();
  };

  // 推荐等级：计算单价 + 实际花费共同决定（越便宜越推荐，纯展示派生值）。
  // 计算单价 = 配置的 (入+出)/2；实际费率 = 近 7d 实际花费/实际 token（含真实
  // in/out/cached 混合，无用量时按计算单价）；两者各自升序取排名后取平均：
  // 最便宜=高，最贵=低，其余=中。不改数据，切换优先级仍是 rank 字段。
  const calcPrice = (r) => ((Number(r.price_per_1m_in) || 0) + (Number(r.price_per_1m_out) || 0)) / 2;
  const rateOf = (r) => {
    const s = (spend || {})[`${r.provider}/${r.model}`];
    return (s && s.tokens > 0) ? s.rate_per_1m : calcPrice(r);
  };
  const recommendOf = (r) => {
    const n = rows.length;
    if (n <= 1) return '高';
    const key = (x) => `${x.provider}/${x.model}`;
    const rankBy = (get) => {
      const sorted = rows.slice().sort((a, b) => get(a) - get(b));
      return new Map(sorted.map((x, i) => [key(x), i]));
    };
    const rc = rankBy(calcPrice);
    const rr = rankBy(rateOf);
    if (rows.every((x) => calcPrice(x) === calcPrice(rows[0])) &&
        rows.every((x) => rateOf(x) === rateOf(rows[0]))) return '高';
    const avg = ((rc.get(key(r)) ?? 0) + (rr.get(key(r)) ?? 0)) / 2;
    return avg <= 0 ? '高' : avg >= n - 1 ? '低' : '中';
  };
  const spendTip = (r) => {
    const s = (spend || {})[`${r.provider}/${r.model}`];
    const actual = (s && s.tokens > 0)
      ? `实际 7d 花费 ¥${s.cost}（${s.tokens} tokens，有效费率 ¥${s.rate_per_1m}/1M）`
      : '近 7d 无用量（实际费率按计算单价）';
    return `计算单价 ¥${calcPrice(r)}/1M（入+出 /2）；${actual}。推荐等级=两者排名平均，越便宜越推荐；切换优先级 rank 在编辑里改`;
  };

  return (
    <Card title="外部候选"
      extra={<Button type="primary"
        onClick={() => setModal({ mode: 'new' })}>新增候选</Button>}>
      <Space align="center" wrap style={{ display: 'flex', marginBottom: 12 }}>
        <Text style={{ marginLeft: 10 }}>外网候选自动切换（fallback）：主候选失败/溢出时自动换到下一个外网候选</Text>
        <Switch size="small" aria-label="外网候选自动切换" loading={fbSaving}
          checked={!['0', 'false', 'no', 'off'].includes(String(envFlag?.AI_GATEWAY_MODEL_FALLBACK ?? 'true').toLowerCase())}
          onChange={setFallback} />
      </Space>
      <Table rowKey={(r) => `${r.provider}/${r.model}`} className="tbl-ext" size="small" sticky={wideSticky()} pagination={false}
        tableLayout="fixed"
        loading={loading} dataSource={rows}
        locale={{ emptyText: '暂无外部候选，点「新增候选」添加' }}
        columns={[
          /* 全部列显式 % 宽（fixed 布局下无宽 auto 列没有最窄列宽，会塌到 0） */
          { title: 'Provider', dataIndex: 'provider', width: '12%' },
          { title: '模型', dataIndex: 'model', width: '15%' },
          { title: '价格入(¥/1M)', dataIndex: 'price_per_1m_in', width: '13%' },
          { title: '价格出(¥/1M)', dataIndex: 'price_per_1m_out', width: '13%' },
          { title: '上下文', dataIndex: 'context_length', width: '11%' },
          { title: '推荐等级', width: '10%', render: (_, r) => {
            const lv = recommendOf(r);
            return (
              <Tooltip title={spendTip(r)}>
                <Tag color={lv === '高' ? 'green' : lv === '中' ? 'blue' : 'orange'}>{lv}</Tag>
              </Tooltip>
            );
          } },
          { title: '操作', width: '26%', render: (_, r, i) => (
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
  // 内部模型（审查模型下拉的数据源，含停用项——停用只控制 /v1/models 暴露，不影响下拉可选）
  const [internals, setInternals] = useState([]);
  // provider 展开地址（qwen 模式按所选模型自动推导端点用；/providers.raw 形态不可直接拼）
  const [provs, setProvs] = useState([]);
  // 只刷下拉数据源：内部模型表拨开关/增删后经 gateway:model-policy-saved 事件同步，
  // 不碰 cfg 草稿（用户编辑途中也不会被冲掉）。
  const loadInternals = useCallback(async () => {
    try {
      const d = await api('/model-policy');
      setInternals(d.policy.internal_models || []);
    } catch (e) { /* 下拉无数据时仍可显示现值，见 modelOptions 兜底 */ }
  }, []);
  const load = useCallback(async () => {
    try {
      const next = await api('/l2-config');
      loadedRef.current = JSON.stringify(next);
      setDirty(false);
      setCfg(next);
      setLoadedAt(new Date());
    } catch (e) { message.error(errText(e)); }
    loadInternals();
    try {
      const p = await api('/providers');
      setProvs(p.providers || []);
    } catch (e) { /* 推导失败则保存时沿用现值 */ }
  }, [loadInternals]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    window.addEventListener('gateway:model-policy-saved', loadInternals);
    return () => window.removeEventListener('gateway:model-policy-saved', loadInternals);
  }, [loadInternals]);
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
  const isLaya = (((cfg || {}).AI_GATEWAY_L2_BACKEND) || 'qwen') === 'laya';
  // qwen 模式端点自动推导：所选内部模型的 provider 展开地址 + /chat/completions。
  // 推导不出（如选了无 provider 的 laya）则沿用现值，绝不存空。
  const derivedUrl = (() => {
    if (!cfg || isLaya) return '';
    const entry = internals.find((m) => m.model === cfg.SMALL_MODEL_NAME);
    const base = String((provs.find((p) => p.name === (entry && entry.provider)) || {}).base_url_resolved || '').replace(/\/+$/, '');
    return base ? `${base}/chat/completions` : '';
  })();
  const effUrl = derivedUrl || (cfg || {}).SMALL_MODEL_URL || '';
  const cfgRow = (label, control) => (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
      <Text style={{ whiteSpace: 'nowrap' }}>{label}</Text>
      {control}
    </div>
  );
  const save = async () => {
    setSaving(true);
    try {
      await api('/l2-config', { method: 'PUT', body: JSON.stringify({
        SMALL_MODEL_ENABLED: cfg.SMALL_MODEL_ENABLED,
        SMALL_MODEL_TIMEOUT: cfg.SMALL_MODEL_TIMEOUT,
        SMALL_MODEL_URL: isLaya ? cfg.SMALL_MODEL_URL : effUrl,
        SMALL_MODEL_NAME: cfg.SMALL_MODEL_NAME,
        SMALL_MODEL_MAX_TOKENS: cfg.SMALL_MODEL_MAX_TOKENS,
        AI_GATEWAY_L2_BACKEND: cfg.AI_GATEWAY_L2_BACKEND || 'qwen',
        AI_GATEWAY_L2_SHADOW: !!cfg.AI_GATEWAY_L2_SHADOW,
        AI_GATEWAY_L2_SHADOW_URL: cfg.AI_GATEWAY_L2_SHADOW_URL || '',
        AI_GATEWAY_WL_L2_SAMPLE: cfg.AI_GATEWAY_WL_L2_SAMPLE,
      }) });
      message.success('已保存并生效（重启后仍生效）'); load();
    } catch (e) { message.error(errText(e)); }
    setSaving(false);
  };
  if (!cfg) return <Text type="secondary">加载中…</Text>;
  // 审查模型下拉：选项来自内部模型登记（含 laya 这类停用项）；现值不在登记内则兜底一项，保证不丢显示。
  const modelOptions = internals.map((m) => ({
    value: m.model,
    label: `${m.provider}/${m.model}${m.enabled ? '' : '（停用）'}`,
  }));
  if (cfg.SMALL_MODEL_NAME && !modelOptions.some((o) => o.value === cfg.SMALL_MODEL_NAME)) {
    modelOptions.push({ value: cfg.SMALL_MODEL_NAME, label: `${cfg.SMALL_MODEL_NAME}（未在内部模型登记）` });
  }
  // 地址类下拉：选项=登记了网址的内部模型（laya 类）；存的是网址原文，schema 不动。
  const urlOptions = internals.filter((m) => m.url).map((m) => ({
    value: m.url, label: `${m.provider}/${m.model}`,
  }));
  const urlOptionsWith = (cur) => ((cur && !urlOptions.some((o) => o.value === cur))
    ? [...urlOptions, { value: cur, label: `${cur}（未在内部模型登记）` }] : urlOptions);
  return (
    <div data-testid="l2cfg-section">
      <Space align="center" wrap>
        <Text strong style={{ fontSize: 14 }}>L2 运行配置</Text>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {loadedAt ? `数据取自 ${loadedAt.toLocaleTimeString('zh-CN', { hour12: false })}（切回本页自动重取）` : ''}
          {dirty ? '（有未保存改动）' : ''}
        </Text>
      </Space>
      <Space direction="vertical" size={10} style={{ width: '100%', marginLeft: 8, marginTop: 12 }}>
        {isLaya && cfgRow('端点：',
          <Select size="small" showSearch style={{ width: 320 }} value={cfg.SMALL_MODEL_URL || ''}
            placeholder="从内部模型中选择"
            options={urlOptionsWith(cfg.SMALL_MODEL_URL || '')}
            onChange={(v) => set('SMALL_MODEL_URL', v)}
            filterOption={(inp, opt) => String((opt && (opt.value || opt.label)) || '').toLowerCase().includes(inp.toLowerCase())} />)}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px 16px' }}>
          {cfgRow('超时(s)：',
            <Slider style={{ width: 280 }} min={0.5} max={60} step={0.5}
              value={cfg.SMALL_MODEL_TIMEOUT} onChange={(v) => set('SMALL_MODEL_TIMEOUT', v)} />)}
          {cfgRow('白名单抽样：',
            <Slider style={{ width: 280 }} min={0} max={1} step={0.05}
              value={cfg.AI_GATEWAY_WL_L2_SAMPLE} onChange={(v) => set('AI_GATEWAY_WL_L2_SAMPLE', v)} />)}
          {cfgRow('模型：',
            <Select size="small" showSearch style={{ width: 280 }} value={cfg.SMALL_MODEL_NAME || ''}
              disabled={isLaya} title={isLaya ? 'backend=laya 时模型字段不生效（/classify 无模型参数）' : '选项来自内部模型登记，改名先去内部模型登记'}
              placeholder="从内部模型中选择"
              options={modelOptions}
              onChange={(v) => set('SMALL_MODEL_NAME', v)}
              filterOption={(inp, opt) => String((opt && (opt.value || opt.label)) || '').toLowerCase().includes(inp.toLowerCase())} />)}
          {cfgRow('max_tokens：',
            <InputNumber size="small" min={8} max={512} value={cfg.SMALL_MODEL_MAX_TOKENS}
              onChange={(v) => set('SMALL_MODEL_MAX_TOKENS', v)} />)}
        </div>
      </Space>
      <Space align="center" wrap style={{ marginTop: 12 }}>
        <Text title="qwen 权威、laya 并行打标（shadow_* 进审计），backend=laya 时不生效">影子双跑：</Text>
        <Switch size="small" aria-label="影子双跑" checked={!!cfg.AI_GATEWAY_L2_SHADOW}
          disabled={(cfg.AI_GATEWAY_L2_BACKEND || 'qwen') === 'laya'}
          onChange={(v) => set('AI_GATEWAY_L2_SHADOW', v)} />
        <Text>影子端点：</Text>
        <Select size="small" showSearch style={{ width: 280 }} value={cfg.AI_GATEWAY_L2_SHADOW_URL || ''}
          placeholder="从内部模型中选择"
          options={urlOptionsWith(cfg.AI_GATEWAY_L2_SHADOW_URL || '')}
          onChange={(v) => set('AI_GATEWAY_L2_SHADOW_URL', v)}
           filterOption={(inp, opt) => String((opt && (opt.value || opt.label)) || '').toLowerCase().includes(inp.toLowerCase())} />
        {(cfg.AI_GATEWAY_L2_BACKEND || 'qwen') === 'laya'
          ? <Text type="secondary">backend=laya 时「端点」即 /classify（qwen 的模型/max_tokens 不生效）</Text>
          : null}
      </Space>
      <Space align="center" wrap style={{ marginTop: 12, marginLeft: 8 }}>
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
  const dirtyRef = useRef(false);
  dirtyRef.current = dirty;

  const load = useCallback(async () => {
    try {
      const d = await api('/l2-prompts');
      setData(d);
      // 从 L2 影子页「自动修补」带过来的候选 system prompt：预填并直接开编辑器（人工确认后保存）
      const pend = sessionStorage.getItem('gw-l2-propose-prompt');
      if (pend != null) {
        sessionStorage.removeItem('gw-l2-propose-prompt');
        setDraft({ system: pend, template: d.user_prompt_template });
        setOpen(true);
      } else {
        setDraft({ system: d.system_prompt, template: d.user_prompt_template });
      }
    } catch (e) { message.error(errText(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);
  // 30s 轮询（替代原刷新按钮）：有未保存编辑时跳过，轮询不得冲掉草稿
  usePoll(() => { if (!dirtyRef.current) load(); });

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
        <Text type="secondary" style={{ fontSize: 12 }}>{dirty ? '（有未保存改动）' : '30s 自动刷新（编辑中暂停）'}</Text>
      </Space>
      <WheelGate>
      <Row gutter={8}>
        <Col span={12}><Text strong style={{ display: 'block', marginBottom: 12 }}>SYSTEM</Text><Typography.Paragraph style={{ maxHeight: 200, overflow: 'auto', marginBottom: 0, whiteSpace: 'pre-wrap' }}>{data.system_prompt.slice(0, 200)}{data.system_prompt.length > 200 ? '…' : ''}</Typography.Paragraph></Col>
        <Col span={12}><Text strong style={{ display: 'block', marginBottom: 12 }}>USER 模板</Text><Typography.Paragraph style={{ maxHeight: 200, overflow: 'auto', marginBottom: 0, whiteSpace: 'pre-wrap' }}>{data.user_prompt_template}</Typography.Paragraph></Col>
      </Row>
      </WheelGate>
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
  // checking: 正在检查的行下标；结果存行上 _health（纯前端展示字段，不落盘——服务端 Pydantic extra=ignore）
  const [checking, setChecking] = useState(null);

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
  }, [modal]);

  const saveAll = async (next) => {
    await savePolicySection('internal_models', next);
    message.success('已保存，热重载即时生效');
    load();
  };

  // 单行健康检查：url 以 /classify 结尾（laya 类非 chat 模型）走 classify 探针，
  // 否则走 provider 连通检查（qwen 类 chat 模型的 url 只是展示用，不进 classify 探针）。
  // 两个接口都是现成的只读探针，不耗 token、不落盘。
  const checkRow = async (r, i) => {
    setChecking(i);
    try {
      let h;
      if (r.url && /\/classify\/?$/.test(r.url)) {
        const d = await api(`/l2-shadow-health?url=${encodeURIComponent(r.url)}`);
        h = { ok: d.ok, latency_ms: d.classify?.latency_ms ?? d.health?.latency_ms,
              error: d.error || '', label: d.classify?.label || '' };
      } else {
        const d = await api('/providers/check', { method: 'POST', body: JSON.stringify({ name: r.provider }) });
        const first = ((d.results || [])[0]) || {};
        h = { ok: !!first.ok, latency_ms: first.latency_ms, error: first.error || '' };
      }
      setRows((prev) => prev.map((x, k) => (k === i ? { ...x, _health: h } : x)));
      if (h.ok) message.success(`健康：${r.provider}/${r.model} 正常（${h.latency_ms ?? '?'}ms）`);
      else message.error(`异常：${r.provider}/${r.model} ${h.error || '未知错误'}`);
    } catch (e) { message.error(errText(e)); }
    setChecking(null);
  };

  return (
    <Card title="内部模型"
      extra={<Button type="primary" onClick={() => setModal({ mode: 'new' })}>新增内部模型</Button>}>
      <Table data-testid="mp-internal-table" rowKey={(r) => `${r.provider}/${r.model}`} size="small" sticky={wideSticky()} pagination={false}
        tableLayout="fixed"
        loading={loading} dataSource={rows}
        locale={{ emptyText: '暂无内部模型，点「新增内部模型」添加' }}
        columns={[
          /* 列宽全 % + tableLayout=fixed；操作列是按钮组，同样给 % 定比。
             Provider 列已删（2026-10-14）：内部模型都是同一个本机 provider，列里全是重复值；字段仍留（弹窗可编辑、路由要用） */
          { title: '模型', dataIndex: 'model', width: '20%', ellipsis: true },
          { title: '服务地址', dataIndex: 'url', width: '30%', ellipsis: true,
            render: (v) => (v ? <Text code style={{ fontSize: 12 }}>{v}</Text> : <Text type="secondary">—</Text>) },
          { title: '备注', dataIndex: 'note', width: '13%', ellipsis: true },
          { title: '健康', width: '26%', ellipsis: true, className: 'cell-center', render: (_, r, i) => (
            <Space size={4}>
              {!r._health ? <Text type="secondary">—</Text>
              : r._health.ok
                ? <Text type="success">正常 {r._health.latency_ms ?? '?'}ms{r._health.label ? ` ${r._health.label}` : ''}</Text>
                : <Text type="danger" title={r._health.error}>{r._health.error || '异常'}</Text>}
              <Button type="link" size="small" loading={checking === i} onClick={() => checkRow(r, i)}>检查</Button>
            </Space>) },
          { title: '操作', width: '11%', render: (_, r, i) => (
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
          else next.push({ ...v, enabled: true });
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
      form.setFieldsValue({ model: '', rpm: 0, note: '', ...(modal.row || {}) });
    }
  }, [modal]);

  const saveAll = async (next) => {
    await savePolicySection('model_limits', next);
    message.success('已保存，热重载即时生效');
    load();
  };

  return (
    <Card title="模型限流"
      extra={<Button type="primary" onClick={() => setModal({ mode: 'new' })}>新增限流</Button>}>
      <Table rowKey="model" className="tbl-limits" size="small" sticky={wideSticky()} pagination={false} tableLayout="fixed"
        loading={loading} dataSource={rows}
        locale={{ emptyText: '未配置限流（全部不限）' }}
        columns={[
          /* 全部列显式 % 宽（fixed 布局下无宽 auto 列没有最窄列宽，会塌到 0）。
             启用列已删（2026-10-14）：开关留在弹窗里，字段仍生效（model_rpm 卡口认 enabled） */
          { title: '模型', dataIndex: 'model', width: '20%', ellipsis: true, render: (v) => <Text code>{v}</Text> },
          { title: 'RPM', dataIndex: 'rpm', width: '20%', render: (v) => (v > 0 ? v : <Tag>不限</Tag>) },
          { title: '备注', dataIndex: 'note', width: '40%' },
          { title: '操作', width: '20%', render: (_, r, i) => (
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


