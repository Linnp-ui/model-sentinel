import React, { useEffect, useState, useCallback, useRef } from 'react';
import { Table, Button, Input, Modal, Form, Select, Tag, Space, message, Popconfirm, Popover, Typography, Alert, Card, List, InputNumber, Spin, Tabs } from 'antd';
import { StopOutlined, ThunderboltOutlined, SlidersOutlined, QuestionCircleOutlined } from '@ant-design/icons';
import { api, errText, RANGES, usePoll } from '../api.js';
import { wideSticky } from '../utils.js';
import { Tip } from '../terms.jsx';

const { Text } = Typography;
// ---------------- 调节策略建议（总览 + KEY 黑白名单页共用；onApplied 供宿主刷新自己的表格） ----------------
const sevColor = { high: 'red', warn: 'orange', info: 'blue' };
// 一键应用的按钮文案/确认提示/颜色按 kind 走（后端 6 种：black/white/unwhite/reorder/remove_rule/add_limit）。
// 旧三元式只认 white/unwhite，reorder/remove_rule/add_limit 全部误标「一键拉黑」且确认框拼出空名。
// 红色只给破坏性操作（拉黑=403、删规则）；加白/撤销白名单/重排/加限流方向偏安全，不红
const APPLY_UI = {
  black: { label: '一键拉黑', danger: true, confirm: (s) => `确认拉黑 ${s.key_name || s.ip || ''}？` },
  white: { label: '一键加白', danger: false, confirm: (s) => `确认将 ${s.key_name || ''} 加入白名单（跳 L2，保留 L1+抽样）？` },
  unwhite: { label: '撤销白名单', danger: false, confirm: () => '确认撤销该白名单规则？（仅「策略建议」来源可撤销，手工规则请到列表移除）' },
  reorder: { label: '一键重排优先级', danger: false, confirm: () => '确认按触发次数重排 L1 规则优先级（一次排到底）？' },
  remove_rule: { label: '一键删除规则', danger: true, confirm: (s) => `确认删除规则 ${s.apply.rule_name || ''}？（policy.yaml 已入库可回滚）` },
  add_limit: { label: '一键加限流', danger: false, confirm: (s) => `确认给 ${s.apply.limit_model || ''} 加全局限流 ${s.apply.limit_rpm}/分钟？` },
};
const applyUiOf = (s) => APPLY_UI[s.apply.kind] || { label: '一键应用', confirm: () => '确认应用该建议？' };
// 建议类型 → 中文（后端 type 是内部英文标识，界面上不直接露英文）
const SUGGEST_TYPE_ZH = {
  key_blacklist: '异常处置拉黑',
  key_rate_black: '异常率超阈拉黑',
  key_white_revoke: '建议撤销白名单',
  key_whitelist: '建议加入白名单',
  key_white_idle: '闲置白名单',
  model_error_rate: '模型错误率偏高',
  model_rate_limit: '模型缺限流',
  rule_priority: '规则优先级',
  silent_rule: '沉默规则',
  volume_watch: '单 IP 调用量偏高',
  latency_high: '延迟偏高',
  degrade_rate: '本地路由占比偏高',
  token_spike: 'token 用量突增',
  provider_breaker: 'Provider 熔断',
};
// 调节建议阈值（挂在建议卡上的弹窗：读写全部建议引擎阈值，/l2-config 合并式更新不碰 L2 项）
// **次数类阈值只配一个基准值（24 小时窗口口径）**，7d/30d 的值由后端按窗口长度等比放大
// （见 stats_service._scaled）；比率/速率类（错误率、异常处置率、峰值分钟、限流累计）不放大。
// 建议类型 → 阈值所在分组（卡片条目上的「阈值」按钮据此跳到对应分组）
// 分组一律按「规则」命名（不再按窗口命名）：面板侧三条规则各自成组，
// 点「阈值」能直接落到那条规则的页签，不会跳到一个混装三类的窗口页签。
const TYPE_TAB = {
  key_blacklist: 'violation', volume_watch: 'volume', model_error_rate: 'error',
  key_rate_black: 'tier', key_whitelist: 'tier', key_white_revoke: 'tier',
  rule_priority: 'rules', silent_rule: 'rules', key_white_idle: 'rules',
  model_rate_limit: 'limit',
  latency_high: 'runtime', degrade_rate: 'runtime', token_spike: 'runtime', provider_breaker: 'runtime',
};
function SuggestThresholdsModal({ open, onClose, onSaved, focusType }) {
  const [cfg, setCfg] = useState(null);
  const [saving, setSaving] = useState(false);
  const [tab, setTab] = useState('violation');
  useEffect(() => {
    if (!open) return;
    setTab(TYPE_TAB[focusType] || 'violation');
    api('/l2-config')
      .then((d) => setCfg({
        violation: d.AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD,
        volume: d.AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD,
        error: d.AI_GATEWAY_SUGGEST_ERROR_RATE,
        error_min: d.AI_GATEWAY_SUGGEST_ERROR_MIN_CALLS,
        kt_black_calls: d.AI_GATEWAY_KEYTIER_MIN_CALLS_BLACK,
        kt_black_viol: d.AI_GATEWAY_KEYTIER_MIN_VIOLATIONS,
        kt_black_rate: d.AI_GATEWAY_KEYTIER_VIOLATION_RATE,
        kt_white_calls: d.AI_GATEWAY_KEYTIER_MIN_CALLS_WHITE,
        kt_revoke: d.AI_GATEWAY_KEYTIER_REVOKE_VIOLATIONS,
        kt_window_days: d.AI_GATEWAY_KEYTIER_WINDOW_HOURS / 24,
        reorder: d.AI_GATEWAY_SUGGEST_REORDER_MIN_HITS,
        limit_calls: d.AI_GATEWAY_SUGGEST_LIMIT_MIN_CALLS,
        limit_peak: d.AI_GATEWAY_SUGGEST_LIMIT_MIN_PEAK,
        max_per_rule: d.AI_GATEWAY_SUGGEST_MAX_PER_RULE,
        max_total: d.AI_GATEWAY_SUGGEST_MAX_TOTAL,
        rule_window_days: (d.AI_GATEWAY_SUGGEST_RULE_WINDOW_HOURS || 0) / 24,
        latency_p95: d.AI_GATEWAY_SUGGEST_LATENCY_P95_MS,
        degrade_rate: d.AI_GATEWAY_SUGGEST_DEGRADE_RATE,
        token_growth: d.AI_GATEWAY_SUGGEST_TOKEN_GROWTH,
        grace_days: d.AI_GATEWAY_SUGGEST_SILENT_GRACE_DAYS,
      }))
      .catch((e) => message.error(errText(e)));
  }, [open, focusType]);
  const setK = (k, v) => setCfg((c) => ({ ...c, [k]: v }));
  const save = async () => {
    setSaving(true);
    try {
      await api('/l2-config', { method: 'PUT', body: JSON.stringify({
        AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD: cfg.violation,
        AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD: cfg.volume,
        AI_GATEWAY_SUGGEST_ERROR_RATE: cfg.error,
        AI_GATEWAY_SUGGEST_ERROR_MIN_CALLS: cfg.error_min,
        AI_GATEWAY_KEYTIER_MIN_CALLS_BLACK: cfg.kt_black_calls,
        AI_GATEWAY_KEYTIER_MIN_VIOLATIONS: cfg.kt_black_viol,
        AI_GATEWAY_KEYTIER_VIOLATION_RATE: cfg.kt_black_rate,
        AI_GATEWAY_KEYTIER_MIN_CALLS_WHITE: cfg.kt_white_calls,
        AI_GATEWAY_KEYTIER_REVOKE_VIOLATIONS: cfg.kt_revoke,
        // 清空输入框会得到 null ⇒ 回落默认 30 天（直接传 0 会被后端 gt=0 拒掉、整次保存失败）
        AI_GATEWAY_KEYTIER_WINDOW_HOURS: Math.round((cfg.kt_window_days || 30) * 24),
        AI_GATEWAY_SUGGEST_REORDER_MIN_HITS: cfg.reorder,
        AI_GATEWAY_SUGGEST_LIMIT_MIN_CALLS: cfg.limit_calls,
        AI_GATEWAY_SUGGEST_LIMIT_MIN_PEAK: cfg.limit_peak,
        AI_GATEWAY_SUGGEST_MAX_PER_RULE: cfg.max_per_rule,
        AI_GATEWAY_SUGGEST_MAX_TOTAL: cfg.max_total,
        AI_GATEWAY_SUGGEST_RULE_WINDOW_HOURS: Math.round((cfg.rule_window_days || 0) * 24),
        AI_GATEWAY_SUGGEST_LATENCY_P95_MS: cfg.latency_p95,
        AI_GATEWAY_SUGGEST_DEGRADE_RATE: cfg.degrade_rate,
        AI_GATEWAY_SUGGEST_TOKEN_GROWTH: cfg.token_growth,
        AI_GATEWAY_SUGGEST_SILENT_GRACE_DAYS: cfg.grace_days,
      }) });
      message.success('已保存并生效（重启后仍生效）');
      // 阈值即时生效（后端 PUT 直接改 os.environ）⇒ 立刻重算建议，别让用户等 30s 轮询
      onSaved && onSaved();
      onClose();
    } catch (e) { message.error(errText(e)); }
    setSaving(false);
  };
  // 阈值行：左边标题 + 它驱动的建议类型（与卡片上的条目同名，便于对号入座），右边输入框
  const rowShell = (label, tag, right) => (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 'var(--gw-sp-4)' }}>
      <Space size={6} style={{ minWidth: 0 }}>
        <Text style={{ whiteSpace: 'nowrap' }}>{label}：</Text>
        {tag && <Tag color="blue" style={{ marginInlineEnd: 0 }}>{tag}</Tag>}
      </Space>
      {right}
    </div>
  );
  // 输入区固定列宽（前缀 14 / 输入框 120 / 后缀 28）⇒ 所有行的输入框 x 坐标一致
  const ctl = (prefix, k, min, max, step, suffix) => (
    <Space size={4} style={{ flex: 'none' }}>
      <span style={{ width: 14, display: 'inline-block', textAlign: 'right' }}>{prefix}</span>
      <InputNumber size="small" min={min} max={max} step={step} value={cfg[k]}
        onChange={(v) => setK(k, v)} style={{ width: 120 }} />
      <span style={{ width: 28, display: 'inline-block' }}>{suffix}</span>
    </Space>
  );
  // 次数类行：一个输入框（24h 基准值）
  const baseRow = (label, k, min, max, step = 1, prefix = '', tag = '') => (
    rowShell(label, tag, ctl(prefix, k, min, max, step, ''))
  );
  const numRow = (label, k, min, max, step = 1, prefix = '', suffix = '', tag = '') => (
    rowShell(label, tag, ctl(prefix, k, min, max, step, suffix))
  );
  // 分组：按"哪条建议规则用这些阈值"归类（每组一个 Tab，用户按要改的那条建议找）
  // 一律按规则命名：面板侧三条规则（1/2/3）各成一组，其余按规则域分组
  const groups = [
    { key: 'violation', label: '异常拉黑', hint: '规则 1：异常处置拉黑（拦截∪本地路由）次数，生效于建议卡当前所选窗口；次数类填 24h 基准，其余窗口自动换算。' },
    { key: 'volume', label: 'IP 用量', hint: '规则 2：单 IP 调用量观察次数，生效于建议卡当前所选窗口；次数类填 24h 基准，其余窗口自动换算。' },
    { key: 'error', label: '错误率', hint: '规则 3：模型错误率告警（比率类，不随窗口放大）+ 最小调用数（次数类，填 24h 基准）。' },
    { key: 'tier', label: 'KEY 分级', hint: '规则 0 生效于 KEY 分级观察窗口（下面可改）；次数类按分级窗口换算。' },
    { key: 'rules', label: '规则维护', hint: '规则 4/5/6 生效于本组窗口（0 = 跟随 KEY 分级窗口）。' },
    { key: 'limit', label: '限流建议', hint: '规则 7：两个条件同时满足才建议加限流（0 = 该条件不参与判断）；与窗口长度无关，不换算。' },
    { key: 'runtime', label: '运行态 & 条数', hint: '规则 8~10 系统健康（0 = 关闭该规则）+ 规则 11 熔断（无阈值）+ 建议条数上限。' },
  ];
  const rowsOf = (key) => ({
    violation: [
      baseRow('异常处置拉黑 · 24h 次数（拦截∪本地路由）', 'violation', 1, 1000, 1, '≥', '异常处置拉黑'),
    ],
    volume: [
      baseRow('IP 用量观察 · 24h 调用次数', 'volume', 1, 100000, 1, '≥', '单 IP 调用量偏高'),
    ],
    error: [
      numRow('错误率告警（调用数需达最小值，见下行）', 'error', 0, 1, 0.01, '≥', '', '模型错误率偏高'),
      baseRow('错误率最小调用数 · 24h（低于此次数不告警，防小样本误报）', 'error_min', 1, 100000, 1, '≥', '', '模型错误率偏高'),
    ],
    tier: [
      baseRow('分级拉黑 · 调用数', 'kt_black_calls', 1, 1000000, 1, '≥', '异常率超阈拉黑'),
      baseRow('分级拉黑 · 异常处置次数', 'kt_black_viol', 0, 100000, 1, '≥', '异常率超阈拉黑'),
      numRow('分级拉黑 · 异常处置率（三项同时满足才拉黑，比率不随窗口变）', 'kt_black_rate', 0, 1, 0.05, '≥', '', '异常率超阈拉黑'),
      baseRow('加白建议 · 调用数（且零异常处置）', 'kt_white_calls', 1, 1000000, 1, '≥', '建议加入白名单'),
      baseRow('撤销白名单建议 · 异常处置次数', 'kt_revoke', 0, 100000, 1, '≥', '建议撤销白名单'),
      numRow('分级观察窗口', 'kt_window_days', 1, 365, 1, '', '天', '异常率超阈拉黑'),
    ],
    rules: [
      baseRow('优先级重排建议 · 最小触发次数（防抖动）', 'reorder', 1, 100000, 1, '≥', '规则优先级'),
      numRow('新规则豁免期（期内零触发不提醒删除）', 'grace_days', 0, 365, 1, '', '天', '沉默规则'),
      numRow('规则维护类建议窗口（0 = 跟随 KEY 分级窗口）', 'rule_window_days', 0, 365, 1, '', '天', '闲置白名单'),
    ],
    limit: [
      numRow('加限流建议 · 最小调用次数（与峰值条件同时满足才建议，0 = 不限该条件）', 'limit_calls', 0, 10000000, 1, '≥', '', '模型缺限流'),
      numRow('加限流建议 · 峰值分钟调用数（与最小调用次数同时满足才建议，0 = 不限）', 'limit_peak', 0, 100000, 1, '≥', '', '模型缺限流'),
    ],
    runtime: [
      numRow('延迟 P95 告警（端到端毫秒）', 'latency_p95', 0, 600000, 100, '≥', '', '延迟偏高'),
      numRow('本地路由占比告警（机密内容降级占比）', 'degrade_rate', 0, 1, 0.05, '≥', '', '本地路由占比偏高'),
      numRow('token 用量突增告警（相对上一等长窗口的倍数）', 'token_growth', 0, 100, 0.5, '≥', '', 'token 用量突增'),
      numRow('每条规则最多报几条', 'max_per_rule', 1, 100, 1, '≤', '', '条数上限'),
      numRow('整张卡最多几条（severity 高的先留）', 'max_total', 1, 200, 1, '≤', '', '条数上限'),
    ],
  }[key]);
  return (
    <Modal title={<Space size={10}>调节建议阈值
      {/* 口径说明不常驻：挂在「口径说明」文字+问号上，悬停才看 */}
      <Popover placement="bottomLeft" title="阈值口径说明" content={
        <div style={{ maxWidth: 400, fontSize: 'var(--gw-fs-12)', lineHeight: 1.7 }}>
          <div>次数类阈值只填一个基准值（24 小时窗口口径），系统按<b>该规则实际生效的窗口</b>自动换算
            ——窗口越长累计越多，阈值等比放大。</div>
          <div style={{ marginTop: 'var(--gw-sp-6)' }}>比率/速率类（错误率、异常处置率、限流的累计调用数与峰值分钟）
            与窗口长度无关，不放大。</div>
          <div style={{ marginTop: 'var(--gw-sp-6)' }}>两个拉黑口径是两条独立规则，不是同一件事：窗口型「异常处置拉黑」
            只看面板窗口内的异常处置次数（抓突发）；「分级拉黑」看分级观察窗口（默认 30 天）的调用数/次数/率三项同时满足
            （抓长期高异常率）。同一个 KEY 只出一条拉黑建议（分级优先，窗口型让位）。</div>
        </div>
      }>
        <span style={{ cursor: 'help', fontSize: 'var(--gw-fs-12)', color: 'rgba(0,0,0,0.45)', whiteSpace: 'nowrap' }}>
          口径说明 <QuestionCircleOutlined />
        </span>
      </Popover>
    </Space>} open={open} onCancel={onClose} width={800} destroyOnClose
      okText="保存生效" cancelText="取消" confirmLoading={saving} okButtonProps={{ disabled: !cfg }}
      onOk={save}>
      {cfg ? <Space direction="vertical" size={8} style={{ width: '100%' }}>
        <Tabs activeKey={tab} onChange={setTab} size="small" items={groups.map((g) => ({
          key: g.key, label: g.label,
          children: (
            <Space direction="vertical" size={10} style={{ width: '100%' }}>
              <Text type="secondary" style={{ fontSize: 'var(--gw-fs-12)' }}>{g.hint}</Text>
              {rowsOf(g.key)}
            </Space>
          ),
        }))} />
      </Space> : <Card size="small" loading />}
    </Modal>
  );
}
export function SuggestionCard({ onApplied }) {
  const [hours, setHours] = useState(168);
  const [loading, setLoading] = useState(false);
  const [sug, setSug] = useState([]);
  const [sugErr, setSugErr] = useState('');
  const [sugMeta, setSugMeta] = useState(null);
  const [thOpen, setThOpen] = useState(false);
  const [thFocus, setThFocus] = useState(null);
  const hoursRef = useRef(hours);
  hoursRef.current = hours;
  const seqRef = useRef(0);
  // silent=true：30s 轮询用（不接遮罩不闪 Spin、后台失败不弹错）
  const loadSug = useCallback(async (h = hours, silent = false) => {
    // 只有最新一次 load 有权摘遮罩：在飞的旧窗请求后落地不能拆掉新接的遮罩
    const seq = ++seqRef.current;
    if (!silent) setLoading(true);
    try {
      const s = await api(`/suggestions?hours=${h}`);
      if (h !== hoursRef.current) return; // 切窗后旧窗请求落地 → 丢弃
      setSug(s.items || []); setSugMeta(s.meta || null); setSugErr('');
    } catch (e) {
      if (h === hoursRef.current) setSugErr(errText(e));
      if (!silent && h === hoursRef.current) message.error(errText(e));
    }
    // 摘遮罩双守卫：seq 防同窗双击刷新互拆；hoursRef 防切窗后、新 load 启动前
    // 旧窗请求先落地把刚接上的遮罩拆掉
    finally { if (!silent && seq === seqRef.current && h === hoursRef.current) setLoading(false); }
  }, [hours]);
  useEffect(() => { loadSug(); }, [loadSug]);
  usePoll(() => loadSug(hoursRef.current, true)); // 替代原刷新按钮（当前窗）
  const applySuggestion = async (s) => {
    try {
      await api('/suggestions/apply', { method: 'POST', body: JSON.stringify(s.apply || {}) });
      message.success('已应用'); loadSug(); onApplied && onApplied();
    } catch (e) { message.error(errText(e)); }
  };
  return (
    <>
    <Card title={<>调节策略建议<Tip term="suggest" /></>}
      extra={<Space>
        <Select value={hours} onChange={setHours} style={{ width: 130 }}
          options={RANGES.map((r) => ({ value: r.hours, label: r.label }))} />
        <Button icon={<SlidersOutlined />}
          onClick={() => { setThFocus(null); setThOpen(true); }}>阈值</Button>
      </Space>}>
      <Spin spinning={loading}>
      {sug.length === 0
        ? (sugErr
          ? <Alert type="warning" showIcon
              message={`建议加载失败：${sugErr}（30s 轮询会自动重拉）`} />
          : <Alert type="success" showIcon
            message="当前无建议：已启用规则均未触发"
            description={sugMeta
              ? <Space direction="vertical" size={4}>
                  <span>{`触发口径 ${sugMeta.trigger}；KEY 分级窗口 ${Math.round(sugMeta.key_tier_window_hours / 24)} 天，规则 1/2/3 窗口 ${sugMeta.window_hours} 小时。`}</span>
                  <Button size="small" icon={<SlidersOutlined />}
                    onClick={() => { setThFocus(null); setThOpen(true); }}>查看/调整阈值</Button>
                </Space>
              : null} />)
        : <List
            size="small"
            dataSource={sug}
            renderItem={(s) => (
              <List.Item
                actions={[
                  ...(s.apply ? [
                    <Popconfirm key="ok"
                      title={applyUiOf(s).confirm(s)}
                      onConfirm={() => applySuggestion(s)}>
                      <Button type="link" danger={applyUiOf(s).danger}
                        icon={<ThunderboltOutlined />}>
                        {applyUiOf(s).label}
                      </Button>
                    </Popconfirm>,
                  ] : []),
                  // 一键跳到这条建议对应的阈值分组（不用自己翻面板找）
                  <Button key="th" type="link" size="small" icon={<SlidersOutlined />}
                    onClick={() => { setThFocus(s.type); setThOpen(true); }}>阈值</Button>,
                ]}
              >
                {/* minWidth:0 + overflowWrap:anywhere：长文案不许把右侧操作按钮挤出卡片 */}
                <Space style={{ minWidth: 0 }} align="flex-start">
                  <Tag color={sevColor[s.severity] || 'default'}>{SUGGEST_TYPE_ZH[s.type] || s.type}</Tag>
                  <span style={{ overflowWrap: 'anywhere' }}>{s.message}</span>
                </Space>
              </List.Item>
            )}
          />}
      </Spin>
    </Card>
    <SuggestThresholdsModal open={thOpen} focusType={thFocus}
      onClose={() => setThOpen(false)} onSaved={() => loadSug()} />
    </>
  );
}
// ---------------- 2 KEY 黑白名单 ----------------
export default function KeyRulesPage() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();
  const [keyOpts, setKeyOpts] = useState([]);
  // key_masked -> 登记名（含停用项：规则指向的 KEY 后来被停用/删除时仍能显示名字）
  const [nameByMask, setNameByMask] = useState({});

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try { setItems((await api('/keyrules')).items); } catch (e) { if (!silent) message.error(errText(e)); }
    finally { if (!silent) setLoading(false); }
  }, []);

  // 登记 KEY 名单（下拉用，只含 name + 掩码，明文不出后端）
  const loadKeys = useCallback(async () => {
    try {
      const k = await api('/keys');
      const items = k.items || [];
      setKeyOpts(items.filter((x) => !x.disabled)
        .map((x) => ({ value: x.name, label: `${x.name}（${x.key_masked || ''}）` })));
      setNameByMask(Object.fromEntries(items.map((x) => [x.key_masked, x.name])));
    } catch (e) { message.error(errText(e)); }
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadKeys(); }, [loadKeys]);
  usePoll(() => load(true)); // 替代原刷新按钮

  const submit = async () => {
    try {
      const v = await form.validateFields();
      await api('/keyrules', { method: 'POST', body: JSON.stringify(v) });
      message.success('已添加');
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (e) { if (e.errorFields) return; message.error(errText(e)); }
  };

  const del = async (id) => {
    try { await api(`/keyrules/${id}`, { method: 'DELETE' }); load(); }
    catch (e) { message.error(errText(e)); }
  };

  /* 列宽全 % + tableLayout=fixed：跟随容器、按比例分配；备注/操作原先无宽（auto
     布局弹性列），现在固定占比 */
  const cols = [
    { title: <>{'类型'}<Tip text="黑名单 = 命中即不服务（403）；白名单 = 可信，少走甚至跳过 L2 审查。" /></>, dataIndex: 'kind', width: '10%',
      render: (v) => (v === 'black' ? <Tag color="black">黑名单</Tag> : <Tag color="green">白名单</Tag>) },
    { title: '名称', dataIndex: 'key_masked', width: '14%',
      render: (m) => (nameByMask[m] || <Text type="secondary">—</Text>) },
    { title: 'KEY', dataIndex: 'key_masked', width: '18%', render: (v) => <Text code>{v}</Text> },
    { title: '来源', dataIndex: 'source', width: '10%',
      render: (v) => (v === 'manual' ? <Tag>手动添加</Tag> : <Tag color="orange">策略建议</Tag>) },
    { title: '备注', dataIndex: 'note', width: '18%', ellipsis: true, align: 'center', className: 'cell-center' },
    { title: '添加时间', dataIndex: 'created_at', width: '18%' },
    { title: '操作', width: '12%',
      render: (_, rec) => (
        <Popconfirm title="确认移除？" onConfirm={() => del(rec.id)}>
          <Button type="link" danger>移除</Button>
        </Popconfirm>
      ) },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Alert type="info" showIcon
        message="默认放行所有 KEY；黑名单命中即拦截（403），且黑名单优先于白名单（同 KEY 双规则时拉黑即时生效）。按 Bearer key 原文精确匹配。" />
      <Card title={<>KEY 规则<Tip text="按 KEY（按登记名单里的 key 原文精确匹配）设的黑名单/白名单。" /></>} extra={<Button type="primary" icon={<StopOutlined />} onClick={() => setModalOpen(true)}>添加规则</Button>}>
      <Table rowKey="id" columns={cols} dataSource={items} loading={loading} size="small" sticky={wideSticky()} tableLayout="fixed"
        pagination={{ pageSize: 50, showSizeChanger: false }} />
      </Card>
      <Modal title="添加 KEY 规则" open={modalOpen} onOk={submit} onCancel={() => setModalOpen(false)}>
        <Form form={form} layout="vertical" initialValues={{ kind: 'black' }}>
          <Form.Item name="kind" label="类型" rules={[{ required: true }]}>
            <Select options={[
              { value: 'black', label: '黑名单（拦截，优先于白名单）' },
              { value: 'white', label: '白名单（放行）' },
            ]} />
          </Form.Item>
          <Form.Item name="key_name" label="KEY（登记名单选择）" rules={[{ required: true, message: '请选择登记 KEY' }]}>
            <Select showSearch optionFilterProp="label" placeholder="选择人员/KEY" options={keyOpts} />
          </Form.Item>
          <Form.Item name="note" label="备注"><Input /></Form.Item>
        </Form>
      </Modal>
    </Space>
  );
}

