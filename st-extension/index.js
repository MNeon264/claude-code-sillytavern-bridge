/*
 * Bridge Memory: the SillyTavern side of claude-code-sillytavern-bridge's
 * Character Memory.
 *
 * Every chat completion request to the Custom endpoint gets a `bridge_memory`
 * field (through Include Body Parameters) saying which chat, persona and
 * character card it's for; nothing else in a request says so, and memory
 * belongs to chats. When a chat is opened and after replies, the extension
 * asks the bridge whether a character's memories from other chats conflict
 * with how this chat starts, and lets you keep them or drop some here.
 *
 * Installed by st-extension/install.sh into data/<user>/extensions/.
 */
import { user_avatar } from '../../../personas.js';

const MODULE = 'bridge_memory';
const VERSION = '1';
const TOKEN_HEADER = 'X-Bridge-Memory-Token';

// Conflicts already shown since the page loaded ("Decide later" shows them
// again the next time the chat is opened after a reload).
const shownChecks = new Set();
let refreshTimers = [];

function context() {
    return SillyTavern.getContext();
}

/** The extension's settings, with a random token the bridge learns from chat requests. */
function settings() {
    const all = context().extensionSettings;
    if (!all[MODULE] || typeof all[MODULE] !== 'object') {
        all[MODULE] = {};
    }
    const s = all[MODULE];
    if (typeof s.token !== 'string' || s.token.length < 32) {
        const bytes = new Uint8Array(24);
        crypto.getRandomValues(bytes);
        s.token = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
        context().saveSettingsDebounced();
    }
    if (typeof s.bridgeUrl !== 'string') {
        s.bridgeUrl = '';
    }
    return s;
}

/** Whether chat requests go to a Custom (OpenAI-compatible) endpoint, as the bridge is. */
function usingCustomEndpoint() {
    const c = context();
    return c.mainApi === 'openai' && c.chatCompletionSettings?.chat_completion_source === 'custom';
}

/** The bridge's address as this browser reaches it: the setting, else the Custom endpoint's URL. */
function bridgeBase() {
    let url = (settings().bridgeUrl || '').trim();
    if (!url) {
        url = String(context().chatCompletionSettings?.custom_url || '').trim();
    }
    return url.replace(/\/+$/, '').replace(/\/v1$/, '');
}

function escapeHtml(text) {
    return String(text ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', '\'': '&#39;' })[ch]);
}

/** A day in this browser's time zone, the one chat file names are in. */
function day(ts) {
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/**
 * A chat the bridge names: its card's or group's name, its branch or own
 * title, the day it was made, and its last message, rather than its file name.
 */
function chatHtml(s, extra = '') {
    const created = s.created_ts ? day(s.created_ts) : (s.created || '');
    const meta = [created ? `created ${created}` : '', extra].filter(Boolean).map(escapeHtml).join(' · ');
    const last = s.last_msg?.text
        ? `<div class="bridge-memory-last"><b>${escapeHtml(s.last_msg.name || '?')}:</b> ${escapeHtml(s.last_msg.text)}</div>` : '';
    return `<span class="bridge-memory-name">${escapeHtml(s.name || s.title || s.chat_id)}`
        + `${s.label ? ` · ${escapeHtml(s.label)}` : ''}</span>`
        + (meta ? ` <span class="bridge-memory-meta">${meta}</span>` : '') + last;
}

/** A recap to read: the short one, if there is one, then each part. */
function recapHtml(parts, short) {
    parts = parts || [];
    if (!parts.some(p => p.recap) && !short) {
        return '<p><small>Not recapped yet.</small></p>';
    }
    const out = [];
    if (short) {
        out.push(`<h5>Short recap, used when the full one doesn't fit</h5><div>${escapeHtml(short)}</div>`);
    }
    for (const p of parts) {
        out.push(`<h5>Messages #${p.from}–#${p.to}</h5>`
            + (p.recap ? `<div>${escapeHtml(p.recap)}</div>` : '<div><small>Not recapped yet.</small></div>'));
    }
    return `<div class="bridge-memory-recap">${out.join('')}</div>`;
}

/** The open chat, the speaking card (set during a generation) and the persona. */
function currentChat() {
    const c = context();
    const meta = c.chatMetadata || {};
    const chid = c.characterId;
    const card = chid !== undefined && chid !== null && chid !== '' ? c.characters[chid] : null;
    return {
        chat_id: String(meta.integrity || ''),
        chat_file: String(c.getCurrentChatId() || ''),
        group_id: c.groupId ? String(c.groupId) : '',
        main_chat: String(meta.main_chat || ''),
        char_avatar: card?.avatar ? String(card.avatar) : '',
        char_name: String(c.name2 || card?.name || ''),
        persona: String(user_avatar || ''),
        persona_name: String(c.name1 || ''),
    };
}

/**
 * Add a field to Include Body Parameters, which SillyTavern's server parses
 * as YAML and merges into the request. A parse error would drop the user's
 * own parameters too, so the field is added in the style they use.
 */
function appendIncludeBody(body, key, value) {
    const text = String(body || '');
    const trimmed = text.trim();
    if (!trimmed) {
        return `${key}: ${JSON.stringify(value)}`;
    }
    if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
        const inner = trimmed.slice(1, -1).trim();
        return `{${inner ? `${inner}, ` : ''}${JSON.stringify(key)}: ${JSON.stringify(value)}}`;
    }
    if (trimmed.startsWith('-')) {
        return `${text}\n- ${key}: ${JSON.stringify(value)}`;
    }
    return `${text}\n${key}: ${JSON.stringify(value)}`;
}

