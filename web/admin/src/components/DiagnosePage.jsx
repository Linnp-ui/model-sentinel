import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import {
  Layout, Menu, Table, Button, AutoComplete, Input, InputNumber, Modal, Form, Select, Tag, Space,
  message, Popconfirm, Typography, Alert, Card, Statistic, Switch, Slider, Tabs,
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
// ---------------- 运维诊断：路由检查器（L1→L2→路由 全链干跑） ----------------
function RouteInspectCard() {
  const [model, setModel] = useState('ext-flash');
  const [text, setText] = useState('');
  const [channel, setChannel] = useState('check');
  const [picked, setPicked] = useState([]);
  const [res, setRes] = useState(null);
  const [running, setRunning] = useState(false);
  const pickFiles = async (e) => {
    const fs = [...(e.target.files || [])].slice(0, 5);
    try {
      setPicked(await Promise.all(fs.map((f) => new Promise((ok, no) => {
        const r = new FileReader();
        r.onload = () => ok({ filename: f.name, file_data: r.result });
        r.onerror = no;
        r.readAsDataURL(f);
      }))));
    } catch (err) { message.error(errText(err)); }
    e.target.value = '';
  };
  const run = async () => {
    if (!text.trim() && !model.trim() && !picked.length) { message.warning('文本 / model / 文件至少给一个'); return; }
    setRunning(true);
    try {
      setRes(await api('/route-inspect', { method: 'POST', body: JSON.stringify({
        model, text, channel: picked.length ? channel : '', files: picked,
      }) }));
    } catch (e) { message.error(errText(e)); }
    setRunning(false);
  };
  const actTag = (a) => a === 'allow' ? <Tag color="green">allow</Tag>
    : a === 'route_local' ? <Tag color="orange">route_local</Tag> : <Tag color="red">{a}</Tag>;
  return (
    <Card title="路由检查器（生产同款 L1→L2→路由 决策链，不落审计不限流）">
      <Space direction="vertical" style={{ width: '100%' }} size="small">
        <Input placeholder="model（如 ext-flash 或 deepseek/deepseek-v4-pro）"
          value={model} onChange={(e) => setModel(e.target.value)} style={{ maxWidth: 380 }} />
        <Input.TextArea rows={5} placeholder="待审查文本（L1 命中即返回；L1 放行且 >30 字或灰区才触发 L2）"
          value={text} onChange={(e) => setText(e.target.value)} />
        <Space size="middle" wrap>
          <Select style={{ width: 300 }} value={channel} onChange={setChannel} options={[
            { value: 'check', label: '文件通道：files/check 全量解析' },
            { value: 'chat', label: '文件通道：workbuddy image_url 内联图' },
            { value: 'responses', label: '文件通道：codex input_file 块' },
          ]} />
          <input type="file" multiple onChange={pickFiles} />
          {picked.length > 0 && <Tag color="blue">已选 {picked.length} 个：{picked.map((f) => f.filename).join('、')}</Tag>}
          {picked.length > 0 && <Button size="small" onClick={() => setPicked([])}>清除文件</Button>}
        </Space>
        <Button type="primary" loading={running} onClick={run}>运行检查</Button>
        {res && (
          <Alert type={res.final_action === 'allow' ? 'success' : res.final_action === 'route_local' ? 'warning' : 'error'}
            showIcon message={<span>终判：{actTag(res.final_action)}（{res.final_rule || '-'}）</span>}
            description={
              <Space direction="vertical" size={4}>
                <Space size="small" wrap>
                  <span>L1：{res.l1.rule || '-'} {actTag(res.l1.action)}</span>
                  {res.l2.triggered
                    ? <span>L2：{res.l2.label} conf={res.l2.confidence}（{res.l2.latency_ms}ms）</span>
                    : <span>L2 跳过：{res.l2.skipped || '-'}</span>}
                  {res.channel && res.channel !== 'text' && <span>通道：{res.channel}</span>}
                  <Text code>去向：{res.routing.provider || res.routing.error} {res.routing.local ? '（内网）' : '（外网）'} {res.routing.model || ''}</Text>
                </Space>
                {(res.l2.triggered || res.l1.findings || (res.routing.gateway_key && !res.routing.gateway_key.configured)) && (
                  <Space direction="vertical" size={2}>
                    {res.l2.triggered && res.l2.reason && <div>L2 理由：{res.l2.reason}</div>}
                    {res.l2.triggered && res.l2.degraded && <div><Tag color="volcano">L2 降级兜底（超时/坏 JSON/熔断）</Tag></div>}
                    {res.l1.findings && Object.keys(res.l1.findings).length > 0
                      && <div>L1 发现：{JSON.stringify(res.l1.findings)}</div>}
                    {res.files && res.files.length > 0 && res.files.map((f, i) => (
                      <div key={i}>文件 {f.filename}{f.ext ? `.${f.ext}` : ''}{f.parsed_chars != null ? `（解析 ${f.parsed_chars} 字）` : ''}：{f.note || ''}</div>
                    ))}
                    {res.routing.gateway_key && !res.routing.gateway_key.configured && res.routing.gateway_key.note
                      && <div><Tag color="red">{res.routing.gateway_key.note}</Tag></div>}
                  </Space>
                )}
              </Space>} />
        )}
      </Space>
    </Card>
  );
}


export default function DiagnosePage() {
  return <RouteInspectCard />;
}
