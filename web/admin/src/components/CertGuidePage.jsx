import { Alert, Button, Card, Space, Typography } from 'antd';
import { DownloadOutlined } from '@ant-design/icons';

const { Paragraph, Text, Title } = Typography;

// 代码块统一样式（改字号只改这一处）
const PRE = { overflow: 'auto', fontSize: 'var(--gw-fs-14)' };

// ---------------- HTTPS 证书安装（客户端一次性） ----------------
// 平台步骤以 docs/https-cert-install.md 为准（改文档顺手改这里）。两处差别：
// 文档写 https://<网关IP>:8080/ca.crt 要用户自己填 IP，这里用当前站点真实 origin；
// 页面多一个「直接下载 CA 证书」按钮，文档没有（文档多「一键下载 CA」一节）。
export default function CertGuidePage() {
  const origin = window.location.origin;
  return (
    <Space direction="vertical" className="cert-guide" style={{ width: '100%' }} size="middle">
      <Card title="HTTPS 证书安装（客户端一次性）">
        <Alert type="info" showIcon style={{ marginBottom: 'var(--gw-sp-12)' }}
          message="网关已切到 HTTPS。浏览器首次访问提示「不安全 / 证书不受信任」，是因为用的是内网自签 CA —— 把下面的 CA 装进本机信任库一次即可，之后不再提示。" />
        <Button type="primary" icon={<DownloadOutlined />} href="/ca.crt" download="gateway-internal-ca.crt">
          直接下载 CA 证书
        </Button>
      </Card>

      <Card title="安装步骤">
        <Title level={5}>Windows（Edge / Chrome）</Title>
        <ol style={{ margin: '0 0 var(--gw-sp-16)', paddingLeft: 20, lineHeight: 1.9, fontSize: 'var(--gw-fs-14)' }}>
          <li>双击下载的 <Text code>gateway-internal-ca.crt</Text></li>
          <li>「安装证书」→ 「当前用户」→ 「将所有的证书放入下列存储」</li>
          <li>「浏览」→ 选「受信任的根证书颁发机构」→ 确定 → 完成</li>
          <li>弹出安全警告点「是」</li>
          <li><Text strong>重启浏览器</Text>，再打开本控制台 → 不再提示不安全</li>
        </ol>
        <pre style={{ ...PRE, maxHeight: 200 }}>
          {`# 命令行方式（PowerShell，点弹窗「是」）\ncertutil -user -addstore Root gateway-internal-ca.crt`}
        </pre>

        <Title level={5}>macOS</Title>
        <Paragraph style={{ marginBottom: 'var(--gw-sp-16)' }}>
          双击 <Text code>.crt</Text> → 钥匙串访问 → 「系统」→ 找到{' '}
          <Text strong>Gateway Internal CA</Text> → 双击 → 「信任」→ 「始终信任」。
        </Paragraph>

        <Title level={5}>Linux</Title>
        <pre style={{ ...PRE, maxHeight: 200 }}>
          {`sudo cp gateway-internal-ca.crt /usr/local/share/ca-certificates/gateway-internal-ca.crt\nsudo update-ca-certificates`}
        </pre>

        <Title level={5}>Firefox（不走系统证书库）</Title>
        <Paragraph>
          设置 → 隐私与安全 → 证书 → 查看证书 → 「证书颁发机构」→ 导入 → 选文件 → 勾「信任此 CA 以标识网站」。
        </Paragraph>
      </Card>

      <Card title="命令行客户端">
        <Paragraph>装完 CA 后以下都不需要额外参数；未装时按需指定：</Paragraph>
        <pre style={{ ...PRE, maxHeight: 240 }}>
          {`# curl（--cacert 仅未装 CA 时需要；/health 免 key，装完 CA 直接访问即可）\ncurl --cacert gateway-internal-ca.crt ${origin}/health\n\n# Python / requests\nREQUESTS_CA_BUNDLE=gateway-internal-ca.crt\n\n# Node\nNODE_EXTRA_CA_CERTS=gateway-internal-ca.crt\n\n# Go (codex / reqwest)\nSSL_CERT_FILE=gateway-internal-ca.crt`}
        </pre>
      </Card>

      <Card title="常见问题">
        <Paragraph style={{ marginBottom: 'var(--gw-sp-8)' }}>
          <Text strong>装了还是提示不安全</Text>：关闭所有浏览器窗口重开；或确认装到了「受信任的根」而非「中间」。
        </Paragraph>
        <Paragraph style={{ marginBottom: 'var(--gw-sp-8)' }}>
          <Text strong>curl 报 CRYPT_E_NO_REVOCATION_CHECK</Text>：已修复（网关提供 <Text code>/ca.crl</Text>），
          升级到最新；老版本用 <Text code>curl --ssl-no-revoke</Text>。
        </Paragraph>
        <Paragraph>
          <Text strong>证书过期 / 换 IP</Text>：联系管理员重签（SAN 列表见 <Text code>agents/deploy.md</Text>）。
        </Paragraph>
      </Card>
    </Space>
  );
}