function onSettingsReady(data) {
    try {
        if (!data || data.chat_completion_source !== 'custom') {
            return;
        }
        const c = context();
        const first = (c.chatMetadata || {}).lastInContextMessageId;
        const info = {
            v: VERSION,
            token: settings().token,
            ...currentChat(),
            type: String(data.type || 'normal'),
            // The first chat message in this request: older ones didn't fit the context.
            first_in_context: Number.isInteger(first) ? first : null,
            chat_length: Array.isArray(c.chat) ? c.chat.length : null,
        };
        data.custom_include_body = appendIncludeBody(data.custom_include_body, 'bridge_memory', info);
    } catch (error) {
        console.warn('[Bridge Memory] could not add the chat to the request', error);
    }
}

async function bridgeFetch(path, options = {}) {
    const base = bridgeBase();
    if (!base) {
        throw new Error('no bridge address');
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
        const headers = { [TOKEN_HEADER]: settings().token };
        if (options.body) {
            headers['Content-Type'] = 'application/json';
        }
        const response = await fetch(base + path, { ...options, headers, signal: controller.signal });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const error = new Error(data.error || `HTTP ${response.status}`);
            error.status = response.status;
            throw error;
        }
        return data;
    } finally {
        clearTimeout(timer);
    }
}

function setState(text) {
    $('#bridge_memory_state').text(text);
}

/** Ask the bridge about the open chat (with the recaps: `recaps`); show conflicts not shown yet. */
async function refresh({ quiet = true, recaps = false } = {}) {
    if (!usingCustomEndpoint()) {
        setState('Not in use: chat requests don\'t go to a Custom endpoint.');
        return null;
    }
    const chat = currentChat();
    if (!chat.chat_file) {
        return null;
    }
    let status;
    try {
        const params = new URLSearchParams({
            chat_file: chat.chat_file, group_id: chat.group_id, char_avatar: chat.char_avatar,
            char_name: chat.char_name, persona: chat.persona, persona_name: chat.persona_name,
        });
        if (recaps) {
            params.set('recaps', '1');
        }
        status = await bridgeFetch(`/ext/memory/status?${params}`);
    } catch (error) {
        setState(error.status === 401
            ? 'Not linked yet: send a message in any chat, then open this chat again.'
            : `Can't reach the bridge at ${bridgeBase() || '(no address)'}: ${error.message}`);
        if (!quiet) {
            toastr.warning($('#bridge_memory_state').text(), 'Bridge Memory');
        }
        return null;
    }
    setState(status.enabled ? 'Connected to the bridge.' : 'Connected, but Character Memory is off in the bridge\'s Settings.');
    for (const ch of status.characters || []) {
        if (ch.check && !shownChecks.has(ch.check.id)) {
            shownChecks.add(ch.check.id);
            await showConflict(ch);
        }
    }
    return status;
}

/** Refresh now and again later: recaps and the conflict check run in the background. */
function scheduleRefresh(delays) {
    refreshTimers.forEach(clearTimeout);
    const chatAtStart = context().getCurrentChatId();
    refreshTimers = delays.map(delay => setTimeout(() => {
        if (context().getCurrentChatId() === chatAtStart) {
            refresh();
        }
    }, delay));
}

