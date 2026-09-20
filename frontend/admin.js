/* ==========================================================================
 * Varanasi Hospital — Staff Portal
 *
 * Talks only to /api/admin/*. This file deliberately holds NO credential: the
 * staff session lives in an httpOnly cookie that page scripts cannot read, so
 * an injected script has nothing here to steal. The browser attaches it to
 * same-origin requests automatically.
 * ======================================================================== */

'use strict';

const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const THEME_KEY = 'varanasi.theme';
const REFRESH_MS = 30000;

const state = {
  signedIn: false,
  tab: 'dashboard',
  departments: [],
  timer: null,
};

/* --- utilities -------------------------------------------------------- */

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function to12Hour(hhmm) {
  const [h, m] = String(hhmm).split(':').map(Number);
  if (Number.isNaN(h)) return hhmm;
  const suffix = h < 12 ? 'AM' : 'PM';
  return `${h % 12 || 12}:${String(m).padStart(2, '0')} ${suffix}`;
}

function formatDate(iso) {
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short' });
}

let toastTimer = null;
function toast(message, tone) {
  const box = $('#admin-toast');
  box.textContent = message;
  box.className = `admin-toast${tone ? ` admin-toast--${tone}` : ''}`;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 4000);
}

/* --- API -------------------------------------------------------------- */

async function api(path, options = {}) {
  const headers = { Accept: 'application/json', ...(options.headers || {}) };
  if (options.body) headers['Content-Type'] = 'application/json';

  // same-origin is the default, and it is what carries the session cookie.
  const res = await fetch(path, { ...options, headers, credentials: 'same-origin' });

  if (res.status === 401) {
    // The session is gone; drop straight back to sign-in rather than
    // leaving stale patient data on screen.
    signOut(true);
    throw new Error('Session expired');
  }

  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
  return body;
}

/* --- auth ------------------------------------------------------------- */

async function signIn(passcode) {
  const error = $('#login-error');
  error.hidden = true;
  try {
    await api('/api/admin/login', {
      method: 'POST',
      body: JSON.stringify({ passcode }),
    });
    // The response body carries a token for API clients; the browser ignores
    // it and uses the httpOnly cookie the server just set.
    state.signedIn = true;
    showPortal();
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  }
}

function signOut(silent) {
  if (state.signedIn) {
    // Best effort: the server drops the session and clears the cookie, but
    // the UI must not wait on it.
    fetch('/api/admin/logout', { method: 'POST', credentials: 'same-origin' })
      .catch(() => {});
  }
  state.signedIn = false;
  stopRefresh();
  $('#portal-view').hidden = true;
  $('#login-view').hidden = false;
  $('#signout-button').hidden = true;
  if (!silent) toast('Signed out.');
}

/* --- rendering: dashboard --------------------------------------------- */

const STAT_TILES = [
  { key: 'today',           label: "Today's appointments", tone: 'brand' },
  { key: 'today_remaining', label: 'Still to come today',  tone: 'info'  },
  { key: 'today_completed', label: 'Completed today',      tone: 'good'  },
  { key: 'upcoming_7_days', label: 'Next 7 days',          tone: 'brand' },
  { key: 'active_total',    label: 'Active bookings',      tone: 'info'  },
  { key: 'cancelled_total', label: 'Cancelled',            tone: 'warn'  },
  { key: 'no_show_total',   label: 'No shows',             tone: 'warn'  },
  { key: 'all_time',        label: 'All records',          tone: 'muted' },
];

function renderDashboard(data) {
  $('#dash-stamp').textContent =
    `Hospital date ${formatDate(data.hospital_date)} · refreshed ${new Date().toLocaleTimeString('en-IN')}`;

  $('#stat-grid').innerHTML = STAT_TILES.map((tile) => `
    <div class="stat stat--${tile.tone}">
      <div class="stat__value">${data.totals[tile.key] ?? 0}</div>
      <div class="stat__label">${escapeHtml(tile.label)}</div>
    </div>`).join('');

  const next = data.next_up || [];
  $('#next-up').innerHTML = next.length
    ? next.map(appointmentCard).join('')
    : '<p class="empty-note">Nothing booked yet.</p>';

  $('#by-department').innerHTML = barList(
    (data.by_department || []).map((d) => ({ label: d.department, value: d.count }))
  );
  $('#by-doctor').innerHTML = barList(
    (data.by_doctor || []).slice(0, 6).map((d) => ({
      label: d.doctor_name, value: d.upcoming, sub: d.department,
    }))
  );
}

function barList(items) {
  if (!items.length) return '<p class="empty-note">No data yet.</p>';
  const max = Math.max(...items.map((i) => i.value), 1);
  return items.map((item) => `
    <div class="bar">
      <div class="bar__head">
        <span class="bar__label">${escapeHtml(item.label)}${
          item.sub ? ` <span class="bar__sub">${escapeHtml(item.sub)}</span>` : ''
        }</span>
        <span class="bar__value">${item.value}</span>
      </div>
      <div class="bar__track"><div class="bar__fill" style="width:${(item.value / max) * 100}%"></div></div>
    </div>`).join('');
}

