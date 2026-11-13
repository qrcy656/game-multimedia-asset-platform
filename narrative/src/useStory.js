import { useCallback, useEffect, useRef, useState } from 'react';
import { api, cloneDocument } from './api';

export default function useStory(onSaved) {
  const [document, setDocument] = useState(null);
  const [envelope, setEnvelope] = useState(null);
  const [status, setStatus] = useState('saved');
  const [error, setError] = useState(null);
  const [historyVersion, bumpHistory] = useState(0);
  const current = useRef({ doc: null, id: '', version: 0, generation: 0, savedGeneration: 0 });
  const pending = useRef(null);
  const history = useRef({ past: [], future: [], lastKey: '', lastTime: 0 });
  const errorRef = useRef(null);
  const onSavedRef = useRef(onSaved);
  onSavedRef.current = onSaved;

  const load = useCallback((env) => {
    current.current = { doc: env?.document || null, id: env?.id || '', version: env?.version || 0, generation: 0, savedGeneration: 0, readOnly: env?.read_only === true };
    history.current = { past: [], future: [], lastKey: '', lastTime: 0 };
    setEnvelope(env); setDocument(env?.document || null); setStatus('saved'); setError(null); errorRef.current = null; bumpHistory(x => x + 1);
  }, []);

  const addHistory = useCallback((oldDoc, key = '') => {
    if (current.current.readOnly) return;
    const h = history.current;
    if (!key || key !== h.lastKey || Date.now() - h.lastTime > 800) h.past = [...h.past.slice(-39), oldDoc];
    h.future = []; h.lastKey = key; h.lastTime = Date.now(); bumpHistory(x => x + 1);
  }, []);

  const edit = useCallback((recipe, key = '', record = true) => {
    const oldDoc = current.current.doc;
    if (!oldDoc || current.current.readOnly) return;
    const next = cloneDocument(oldDoc);
    recipe(next);
    if (record) addHistory(oldDoc, key);
    current.current.doc = next; current.current.generation += 1;
    if (errorRef.current && errorRef.current.status !== 409) { errorRef.current = null; setError(null); }
    setDocument(next); setStatus(errorRef.current?.status === 409 ? 'conflict' : errorRef.current ? 'error' : 'dirty');
  }, [addHistory]);

  const save = useCallback(async (retry = true) => {
    if (!current.current.doc || current.current.readOnly) return true;
    if (pending.current) return pending.current;
    if (current.current.generation === current.current.savedGeneration) return true;
    if (errorRef.current?.status === 409 || (!retry && errorRef.current)) return false;
    errorRef.current = null; setError(null);
    const task = (async () => {
      try {
        while (current.current.generation !== current.current.savedGeneration) {
          const c = current.current;
          const snapshot = { id: c.id, version: c.version, generation: c.generation, doc: c.doc };
          setStatus('saving');
          const env = await api(`/api/projects/${encodeURIComponent(snapshot.id)}`, { method: 'PUT', body: JSON.stringify({ version: snapshot.version, document: snapshot.doc }) });
          if (current.current.id !== snapshot.id) throw new Error('项目切换过程中保存被中断，请导出当前编辑内容后重新载入。');
          current.current.version = env.version;
          current.current.savedGeneration = snapshot.generation;
          setEnvelope(env);
          onSavedRef.current?.(env);
        }
        setStatus('saved');
        return true;
      } catch (e) {
        errorRef.current = e; setError(e); setStatus(e.status === 409 ? 'conflict' : 'error');
        return false;
      } finally { pending.current = null; }
    })();
    pending.current = task;
    return task;
  }, []);

  useEffect(() => {
    if (!document || current.current.generation === current.current.savedGeneration || errorRef.current) return;
    const timer = setTimeout(() => save(false), 900);
    return () => clearTimeout(timer);
  }, [document, save]);

  useEffect(() => {
    const handler = (e) => {
      if (current.current.generation !== current.current.savedGeneration) { e.preventDefault(); e.returnValue = ''; }
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, []);

  const travel = useCallback((direction) => {
    if (current.current.readOnly) return;
    const h = history.current;
    const source = direction === 'undo' ? h.past : h.future;
    if (!source.length) return;
    const target = source.pop();
    (direction === 'undo' ? h.future : h.past).push(current.current.doc);
    h.lastKey = ''; current.current.doc = target; current.current.generation += 1;
    if (errorRef.current && errorRef.current.status !== 409) { errorRef.current = null; setError(null); }
    setDocument(target); setStatus(errorRef.current?.status === 409 ? 'conflict' : errorRef.current ? 'error' : 'dirty'); bumpHistory(x => x + 1);
  }, []);

  return { document, envelope, status, error, current, load, edit, save, addHistory, undo: () => travel('undo'), redo: () => travel('redo'), canUndo: !current.current.readOnly && history.current.past.length > 0, canRedo: !current.current.readOnly && history.current.future.length > 0, historyVersion };
}