async function showConflict(ch) {
    const c = context();
    const sources = ch.check.sources || [];
    const items = sources.map(s => `
        <label class="checkbox_label bridge-memory-source">
            <input type="checkbox" data-chat="${escapeHtml(s.chat_id)}" checked>
            <span>${chatHtml(s)}</span>
        </label>`).join('');
    const html = `
        <div class="bridge-memory-popup">
            <h3>${escapeHtml(ch.name)}'s memories conflict with this chat</h3>
            <p>${escapeHtml(ch.check.explanation || 'How this chat starts doesn\'t match what they remember.')}</p>
            <p>Memories ${escapeHtml(ch.name)} brings into this chat. Untick the ones to leave out of this chat:</p>
            ${items}
            <p><small>Ticked memories stay; they count over the card's scenario. You can change this later on the bridge's Memory tab.</small></p>
        </div>`;
    let dropped = [];
    const popup = new c.Popup(html, c.POPUP_TYPE.CONFIRM, '', {
        okButton: 'Apply',
        cancelButton: 'Decide later',
        wider: true,
        allowVerticalScrolling: true,
        onClosing: (p) => {
            if (p.result === c.POPUP_RESULT.AFFIRMATIVE) {
                dropped = Array.from(p.content.querySelectorAll('input[data-chat]'))
                    .filter(input => !input.checked)
                    .map(input => input.dataset.chat);
            }
            return true;
        },
    });
    const result = await popup.show();
    if (result !== c.POPUP_RESULT.AFFIRMATIVE) {
        return;
    }
    try {
        await bridgeFetch(`/ext/memory/check/${encodeURIComponent(ch.check.id)}`, {
            method: 'POST',
            body: JSON.stringify({ action: dropped.length ? 'drop' : 'keep', drop: dropped }),
        });
        toastr.info(dropped.length
            ? `${ch.name} won't use ${dropped.length} of those memories in this chat.`
            : `${ch.name} keeps those memories in this chat.`, 'Bridge Memory');
    } catch (error) {
        toastr.error(`Couldn't save that: ${error.message}`, 'Bridge Memory');
    }
}

function recapText(recap) {
    if (!recap || !recap.available) {
        return 'chat file not found';
    }
    if (!recap.parts) {
        return 'nothing to recap';
    }
    return recap.missing ? `recapped ${recap.done} of ${recap.parts} parts so far` : 'recapped';
}

/** Per character: the chats they remember here, each opening to its recap, then their recap of this chat. */
async function showMemories() {
    const status = await refresh({ quiet: false, recaps: true });
    if (!status) {
        return;
    }
    const sections = (status.characters || []).map(ch => {
        const list = ch.sources.length
            ? ch.sources.map(s => `
                <details class="bridge-memory-item">
                    <summary>${chatHtml(s, `${s.how === 'link' ? 'connected' : 'persona binding'}; ${recapText(s.recap)}`)}</summary>
                    ${recapHtml(s.parts, s.short)}
                </details>`).join('')
            : '<p><small>Nothing from other chats.</small></p>';
        const own = ch.own && (ch.own.short || (ch.own.parts || []).some(p => p.recap)) ? `
                <details class="bridge-memory-item">
                    <summary><span class="bridge-memory-name">What ${escapeHtml(ch.name)} remembers of this chat</span>
                        <div class="bridge-memory-last">Used in the chats it's connected or bound to, and here once its oldest messages no longer fit.</div></summary>
                    ${recapHtml(ch.own.parts, ch.own.short)}
                </details>` : '';
        const bound = ch.bound ? ' <small>(bound to this persona)</small>' : '';
        return `<h4>${escapeHtml(ch.name)}${bound}</h4>${list}${own}`;
    });
    const body = sections.length ? sections.join('') : '<p>No character in this chat.</p>';
    await context().callGenericPopup(
        `<div class="bridge-memory-popup"><h3>Memories in this chat</h3><p><small>Open one to read its recap.</small></p>${body}<p><small>Connect chats, bind personas and edit recaps on the bridge's Memory tab.</small></p></div>`,
        context().POPUP_TYPE.TEXT, '', { wider: true, allowVerticalScrolling: true });
}

jQuery(() => {
    const html = `
        <div class="bridge-memory-settings">
            <div class="inline-drawer">
                <div class="inline-drawer-toggle inline-drawer-header">
                    <b>Bridge Memory</b>
                    <div class="inline-drawer-icon fa-solid fa-circle-chevron-down down"></div>
                </div>
                <div class="inline-drawer-content">
                    <small>Tells the Claude Code bridge which chat, persona and character each request is for, so characters can remember other chats. Manage memories on the bridge's Memory tab.</small>
                    <label for="bridge_memory_url">Bridge address, as this browser reaches it</label>
                    <input id="bridge_memory_url" class="text_pole" type="text" placeholder="the Custom endpoint's URL">
                    <div class="flex-container">
                        <div id="bridge_memory_show" class="menu_button">Memories in this chat</div>
                    </div>
                    <small id="bridge_memory_state"></small>
                </div>
            </div>
        </div>`;
    $('#extensions_settings2').append(html);
    $('#bridge_memory_url').val(settings().bridgeUrl).on('input', function () {
        settings().bridgeUrl = String($(this).val()).trim();
        context().saveSettingsDebounced();
    });
    $('#bridge_memory_show').on('click', showMemories);

    const { eventSource, eventTypes } = context();
    eventSource.on(eventTypes.CHAT_COMPLETION_SETTINGS_READY, onSettingsReady);
    eventSource.on(eventTypes.CHAT_CHANGED, () => scheduleRefresh([800, 30000, 90000]));
    eventSource.on(eventTypes.MESSAGE_RECEIVED, () => scheduleRefresh([1500, 20000, 60000]));
});
