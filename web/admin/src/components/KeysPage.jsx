import { useEffect, useState, useCallback } from 'react';
import { Table, Button, Input, Modal, Form, Space, message, Popconfirm, Typography, Alert, Card, Tag } from 'antd';
import { KeyOutlined, CopyOutlined, ImportOutlined } from '@ant-design/icons';
import { api, errText, copyText } from '../api.js';
import { wideSticky } from '../utils.js';
import { Tip } from '../terms.jsx';

const { Text } = Typography;
// 批量导入：每行一条（名称,key 或只写 key）；名称用于统计归因，缺名称按行号自动命名
const IMPORT_HINT = `每行一条，支持两种写法：
名称,key            推荐：名称用于统计归因（谁的调用算谁的）
sk-xxxxxxxxxxxx     只写 key 也收：名称自动取 导入-<行号>
# 开头的行是注释，忽略；key 至少 8 位；已登记的 key 会被跳过`;
// ---------------- 4.2 API KEY 管理 ----------------
export default function KeysPage() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [form] = Form.useForm();
  const [genOpen, setGenOpen] = useState(false);
  const [genForm] = Form.useForm();
  const [genResult, setGenResult] = useState(null);
  const [impOpen, setImpOpen] = useState(false);
  const [impText, setImpText] = useState('');
  const [impResult, setImpResult] = useState(null);
  const [impBusy, setImpBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems((await api('/keys')).items);
    } catch (e) { message.error(errText(e)); }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  const openEdit = (rec) => {
    setEditing(rec.id);
    form.setFieldsValue({ name: rec.name, owner: rec.owner || '', note: rec.note || '', weekly_cost_cap: rec.weekly_cost_cap ?? 0 });
    setModalOpen(true);
  };
  const submit = async () => {
    try {
      const v = await form.validateFields();
      await api(`/keys/${editing}`, { method: 'PATCH',
        body: JSON.stringify({ name: v.name, owner: v.owner || '', note: v.note || '', weekly_cost_cap: Number(v.weekly_cost_cap) || 0 }) });
      message.success('已保存');
      setModalOpen(false);
      setEditing(null);
      form.resetFields();
      load();
    } catch (e) { if (e.errorFields) return; message.error(errText(e)); }
  };

  const openGenerate = () => { setGenResult(null); genForm.resetFields(); setGenOpen(true); };

  const doGenerate = async () => {
    try {
      const v = await genForm.validateFields();
      const d = await api('/keys/generate', { method: 'POST', body: JSON.stringify(v) });
      setGenResult(d.item);
      message.success('KEY 已生成，请尽快保存');
      load();
    } catch (e) { if (e.errorFields) return; message.error(errText(e)); }
  };

  const del = async (id) => {
    try { await api(`/keys/${id}`, { method: 'DELETE' }); message.success('已删除'); load(); }
    catch (e) { message.error(errText(e)); }
  };

  const openImport = () => { setImpResult(null); setImpText(''); setImpOpen(true); };

  const doImport = async () => {
    if (!impText.trim()) { message.error('请先粘贴要导入的 KEY'); return; }
    setImpBusy(true);
    try {
      const d = await api('/keys/import', { method: 'POST', body: JSON.stringify({ text: impText }) });
      setImpResult(d);
      message.success(`导入完成：新增 ${d.added} 条`);
      load();
    } catch (e) { message.error(errText(e)); }
    setImpBusy(false);
  };


  /* 列宽全 % + tableLayout=fixed：表跟随容器宽度、列按比例分配。
     备注 600px 定宽曾把窄屏表撑出横向滚动；启用/停用功能已删（2026-10-14） */
  const cols = [
    { title: '名称', dataIndex: 'name', width: '14%', ellipsis: true, className: 'cell-center' },
    { title: 'KEY', dataIndex: 'key_masked', width: '16%', ellipsis: true, className: 'cell-center', render: (v) => <Text code>{v}</Text> },
    { title: '备注', dataIndex: 'note', width: '27%', ellipsis: true },
    { title: '登记时间', dataIndex: 'created_at', width: '16%', ellipsis: true, className: 'cell-center' },
    {
      title: '操作',
      width: '27%',
      render: (_, rec) => (
        <Space wrap>
          <Button type="link" onClick={() => openEdit(rec)}>编辑</Button>
          <Popconfirm title="确认删除该 KEY 映射？" onConfirm={() => del(rec.id)}>
            <Button type="link" data-testid={`keys-del-${rec.id}`} danger>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card title={<>已登记 KEY<Tip term="api_key" /></>} extra={<Space>
        <Button icon={<ImportOutlined />} onClick={openImport}>批量导入</Button>
        <Button type="primary" icon={<KeyOutlined />} onClick={openGenerate}>生成 KEY</Button>
      </Space>}>
      <Table rowKey="id" className="tbl-keys" columns={cols} dataSource={items} loading={loading} size="small"
        sticky={wideSticky()}
        tableLayout="fixed"
        locale={{ emptyText: '暂无已登记 KEY（点「生成 KEY」或「批量导入」添加）' }}
        pagination={{ pageSize: 50, showSizeChanger: false }} />
      </Card>
      <Modal title="编辑 API KEY" open={modalOpen} onOk={submit} onCancel={() => { setEditing(null); form.resetFields(); setModalOpen(false); }}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="名称（人员标识，统计归因用）" rules={[{ required: true }]}>
            <Input id="key-name" />
          </Form.Item>
          <Form.Item name="owner" label="归属团队"><Input id="key-owner" /></Form.Item>
          <Form.Item name="note" label="备注"><Input.TextArea id="key-note" rows={2} /></Form.Item>
          <Form.Item name="weekly_cost_cap" label={<>{'每周费用上限（元，0=不限）'}<Tip text="按这个 KEY 的 token 用量折算的内部费用；超过上限会进建议卡（不是自动断）。" /></>}><Input id="key-cost-cap" type="number" min={0} style={{ width: '100%' }} /></Form.Item>
        </Form>
      </Modal>
      <Modal title="生成 API KEY" open={genOpen}
        onOk={doGenerate}
        onCancel={() => { setGenOpen(false); genForm.resetFields(); }}
        okText="生成">
        <Form form={genForm} layout="vertical">
          <Form.Item name="name" label="名称（人员标识）" rules={[{ required: true, min: 1 }]}>
            <Input id="gen-name" placeholder="张三" />
          </Form.Item>
          <Form.Item name="owner" label="归属团队"><Input id="gen-owner" placeholder="平台/算法/产品" /></Form.Item>
          <Form.Item name="note" label="备注"><Input.TextArea id="gen-note" rows={2} placeholder="选填" /></Form.Item>
        </Form>
        {genResult && (
          <div style={{ marginTop: 12 }}>
            <Alert type="warning" showIcon message="KEY 明文只在此处显示一次，请立即保存。" />
            <Space style={{ marginTop: 8, display: 'flex', flexDirection: 'column' }}>
              <Text code style={{ wordBreak: 'break-all' }}>{genResult.key_plain}</Text>
              <Button
                icon={<CopyOutlined />}
                onClick={(e) => {
                  e.stopPropagation();
                  const txt = genResult.key_plain;
                  if (!txt) { message.error('复制失败，无内容'); return; }
                  copyText(txt, '已复制到剪贴板', '复制失败，请手动选中复制');
                }}
              >
                复制
              </Button>
            </Space>
          </div>
        )}
      </Modal>
      <Modal title="批量导入 API KEY（手写 key）" open={impOpen} width={720}
        onOk={doImport} okText="开始导入" confirmLoading={impBusy}
        onCancel={() => { setImpOpen(false); setImpText(''); setImpResult(null); }}
        destroyOnClose>
        <Alert type="info" showIcon style={{ whiteSpace: 'pre-wrap', marginBottom: 12 }}
          message={<div style={{ whiteSpace: 'pre-wrap', lineHeight: 1.8 }}>{IMPORT_HINT}</div>} />
        <Input.TextArea id="imp-text" rows={9} value={impText} onChange={(e) => setImpText(e.target.value)}
          placeholder={'张三,sk-abcdefghijklmnop\n李四,sk-zyxwvutsrqponmlk\n# 下面是只写 key 的写法\nsk-0011223344556677'} />
        {impResult && (
          <div style={{ marginTop: 12 }}>
            <Alert type={impResult.added ? 'success' : 'warning'} showIcon
              message={`共 ${impResult.results.length} 行，新增 ${impResult.added} 条（KEY 明文不会回显，只回掩码）`} />
            <Table size="small" style={{ marginTop: 8 }} rowKey="line" pagination={false}
              tableLayout="fixed" dataSource={impResult.results}
              columns={[
                { title: '行号', dataIndex: 'line', width: '10%' },
                { title: 'KEY（掩码）', dataIndex: 'key_masked', width: '34%', render: (v) => v ? <Text code>{v}</Text> : '-' },
                { title: '结果', dataIndex: 'status', width: '14%', render: (v) => v === 'ok'
                  ? <Tag color="green">已导入</Tag> : v === 'duplicate' ? <Tag color="orange">已存在</Tag> : <Tag color="red">无效</Tag> },
                { title: '说明', dataIndex: 'message', width: '42%', render: (v, r) => r.status === 'ok' ? `登记为 ${v}` : (v || '-') },
              ]} />
          </div>
        )}
      </Modal>
    </Space>
  );
}

