// Arcaidia Effector — каталог эффектов: заводские пресеты + библиотека пользователя
// Переключение НЕ теряет правки: рабочие копии живут в сессии (AFX.openDoc),
// несохранённые эффекты помечены точкой; в каталог пишет только кнопка "Сохранить".
(function () {
'use strict';
const localStorage = window.FantasyEffects.storage;
const AFX = window.AFX;
const D = AFX.Dom, h = D.h, L = AFX.t;

const Cat = AFX.Catalog = {};
let bodyEl = null;
let userItems = [];      // [{file?, id?, name, data?, thumb?}]
let sourceRecords = {};
try { sourceRecords = JSON.parse(localStorage.getItem('shared-effect-sources') || '{}'); } catch (_) {}
function viewId(item) { return item.is_own === false ? 'shared:' + item.owner_key + ':' + (item.id || item.file) : item.id; }
function rememberSource(id, source) {
    sourceRecords[id] = source;
    try { localStorage.setItem('shared-effect-sources', JSON.stringify(sourceRecords)); } catch (_) {}
}

Cat.init = function () {
    bodyEl = document.getElementById('catalog-body');
    const btns = document.getElementById('catalog-btns');

    const bNew = h('span', { cls: 'mini-btn', tip: L('New empty effect (current edits stay in the session)') }, D.icon('plus'), L('New'));
    bNew.addEventListener('click', function () {
        AFX.openDoc(AFX.Model.newDoc());
    });
    const bImp = h('span', { cls: 'mini-btn', tip: L('Open an effect from a .json file (you can also drop the file into the window)') }, L('Import'));
    const fileInp = h('input', { type: 'file', accept: '.json', style: 'display:none;' });
    bImp.addEventListener('click', function () { fileInp.click(); });
    fileInp.addEventListener('change', function () {
        if (fileInp.files[0]) Cat.importJsonFile(fileInp.files[0]);
        fileInp.value = '';
    });
    const bExp = h('span', { cls: 'mini-btn', tip: L('Download the current effect as a .json file with its textures embedded') }, L('Export'));
    bExp.addEventListener('click', function () {
        const file = AFX.Model.effectFile(AFX.state.doc);
        AFX.downloadText(AFX.safeFileName(AFX.state.doc.name) + '.json', JSON.stringify(file));
    });
    btns.appendChild(bNew);
    btns.appendChild(bImp);
    btns.appendChild(bExp);
    btns.appendChild(fileInp);

    AFX.on('doc', Cat.rebuild);
    AFX.on('dirty', Cat.rebuild);
    Cat.detectServer(function () {
        Cat.refresh();
    });
};

Cat.detectServer = function (done) {
    // Private deployment always uses the authenticated personal library.
    fetch('api/list', { cache: 'no-store' }).then(r => r.json())
        .then(list => {
            if (!Array.isArray(list)) throw new Error('Invalid library response');
            userItems = list;
        })
        .catch(() => {
            userItems = [];
            AFX.setStatus(L('Personal library is unavailable. Your draft stays in this browser.'));
        }).finally(done);
};

Cat.refresh = function () {
    fetch('api/list', { cache: 'no-store' }).then(r => r.json()).then(list => {
        if (!Array.isArray(list)) throw new Error('Invalid library response');
        userItems = list;
        Cat.rebuild();
    }).catch(() => {
        AFX.setStatus(L('Personal library is unavailable. Your draft stays in this browser.'));
        Cat.rebuild();
    });
};

Cat.rebuild = function () {
    if (!bodyEl) return;
    bodyEl.innerHTML = '';

    // черновики сессии: рабочие копии без строки в каталоге (новые/импортированные)
    const knownIds = new Set();
    userItems.forEach(item => {
        const id = viewId(item) || (item.data && item.data.doc && item.data.doc.id);
        if (id) knownIds.add(id);
    });
    AFX.Factory.forEach(p => knownIds.add('factory:' + p.id));
    const drafts = (AFX.wsList ? AFX.wsList() : []).filter(d => !knownIds.has(d.id));
    if (drafts.length) {
        bodyEl.appendChild(h('div', { cls: 'cat-group', text: L('Unsaved') }));
        drafts.forEach(d => bodyEl.appendChild(draftRow(d)));
    }

    bodyEl.appendChild(h('div', { cls: 'cat-group', text: '我的与共享特效' }));
    if (!userItems.length) {
        bodyEl.appendChild(h('div', { cls: 'catalog-empty', text: L('Empty. The Save button adds the current effect.') }));
    }
    userItems.forEach(item => bodyEl.appendChild(userRow(item)));

    bodyEl.appendChild(h('div', { cls: 'cat-group', text: L('Factory') }));
    AFX.Factory.forEach(p => bodyEl.appendChild(factoryRow(p)));
};

// живая миниатюра черновика (без кэша: док меняется)
function liveThumb(id) {
    const c = h('canvas', { width: 42, height: 42 });
    setTimeout(function () {
        try {
            const doc = AFX.wsGetDoc(id);
            if (!doc) return;
            const img = AFX.Engine.renderThumb(doc, 42, doc.comp.dur * 0.35);
            c.getContext('2d').drawImage(img, 0, 0);
        } catch (e) {}
    }, 30);
    return c;
}

function draftRow(d) {
    const row = h('div', {
        cls: 'cat-row' + (isCurrent(d.id) ? ' current' : ''), tipHead: d.name,
        tip: L('Unsaved draft that lives only in the session: Save puts it into the catalog')
    });
    row.appendChild(liveThumb(d.id));
    row.appendChild(nameSpan(d.name, d.id, L('not saved')));
    const discard = function () {
        if (!confirm(L('Discard unsaved effect') + ' "' + d.name + '"?')) return;
        AFX.wsDelete(d.id);
        Cat.rebuild();
    };
    if (!d.current) {
        const del = h('span', { cls: 'mini-btn del', tip: L('Discard this unsaved draft from the session — its edits are lost') }, D.icon('del'));
        del.addEventListener('click', function (e) {
            e.stopPropagation();
            discard();
        });
        row.appendChild(del);
    }
    row.addEventListener('click', function () {
        const doc = AFX.wsGetDoc(d.id);
        if (doc) AFX.openDoc(doc);
    });
    row.addEventListener('contextmenu', function (e) {
        e.preventDefault();
        const items = [{ label: L('Open'), click: function () { const doc = AFX.wsGetDoc(d.id); if (doc) AFX.openDoc(doc); } }];
        if (!d.current) {
            items.push({ label: L('Remove from session'), danger: true, click: discard });
        }
        D.ctxMenu(e.clientX, e.clientY, items);
    });
    return row;
}

const thumbCache = new Map();
function thumbCanvas(key, makeDoc, thumbT) {
    let c = thumbCache.get(key);
    if (c) return c;
    c = h('canvas', { width: 42, height: 42 });
    thumbCache.set(key, c);
    setTimeout(function () {
        try {
            const doc = makeDoc();
            if (!doc) return;
            const t = thumbT != null ? thumbT : doc.comp.dur * 0.35;
            const img = AFX.Engine.renderThumb(doc, 42, t);
            c.getContext('2d').drawImage(img, 0, 0);
        } catch (e) { console.warn('thumb', e); }
    }, 30);
    return c;
}

function nameSpan(name, id, sub) {
    const nm = h('span', { cls: 'cname' });
    const line = h('span', { style: 'display:flex;align-items:center;gap:5px;' });
    if (id != null && AFX.wsDirty(id)) {
        line.appendChild(h('span', { cls: 'dirty-dot', tip: L('Unsaved edits (kept in the session)') }));
    }
    line.appendChild(h('span', { text: name }));
    nm.appendChild(line);
    if (sub) nm.appendChild(h('span', { cls: 'csub', text: sub }));
    return nm;
}

function isCurrent(id, name) {
    const d = AFX.state.doc;
    if (!d) return false;
    if (id != null) return d.id === id;
    return d.name === name;
}

function factoryRow(p) {
    const fid = 'factory:' + p.id;
    const row = h('div', {
        cls: 'cat-row' + (isCurrent(fid) ? ' current' : ''), tipHead: p.name,
        tip: L('Factory preset: click to open a working copy, right-click to reset your edits')
    });
    row.appendChild(thumbCanvas('factory:' + p.id, () => p.build(), p.thumbT));
    row.appendChild(nameSpan(p.name, fid, L('factory preset')));
    const open = function (fresh) {
        const d = p.build();
        d.id = fid;
        AFX.openDoc(d, { fresh: fresh });
    };
    row.addEventListener('click', function () { open(false); });
    row.addEventListener('contextmenu', function (e) {
        e.preventDefault();
        const items = [{ label: L('Open'), click: function () { open(false); } }];
        if (AFX.wsDirty(fid)) {
            items.push({ label: L('Reset edits (to factory)'), danger: true, click: function () { open(true); } });
        }
        D.ctxMenu(e.clientX, e.clientY, items);
    });
    return row;
}

function userRow(item) {
    const itemId = viewId(item) || null;
    const row = h('div', {
        cls: 'cat-row' + (isCurrent(itemId, item.name) ? ' current' : ''), tipHead: item.name,
        tip: L('Saved effect: click to open it, right-click to reset your edits or delete it')
    });
    if (item.thumb) {
        row.appendChild(h('img', { src: item.thumb }));
    } else {
        row.appendChild(h('canvas', { width: 42, height: 42 }));
    }
    row.appendChild(nameSpan(item.name, itemId, (item.owner_name || '') + (item.is_own ? ' · 我的' : item.editable ? ' · 可管理' : ' · 使用时复制')));

    const del = h('span', { cls: 'mini-btn del', tip: L('Delete this saved effect from the catalog; with the server its file is removed too') }, D.icon('del'));
    del.addEventListener('click', function (e) {
        e.stopPropagation();
        removeItem(item);
    });
    if (item.editable !== false) row.appendChild(del);

    const open = function (fresh) {
        loadItem(item, function (doc) {
            if (doc) AFX.openDoc(doc, { fresh: fresh });
            else alert(L('Failed to read the effect.'));
        });
    };
    row.addEventListener('click', function () { open(false); });
    row.addEventListener('contextmenu', function (e) {
        e.preventDefault();
        const items = [{ label: L('Open'), click: function () { open(false); } }];
        if (itemId != null && AFX.wsDirty(itemId)) {
            items.push({ label: L('Reset edits (to saved)'), danger: true, click: function () { open(true); } });
        }
        if (item.editable !== false) {
            items.push({ sep: true });
            items.push({ label: L('Delete from catalog'), danger: true, click: function () { removeItem(item); } });
        }
        items.push({ label: '复制为我的特效', click: function () { loadItem(item, doc => {
            if (!doc) return;
            delete sourceRecords[doc.id];
            doc.id = crypto.randomUUID(); doc.name += '（副本）'; AFX.openDoc(doc);
        }, true); } });
        D.ctxMenu(e.clientX, e.clientY, items);
    });
    return row;
}

function loadItem(item, cb, copy = false) {
    const query = new URLSearchParams({ f: item.file });
    if (item.owner_key) query.set('owner_key', item.owner_key);
    fetch('api/effect?' + query, { cache: 'no-store' })
        .then(async r => ({ file: await r.json(), revision: (r.headers.get('etag') || '').replaceAll('"', '') }))
        .then(({file, revision}) => AFX.Model.loadEffectFile(file, doc => {
            if (!doc) return cb(null);
            if (item.editable === false || copy) {
                doc.id = crypto.randomUUID();
                if (!copy) doc.name += '（副本）';
                AFX.setStatus('已打开共享特效的工作副本；保存后归当前账号。');
            } else {
                const original_id = doc.id;
                doc.id = viewId(item) || doc.id;
                rememberSource(doc.id, { owner_key: item.owner_key, file: item.file, revision, original_id });
            }
            cb(doc);
        }))
        .catch(() => cb(null));
}

function removeItem(item) {
    if (item.editable === false) return;
    if (!confirm(L('Delete effect') + ' "' + item.name + '"?')) return;
    const itemId = item.id || (item.data && item.data.doc && item.data.doc.id) || null;
    fetch('api/delete', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: item.file, owner_key: item.owner_key })
    }).then(r => r.json()).then(res => {
        if (!res || res.ok !== true) throw new Error('Delete was not confirmed');
        if (itemId) AFX.wsDelete(itemId);
        Cat.refresh();
    }).catch(() => AFX.setStatus(L('Delete failed. Your effect was not removed.')));
}

