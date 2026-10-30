import { useEffect, useState, useCallback } from 'react';
import { AutoComplete, InputNumber, Modal, Form, Select, message } from 'antd';
import { api, errText } from '../api.js';

// ---------------- 1.1/1.2/1.3 模型配置 ----------------
// 外部候选表单共享项：Provider 下拉 + 按 provider 拉模型列表供模型自动补全（手输仍可用）。
// 用于 ExternalCandidatesCard 新增/编辑弹窗、别名弹窗内"新增外部候选"。
export function ExternalCandidateFormItems({ form, disabled = false }) {
  const [providers, setProviders] = useState([]);
  const [modelOpts, setModelOpts] = useState({}); // provider -> [{value,label}]
  const prov = Form.useWatch('provider', form);

  useEffect(() => {
    api('/providers').then((d) => setProviders(d.providers || [])).catch(() => {});
  }, []);
  const loadModels = useCallback(async (p) => {
    if (!p) return;
    if (modelOpts[p]) return;
    try {
      const d = await api('/providers/' + encodeURIComponent(p) + '/models');
      setModelOpts((m) => ({ ...m, [p]: (d.models || []).map((x) => ({ value: x.id, label: x.id, context_length: x.context_length, pricing: x.pricing })) }));
    } catch (e) {
      message.warning(`拉取 ${p} 模型列表失败（${errText(e)}）：生产缺该 provider env key 时先到 Provider 页配置，或直接手输模型名`);
    }
  }, [modelOpts]);
  useEffect(() => { if (prov) loadModels(prov); }, [prov]);

  return (<>
    <Form.Item name="provider" label="Provider" rules={[{ required: true }]}>
      <Select showSearch placeholder="选择 provider"
              disabled={disabled}
              options={providers.map((p) => ({ value: p.name, label: p.name + (p.local ? '（内网）' : '') }))}
              onChange={(v) => loadModels(v)} />
    </Form.Item>
    <Form.Item name="model" label="模型" rules={[{ required: true }]}>
      <AutoComplete disabled={disabled} options={modelOpts[prov] || []} style={{ width: '100%' }}
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
      </Form>
    </Modal>
  );
}

