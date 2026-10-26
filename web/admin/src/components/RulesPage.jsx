import { useEffect, useState, useCallback, useRef } from 'react';
import { Table, Button, Input, InputNumber, Modal, Form, Select, Tag, Space, message, Popconfirm, Typography, Alert, Card, Slider, Checkbox, Tooltip, Radio, Switch } from 'antd';
import { RobotOutlined, DeleteOutlined } from '@ant-design/icons';
import { api, errText, usePoll } from '../api.js';
import { wideSticky } from '../utils.js';
import { Tip } from '../terms.jsx';

const { Text } = Typography;
import { ACTION_INFO, RuleZh } from '../ruleNames.jsx';
import { L2PromptsSection } from './ModelPolicyPage.jsx';
// ---------------- 5.1/5.2/5.3 规则管理 ----------------
// ---------------- L2 拦截配置（scope 两档 + 判密阈值；持久化 model_policy.yaml，热重载生效） ----------------
// 判定档 = L2 小模型的「视野」：每一档各取一段文本、各自判一次密；
// 任一档判 CONFIDENTIAL（且 confidence ≥ 阈值）⇒ 整条请求按 route_local 处理（转内网/拦截，内容不出境）。
const L2_SCOPE_META = [
  { key: 'last_user', label: '最近用户消息', tip: '判定基线，不受预算裁剪' },
  { key: 'tool', label: '工具结果', tip: '最后一条 tool 结果' },
  { key: 'long_text', label: '长文抽样', tip: '整段长文均匀抽样，含结尾' },
];

// 悬停说明：讲清「取哪段文本 + 例子 + 为什么开它」，口径与 main.py._select_l2_scopes 一致
const SCOPE_TIPS = {
  last_user: (
    <div style={{ maxWidth: 380, fontSize: 'var(--gw-fs-12)', lineHeight: 1.8 }}>
      <div><b>取什么</b>：本次请求里最后一条 role=user 的纯文本消息全文（带 tool_calls / tool_call_id 的消息跳过；多模态只取文本部分）。</div>
      <div style={{ marginTop: 'var(--gw-sp-6)' }}><b>为什么默认开</b>：它是判定基线 —— 用户真正问的那句话；且<b>不受预算裁剪</b>（工具结果 / 长文抽样档把 L2 文本预算用完，这一档照样审）。</div>
      <div style={{ marginTop: 'var(--gw-sp-6)' }}><b>例子</b></div>
      <div>· 用户问「帮我看下这份薪酬表」→ 这一档审的就是这句。</div>
      <div>· 请求里只有工具消息、没有 user 文本 → 这一档为空 ⇒ 不发这次 L2 调用（少一档就少一次小模型判定）。</div>
      <div style={{ marginTop: 'var(--gw-sp-6)' }}><b>代价</b>：每档 = 一次小模型判定（~230ms）。</div>
    </div>
  ),
  tool: (
    <div style={{ maxWidth: 380, fontSize: 'var(--gw-fs-12)', lineHeight: 1.8 }}>
      <div><b>取什么</b>：最后一条<b>工具结果</b>正文，截到 chunk 长度（默认 1200 字）。</div>
      <div style={{ marginTop: 'var(--gw-sp-6)' }}><b>为什么开它</b>：RAG 召回的内部文档、网页抓取结果都走这条路 —— 第三方内容进上下文的主通路。用户自己没提敏感词，工具结果里可能整段是内部资料。</div>
      <div style={{ marginTop: 'var(--gw-sp-6)' }}><b>例子</b></div>
      <div>· 用户问「总结刚检索到的资料」，工具返回含「图号 A-12 / 施工图 / 混凝土标号 C30」的内部文档 → 这一档判密 ⇒ 整条请求转内网，不出境。</div>
      <div>· 工具只返回「无结果」→ 这一档为空 ⇒ 不发这次调用。</div>
      <div style={{ marginTop: 'var(--gw-sp-6)' }}><b>注意</b>：只取<b>最后一条</b>工具结果，不是全部工具历史。</div>
    </div>
  ),
  long_text: (
    <div style={{ maxWidth: 380, fontSize: 'var(--gw-fs-12)', lineHeight: 1.8 }}>
      <div><b>取什么</b>：与「最近用户消息」<b>同一个源文本</b>（用户 query + 内联 OCR 文本），按 chunk（默认 1200 字）切窗后均匀抽样，最多 4 段（含结尾）。与「最近用户消息」重叠的开头不重复判定；那个档没勾时整段都参与抽样。单块槽位被其它档占用时相应减少。</div>
      <div style={{ marginTop: 'var(--gw-sp-6)' }}><b>为什么开它</b>：长粘贴（整份合同、长表格）的开头常是抬头与目录，敏感字段在更后面；只看开头会整段漏掉。抽样才能让 1 万字文档的中后段也有机会被审到。</div>
      <div style={{ marginTop: 'var(--gw-sp-6)' }}><b>例子</b></div>
      <div>· 粘贴 8000 字合同：开头是「XX 建设项目施工图设计说明」，第 7000 字处才有「结构图号 A-12 / 单价 480 元 / 决算 320 万」→ 抽样含结尾 ⇒ 能看到并判密。</div>
      <div>· 文本太短、没有可补抽的长文段（且开头已由「最近用户消息」覆盖）→ 这一档为空 ⇒ 不发这次调用。</div>
      <div style={{ marginTop: 'var(--gw-sp-6)' }}><b>预算</b>：与「工具结果」档共享 L2 文本预算（默认 6000 字，先到先用）；「最近用户消息」不占预算。</div>
    </div>
  ),
};

