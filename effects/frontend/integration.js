/* Community Fantasy Workbench adapter. Credentials are supplied only by the server. */
(function () {
    'use strict';

    const boot = window.FantasyEffectsSession;
    const valid = !!(boot && boot.user && boot.user.id != null &&
        String(boot.user.id) && typeof boot.csrf === 'string' && boot.csrf &&
        typeof boot.storage_key === 'string' && boot.storage_key);
    const owner = valid ? String(boot.user.id) : '';
    const csrf = valid ? boot.csrf : '';
    const apiBase = valid && typeof boot.api_base === 'string' && /^(?:\/[A-Za-z0-9_-]+)*$/.test(boot.api_base) ? boot.api_base : '';
    const prefix = valid ? 'fantasy-effects:' + encodeURIComponent(boot.storage_key) + ':' : null;
    const originalFetch = window.fetch.bind(window);
    let locked = !valid;
    let lockReason = valid ? '' : '登录信息不可用，请返回平台重新打开特效制作。';
    let connection = 'checking';
    let checking = null;
    let storageWarning = false;
    let storage;
    // Retain the original namespace for this document's entire lifetime.
    try { storage = window.localStorage; } catch (_) { storageWarning = true; }
    const temporary = new Map();

    function rawKeys() {
        if (!prefix) return [];
        if (!storage) return Array.from(temporary.keys());
        const keys = [];
        for (let i = 0; i < storage.length; i++) {
            const key = storage.key(i);
            if (key && key.startsWith(prefix)) keys.push(key.slice(prefix.length));
        }
        return keys;
    }
    const methods = {
        getItem(key) {
            if (!prefix) return null;
            key = String(key);
            return storage ? storage.getItem(prefix + key) : (temporary.get(key) ?? null);
        },
        setItem(key, value) {
            if (!prefix) throw new Error('Missing authenticated storage namespace');
            key = String(key); value = String(value);
            // A locked tab may flush its own draft, never another user's namespace.
            if (storage) {
                try { storage.setItem(prefix + key, value); }
                catch (error) { storageWarning = true; renderStatus(); throw error; }
            }
            else { temporary.set(key, value); storageWarning = true; renderStatus(); }
        },
        removeItem(key) {
            if (!prefix) return;
            key = String(key);
            if (storage) storage.removeItem(prefix + key);
            else temporary.delete(key);
        },
        clear() { rawKeys().forEach(key => methods.removeItem(key)); },
        key(index) { return rawKeys()[Number(index)] ?? null; }
    };
    const scopedStorage = new Proxy(Object.create(null), {
        get(_target, key) {
            if (key === 'length') return rawKeys().length;
            if (Object.prototype.hasOwnProperty.call(methods, key)) return methods[key];
            if (typeof key === 'symbol') return undefined;
            return methods.getItem(key) ?? undefined;
        },
        set(_target, key, value) { methods.setItem(key, value); return true; },
        deleteProperty(_target, key) { methods.removeItem(key); return true; },
        ownKeys() { return rawKeys(); },
        has(_target, key) { return rawKeys().includes(String(key)); },
        getOwnPropertyDescriptor(_target, key) {
            if (rawKeys().includes(String(key))) {
                return { configurable: true, enumerable: true, writable: true, value: methods.getItem(key) };
            }
        }
    });

    function flushDraft() {
        try { if (window.AFX && window.AFX.flushAutosave) window.AFX.flushAutosave(); }
        catch (_) { storageWarning = true; }
    }
    function assertActive() {
        if (locked) throw new Error('Effects session is locked');
        return true;
    }
    function lock(reason) {
        if (!locked) flushDraft();
        locked = true;
        lockReason = reason || '登录已失效或账号已切换。请返回平台重新打开特效制作。';
        if (window.AFX && window.AFX.state) window.AFX.state.playing = false;
        renderStatus();
    }
    function setConnection(value) {
        const wasOffline = connection === 'offline';
        connection = value;
        renderStatus();
        if (wasOffline && value === 'online' && !locked && window.AFX && window.AFX.Catalog) {
            window.AFX.Catalog.refresh();
        }
    }
    function apiURL(input) {
        const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, window.location.href);
        if (url.origin !== window.location.origin) return null;
        if (apiBase && url.pathname.startsWith('/api/')) url.pathname = apiBase + url.pathname;
        return url.pathname.startsWith(apiBase + '/api/') ? url : null;
    }
    window.fetch = async function (input, init) {
        const url = apiURL(input);
        if (!url) return originalFetch(input, init);
        assertActive();
        let request = new Request(input, init);
        if (request.url !== url.href) {
            // Keep local HTTP/1.1 saves as ordinary uploads. A Request passed
            // as RequestInit exposes a stream and makes Chromium require H2.
            const body = request.body ? await request.blob() : undefined;
            request = new Request(url.href, {method: request.method, headers: request.headers, body,
                mode: request.mode, credentials: request.credentials, cache: request.cache,
                redirect: request.redirect, referrer: request.referrer, referrerPolicy: request.referrerPolicy,
                integrity: request.integrity, keepalive: request.keepalive, signal: request.signal});
        }
        const headers = new Headers(request.headers);
        headers.set('X-Effects-Owner', owner);
        if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method.toUpperCase())) headers.set('X-Effects-CSRF', csrf);
        let response;
        try {
            response = await originalFetch(new Request(request, {
                headers, credentials: 'same-origin', cache: 'no-store', redirect: 'error'
            }));
        } catch (error) {
            setConnection('offline');
            flushDraft();
            throw error;
        }
        let failure;
        if (response.status === 403) { try { failure = await response.clone().json(); } catch (_) {} }
        if (response.status === 401 || (response.status === 403 && ['owner_changed', 'invalid_csrf'].includes(failure?.code))) {
            lock();
            throw new Error('Effects session rejected: ' + response.status);
        }
        assertActive();
        if (!response.ok) {
            const error = new Error(failure?.error || 'Effects request failed: ' + response.status);
            error.status = response.status;
            throw error;
        }
        // A pending response must not be applied after another request locks this tab.
        for (const name of ['json', 'text', 'blob', 'arrayBuffer']) {
            const read = response[name].bind(response);
            response[name] = async function () {
                const value = await read();
                assertActive();
                return value;
            };
        }
        setConnection('online');
        return response;
    };

    async function checkSession() {
        if (locked) return false;
        if (checking) return checking;
        checking = (async function () {
            try {
                const response = await window.fetch('/api/session');
                const session = await response.json();
                if (!session || !session.user || String(session.user.id) !== owner) { lock(); return false; }
                return true;
            } catch (_) { return false; }
            finally { checking = null; }
        })();
        return checking;
    }

    window.FantasyEffects = Object.freeze({
        storage: scopedStorage, assertActive, checkSession, flushDraft, lock,
        isActive: () => !locked,
        serverRequired: true
    });

    function safeURL(value, fallback) {
        try {
            const url = new URL(value, window.location.href);
            return ['http:', 'https:'].includes(url.protocol) ? url.href : fallback;
        } catch (_) { return fallback; }
    }
    function node(tag, text, className) {
        const el = document.createElement(tag);
        if (text) el.textContent = text;
        if (className) el.className = className;
        return el;
    }
    function renderStatus() {
        const status = document.getElementById('fantasy-effects-status');
        if (status) {
            status.textContent = locked ? '会话已锁定' : storageWarning ? '浏览器无法持久保存草稿，请及时保存到个人特效库' :
                connection === 'offline' ? '连接已中断 · 本账号草稿保留在此浏览器，请恢复连接后保存' :
                connection === 'checking' ? '正在检查登录…' : '共享特效库 · 管理自己的特效，使用协作者素材';
            status.dataset.state = locked ? 'locked' : storageWarning ? 'offline' : connection;
        }
        const app = document.getElementById('app');
        if (app) {
            app.inert = locked;
            app.setAttribute('aria-hidden', locked ? 'true' : 'false');
        }
        const overlay = document.getElementById('fantasy-effects-lock');
        if (overlay) {
            overlay.hidden = !locked;
            document.getElementById('fantasy-effects-lock-message').textContent = lockReason;
        }
    }
    function mount() {
        const header = node('nav', null, 'fantasy-effects-nav');
        header.id = 'fantasy-effects-nav';
        header.setAttribute('aria-label', '平台导航');
        header.appendChild(node('strong', '特效制作', 'fantasy-effects-brand'));
        const home = node('a', '返回主页');
        home.href = safeURL(boot && boot.home_url, '/');
        home.title = '保存本账号的浏览器草稿，然后返回平台主页';
        home.addEventListener('click', flushDraft);
        header.appendChild(home);
        const library = node('button', '共享特效库');
        library.type = 'button';
        library.title = '查看本人及协作者的特效；他人特效可使用并保存为自己的副本';
        library.addEventListener('click', function () {
            if (locked) return;
            if (window.AFX && window.AFX.Catalog) window.AFX.Catalog.refresh();
            const panel = document.getElementById('panel-catalog');
            if (panel) { panel.setAttribute('tabindex', '-1'); panel.focus(); }
        });
        header.appendChild(library);
        const status = node('span', null, 'fantasy-effects-status');
        status.id = 'fantasy-effects-status';
        status.setAttribute('role', 'status');
        header.appendChild(status);
        header.appendChild(node('span', '账号：' + (valid ? (boot.user.name || owner) : '未登录'), 'fantasy-effects-account'));
        const logout = node('a', '退出登录');
        logout.href = safeURL(boot && boot.logout_url, home.href);
        logout.title = '保留当前账号的浏览器草稿并退出平台登录';
        logout.addEventListener('click', () => lock('已退出登录。请返回平台登录后重新打开特效制作。'));
        header.appendChild(logout);
        document.body.prepend(header);

        const overlay = node('div', null, 'fantasy-effects-lock');
        overlay.id = 'fantasy-effects-lock';
        overlay.setAttribute('role', 'alertdialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.setAttribute('aria-labelledby', 'fantasy-effects-lock-title');
        const box = node('div', null, 'fantasy-effects-lock-box');
        const title = node('h2', '特效制作已锁定');
        title.id = 'fantasy-effects-lock-title';
        box.appendChild(title);
        const message = node('p');
        message.id = 'fantasy-effects-lock-message';
        box.appendChild(message);
        box.appendChild(node('p', '编辑、保存与导出已暂停。未保存的草稿仍属于原账号。'));
        const returnHome = node('a', '返回平台');
        returnHome.href = home.href;
        box.appendChild(returnHome);
        overlay.appendChild(box);
        document.body.appendChild(overlay);

        // Guard delayed imports/exports as well as ordinary UI interactions.
        const afx = window.AFX;
        if (afx) {
            const guard = function (object, name) {
                if (!object || typeof object[name] !== 'function') return;
                const action = object[name];
                object[name] = function () { if (locked) return; return action.apply(this, arguments); };
            };
            ['openDoc', 'saveCurrent', 'exportAtlasPNG', 'exportMeta', 'downloadBlob', 'downloadText',
                'setLang', 'pushUndo', 'undo', 'redo'].forEach(name => guard(afx, name));
            ['saveCurrent', 'importJsonFile'].forEach(name => guard(afx.Catalog, name));
        }
        renderStatus();
        if (!locked) checkSession();
        window.setInterval(checkSession, 15000);
    }
    // Registered before upstream listeners, including document-wide shortcuts and drops.
    ['click', 'dblclick', 'contextmenu', 'keydown', 'keyup', 'pointerdown', 'pointermove', 'pointerup',
        'mousedown', 'mouseup', 'touchstart', 'touchmove', 'wheel', 'beforeinput', 'input', 'change', 'drop', 'dragover']
        .forEach(type => window.addEventListener(type, function (event) {
            if (!locked) return;
            const target = event.target;
            if (target && target.closest && target.closest('#fantasy-effects-nav, #fantasy-effects-lock')) return;
            event.preventDefault(); event.stopImmediatePropagation();
        }, { capture: true, passive: false }));
    window.addEventListener('focus', checkSession);
    window.addEventListener('online', checkSession);
    window.addEventListener('offline', () => { setConnection('offline'); flushDraft(); });
    window.addEventListener('pagehide', flushDraft);
    window.addEventListener('pageshow', checkSession);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) checkSession(); else flushDraft(); });
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
    else mount();
})();