// сохранить текущий эффект в каталог; done(ok) — по факту записи (fs-режим асинхронный)
Cat.saveCurrent = function (done) {
    const doc = AFX.state.doc;
    const file = AFX.Model.effectFile(doc);
    const source = sourceRecords[doc.id];
    if (source?.original_id) file.doc.id = source.original_id;
    const ok = function (v) { if (done) done(v); };
    let thumb = null;
    try { thumb = AFX.Engine.renderThumb(doc, 42).toDataURL('image/png'); } catch (e) {}

    fetch('api/save', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: source ? source.file.slice(0, -5) : doc.name, data: file, thumb: thumb,
            ...(source ? { owner_key: source.owner_key, expected_sha256: source.revision } : { create_only: true }) })
    }).then(r => r.json()).then(res => {
        if (!res || res.ok !== true || typeof res.file !== 'string') throw new Error('Save was not confirmed');
        rememberSource(doc.id, { owner_key: res.owner_key, file: res.file, revision: res.revision, original_id: file.doc.id });
        AFX.setStatus(L('Saved') + ': ' + res.file);
        ok(true);
        Cat.refresh();
    }).catch(() => { AFX.setStatus(L('Server save error')); ok(false); });
};

Cat.importJsonFile = function (f) {
    const reader = new FileReader();
    reader.onload = function () {
        try {
            const file = JSON.parse(reader.result);
            AFX.Model.loadEffectFile(file, function (doc) {
                if (doc) AFX.openDoc(doc);
                else alert(L('Not an Arcaidia Effector file.'));
            });
        } catch (e) {
            alert(L('Failed to parse JSON.'));
        }
    };
    reader.readAsText(f);
};
})();
