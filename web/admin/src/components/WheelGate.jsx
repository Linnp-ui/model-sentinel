import { useEffect, useRef } from 'react';

// 嵌套滚动容器滚轮让位页面：滚轮落在容器内仍可滚的元素上时 preventDefault + 手动滚页面
// （容器仍可拖自己的滚动条翻）。React onWheel 是 passive 拦不住默认滚动，须原生非 passive。
// 只用于页面级流动的嵌套 scroller；Modal 内不套（弹窗锁页面滚动，滚内部内容才是对的）
export default function WheelGate({ children, style }) {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const onWheel = (e) => {
      let t = e.target;
      while (t && t !== el && t.nodeType === 1) {
        if (t.scrollHeight > t.clientHeight) {
          e.preventDefault();
          window.scrollBy({ top: e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY });
          return;
        }
        t = t.parentElement;
      }
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, []);
  return <div ref={ref} style={style}>{children}</div>;
}
