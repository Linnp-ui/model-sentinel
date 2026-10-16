import React, { useEffect, useState, useCallback, useRef } from 'react';
import { Table, Button, Input, Modal, Form, Select, Tag, Space, message, Popconfirm, Typography, Alert, Card, List, InputNumber, Spin } from 'antd';
import { StopOutlined, ThunderboltOutlined, SlidersOutlined } from '@ant-design/icons';
import { api, errText, RANGES, usePoll } from '../api.js';

const { Text } = Typography;
// 按窗口阈值 map（{'*': v} 或 {24:..,168:..,720:..}）取 24h 代表值：'*' 优先，否则 24，否则第一个
const pick24 = (m) => (m && (m['*'] ?? m[24] ?? Object.values(m)[0])) || '-';
// ---------------- 调节策略建议（总览 + KEY 黑白名单页共用；onApplied 供宿主刷新自己的表格） ----------------
const sugTitleOf = (meta, h) => {
  return '调节策略建议';
};
const sevColor = { high: 'red', warn: 'orange', info: 'blue' };
// 一键应用的按钮文案/确认提示/颜色按 kind 走（后端 6 种：black/white/unwhite/reorder/remove_rule/add_limit）。
// 旧三元式只认 white/unwhite，reorder/remove_rule/add_limit 全部误标「一键拉黑」且确认框拼出空名。
// 红色只给破坏性操作（拉黑=403、删规则）；加白/撤销白名单/重排/加限流方向偏安全，不红
const APPLY_UI = {
  black: { label: '一键拉黑', danger: true, confirm: (s) => `确认拉黑 ${s.key_name || s.ip || ''}？` },
  white: { label: '一键加白', danger: false, confirm: (s) => `确认将 ${s.key_name || ''} 加入白名单（跳 L2，保留 L1+抽样）？` },
  unwhite: { label: '撤销白名单', danger: false, confirm: () => '确认撤销该白名单规则？' },
  reorder: { label: '一键重排优先级', danger: false, confirm: () => '确认按触发次数重排 L1 规则优先级（一次排到底）？' },
  remove_rule: { label: '一键删除规则', danger: true, confirm: (s) => `确认删除规则 ${s.apply.rule_name || ''}？（policy.yaml 已入库可回滚）` },
  add_limit: { label: '一键加限流', danger: false, confirm: (s) => `确认给 ${s.apply.limit_model || ''} 加全局限流 ${s.apply.limit_rpm}/分钟？` },
};
const applyUiOf = (s) => APPLY_UI[s.apply.kind] || { label: '一键应用', confirm: () => '确认应用该建议？' };
// 调节建议阈值（挂在建议卡上的弹窗：读写全部 13 个建议引擎阈值，/l2-config 合并式更新不碰 L2 项）
// 拉黑/用量两项按窗口各配一个阈值（窗口越长累计越多，单值在 30 天窗必误报）；错误率是比率、
// 其余各项是长窗口（30 天）口径，都不随建议卡所选窗口变。
const WIN_SHORT = { 24: '24h', 168: '7d', 720: '30d' };
function SuggestThresholdsModal({ open, onClose }) {
  const [cfg, setCfg] = useState(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!open) return;
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
        grace_days: d.AI_GATEWAY_SUGGEST_SILENT_GRACE_DAYS,
      }))
      .catch((e) => message.error(errText(e)));
  }, [open]);
  const setWin = (which, h, v) => setCfg((c) => ({ ...c, [which]: { ...c[which], [h]: v } }));
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
        AI_GATEWAY_KEYTIER_WINDOW_HOURS: Math.round(cfg.kt_window_days * 24),
        AI_GATEWAY_SUGGEST_REORDER_MIN_HITS: cfg.reorder,
        AI_GATEWAY_SUGGEST_LIMIT_MIN_CALLS: cfg.limit_calls,
        AI_GATEWAY_SUGGEST_SILENT_GRACE_DAYS: cfg.grace_days,
      }) });
      message.success('已保存并生效（重启后仍生效）');
    } catch (e) { message.error(errText(e)); }
    setSaving(false);
  };
  // 所有行尾部恒定 14px 后缀位（天/空），保证每行输入框右缘对齐同一 x
  const row = (label, which, max, prefix = '') => (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 4 }}>
      <Text style={{ whiteSpace: 'nowrap' }}>{label}：</Text>
      <Space size={4}>
        {prefix && <Text type="secondary">{prefix}</Text>}
        {RANGES.map((r) => (
          <React.Fragment key={r.hours}>
            <Text type="secondary">{WIN_SHORT[r.hours]}</Text>
            <InputNumber size="small" min={1} max={max} value={cfg[which][r.hours]}
              onChange={(v) => setWin(which, r.hours, v)} />
          </React.Fragment>
        ))}
        <span style={{ display: 'inline-block', width: 14 }} />
      </Space>
    </div>
  );
  const numRow = (label, k, min, max, step = 1, prefix = '', suffix = '') => (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 4 }}>
      <Text style={{ whiteSpace: 'nowrap' }}>{label}：</Text>
      <Space size={4}>
        {prefix && <Text type="secondary">{prefix}</Text>}
        <InputNumber size="small" min={min} max={max} step={step} value={cfg[k]}
          onChange={(v) => setK(k, v)} />
        <Text type="secondary" style={{ width: 14, display: 'inline-block' }}>{suffix}</Text>
      </Space>
    </div>
  );
  const sec = (t) => <Text strong style={{ fontSize: 16 }}>{t}</Text>;
  return (
    // 800=容纳窗口型行（长 label+3 个输入框单行排开，不换行才谈得上对齐）
    <Modal title="调节建议阈值" open={open} onCancel={onClose} width={800} destroyOnClose
      okText="保存生效" cancelText="取消" confirmLoading={saving} okButtonProps={{ disabled: !cfg }}
      onOk={save}>
      {cfg ? <Space direction="vertical" size={10} style={{ width: '100%' }}>
        {sec('窗口型（按建议卡所选窗口分别生效，窗口越长应越大）')}
        {row('KEY 拉黑建议（窗口内异常 拦截∪本地路由 次数）', 'violation', 1000, '≥')}
        {row('IP 用量观察（窗口内调用次数）', 'volume', 100000, '≥')}
        {sec('模型错误率')}
        {numRow(`错误率告警（且调用 ≥ ${cfg.error_min ?? '-'}）`, 'error', 0, 1, 0.01, '≥')}
        {numRow('错误率最小调用数（低于此次数不告警，防小样本误报）', 'error_min', 1, 100000)}
        {sec('KEY 分级（长窗口口径）')}
        {numRow('拉黑建议 · 调用数', 'kt_black_calls', 1, 1000000, 1, '≥')}
        {numRow('拉黑建议 · 异常处置次数', 'kt_black_viol', 0, 100000, 1, '≥')}
        {numRow('拉黑建议 · 异常处置率', 'kt_black_rate', 0, 1, 0.05, '≥')}
        {numRow('加白建议 · 调用数（且零异常处置）', 'kt_white_calls', 1, 1000000, 1, '≥')}
        {numRow('撤销白名单建议 · 异常处置次数', 'kt_revoke', 0, 100000, 1, '≥')}
        {numRow('分级观察窗口', 'kt_window_days', 1, 365, 1, '', '天')}
        {sec('规则 / 限流建议')}
        {numRow('优先级重排建议 · 最小触发次数（防抖动）', 'reorder', 1, 100000, 1, '≥')}
        {numRow('加限流建议 · 最小调用次数', 'limit_calls', 1, 10000000, 1, '≥')}
        {numRow('新规则豁免期（期内零触发不提醒删除）', 'grace_days', 0, 365, 1, '', '天')}
        <Text type="secondary" style={{ fontSize: 12 }}>
          窗口型两项随建议卡所选窗口生效；其余各项是固定长窗口（30 天）口径，不随卡上窗口变。
          当前 24h：拉黑 ≥{pick24(cfg.violation)} / 用量 ≥{pick24(cfg.volume)} / 错误率 ≥{cfg.error}（与上方输入框同源，非写死）
        </Text>
      </Space> : <Card size="small" loading />}
    </Modal>
  );
}
export function SuggestionCard({ onApplied }) {
  const [hours, setHours] = useState(168);
  const [loading, setLoading] = useState(false);
  const [sug, setSug] = useState([]);
  const [sugMeta, setSugMeta] = useState(null);
  const [thOpen, setThOpen] = useState(false);
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
      setSug(s.items || []); setSugMeta(s.meta || null);
    } catch (e) { if (!silent && h === hoursRef.current) message.error(errText(e)); }
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
    <Card title={sugTitleOf(sugMeta, hours)}
      extra={<Space>
        <Select value={hours} onChange={setHours} style={{ width: 130 }}
          options={RANGES.map((r) => ({ value: r.hours, label: r.label }))} />
        <Button className="btn-neutral" icon={<SlidersOutlined />} onClick={() => setThOpen(true)}>阈值</Button>
      </Space>}>
      <Spin spinning={loading}>
      {sug.length === 0
        ? <Alert type="success" showIcon
            message="当前无建议：各指标均在阈值内"
            description={sugMeta
              ? <Space direction="vertical" size={4}>
                  <span>{`触发口径 ${sugMeta.trigger}；KEY 分级窗口 ${Math.round(sugMeta.key_tier_window_hours / 24)} 天，规则 1/2/3 窗口 ${sugMeta.window_hours} 小时。`}</span>
                  <Button className="btn-neutral" size="small" icon={<SlidersOutlined />} onClick={() => setThOpen(true)}>查看/调整阈值</Button>
                </Space>
              : null} />
        : <List
            size="small"
            dataSource={sug}
            renderItem={(s) => (
              <List.Item
                actions={s.apply ? [
                  <Popconfirm key="ok"
                    title={applyUiOf(s).confirm(s)}
                    onConfirm={() => applySuggestion(s)}>
                    <Button type={applyUiOf(s).danger ? 'link' : 'default'} danger={applyUiOf(s).danger}
                      icon={<ThunderboltOutlined />}>
                      {applyUiOf(s).label}
                    </Button>
                  </Popconfirm>,
                ] : []}
              >
                <Space>
                  <Tag color={sevColor[s.severity] || 'default'}>{s.type}</Tag>
                  <span>{s.message}</span>
                </Space>
              </List.Item>
            )}
          />}
      </Spin>
    </Card>
    <SuggestThresholdsModal open={thOpen} onClose={() => setThOpen(false)} />
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

  const del = async (id) => { await api(`/keyrules/${id}`, { method: 'DELETE' }); load(); };

  /* 列宽全 % + tableLayout=fixed：跟随容器、按比例分配；备注/操作原先无宽（auto
     布局弹性列），现在固定占比 */
  const cols = [
    { title: '类型', dataIndex: 'kind', width: '10%',
      render: (v) => (v === 'black' ? <Tag color="black">黑名单</Tag> : <Tag color="green">白名单</Tag>) },
    { title: '名称', dataIndex: 'key_masked', width: '14%',
      render: (m) => (nameByMask[m] || <Text type="secondary">—</Text>) },
    { title: 'KEY', dataIndex: 'key_masked', width: '18%', render: (v) => <Text code>{v}</Text> },
    { title: '来源', dataIndex: 'source', width: '10%',
      render: (v) => (v === 'manual' ? <Tag>手动添加</Tag> : <Tag color="orange">策略建议</Tag>) },
    { title: '备注', dataIndex: 'note', width: '18%', ellipsis: true },
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
      <Space>
        <Button type="primary" icon={<StopOutlined />} onClick={() => setModalOpen(true)}>添加规则</Button>
      </Space>
      <Card title="KEY 规则">
      <Table rowKey="id" columns={cols} dataSource={items} loading={loading} size="small" tableLayout="fixed"
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

