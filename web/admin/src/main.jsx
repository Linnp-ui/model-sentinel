import ReactDOM from 'react-dom/client';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import App from './App.jsx';
import './theme.css';

ReactDOM.createRoot(document.getElementById('root')).render(
  <ConfigProvider locale={zhCN} theme={{
    token: {
      colorPrimary: '#2f54eb',
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
