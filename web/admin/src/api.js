// 管理台共享工具：fetch 样板 / 错误文案 / 复制兜底 / 面板时间窗
// 注意：管理台走 HTTP（局域网 IP）= 非安全上下文，无 navigator.clipboard，复制必须带 execCommand 兜底。
import { message } from 'antd';

export const fetchJson = async (path, opts = {}) => {
  // 浏览器 fetch 发 string body 默认 Content-Type: text/plain，FastAPI 遇到非 JSON
  // content-type 不解析 body → 422 model_attributes_type（管理台所有写操作全灭）。
  // 这里统一补 JSON 头（调用方显式传了则不动；无 FormData 调用，无 multipart 冲突）。
  const headers = { ...(opts.headers || {}) };
  if (opts.body !== undefined
      && !Object.keys(headers).some((k) => k.toLowerCase() === 'content-type')) {
    headers['Content-Type'] = 'application/json';
  }
  const r = await fetch(path, { ...opts, headers });
  if (r.status === 401 || r.status === 302) {
    window.location.href = '/admin/login?next=/admin/app';
    throw new Error('未登录');
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail) || `HTTP ${r.status}`);
  return body;
};

export const api = (path, opts) => fetchJson(`/admin/api${path}`, opts);

export const errText = (e) => (Array.isArray(e) ? e.map(x => `${(x.loc || []).join('.')}: ${x.msg}`).join('; ') : String(e.message || e));

export const copyText = async (t, okMsg = '已复制', failMsg = '复制失败') => {
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(t);
    } else {
      const ta = document.createElement('textarea');
      ta.value = t;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      ta.style.top = '-9999px';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      if (!ok) throw new Error('copy failed');
    }
    message.success(okMsg);
  } catch {
    message.error(failMsg);
  }
};

export const RANGES = [
  { hours: 24, label: '最近 24 小时' },
  { hours: 168, label: '最近 7 天' },
  { hours: 720, label: '最近 30 天' },
];
