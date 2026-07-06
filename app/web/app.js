/* Drive Cleanser SPA — hash router, fetch-based, no build step. */
const $ = (s, el = document) => el.querySelector(s);
const main = $('#main');

const api = async (path, opts = {}) => {
  if (opts.body) { opts.headers = { 'Content-Type': 'application/json' }; opts.body = JSON.stringify(opts.body); }
  const r = await fetch('/api' + path, opts);
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || r.statusText); }
  return r.json();
};

const toast = (msg, ms = 3200) => {
  const t = $('#toast'); t.textContent = msg; t.classList.remove('hidden');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add('hidden'), ms);
};
const esc = s => (s ?? '').toString().replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmtSize = b => !b ? '—' : b > 1e9 ? (b / 1e9).toFixed(2) + ' GB' : b > 1e6 ? (b / 1e6).toFixed(1) + ' MB' : Math.round(b / 1e3) + ' KB';
const fmtDate = t => t ? t.slice(0, 10) : '—';
const thumb = id => `<img loading="lazy" src="/api/thumb/${id}" onerror="this.outerHTML='<div class=noimg>🎞️</div>'">`;

/* ---------- job polling ---------- */
let jobTimer = null;
async function pollJob() {
  try {
    const st = await api('/status');
    const job = st.job;
    const bar = $('#jobbar');
    if (job && job.status === 'running') {
      bar.classList.remove('hidden');
      $('#jobmsg').textContent = job.message || 'working…';
      $('#jobprog').style.width = (job.progress * 100).toFixed(0) + '%';
      if (!jobTimer) jobTimer = setInterval(pollJob, 2000);
    } else {
      bar.classList.add('hidden');
      if (jobTimer) { clearInterval(jobTimer); jobTimer = null; if (job) { toast('Scan ' + job.status); render(); } }
    }
    return st;
  } catch (e) { return null; }
}

