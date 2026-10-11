(() => {
  'use strict';

  const byId = (id) => document.getElementById(id);
  const UI_CONTRACT_VERSION = '2.0.0';
  const UI_ASSET_VERSION = 'agent-ui-2.0.0-t18.2';
  let compatible = false;
  let agentSupported = false;
  let agentBusy = false;
  let agentCachePending = false;
  let catalog = null;
  let credential = null;
  const projectOriginals = new WeakMap();
  const ui = {
    form: byId('config-form'), fields: byId('configuration-fields'), projects: byId('projects'),
    count: byId('project-count'), empty: byId('empty-projects'), add: byId('add-project'),
    save: byId('save-config'), saveConnection: byId('save-connection'), enableCloudflare: byId('enable-cloudflare'), saveState: byId('save-state'), saveDescription: byId('save-description'),
    mode: byId('tunnel-mode'), port: byId('server-port'), path: byId('server-path'), publicUrl: byId('public-url'),
    cloudflareMessage: byId('cloudflare-message'), cloudflareDraft: byId('cloudflare-draft-note'), cloudflarePrerequisite: byId('cloudflare-prerequisite'), customPublicUrl: byId('custom-public-url'),
    tunnelHelp: byId('tunnel-help'), namedWarning: byId('named-warning'), cloudflareGuide: byId('cloudflare-guide'), localModeNote: byId('local-mode-note'),
    startup: byId('startup-enabled'), startupStatus: byId('startup-status'),
    message: byId('page-message'), conflict: byId('conflict-message'), reload: byId('reload-config'),
    probe: byId('probe-now'), restart: byId('restart-mcp'), statusError: byId('status-error'),
    freshness: byId('status-freshness'), chart: byId('network-chart'), historyNote: byId('history-note'),
    events: byId('connection-events'), mcpUrl: byId('mcp-url'), copy: byId('copy-url'),
    localUrl: byId('local-mcp-url'), copyLocal: byId('copy-local-url'),
    ngrokGuide: byId('ngrok-guide'), ngrokToken: byId('ngrok-authtoken'), ngrokEnable: byId('enable-ngrok'),
    ngrokState: byId('ngrok-credential-state'), ngrokDraft: byId('ngrok-draft-note'),
    ngrokPrerequisite: byId('ngrok-prerequisite'), ngrokRefresh: byId('refresh-ngrok'), ngrokMessage: byId('ngrok-message'),
  };
  let token = '';
  let revision = null;
  let loadedData = null;
  let dirty = false;
  let busy = false;
  let polling = false;
  let cardSequence = 0;
  let ready = false;
  let pendingExternalRevision = null;
  let ngrokInfo = null;
  let startupSupported = null;
  let activeTunnelAction = null;
  let lastStatus = null;

  const clone = (value) => JSON.parse(JSON.stringify(value));
  const message = (text, kind = '') => {
    ui.message.textContent = text;
    ui.message.className = `message ${kind}`;
    ui.message.hidden = !text;
    ui.message.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  };
  const explain = (error) => {
    const extra = Array.isArray(error.details) ? error.details.join('\n') : '';
    return [error.message || '本机服务未完成操作。', error.hint, extra].filter(Boolean).join('\n');
  };

  async function api(path, { method = 'GET', data, timeout = 18000, renewSession = true } = {}) {
    if (method !== 'GET' && path !== '/api/probe' && !compatible) {
      throw new Error('页面与服务版本不一致。请刷新页面；当前保留只读状态。');
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeout);
    try {
      const headers = { Accept: 'application/json' };
      if (method !== 'GET') {
        headers['Content-Type'] = 'application/json';
        headers['X-Setup-Token'] = token;
        headers['X-UI-Contract-Version'] = UI_CONTRACT_VERSION;
        headers['X-UI-Asset-Version'] = UI_ASSET_VERSION;
      }
      const response = await fetch(path, {
        method, headers, body: data === undefined ? undefined : JSON.stringify(data),
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
      });
      let result;
      try { result = await response.json(); } catch {
        throw Object.assign(new Error('本机服务返回了无法读取的响应。'), { hint: '查看启动窗口，确认 Setup 仍在运行，然后重新读取配置。' });
      }
      if (!response.ok || result.error || result.ok === false) {
        const failure = result.error || {};
        // TOKEN_REQUIRED is rejected by Setup before any mutation is accepted.
        // Retry this rejection once; never replay a timeout or network failure.
        if (method !== 'GET' && response.status === 403 && failure.code === 'TOKEN_REQUIRED' && renewSession) {
          const session = await api('/api/session');
          if (!session.token) throw new Error('本机配置会话未恢复，请重新打开配置页面。');
          token = session.token;
          return api(path, { method, data, timeout, renewSession: false });
        }
        throw Object.assign(new Error(failure.message || '操作没有完成。'), failure, { status: response.status });
      }
      return result;
    } catch (error) {
      if (error.name === 'AbortError') {
        throw Object.assign(new Error('操作等待超时，尚未确认生效。'), {
          hint: '请等待启动窗口中的操作结束，再重新读取配置和检测状态；不要重复提交。', code: 'REQUEST_TIMEOUT',
        });
      }
      if (error instanceof TypeError) {
        throw Object.assign(new Error('暂时无法连接本机配置服务。'), {
          hint: '确认 setup.bat 的窗口仍在运行；关闭后重新双击 setup.bat，再刷新页面。', code: 'SETUP_UNREACHABLE',
        });
      }
      throw error;
    } finally {
      window.clearTimeout(timer);
    }
  }

  function controls() {
    const writable = ready && compatible && !busy && !agentBusy;
    ui.fields.disabled = !writable;
    for (const input of [ui.mode, ui.port, ui.path, ui.publicUrl]) input.disabled = !writable;
    ui.startup.disabled = !writable || startupSupported !== true;
    ui.save.disabled = !writable || !dirty;
    ui.saveConnection.disabled = ui.save.disabled;
    ui.saveConnection.hidden = !dirty;
    ui.probe.disabled = !ready || busy;
    ui.restart.disabled = !writable;
    ui.reload.disabled = busy;
    const blockedDraft = hasUnsavedNonTunnelConfig();
    ui.enableCloudflare.disabled = !writable || blockedDraft;
    ui.cloudflareDraft.hidden = !blockedDraft;
    ui.enableCloudflare.textContent = activeTunnelAction === 'cloudflare' ? '正在连接…' : '启用 / 重试临时连接';
    ui.enableCloudflare.setAttribute('aria-busy', activeTunnelAction === 'cloudflare' ? 'true' : 'false');
    ui.ngrokEnable.disabled = !writable || blockedDraft || ngrokInfo?.agent_available !== true;
    ui.ngrokToken.disabled = !writable;
    ui.ngrokRefresh.disabled = !ready || busy;
    ui.ngrokDraft.hidden = !blockedDraft;
    ui.ngrokEnable.setAttribute('aria-busy', busy ? 'true' : 'false');
    ui.save.setAttribute('aria-busy', busy ? 'true' : 'false');
    ui.form.setAttribute('aria-busy', busy ? 'true' : 'false');
    if (busy) {
      ui.saveState.textContent = '正在应用…';
      ui.saveDescription.textContent = '验证完成后确认。';
    } else if (dirty) {
      ui.saveState.textContent = '有未保存的修改';
      ui.saveDescription.textContent = '保存后立即应用。';
    } else if (ready && !compatible) {
      ui.saveState.textContent = '页面版本需要更新';
      ui.saveDescription.textContent = '只读状态仍可查看，请刷新页面。';
    } else if (ready) {
      ui.saveState.textContent = '已读取配置';
      ui.saveDescription.textContent = '';
    }
    agentControls(writable);
  }

  function markDirty() {
    dirty = !loadedData || JSON.stringify(collectConfig()) !== JSON.stringify(loadedData);
    message('');
    controls();
  }

  function tunnelHelp() {
    const descriptions = {
      off: '当前仅供本机使用。下方 ngrok 向导可帮助你连接 ChatGPT。',
      cloudflare: '无需注册；需要本机已安装 cloudflared。重启后地址可能变化。',
      ngrok: '推荐持续接入。需要账号、authtoken 和本机程序。',
      named: '远程转发规则无法在本机验证，此模式不会启动。',
    };
    ui.tunnelHelp.textContent = descriptions[ui.mode.value] || '';
    ui.namedWarning.hidden = ui.mode.value !== 'named';
    const ngrok = ui.mode.value === 'ngrok';
    ui.ngrokGuide.hidden = !['off', 'ngrok'].includes(ui.mode.value);
    ui.cloudflareGuide.hidden = ui.mode.value !== 'cloudflare';
    ui.localModeNote.hidden = ui.mode.value !== 'off';
    ui.customPublicUrl.hidden = ui.mode.value === 'cloudflare';
    ui.publicUrl.required = false;
    byId('public-url-label').textContent = '已有公网 MCP 地址（可选）';
    byId('public-url-help').textContent = ngrok ? '留空自动获取 ngrok 地址；已有域名可填完整 HTTPS MCP 地址。' : '填写完整 HTTPS 地址，含 MCP 路径。临时隧道留空。';
    ui.mode.setCustomValidity(ui.mode.value === 'named' ? '请选择临时 Cloudflare 或 ngrok：本机无法验证 Named Tunnel 的远程转发隔离。' : '');
    if (lastStatus) renderStatus(lastStatus);
  }

  function refreshProject(card) {
    const name = card.querySelector('.project-name-input').value.trim();
    const path = card.querySelector('.project-path-input').value.trim();
    const enabled = card.querySelector('.controlled-write').checked;
    card.querySelector('.project-name').textContent = name || '新项目';
    card.querySelector('.project-path').textContent = path || '填写本地项目目录';
    card.querySelector('.project-path').title = path;
    card.querySelector('.permission-badge').textContent = enabled ? '受控写' : '只读';
    card.querySelector('.permission-badge').classList.toggle('write', enabled);
    card.querySelector('.write-settings').hidden = !enabled;
    card.querySelector('.write-roots').required = enabled;
    card.querySelector('.write-roots').disabled = !enabled;
  }

  function projectCount() {
    const count = ui.projects.children.length;
    ui.count.textContent = `${count} 个项目`;
    ui.empty.hidden = count > 0;
  }

  function createProject(project, open = false) {
    const fragment = byId('project-template').content.cloneNode(true);
    const card = fragment.querySelector('.project');
    const sequence = ++cardSequence;
    card.dataset.key = String(sequence);
    projectOriginals.set(card, clone(project));
    card.open = open;
    const binding = [
      ['name', '.project-name-input', '.name-label'],
      ['id', '.project-id-input', '.id-label'],
      ['path', '.project-path-input', '.path-label'],
      ['write-roots', '.write-roots', '.roots-label'],
      ['agent-profiles', '.project-agent-profiles', '.profiles-label'],
    ];
    for (const [key, inputSelector, labelSelector] of binding) {
      const input = card.querySelector(inputSelector);
      input.id = `project-${sequence}-${key}`;
      input.name = input.id;
      card.querySelector(labelSelector).htmlFor = input.id;
    }
    card.querySelector('.project-name-input').value = project.name || '';
    card.querySelector('.project-id-input').value = project.id || '';
    card.querySelector('.project-path-input').value = project.path || '';
    card.querySelector('.write-roots').value = (project.write_roots || ['docs']).join('\n');
    const readOnly = card.querySelector('.read-only');
    const controlled = card.querySelector('.controlled-write');
    readOnly.name = controlled.name = `permission-${sequence}`;
    readOnly.checked = !project.write_enabled;
    controlled.checked = Boolean(project.write_enabled);
    card.querySelector('.project-agent-enabled').checked = project.agent?.context_enabled === true;
    card.querySelector('.project-agent-profiles').value = (project.agent?.allowed_profiles || []).join('\n');
    card.addEventListener('input', () => {
      for (const input of card.querySelectorAll('input, textarea')) input.setCustomValidity('');
      refreshProject(card);
      markDirty();
    });
    card.addEventListener('change', () => { refreshProject(card); markDirty(); });
    card.addEventListener('toggle', () => {
      if (!card.open || !card.isConnected) return;
      const listBounds = ui.projects.getBoundingClientRect();
      const cardBounds = card.getBoundingClientRect();
      if (cardBounds.top < listBounds.top || cardBounds.bottom > listBounds.bottom) {
        ui.projects.scrollTop += cardBounds.top - listBounds.top;
      }
    });
    card.querySelector('.project-path-input').addEventListener('blur', () => {
      const path = card.querySelector('.project-path-input').value.trim();
      const name = card.querySelector('.project-name-input');
      const id = card.querySelector('.project-id-input');
      if (path && !name.value.trim()) {
        name.value = path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '';
        if (!id.value.trim()) {
          const preferred = name.value.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '');
          const used = new Set([...ui.projects.querySelectorAll('.project-id-input')].map((input) => input.value));
          id.value = preferred && !used.has(preferred) ? preferred : `project-${sequence}`;
        }
        refreshProject(card);
        markDirty();
      }
    });
    card.querySelector('.remove-project').addEventListener('click', () => {
      card.remove();
      projectCount();
      markDirty();
      ui.add.focus();
    });
    refreshProject(card);
    ui.projects.append(fragment);
    projectCount();
    return card;
  }

  function applyConfig(result) {
    revision = result.revision;
    loadedData = clone(result.data);
    loadedData.startup = { ...loadedData.startup, enabled: loadedData.startup?.enabled === true };
    renderAgentConfig();
    ui.projects.replaceChildren();
    const projects = loadedData.projects || [];
    for (const project of projects) createProject(project);
    if (!projects.length) projectCount();
    ui.port.value = loadedData.server?.port ?? 8168;
    ui.path.value = loadedData.server?.path || '/mcp';
    const mode = loadedData.tunnel?.mode || 'off';
    let namedOption = ui.mode.querySelector('option[value="named"]');
    if (mode === 'named' && !namedOption) {
      namedOption = document.createElement('option');
      namedOption.value = 'named';
      namedOption.textContent = 'Cloudflare Named · 需要更换';
      ui.mode.append(namedOption);
    } else if (mode !== 'named' && namedOption) {
      namedOption.remove();
    }
    ui.mode.value = mode;
    ui.publicUrl.value = loadedData.tunnel?.public_url || '';
    ui.startup.checked = loadedData.startup.enabled;
    tunnelHelp();
    dirty = false;
    pendingExternalRevision = null;
    ui.conflict.hidden = true;
    ready = true;
    controls();
    if (result.config_error) {
      message(`磁盘配置有问题，当前显示的是最近可用配置。\n${typeof result.config_error === 'string' ? result.config_error : explain(result.config_error)}`, 'warning');
    }
  }

  async function loadConfig() {
    const result = await api('/api/config');
    if (!result.data || result.revision === undefined) throw new Error('本机服务没有返回有效的配置版本。');
    applyConfig(result);
  }

  function collectConfig() {
    const data = clone(loadedData);
    data.projects = [...ui.projects.children].map((card) => ({
      ...clone(projectOriginals.get(card) || {}),
      id: card.querySelector('.project-id-input').value.trim(),
      name: card.querySelector('.project-name-input').value.trim(),
      path: card.querySelector('.project-path-input').value.trim(),
      write_enabled: card.querySelector('.controlled-write').checked,
      write_roots: card.querySelector('.write-roots').value.split(/\r?\n/).map((root) => root.trim()).filter(Boolean),
      ...(projectOriginals.get(card)?.agent || agentSupported ? { agent: {
        ...clone(projectOriginals.get(card)?.agent || {}),
        context_enabled: card.querySelector('.project-agent-enabled').checked,
        allowed_profiles: card.querySelector('.project-agent-profiles').value.split(/\r?\n/).map((v) => v.trim()).filter(Boolean),
      }} : {}),
    }));
    data.server = { ...data.server, port: Number(ui.port.value), path: ui.path.value.trim() };
    data.tunnel = { ...data.tunnel, mode: ui.mode.value, public_url: ui.publicUrl.value.trim() };
    data.startup = { ...data.startup, enabled: ui.startup.checked };
    if (data.agent && agentSupported) data.agent = collectAgent();
    return data;
  }

  function hasUnsavedNonTunnelConfig() {
    if (!ready || !dirty) return false;
    const candidate = collectConfig();
    const current = clone(loadedData);
    delete candidate.tunnel;
    delete current.tunnel;
    return JSON.stringify(candidate) !== JSON.stringify(current);
  }

  function validateForm() {
    const invalidAgentField = byId('agent-settings').querySelector(':invalid');
    if (invalidAgentField) {
      byId('agent-settings-disclosure').open = true;
      const profile = invalidAgentField.closest('.agent-profile');
      const advanced = invalidAgentField.closest('details.advanced');
      if (profile) profile.open = true;
      if (advanced) advanced.open = true;
    }
    if (!validateAgent()) return false;
    const ids = new Set();
    for (const card of ui.projects.children) {
      const id = card.querySelector('.project-id-input');
      id.setCustomValidity(ids.has(id.value.trim()) ? '项目 ID 重复，请改为唯一的 ID。' : '');
      ids.add(id.value.trim());
      const roots = card.querySelector('.write-roots');
      if (!roots.disabled) {
        const values = roots.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
        const badRoot = values.some((value) => value.replace(/\\/g, '/').split('/').some((part) => !part || part === '.' || part === '..') || /[:*]/.test(value));
        roots.setCustomValidity(!values.length ? '请至少填写一个可写目录，例如 docs。' : badRoot ? '可写目录必须是干净的相对路径，不能含 ..、绝对路径或通配符。' : '');
      }
      const invalid = card.querySelector(':invalid');
      if (invalid) card.open = true;
    }
    if (document.querySelector('.advanced :invalid')) document.querySelector('.advanced').open = true;
    return ui.form.reportValidity();
  }

  async function saveConfig(event) {
    event.preventDefault();
    if (!ready || busy || !dirty || !validateForm()) return;
    if (pendingExternalRevision && pendingExternalRevision !== revision) {
      ui.conflict.hidden = false;
      message('当前版本已过期。请重新读取最新配置，合并修改后再保存。', 'warning');
      return;
    }
    busy = true;
    controls();
    message('正在保存…');
    try {
      const result = await api('/api/config', { method: 'PUT', data: { revision, data: collectConfig() }, timeout: 65000 });
      if (result.ok !== true || result.applied?.verified !== true || !result.data || result.revision === undefined) {
        throw Object.assign(new Error('后端未确认配置已经生效。'), { hint: '重新读取配置并查看 Local MCP 状态，再决定是否重试。' });
      }
      applyConfig(result);
      agentSupported = compatible && Boolean(loadedData?.agent);
      controls();
      if (compatible && byId('agent-settings-disclosure').open) await readAgentCache();
      message('已生效。');
      await pollStatus();
    } catch (error) {
      if (error.status === 409 || error.code === 'REVISION_CONFLICT') ui.conflict.hidden = false;
      message(explain(error), 'error');
      if (error.code === 'REQUEST_TIMEOUT') ui.conflict.hidden = false;
    } finally {
      busy = false;
      controls();
    }
  }

  function cloudflareMessage(text, error = false) {
    ui.cloudflareMessage.textContent = text;
    ui.cloudflareMessage.className = `tunnel-message${error ? ' error' : ''}`;
    ui.cloudflareMessage.hidden = !text;
    ui.cloudflareMessage.setAttribute('role', error ? 'alert' : 'status');
  }

  async function refreshCloudflareInfo() {
    try {
      const info = await api('/api/cloudflare');
      if (typeof info.agent_available === 'boolean') ui.cloudflarePrerequisite.hidden = info.agent_available;
    } catch { /* The activation endpoint gives an actionable installation error. */ }
  }

  async function enableCloudflare() {
    if (!ready || busy) return;
    if (hasUnsavedNonTunnelConfig()) {
      cloudflareMessage('请先保存项目、端口或开机启动的修改，再启用临时连接。', true);
      return;
    }
    if (pendingExternalRevision && pendingExternalRevision !== revision) {
      ui.conflict.hidden = false;
      cloudflareMessage('配置已在别处修改。请先重新读取最新配置。', true);
      return;
    }
    busy = true;
    activeTunnelAction = 'cloudflare';
    controls();
    cloudflareMessage('正在启动 Cloudflare，并验证公网地址…');
    try {
      const result = await api('/api/cloudflare', { method: 'POST', data: { revision }, timeout: 90000 });
      if (result.ok !== true || result.applied?.verified !== true || !tunnelAccepted(result.applied) || result.data?.tunnel?.mode !== 'cloudflare' || typeof result.applied?.public_url !== 'string' || !result.applied.public_url || result.revision === undefined) {
        throw Object.assign(new Error('尚未确认临时连接已经生效。'), { hint: '请查看连接状态，再重试；持续接入建议使用 ngrok。' });
      }
      applyConfig(result);
      const status = await api('/api/status');
      renderStatus(status);
      const needsAuth = result.applied.connection_state === 'auth_required';
      if ((!needsAuth && stateClass(status.public?.state) !== 'up') || !ui.mcpUrl.value || status.public.url !== result.applied.public_url) {
        throw Object.assign(new Error('配置已应用，公网地址尚未确认在线。'), { hint: '请立即检测或重试；仍无法连接时可改用 ngrok。' });
      }
      cloudflareMessage(needsAuth ? '连接地址与认证发现已验证；请在 ChatGPT 完成 OAuth 授权。工具调用尚未验证。' : '已连接。复制已验证的公网地址，填入 ChatGPT。');
    } catch (error) {
      if (error.status === 409 || error.code === 'REVISION_CONFLICT' || error.code === 'REQUEST_TIMEOUT') ui.conflict.hidden = false;
      cloudflareMessage(`${explain(error)}\n可重试临时连接，或选择推荐的 ngrok。`, true);
      await pollStatus();
    } finally {
      activeTunnelAction = null;
      busy = false;
      controls();
    }
  }

  function ngrokMessage(text, error = false) {
    ui.ngrokMessage.textContent = text;
    ui.ngrokMessage.className = `ngrok-message${error ? ' error' : ''}`;
    ui.ngrokMessage.hidden = !text;
    ui.ngrokMessage.setAttribute('role', error ? 'alert' : 'status');
  }

  async function refreshNgrokInfo() {
    try {
      const info = await api('/api/ngrok');
      if (typeof info.credential_saved !== 'boolean' || typeof info.agent_available !== 'boolean') {
        throw new Error('尚未取得 ngrok 安装和凭据状态。');
      }
      ngrokInfo = info;
      ui.ngrokState.textContent = info.credential_saved ? '本机已保存凭据，可直接启用' : '尚未保存凭据';
      ui.ngrokToken.placeholder = info.credential_saved ? '已有凭据；仅更换时重新粘贴' : '粘贴刚才复制的 authtoken';
      ui.ngrokPrerequisite.hidden = info.agent_available;
      controls();
    } catch (error) {
      ngrokInfo = null;
      ui.ngrokState.textContent = '尚未取得凭据状态';
      ngrokMessage(explain(error), true);
      controls();
    }
  }

  async function enableNgrok() {
    if (!ready || busy) return;
    if (hasUnsavedNonTunnelConfig()) {
      ngrokMessage('请先保存项目、端口或开机启动的修改，再启用 ngrok。', true);
      return;
    }
    if (pendingExternalRevision && pendingExternalRevision !== revision) {
      ui.conflict.hidden = false;
      ngrokMessage('配置已在别处修改。请先重新读取最新配置。', true);
      return;
    }
    let secretText = ui.ngrokToken.value.trim();
    if (!ngrokInfo?.credential_saved && !secretText) {
      ui.ngrokToken.setCustomValidity('请粘贴 ngrok 页面复制的 authtoken。');
      ui.ngrokToken.reportValidity();
      return;
    }
    const data = { revision, public_url: ui.publicUrl.value.trim() };
    if (secretText) data.token = secretText;
    busy = true;
    controls();
    ngrokMessage('正在验证凭据、启动 ngrok，并探测公网 MCP…');
    try {
      const result = await api('/api/ngrok', { method: 'POST', data, timeout: 90000 });
      if (result.ok !== true || result.applied?.verified !== true || !tunnelAccepted(result.applied) || result.credential_saved !== true || !result.data || result.revision === undefined) {
        throw Object.assign(new Error('尚未确认 ngrok 已经生效。'), { hint: '请查看连接状态，再重新检测。' });
      }
      ui.ngrokToken.value = '';
      secretText = '';
      ngrokInfo = { credential_saved: true, agent_available: true };
      applyConfig(result);
      const status = await api('/api/status');
      renderStatus(status);
      const needsAuth = result.applied.connection_state === 'auth_required';
      if ((!needsAuth && stateClass(status.public?.state) !== 'up') || !ui.mcpUrl.value) {
        throw Object.assign(new Error('配置已应用，公网 MCP 尚未确认在线。'), { hint: '请查看 Public URL 状态，再点击立即检测。' });
      }
      ui.ngrokState.textContent = '本机已保存凭据';
      ui.ngrokToken.placeholder = '已有凭据；仅更换时重新粘贴';
      ngrokMessage(needsAuth ? '连接地址与认证发现已验证；请在 ChatGPT 完成 OAuth 授权。工具调用尚未验证。' : '已连接。复制页面中的公网 MCP 地址，即可接入 ChatGPT。');
    } catch (error) {
      if (error.status === 409 || error.code === 'REVISION_CONFLICT') ui.conflict.hidden = false;
      const explanation = explain(error);
      ngrokMessage(secretText ? explanation.split(secretText).join('[凭据已隐藏]') : explanation, true);
    } finally {
      secretText = '';
      delete data.token;
      busy = false;
      controls();
    }
  }

  function timeMs(value) {
    if (value === null || value === undefined || value === '') return NaN;
    if (typeof value === 'number') return value < 100000000000 ? value * 1000 : value;
    return Date.parse(value);
  }
  function atTime(value) {
    const time = timeMs(value);
    if (!Number.isFinite(time)) return '暂无';
    const date = new Date(time);
    const clock = date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    return date.toDateString() === new Date().toDateString() ? clock : `${date.toLocaleDateString('zh-CN')} ${clock}`;
  }
  function latency(value) {
    return Number.isFinite(value) ? `${Math.round(value)} ms` : '未测得延迟';
  }
  function stateClass(value) {
    if (value === true || ['online', 'up', 'active', 'running', 'connected', 'healthy', 'ok'].includes(value)) return 'up';
    if (value === false || ['offline', 'down', 'failed', 'error', 'disconnected', 'stopped'].includes(value)) return 'down';
    if (['starting', 'reconnecting', 'checking', 'pending', 'degraded'].includes(value)) return 'waiting';
    return 'unknown';
  }
  function stateLabel(value, layer) {
    const labels = {
      online: '在线', up: '在线', active: '运行中', running: '运行中', connected: '已连接', healthy: '在线', ok: '在线',
      offline: '离线', down: '断连', failed: '失败', error: '失败', disconnected: '已断开', stopped: '已停止',
      starting: '启动中', reconnecting: '重连中', checking: '检测中', pending: '等待检测', degraded: '连接不稳定',
      off: '未启用', disabled: '未启用', unknown: '待检测', unconfigured: '未配置', unavailable: '不可用',
      auth_required: '需要授权', forbidden: '权限不足', protocol_error: '协议异常', invalid: '协议异常',
    };
    if (value === true) return '在线';
    if (value === false) return '断连';
    return labels[value] || (layer === 'tunnel' ? '待确认' : '待检测');
  }

  function setLayer(layer, state, text, detail, hint = '') {
    const row = document.querySelector(`[data-layer="${layer}"]`);
    row.dataset.state = stateClass(state);
    row.querySelector('.status-value').textContent = text;
    row.querySelector('.status-detail').textContent = detail;
    const hintNode = row.querySelector('.status-hint');
    hintNode.textContent = hint;
    hintNode.hidden = !hint || layer === 'activity' || !(['down', 'waiting'].includes(stateClass(state)) || state === 'unavailable');
  }

  function renderStatus(status) {
    lastStatus = status;
    const local = status.local || {};
    const localAddress = typeof local.url === 'string' ? local.url : '';
    ui.localUrl.value = localAddress;
    ui.copyLocal.disabled = !localAddress;
    const tunnel = status.tunnel || {};
    const publicProbe = status.public || {};
    const activity = status.activity || {};
    const startup = status.startup;
    byId('core-availability').textContent = stateClass(local.state) === 'up'
      ? 'Core 在线 · 本机 MCP 已验证，不依赖 Codex 或 OAuth。' : 'Core 尚未确认在线 · 请查看 Local MCP。';
    byId('connection-recovery').textContent = stateClass(local.state) !== 'up'
      ? '本机 MCP 尚未恢复；保留已有配置，请查看 Local MCP 详情。'
      : stateClass(tunnel.state) === 'waiting' ? '隧道正在恢复。保留当前 ChatGPT 连接，等待下一次探测。'
      : tunnel.mode === 'off' ? '当前仅供本机客户端使用；ChatGPT 网页需要公网连接。'
      : stateClass(publicProbe.state) !== 'up' ? '公网连接尚未恢复。无需重新填写凭据，请查看 Tunnel 与 Public URL。'
      : '公网探测已通过。若 ChatGPT 会话仍不可用，重新选择已有应用后调用 list_projects；客户端状态需在 ChatGPT 确认。';
    if (status.agent) renderAgentStatus(status.agent);
    const auth = status.auth || {};
    const authNote = document.querySelector('.auth-note');
    const instructions = document.querySelector('.chatgpt-steps li:last-child');
    if (auth.mode === 'oauth') {
      authNote.textContent = auth.chatgpt_connection === 'verified' ? '当前 ChatGPT 授权已有验证记录；URL 或身份变化后需重新核验。'
        : '此连接需要 OAuth 授权。网络可达不代表已授权，请在 ChatGPT 完成连接授权。';
      instructions.textContent = '点新增（＋），填写上方地址，选择 OAuth，按提示完成授权并检查工具。';
    } else if (auth.mode === 'none') {
      authNote.textContent = '当前 MCP 无身份验证。持有公网地址的人可调用已授权项目，请只分享给可信客户端。';
      instructions.textContent = '点新增（＋），填写上方公网地址，按当前服务选择无身份验证并检查工具。';
    } else {
      authNote.textContent = '认证状态尚未确认；不要把公网地址或在线状态视为身份授权。';
      instructions.textContent = '点新增（＋），填写上方地址，按服务确认的认证方式完成连接。';
    }
    if (startup && typeof startup.supported === 'boolean') {
      startupSupported = startup.supported;
      const configured = startup.enabled === true;
      const verified = configured && startup.registered === true && startup.verified === true;
      ui.startupStatus.textContent = startup.message || (!startup.supported ? '此系统暂不支持自动启动。' : configured ? verified ? '已开启，下次登录 Windows 后自动启动。' : '已选择开启，尚未确认启动项。' : '未开启。更改后请保存配置。');
      ui.startupStatus.classList.toggle('startup-warning', startup.verified === false);
      ui.startup.disabled = !ready || !compatible || busy || !startup.supported;
    }
    setLayer('local', local.state, `${stateLabel(local.state, 'local')}${Number.isFinite(local.latency_ms) ? ` · ${latency(local.latency_ms)}` : ''}`, local.url || '本机 MCP 服务', local.hint);
    const tunnelMode = { cloudflare: 'Cloudflare 临时隧道', ngrok: 'ngrok', named: 'Cloudflare Named', off: '仅本机' }[tunnel.mode] || '隧道进程';
    setLayer('tunnel', tunnel.state, stateLabel(tunnel.state, 'tunnel'), `${tunnelMode}${tunnel.pid ? ` · PID ${tunnel.pid}` : ''}`, tunnel.hint);
    setLayer('public', publicProbe.state, `${stateLabel(publicProbe.state, 'public')}${Number.isFinite(publicProbe.latency_ms) ? ` · ${latency(publicProbe.latency_ms)}` : ''}`, publicProbe.url || '尚无公网 MCP 地址', publicProbe.hint);
    const count = activity.count ?? activity.total_calls ?? 0;
    const last = activity.last_call || activity.last_call_at;
    const lastTime = typeof last === 'object' && last !== null ? last.time || last.at || last.timestamp : last;
    const lastTool = typeof last === 'object' && last !== null ? last.tool || last.name : activity.last_tool || activity.tool;
    setLayer('activity', lastTime ? 'online' : 'unknown', lastTime ? `最近调用 ${atTime(lastTime)}` : '尚无工具调用', `${count} 次实际调用${lastTool ? ` · ${lastTool}` : ''}`, '探测请求不计入实际工具调用。');
    const url = publicProbe.url || '';
    const activeMode = tunnel.mode || loadedData?.tunnel?.mode;
    const matchingProvider = activeMode === ui.mode.value && !['off', 'named'].includes(activeMode);
    let usableUrl = false;
    try {
      const parsed = new URL(url);
      const cloudflareAddress = parsed.hostname.endsWith('.trycloudflare.com');
      usableUrl = matchingProvider && parsed.protocol === 'https:' && (activeMode === 'cloudflare' ? cloudflareAddress : !cloudflareAddress);
    } catch { /* No valid address for this provider yet. */ }
    ui.mcpUrl.value = usableUrl ? url : '';
    ui.copy.disabled = !usableUrl;
    if (!usableUrl && url) {
      document.querySelector('[data-layer="public"] .status-detail').textContent = matchingProvider ? '尚未取得当前连接的有效公网地址。' : '此探测属于当前已启用的连接；请启用所选方式后获取新地址。';
    }
    byId('mcp-url-status').textContent = usableUrl ? stateClass(publicProbe.state) === 'up' ? '公网地址已验证，可以填入 ChatGPT。' : '公网地址尚未验证，请查看连接状态。' : '完成连接配置后获取公网地址。';
    byId('mcp-url-status').classList.toggle('url-warning', usableUrl && stateClass(publicProbe.state) !== 'up');
    const sampledAt = status.time || status.updated_at || status.history?.at(-1)?.time;
    ui.freshness.textContent = sampledAt ? `最近探测 ${atTime(sampledAt)}` : '等待首次探测';
    ui.statusError.hidden = true;
    renderHistory(status.history || [], sampledAt);
    renderEvents(status);
    const currentRevision = status.config?.revision;
    if (ready && currentRevision && currentRevision !== revision) {
      pendingExternalRevision = currentRevision;
      ui.conflict.hidden = false;
    }
    if (status.config?.error) {
      ui.statusError.textContent = typeof status.config.error === 'string' ? `配置未生效：${status.config.error}` : explain(status.config.error);
      ui.statusError.hidden = false;
    }
  }

  function svgNode(tag, attrs, text) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderHistory(history, probeTime) {
    const latest = timeMs(probeTime);
    const end = Number.isFinite(latest) ? latest : Date.now();
    const start = end - 300000;
    const samples = history.map((item) => ({ ...item, stamp: timeMs(item.time || item.timestamp) }))
      .filter((item) => Number.isFinite(item.stamp) && item.stamp >= start && item.stamp <= end).sort((a, b) => a.stamp - b.stamp);
    const plotX = 52, plotWidth = 360;
    ui.chart.replaceChildren();
    for (const [layer, label, y] of [['local', 'Local', 13], ['public', 'Public', 39]]) {
      ui.chart.append(svgNode('text', { x: 0, y: y + 9 }, label));
      ui.chart.append(svgNode('rect', { x: plotX, y, width: plotWidth, height: 13, rx: 2, class: 'chart-bg' }));
      for (const item of samples) {
        const state = item[layer]?.state ?? item[layer];
        const x = plotX + ((item.stamp - start) / 300000) * plotWidth;
        const sample = svgNode('rect', { x: Math.min(x, plotX + plotWidth - 3), y: y + 1, width: 3, height: 11, rx: 1, class: `sample-${stateClass(state)}` });
        sample.append(svgNode('title', {}, `${label} ${atTime(item.stamp)}：${stateLabel(state, layer)}；${latency(item[`${layer}_latency_ms`])}`));
        ui.chart.append(sample);
      }
    }
    ui.chart.append(svgNode('text', { x: plotX, y: 74 }, '5 分钟前'));
    ui.chart.append(svgNode('text', { x: plotX + plotWidth, y: 74, 'text-anchor': 'end' }, '现在'));
    ui.chart.setAttribute('aria-label', `最近 5 分钟 Local MCP 与 Public URL 的真实探测，共 ${samples.length} 个采样点。蓝色在线，红色断连，空白尚无采样。`);
    ui.historyNote.textContent = samples.length ? `${samples.length} 次真实采样 · 灰色为未配置或待检测` : '等待真实采样';
  }

  function renderEvents(status) {
    const events = [];
    for (const [layer, label] of [['local', 'Local MCP'], ['public', 'Public URL']]) {
      if (status[layer]?.last_disconnect) events.push({ time: status[layer].last_disconnect, text: `${label} 断连` });
      if (status[layer]?.last_reconnect) events.push({ time: status[layer].last_reconnect, text: `${label} 重连成功` });
    }
    for (const event of status.events || []) {
      const type = { disconnect: '断连', reconnect: '重连成功', online: '恢复在线', offline: '断连', start: '启动', stop: '停止' }[event.type] || event.type;
      events.push({ time: event.time || event.timestamp, text: event.message || `${event.layer || event.component || ''} ${type || ''}`.trim() });
    }
    const unique = new Map(events.filter((event) => Number.isFinite(timeMs(event.time))).map((event) => [`${event.time}-${event.text}`, event]));
    const latest = [...unique.values()].sort((a, b) => timeMs(b.time) - timeMs(a.time)).slice(0, 6);
    ui.events.replaceChildren();
    for (const event of latest) {
      const item = document.createElement('li');
      item.textContent = `${atTime(event.time)} · ${event.text}`;
      ui.events.append(item);
    }
    if (!latest.length) {
      const item = document.createElement('li');
      item.textContent = '尚无断连或重连事件';
      ui.events.append(item);
    }
  }

  async function pollStatus() {
    if (polling) return;
    polling = true;
    try {
      const status = await api('/api/status');
      renderStatus(status);
      if (ready && !busy) {
        const latestConfig = await api('/api/config');
        if (!busy && latestConfig.revision !== revision) {
          if (dirty) {
            pendingExternalRevision = latestConfig.revision;
            ui.conflict.hidden = false;
          } else {
            applyConfig(latestConfig);
          }
        }
        if (latestConfig.config_error) {
          ui.statusError.textContent = typeof latestConfig.config_error === 'string' ? latestConfig.config_error : explain(latestConfig.config_error);
          ui.statusError.hidden = false;
        }
      }
    }
    catch (error) {
      ui.statusError.textContent = explain(error);
      ui.statusError.hidden = false;
      ui.freshness.textContent = '无法获取最新状态 · 下方保留上次探测结果';
      byId('core-availability').textContent = 'Core 当前状态未知 · 上次探测不能代表当前在线。';
      byId('connection-recovery').textContent = '正在等待本机配置服务恢复；输入与配置保留，不要重复提交。';
    } finally { polling = false; }
  }

  async function runtimeAction(path, description) {
    if (!ready || busy) return;
    busy = true;
    controls();
    ui.saveState.textContent = description;
    try {
      const result = await api(path, { method: 'POST', data: {}, timeout: 65000 });
      if (path === '/api/restart' && (result.ok !== true || result.applied?.verified !== true)) throw new Error('后端尚未确认重启完成，请检测 Local MCP 状态。');
      if (result.local || result.status?.local) renderStatus(result.status || result);
      else await pollStatus();
      if (path === '/api/restart') message('已重启。请确认当前 MCP 地址。');
    } catch (error) { message(explain(error), 'error'); }
    finally { busy = false; controls(); }
  }
  function tunnelAccepted(applied) {
    return applied?.public_verified === true || (applied?.connection_state === 'auth_required'
      && applied.public_verified === false && applied.public_reachable === true && applied.auth_discovery_verified === true);
  }

  const profileOriginals = new WeakMap();
  const agentNote = (text, error = false) => {
    const node = byId('agent-message');
    node.textContent = text;
    node.hidden = !text;
    node.className = `message${error ? ' error' : ''}`;
    node.setAttribute('role', error ? 'alert' : 'status');
  };
  function options(select, rows, current) {
    select.replaceChildren();
    for (const [value, text] of rows) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = text;
      select.append(option);
    }
    if (current !== undefined && !rows.some(([value]) => value === current)) {
      const option = document.createElement('option');
      option.value = current;
      option.textContent = `${current || '模型默认'} · 未验证，保留原选择`;
      select.append(option);
    }
    if (current !== undefined) select.value = current;
  }
  function catalogModels() {
    const mode = byId('agent-auth-mode').value;
    if (catalog?.auth_mode !== mode || catalog.provider !== byId('agent-api-provider').value) return [];
    return (catalog.models || []).filter((model) => model.auth_mode === mode);
  }
  function modelDetails(card) {
    const current = card.querySelector('.profile-model').value.trim();
    const model = catalogModels().find((item) => item.model_id === current);
    const candidate = card.querySelector('.profile-candidate');
    options(candidate, [['', '选择候选或下方手填'], ...catalogModels().map((item) => [item.model_id, item.display_name])],
      model ? current : '');
    const effort = card.querySelector('.profile-effort');
    const tier = card.querySelector('.profile-tier');
    const effortValue = effort.value, tierValue = tier.value;
    const effortLabels = { minimal: '最少', low: '低', medium: '中', high: '高', xhigh: '很高' };
    options(effort, [['', '模型默认（明确使用模型默认）'], ...(model?.reasoning_efforts || []).map((v) => [v, `${effortLabels[v] || v} · ${v}`])], effortValue);
    options(tier, [['standard', '标准速度'], ...(model?.service_tiers || []).filter((v) => v !== 'standard').map((v) => [v, `${v} · 可能增加消耗`])], tierValue || 'standard');
    const availability = { unverified: '候选尚未试跑', verified: '有历史验证记录（仅同一身份与参数有效）', unavailable: '当前不可用', unknown: '权限未知' };
    const compatibility = { supported: '参数兼容', unsupported: '参数不支持', unknown: '兼容性未知' };
    card.querySelector('.profile-verification').textContent = model
      ? `${availability[model.availability] || '权限未知'}；${compatibility[model.compatibility] || '兼容性未知'}。${model.reason || ''} 验证日期：${model.checked_at ? atTime(model.checked_at) : '未知'}。`
      : '手填选择保留；当前身份的权限、参数支持和费用未知。';
  }
  function createProfile(id, profile) {
    const card = document.createElement('details');
    card.className = 'agent-profile';
    card.dataset.profile = id;
    card.open = byId('agent-enabled').checked && id === loadedData?.agent?.default_profile;
    profileOriginals.set(card, clone(profile));
    const summary = document.createElement('summary');
    summary.textContent = id;
    card.append(summary);
    const grid = document.createElement('div');
    grid.className = 'fields two-fields';
    for (const [name, label, type] of [['candidate', '模型候选', 'select'], ['model', '模型 ID（可手填）', 'input'],
      ['effort', '推理强度', 'select'], ['tier', '速度', 'select'], ['timeout', '阅读超时（秒）', 'input']]) {
      const group = document.createElement('div');
      const input = document.createElement(type);
      input.className = `profile-${name}`;
      input.id = `agent-profile-${++cardSequence}-${name}`;
      const title = document.createElement('label');
      title.htmlFor = input.id;
      title.textContent = label;
      if (type === 'input') input.type = name === 'timeout' ? 'number' : 'text';
      if (name === 'timeout') { input.min = '60'; input.max = '7200'; input.value = profile.timeout_seconds ?? 1200; }
      if (name === 'model') { input.value = profile.model || ''; input.maxLength = 128; input.spellcheck = false; }
      if (name === 'effort') options(input, [[profile.reasoning_effort || '', profile.reasoning_effort || '模型默认']]);
      if (name === 'tier') options(input, [[profile.service_tier || 'standard', profile.service_tier || '标准速度']]);
      group.append(title, input);
      grid.append(group);
    }
    const verification = document.createElement('p');
    verification.className = 'field-help profile-verification';
    card.append(grid, verification);
    const remove = document.createElement('button');
    remove.type = 'button'; remove.className = 'text-button danger'; remove.textContent = '移除此档案';
    remove.addEventListener('click', () => {
      if (byId('agent-profiles').children.length <= 1) return agentNote('至少保留一个阅读档案。', true);
      const referenced = [...ui.projects.children].some((item) => item.querySelector('.project-agent-profiles').value.split(/\r?\n/).map((v) => v.trim()).includes(id));
      if (byId('agent-default-profile').value === id || referenced) return agentNote('该档案仍是默认选择或被项目引用，请先修改引用。', true);
      card.remove(); refreshProfileChoices(); markDirty();
    });
    card.append(remove);
    card.querySelector('.profile-candidate').addEventListener('change', (event) => {
      if (!event.target.value) return;
      card.querySelector('.profile-model').value = event.target.value;
      modelDetails(card); markDirty();
    });
    card.querySelector('.profile-model').addEventListener('input', () => modelDetails(card));
    byId('agent-profiles').append(card);
    modelDetails(card);
  }
  function refreshProfileChoices(selected = byId('agent-default-profile').value) {
    options(byId('agent-default-profile'), [...byId('agent-profiles').children].map((card) => [card.dataset.profile, card.dataset.profile]), selected);
  }
  function renderAgentConfig() {
    const agent = loadedData?.agent;
    byId('agent-enabled').checked = agent?.enabled === true;
    byId('agent-auth-mode').value = agent?.codex?.auth_mode || 'unselected';
    byId('agent-executable').value = agent?.codex?.executable || '';
    byId('agent-api-provider').value = agent?.codex?.api_provider || 'openai';
    byId('agent-api-url').value = agent?.codex?.api_base_url || 'https://api.openai.com/v1';
    byId('agent-endpoint-consent').checked = false;
    byId('agent-test-consent').checked = false;
    byId('agent-profiles').replaceChildren();
    for (const [id, profile] of Object.entries(agent?.profiles || {})) createProfile(id, profile);
    refreshProfileChoices(agent?.default_profile);
    const values = [['agent-max-running', agent?.limits?.max_running ?? 2], ['agent-max-queued', agent?.limits?.max_queued ?? 20],
      ['agent-max-response', agent?.limits?.max_response_chars ?? 32000], ['agent-max-wait', agent?.limits?.max_wait_seconds ?? 20],
      ['agent-retention-days', agent?.retention?.days ?? 30], ['agent-storage-bytes', agent?.retention?.max_storage_bytes ?? 1073741824]];
    for (const [id, value] of values) byId(id).value = value;
    agentDisclosure();
    agentModeChanged();
  }
  function agentDisclosure() {
    const enabled = byId('agent-enabled').checked;
    byId('agent-settings-disclosure').open = enabled;
    document.querySelector('.agent-model-test').hidden = !enabled;
    if (enabled) for (const card of byId('agent-profiles').children) card.open = card.dataset.profile === byId('agent-default-profile').value;
  }
  function agentModeChanged() {
    const mode = byId('agent-auth-mode').value;
    byId('agent-api-secret').hidden = mode !== 'api_key';
    byId('agent-api-key').value = '';
    byId('agent-test-consent').checked = false;
    byId('agent-billing').textContent = mode === 'chatgpt'
      ? '使用本机已登录的 ChatGPT / Codex 额度。候选不保证套餐权限；不会回退到 API Key。'
      : mode === 'api_key' ? '使用专用 API Key 对应端点的 API 费用。用量与费用未知；不会回退到套餐账号。'
        : '尚未选择额度来源。不会自动使用任何已有登录或 Key。';
    for (const card of byId('agent-profiles').children) modelDetails(card);
  }
  function collectAgent() {
    const original = loadedData.agent;
    const profiles = Object.fromEntries([...byId('agent-profiles').children].map((card) => [card.dataset.profile, {
      ...clone(profileOriginals.get(card)), model: card.querySelector('.profile-model').value.trim(),
      reasoning_effort: card.querySelector('.profile-effort').value, service_tier: card.querySelector('.profile-tier').value,
      timeout_seconds: Number(card.querySelector('.profile-timeout').value),
    }]));
    return { ...clone(original), enabled: byId('agent-enabled').checked,
      codex: { ...original.codex, enabled: byId('agent-enabled').checked, auth_mode: byId('agent-auth-mode').value,
        executable: byId('agent-executable').value.trim(), api_provider: byId('agent-api-provider').value,
        api_base_url: byId('agent-api-url').value.trim() },
      default_profile: byId('agent-default-profile').value, profiles,
      limits: { ...original.limits, max_running: Number(byId('agent-max-running').value),
        max_queued: Number(byId('agent-max-queued').value), max_response_chars: Number(byId('agent-max-response').value), max_wait_seconds: Number(byId('agent-max-wait').value) },
      retention: { ...original.retention, days: Number(byId('agent-retention-days').value), max_storage_bytes: Number(byId('agent-storage-bytes').value) } };
  }
  function validateAgent() {
    if (!agentSupported) return true;
    const agent = collectAgent();
    if (!Object.hasOwn(agent.profiles, agent.default_profile)) { agentNote('默认阅读档案不存在，请重新选择。', true); return false; }
    if (agent.enabled && agent.codex.auth_mode === 'unselected') { agentNote('启用前请选择额度来源。', true); return false; }
    if (agent.codex.api_base_url !== loadedData.agent.codex.api_base_url || agent.codex.api_provider !== loadedData.agent.codex.api_provider) {
      if (!byId('agent-endpoint-consent').checked) { agentNote('API 端点已变更，请确认凭据目的地后保存。', true); return false; }
    }
    for (const [id, profile] of Object.entries(agent.profiles)) {
      if (agent.enabled && !profile.model.trim()) { agentNote(`档案 ${id} 缺少模型 ID；关闭时可以保存草稿。`, true); return false; }
      const model = catalogModels().find((item) => item.model_id === profile.model);
      if (agent.enabled && model && (model.compatibility === 'unsupported'
        || (model.reasoning_effort_status === 'not_supported' && profile.reasoning_effort)
        || (model.reasoning_effort_status === 'known' && profile.reasoning_effort && !model.reasoning_efforts.includes(profile.reasoning_effort))
        || (model.service_tiers.length && !model.service_tiers.includes(profile.service_tier)))) {
        agentNote(`档案 ${id} 的参数不支持。原选择已保留，请明确修改；不会自动改回默认。`, true); return false;
      }
    }
    for (const card of ui.projects.children) {
      const allowed = card.querySelector('.project-agent-profiles').value.split(/\r?\n/).map((v) => v.trim()).filter(Boolean);
      if (allowed.some((id) => !Object.hasOwn(agent.profiles, id))) { agentNote('项目引用了不存在的阅读档案，请修正后保存。', true); card.open = true; return false; }
      if (card.querySelector('.project-agent-enabled').checked && !allowed.length) { agentNote('允许项目阅读时，至少选择一个阅读档案。', true); card.open = true; return false; }
    }
    return true;
  }
  function agentControls(writable) {
    const available = writable && agentSupported && !agentBusy;
    byId('agent-enabled').disabled = !available;
    byId('agent-settings').disabled = !available;
    for (const card of ui.projects.children) for (const input of card.querySelectorAll('.project-agent input,.project-agent textarea')) input.disabled = !available;
    if (!agentSupported) byId('agent-availability').textContent = '当前服务尚未提供 Codex 设置能力；原有配置仍可使用。';
    else if (lastStatus?.agent) renderAgentStatus(lastStatus.agent);
    else byId('agent-availability').textContent = '设置可编辑；执行能力尚未验证。';
    const clean = available && !dirty;
    byId('agent-refresh-models').disabled = !clean || !byId('agent-enabled').checked;
    byId('agent-probe').disabled = !clean || !byId('agent-enabled').checked;
    byId('agent-save-key').disabled = !clean || byId('agent-auth-mode').value !== 'api_key' || !credential;
    byId('agent-delete-key').disabled = !clean || !credential?.configured;
    const profile = loadedData?.agent?.default_profile;
    const projects = (loadedData?.projects || []).filter((p) => p.agent?.context_enabled && p.agent.allowed_profiles?.includes(profile));
    const selectedProject = byId('agent-test-project').value;
    options(byId('agent-test-project'), projects.map((p) => [p.id, p.name]), projects.some((p) => p.id === selectedProject) ? selectedProject : projects[0]?.id);
    byId('agent-model-test').disabled = !clean || loadedData?.agent?.enabled !== true || !projects.length
      || !byId('agent-test-consent').checked || !credential || loadedData.agent.codex.auth_mode === 'unselected'
      || (loadedData.agent.codex.auth_mode === 'api_key' && credential.configured !== true);
    byId('agent-settings').setAttribute('aria-busy', agentBusy ? 'true' : 'false');
  }
  async function readAgentCache() {
    if (!loadedData?.agent || agentCachePending) return;
    agentCachePending = true;
    try {
      const [keyResult, modelResult] = await Promise.all([api('/api/agent/credential'), api('/api/agent/models')]);
      if (keyResult.ok !== true || !keyResult.credential?.credential_revision || modelResult.ok !== true
        || !Array.isArray(modelResult.catalog?.models) || modelResult.catalog.model_request_performed !== false) throw new Error('能力返回格式不兼容。');
      credential = keyResult.credential; catalog = modelResult.catalog; agentSupported = true;
      renderCredential();
      for (const card of byId('agent-profiles').children) modelDetails(card);
      byId('agent-catalog-note').textContent = [catalog.warning, '候选不是权限证明；手填 ID 始终保留。目录读取不会试跑模型。'].filter(Boolean).join(' ');
    } catch (error) { agentSupported = false; agentNote(`Codex 设置尚不可用：${explain(error)}`, true); }
    agentCachePending = false;
    controls();
  }
  function renderCredential() {
    const states = { configured: '已保存', missing: '未保存', invalid: '凭据失效', endpoint_mismatch: '端点已改变，需要重新绑定', unavailable: '凭据存储不可用', unknown: '状态未知' };
    byId('agent-credential-state').textContent = `Key：${states[credential?.status] || '未知'}。${credential?.bound_endpoint ? `绑定端点：${credential.bound_endpoint}` : ''}`;
  }
  async function agentAction(operation) {
    if (!ready || !compatible || !agentSupported || busy || agentBusy) return;
    if (dirty) return agentNote('先保存当前设置，再执行此操作。', true);
    agentBusy = true; controls(); agentNote('正在处理…');
    try { await operation(); }
    catch (error) { agentNote(explain(error), true); if (error.status === 409) ui.conflict.hidden = false; }
    finally { agentBusy = false; controls(); }
  }
  const requestId = () => crypto.randomUUID();
  byId('agent-add-profile').addEventListener('click', () => {
    const id = window.prompt('阅读档案名称（最多 64 字符）');
    if (!id) return;
    if (id !== id.trim() || id.length > 64 || [...byId('agent-profiles').children].some((card) => card.dataset.profile === id)) return agentNote('档案名称无效或已存在。', true);
    createProfile(id, { backend: 'codex', mode: 'context', model: '', reasoning_effort: '', service_tier: 'standard', timeout_seconds: 1200 });
    byId('agent-profiles').lastElementChild.open = true;
    refreshProfileChoices(); markDirty();
  });
  byId('agent-auth-mode').addEventListener('change', () => { catalog = null; agentModeChanged(); });
  byId('agent-enabled').addEventListener('change', agentDisclosure);
  byId('agent-settings-disclosure').addEventListener('toggle', () => {
    if (ready && compatible && byId('agent-settings-disclosure').open && !credential) readAgentCache();
  });
  byId('agent-api-provider').addEventListener('change', () => { catalog = null; byId('agent-endpoint-consent').checked = false; agentModeChanged(); });
  byId('agent-api-url').addEventListener('input', () => {
    catalog = null; byId('agent-endpoint-consent').checked = false; byId('agent-api-key').value = '';
    for (const card of byId('agent-profiles').children) modelDetails(card);
  });
  byId('agent-test-consent').addEventListener('change', controls);
  document.querySelector('.agent-section').addEventListener('input', (event) => {
    if (['agent-api-key', 'agent-test-consent', 'agent-test-project', 'agent-endpoint-consent'].includes(event.target.id)) return;
    byId('agent-test-consent').checked = false; agentNote(''); markDirty();
  });
  document.querySelector('.agent-section').addEventListener('change', (event) => {
    if (['agent-api-key', 'agent-test-consent', 'agent-test-project', 'agent-endpoint-consent'].includes(event.target.id)) return;
    markDirty();
  });
  byId('agent-refresh-models').addEventListener('click', () => agentAction(async () => {
    const result = await api('/api/agent/models', { method: 'POST', data: { revision, backend: 'codex' } });
    if (result.ok !== true || result.catalog?.model_request_performed !== false) throw new Error('模型目录响应无效，未确认零模型请求。');
    catalog = result.catalog;
    for (const card of byId('agent-profiles').children) modelDetails(card);
    agentNote(catalog.warning || '已刷新候选；未试跑模型，权限仍以验证结果为准。');
  }));
  byId('agent-probe').addEventListener('click', () => agentAction(async () => {
    const result = await api('/api/agent/probe', { method: 'POST', data: { revision, backend: 'codex' }, timeout: 15000 });
    if (result.ok !== true || result.probe?.model_request_performed !== false) throw new Error('检测响应未确认零模型请求。');
    renderAgentStatus(result.probe); agentNote('检测结束；未调用模型。');
  }));
  byId('agent-save-key').addEventListener('click', () => agentAction(async () => {
    const input = byId('agent-api-key');
    if (!input.value.trim()) throw new Error('请填写 API Key。');
    try {
      const result = await api('/api/agent/credential', { method: 'PUT', data: { revision, request_id: requestId(), expected_credential_revision: credential.credential_revision, api_key: input.value } });
      if (result.ok !== true || !result.credential?.credential_revision) throw new Error('凭据保存未被确认。');
      credential = result.credential; catalog = null; renderCredential(); for (const card of byId('agent-profiles').children) modelDetails(card); agentNote('Key 已单独保存，输入已清空；模型权限仍未验证。');
    } catch (error) {
      throw Object.assign(new Error(explain(error).split(input.value).join('[凭据已隐藏]')), { status: error.status, code: error.code });
    } finally { input.value = ''; }
  }));
  byId('agent-delete-key').addEventListener('click', () => {
    if (!window.confirm('删除 Key 将撤销使用它的任务。确认删除？')) return;
    agentAction(async () => {
      const result = await api('/api/agent/credential', { method: 'DELETE', data: { revision, request_id: requestId(), expected_credential_revision: credential.credential_revision } });
      if (result.ok !== true || !result.credential?.credential_revision) throw new Error('凭据撤销未被确认。');
      credential = result.credential; catalog = null; byId('agent-api-key').value = ''; renderCredential(); for (const card of byId('agent-profiles').children) modelDetails(card); agentNote('Key 已删除；历史记录与模型偏好保留。');
    });
  });
  byId('agent-model-test').addEventListener('click', () => {
    if (!byId('agent-test-consent').checked || byId('agent-model-test').disabled) return;
    const project_id = byId('agent-test-project').value, profile = loadedData.agent.default_profile;
    byId('agent-test-consent').checked = false;
    agentAction(async () => {
      const result = await api('/api/agent/model-test', { method: 'POST', data: { revision, request_id: requestId(), project_id, profile, expected_credential_revision: credential.credential_revision, allow_model_request: true } });
      if (result.schema_version !== UI_CONTRACT_VERSION || result.kind !== 'task' || result.task?.purpose !== 'model_test' || !result.task.task_id) throw new Error('未收到受管模型测试任务，不能宣称验证成功。');
      byId('agent-test-result').textContent = `测试已提交：${result.task.task_id}；结果待确认。实际用量与费用未知。`;
      agentNote('已提交一次受管短测试；状态更新只读缓存，不会新建任务。');
    });
  });
  function renderAgentStatus(state) {
    const names = { disabled: '已关闭', not_installed: '未安装 Codex', auth_required: '本机 CLI 尚未登录', unsupported_version: '安装版本不支持', capability_unavailable: '阅读能力不可用', ready: '已有就绪证据', unknown: '状态未知', auth_unselected: '尚未选择额度来源', auth_mismatch: '身份与选择不一致', model_unselected: '尚未选择模型', model_unverified: '模型未验证', model_unavailable: '模型无权限或不可用', parameter_unavailable: '推理或速度参数不支持', credential_unavailable: 'API 凭据不可用' };
    const errors = state?.last_error?.message || (Array.isArray(state?.errors) ? state.errors.map((e) => e.message || '').filter(Boolean).join(' ') : '');
    byId('agent-availability').textContent = state?.availability === 'disabled' ? 'Codex 已关闭。原连接与项目工具继续可用。'
      : `Codex：${names[state?.availability] || '状态未知'}。${errors} 请求模型：${state?.requested_model || '未知'}；推理：${state?.requested_reasoning_effort || '模型默认 / 未知'}；速度：${state?.requested_service_tier || '未知'}；验证日期：${state?.checked_at ? atTime(state.checked_at) : '未知'}。`;
    byId('agent-execution-access').textContent = '未经验证的公网调用不能启动 Codex。本机受信任执行不代表严格沙箱；执行状态以当前检测结果为准。';
    const test = state?.last_model_test;
    if (test) byId('agent-test-result').textContent = `${test.message || '测试结果'}；状态 ${test.status}；验证 ${test.verification}；模型 ${test.model_id}；日期 ${atTime(test.updated_at)}。实际费用与剩余额度未知。`;
  }

  ui.form.addEventListener('submit', saveConfig);
  for (const input of [ui.mode, ui.port, ui.path, ui.publicUrl, ui.startup]) {
    input.addEventListener('input', () => { input.setCustomValidity(''); if (input === ui.mode) tunnelHelp(); markDirty(); });
    input.addEventListener('change', () => { if (input === ui.mode) tunnelHelp(); markDirty(); });
  }
  ui.add.addEventListener('click', () => {
    const card = createProject({ id: '', name: '', path: '', write_enabled: false, write_roots: ['docs'] }, true);
    markDirty();
    card.querySelector('.project-path-input').focus();
  });
  ui.reload.addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    controls();
    try {
      await loadConfig();
      message('已读取最新配置。');
    } catch (error) { message(explain(error), 'error'); }
    finally { busy = false; controls(); }
  });
  ui.probe.addEventListener('click', async () => { await runtimeAction('/api/probe', '正在进行真实连接探测…'); if (!busy) await refreshNgrokInfo(); });
  ui.restart.addEventListener('click', () => runtimeAction('/api/restart', '正在受控重启 MCP 与隧道…'));
  async function copyAddress(input, button) {
    if (!input.value) return;
    const original = button.textContent;
    try {
      await navigator.clipboard.writeText(input.value);
      button.textContent = '已复制';
      window.setTimeout(() => { button.textContent = original; }, 1600);
    } catch {
      input.focus();
      input.select();
      message('地址已选中。请按 Ctrl+C（Mac 上为 ⌘C）复制。');
    }
  }
  ui.copy.addEventListener('click', () => copyAddress(ui.mcpUrl, ui.copy));
  ui.copyLocal.addEventListener('click', () => copyAddress(ui.localUrl, ui.copyLocal));
  ui.ngrokToken.addEventListener('input', () => { ui.ngrokToken.setCustomValidity(''); ngrokMessage(''); });
  ui.ngrokEnable.addEventListener('click', enableNgrok);
  ui.enableCloudflare.addEventListener('click', enableCloudflare);
  ui.ngrokRefresh.addEventListener('click', refreshNgrokInfo);
  window.addEventListener('beforeunload', (event) => {
    if (dirty) { event.preventDefault(); event.returnValue = ''; }
  });

  async function start() {
    try {
      let info = {};
      try { info = await api('/api/info'); } catch { /* Missing handshake stays read-only; try the existing read paths. */ }
      compatible = info.ui_contract_version === UI_CONTRACT_VERSION && info.ui_asset_version === UI_ASSET_VERSION;
      const session = await api('/api/session');
      token = session.token || '';
      if (!token) throw new Error('本机服务没有提供有效的配置会话，请刷新页面。');
      await loadConfig();
      agentSupported = compatible && Boolean(loadedData?.agent);
      controls();
      if (compatible && byId('agent-settings-disclosure').open) await readAgentCache();
      await pollStatus();
      await Promise.all([refreshNgrokInfo(), refreshCloudflareInfo()]);
      if (!compatible) message('页面与服务版本不一致。请刷新页面或更新对应版本；当前只读取配置与状态。', 'warning');
    } catch (error) {
      message(explain(error), 'error');
      ui.saveState.textContent = '尚未读取配置';
      ui.saveDescription.textContent = '确认启动窗口仍在运行，再刷新页面。';
    }
    window.setInterval(() => { if (!busy && !document.hidden) pollStatus(); }, 5000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden && !busy) pollStatus(); });
  }
  start();
})();