/* --- rendering: appointments ------------------------------------------ */

const STATUS_LABELS = {
  confirmed: 'Confirmed',
  rescheduled: 'Rescheduled',
  completed: 'Completed',
  no_show: 'No show',
  cancelled: 'Cancelled',
};

function appointmentCard(appt, withActions) {
  const status = appt.status || 'unknown';
  const actions = withActions ? `
    <div class="appt__actions">
      ${status !== 'completed' ? `<button type="button" class="chip-btn chip-btn--good" data-set="completed" data-id="${escapeHtml(appt.appointment_id)}">Mark complete</button>` : ''}
      ${status !== 'no_show' ? `<button type="button" class="chip-btn chip-btn--warn" data-set="no_show" data-id="${escapeHtml(appt.appointment_id)}">No show</button>` : ''}
      ${status !== 'cancelled' ? `<button type="button" class="chip-btn chip-btn--bad" data-set="cancelled" data-id="${escapeHtml(appt.appointment_id)}">Cancel</button>` : ''}
      ${status !== 'confirmed' ? `<button type="button" class="chip-btn" data-set="confirmed" data-id="${escapeHtml(appt.appointment_id)}">Reopen</button>` : ''}
    </div>` : '';

  return `
    <article class="result-card appt">
      <div class="appt__when">
        <span class="appt__date">${escapeHtml(formatDate(appt.date))}</span>
        <span class="appt__time">${escapeHtml(to12Hour(appt.time))}</span>
      </div>
      <div class="appt__body">
        <div class="result-card__title">${escapeHtml(appt.patient_name || 'Unnamed')}
          <span class="status-pill status-pill--${escapeHtml(status)}">${escapeHtml(STATUS_LABELS[status] || status)}</span>
        </div>
        <div class="result-card__sub">${escapeHtml(appt.doctor_name || '')} · ${escapeHtml(appt.department || '')}</div>
        <div class="result-card__meta">
          <strong>Phone</strong> ${escapeHtml(appt.patient_phone || '—')} ·
          <strong>Ref</strong> <code>${escapeHtml(appt.appointment_id)}</code>
          ${appt.reason ? `<br><strong>Reason</strong> ${escapeHtml(appt.reason)}` : ''}
          ${appt.staff_note ? `<br><strong>Staff note</strong> ${escapeHtml(appt.staff_note)}` : ''}
        </div>
        ${actions}
      </div>
    </article>`;
}

function filterParams() {
  const params = new URLSearchParams();
  const scope = $('#f-scope').value;
  const status = $('#f-status').value;
  const dept = $('#f-department').value;
  const q = $('#f-query').value.trim();
  if (scope) params.set('scope', scope);
  if (status) params.set('status', status);
  if (dept) params.set('department', dept);
  if (q) params.set('q', q);
  return params;
}

async function loadAppointments() {
  const body = await api(`/api/admin/appointments?${filterParams()}`);
  $('#appt-count').textContent =
    `${body.count} ${body.count === 1 ? 'appointment' : 'appointments'}`;
  $('#appt-list').innerHTML = body.count
    ? body.appointments.map((a) => appointmentCard(a, true)).join('')
    : '<p class="empty-note">Nothing matches these filters.</p>';
}

