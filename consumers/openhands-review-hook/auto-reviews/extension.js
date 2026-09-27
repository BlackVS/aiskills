export function activate(host) {
  if (host.apiVersion !== "1") throw new Error("Auto Reviews requires Canvas host API 1");
  const modelCache = new Map();
  const cacheKey = provider => JSON.stringify([provider.id, provider.url || null]);
  const request = (path, method = "GET", body) => host.agentServer.request({path, method, ...(body === undefined ? {} : {body})});
  function providerModels(provider) {
    const key = cacheKey(provider);
    if (!modelCache.has(key)) {
      const pending = request('/api/review-control/models?provider=' + encodeURIComponent(provider.id))
        .then(data => {
          if (!data.models.length) throw new Error('This provider returned no models.');
          return data;
        }).catch(error => {
          if (modelCache.get(key) === pending) modelCache.delete(key);
          throw error;
        });
      modelCache.set(key, pending);
    }
    return modelCache.get(key);
  }
  return host.registerPage("settings", ({ container }) => {
    let disposed = false, revision, savedSelection, savedControls, providers = [], selections = {}, busy = false, editingProvider = null;
    let progressTimer, busyStartedAt;
    const root = document.createElement("section");
    root.style.cssText = "max-width:760px;margin:32px auto;padding:24px;font:inherit;line-height:1.6";
    root.className = "auto-reviews";
    root.innerHTML = `<style>
      .auto-reviews h1 {font-size:26px;font-weight:650;margin:0 0 12px;line-height:1.25}
      .auto-reviews h2 {font-size:20px;font-weight:600;margin:28px 0 12px}
      .auto-reviews p {margin:12px 0}
      .auto-reviews fieldset {min-width:0;border:1px solid GrayText;border-radius:10px;padding:12px 16px;margin:20px 0}
      .auto-reviews legend {padding:0 6px;font-weight:600}
      .auto-reviews input,.auto-reviews select {box-sizing:border-box;font:inherit}
      .auto-reviews select option {color:CanvasText;background-color:Canvas}
      .auto-reviews button {display:inline-block;padding:9px 16px;margin:4px 8px 4px 0;border:1px solid GrayText;border-radius:7px;background:ButtonFace;color:ButtonText;font:inherit;font-weight:600;cursor:pointer}
      .auto-reviews [hidden] {display:none}
      .auto-reviews button:hover:not(:disabled) {filter:brightness(1.1)}
      .auto-reviews button:disabled,.auto-reviews select:disabled {opacity:.55;cursor:default}
      .auto-reviews button.needs-save:not(:disabled) {background:Highlight;color:HighlightText;border-color:Highlight}
      .auto-reviews [data-unsaved] {font-size:14px;font-weight:600;margin-left:4px}
      .auto-reviews :is(button,select,input,summary):focus-visible {outline:2px solid Highlight;outline-offset:3px}
      .auto-reviews ul {list-style:none;padding:0;margin:0 0 16px}
      .auto-reviews [data-providers]>li {padding:10px 12px;border:1px solid GrayText;border-radius:7px;margin:8px 0}
      .auto-reviews [data-model-list] {margin:6px 0 0;max-height:300px;overflow:auto}
      .auto-reviews [data-model-list]>li {padding:0;border:0;margin:0;overflow-wrap:anywhere}
      .auto-reviews details {border:1px solid GrayText;border-radius:10px;padding:12px 16px;margin-top:20px}
      .auto-reviews summary {display:list-item;list-style:disclosure-closed inside;font-weight:600;cursor:pointer;padding:4px}
      .auto-reviews details[open]>summary {list-style-type:disclosure-open}
      .auto-reviews [data-feedback] {position:sticky;top:0;z-index:1;display:flex;align-items:center;gap:10px;padding:12px 14px;margin:16px 0;border:1px solid GrayText;border-radius:8px;background:Canvas;color:CanvasText;min-height:24px}
      .auto-reviews [data-spinner] {display:none;width:16px;height:16px;flex-shrink:0;border:2px solid currentColor;border-right-color:transparent;border-radius:50%}
      .auto-reviews[data-busy=true] [data-spinner] {display:inline-block;animation:auto-reviews-spin .8s linear infinite}
      .auto-reviews [data-elapsed] {margin-left:auto;white-space:nowrap;font-size:13px}
      @keyframes auto-reviews-spin {to {transform:rotate(360deg)}}
      @media (prefers-reduced-motion:reduce) {.auto-reviews[data-busy=true] [data-spinner] {animation:none}}
    </style><h1>Auto Reviews</h1>
      <p>Choose a provider, model and reasoning effort for automatic pull request reviews.</p>
      <div data-feedback><span data-spinner aria-hidden="true"></span><span role="status" aria-live="polite">Loading saved settings and provider connections…</span><span data-elapsed aria-hidden="true"></span></div>
      <form data-review><p data-warning="settings" role="alert"></p><fieldset><legend>Primary reviewer</legend><label>Provider <select name="primaryProvider"></select></label><label>Model <select name="primaryModel"></select></label><label>Reasoning effort <select name="primaryEffort"></select></label><p data-warning="primary" role="alert"></p></fieldset>
      <fieldset><legend>Secondary reading profile</legend><label>Provider <select name="secondaryProvider"></select></label><label>Model <select name="secondaryModel"></select></label><label>Reasoning effort <select name="secondaryEffort"></select></label><p data-secondary-hint></p><p data-warning="secondary" role="alert"></p></fieldset>
      <p>With a secondary profile the review runs in combined mode: the conversation reads the PR on the secondary (fast) profile, then switches to the primary for the review pass, verification and write-up, and the primary signs the review. None keeps the single-profile review.</p>
      <fieldset><legend>Fallback reviewer</legend><label>Provider <select name="fallbackProvider"></select></label><label>Model <select name="fallbackModel"></select></label><label>Reasoning effort <select name="fallbackEffort"></select></label><p data-warning="fallback" role="alert"></p></fieldset>
      <p>On a confirmed quota or rate-limit error, retry once with the fallback, then stop.</p>
      <p>Settings apply to new reviews. An explicit reviewer label on a PR overrides the primary; fallback applies only when that reviewer is the configured primary. Canvas's conversation model is independent.</p>
      <button type="submit">Save settings</button> <button type="button" data-revert title="Discard unsaved edits and restore your last loaded or saved selections">Revert changes</button> <button type="button" data-reload title="Fetch saved settings and available models from the server; discards unsaved edits">Reload</button><span data-unsaved aria-live="polite"></span></form>
      <h2>Providers</h2><ul data-providers></ul>
      <details data-provider-editor><summary>Add API provider</summary><form data-provider>
      <label>Name <input name="display_name" required maxlength="128" placeholder="My provider"></label>
      <label>API base URL <input name="base_url" type="url" required placeholder="https://provider.example/v1"></label>
      <label>API key or token <input name="api_key" type="password" required autocomplete="off"></label>
      <p>Use an OpenAI-compatible API endpoint. Credentials are saved in Canvas's provider connections. Claude and Codex use their installed account connections.</p>
      <p>Test connection checks authentication and the model list without sending an inference request. Add provider saves only after this check passes.</p><button type="button" data-test-new>Test connection</button><button type="submit">Add provider</button><button type="button" data-cancel-edit hidden>Cancel edit</button><p data-new-result role="status"></p></form></details>`;
    for (const el of root.querySelectorAll("label")) el.style.cssText = "display:block;margin:12px 0";
    for (const el of root.querySelectorAll("input,select")) el.style.cssText = "display:block;width:100%;padding:10px;color:CanvasText;background:Canvas;border:1px solid #888;border-radius:6px";
    container.append(root);
    // Native option menus need an explicit color scheme; transparent controls
    // otherwise inherit light text while the browser opens a white popup.
    function syncTheme() {
      const rgb = getComputedStyle(root).color.match(/[\d.]+/g)?.slice(0, 3).map(Number);
      if (rgb?.length === 3) root.style.colorScheme =
        (rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722) > 128 ? "dark" : "light";
    }
    const themeObserver = new MutationObserver(syncTheme);
    for (let ancestor = root.parentElement; ancestor; ancestor = ancestor.parentElement) {
      themeObserver.observe(ancestor, {attributes: true, attributeFilter: ["class", "style", "data-theme"]});
    }
    const systemTheme = matchMedia("(prefers-color-scheme: dark)");
    systemTheme.addEventListener("change", syncTheme);
    syncTheme();
    const form = root.querySelector("[data-review]"), add = root.querySelector("[data-provider]");
    const status = root.querySelector('[role="status"]');

    const roles = ['primary', 'secondary', 'fallback'];
    const select = (role, type) => form.elements[role + type];
    const message = text => { if (!disposed) status.textContent = text; };
    const providerOf = id => providers.find(item => item.id === id);
    // The inventory row says whether an agent can be a reading profile; older servers omit the field.
    const switchable = id => { const p = providerOf(id); return !!p && (p.switchable ?? p.kind === 'api'); };
    const effortSelectable = role => (providerOf(select(role, 'Provider').value)?.efforts || []).length > 0;
    function currentSelection() {
      const choice = role => select(role, 'Provider').value ? {provider: select(role, 'Provider').value, model: select(role, 'Model').value, effort: select(role, 'Effort').value || null} : null;
      return {primary: choice('primary'), secondary: choice('secondary'), fallback: choice('fallback')};
    }
    function validSelection() {
      const {primary, secondary, fallback} = currentSelection();
      const chosen = [primary, secondary, fallback].filter(Boolean).map(item => JSON.stringify(item));
      const known = item => !item || !!providerOf(item.provider);  // an 'unavailable' sentinel never saves
      const flagged = [...roles, 'settings'].some(slot => root.querySelector(`[data-warning="${slot}"]`)?.textContent);
      return !flagged && known(primary) && known(secondary) && known(fallback)
        && !!primary?.model && (!secondary || !!secondary.model) && (!fallback || !!fallback.model)
        && new Set(chosen).size === chosen.length
        && (!secondary || (switchable(primary.provider) && switchable(secondary.provider)));
    }
    function syncSecondary() {
      const primary = select('primary', 'Provider').value, unknown = !!primary && !providerOf(primary);
      const allowed = switchable(primary) || unknown;  // an unavailable primary keeps the saved secondary until a person decides
      const picker = select('secondary', 'Provider');
      if (!allowed && picker.value) { picker.value = ''; select('secondary', 'Model').replaceChildren(); select('secondary', 'Effort').replaceChildren(); }
      picker.disabled = busy || !allowed;
      select('secondary', 'Model').disabled = busy || !allowed || !picker.value;
      select('secondary', 'Effort').disabled = busy || !allowed || !picker.value || !effortSelectable('secondary');
      root.querySelector('[data-secondary-hint]').textContent = allowed ? '' : 'Available when the primary is an API provider; account agents cannot switch models mid-review.';
    }
    const hasChanges = () => savedSelection !== undefined && JSON.stringify(currentSelection()) !== savedSelection;
    function rememberSaved() {
      savedSelection = JSON.stringify(currentSelection());
      savedControls = [...form.querySelectorAll('select')].map(control => ({
        name: control.name, value: control.value,
        options: [...control.options].map(option => ({text: option.text, value: option.value}))
      }));
    }
    function updateSave() {
      const changed = hasChanges(), valid = validSelection();
      const save = form.querySelector('[type="submit"]');
      save.disabled = busy || revision === undefined || !changed || !valid;
      save.classList.toggle('needs-save', changed);
      root.querySelector('[data-revert]').disabled = busy || !changed;
      root.querySelector('[data-unsaved]').textContent = changed ? 'Unsaved changes' : '';
    }
    function lock(value) {
      busy = value;
      root.dataset.busy = String(value);
      form.setAttribute('aria-busy', String(value));
      add.setAttribute('aria-busy', String(value));
      const elapsed = root.querySelector('[data-elapsed]');
      if (value && progressTimer === undefined) {
        busyStartedAt = Date.now();
        elapsed.textContent = 'Working…';
        progressTimer = setInterval(() => {
          elapsed.textContent = Math.floor((Date.now() - busyStartedAt) / 1000) + 's';
        }, 1000);
      } else if (!value) {
        clearInterval(progressTimer); progressTimer = undefined; elapsed.textContent = '';
      }
      for (const el of root.querySelectorAll("button,input,select")) el.disabled = value;
      if (!value) {
        for (const role of roles) {
          select(role, "Model").disabled = !select(role, "Provider").value;
          select(role, "Effort").disabled = !effortSelectable(role);
        }
        syncSecondary();
      }
      updateSave();
    }
    function efforts(role, current) {
      const picker = select(role, 'Effort'), provider = providerOf(select(role, 'Provider').value);
      picker.replaceChildren();
      if (!provider) return;
      const options = provider.efforts || [];
      if (provider.kind !== 'api') picker.add(new Option(options.length ? 'Adapter default' : 'Adapter default (not selectable)', ''));
      for (const effort of options) picker.add(new Option(effort, effort));
      picker.value = current && options.includes(current) ? current : provider.kind === 'api' ? (options.includes('high') ? 'high' : options[0] || '') : '';
      picker.disabled = busy || !options.length;
    }
    async function models(role, current, effort) {
      const provider = select(role, "Provider").value, model = select(role, "Model");
      model.replaceChildren(); efforts(role, effort);
      if (!provider) return;
      const providerName = providers.find(item => item.id === provider)?.name || provider;
      message(`Fetching ${role} models from ${providerName}… Provider discovery can take a few seconds.`);
      model.add(new Option('Loading models…', ''));
      let data;
      try { data = await providerModels(providers.find(item => item.id === provider) || {id: provider}); }
      catch (error) {
        if (!disposed) model.replaceChildren(new Option('Models unavailable', ''));
        throw error;
      }
      if (disposed) return;
      model.replaceChildren();
      for (const item of data.models) model.add(new Option(item.label, item.id));
      if (current && data.models.some(m => m.id === current)) model.value = current;
      else if (current) { model.add(new Option(current + " (not advertised; choose a model)", "", true, true)); }
      if (!data.models.length) throw new Error("This provider returned no models.");
    }
    async function load(force = false) {
      if (force) modelCache.clear();
      lock(true); message("Loading saved settings and provider connections…");
      try {
        const [settings, inventory] = await Promise.all([request('/api/review-control/settings'), request('/api/review-control/providers')]);
        if (disposed) return;
        revision = settings.settings.revision; providers = inventory.providers; selections = inventory.selections;
        const available = new Set(providers.map(cacheKey));
        for (const key of modelCache.keys()) if (!available.has(key)) modelCache.delete(key);
        const list = root.querySelector('[data-providers]'); list.replaceChildren();
        for (const p of providers) {
          const item = document.createElement('li');
          const title = document.createElement('div');
          title.textContent = p.name + (p.kind === 'subscription' ? ' · Account connection (ACP)' : ' · API provider');
          const result = document.createElement('div'); result.setAttribute('role', 'status');
          const buttons = ['List models', 'Test connection'].map(label => {
            const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
            button.onclick = async () => {
              if (busy) return;
              lock(true); result.replaceChildren(); message(`Checking ${p.name}…`);
              try {
                let data;
                if (label === 'List models') {
                  data = {ok: true, ...await providerModels(p)};
                } else {
                  modelCache.delete(cacheKey(p));
                  data = await request('/api/review-control/test-provider', 'POST', {provider: p.id});
                  if (data.ok) modelCache.set(cacheKey(p), Promise.resolve({models: data.models}));
                }
                if (disposed) return;
                if (!data.ok) { result.textContent = data.message; message(`${p.name}: ${data.message}`); return; }
                result.textContent = label === 'List models' ? `${data.models.length} models available. Use Test connection to refresh.` : `Connected. ${data.models.length} models available. No inference request sent.`;
                if (label === 'List models') {
                  const modelsList = document.createElement('ul'); modelsList.setAttribute('data-model-list', '');
                  for (const model of data.models) { const row = document.createElement('li'); row.textContent = model.id; modelsList.append(row); }
                  result.append(modelsList);
                }
                message(label === 'List models' ? `${p.name}: models loaded.` : `${p.name}: connection check passed.`);
              } catch { if (!disposed) result.textContent = 'Connection test unavailable. Try again.'; message('Connection test unavailable. Try again.'); }
              finally { if (!disposed) lock(false); }
            };
            return button;
          });
          item.append(title, ...buttons, result);
          if (p.kind === 'api' && p.id.startsWith('connection:')) {
            const edit = document.createElement('button'); edit.type = 'button'; edit.textContent = 'Edit';
            edit.onclick = () => {
              if (busy) return;
              editingProvider = p.id;
              add.elements.display_name.value = p.name; add.elements.base_url.value = p.url || '';
              add.elements.api_key.value = ''; add.elements.api_key.required = false;
              add.elements.api_key.placeholder = 'Leave blank to keep the current token';
              add.querySelector('[type="submit"]').textContent = 'Save provider';
              root.querySelector('[data-cancel-edit]').hidden = false;
              root.querySelector('[data-new-result]').textContent = 'Changes are tested before saving. A blank token keeps the current token.';
              const editor = root.querySelector('[data-provider-editor]'); editor.open = true;
              editor.querySelector('summary').textContent = 'Edit API provider'; editor.scrollIntoView({block:'nearest'});
            };
            item.insertBefore(edit, result);
          }
          list.append(item);
        }
        const failures = [];
        for (const role of roles) {
          const picker = select(role, 'Provider'); picker.replaceChildren();
          if (role === 'fallback') picker.add(new Option('None — stop on usage limit', ''));
          if (role === 'secondary') picker.add(new Option('None — single-profile review', ''));
          for (const p of providers) { if (role === 'secondary' && !p.switchable) continue; picker.add(new Option(p.name + (p.kind === 'subscription' ? ' (account)' : ' (API)'), p.id)); }
          const name = settings.settings[role], current = selections[name];
          // A saved profile that was deleted or broken in Canvas is shown, never dropped or repaired: a person replaces or fixes it.
          const fix = 'Choose a replacement, or fix the profile in Canvas and reload.';
          root.querySelector(`[data-warning="${role}"]`).textContent =
            settings.problems?.[role] ? `Saved ${role} profile "${name}" cannot run: ${settings.problems[role]}. ${fix}`
            : current?.problem ? `Saved ${role} profile "${name}" references ${current.problem}. ${fix}`
            : name && !current ? `Saved ${role} profile "${name}" no longer exists. ${fix}`
            : current && !providers.some(p => p.id === current.provider) ? `The provider of saved ${role} profile "${name}" is not available. ${fix}` : '';
          if (current) {
            if (!providers.some(p => p.id === current.provider)) picker.add(new Option(current.provider + ' (unavailable)', current.provider));
            picker.value = current.provider;
          } else if (settings.settings[role]) {
            picker.add(new Option(settings.settings[role] + ' (unavailable)', 'unavailable'));
            picker.value = 'unavailable';
          } else picker.value = '';
          if (!providerOf(picker.value)) { select(role, 'Model').replaceChildren(); select(role, 'Effort').replaceChildren(); continue; }  // nothing to discover for a missing provider
          try { await models(role, current?.model, current?.effort); } catch { failures.push(role); }
        }
        syncSecondary();
        rememberSaved();
        root.querySelector('[data-warning="settings"]').textContent = settings.problems?.settings ? `Saved settings cannot be used: ${settings.problems.settings}` : '';
        const attention = [...roles, 'settings'].some(role => root.querySelector(`[data-warning="${role}"]`).textContent);
        message(failures.length ? 'Could not fetch ' + failures.join(' and ') + ' models. Check the provider connection in Canvas, then reload.'
          : attention ? 'A saved profile needs attention; see the warning under its role.' : 'Current automatic review settings.');
      } catch { message('Could not load settings. Check the connection and reload.'); }
      finally { if (!disposed) lock(false); }
    }
    for (const role of roles) {
      select(role, 'Provider').onchange = async () => {
        root.querySelector(`[data-warning="${role}"]`).textContent = '';
        root.querySelector('[data-warning="settings"]').textContent = '';
        lock(true); message('Fetching available models…');
        try { await models(role); message('Models loaded. Save to apply your selection.'); }
        catch { message('Could not discover models. Check the provider URL, credentials, or account login in Canvas.'); }
        finally { if (!disposed) lock(false); }
      };
      select(role, 'Model').onchange = updateSave;
      select(role, 'Effort').onchange = updateSave;
    }
    form.onsubmit = async event => {
      event.preventDefault(); if (busy || !hasChanges()) return;
      const {primary, secondary, fallback} = currentSelection();
      if (!validSelection()) { message('Choose a primary; secondary and fallback must differ from it, and a secondary needs API providers on both.'); return; }
      lock(true); message('Saving settings…');
      try {
        const data = await request('/api/review-control/settings', 'PUT', {revision, primary, secondary, fallback});
        if (!disposed) { revision = data.settings.revision; rememberSaved(); for (const slot of [...roles, 'settings']) root.querySelector(`[data-warning="${slot}"]`).textContent = ''; }
        message('Saved. New reviews will use these settings.');
      } catch { message('Could not save. Settings may have changed, or provider discovery failed. Reload before trying again.'); }
      finally { if (!disposed) lock(false); }
    };
    function resetProviderEditor() {
      editingProvider = null; add.reset(); add.elements.api_key.required = true;
      add.elements.api_key.placeholder = ''; add.querySelector('[type="submit"]').textContent = 'Add provider';
      root.querySelector('[data-cancel-edit]').hidden = true;
      root.querySelector('[data-provider-editor] summary').textContent = 'Add API provider';
      root.querySelector('[data-new-result]').textContent = '';
    }
    root.querySelector('[data-cancel-edit]').onclick = () => { if (!busy) resetProviderEditor(); };
    async function checkNewProvider(save) {
      if (busy || !add.reportValidity()) return;
      const body = {display_name: add.elements.display_name.value.trim(), base_url: add.elements.base_url.value.trim(), api_key: add.elements.api_key.value.trim(), provider: 'custom'};
      const feedback = root.querySelector('[data-new-result]');
      lock(true); message('Testing provider authentication and model discovery…'); feedback.textContent = '';
      try {
        const probe = {base_url: body.base_url};
        if (editingProvider) probe.provider = editingProvider;
        if (body.api_key) probe.api_key = body.api_key;
        const checked = await request('/api/review-control/test-provider', 'POST', probe);
        if (disposed) return;
        if (!checked.ok) { feedback.textContent = checked.message + ' Provider was not saved.'; message(feedback.textContent); return; }
        if (!save) { feedback.textContent = `Connection check passed: ${checked.models.length} models. Nothing saved; no inference request sent.`; message(feedback.textContent); return; }
        message('Connection check passed. Saving provider…');
        if (editingProvider) {
          const update = {display_name: body.display_name, base_url: body.base_url};
          if (body.api_key) update.api_key = body.api_key;
          await request('/api/llm/provider-connections/' + encodeURIComponent(editingProvider.slice('connection:'.length)), 'PATCH', update);
          const previous = providers.find(provider => provider.id === editingProvider);
          if (previous) modelCache.delete(cacheKey(previous));
          modelCache.set(cacheKey({id: editingProvider, url: body.base_url}), Promise.resolve({models: checked.models}));
        } else {
          const created = await request('/api/llm/provider-connections', 'POST', body);
          modelCache.set(cacheKey({id: 'connection:' + created.id, url: body.base_url}), Promise.resolve({models: checked.models}));
        }
        if (disposed) return;
        resetProviderEditor(); await load(); feedback.textContent = 'Provider tested and saved in Canvas.'; message(feedback.textContent);
      } catch { if (!disposed) { feedback.textContent = 'Could not complete the provider check or save. Reload to check saved connections before retrying.'; message(feedback.textContent); } }
      finally { body.api_key = ''; if (!disposed) lock(false); }
    }
    add.onsubmit = event => { event.preventDefault(); checkNewProvider(true); };
    root.querySelector('[data-test-new]').onclick = () => checkNewProvider(false);
    root.querySelector('[data-reload]').onclick = () => load(true);
    root.querySelector('[data-revert]').onclick = () => {
      if (busy || !savedControls) return;
      for (const saved of savedControls) {
        const control = form.elements[saved.name];
        control.replaceChildren(...saved.options.map(option => new Option(option.text, option.value)));
        control.value = saved.value;
      }
      lock(false);
      message('Unsaved changes discarded. Restored your last loaded or saved settings.');
    };
    load();
    return () => { disposed = true; clearInterval(progressTimer); themeObserver.disconnect(); systemTheme.removeEventListener("change", syncTheme); add.reset(); root.remove(); };
  });
}
