import ReactDOM from 'react-dom/client';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import App from './App.jsx';
import './theme.css';

ReactDOM.createRoot(document.getElementById('root')).render(
  <ConfigProvider locale={zhCN} theme={{
    token: {
      colorPrimary: '#2f54eb',
      colorLink: '#2f54eb',            // 与主色一致：antd 默认 #1677ff 在白底 4.1:1 不达 WCAG AA
      colorTextDescription: 'rgba(0, 0, 0, 0.6)',  // 默认 0.45 在白底约 3.35:1（说明文字/统计标题/菜单分组），不达 AA
      colorSuccess: '#389e0d',         // 色阶亮色只留给图形；文本/图标用深一档（3.46:1）
      colorWarning: '#d46b08',
      colorError: '#cf1322',
      colorBgLayout: '#f5f6f8',
      fontSize: 14,
      fontSizeSM: 13,
      borderRadius: 8,
      motionDurationMid: '0.2s',
    },
    components: {
      Table: { headerBg: '#FAFAFA' },
      Card: { paddingLG: 16 },
    },
  }}>
    <App />
  </ConfigProvider>
);
