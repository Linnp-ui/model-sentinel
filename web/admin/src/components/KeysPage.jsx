import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import {
  Layout, Menu, Table, Button, AutoComplete, Input, InputNumber, Modal, Form, Select, Tag, Space,
  message, Popconfirm, Typography, Alert, Card, Switch, Slider, Tabs,
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
// ---------------- 4.2 API KEY 管理 ----------------
export default function KeysPage() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [form] = Form.useForm();
  const [meta, setMeta] = useState(null);
  const [impOpen, setImpOpen] = useState(false);
  const [genOpen, setGenOpen] = useState(false);
  const [genForm] = Form.useForm();
  const [genResult, setGenResult] = useState(null);
  const [impText, setImpText] = useState('');
  const [impResult, setImpResult] = useState(null);
  const [impLoading, setImpLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems((await api('/keys')).items);
      setMeta(await api('/meta'));
    } catch (e) { message.error(errText(e)); }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  const openEdit = (rec) => {
    setEditing(rec.id);
    form.setFieldsValue({ name: rec.name, owner: rec.owner || '', note: rec.note || '' });
    setModalOpen(true);
  };
  const openCreate = () => { setEditing(null); form.resetFields(); setModalOpen(true); };
  const submit = async () => {
    try {
      const v = await form.validateFields();
      if (editing) {
        await api(`/keys/${editing}`, { method: 'PATCH',
          body: JSON.stringify({ name: v.name, owner: v.owner || '', note: v.note || '' }) });
      } else {
        await api('/keys', { method: 'POST', body: JSON.stringify(v) });
      }
      message.success('已登记');
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

  const toggle = async (rec) => {
    await api(`/keys/${rec.id}`, { method: 'PATCH', body: JSON.stringify({ disabled: !rec.disabled }) });
    load();
  };

  const del = async (id) => { await api(`/keys/${id}`, { method: 'DELETE' }); message.success('已删除'); load(); };

  const doImport = async () => {
    setImpLoading(true);
    try {
      const d = await api('/keys/import', { method: 'POST', body: JSON.stringify({ text: impText }) });
      setImpResult(d);
      load();
    } catch (e) { message.error(errText(e)); }
    setImpLoading(false);
  };


  const cols = [
    { title: '名称（人员）', dataIndex: 'name' },
    { title: 'KEY', dataIndex: 'key_masked', render: (v) => <Text code>{v}</Text> },
    { title: '备注', dataIndex: 'note', width: 400, ellipsis: true },
    { title: '状态', dataIndex: 'disabled', width: 72, render: (v, rec) => (<Tag data-testid={`keys-status-${rec.id}`} color={v ? 'red' : 'green'}>{v ? '停用' : '启用'}</Tag>) },
    { title: '登记时间', dataIndex: 'created_at' },
    {
      title: '操作',
      render: (_, rec) => (
        <Space>
          <Button onClick={() => openEdit(rec)}>编辑</Button>
          <Button onClick={() => toggle(rec)}>{rec.disabled ? '启用' : '停用'}</Button>
          <Popconfirm title="确认删除该 KEY 映射？" onConfirm={() => del(rec.id)}>
            <Button data-testid={`keys-del-${rec.id}`} danger>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Space>
        <Button type="primary" icon={<KeyOutlined />} onClick={openCreate}>登记 KEY</Button>
        <Button icon={<KeyOutlined />} onClick={openGenerate}>生成 KEY</Button>
        <Button icon={<ImportOutlined />} onClick={() => { setImpResult(null); setImpOpen(true); }}>批量导入</Button>
        <Button data-testid="keys-reload" icon={<ReloadOutlined />} onClick={load}>刷新</Button>
        {meta && <Text data-testid="keys-meta-registered" style={{ marginLeft: 16, fontSize: 16 }}>已登记 <Text strong style={{ fontSize: 18 }}>{meta.api_keys}</Text></Text>}
      </Space>
      <Table rowKey="id" columns={cols} dataSource={items} loading={loading} size="small"
        locale={{ emptyText: '暂无已登记 KEY，点上方「登记 KEY」添加' }}
        pagination={{ pageSize: 50, showSizeChanger: false }} />
      <Modal title={editing ? "编辑 API KEY" : "登记 API KEY"} open={modalOpen} onOk={submit} onCancel={() => { setEditing(null); form.resetFields(); setModalOpen(false); }}>
        <Form form={form} layout="vertical">
          {editing ? null : (
          <Form.Item name="key" label="KEY 明文" rules={editing ? [] : [{ required: true, min: 8 }]}>
            <Input.Password id="key-secret" placeholder="sk-..." />
          </Form.Item>
          )}
          <Form.Item name="name" label="名称（人员标识，统计归因用）" rules={[{ required: true }]}>
            <Input id="key-name" />
          </Form.Item>
          <Form.Item name="owner" label="归属团队"><Input id="key-owner" /></Form.Item>
          <Form.Item name="note" label="备注"><Input.TextArea id="key-note" rows={2} /></Form.Item>
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
                  const fail = () => message.error('复制失败，请手动选中复制');
                  const done = () => message.success('已复制到剪贴板');
                  if (navigator.clipboard) {
                    navigator.clipboard.writeText(txt).then(done, fail);
                  } else {
                    try {
                      const ta = document.createElement('textarea');
                      ta.value = txt;
                      ta.style.position = 'fixed';
                      ta.style.left = '-9999px';
                      ta.style.top = '-9999px';
                      ta.style.opacity = '0';
                      document.body.appendChild(ta);
                      ta.select();
                      const ok = document.execCommand('copy');
                      document.body.removeChild(ta);
                      if (ok) done(); else fail();
                    } catch { fail(); }
                  }
                }}
              >
                复制
              </Button>
            </Space>
          </div>
        )}
      </Modal>
      <Modal title="批量导入 KEY（每行一个：名称, key）" open={impOpen} onOk={doImport} onCancel={() => setImpOpen(false)}
        okText="导入" confirmLoading={impLoading} width={640}>
        <Input.TextArea rows={8} value={impText} onChange={(e) => setImpText(e.target.value)}
          placeholder={'张三, sk-xxx-aaaa1111\n李四，sk-xxx-bbbb2222\n# 以 # 开头的行为注释'}
          style={{ fontFamily: 'monospace' }} />
        {impResult && (
          <div style={{ marginTop: 12, maxHeight: 240, overflow: 'auto' }}>
            <Text strong>已导入 {impResult.added} 个</Text>
            <Table rowKey="line" size="small" pagination={false} style={{ marginTop: 8 }}
              dataSource={impResult.results}
              columns={[
                { title: '行', dataIndex: 'line', width: 50 },
                { title: 'KEY', dataIndex: 'key_masked' },
                { title: '结果', dataIndex: 'status', width: 90,
                  render: (v) => (v === 'ok' ? <Tag color="green">成功</Tag> : v === 'duplicate' ? <Tag color="orange">已存在</Tag> : <Tag color="red">无效</Tag>) },
                { title: '说明', dataIndex: 'message' },
              ]} />
          </div>
        )}
      </Modal>
    </Space>
  );
}