// L2 配置劈两页：本页管判定档/判密阈值/提示词，模型配置页管端点/模型/超时/影子/灰区下限。互链别让用户猜
const L2_CROSS_LINK = (
  <Tooltip title="端点 / 模型 / 超时 / 影子 / 灰区下限在「模型配置」页的审查模型卡">
    <Button type="link" size="small" style={{ padding: 0 }}
      onClick={() => { window.location.hash = '/modelpolicy'; }}>审查模型 →</Button>
  </Tooltip>
);

const SCOPE_OVERVIEW = (
  <div style={{ maxWidth: 380, fontSize: 'var(--gw-fs-12)', lineHeight: 1.8 }}>
    <div><b>判定档</b> = L2 小模型的「视野」：每一档各取一段文本、各自判一次密。</div>
    <div style={{ marginTop: 'var(--gw-sp-6)' }}><b>后果</b>：任一档判 <b>CONFIDENTIAL</b> 且 confidence ≥ 判密阈值 ⇒ 整条请求按 route_local 处理（转内网 / 拦截，内容不出境）。</div>
    <div style={{ marginTop: 'var(--gw-sp-6)' }}><b>块数上限 5</b>：各块并发判一次（单块 ~230ms，3 块 ~320ms、5 块 ~390ms）；块内文本为空则跳过（不发那次调用）。</div>
    <div style={{ marginTop: 'var(--gw-sp-6)' }}>悬停每个勾选框看该档取哪段文本 + 例子。</div>
  </div>
);

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
  if (!rm) return <Card title={<>L2 拦截配置<Tip term="l2" /></>} loading extra={L2_CROSS_LINK} />;
  return (
    <Card title={<>L2 拦截配置<Tip term="l2" /></>} extra={L2_CROSS_LINK}>
      <div style={{ marginBottom: 'var(--gw-sp-12)' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 'var(--gw-sp-16)' }}>
          <Tooltip title={SCOPE_OVERVIEW}>
            <Text strong style={{ cursor: 'help' }}>判定档（命中 CONFIDENTIAL → 本地路由拦截）</Text>
          </Tooltip>
          <Button type="primary" loading={saving} data-testid="l2rules-save" onClick={save}>保存</Button>
        </div>
        <Space wrap>
          {L2_SCOPE_META.map((m) => (
            <Tooltip key={m.key} title={SCOPE_TIPS[m.key] || m.tip}>
              <Checkbox data-testid={`l2scope-scopes-${m.key}`}
                checked={rm.scopes.has(m.key)} onChange={() => toggle('scopes', m.key)}>
                {m.label} <Text type="secondary" code>{m.key}</Text>
              </Checkbox>
            </Tooltip>
          ))}
        </Space>
      </div>
      <Space align="center" wrap>
        <Text>判密阈值：confidence ≥ {rm.threshold.toFixed(2)}</Text><Tip term="threshold" />
        <Slider style={{ width: 220 }} min={0} max={1} step={0.05}
          value={rm.threshold} onChange={(v) => setRm((c) => ({ ...c, threshold: v }))} />
      </Space>
      <div style={{ marginTop: 'var(--gw-sp-16)', paddingTop: 'var(--gw-sp-12)', borderTop: '1px solid var(--gw-border)' }}>
        <L2PromptsSection />
      </div>
    </Card>
  );
}

