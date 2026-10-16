import { Button, Input, Form, message, Card } from 'antd';
import { LockOutlined } from '@ant-design/icons';
import { api, errText } from '../api.js';

// ---------------- 1.4 管理员密码 ----------------
export default function PasswordPage() {
  const [form] = Form.useForm();

  const submit = async () => {
    try {
      const v = await form.validateFields();
      await api('/password', { method: 'POST', body: JSON.stringify(v) });
      message.success('密码已修改，请重新登录');
      setTimeout(() => { window.location.href = '/admin/login'; }, 1200);
    } catch (e) { if (e.errorFields) return; message.error(errText(e)); }
  };

  return (
    <Card title="修改管理员密码" style={{ maxWidth: 480, margin: '0 auto' }}>
      <Form form={form} layout="vertical" onFinish={submit}>
        <Form.Item name="old_password" label="当前密码" rules={[{ required: true }]}>
          <Input.Password />
        </Form.Item>
        <Form.Item name="new_password" label="新密码（至少 8 位）" rules={[{ required: true, min: 8 }]}>
          <Input.Password />
        </Form.Item>
        <Form.Item dependencies={['new_password']} noStyle>
          {({ getFieldValue }) => (
            <Form.Item name="confirm" label="确认新密码"
              rules={[{ required: true }, {
                validator: (_, v) => v === getFieldValue('new_password')
                  ? Promise.resolve() : Promise.reject(new Error('两次输入不一致')),
              }]}>
              <Input.Password />
            </Form.Item>
          )}
        </Form.Item>
        <Button type="primary" htmlType="submit" icon={<LockOutlined />}>修改密码</Button>
      </Form>
    </Card>
  );
}

