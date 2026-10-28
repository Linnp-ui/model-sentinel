import { useEffect, useState, useCallback } from 'react';
import { Table, Button, Input, InputNumber, Modal, Form, Select, Tag, Space, message, Popconfirm, Typography, Card, Switch, Row, Col } from 'antd';
import { api, errText, usePoll } from '../api.js';
import { wideSticky } from '../utils.js';
import { Tip } from '../terms.jsx';

const { Text } = Typography;
// ---------------- Provider 注册表（routing.yaml 读写；密钥仍只走 env） ----------------
function ProviderRegistryCard() {
  const [data, setData] = useState(null);
  const [modal, setModal] = useState(null);
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try { setData(await api('/providers')); } catch (e) { message.error(errText(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const openModal = (row) => {
    setModal(row ? { row } : {});
    form.setFieldsValue(row ? {
      name: row.name, base_url: row.base_url || '',
      api_key: '', api_mode: row.api_mode || 'openai', kind: row.kind || 'chat',
      default_model: row.default_model || '',
      timeout: row.timeout ?? 60, local: !!row.local,
    } : { api_mode: 'openai', kind: 'chat', timeout: 60, api_key: '' });
  };

  // 点确定必过本地模型确认：仅「候选全内网」的别名组跳过 L1/L2，标错即安全边界错
  const save = async () => {
    const v = await form.validateFields();
    Modal.confirm({
      title: '确认本地模型属性',
      content: `「${v.name}」是否为本地模型（内容不出境）= ${v.local ? '是（内网）' : '否（外网）'}；仅当它属于「候选全内网」的别名组时整组跳过 L1/L2，单独点名的内网模型仍照常审查。设错请点取消回去改开关。`,
      okText: '确认无误，保存',
      onOk: () => doSave(v),
    });
  };
  const doSave = async (v) => {
    const keyVal = (v.api_key || '').trim();
    const body = { ...v };
    delete body.api_key;
    setSaving(true);
    try {
      const oldName = modal?.row ? modal.row.name : body.name;
      if (modal?.row) {
        if (body.name && body.name !== oldName) body.new_name = body.name;
        delete body.name;
      }
      const name = modal?.row ? (body.new_name || oldName) : body.name;
      if (modal?.row) await api(`/providers/${encodeURIComponent(oldName)}`, { method: 'PUT', body: JSON.stringify(body) });
      else await api('/providers', { method: 'POST', body: JSON.stringify(body) });
      if (keyVal) {
        try { await api(`/providers/${encodeURIComponent(name)}/key`, { method: 'PUT', body: JSON.stringify({ value: keyVal }) }); }
        catch (e) { message.warning(`provider 已保存，但 key 写入失败：${errText(e)}`); }
      }
      message.success('已保存，热重载即时生效');
      setModal(null);
      load();
    } catch (e) { message.error(errText(e)); }
    setSaving(false);
  };

  const [keyModal, setKeyModal] = useState(null);
  const [keyVal, setKeyVal] = useState('');
  const [keyBusy, setKeyBusy] = useState(false);
  const saveKey = async () => {
    const r = keyModal.row;
    setKeyBusy(true);
    try {
      await api(`/providers/${encodeURIComponent(r.name)}/key`, { method: 'PUT', body: JSON.stringify({ value: keyVal }) });
      message.success(keyVal.trim() ? `${r.api_key_env} 已保存，即时生效（免重启）` : `${r.api_key_env} 已清除`);
      setKeyModal(null);
      setKeyVal('');
      load();
      // 存完顺手健康检查，闭环验证 key 是否真的可用
      if (keyVal.trim()) {
        try {
          const d = await api('/providers/check', { method: 'POST', body: JSON.stringify({ name: r.name }) });
          const one = (d.results || [])[0] || {};
          if (one.ok) message.success(`健康检查：${r.name} 可达（${one.latency_ms}ms，${one.model_count} 个模型）`);
          else message.warning(`健康检查：${r.name} 仍失败 — ${one.error || 'HTTP ' + one.status}（key 可能无效）`);
        } catch (e) { /* 验证失败不阻塞保存结果 */ }
      }
    } catch (e) { message.error(errText(e)); }
    setKeyBusy(false);
  };

  const rows = data?.providers || [];
  return (
    <Card title={<>注册表<Tip term="provider" /></>}
      extra={<Button type="primary" size="small" onClick={() => openModal(null)}>新增 provider</Button>}>
      <Table size="small" sticky={wideSticky()} rowKey="name" className="tbl-registry" pagination={false} tableLayout="fixed" dataSource={rows} columns={[
        /* 列宽全 % + tableLayout=fixed：跟随容器、按比例分配。Key 列已删（2026-10-16）：
           环境变量名+已配/缺 信息密度低，配/改 入口并入操作列 */
        { title: '名称', dataIndex: 'name', width: '14%', ellipsis: true },
        { title: <>{'base_url'}<Tip term="base_url" /></>, dataIndex: 'base_url', width: '38%', ellipsis: true },
        { title: <>{'类型'}<Tip term="local_provider" /></>, width: '12%', render: (_, r) => (
          <Space size={4}>{r.local ? <Tag color="blue" style={{ marginInlineEnd: 0 }}>内网</Tag> : <Tag style={{ marginInlineEnd: 0 }}>外网</Tag>}{r.kind === 'embedding' && <Tag color="purple" style={{ marginInlineEnd: 0 }}>向量</Tag>}</Space>) },
        { title: '操作', width: '36%', render: (_, r) => (
          <Space>
            <Button type="link" size="small" onClick={async () => {
              try {
                const d = await api('/providers/check', { method: 'POST', body: JSON.stringify({ name: r.name }) });
                const one = (d.results || [])[0] || {};
                if (one.ok) message.success(`健康检查：${r.name} 可达（${one.latency_ms}ms，${one.model_count} 个模型）`);
                else message.warning(`健康检查：${r.name} 仍失败 — ${one.error || 'HTTP ' + one.status}（key 可能无效）`);
              } catch (e) { message.error(errText(e)); }
            }}>检查</Button>
            <Button type="link" size="small" onClick={() => openModal(r)}>编辑</Button>
            <Popconfirm title={`删除 ${r.name}？被默认路由/候选/别名/策略引用时会拒绝`} onConfirm={async () => {
              try { await api(`/providers/${encodeURIComponent(r.name)}`, { method: 'DELETE' }); message.success('已删除'); load(); }
              catch (e) { message.error(errText(e)); }
            }}><Button type="link" size="small" danger>删除</Button></Popconfirm>
          </Space>) },
      ]} />
      <Modal open={!!modal} title={modal?.row ? `编辑 provider ${modal.row.name}` : '新增 provider'}
        onOk={save} confirmLoading={saving} onCancel={() => setModal(null)} destroyOnClose width={560}>
        <Form form={form} layout="vertical" size="small">
          <Form.Item name="name" label="名称" rules={[{ required: true, pattern: /^[A-Za-z0-9_-]+$/, message: '仅字母/数字/_/-' }]}>
            <Input placeholder="如 oneapi" />
          </Form.Item>
          <Form.Item name="base_url" label={<>{'base_url'}<Tip term="base_url" /></>} rules={[{ required: true }]}>
            <Input placeholder="https://api.example.com/v1" />
          </Form.Item>
          <Row gutter={8}>
            <Col span={12}>
              <Form.Item name="api_mode" label={<>{'接口类型'}<Tip term="api_mode" /></>}>
                <Select options={(data?.api_modes || ['openai']).map((m) => ({ value: m, label: ({
                  openai: 'OpenAI Compatible',
                  openai_responses: 'OpenAI Responses',
                  anthropic: 'Anthropic',
                  bedrock: 'Amazon Bedrock',
                  gemini: 'Google (Gemini)',
                }[m] || m) }))} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="kind" label="类型">
                <Select options={[{ value: 'chat', label: 'chat' }, { value: 'embedding', label: 'embedding' }]} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="api_key" label="API Key（可选，仅写不回显）">
            <Input.Password placeholder={modal?.row?.configured ? '留空=不修改' : '留空=不配置'} autoComplete="new-password" />
          </Form.Item>
          <Form.Item name="timeout" label="超时（秒）" rules={[{ required: true }]}>
            <InputNumber min={1} max={600} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="local" label={<>{'内网 provider（local: true，内容不出境）'}<Tip term="local_provider" /></>} valuePropName="checked">
            <Switch aria-label="内网 provider" />
          </Form.Item>
        </Form>
      </Modal>
      <Modal open={!!keyModal} title={keyModal ? `配置 ${keyModal.row.api_key_env}（${keyModal.row.name}）` : ''}
        onOk={saveKey} confirmLoading={keyBusy} okText="保存"
        onCancel={() => { setKeyModal(null); setKeyVal(''); }} destroyOnClose>
        <Input.Password value={keyVal} onChange={(e) => setKeyVal(e.target.value)}
          placeholder={keyModal?.row?.configured ? '留空保存 = 清除该 key' : 'key 值（至少 8 位）'}
          autoComplete="new-password" onPressEnter={saveKey} />
        <Text type="secondary" style={{ display: 'block', marginTop: 'var(--gw-sp-8)' }}>
          即时生效（免重启）并写入 .env 持久化；保存后自动跑健康检查验证；
          值只写不读，任何端点不返回明文
        </Text>
      </Modal>
    </Card>
  );
}

// ---------------- 运维诊断：provider 健康检查 ----------------
// 总览页也内嵌这张卡（手动触发，不自动轮询：检查打上游真实 HTTP）
export function ProviderHealthCard({ compact = false }) {
  const [rows, setRows] = useState(null);
  const [checking, setChecking] = useState(false);
  const check = async (name) => {
    setChecking(true);
    try {
      const d = await api('/providers/check', { method: 'POST', body: JSON.stringify({ name: name || '' }) });
      setRows(d.results);
      const bad = (d.results || []).filter((r) => !r.ok).length;
      if (bad) message.warning(`健康检查完成：${bad} 个异常`);
      else message.success('健康检查完成：全部正常');
    } catch (e) { message.error(errText(e)); }
    setChecking(false);
  };
  return (
    <Card title="Provider 健康检查"
      extra={<Space>
        {compact && <Button type="link" size="small" style={{ padding: 0 }} onClick={() => { window.location.hash = '/provider'; }}>Provider 管理 →</Button>}
        <Button size="small" loading={checking} onClick={() => check('')}>全部检查</Button>
      </Space>}>
      <Table size="small" sticky={wideSticky()} rowKey="name" pagination={false} tableLayout="fixed" dataSource={rows || []}
        locale={{ emptyText: '尚未检查，点右上角「全部检查」开始' }}
        columns={[
          /* 列宽 % + tableLayout=fixed：全量模式占比合计 100；compact 只留前两列，
             fixed 布局会把剩余宽度按列均分，两列同样铺满卡片 */
          { title: 'Provider', dataIndex: 'name', width: '24%' },
          { title: '状态', width: '12%', render: (_, r) => r.ok ? <Tag color="green">正常</Tag> : <Tag color="red">异常</Tag> },
          ...(!compact && [
            { title: <>{'延迟(ms)'}<Tip text="这次健康检查往返用了多久（毫秒）。越大说明上游越慢或网络越绕。" /></>, dataIndex: 'latency_ms', width: '16%' },
            { title: <>{'模型数'}<Tip text="这个上游当前对外提供几个模型可选。" /></>, dataIndex: 'model_count', width: '16%' },
            { title: '操作', width: '32%', render: (_, r) => (
              <Button type="link" size="small" loading={checking} onClick={() => check(r.name)}>检查</Button>) },
          ] || []),
        ]} />
    </Card>
  );
}

// ---------------- 运维诊断：熔断器（逐 provider 明细 + 手动置位） ----------------
function CircuitCard() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState('');
  const load = useCallback(async (silent = false) => {
    try { setData(await api('/circuit')); } catch (e) { if (!silent) message.error(errText(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);
  usePoll(() => load(true)); // 替代原刷新按钮（熔断状态含冷却倒计时，30s 自动跟进）
  const setState = async (name, state) => {
    setBusy(name);
    try {
      await api(`/circuit/${encodeURIComponent(name)}`, { method: 'POST', body: JSON.stringify({ state }) });
      message.success(`${name} 已置为 ${state}`);
      load();
    } catch (e) { message.error(errText(e)); }
    setBusy('');
  };
  const rows = Object.entries(data?.providers || {}).map(([name, p]) => ({ name, ...p }));
  const cfg = data?.config || {};
  return (
    <Card title={<>熔断器<Tip term="circuit" /></>}>
      {/* 口径说明常驻小字：每格都要能回答「这一格是什么、为什么是这个值」 */}
      <Typography.Paragraph type="secondary" style={{ fontSize: 'var(--gw-fs-12)', lineHeight: 1.8, marginBottom: 'var(--gw-sp-8)' }}>
        <div>· <b>打开</b>：滑动窗口 <b>{cfg.window_seconds ?? '—'}s</b> 内失败 ≥ <b>{cfg.failure_threshold ?? '—'}</b> 次即 <b>OPEN</b>（熔断键 = provider::模型，别名组候选与点名请求共用一份状态），该组合之后直接 fail-fast 返回 503（不真实出境）；别名组跳过该候选顺延下一候选，点名请求先在候选表内顺延，全部不可用才 503（route_local/block 不切换）。</div>
        <div>· <b>冷却与恢复</b>：OPEN 后冷却 <b>{cfg.recovery_seconds ?? '—'}s</b>（冷却剩余秒数见下表）自动转 <b>HALF_OPEN</b> 放行探针流量，成功 <b>{cfg.success_threshold ?? '—'}</b> 次回 <b>CLOSED</b>（清空失败计数）；探针再失败立刻重新 OPEN 并重新计时。</div>
      </Typography.Paragraph>
      <Table size="small" sticky={wideSticky()} rowKey="name" className="tbl-circuit" pagination={false} tableLayout="fixed" dataSource={rows}
        columns={[
          /* 列宽用 % 自适应（灌数轮次表口径） */
          { title: 'Provider', dataIndex: 'provider', width: '16%' },
          { title: '模型', dataIndex: 'model', width: '18%', render: (v) => v || '-' },
          { title: '状态', dataIndex: 'state', width: '12%', render: (v) => v === 'open'
            ? <Tag color="red">OPEN</Tag> : v === 'half_open' ? <Tag color="orange">HALF_OPEN</Tag> : <Tag color="green">CLOSED</Tag> },
          { title: '窗口内失败', dataIndex: 'failure_count_window', width: '12%' },
          { title: '冷却剩余(s)', dataIndex: 'cooldown_remaining_s', width: '12%' },
          { title: '操作', width: '30%', render: (_, r) => (
            <Space>
              <Popconfirm title={`手动打开 ${r.provider}${r.model ? '::' + r.model : ''} 熔断？该组合的请求不再直连它：别名组切链内下一候选（跳过内网候选）；点名请求先在候选表内顺延（可能落到别的 provider），全部不可用才 503；不会静默转本地`}
                onConfirm={() => setState(r.name, 'open')}>
                <Button type="link" size="small" danger loading={busy === r.name}>打开</Button>
              </Popconfirm>
              <Button type="link" size="small" loading={busy === r.name} onClick={() => setState(r.name, 'closed')}>关闭</Button>
            </Space>) },
        ]} />
    </Card>
  );
}


export default function ProviderPage() {
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <ProviderRegistryCard />
      <ProviderHealthCard />
      <CircuitCard />
    </Space>
  );
}
