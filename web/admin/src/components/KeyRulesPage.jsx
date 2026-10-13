import React, { useEffect, useState, useCallback, useRef } from 'react';
import { Table, Button, Input, Modal, Form, Select, Tag, Space, message, Popconfirm, Typography, Alert, Card, List, InputNumber, Spin } from 'antd';
import { StopOutlined, ReloadOutlined, ThunderboltOutlined, SlidersOutlined } from '@ant-design/icons';
import { api, errText, RANGES } from '../api.js';

const { Text } = Typography;
// 按窗口阈值 map（{'*': v} 或 {24:..,168:..,720:..}）取 24h 代表值：'*' 优先，否则 24，否则第一个
const pick24 = (m) => (m && (m['*'] ?? m[24] ?? Object.values(m)[0])) || '-';
// ---------------- 调节策略建议（总览 + KEY 黑白名单页共用；onApplied 供宿主刷新自己的表格） ----------------
const RANGE_LABEL = (h) => (RANGES.find((r) => r.hours === h) || {}).label || `最近 ${h} 小时`;
const sugTitleOf = (meta, h) => {
  const base = '调节策略建议 · 触发 = 拦截 ∪ 本地路由';
  const kt = meta && meta.key_tier_window_hours;
  return (meta && meta.has_dual_window && kt)
    ? `${base}（明细 ${RANGE_LABEL(h)}；KEY 分级 ${Math.round(kt / 24)} 天）`
    : `${base}（${RANGE_LABEL(h)}）`;
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
// 调节建议阈值（挂在建议卡上的弹窗：只读写 3 个 SUGGEST_* 键，/l2-config 合并式更新不碰 L2 项）
// 拉黑/用量两项按窗口各配一个阈值（窗口越长累计越多，单值在 30 天窗必误报）；错误率是比率，不随窗口变。
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
      }))
      .catch((e) => message.error(errText(e)));
  }, [open]);
  const setWin = (which, h, v) => setCfg((c) => ({ ...c, [which]: { ...c[which], [h]: v } }));
  const save = async () => {
    setSaving(true);
    try {
      await api('/l2-config', { method: 'PUT', body: JSON.stringify({
        AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD: cfg.violation,
        AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD: cfg.volume,
        AI_GATEWAY_SUGGEST_ERROR_RATE: cfg.error,
      }) });
      message.success('已保存并生效（重启后仍生效）');
    } catch (e) { message.error(errText(e)); }
    setSaving(false);
  };
  const row = (label, which, max) => (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 4 }}>
      <Text style={{ whiteSpace: 'nowrap' }}>{label}：</Text>
      <Space size={4}>
        {RANGES.map((r) => (
          <React.Fragment key={r.hours}>
            <Text type="secondary">{WIN_SHORT[r.hours]}</Text>
            <InputNumber size="small" min={1} max={max} value={cfg[which][r.hours]}
              onChange={(v) => setWin(which, r.hours, v)} />
          </React.Fragment>
        ))}
      </Space>
    </div>
  );
  return (
    <Modal title="调节建议阈值" open={open} onCancel={onClose} width={560} destroyOnClose
      okText="保存生效" cancelText="取消" confirmLoading={saving} okButtonProps={{ disabled: !cfg }}
      onOk={save}>
      {cfg ? <Space direction="vertical" size={10} style={{ width: '100%' }}>
        {row('KEY 拉黑建议（窗口内异常 拦截∪本地路由 次数 ≥）', 'violation', 1000)}
        {row('IP 用量观察（窗口内调用次数 ≥）', 'volume', 100000)}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 4 }}>
          <Text style={{ whiteSpace: 'nowrap' }}>模型错误率告警（错误率 ≥ 且调用 ≥ 20）：</Text>
          <InputNumber size="small" min={0} max={1} step={0.01} value={cfg.error}
            onChange={(v) => setCfg((c) => ({ ...c, error: v }))} />
        </div>
        <Text type="secondary" style={{ fontSize: 12 }}>
          阈值按建议卡所选窗口分别生效（窗口越长应越大）；错误率与窗口无关。当前 24h：拉黑 ≥{pick24(cfg.violation)} / 用量 ≥{pick24(cfg.volume)} / 错误率 ≥{cfg.error}（与上方输入框同源，非写死）
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
  const loadSug = useCallback(async (h = hours) => {
    // 只有最新一次 load 有权摘遮罩：在飞的旧窗请求后落地不能拆掉新接的遮罩
    const seq = ++seqRef.current;
    setLoading(true);
    try {
      const s = await api(`/suggestions?hours=${h}`);
      if (h !== hoursRef.current) return; // 切窗后旧窗请求落地 → 丢弃
      setSug(s.items || []); setSugMeta(s.meta || null);
    } catch (e) { if (h === hoursRef.current) message.error(errText(e)); }
    // 摘遮罩双守卫：seq 防同窗双击刷新互拆；hoursRef 防切窗后、新 load 启动前
    // 旧窗请求先落地把刚接上的遮罩拆掉
    finally { if (seq === seqRef.current && h === hoursRef.current) setLoading(false); }
  }, [hours]);
  useEffect(() => { loadSug(); }, [loadSug]);
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
        <Button icon={<SlidersOutlined />} onClick={() => setThOpen(true)}>阈值</Button>
        <Button icon={<ReloadOutlined />} onClick={() => loadSug()}>刷新</Button>
      </Space>}>
      <Spin spinning={loading}>
      {sug.length === 0
        ? <Alert type="success" showIcon
            message="当前无建议：各指标均在阈值内"
            description={sugMeta
              ? <Space direction="vertical" size={4}>
                  <span>{`触发口径 ${sugMeta.trigger}；KEY 分级窗口 ${Math.round(sugMeta.key_tier_window_hours / 24)} 天，规则 1/2/3 窗口 ${sugMeta.window_hours} 小时。`}</span>
                  <Button size="small" icon={<SlidersOutlined />} onClick={() => setThOpen(true)}>查看/调整阈值</Button>
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
                    <Button danger={applyUiOf(s).danger}
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

  const load = useCallback(async () => {
    setLoading(true);
    try { setItems((await api('/keyrules')).items); } catch (e) { message.error(errText(e)); }
    setLoading(false);
  }, []);

  // 登记 KEY 名单（下拉用，只含 name + 掩码，明文不出后端）
  const loadKeys = useCallback(async () => {
    try {
      const k = await api('/keys');
      setKeyOpts((k.items || []).filter((x) => !x.disabled)
        .map((x) => ({ value: x.name, label: `${x.name}（${x.key_masked || ''}）` })));
    } catch (e) { message.error(errText(e)); }
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadKeys(); }, [loadKeys]);

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

  /* 列宽用 % 自适应（灌数轮次表口径） */
  const cols = [
    { title: '类型', dataIndex: 'kind', width: '10%',
      render: (v) => (v === 'black' ? <Tag color="black">黑名单</Tag> : <Tag color="green">白名单</Tag>) },
    { title: 'KEY', dataIndex: 'key_masked', width: '18%', render: (v) => <Text code>{v}</Text> },
    { title: '来源', dataIndex: 'source', width: '10%',
      render: (v) => (v === 'auto' ? <Tag color="orange">策略建议</Tag> : v) },
    { title: '备注', dataIndex: 'note', ellipsis: true },
    { title: '添加时间', dataIndex: 'created_at', width: '16%' },
    { title: '操作',
      render: (_, rec) => (
        <Popconfirm title="确认移除？" onConfirm={() => del(rec.id)}>
          <Button danger>移除</Button>
        </Popconfirm>
      ) },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Alert type="info" showIcon
        message="默认放行所有 KEY；黑名单命中即拦截（403），且黑名单优先于白名单（同 KEY 双规则时拉黑即时生效）。按 Bearer key 原文精确匹配。" />
      <Space>
        <Button type="primary" icon={<StopOutlined />} onClick={() => setModalOpen(true)}>添加规则</Button>
        <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
      </Space>
      <Card title="KEY 规则">
      <Table rowKey="id" columns={cols} dataSource={items} loading={loading} size="small" pagination={{ pageSize: 50, showSizeChanger: false }} />
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

