const _bjFmt = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai',
  dateStyle: 'short', timeStyle: 'medium', hour12: false });

export const bj = (ts) => {
  const t = Date.parse(String(ts).replace(' ', 'T').slice(0, 19) + 'Z');
  if (Number.isNaN(t)) return String(ts || '');
  return _bjFmt.format(t);
};

export const avg = (vs) => {
  const v = vs.filter((x) => x != null);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
};
