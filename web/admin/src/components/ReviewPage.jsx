import { useState } from 'react';
import { Button, Input, Space, message, Alert, Card } from 'antd';
import { api, errText } from '../api.js';
import RouteInspectCard from './DiagnosePage.jsx';

// 规则测试 + 路由检查器：从 RulesPage 抽出的干跑工具页（改 L1/L2 规则后在这里验，
// 不落审计、不限流、不真实出境）。与规则管理的区别：这里是"验证"，那边是"配置"
export default function ReviewPage() {
  const [l1Test, setL1Test] = useState(null);
  const [l2Test, setL2Test] = useState(null);
  const [l2Input, setL2Input] = useState('');

  const runL1Test = async () => {
    try {
      const f = document.getElementById('l1test-text');
      const fn = document.getElementById('l1test-filename');
      setL1Test(await api('/policies/test', { method: 'POST', body: JSON.stringify({
        text: f?.value || '', filename: fn?.value || '' }) }));
    } catch (e) { message.error(errText(e)); }
  };

  const runL2Test = async () => {
    try {
      const d = await api('/l2/test', { method: 'POST', body: JSON.stringify({ text: l2Input }) });
      setL2Test(d.result);
    } catch (e) { message.error(errText(e)); }
  };

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card title="L1 规则测试">
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input.TextArea id="l1test-text" rows={4} placeholder="模拟提示词内容…" />
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
            <Input id="l1test-filename" style={{ width: 200 }} placeholder="模拟文件名（可选）" />
            <Button type="primary" onClick={runL1Test}
              style={{ marginLeft: 'auto' }}>测试命中</Button>
          </div>
          {l1Test && (
            <Alert showIcon type={l1Test.matched ? 'warning' : 'success'}
              message={l1Test.matched
                ? `命中规则 ${l1Test.rule} -> ${l1Test.action}`
                : '未命中任何规则（默认放行）'} />
          )}
        </Space>
      </Card>

      <Card title="L2 规则测试">
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input.TextArea rows={4} value={l2Input} onChange={(e) => setL2Input(e.target.value)}
            placeholder="粘贴待识别内容，查看 L2 审查判定（label / confidence / reason）…" />
          <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <Button type="primary" onClick={runL2Test}>运行 L2 判定</Button>
          </div>
          {l2Test && (
            <Alert showIcon
              type={l2Test.label === 'CONFIDENTIAL' ? 'warning' : 'success'}
              message={`${l2Test.label}  confidence=${l2Test.confidence}`}
              description={`${l2Test.reason || ''}${l2Test.disabled ? '（L2 未启用，当前为降级结果）' : ''}`} />
          )}
        </Space>
      </Card>

      <RouteInspectCard />
    </Space>
  );
}