export default function RulesPage() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState(null); // 规则名或 null=新增
  const [prioSort, setPrioSort] = useState('ascend'); // 优先级仅升/降两态，点击直接切换
  const [form] = Form.useForm();
  const [aiLoading, setAiLoading] = useState(false);
  // 拦截模式 on_block：fallback_local（转内网回答）↔ reject（真实 403）；写 routing.yaml 热重载
  const [onBlockMode, setOnBlockMode] = useState('fallback_local');
  const [onBlockLoading, setOnBlockLoading] = useState(false);
  const loadOnBlock = async () => {
    try { const d = await api('/routing'); setOnBlockMode(d?.routing?.on_block || 'fallback_local'); }
    catch (e) { /* 读失败保持默认，不打断页面 */ }
  };
  useEffect(() => { loadOnBlock(); }, []);
  const toggleOnBlock = async (checked) => {
    const mode = checked ? 'reject' : 'fallback_local';
    setOnBlockLoading(true);
    try {
      await api('/routing/on-block', { method: 'PUT', body: JSON.stringify({ mode }) });
      setOnBlockMode(mode);
      message.success(mode === 'reject'
        ? '已切到真实拒绝：block 规则命中时客户端收到 403（密钥不送入任何模型）'
        : '已切到转内网兜底：block 规则命中时转内网本地模型回答（内容不出境）');
    } catch (e) { message.error(errText(e)); }
    finally { setOnBlockLoading(false); }
  };
  // rule target dropdowns: providers from routing.yaml, models proxied upstream
  const [providers, setProviders] = useState([]);
  const [modelOptions, setModelOptions] = useState([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsFailed, setModelsFailed] = useState(false);
  const [versions, setVersions] = useState([]);
  const [choices, setChoices] = useState(null);
  // 条件组构造器：多行（字段+匹配方式+匹配项）+ any/all 组合；单行时等价老单条件
  const [rows, setRows] = useState([]);
  const [combiner, setCombiner] = useState('any');
  // when_json 被手改后构造器暂停自动同步（whenDirtyRef 的内部镜像，UI 用）
  const [whenDirty, setWhenDirty] = useState(false);
  // 老规则的 when 逆解析不出（手写形状/无构造器等价物）：构造器空白有两义（无条件 vs 解析不出），显式提示
  const [unparsed, setUnparsed] = useState(false);
  // AI 生成的覆盖范围限定（允许生成的 content_type 集合）；空=不限
  const [aiScopes, setAiScopes] = useState([]);
  // 覆盖范围选项 = choices 里的 distinct content_type（唯一真源仍是后端 MATCH_CHOICES）；
  // bool 字段 label 首段共用「开关」，取第二段区分；title=该字段各匹配方式说明（悬停用）
  const scopeOptions = [...new Map((choices || []).map((c) => [c.content_type, c])).keys()].map((ct) => {
    const cts = (choices || []).filter((c) => c.content_type === ct);
    const c = cts[0];
    const parts = (c.label || '').split(' · ');
    return { value: ct,
      label: parts[0] === '开关' ? `开关·${(parts[1] || ct).split(' = ')[0]}` : (parts[0] || ct),
      title: [...new Set(cts.map((x) => x.desc).filter(Boolean))].join('；') };
  });
  const [verView, setVerView] = useState(null);
  // 构造器可选项来自后端 policy_admin.MATCH_CHOICES（唯一真源），不在前端抄一份
  const loadChoices = useCallback(async () => {
    try {
      const d = await api('/policies/match-choices');
      setChoices([...(d.choices || []), ...(d.self_field || [])]);
    } catch (e) { setChoices([]); }
  }, []);
  const VALUE_HINT = {
    regex: '正则表达式，如 AWS_SECRET|akia',
    contains: '关键词1, 关键词2',
    size: '字节数，如 1048576',
    in: '枚举值1, 枚举值2',
    not_in: '枚举值1, 枚举值2（都不匹配才算命中）',
    bool: 'true / false（是 / 否 也认）',
  };
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
  // 行状态用事件参数显式传进同步函数（不读 state），避免防抖闭包拿旧值。
  const progRef = useRef(false);
  const whenDirtyRef = useRef(false);
  const whenSyncTimer = useRef(null);
  // 编辑弹窗有未保存改动（表单或构造器动过手）：关闭前弹确认，destroyOnClose 静默丢草稿不可接受
  const editDirtyRef = useRef(false);
  const syncGroupFromBuilder = useCallback(async (rws, cmb) => {
    if (!modalOpen) return;
    // 还有行没填匹配项就不同步——否则空值必然 422（contains/in 要求至少一个值）弹误报，
    // when_json 保持原样，等全填齐再同步。
    if (rws.some((r) => !r.key || String(r.value || '').trim() === '')) return;
    try {
      const d = await api('/policies/build-when-group', {
        method: 'POST', body: JSON.stringify({
          combiner: cmb,
          rows: rws.map((r) => {
            const [ct, mt] = r.key.split('|');
            return { content_type: ct, match_type: mt, match_value: r.value };
          }),
        }),
      });
      progRef.current = true;
      form.setFieldsValue({ when_json: JSON.stringify(d.when, null, 2) });
      progRef.current = false;
    } catch (e) { message.error(errText(e)); }
  }, [modalOpen, form]);
  const scheduleSync = useCallback((rws, cmb) => {
    if (whenSyncTimer.current) clearTimeout(whenSyncTimer.current);
    whenSyncTimer.current = setTimeout(() => { syncGroupFromBuilder(rws, cmb); }, 400);
  }, [syncGroupFromBuilder]);
  const onFormValuesChange = (changed) => {
    if (progRef.current) return;
    editDirtyRef.current = true;
    if (changed.when_json !== undefined) {
      whenDirtyRef.current = true;
      setWhenDirty(true);
      if (whenSyncTimer.current) { clearTimeout(whenSyncTimer.current); whenSyncTimer.current = null; }
    }
  };
  const updateRow = (i, patch) => {
    const next = rows.map((r, j) => (j === i ? { ...r, ...patch } : r));
    whenDirtyRef.current = false;
    setWhenDirty(false);
    editDirtyRef.current = true;
    setRows(next);
    scheduleSync(next, combiner);
  };
  const removeRow = (i) => {
    const next = rows.filter((_, j) => j !== i);
    whenDirtyRef.current = false;
    setWhenDirty(false);
    editDirtyRef.current = true;
    setRows(next);
    scheduleSync(next, combiner);
  };
  const changeCombiner = (v) => {
    whenDirtyRef.current = false;
    setWhenDirty(false);
    editDirtyRef.current = true;
    setCombiner(v);
    scheduleSync(rows, v);
  };

  const loadVersions = useCallback(async () => {
    try { setVersions((await api('/policy/versions?limit=50')).items || []); } catch { setVersions([]); }
  }, []);
  // silent=true：30s 轮询用（不点 loading 不闪 Spin、后台失败不弹错）
  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try { setItems((await api('/policies')).items); } catch (e) { if (!silent) message.error(errText(e)); }
    finally { if (!silent) setLoading(false); }
    loadVersions();
  }, [loadVersions]);
  useEffect(() => { load(); }, [load]);
  usePoll(() => load(true)); // 替代原刷新按钮（规则+历史版本一起刷）

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

  // 构造器预填：parsed 是 {content_type,match_type,match_value}（单行）
  // 或 {combiner, rows:[...]}（条件组）；null = 留空单行（不覆盖 when_json）
  const prefillBuilder = (parsed) => {
    if (!parsed) { setCombiner('any'); setRows([{ key: '', value: '' }]); return; }
    const fmt = (v) => (Array.isArray(v) ? v.join(', ') : (v ?? ''));
    if (parsed.combiner && Array.isArray(parsed.rows) && parsed.rows.length) {
      setCombiner(parsed.combiner);
      setRows(parsed.rows.map((r) => ({ key: `${r.content_type}|${r.match_type}`, value: fmt(r.match_value) })));
    } else {
      setCombiner('any');
      setRows([{ key: `${parsed.content_type}|${parsed.match_type}`, value: fmt(parsed.match_value) }]);
    }
  };
  const openEdit = async (rec) => {
    setEditing(rec ? rec.name : null);
    setAiScopes([]);
    progRef.current = true;
    setWhenDirty(false);
    editDirtyRef.current = false;
    if (rec) {
      form.setFieldsValue({
        name: rec.name, priority: rec.priority, action: rec.action,
        description: rec.description, when_json: JSON.stringify(rec.when, null, 2),
        target_provider: rec.target?.provider || '', target_model: rec.target?.model || '',
        ai_description: '',
      });
      // 已有规则：优先用落库的 match（新建/编辑时持久化的构造器值，单行老形状或
      // {combiner, rows} 组形状，原样还原）；没有（老规则）再 parse-when 逆解析
      // （单条件或 any/all 条件组都能还原）。都认不出保持空白，绝不预填默认值。
      let pre = null;
      const pm = rec.match || {};
      if (pm.combiner && Array.isArray(pm.rows) && pm.rows.length) pre = pm;
      else if (pm.content_type && pm.match_type) pre = pm;
      if (!pre) {
        try {
          const d = await api('/policies/parse-when', {
            method: 'POST', body: JSON.stringify({ when: rec.when }),
          });
          if (d.parsed) pre = d.parsed;
        } catch { /* 保持空白，手动改 when_json */ }
      }
      // 解析不出且 when 非平凡 → 构造器空白是「解析不出」不是「无条件」
      setUnparsed(!pre && !!(rec.when && rec.when !== 'always'));
      prefillBuilder(pre);
    } else {
      setUnparsed(false);
      form.setFieldsValue({
        name: '', priority: 100, action: 'allow', description: '',
        when_json: '{"text contains_any": ["关键词1", "关键词2"]}',
        target_provider: '', target_model: '',
        ai_description: '',
      });
      setCombiner('any');
      setRows([{ key: 'prompt|contains', value: '' }]);
    }
    progRef.current = false;
    setModalOpen(true);
  };

  const aiGenerate = async () => {
    const desc = form.getFieldValue('ai_description');
    if (!desc) return;
    setAiLoading(true);
    try {
      // 累积追加：重复点击时把当前已有关键词传给后端排除，本次只出新词（regex/bool 行天然固定，不传）
      const used = rows
        .filter((r) => { const mt = String(r.key || '').split('|')[1]; return mt === 'contains' || mt === 'in' || mt === 'not_in'; })
        .flatMap((r) => String(r.value || '').split(',').map((s) => s.trim()).filter(Boolean));
      const d = await api('/l2/generate-rule', { method: 'POST', body: JSON.stringify({ description: desc, exclude: used, scopes: aiScopes }) });
      const s = d.suggestion;
      const fmt = (v) => (Array.isArray(v) ? v.join(', ') : String(v ?? ''));
      // AI 连字段一起选（1-4 行 any/all）；旧格式 {match_type,value} 按提示词单行兜底
      let next;
      if (Array.isArray(s.rows) && s.rows.length) {
        next = s.rows.map((r) => ({ key: `${r.content_type}|${r.match_type}`, value: fmt(r.value) }));
      } else {
        const mt = s.match_type === 'contains' ? 'contains' : 'regex';
        next = [{ key: `prompt|${mt}`, value: fmt(s.value) }];
      }
      // 累积合并（只增不减）：关键词类按 (字段,方式) 取并集追加，已有项不删；
      // regex/bool 行已有则保留、没有才补；combiner 沿用已有行，首生成才取 AI 给的
      const kw = (k) => ['contains', 'in', 'not_in'].includes(String(k || '').split('|')[1]);
      const cur = rows.filter((r) => r.key && String(r.value ?? '').trim() !== '');
      const merged = cur.map((r) => ({ ...r }));
      let added = 0;
      for (const r of next) {
        const i = merged.findIndex((m) => m.key === r.key);
        if (i < 0) {
          merged.push(r);
          added += kw(r.key) ? String(r.value).split(',').filter((x) => x.trim()).length : 1;
          continue;
        }
        if (kw(r.key)) {
          const old = String(merged[i].value).split(',').map((x) => x.trim()).filter(Boolean);
          const news = String(r.value).split(',').map((x) => x.trim()).filter(Boolean).filter((x) => !old.includes(x));
          if (news.length) { merged[i] = { ...merged[i], value: [...old, ...news].join(', ') }; added += news.length; }
        }
      }
      const cmb = cur.length ? combiner : (s.combiner || 'any');
      whenDirtyRef.current = false;
      setWhenDirty(false);
      editDirtyRef.current = true; // AI 生成的匹配项也是未保存改动
      setCombiner(cmb);
      setRows(merged);
      scheduleSync(merged, cmb);
      if (added) message.success(`新增 ${added} 个匹配项（${s.explanation}），已有项保留，请确认后保存`);
      else message.warning(`本次未产生新词（${s.explanation}），已有项保留，可再试`);
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
      // 构造器匹配信息随规则持久化（下次编辑原样还原）：单行用老形状
      // {content_type,match_type,match_value}（兼容老规则），多行用 {combiner, rows}。
      // when_json 被手改过（与构造器不同步）或行未填齐时不落 match。
      const filled = rows.filter((r) => r.key && String(r.value ?? '').trim() !== '');
      if (filled.length === rows.length && filled.length > 0 && !whenDirtyRef.current) {
        body.match = filled.length === 1
          ? { content_type: filled[0].key.split('|')[0], match_type: filled[0].key.split('|')[1], match_value: filled[0].value }
          : { combiner, rows: filled.map((r) => ({ content_type: r.key.split('|')[0], match_type: r.key.split('|')[1], match_value: r.value })) };
      }
      const r = editing
        ? await api(`/policies/${encodeURIComponent(editing)}`, { method: 'PUT', body: JSON.stringify(body) })
        : await api('/policies', { method: 'POST', body: JSON.stringify(body) });
      message.success(r.ok ? '已保存并生效' : '完成');
      editDirtyRef.current = false;
      setWhenDirty(false);
      setModalOpen(false);
      load();
    } catch (e) { if (e.errorFields) return; message.error(errText(e)); }
  };

  const del = async (name) => {
    try { await api(`/policies/${encodeURIComponent(name)}`, { method: 'DELETE' }); load(); }
    catch (e) { message.error(errText(e)); }
  };

  // 关弹窗（取消/X/ESC）走这里：改过就确认一次，防静默丢草稿
  const requestClose = () => {
    if (editDirtyRef.current) {
      Modal.confirm({
        title: '放弃未保存的修改？',
        content: '弹窗里的规则内容还没保存，关闭后不会保留。',
        okText: '放弃修改', okButtonProps: { danger: true }, cancelText: '继续编辑',
        onOk: () => { editDirtyRef.current = false; setEditing(null); setModalOpen(false); },
      });
      return;
    }
    setEditing(null); setModalOpen(false);
  };

  const cols = [
    /* 列宽全 % + tableLayout=fixed：跟随容器、按比例分配。原 scroll.x=1070 定宽
       在窄屏撑出横向滚动；fixed 布局下 % 才是真·定比（auto 布局 % 只是建议值） */
    { title: <>{'优先级'}<Tip term="rule_priority" /></>, dataIndex: 'priority', width: '10%', sorter: (a, b) => a.priority - b.priority, sortOrder: prioSort },
    { title: '名称', dataIndex: 'name', width: '15%', ellipsis: true, align: 'center', className: 'cell-center',
      render: (v) => <Text code title={v}><RuleZh name={v} /></Text> },
    { title: <>{'动作'}<Tip term="action" /></>, dataIndex: 'action', width: '12%',
      render: (v) => {
        const [name, color] = ACTION_INFO[v] || [v || '—', undefined];
        return <Tag color={color} title={v}>{name}</Tag>;
      } },
    { title: '说明', dataIndex: 'description', width: '43%', ellipsis: true },
    // 操作列要装下 编辑+删除 两个默认尺寸按钮 + 24/24 cell padding，
    // 150 会撑出 wrapper 右缘（2026-10 实测）
    { title: '操作', width: '20%',
      render: (_, rec) => (
        <Space>
          <Button type="link" onClick={() => openEdit(rec)}>编辑</Button>
          {rec.name !== '默认放行' && (
            <Popconfirm title="确认删除该规则？" onConfirm={() => del(rec.name)}>
              <Button type="link" data-testid={`rules-del-${rec.name}`} danger>删除</Button>
            </Popconfirm>
          )}
        </Space>
      ) },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card title={<>L1 规则<Tip term="l1" /></>} extra={
        <Space>
          <Button type="primary" onClick={() => openEdit(null)}>新增规则</Button>
        </Space>
      }>
        <Table rowKey="name" className="tbl-l1" columns={cols} dataSource={items} loading={loading} size="small"
          sticky={wideSticky()}
          tableLayout="fixed"
          pagination={{ pageSize: 50, showSizeChanger: false }}
          onChange={(_p, _f, sorter, extra) => {
            // antd 默认三态 升→降→取消(null)；把 null 映射回升序 ⇒ 只有升/降两态，点击直接切换
            if (extra.action === 'sort') setPrioSort(sorter.order === 'descend' ? 'descend' : 'ascend');
          }} />
      </Card>

      <Card title={<>拦截模式 on_block<Tip term="on_block" /></>} size="small" style={{ marginTop: 'var(--gw-sp-12)' }}>
        <Space align="center" wrap>
          <Switch checked={onBlockMode === 'reject'} loading={onBlockLoading}
            onChange={toggleOnBlock}
            checkedChildren="reject（真实 403）" unCheckedChildren="fallback_local（转内网回答）" />
          <Text type="secondary" style={{ fontSize: 'var(--gw-fs-12)', marginLeft: 'var(--gw-sp-8)' }}>
            {onBlockMode === 'reject'
              ? 'block 规则命中时客户端直接收到 403，密钥不送入任何模型上下文'
              : 'block 规则命中时转内网本地模型回答（内容不出境，审计记 downgraded_from: <命中的规则名>）'}
          </Text>
        </Space>
      </Card>

      <L2RulesCard />

      <Card title="历史版本" style={{ marginTop: 'var(--gw-sp-12)' }}>
        {versions.length ? (
          <Table rowKey="id" size="small" scroll={{ y: 400 }} pagination={false} tableLayout="fixed" dataSource={versions} columns={[
            /* 列宽全 % + tableLayout=fixed；操作列两个链接按钮，14% 够放 */
            { title: '时间', dataIndex: 'ts', width: '16%' },
            { title: '变更内容（与下一更新状态比）', width: '70%', ellipsis: true,
              render: (_, r) => {
                const cs = r.changes;
                const text = (Array.isArray(cs) && cs.length) ? cs.join('；') : '无变化（内容相同）';
                return <span title={text}>{text}</span>;
              } },
            { title: '操作', width: '14%', render: (_, r) => (
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
        <pre style={{ maxHeight: 480, overflow: 'auto', fontSize: 'var(--gw-fs-12)' }}>{verView || ''}</pre>
      </Modal>

      <Modal title={editing ? `编辑规则：${editing}` : '新增规则'} open={modalOpen}
        destroyOnClose onOk={submit} onCancel={requestClose} width={680}>
        <Form form={form} layout="vertical" onValuesChange={onFormValuesChange}>
          <Space style={{ width: '100%' }} size="middle">
            <Form.Item name="name" label="规则名" rules={[{ required: true }]}>
              <Input id="rule-name" />
            </Form.Item>
            <Form.Item name="priority" label={<>{'优先级（小者先）'}<Tip term="rule_priority" /></>}>
              <InputNumber min={0} max={1000} />
            </Form.Item>
            <Form.Item name="action" label={<>{'动作'}<Tip term="action" /></>} rules={[{ required: true }]}>
              <Select style={{ width: 140 }} options={[
                { value: 'allow', label: 'allow 放行' },
                { value: 'route_local', label: 'route_local 转内网' },
                { value: 'block', label: 'block 拦截' },
              ]} />
            </Form.Item>
          </Space>
          <Card title={<>匹配方式构造器 <Text type="secondary" style={{ fontSize: 'var(--gw-fs-12)', fontWeight: 'normal' }}>（字段悬停看说明）</Text>
            {unparsed && <Tag color="orange" style={{ marginLeft: 'var(--gw-sp-8)', fontWeight: 'normal' }}>
              原条件解析不出（手写形状），JSON 原样保留；在下方填齐新条件会替换它
            </Tag>}
            {whenDirty && <Tag color="orange" style={{ marginLeft: 'var(--gw-sp-8)', fontWeight: 'normal' }}>JSON 已手改，构造器同步暂停（再改构造器任一条件即恢复）</Tag>}
          </>} style={{ marginBottom: 'var(--gw-sp-12)' }}>
            <Space direction="vertical" style={{ width: '100%' }} size={6}>
              {rows.map((r, i) => {
                const mt = String(r.key || '').split('|')[1] || '';
                const choice = (choices || []).find((c) => c.key === r.key);
                return (
                  <Space key={i} align="center" wrap>
                    <Tooltip title={choice ? choice.desc : undefined} overlayStyle={{ maxWidth: 400 }}>
                      <Text style={{ display: 'inline-block', minWidth: 200, paddingLeft: 'var(--gw-sp-12)' }}>{choice ? choice.label : (r.key || '—')}</Text>
                    </Tooltip>
                    <Input id={rows.length === 1 ? 'rule-match-value' : `rule-row-value-${i}`}
                      style={{ minWidth: 260 }} placeholder={VALUE_HINT[mt] || '匹配项'}
                      value={r.value} onChange={(e) => updateRow(i, { value: e.target.value })} />
                    {rows.length > 1 && (
                      <Button type="text" danger icon={<DeleteOutlined />} aria-label="删除条件"
                        onClick={() => removeRow(i)} />
                    )}
                  </Space>
                );
              })}
              {rows.length > 1 && (
                <Radio.Group size="small" value={combiner} onChange={(e) => changeCombiner(e.target.value)}>
                  <Radio.Button value="any">任一命中</Radio.Button>
                  <Radio.Button value="all">全部命中</Radio.Button>
                </Radio.Group>
              )}
            </Space>
            <Space style={{ marginTop: 'var(--gw-sp-8)' }} wrap>
              {/* 覆盖范围：自定义下拉——选项内嵌 Tooltip 会被 rc-select 悬停重渲染吞掉
                （实测含 delay=0），悬停说明挂自建列表行上，与行字段标签同款 antd Tooltip；
                display:none 带上原生菜单仅为挂载 OptionList，恢复 rc-select 键盘机制
                （否则 Escape 会绕过 listbox 直达弹窗 wrap 把弹窗关掉） */}
              <Select id="ai-scopes" mode="multiple" allowClear showSearch={false}
                placeholder="覆盖范围（不选=不限）"
                style={{ width: 280 }} options={scopeOptions} value={aiScopes}
                dropdownRender={(menu) => (
                  <div>
                    <div style={{ display: 'none' }}>{menu}</div>
                    <div style={{ maxHeight: 300, overflowY: 'auto' }}>
                    <style>{'.sc-opt{padding:5px 12px;cursor:pointer;display:flex;align-items:center;gap:8px}.sc-opt:hover{background:var(--gw-track)}'}</style>
                    {scopeOptions.map((o) => {
                      const on = aiScopes.includes(o.value);
                      return (
                        <Tooltip key={o.value} title={o.title} overlayStyle={{ maxWidth: 400 }}>
                          <div className="sc-opt"
                            onClick={() => {
                              setAiScopes((c) => (on ? c.filter((x) => x !== o.value) : [...c, o.value]));
                              // 行 div 不可聚焦，点击后焦点掉 body → Escape 会关整个弹窗；
                              // 还焦给选择器输入框，Escape 只关下拉（同原生 rc-select 行为）
                              document.getElementById('ai-scopes')?.focus();
                            }}>
                            <span style={{ width: 16, height: 16, flexShrink: 0, border: '1px solid',
                              borderColor: on ? '#1677ff' : 'var(--gw-border)', borderRadius: 2,
                              background: on ? '#1677ff' : 'var(--gw-surface)', color: '#fff', fontSize: 'var(--gw-fs-12)',
                              lineHeight: '16px', textAlign: 'center' }}>{on ? '✓' : ''}</span>
                            <span>{o.label}</span>
                          </div>
                        </Tooltip>
                      );
                    })}
                    </div>
                  </div>
                )}
                onChange={(v) => setAiScopes(Array.isArray(v) ? v : [])}
                maxTagCount={2} />
              <Input id="ai-desc" style={{ width: 320 }} placeholder="用大白话描述需求，AI 生成字段+方式+匹配项，如：拦截含 AWS 密钥的提示词；再点一次=追加新词（避开已用词，已有不删）"
                onChange={(e) => form.setFieldValue('ai_description', e.target.value)} />
              <Button icon={<RobotOutlined />} loading={aiLoading}
                onClick={() => aiGenerate()}>AI 生成匹配条件</Button>
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
          <Form.Item name="description" label="说明"><Input /></Form.Item>
        </Form>
      </Modal>
    </Space>
  );
}

