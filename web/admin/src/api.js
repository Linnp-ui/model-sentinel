export const api = async (path, opts = {}) => {
  const { headers, ...rest } = opts;
  const r = await fetch(`/admin/api${path}`, {
    ...rest,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
  if (r.status === 401 || r.status === 302) {
    window.location.href = '/admin/login?next=/admin/app';
    throw new Error('未登录');
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail) || `HTTP ${r.status}`);
  return body;
};

import StatsPage from './StatsPage.jsx';
import MetricsPage from './MetricsPage.jsx';
import { ruleZh, RULE_ZH } from './ruleNames.jsx';

export const errText = (e) => (Array.isArray(e) ? e.map(x => `${(x.loc || []).join('.')}: ${x.msg}`).join('; ') : String(e.message || e));
