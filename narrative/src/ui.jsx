import React, { useEffect, useRef } from 'react';

const paths = {
  book: <><path d="M4 4h6a3 3 0 0 1 3 3v14a4 4 0 0 0-4-3H4z"/><path d="M20 4h-4a3 3 0 0 0-3 3v14a4 4 0 0 1 4-3h3z"/></>,
  back: <><path d="m10 5-7 7 7 7M3 12h18"/></>,
  plus: <path d="M12 4v16M4 12h16"/>,
  close: <path d="m6 6 12 12M6 18 18 6"/>,
  save: <><path d="M4 3h13l4 4v14H3V3z"/><path d="M7 3v7h10V3M7 21v-7h10v7"/></>,
  upload: <><path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/></>,
  download: <><path d="M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4"/></>,
  trash: <><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/></>,
  scene: <><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M7 9h10M7 13h7"/></>,
  dialogue: <><path d="M4 4h16v12H9l-5 5z"/><path d="M8 8h8M8 12h5"/></>,
  branch: <><path d="m12 2 10 10-10 10L2 12z"/><path d="M12 7v5m0 4h.01"/></>,
  note: <><path d="M4 3h16v13l-5 5H4zM15 21v-5h5"/><path d="M8 8h8M8 12h6"/></>,
  boards: <><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></>,
  substory: <><rect x="3" y="3" width="15" height="15" rx="3"/><path d="M8 8h13v13H8zM12 12h5M12 16h5"/></>,
  people: <><circle cx="9" cy="8" r="4"/><path d="M2 21v-2a7 7 0 0 1 14 0v2M16 4a4 4 0 0 1 0 8m3 3a6 6 0 0 1 3 6"/></>,
  media: <><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 6-6 5 5 3-3 4 4"/></>,
  undo: <><path d="m8 4-5 5 5 5M3 9h10a7 7 0 0 1 0 14"/></>,
  redo: <><path d="m16 4 5 5-5 5m5-5H11a7 7 0 0 0 0 14"/></>,
  check: <path d="m4 12 5 5L20 6"/>,
  chevron: <path d="m9 5 7 7-7 7"/>,
  link: <><path d="m9 15 6-6M8 17l-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m2-1 2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0"/></>,
  spark: <><path d="m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3z"/></>,
  info: <><circle cx="12" cy="12" r="9"/><path d="M12 10v7m0-10v.01"/></>,
};
export function Icon({ name, size = 18 }) { return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.scene}</svg>; }
export function IconButton({ name, title, children, ...props }) { return <button type="button" className="icon-button" title={title} aria-label={title} {...props}><Icon name={name}/>{children}</button>; }
export function Field({ label, hint, children }) { return <label className="field"><span className="field-label">{label}</span>{children}{hint && <small>{hint}</small>}</label>; }
export function Empty({ icon = 'scene', title, children }) { return <div className="empty-small"><Icon name={icon} size={28}/><strong>{title}</strong><p>{children}</p></div>; }
export function Modal({ title, children, onClose, wide = false }) {
  const ref = useRef(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const old = document.activeElement;
    ref.current?.focus();
    const key = e => {
      if (e.key === 'Escape') { e.stopPropagation(); closeRef.current(); }
      if (e.key === 'Tab') {
        const items = [...ref.current.querySelectorAll('button:not(:disabled),a[href],input,select,textarea,[tabindex="0"]')].filter(x => x.offsetParent !== null);
        if (!items.length) return;
        if (e.shiftKey && document.activeElement === items[0]) { e.preventDefault(); items.at(-1).focus(); }
        else if (!e.shiftKey && document.activeElement === items.at(-1)) { e.preventDefault(); items[0].focus(); }
      }
    };
    document.addEventListener('keydown', key);
    return () => { document.removeEventListener('keydown', key); old?.focus?.(); };
  }, []);
  return <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><section ref={ref} tabIndex={-1} className={`modal ${wide ? 'wide' : ''}`} role="dialog" aria-modal="true" aria-label={title}><header><h2>{title}</h2><IconButton name="close" title="关闭弹窗" onClick={onClose}/></header>{children}</section></div>;
}
export const types = { scene: { label: '场景', color: '#5dcbb8' }, dialogue: { label: '对话', color: '#80aaff' }, branch: { label: '剧情分支', color: '#c4a4ff' }, note: { label: '便签', color: '#e5bb6e' }, substory: { label: '子剧情', color: '#b1bb81' } };
