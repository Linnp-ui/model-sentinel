import React, { useEffect, useState, useCallback } from 'react';
import { AutoComplete, Input, InputNumber, Modal, Form, Select, message } from 'antd';
import { api, errText } from '../api.js';

// ---------------- 1.1/1.2/1.3 模型配置 ----------------
// 外部候选表单共享项：Provider 下拉 + 按 provider 拉模型列表供模型自动补全（手输仍可用）。
// 用于 ExternalCandidatesCard 新增/编辑弹窗、别名弹窗内"新增外部候选"。
export function ExternalCandidateFormItems({ form }) {
  const [providers, setProviders] = useState([]);
  const [modelOpts, setModelOpts] = useState({}); // provider -> [{value,label}]
  const [provKey, setProvKey] = useState('');
  const prov = Form.useWatch('provider', form);

  useEffect(() => {
    api('/providers').then((d) => setProviders(d.providers || [])).catch(() => {});
  }, []);
  const loadModels = useCallback(async (p, key) => {
    if (!p) return;
    if (key === undefined && modelOpts[p]) return;
    if (key !== undefined) setModelOpts((m) => { const n = { ...m }; delete n[p]; return n; });
    try {
      const d = await api('/providers/' + encodeURIComponent(p) + '/models', key ? { headers: { 'X-Api-Key': key } } : {});
      setModelOpts((m) => ({ ...m, [p]: (d.models || []).map((x) => ({ value: x.id, label: x.id, context_length: x.context_length, pricing: x.pricing })) }));
    } catch (e) {
      message.warning(`拉取 ${p} 模型列表失败（${errText(e)}）：可填 provider key 后重试，或直接手输模型名`);
    }
  }, [modelOpts]);
  useEffect(() => { if (prov) loadModels(prov); }, [prov]);

  return (<>
    <Form.Item name="provider" label="Provider" rules={[{ required: true }]}>
      <Select showSearch placeholder="选择 provider"
              options={providers.map((p) => ({ value: p.name, label: p.name + (p.local ? '（内网）' : '') }))}
              onChange={(v) => loadModels(v, provKey || undefined)} />
    </Form.Item>
    <Form.Item label="provider key（拉模型列表/验证出口用，不落库）"
      tooltip="gateway_only 模式实际外发走网关 env key：该 provider 未配 env key 时候选保存后外发会 503（可用「Provider 健康检查」验证）">
      <Input.Password placeholder="生产缺该 provider 的 env key 时填写，不落库"
                      autoComplete="new-password" value={provKey}
                      onChange={(e) => setProvKey(e.target.value)}
                      onBlur={() => { if (prov && provKey) loadModels(prov, provKey); }}
                      onPressEnter={() => { if (prov && provKey) loadModels(prov, provKey); }} />
    </Form.Item>
    <Form.Item name="model" label="模型" rules={[{ required: true }]}>
      <AutoComplete options={modelOpts[prov] || []} style={{ width: '100%' }}
                    filterOption={(inp, opt) => String((opt && opt.value) || '').toLowerCase().includes(inp.toLowerCase())}
                    placeholder="下拉选择或手输模型名"
                     onSelect={(v, opt) => {
                      // 上游 pricing 是 USD/token；价格字段单位 ¥/1M（固定汇率 7.2 换算，排序只比相对大小）
                      const toPer1M = (x) => {
                        const f = parseFloat(x);
                        return Number.isFinite(f) ? Math.round(f * 1e6 * 7.2 * 1e4) / 1e4 : null;
                      };
                      const patch = {};
                      if (opt && opt.context_length) patch.context_length = opt.context_length;
                      const pr = (opt && opt.pricing) || {};
                      const cIn = toPer1M(pr.prompt);  if (cIn != null) patch.price_per_1m_in = cIn;
                      const cOut = toPer1M(pr.completion); if (cOut != null) patch.price_per_1m_out = cOut;
                      const cCached = toPer1M(pr.cached_tokens ?? pr.cached); if (cCached != null) patch.price_per_1m_cached = cCached;
                      if (Object.keys(patch).length) {
                        form.setFieldsValue(patch);
                        message.success('已按上游返回自动填入价格/上下文');
                      }
                    }} />
    </Form.Item>
  </>);
}

// 外部候选弹窗共享组件：两个卡（ExternalCandidatesCard / AliasGroupsCard）统一入口。
export default function ExternalCandidateModal({ open, onOk, onCancel, initial, title = '新增候选', okText = '保存' }) {
  const [form] = Form.useForm();
  useEffect(() => {
    if (open) {
      form.resetFields();
      form.setFieldsValue({
        provider: '', model: '',
        price_per_1m_in: initial?.price_per_1m_in ?? 0,
        price_per_1m_out: initial?.price_per_1m_out ?? 0,
        price_per_1m_cached: initial?.price_per_1m_cached ?? 0,
        context_length: initial?.context_length ?? 65536,
        rank: initial?.rank ?? 100,
        note: initial?.note ?? '',
        ...initial,
      });
    }
  }, [open, initial]);

  return (
    <Modal title={title} open={open} onCancel={onCancel} destroyOnClose
           okText={okText} cancelText="取消"
           onOk={async () => {
             const v = await form.validateFields();
             await onOk(v, form);
           }}>
      <Form form={form} layout="vertical" preserve={false}>
        <ExternalCandidateFormItems form={form} />
        <Form.Item name="price_per_1m_in" label="价格入(¥/1M)"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>
        <Form.Item name="price_per_1m_out" label="价格出(¥/1M)"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>
        <Form.Item name="price_per_1m_cached" label="价格缓存命中(¥/1M，0=按全价)" tooltip="上游缓存命中 token 的单价；0 表示未配置，计费时缓存部分按全价算（高估）"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>
        <Form.Item name="context_length" label="上下文"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>
        <Form.Item name="rank" label="Rank（小优先）"><InputNumber style={{ width: '100%' }} /></Form.Item>
        <Form.Item name="note" label="备注"><Input /></Form.Item>
      </Form>
    </Modal>
  );
}

