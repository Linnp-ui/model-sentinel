import { useState } from 'react';
import { Button, Input, Space, message, Alert, Card } from 'antd';
import { api, errText } from '../api.js';
import RouteInspectCard from './DiagnosePage.jsx';

// 干跑工具页（改 L1/L2 规则后在这里验，不落审计、不限流、不真实出境）。
// 与规则管理的区别：这里是"验证"，那边是"配置"。
// L1 规则测试卡 2026-10-19 删除：只跑 decide()，是「路由检查器」的真子集
// （后者同样回命中规则名/动作，还支持上传文件、跑文件解析与 L2/路由全链）——
// 两个工具留一个，避免"看着一样、结果不同"。后端 /policies/test 仍在，无 UI 消费者。
export default function ReviewPage() {
  const [l2Test, setL2Test] = useState(null);
  const [l2Input, setL2Input] = useState('');

  const runL2Test = async () => {
    try {
      const d = await api('/l2/test', { method: 'POST', body: JSON.stringify({ text: l2Input }) });
      setL2Test(d.result);
    } catch (e) { message.error(errText(e)); }
  };

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
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