/* ---------- pages ---------- */
const pages = {

  async dashboard() {
    const st = await api('/status');
    const f = st.files || {}; const caps = st.capabilities || {};
    const cap = (on, name, hint) => `<span class="badge ${on ? 'ok' : 'warn'}">${name}: ${on ? 'on' : 'off'}${on ? '' : ' — ' + hint}</span>`;
    const recs = Object.entries(st.pending_recommendations || {}).map(([c, n]) =>
      `<a class="btn" href="#collections/${encodeURIComponent(c)}">${esc(c)} <b>${n}</b></a>`).join(' ') || '<span class="muted">none yet — run a scan</span>';
    main.innerHTML = `
      <h1>Dashboard</h1>
      <div class="cards">
        <div class="stat"><div class="n">${f.total || 0}</div><div class="l">files indexed</div></div>
        <div class="stat"><div class="n">${f.photos || 0}</div><div class="l">photos</div></div>
        <div class="stat"><div class="n">${f.videos || 0}</div><div class="l">videos</div></div>
        <div class="stat"><div class="n">${f.analyzed || 0}</div><div class="l">analyzed</div></div>
        <div class="stat"><div class="n">${f.trashed || 0}</div><div class="l">trashed (undoable)</div></div>
        <div class="stat"><div class="n">${f.errors || 0}</div><div class="l">errors</div></div>
      </div>
      <h2>Capabilities</h2>
      <div class="panel">
        ${cap(caps.clip, 'CLIP semantic AI', 'pip install -r requirements-ml.txt')}
        ${cap(caps.faces, 'Face recognition', 'pip install -r requirements-ml.txt')}
        ${cap(caps.ffmpeg, 'Video analysis (ffmpeg)', 'brew install ffmpeg')}
      </div>
      <h2>Sources</h2>
      <div class="panel">
        <div class="row" style="margin-bottom:10px">
          <b>Google Drive</b>
          <span class="badge ${st.gdrive.connected ? 'ok' : ''}">${st.gdrive.connected ? (st.gdrive.write ? 'connected (read/write)' : 'connected (read-only)') : 'not connected'}</span>
          <button id="gd-connect">Connect (read-only)</button>
          <button id="gd-connect-w" title="Needed only to execute approved trash actions">Enable write access</button>
        </div>
        <div class="row">
          <b>Local folder</b>
          <input type="text" id="lf-root" placeholder="/path/to/folder (e.g. a synced iCloud/Drive folder)"
                 value="${esc(st.localfs_root || '')}" style="flex:1;min-width:260px">
          <button id="lf-set">Set</button>
        </div>
      </div>
      <h2>Scan</h2>
      <div class="panel row">
        <select id="scan-src">
          <option value="gdrive">Google Drive</option>
          <option value="localfs">Local folder</option>
        </select>
        <input type="number" id="scan-max" placeholder="max files (blank = all)" style="width:190px">
        <button class="primary" id="scan-go">Start scan</button>
        <button id="scan-stop">Cancel</button>
        <span class="muted">Scanning is 100% read-only: nothing is moved, renamed, or deleted.</span>
      </div>
      <h2>Pending review queues</h2>
      <div class="panel row">${recs}</div>`;
    $('#gd-connect').onclick = () => api('/sources/gdrive/connect', { method: 'POST', body: { write: false } })
      .then(() => { toast('Connected read-only'); render(); }).catch(e => toast(e.message, 8000));
    $('#gd-connect-w').onclick = () => confirm('Enable write access? This is only used to move user-approved files to Drive trash (reversible for 30 days).') &&
      api('/sources/gdrive/connect', { method: 'POST', body: { write: true } })
        .then(() => { toast('Write access enabled'); render(); }).catch(e => toast(e.message, 8000));
    $('#lf-set').onclick = () => api('/sources/localfs', { method: 'POST', body: { root: $('#lf-root').value } })
      .then(() => toast('Folder set')).catch(e => toast(e.message, 6000));
    $('#scan-go').onclick = () => api('/scan', { method: 'POST', body: { source: $('#scan-src').value, max_files: parseInt($('#scan-max').value) || null } })
      .then(() => { toast('Scan started'); pollJob(); }).catch(e => toast(e.message, 6000));
    $('#scan-stop').onclick = () => api('/scan/cancel', { method: 'POST' }).then(() => toast('Cancelling…'));
  },

  async library() {
    main.innerHTML = `<h1>Library</h1>
      <div class="row" style="margin-bottom:12px">
        <select id="f-kind"><option value="">All types</option><option value="photo">Photos</option><option value="video">Videos</option></select>
        <button id="f-go">Filter</button>
      </div><div class="grid" id="lib"></div>
      <div class="row" style="margin-top:14px"><button id="more">Load more</button></div>`;
    let page = 0;
    const load = async (reset) => {
      if (reset) { page = 0; $('#lib').innerHTML = ''; }
      const d = await api(`/files?page=${page}&kind=${$('#f-kind').value}`);
      $('#lib').insertAdjacentHTML('beforeend', d.files.map(f => `
        <div class="tile" onclick="showFile(${f.id})">${thumb(f.id)}
          <div class="cap">${f.kind === 'video' ? '🎞️ ' : ''}${esc(f.name)}<br>
          ${fmtDate(f.taken_time || f.created_time)} · ${fmtSize(f.size)}</div></div>`).join(''));
      if (!d.files.length) toast('No more files'); else page++;
    };
    $('#f-go').onclick = () => load(true);
    $('#more').onclick = () => load(false);
    load(true);
  },

  async duplicates(sub) {
    const d = await api('/duplicates' + (sub ? `?kind=${sub}` : ''));
    const tabs = ['', 'exact', 'near', 'video'];
    const totalOthers = d.groups.reduce((s, g) => s + g.members.filter(m => m.file_id !== g.keep_file_id).length, 0);
    const anyVideo = d.groups.some(g => g.kind === 'video');
    main.innerHTML = `<h1>Duplicates <span class="muted">(${d.groups.length} unresolved groups)</span></h1>
      <div class="tabs">${tabs.map(t => `<button data-t="${t}" class="${(sub || '') === t ? 'active' : ''}">${t || 'all'}</button>`).join('')}</div>
      ${d.groups.length ? `<div class="row actionbar">
        <button class="danger" id="dup-all">Resolve ALL shown: keep ✓ choices, trash ${totalOthers} cop${totalOthers === 1 ? 'y' : 'ies'}…</button>
        <span class="muted">or resolve group by group below — every trash is undoable from Activity</span>
      </div>` : ''}
      <div id="groups"></div>`;
    main.querySelectorAll('.tabs button').forEach(b => b.onclick = () => { location.hash = '#duplicates/' + b.dataset.t; });
    const btn = $('#dup-all');
    if (btn) btn.onclick = async () => {
      if (!confirm(`Resolve all ${d.groups.length} groups shown?\n\nKeeps each group's ✓ file and moves ${totalOthers} duplicate cop${totalOthers === 1 ? 'y' : 'ies'} to the undoable trash.`)) return;
      if (anyVideo && !confirm('Some groups are VIDEOS. Second confirmation: trash video duplicates too?')) return;
      const r = await api('/duplicates/resolve-all', { method: 'POST', body: { kind: sub || null } });
      toast(`Resolved ${r.groups_resolved} groups — ${r.trashed} files trashed${r.errors ? `, ${r.errors} errors` : ''}`, 6000);
      render();
    };
    $('#groups').innerHTML = d.groups.map(g => `
      <div class="panel dupgroup">
        <div><span class="badge ${g.kind === 'exact' ? 'ok' : 'warn'}">${g.kind}</span>
          ${g.kind === 'video' ? '<span class="badge danger">videos always need explicit approval</span>' : ''}</div>
        <div class="expl">${esc(g.explanation)}</div>
        <div class="members">${g.members.map(m => `
          <div class="dupmember ${m.file_id === g.keep_file_id ? 'keep' : ''}">
            <div onclick="showFile(${m.file_id})" style="cursor:pointer">${thumb(m.file_id)}</div>
            <div class="meta">${esc(m.name)}<br>${m.width || '?'}×${m.height || '?'} · ${fmtSize(m.size)} · q=${m.quality ?? '—'}</div>
            <div class="row" style="gap:4px">
              ${m.file_id === g.keep_file_id
                ? '<span class="badge ok">✓ will keep</span>'
                : `<button class="sm" onclick="setKeep(${g.id},${m.file_id})" title="mark as the keeper">✓ prefer</button>`}
              <button class="sm danger" onclick="resolveGroup(${g.id},${m.file_id},${g.members.length - 1},'${g.kind}')"
                title="keep this file, trash the other ${g.members.length - 1}">keep only this</button>
            </div>
          </div>`).join('')}</div>
      </div>`).join('') || '<p class="muted">No unresolved duplicate groups 🎉</p>';
  },

  async people() {
    const d = await api('/people');
    main.innerHTML = `<h1>People</h1>
      <h2>Labeled</h2><div class="grid" id="persons"></div>
      <h2>Unlabeled clusters — name a person once to tag all their media</h2>
      <div class="grid" id="clusters"></div>`;
    $('#persons').innerHTML = d.persons.map(p => `
      <div class="tile"><div class="noimg">👤</div>
        <div class="cap"><b>${esc(p.name)}</b><br>${p.n_files} files · ${p.n_faces} faces</div></div>`).join('')
      || '<p class="muted">No labeled people yet.</p>';
    $('#clusters').innerHTML = d.unlabeled_clusters.map(c => `
      <div class="tile facecluster" style="padding:8px">
        ${thumb(c.sample_file_id)}
        <div class="cap">cluster ${c.cluster_id} · ${c.n_faces} faces in ${c.n_files} files</div>
        <input type="text" placeholder="Name (e.g. Mom)" id="cl-${c.cluster_id}">
        <button class="sm" style="margin-top:6px" onclick="labelCluster(${c.cluster_id})">Label</button>
      </div>`).join('')
      || '<p class="muted">No unlabeled clusters. Install the ML extras and re-scan to enable face recognition.</p>';
  },

  async search() {
    main.innerHTML = `<h1>Search</h1>
      <div class="panel row">
        <input type="text" id="q" placeholder='Try "birthday cake", "beach sunset", "Mom cooking", "vacation 2022"…' style="flex:1">
        <button class="primary" id="go">Search</button>
      </div><div class="grid" id="results"></div>`;
    const go = async () => {
      const d = await api('/search?q=' + encodeURIComponent($('#q').value));
      $('#results').innerHTML = d.results.map(f => `
        <div class="tile" onclick="showFile(${f.id})">${thumb(f.id)}
          <div class="cap">${esc(f.name)}<br>${fmtDate(f.taken_time || f.created_time)} · score ${f.score}</div></div>`).join('')
        || '<p class="muted">No results.</p>';
    };
    $('#go').onclick = go;
    $('#q').onkeydown = e => e.key === 'Enter' && go();
  },

  async collections(sub) {
    const d = await api('/collections');
    const colls = ['Duplicate Candidates', 'Screenshots', 'Documents', 'Memes', 'Review', 'Keep'];
    const pend = {}; d.collections.forEach(c => { if (c.status === 'pending') pend[c.collection] = c.n; });
    const active = sub ? decodeURIComponent(sub) : (colls.find(c => pend[c]) || 'Keep');
    main.innerHTML = `<h1>Collections</h1>
      <div class="tabs">${colls.map(c =>
        `<button data-c="${esc(c)}" class="${c === active ? 'active' : ''}">${esc(c)}${pend[c] ? ` (${pend[c]})` : ''}</button>`).join('')}</div>
      <div class="row" style="margin-bottom:10px">
        <button class="ok" id="approve-all">Approve all shown</button>
        <button id="reject-all">Reject all shown</button>
        <button class="danger" id="exec">Execute approved trash actions…</button>
      </div>
      <div class="reclist" id="recs"></div>`;
    main.querySelectorAll('.tabs button').forEach(b => b.onclick = () => { location.hash = '#collections/' + encodeURIComponent(b.dataset.c); });
    const d2 = await api('/recommendations?collection=' + encodeURIComponent(active));
    window._shown = d2.recommendations;
    $('#recs').innerHTML = d2.recommendations.map(r => `
      <div class="rec" id="rec-${r.id}">
        <div onclick="showFile(${r.file_id})" style="cursor:pointer">${thumb(r.file_id)}</div>
        <div class="info">
          <div class="name">${r.kind === 'video' ? '🎞️ ' : ''}${esc(r.name)}
            <span class="badge">${esc(r.action)}</span></div>
          <div class="why">${esc(r.explanation)}</div>
        </div>
        <div class="conf">${(r.confidence * 100).toFixed(0)}%</div>
        <button class="sm ok" onclick="decide(${r.id},'approve')">Approve</button>
        <button class="sm" onclick="decide(${r.id},'reject')">Reject</button>
      </div>`).join('') || '<p class="muted">Nothing pending here.</p>';
    $('#approve-all').onclick = () => bulkDecide('approve');
    $('#reject-all').onclick = () => bulkDecide('reject');
    $('#exec').onclick = executeApproved;
  },

  async cleanup() {
    const cs = (window._cleanup ||= { folder: null, keepC: new Set(), keepP: new Set(), sel: new Set() });
    const fd = await api('/cleanup/folders');
    const step = !cs.folder ? 1 : 2;
    main.innerHTML = `<h1>Cleanup — filter by people</h1>
      <div class="steps">
        <span class="step ${step === 1 ? 'on' : ''}">1 · pick a folder</span>
        <span class="step ${step === 2 ? 'on' : ''}">2 · tick the people you KNOW</span>
        <span class="step">3 · review files with none of them → trash</span>
      </div>
      <div class="panel row">
        <select id="cl-folder">
          <option value="">— choose folder —</option>
          ${fd.folders.map(f => `<option value="${esc(f.folder)}" ${f.folder === cs.folder ? 'selected' : ''}>
            ${esc(f.folder)} (${f.files} files, ${f.files_with_faces} with faces)</option>`).join('')}
        </select>
        <label class="muted"><input type="checkbox" id="cl-nofaces"> also show files with no faces at all</label>
      </div>
      <div id="cl-people"></div>
      <div id="cl-results"></div>`;
    $('#cl-folder').onchange = () => { cs.folder = $('#cl-folder').value || null; cs.keepC.clear(); cs.keepP.clear(); cs.sel.clear(); render(); };
    if (!cs.folder) return;

    const d = await api('/cleanup/clusters?folder=' + encodeURIComponent(cs.folder));
    const chip = (id, kind, label, sub, sample) => {
      const on = (kind === 'c' ? cs.keepC : cs.keepP).has(id);
      return `<div class="tile facechip ${on ? 'known' : ''}" onclick="clToggle('${kind}',${id})">
        <img loading="lazy" src="/api/face/${sample}/crop" onerror="this.src='/api/thumb/0'">
        <div class="cap"><b>${esc(label)}</b><br>${sub}</div></div>`;
    };
    $('#cl-people').innerHTML = `
      <h2>People seen in “${esc(cs.folder)}” — tick everyone you know (green = keep their files)</h2>
      <div class="grid">
        ${d.persons.map(p => chip(p.id, 'p', p.name, `${p.n_files} files`, p.sample_face_id)).join('')}
        ${d.clusters.map(c => chip(c.cluster_id, 'c', 'person ' + c.cluster_id, `${c.n_files} files · ${c.n_faces} faces`, c.sample_face_id)).join('')}
      </div>
      ${d.unclustered_faces ? `<p class="muted" style="margin-top:8px">+ ${d.unclustered_faces} one-off faces in ${d.unclustered_files} files (each seen only once — can't be selected, their files stay in the review list below)</p>` : ''}
      <div class="row" style="margin-top:12px">
        <button class="primary" id="cl-go">Show files WITHOUT my selected people</button>
        <span class="muted">${cs.keepC.size + cs.keepP.size} selected as known</span>
      </div>`;
    $('#cl-go').onclick = async () => {
      const r = await api('/cleanup/candidates', { method: 'POST', body: {
        folder: cs.folder, keep_clusters: [...cs.keepC], keep_persons: [...cs.keepP],
        include_nofaces: $('#cl-nofaces').checked } }).catch(e => { toast(e.message, 6000); });
      if (!r) return;
      clGridInit(r.candidates.concat(r.nofaces || []), r);
    };
  },

  async record() {
    const s = await api('/record/summary');
    const reasons = Object.entries(s.by_reason).map(([k, v]) =>
      `<span class="badge">${esc(k)}: ${v}</span>`).join(' ') || '<span class="muted">nothing deleted yet</span>';
    main.innerHTML = `<h1>Deletion record</h1>
      <p class="muted">Permanent registry of everything removed — filename, path, and checksums (md5/sha256)
      plus perceptual hash — so you can filter the same files on any other drive or backup.</p>
      <div class="cards" style="margin:14px 0">
        <div class="stat"><div class="n">${s.deleted_files}</div><div class="l">files deleted (via app)</div></div>
        <div class="stat"><div class="n">${(s.deleted_bytes / 1e9).toFixed(2)} GB</div><div class="l">space reclaimed</div></div>
        <div class="stat"><div class="n">${s.with_checksum}</div><div class="l">have checksums</div></div>
        <div class="stat"><div class="n">${(s.local_trash_bytes / 1e9).toFixed(2)} GB</div><div class="l">sitting in local trash</div></div>
      </div>
      <div class="panel"><h2 style="margin-top:0">By reason</h2>${reasons}</div>
      <div class="panel">
        <h2 style="margin-top:0">Export the record</h2>
        <p class="muted">Writes <b>deleted-record.sqlite</b> + <b>deleted-record.csv</b> into ${esc(s.export_dir)}.
        Optionally merge the download-phase duplicate report (files never downloaded because an identical copy was kept):</p>
        <div class="row" style="margin:10px 0">
          <input type="text" id="rec-report" value="~/DriveDump/Photos/skipped-duplicates.jsonl" style="flex:1;min-width:300px">
          <label class="muted"><input type="checkbox" id="rec-inc" checked> include</label>
        </div>
        <button class="primary" id="rec-export">Export deletion record</button>
        <div id="rec-out" style="margin-top:10px"></div>
      </div>
      <div class="panel">
        <h2 style="margin-top:0">Local trash</h2>
        <p class="muted">Deleted files are still recoverable at <b>${esc(s.local_trash_dir)}</b>
        (${(s.local_trash_bytes / 1e9).toFixed(2)} GB). This app never permanently deletes anything —
        when you've exported the record and are sure, empty it yourself in Terminal:</p>
        <pre class="kv" style="background:var(--panel2);padding:10px;border-radius:8px;overflow-x:auto">rm -rf "${esc(s.local_trash_dir)}"</pre>
      </div>`;
    $('#rec-export').onclick = async () => {
      const body = $('#rec-inc').checked ? { download_report: $('#rec-report').value } : {};
      const r = await api('/record/export', { method: 'POST', body }).catch(e => { toast(e.message, 8000); });
      if (!r) return;
      $('#rec-out').innerHTML = `<span class="badge ok">exported</span><br>
        Deletion record: ${r.deleted_rows} deleted + ${r.skipped_duplicate_rows} download-skipped →
        <b>${esc(r.sqlite)}</b> + csv<br>
        Duplication record: ${r.duplication_rows} rows (every group member, keeper + fate) →
        <b>${esc(r.dup_sqlite)}</b> + csv`;
      toast('Records exported');
    };
  },

  async activity() {
    const [d, dr] = await Promise.all([api('/actions'), api('/recommendations?status=approved&page_size=200')]);
    main.innerHTML = `<h1>Activity</h1>
      <h2>Approved, awaiting execution (${dr.recommendations.length})</h2>
      <div class="panel">${dr.recommendations.length
        ? `<div class="row"><button class="danger" id="exec2">Execute ${dr.recommendations.length} approved action(s)…</button>
           <span class="muted">Only explicitly approved items are ever executed; 'delete' = reversible trash.</span></div>`
        : '<span class="muted">Nothing approved and unexecuted.</span>'}</div>
      <h2>Audit log</h2>
      <table><tr><th>When</th><th>Action</th><th>Detail</th><th></th></tr>
      ${d.actions.map(a => `<tr>
        <td class="muted">${esc(a.executed_at)}</td><td><span class="badge">${esc(a.action)}</span></td>
        <td>${esc(a.detail)}${a.undone_at ? ' <span class="badge ok">undone</span>' : ''}</td>
        <td>${a.undone_at ? '' : `<button class="sm" onclick="undoAction(${a.id})">Undo</button>`}</td>
      </tr>`).join('') || '<tr><td colspan=4 class="muted">No actions executed yet.</td></tr>'}</table>`;
    const ex = $('#exec2');
    if (ex) ex.onclick = () => { window._shown = dr.recommendations; executeApproved(); };
  },
};

/* ---------- shared actions ---------- */
window.showFile = async id => {
  const f = await api('/files/' + id);
  const labels = f.labels.map(l => `<span class="badge">${esc(l.label)} ${(l.score * 100).toFixed(0)}% <i>(${l.method})</i></span>`).join('');
  const facesHtml = f.faces.map(fa => `<span class="badge">${fa.person_name ? '👤 ' + esc(fa.person_name) : 'face (cluster ' + (fa.cluster_id ?? '—') + ')'}${fa.frame_time != null ? ' @' + fa.frame_time + 's' : ''}</span>`).join('');
  const recs = f.recommendations.map(r => `<div class="kv" style="margin-top:4px"><span class="badge ${r.status === 'pending' ? 'warn' : ''}">${esc(r.collection)} · ${esc(r.status)}</span> ${esc(r.explanation)} <span class="conf">${(r.confidence * 100).toFixed(0)}%</span></div>`).join('');
  const div = document.createElement('div');
  div.className = 'modal-bg';
  div.innerHTML = `<div class="modal">
    <img class="big" src="/api/thumb/${id}" onerror="this.remove()">
    <h1>${f.kind === 'video' ? '🎞️ ' : ''}${esc(f.name)}</h1>
    <div class="kv"><b>${f.width || '?'}×${f.height || '?'}</b> · ${fmtSize(f.size)} · taken ${fmtDate(f.taken_time)} ·
      quality <b>${f.quality ?? '—'}</b> · status ${esc(f.status)}${f.duration ? ` · ${f.duration.toFixed(0)}s` : ''}</div>
    ${f.summary ? `<p class="kv" style="margin-top:8px">${esc(f.summary)}</p>` : ''}
    ${f.frames ? `<p class="kv">${f.frames.length} frames sampled · ${f.frames.filter(x => x.is_representative).length} representative scenes</p>` : ''}
    <h2>Labels</h2>${labels || '<span class="muted">none</span>'}
    <h2>Faces</h2>${facesHtml || '<span class="muted">none detected</span>'}
    <h2>Recommendations</h2>${recs || '<span class="muted">none</span>'}
    <div class="row" style="margin-top:14px"><button onclick="this.closest('.modal-bg').remove()">Close</button></div>
  </div>`;
  div.onclick = e => { if (e.target === div) div.remove(); };
  document.body.appendChild(div);
};

window.decide = async (id, decision) => {
  await api(`/recommendations/${id}/decide`, { method: 'POST', body: { decision } });
  const el = $('#rec-' + id); if (el) { el.style.opacity = .35; el.querySelectorAll('button').forEach(b => b.remove()); }
  toast(decision === 'approve' ? 'Approved — execute from the Execute button when ready' : 'Rejected — the AI will learn from this');
};

window.bulkDecide = async decision => {
  const shown = window._shown || [];
  if (!shown.length) return toast('Nothing to decide');
  if (!confirm(`${decision === 'approve' ? 'Approve' : 'Reject'} all ${shown.length} shown recommendations?`)) return;
  for (const r of shown) await api(`/recommendations/${r.id}/decide`, { method: 'POST', body: { decision } }).catch(() => {});
  toast('Done'); render();
};

window.executeApproved = async () => {
  const d = await api('/recommendations?status=approved&page_size=200');
  const trash = d.recommendations.filter(r => r.action === 'trash');
  if (!trash.length) return toast('No approved trash actions to execute');
  const vids = trash.filter(r => r.kind === 'video');
  let msg = `Execute ${trash.length} approved trash action(s)?\n\nFiles will be moved to trash (reversible), never permanently deleted.`;
  if (vids.length) msg += `\n\n⚠️ This includes ${vids.length} VIDEO(S):\n` + vids.slice(0, 10).map(v => '  • ' + v.name).join('\n');
  if (!confirm(msg)) return;
  if (vids.length && !confirm(`Second confirmation for videos: really trash ${vids.length} video(s)?`)) return;
  const res = await api('/actions/execute', { method: 'POST', body: { rec_ids: trash.map(r => r.id) } });
  toast(`Executed ${res.executed.length}, skipped ${res.skipped.length}, errors ${res.errors.length}`, 6000);
  if (res.errors.length) console.warn(res.errors), alert(res.errors.map(e => `rec ${e.rec_id}: ${e.error}`).join('\n'));
  render();
};

window.undoAction = async id => {
  await api(`/actions/${id}/undo`, { method: 'POST' }).then(() => { toast('Restored'); render(); })
    .catch(e => toast('Undo failed: ' + e.message, 6000));
};

window.resolveGroup = async (gid, fid, others, kind) => {
  if (!confirm(`Keep this file and move the other ${others} cop${others === 1 ? 'y' : 'ies'} to trash?\n\nUndoable from the Activity tab.`)) return;
  if (kind === 'video' && !confirm('These are VIDEOS — second confirmation to trash the other copies?')) return;
  const r = await api(`/duplicates/${gid}/resolve`, { method: 'POST', body: { keep_file_id: fid } });
  toast(`Group resolved — ${r.executed.length} trashed${r.errors.length ? `, ${r.errors.length} errors` : ''}`);
  render();
};

window.setKeep = async (gid, fid) => {
  await api(`/duplicates/${gid}/keep`, { method: 'POST', body: { file_id: fid } });
  toast('Keep choice updated'); render();
};

window.clToggle = (kind, id) => {
  const cs = window._cleanup;
  const set = kind === 'c' ? cs.keepC : cs.keepP;
  set.has(id) ? set.delete(id) : set.add(id);
  render();
};

/* ---------- Finder-style selectable grid for Cleanup results ---------- */
const CL_CHUNK = 400;
let clG = null;   // {files, sel:Set(ids), anchor, focus, rendered}

function clGridInit(files, r) {
  clG = { files, sel: new Set(), anchor: 0, focus: 0, rendered: 0 };
  $('#cl-results').innerHTML = `
    <h2>${files.length} files contain none of your selected people
      <span class="muted">(${r.kept_files} of ${r.total_with_faces} face-files kept — someone you know is in them)</span></h2>
    <div class="row actionbar">
      <button id="cl-selall">Select all (⌘A)</button>
      <button id="cl-selnone">None (esc)</button>
      <button class="danger" id="cl-trash">Trash selected (⌫)…</button>
      <span class="muted" id="cl-count">0 selected · click toggles · shift+click / shift+arrows add ranges · space preview</span>
    </div>
    <div class="grid" id="cl-grid"></div><div id="cl-more"></div>`;
  clRenderChunk();
  new IntersectionObserver((es, obs) => {
    if (es.some(e => e.isIntersecting)) { clRenderChunk(); if (clG.rendered >= clG.files.length) obs.disconnect(); }
  }).observe($('#cl-more'));
  $('#cl-selall').onclick = () => clSetSel(clG.files.map(f => f.id));
  $('#cl-selnone').onclick = () => clSetSel([]);
  $('#cl-trash').onclick = clTrashSel;
}

function clRenderChunk() {
  if (!clG || clG.rendered >= clG.files.length) return;
  const end = Math.min(clG.rendered + CL_CHUNK, clG.files.length);
  const html = clG.files.slice(clG.rendered, end).map((f, i) => {
    const idx = clG.rendered + i;
    return `<div class="tile selectable ${clG.sel.has(f.id) ? 'sel' : ''}" id="clf-${f.id}" data-idx="${idx}"
      onclick="clClick(event,${idx})" ondblclick="showFile(${f.id})">
      ${thumb(f.id)}
      <div class="cap">${f.kind === 'video' ? '🎞️ ' : ''}${esc(f.name)}<br>
        ${f.n_faces} face(s)${f.closest_known_sim ? ` · closest known: ${(f.closest_known_sim * 100).toFixed(0)}%` : ''}</div>
    </div>`;
  }).join('');
  $('#cl-grid').insertAdjacentHTML('beforeend', html);
  clG.rendered = end;
}

function clRecount() {
  const el = $('#cl-count');
  if (el) el.textContent = `${clG.sel.size} of ${clG.files.length} selected`;
}

function clSetSel(ids) {
  const next = new Set(ids);
  for (const f of clG.files) {              // update only tiles whose state changed
    const was = clG.sel.has(f.id), is = next.has(f.id);
    if (was !== is) $('#clf-' + f.id)?.classList.toggle('sel', is);
  }
  clG.sel = next;
  clRecount();
}

function clFocusTo(idx, extend) {
  // Selection is ADDITIVE everywhere: plain arrows only move the focus ring
  // (your selection is never wiped by navigating); shift+arrows ADD the
  // anchor..focus range to the selection.
  idx = Math.max(0, Math.min(clG.files.length - 1, idx));
  while (idx >= clG.rendered) clRenderChunk();   // ensure target is in the DOM
  document.querySelector('.tile.focused')?.classList.remove('focused');
  clG.focus = idx;
  if (extend) {
    const [a, b] = [Math.min(clG.anchor, idx), Math.max(clG.anchor, idx)];
    const next = new Set(clG.sel);
    for (const f of clG.files.slice(a, b + 1)) next.add(f.id);
    clSetSel([...next]);
  } else {
    clG.anchor = idx;   // plain move: selection untouched, anchor follows focus
  }
  const el = $('#clf-' + clG.files[idx].id);
  if (el) { el.classList.add('focused'); el.scrollIntoView({ block: 'nearest' }); }
}

window.clClick = (ev, idx) => {
  const id = clG.files[idx].id;
  document.querySelector('.tile.focused')?.classList.remove('focused');
  if (ev.shiftKey) {
    // add the whole range to the existing selection
    const [a, b] = [Math.min(clG.anchor, idx), Math.max(clG.anchor, idx)];
    const next = new Set(clG.sel);
    for (const f of clG.files.slice(a, b + 1)) next.add(f.id);
    clG.focus = idx;
    clSetSel([...next]);
  } else {
    // plain click toggles this one file in/out — never clears the rest
    const next = new Set(clG.sel);
    next.has(id) ? next.delete(id) : next.add(id);
    clG.anchor = clG.focus = idx;
    clSetSel([...next]);
  }
  $('#clf-' + id)?.classList.add('focused');
};

function clCols() {
  const tiles = document.querySelectorAll('#cl-grid .tile');
  if (tiles.length < 2) return 1;
  const top0 = tiles[0].offsetTop;
  for (let i = 1; i < tiles.length; i++) if (tiles[i].offsetTop !== top0) return i;
  return tiles.length;
}

async function clTrashSel() {
  if (!clG || !clG.sel.size) return toast('Nothing selected');
  const chosen = clG.files.filter(f => clG.sel.has(f.id));
  const vids = chosen.filter(f => f.kind === 'video');
  if (!confirm(`Move ${chosen.length} file(s) to trash?\n\nThey go to the app's local trash (undoable from the Activity tab), not permanent deletion.`)) return;
  if (vids.length && !confirm(`Second confirmation: ${vids.length} of these are VIDEOS. Trash them too?`)) return;
  const res = await api('/cleanup/trash', { method: 'POST', body: { file_ids: [...clG.sel] } });
  toast(`Trashed ${res.executed.length} file(s) — undo anytime from Activity`, 6000);
  // drop trashed tiles in place — no full reload, keeps your scroll position
  const gone = new Set(clG.sel);
  clG.files = clG.files.filter(f => !gone.has(f.id));
  gone.forEach(id => $('#clf-' + id)?.remove());
  clG.sel = new Set();
  clG.rendered = document.querySelectorAll('#cl-grid .tile').length;
  document.querySelectorAll('#cl-grid .tile').forEach((t, i) => { t.dataset.idx = i; t.setAttribute('onclick', `clClick(event,${i})`); });
  clG.anchor = clG.focus = 0;
  clRecount();
}

document.addEventListener('keydown', ev => {
  if (!clG || !location.hash.startsWith('#cleanup')) return;
  if (document.querySelector('.modal-bg')) { if (ev.key === 'Escape') document.querySelector('.modal-bg').remove(); return; }
  if (/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName || '')) return;
  const cols = clCols();
  const moves = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -cols, ArrowDown: cols };
  if (ev.key in moves) { ev.preventDefault(); clFocusTo(clG.focus + moves[ev.key], ev.shiftKey); }
  else if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 'a') { ev.preventDefault(); clSetSel(clG.files.map(f => f.id)); }
  else if (ev.key === 'Escape') clSetSel([]);
  else if (ev.key === ' ') { ev.preventDefault(); const f = clG.files[clG.focus]; if (f) showFile(f.id); }
  else if (ev.key === 'Backspace' || ev.key === 'Delete') { ev.preventDefault(); clTrashSel(); }
});

window.labelCluster = async cid => {
  const name = $('#cl-' + cid).value.trim();
  if (!name) return toast('Enter a name first');
  const r = await api('/people/label', { method: 'POST', body: { cluster_id: cid, name } });
  toast(`Tagged ${r.files_tagged} files as ${name}`); render();
};

/* ---------- router ---------- */
function render() {
  const [page, sub] = (location.hash.slice(1) || 'dashboard').split('/');
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('active', a.dataset.page === page));
  (pages[page] || pages.dashboard)(sub).catch(e => { main.innerHTML = `<div class="panel">Error: ${esc(e.message)}</div>`; });
}
window.addEventListener('hashchange', render);
render();
pollJob();
setInterval(pollJob, 5000);
