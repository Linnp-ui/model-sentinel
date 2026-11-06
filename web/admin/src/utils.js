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

/** 全站表格统一最小宽度（px）：所有卡片内表格都带 scroll={{x: TABLE_X}}，
    antd 会把 <table> 定为 width=TABLE_X、min-width:100% ⇒ 卡片宽度 ≥ TABLE_X 时铺满不横滚，
    窄于它时所有表**同一时刻**出现表内横滚（触发点不再随列数/单元格内容漂移）。
    低于该值仍可读的列宽靠百分比分配；按钮/Tag 列用 px 定宽（见各表 columns）。
    shortcut: 只覆盖窗口宽度（外层卡全宽场景）；半宽卡内的表（总览健康检查）不传 scroll。 */
export const TABLE_X = 1000;

/** 表头冻结只在 ≥1200px 开：rc-table 的 sticky 把表头/表体拆成两个容器，
    **设了 scroll.x 才会联动横向滚动**（全站表格已统一传 scroll={{x: TABLE_X}}）。 */

const _wideMq = typeof window === 'undefined' ? null : window.matchMedia('(min-width: 1200px)');
export const wideSticky = () => (_wideMq ? _wideMq.matches : true);

// AI 生成累积合并（RulesPage「换一组」，只增不减）。返回 {rows, added, skipped:[中文短语]}。
// 关键词类(contains/in/not_in)：同 (字段,方式) 行取并集追加，全重复计入 skipped；
// regex：已有行同值=重复跳过，异值=新行追加（builder/build-when-group/引擎全链路支持
//   同字段多行；此前同键 regex 静默丢弃 ⇒ AI 新正则永远加不进）；
// bool：已有行保留现有值（==false 行进 any 组语义反，不加）；
// 8 行=构造器手动加行与 build-when-group max_length 的共同上限：超限条件不入 builder，
//   否则同步 422 后 when_json 停旧值，保存会把"已加条件"静默丢掉。
export const mergeAiRows = (cur, next, choices = []) => {
  const kw = (k) => ['contains', 'in', 'not_in'].includes(String(k || '').split('|')[1]);
  const labelOf = (k) => {
    const [ct, mt] = String(k || '').split('|');
    const c = (choices || []).find((x) => x.content_type === ct && x.match_type === mt);
    return c ? c.label.replace(' · ', '·') : (k || '该条件');
  };
  const wordsOf = (v) => String(v).split(',').map((x) => x.trim()).filter(Boolean);
  const merged = cur.map((r) => ({ ...r }));
  let added = 0;
  const skipped = [];
  for (const r of next) {
    const i = merged.findIndex((m) => m.key === r.key);
    const mt = String(r.key || '').split('|')[1];
    const sameValue = merged.some((m) => m.key === r.key && String(m.value || '').trim() === String(r.value).trim());
    if (i < 0 || (mt === 'regex' && !sameValue)) {
      if (merged.length >= 8) { skipped.push(`${labelOf(r.key)} 超 8 行上限`); continue; }
      merged.push(r);
      added += kw(r.key) ? wordsOf(r.value).length : 1;
      continue;
    }
    if (kw(r.key)) {
      const old = wordsOf(merged[i].value);
      const news = wordsOf(r.value).filter((x) => !old.includes(x));
      if (news.length) { merged[i] = { ...merged[i], value: [...old, ...news].join(', ') }; added += news.length; }
      else skipped.push(`${labelOf(r.key)} ${wordsOf(r.value).length} 词与现有重复`);
    } else if (mt === 'regex') {
      skipped.push(`${labelOf(r.key)} 与现有行相同`);
    } else {
      skipped.push(`${labelOf(r.key)} 保留现有值`);
    }
  }
  return { rows: merged, added, skipped };
};

// 页内多页签共用一条 URL：只改自己负责的参数，保留其余（审计/明细两页签的筛选互不覆盖）。
// 返回新 URLSearchParams 前先改，replaceState 不触发 hashchange（同页内写态的既有口径）。
export const patchHashQuery = (mutate, path = '/audit') => {
  const p = new URLSearchParams(window.location.hash.split('?')[1] || '');
  mutate(p);
  const qs = p.toString();
  history.replaceState(null, '', '#' + path + (qs ? '?' + qs : ''));
};
