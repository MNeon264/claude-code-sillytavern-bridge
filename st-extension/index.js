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
 * It also adds a "Group reply" reply strategy to group chats: one reply
 * written jointly by every unmuted member, from their joined cards, with
 * {{char}} as their names joined by commas. SillyTavern has no hook for a
 * reply strategy, so the extension changes the group in memory around each
 * turn and puts it back (see ensurePooled and groupReplyStart).
 *
 * Installed by st-extension/install.sh into data/<user>/extensions/.
 */
import { setCharacterId, setCharacterName } from '../../../../script.js';
import { editGroup, group_activation_strategy, group_generation_mode, groups, is_group_generating, openGroupId } from '../../../group-chats.js';
import { user_avatar } from '../../../personas.js';
import { power_user } from '../../../power-user.js';

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
    if (!s.groupReply || typeof s.groupReply !== 'object') {
        s.groupReply = {};
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

/**
 * A recap to read: what this chat gets of it (`passed`: its synopsis or chapters,
 * minus those left out here), then the part recaps they're written from, folded.
 * Without `passed`, the part recaps alone.
 */
function recapHtml(parts, passed) {
    parts = parts || [];
    if (!parts.some(p => p.recap)) {
        return '<p><small>Not recapped yet.</small></p>';
    }
    const out = [];
    const sections = passed?.sections || [];
    sections.forEach((s, i) => {
        const what = s.title ? `Chapter ${i + 1}: ${s.title}` : passed.mode === 'chapters' ? 'The whole chat' : 'Synopsis';
        const note = !s.on ? ' (left out here)' : !s.ready ? ' (not written yet; the part recaps meanwhile)' : '';
        out.push(`<h5>${escapeHtml(what)}, messages #${s.from}–#${s.to}${note}</h5>`
            + (s.on && s.ready ? `<div>${escapeHtml(s.text)}</div>` : ''));
    });
    const partsHtml = parts.map(p => `<h5>Messages #${p.from}–#${p.to}${p.there === false ? ' (not in it: a plain account)' : ''}</h5>`
        + (p.recap ? `<div>${escapeHtml(p.recap)}</div>` : '<div><small>Not recapped yet.</small></div>')).join('');
    out.push(sections.length ? `<details><summary><small>Part recaps, 40 messages each</small></summary>${partsHtml}</details>` : partsHtml);
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

/** "; 2 of 3 chapters" for a chat divided into chapters. */
function chaptersText(passed) {
    if (!passed?.chaptered) {
        return '';
    }
    return `; ${passed.sections.filter(s => s.on).length} of ${passed.sections.length} chapters`;
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
                    <summary>${chatHtml(s, `${s.how === 'link' ? 'connected' : 'persona binding'}; ${recapText(s.recap)}${chaptersText(s.passed)}${s.there === false ? `; background: ${ch.name} isn't in it` : ''}`)}</summary>
                    ${recapHtml(s.parts, s.passed)}
                </details>`).join('')
            : '<p><small>Nothing from other chats.</small></p>';
        const own = ch.own && (ch.own.parts || []).some(p => p.recap) ? `
                <details class="bridge-memory-item">
                    <summary><span class="bridge-memory-name">What ${escapeHtml(ch.name)} remembers of this chat</span>
                        <div class="bridge-memory-last">Used in the chats it's connected or bound to, and here once its oldest messages no longer fit.</div></summary>
                    ${recapHtml(ch.own.parts)}
                </details>` : '';
        const bound = ch.bound ? ' <small>(bound to this persona)</small>' : '';
        return `<h4>${escapeHtml(ch.name)}${bound}</h4>${list}${own}`;
    });
    const body = sections.length ? sections.join('') : '<p>No character in this chat.</p>';
    await context().callGenericPopup(
        `<div class="bridge-memory-popup"><h3>Memories in this chat</h3><p><small>Open one to read its recap.</small></p>${body}<p><small>Connect chats, bind personas and edit recaps on the bridge's Memory tab.</small></p></div>`,
        context().POPUP_TYPE.TEXT, '', { wider: true, allowVerticalScrolling: true });
}

const GROUP_REPLY = 'bridge_group_reply';
const STRATEGY_SELECT = 'rm_group_activation_strategy';

// What a group-reply turn changed, to put back: the group's generation mode
// and the group trimming setting.
let pending = null;
// The turn that's starting was asked of one member (/trigger), so it stays theirs.
let forcedMember = false;

function groupReplyOn(groupId) {
    return !!(groupId && settings().groupReply[groupId]);
}

/** The group's unmuted members whose cards exist, as {avatar, name, index}. */
function unmutedMembers(group) {
    const characters = context().characters;
    return group.members
        .filter(avatar => !group.disabled_members.includes(avatar))
        .map(avatar => characters.findIndex(c => c.avatar === avatar))
        .filter(index => index !== -1)
        .map(index => ({ avatar: characters[index].avatar, name: characters[index].name, index }));
}

/**
 * A group-reply group runs under Pooled order, which drafts exactly one
 * unmuted member however the turn starts (a message, Regenerate, auto mode);
 * groupReplyDrafted then makes it the joint reply. It is also what the group
 * falls back to without this extension.
 */
function ensurePooled(groupId) {
    const group = groupReplyOn(groupId) && groups.find(g => g.id === groupId);
    if (group && group.activation_strategy !== group_activation_strategy.POOLED) {
        group.activation_strategy = group_activation_strategy.POOLED;
        editGroup(group.id, false, false);
    }
}

/** Show Group reply as the strategy of the group whose panel is open; SillyTavern resets the select whenever the panel opens. */
function syncGroupReplySelect() {
    const select = $(`#${STRATEGY_SELECT}`);
    const on = groupReplyOn(openGroupId);
    if (on && select.val() !== GROUP_REPLY) {
        select.val(GROUP_REPLY);
    }
    $('#bridge_group_reply_note').toggle(on);
}

/** Before SillyTavern's own handler: Group reply is stored here, any other choice clears it and goes on to SillyTavern. */
function onStrategyChange(event) {
    if (event.target?.id !== STRATEGY_SELECT) {
        return;
    }
    const groupId = openGroupId;
    if (!groupId) {
        return;
    }
    const s = settings();
    if (event.target.value === GROUP_REPLY) {
        event.stopImmediatePropagation();
        s.groupReply[groupId] = true;
        ensurePooled(groupId);
    } else {
        delete s.groupReply[groupId];
    }
    context().saveSettingsDebounced();
    syncGroupReplySelect();
}

/** A turn from the chat box or a command: remember whether it names its member. */
function groupReplyCommand(type, options, dryRun) {
    if (is_group_generating || dryRun) {
        return;
    }
    forcedMember = typeof options?.force_chid === 'number';
    ensurePooled(context().groupId);
}

/**
 * The group's turn has started; its one member is drafted next. A group-reply
 * turn joins the cards (exclude muted) and keeps SillyTavern from cutting the
 * reply at the other members' "Name:" lines, until the turn ends.
 */
function groupReplyStart({ selected_group: groupId, type } = {}) {
    groupReplyRestore();
    const forced = forcedMember;
    forcedMember = false;
    if (forced || !groupReplyOn(groupId) || ['impersonate', 'quiet'].includes(type)) {
        return;
    }
    const group = groups.find(g => g.id === groupId);
    const unmuted = group ? unmutedMembers(group) : [];
    if (!unmuted.length) {
        return;
    }
    pending = {
        group,
        unmuted,
        // A swipe or continue stays with the card of the message it's for.
        keepSpeaker: ['swipe', 'continue'].includes(type),
        mode: group.generation_mode,
        trimming: power_user.disable_group_trimming,
    };
    group.generation_mode = group_generation_mode.APPEND;
    power_user.disable_group_trimming = true;
}

/** The member is drafted: the reply is the first unmuted member's card, and {{char}} names everyone. */
function groupReplyDrafted() {
    if (!pending) {
        return;
    }
    const { unmuted } = pending;
    if (!pending.keepSpeaker) {
        setCharacterId(unmuted[0].index);
    }
    setCharacterName(unmuted.map(m => m.name).join(', '));
}

/** The turn ended: put everything back, and save the group if SillyTavern saved it meanwhile. */
function groupReplyRestore() {
    if (!pending) {
        return;
    }
    const { group, mode } = pending;
    power_user.disable_group_trimming = pending.trimming;
    const changed = group.generation_mode !== mode;
    group.generation_mode = mode;
    pending = null;
    if (changed) {
        editGroup(group.id, true, false).catch(error => console.warn('[Bridge Memory] could not save the group', error));
    }
}

jQuery(() => {
    $(`#${STRATEGY_SELECT}`).append(`<option value="${GROUP_REPLY}">Group reply (all unmuted)</option>`);
    $(`#${STRATEGY_SELECT}`).closest('div').append(
        '<small id="bridge_group_reply_note" style="display:none">One joint reply from every unmuted member; their cards are joined (exclude muted) whatever the handling mode says.</small>');
    document.addEventListener('change', onStrategyChange, true);
    const panel = document.getElementById('rm_group_chats_block');
    if (panel) {
        new MutationObserver(syncGroupReplySelect).observe(panel, { attributes: true, attributeFilter: ['style', 'class'] });
    }

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
    eventSource.on(eventTypes.CHAT_CHANGED, syncGroupReplySelect);
    eventSource.on(eventTypes.GROUP_UPDATED, syncGroupReplySelect);
    eventSource.on(eventTypes.CHAT_CHANGED, () => ensurePooled(context().groupId));
    eventSource.on(eventTypes.GENERATION_AFTER_COMMANDS, groupReplyCommand);
    eventSource.on(eventTypes.GROUP_WRAPPER_STARTED, groupReplyStart);
    eventSource.on(eventTypes.GROUP_MEMBER_DRAFTED, groupReplyDrafted);
    eventSource.on(eventTypes.GROUP_WRAPPER_FINISHED, () => {
        groupReplyRestore();
        syncGroupReplySelect();
    });
    eventSource.on(eventTypes.MESSAGE_RECEIVED, () => scheduleRefresh([1500, 20000, 60000]));
});