async function setStatus(id, status) {
  try {
    await api(`/api/admin/appointments/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    });
    toast(`${id} → ${STATUS_LABELS[status] || status}`, 'good');
    await loadAppointments();
  } catch (err) {
    toast(err.message, 'bad');
  }
}

/* --- rendering: doctors & escalations ---------------------------------- */

function renderDoctors(list) {
  $('#doctor-board').innerHTML = list.map((doc) => `
    <article class="result-card">
      <div class="result-card__title">${escapeHtml(doc.name)}
        ${doc.today_count ? `<span class="status-pill status-pill--confirmed">${doc.today_count} today</span>` : ''}
      </div>
      <div class="result-card__sub">${escapeHtml(doc.specialization || doc.department || '')}</div>
      <div class="result-card__meta">
        <strong>Upcoming</strong> ${doc.upcoming_count} ·
        <strong>Department</strong> ${escapeHtml(doc.department || '—')}
        ${doc.next_appointment
          ? `<br><strong>Next</strong> ${escapeHtml(formatDate(doc.next_appointment.date))} at ${escapeHtml(to12Hour(doc.next_appointment.time))}`
          : '<br><em>No upcoming appointments.</em>'}
      </div>
    </article>`).join('') || '<p class="empty-note">No doctors loaded.</p>';
}

function renderEscalations(data) {
  $('#esc-notice').textContent = data.in_memory_notice || '';
  $('#emergency-log').innerHTML = (data.emergencies || []).map((e) => `
    <article class="result-card result-card--alert">
      <div class="result-card__title">${escapeHtml(e.severity || 'emergency')}</div>
      <div class="result-card__meta">
        <strong>At</strong> ${escapeHtml(e.timestamp || e.created_at || '—')}
        ${e.signals?.length ? `<br><strong>Signals</strong> ${escapeHtml(e.signals.join(', '))}` : ''}
      </div>
    </article>`).join('') || '<p class="empty-note">No emergency detections this run.</p>';

  $('#handoff-log').innerHTML = (data.handoffs || []).map((h) => `
    <article class="result-card">
      <div class="result-card__title">${escapeHtml(h.reason || 'Handoff')}
        <span class="status-pill status-pill--${escapeHtml(h.status || 'queued')}">${escapeHtml(h.status || 'queued')}</span>
      </div>
      <div class="result-card__meta">
        ${h.patient_name ? `<strong>Patient</strong> ${escapeHtml(h.patient_name)} · ` : ''}
        ${h.patient_phone ? `<strong>Phone</strong> ${escapeHtml(h.patient_phone)}` : ''}
        ${h.notes ? `<br>${escapeHtml(h.notes)}` : ''}
        <br><strong>At</strong> ${escapeHtml(h.timestamp || h.created_at || '—')}
      </div>
    </article>`).join('') || '<p class="empty-note">No handoff requests this run.</p>';
}

/* --- tabs & refresh ---------------------------------------------------- */

async function loadTab(tab) {
  try {
    if (tab === 'dashboard') renderDashboard(await api('/api/admin/dashboard'));
    else if (tab === 'appointments') await loadAppointments();
    else if (tab === 'doctors') renderDoctors((await api('/api/admin/doctors')).doctors);
    else if (tab === 'escalations') renderEscalations(await api('/api/admin/escalations'));
  } catch (err) {
    if (err.message !== 'Session expired') toast(err.message, 'bad');
  }
}

function selectTab(tab) {
  state.tab = tab;
  $$('.admin-tab').forEach((b) => b.classList.toggle('is-active', b.dataset.tab === tab));
  $$('.admin-panel').forEach((p) => { p.hidden = p.id !== `tab-${tab}`; });
  loadTab(tab);
}

function startRefresh() {
  stopRefresh();
  state.timer = setInterval(() => loadTab(state.tab), REFRESH_MS);
}

function stopRefresh() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
}

/* --- departments ------------------------------------------------------- */

async function loadDepartments() {
  try {
    const body = await fetch('/api/departments', { headers: { Accept: 'application/json' } })
      .then((r) => r.json());
    const names = (body.departments || []).map((d) => d.name).filter(Boolean);
    state.departments = names;
    const select = $('#f-department');
    names.forEach((name) => {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    });
  } catch { /* filters still work without the list */ }
}

/* --- theme ------------------------------------------------------------- */

function initTheme() {
  const toggle = $('#switch');
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch { /* ignore */ }
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  const apply = (dark, persist) => {
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    toggle.checked = dark;
    if (persist) {
      try { localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light'); } catch { /* ignore */ }
    }
  };
  apply(saved ? saved === 'dark' : media.matches, false);
  toggle.addEventListener('change', () => apply(toggle.checked, true));
}

/* --- wiring ------------------------------------------------------------ */

function showPortal() {
  $('#login-view').hidden = true;
  $('#portal-view').hidden = false;
  $('#signout-button').hidden = false;
  selectTab(state.tab);
  startRefresh();
}

async function init() {
  initTheme();
  loadDepartments();

  $('#login-form').addEventListener('submit', (event) => {
    event.preventDefault();
    signIn($('#passcode').value);
    $('#passcode').value = '';
  });

  $$('.admin-tab').forEach((btn) => {
    btn.addEventListener('click', () => selectTab(btn.dataset.tab));
  });

  document.addEventListener('click', (event) => {
    const setBtn = event.target.closest('[data-set]');
    if (setBtn) { setStatus(setBtn.dataset.id, setBtn.dataset.set); return; }
    const action = event.target.closest('[data-action]');
    if (!action) return;
    if (action.dataset.action === 'signout') signOut();
    // A plain link cannot carry the Authorization header, so the CSV is
    // fetched and handed to the browser as a blob instead.
    if (action.dataset.action === 'export') exportCsv();
  });

  ['#f-scope', '#f-status', '#f-department'].forEach((sel) => {
    $(sel).addEventListener('change', loadAppointments);
  });
  let searchTimer = null;
  $('#f-query').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadAppointments, 250);
  });

  // Pause polling while the tab is hidden — no point refreshing a screen
  // nobody is looking at.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopRefresh();
    else if (state.signedIn) { loadTab(state.tab); startRefresh(); }
  });

  // The cookie is invisible to scripts, so just ask the server whether this
  // browser already has a live session.
  try {
    await api('/api/admin/session');
    state.signedIn = true;
    showPortal();
    return;
  } catch { /* not signed in — fall through */ }
  $('#login-view').hidden = false;
}

async function exportCsv() {
  try {
    const res = await fetch(`/api/admin/export.csv?${filterParams()}`, {
      credentials: 'same-origin',
    });
    if (!res.ok) throw new Error(`Export failed (${res.status})`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `appointments-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast('CSV downloaded.', 'good');
  } catch (err) {
    toast(err.message, 'bad');
  }
}

init();
