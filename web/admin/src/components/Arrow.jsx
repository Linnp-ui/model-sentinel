// 统一右箭头：避免文本字形在不同字体/字号下垂直偏移。
export default function Arrow({ size }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
      verticalAlign: 'middle', lineHeight: 1, flexShrink: 0,
      ...(size ? { fontSize: size } : {}) }}>
      <svg width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
        style={{ display: 'block' }}>
        <path d="M5 12h14M12 5l7 7-7 7" />
      </svg>
    </span>
  );
}
