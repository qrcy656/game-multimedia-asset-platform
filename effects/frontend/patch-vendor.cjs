// Reproducible, narrowly scoped patch against the pinned upstream checkout.
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../vendor');
function edit(file, action) {
  const dest = path.join(root, file);
  const original = fs.readFileSync(dest, 'utf8');
  let text = original.replace(/\r\n/g, '\n');
  function replace(before, after) {
    if (!text.includes(before)) throw new Error(file + ': patch anchor absent: ' + before.slice(0, 60));
    text = text.replace(before, after);
  }
  action(replace);
  fs.writeFileSync(dest, original.includes('\r\n') ? text.replace(/\n/g, '\r\n') : text);
}
edit('index.html', r => {
  r('<title>Arcaidia Effector</title>', '<title>特效制作 · Arcaidia Effector</title>');
  r('<link rel="stylesheet" href="css/editor.css">', '<link rel="stylesheet" href="css/editor.css">\n<link rel="stylesheet" href="integration.css">\n<script src="/community.js"></script>\n<script src="fantasy-session.js"></script>\n<script src="integration.js"></script>');
});
for (const file of ['js/ui/app.js', 'js/ui/catalog.js', 'js/core/i18n.js']) {
  edit(file, r => r("'use strict';", "'use strict';\nconst localStorage = window.FantasyEffects.storage;"));
}
edit('js/ui/catalog.js', r => {
  r('let fsMode = false;      // сервер доступен\n', '');
  r("const LS_KEY = 'afx.library';\n", '');
  r(`Cat.detectServer = function (done) {
    fetch('api/list', { cache: 'no-store' }).then(r => r.ok ? r.json() : Promise.reject())
        .then(list => { fsMode = true; userItems = list; done(); })
        .catch(() => {
            fsMode = false;
            try { userItems = JSON.parse(localStorage.getItem(LS_KEY) || '[]'); }
            catch (e) { userItems = []; }
            done();
        });
};`, `Cat.detectServer = function (done) {
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
};`);
  r(`Cat.refresh = function () {
    if (fsMode) {
        fetch('api/list', { cache: 'no-store' }).then(r => r.json()).then(list => {
            userItems = list;
            Cat.rebuild();
        }).catch(Cat.rebuild);
    } else {
        Cat.rebuild();
    }
};`, `Cat.refresh = function () {
    fetch('api/list', { cache: 'no-store' }).then(r => r.json()).then(list => {
        if (!Array.isArray(list)) throw new Error('Invalid library response');
        userItems = list;
        Cat.rebuild();
    }).catch(() => {
        AFX.setStatus(L('Personal library is unavailable. Your draft stays in this browser.'));
        Cat.rebuild();
    });
};`);
  r("fsMode ? L('Mine') + ' (library/)' : L('Mine') + ' (localStorage)'", "L('Personal library')");
  r(`function loadItem(item, cb) {
    if (fsMode) {
        fetch('api/effect?f=' + encodeURIComponent(item.file), { cache: 'no-store' })
            .then(r => r.json())
            .then(file => AFX.Model.loadEffectFile(file, cb))
            .catch(() => cb(null));
    } else {
        AFX.Model.loadEffectFile(item.data, cb);
    }
}`, `function loadItem(item, cb) {
    fetch('api/effect?f=' + encodeURIComponent(item.file), { cache: 'no-store' })
        .then(r => r.json())
        .then(file => AFX.Model.loadEffectFile(file, cb))
        .catch(() => cb(null));
}`);
  r(`    if (itemId) AFX.wsDelete(itemId);
    if (fsMode) {
        fetch('api/delete', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file: item.file })
        }).then(() => Cat.refresh());
    } else {
        userItems = userItems.filter(x => x !== item);
        saveLS();
        Cat.rebuild();
    }`, `    fetch('api/delete', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: item.file })
    }).then(r => r.json()).then(res => {
        if (!res || res.ok !== true) throw new Error('Delete was not confirmed');
        if (itemId) AFX.wsDelete(itemId);
        Cat.refresh();
    }).catch(() => AFX.setStatus(L('Delete failed. Your effect was not removed.')));`);
  r(`function saveLS() {
    try {
        localStorage.setItem(LS_KEY, JSON.stringify(userItems));
        return true;
    } catch (e) {
        alert(L('localStorage is full (likely textures). Use run.bat: the server stores effects as files.'));
        return false;
    }
}

`, '');
  r(`    if (fsMode) {
        fetch('api/save', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: doc.name, data: file, thumb: thumb })
        }).then(r => r.json()).then(res => {
            AFX.setStatus(L('Saved') + ': library/' + res.file);
            ok(true);
            Cat.refresh();
        }).catch(() => { AFX.setStatus(L('Server save error')); ok(false); });
    } else {
        const existing = userItems.find(x => (x.data && x.data.doc && x.data.doc.id) === doc.id) ||
            userItems.find(x => x.name === doc.name);
        const item = existing || {};
        item.name = doc.name;
        item.data = file;
        item.id = doc.id;
        item.thumb = thumb;
        if (!existing) userItems.unshift(item);
        const written = saveLS();                 // не влезло — saveLS уже сказал об этом алертом
        if (!written && !existing) userItems = userItems.filter(x => x !== item); // строки быть не должно
        if (written) AFX.setStatus(L('Saved to localStorage'));
        ok(written);
        Cat.rebuild();
    }`, `    fetch('api/save', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: doc.name, data: file, thumb: thumb })
    }).then(r => r.json()).then(res => {
        if (!res || res.ok !== true || typeof res.file !== 'string') throw new Error('Save was not confirmed');
        AFX.setStatus(L('Saved') + ': ' + res.file);
        ok(true);
        Cat.refresh();
    }).catch(() => { AFX.setStatus(L('Server save error')); ok(false); });`);
});
edit('js/ui/app.js', r => {
  r('function boot() {', 'function boot() {\n    if (!window.FantasyEffects.isActive()) return;');
  r("L('Save the effect to the catalog: a file in library/ with the server, otherwise localStorage (Ctrl+S)')", "L('Save the current effect to your account\u2019s private library on the server (Ctrl+S)')");
});
edit('js/core/i18n.js', r => {
  r('const RU = {', `const RU = {
    'Personal library': 'Личная библиотека',
    'Personal library is unavailable. Your draft stays in this browser.': 'Личная библиотека недоступна. Черновик остаётся в этом браузере.',
    'Delete failed. Your effect was not removed.': 'Не удалось удалить. Ваш эффект сохранён.',`);
  r("    'Save the effect to the catalog: a file in library/ with the server, otherwise localStorage (Ctrl+S)':\n        'Сохранить эффект в каталог: файл в library/ при запущенном сервере, иначе localStorage (Ctrl+S)',", "    'Save the current effect to your account\u2019s private library on the server (Ctrl+S)':\n        'Сохранить текущий эффект в личную библиотеку вашей учётной записи на сервере (Ctrl+S)',");
});
edit('tools/i18ncheck.mjs', r => {
  r('vm.createContext(sandbox);', 'sandbox.window.FantasyEffects = { storage: sandbox.localStorage };\nvm.createContext(sandbox);');
});
require('./finalize-vendor.cjs');
