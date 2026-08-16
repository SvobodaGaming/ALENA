/* АЛЁНА – интерфейс: список проверок, детали, загрузка, общие элементы. */
(() => {
  "use strict";

  const $  = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = s => String(s == null ? '' : s)
    .replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  /* Токен сессии: сервер принимает изменяющий запрос только с ним, иначе
     страницу можно было бы отправить с чужого сайта от имени вошедшего. */
  const CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  const post = (url, opts = {}) => fetch(url, Object.assign({}, opts, {
    method: 'POST',
    headers: Object.assign({ 'X-CSRF-Token': CSRF }, opts.headers || {}),
  }));

  /* ── Общее: тема, меню, всплывающие сообщения ── */

  const themeBtn = $('#theme-btn');
  if (themeBtn) {
    const root = document.documentElement;
    const saved = localStorage.getItem('alena-theme');
    if (saved) root.dataset.theme = saved;
    themeBtn.onclick = () => {
      const dark = matchMedia('(prefers-color-scheme: dark)').matches;
      const cur = root.dataset.theme || (dark ? 'dark' : 'light');
      root.dataset.theme = cur === 'dark' ? 'light' : 'dark';
      localStorage.setItem('alena-theme', root.dataset.theme);
      if (typeof window.redrawCharts === 'function') window.redrawCharts();
    };
  }

  const burger = $('#burger');
  if (burger) burger.onclick = () => $('#rail').classList.toggle('open');

  function toast(text) {
    const t = document.createElement('div');
    t.className = 'toast';
    t.textContent = text;
    t.style.cssText = 'position:fixed;left:50%;bottom:var(--space-5);transform:translateX(-50%);' +
      'background:var(--ink);color:var(--surface);padding:var(--space-2) var(--space-4);' +
      'border-radius:var(--radius-pill);font-size:var(--text-13);z-index:60;box-shadow:var(--shadow-3)';
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  }
  window.alenaToast = toast;

  /* ── Подтверждение действия ── */

  const modalBack = $('#modal-back');
  let modalOk = null;

  function confirmAction({ title, sub = '', body = '', okText = 'Подтвердить',
                           cancelText = 'Отмена', danger = false, wide = false, onOk }) {
    if (!modalBack) { if (confirm(title)) onOk && onOk(); return; }
    $('#modal-title').textContent = title;
    $('#modal-sub').textContent = sub;
    $('#modal-body').innerHTML = body;
    $('#modal-cancel').textContent = cancelText;
    modalBack.querySelector('.modal').classList.toggle('wide', wide);
    const ok = $('#modal-ok');
    ok.textContent = okText;
    ok.className = 'btn ' + (danger ? 'danger' : 'primary');
    modalOk = onOk;
    modalBack.hidden = false;
    ok.focus();
  }
  window.alenaConfirm = confirmAction;

  function closeModal() { if (modalBack) { modalBack.hidden = true; modalOk = null; } }
  if (modalBack) {
    $('#modal-cancel').onclick = closeModal;
    modalBack.onclick = e => { if (e.target === modalBack) closeModal(); };
    $('#modal-ok').onclick = () => { const f = modalOk; closeModal(); if (f) f(); };
    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
  }

  /* Формы, требующие подтверждения: data-confirm="Вопрос" */
  $$('form[data-confirm]').forEach(form => {
    form.addEventListener('submit', e => {
      if (form.dataset.confirmed === 'yes') return;
      e.preventDefault();
      confirmAction({
        title: form.dataset.confirm,
        sub: form.dataset.confirmSub || '',
        okText: form.dataset.confirmOk || 'Подтвердить',
        danger: form.dataset.confirmDanger === 'yes',
        onOk: () => { form.dataset.confirmed = 'yes'; form.submit(); },
      });
    });
  });

  /* ── Копирование в буфер ── */

  async function copyText(text, okMessage = 'Скопировано') {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        // http без TLS: clipboard-API недоступен, остаётся старый способ
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;opacity:0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      toast(okMessage);
    } catch (e) {
      toast('Не удалось скопировать – выделите текст вручную');
    }
  }

  /* ── Веса критериев (страницы «Новая проверка» и «Настройки») ── */

  function recalcWeights() {
    const rows = $$('.crit');
    if (!rows.length) return;
    const val = r => {
      const n = parseInt(r.querySelector('.w-in').value, 10);
      return Math.max(0, Math.min(100, isNaN(n) ? 0 : n));
    };
    const on = r => r.querySelector('input[type="checkbox"]').checked;
    const active = rows.filter(on);
    const total = active.reduce((s, r) => s + val(r), 0);

    rows.forEach(r => {
      const out = r.querySelector('.crit-share');
      if (!on(r)) { out.textContent = '–'; out.title = 'Критерий снят'; return; }
      // Сумма нулей – вырожденный случай, критерии считаются равными.
      const share = total ? val(r) * 100 / total : 100 / active.length;
      out.textContent = (share < 9.95 ? share.toFixed(1) : Math.round(share)) + '%';
      out.title = 'Доля в рекомендуемой оценке';
    });
  }

  window.alenaRecalcWeights = recalcWeights;

  if ($('.crit')) {
    $$('.w-in').forEach(i => i.addEventListener('input', recalcWeights));
    $$('.crit input[type="checkbox"]').forEach(c => c.addEventListener('change', recalcWeights));
    const equal = $('#w-equal');
    if (equal) equal.onclick = () => {
      $$('.w-in').forEach(i => { i.value = 100; });
      recalcWeights();
    };
    recalcWeights();
  }

  /* ── Значки состояния ── */

  const ICON = {
    ok:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg>',
    warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 3.5L1.8 20.5h20.4z"/><path d="M12 10v4.4M12 17.6v.1"/></svg>',
    crit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    idle: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5v5l3 2" stroke-linecap="round"/></svg>',
  };
  const chip = (tone, text) => `<span class="chip ${tone}">${ICON[tone]}${esc(text)}</span>`;
  const gostTone = v => v >= 85 ? 'ok' : v >= 70 ? 'warn' : 'crit';
  const plagTone = (v, threshold) => v >= threshold ? 'crit' : v >= threshold * 0.6 ? 'warn' : 'ok';
  const toneVar = t => `var(--${t})`;

  const plural = (n, forms) => {
    const a = Math.abs(n) % 100, b = a % 10;
    return forms[a > 10 && a < 20 ? 2 : b > 1 && b < 5 ? 1 : b === 1 ? 0 : 2];
  };

  /* ── Дата проверки ── */

  /* Дата хранится строкой «дд.мм.гггг чч:мм»: сравнивать её как текст нельзя –
     порядок получался бы по числу месяца, а не по дате. */
  const STAMP_RE = /^(\d{2})\.(\d{2})\.(\d{4})(?:[ T](\d{2}):(\d{2}))?/;

  const stampKey = s => {
    const m = STAMP_RE.exec(String(s || ''));
    return m ? m[3] + m[2] + m[1] + (m[4] || '00') + (m[5] || '00') : '';
  };

  /* Поиск по дате: «14.08», «14.08.2026», «08.2026», «2026-08-14» и «12:34»
     должны находить проверку, поэтому дату приводим сразу к двум видам –
     как она показана и в порядке «год.месяц.день». */
  const stampForms = s => {
    const raw = String(s || '').toLowerCase();
    const m = STAMP_RE.exec(raw);
    return m ? [raw, `${m[3]}.${m[2]}.${m[1]}`] : [raw];
  };

  /* ── Текст отзыва (тот же, что складывает checker/grading.py) ── */

  const FLAW_TEXT = (() => {
    const box = $('#flaw-map');
    try { return box ? JSON.parse(box.textContent) : {}; } catch (e) { return {}; }
  })();

  function feedbackLines(st, thr, details) {
    if (st.error) return [`Файл не удалось прочитать: ${st.error}`];
    // У проверок, сделанных до появления отзывов, есть только коды критериев.
    const flaws = st.flaws
      || (st.fails || []).map(c => ({ code: c, text: FLAW_TEXT[c] || c, details: '' }));
    const lines = flaws.map(f =>
      details && f.details ? `${f.text} (${f.details})` : f.text);
    // Та же строка, что и в checker/grading.py: отзыв должен совпадать с тем,
    // что напечатано в HTML-отчёте.
    if (st.no_text) {
      lines.push('Текст из файла не извлекается – скорее всего это скан или '
        + 'нестандартные шрифты. Заимствование автоматически не проверено, '
        + 'нужна ручная проверка');
    }
    if (st.plag != null && thr != null && st.plag >= thr) {
      lines.push(`Совпадение с другой работой – ${st.plag}% (допустимый порог ${thr}%)`);
    }
    return lines;
  }

  function feedbackText(st, thr, details) {
    const lines = feedbackLines(st, thr, details);
    let head = st.fio || 'Работа';
    if (st.group) head += `, ${st.group}`;
    const body = lines.length
      ? 'Замечания по оформлению:\n' + lines.map(l => '• ' + l).join('\n')
      : 'Замечаний по оформлению нет.';
    const g = st.grade || {};
    let tail = '';
    if (g.pct != null) {
      tail = `\n\nРекомендуемая оценка за оформление: ${g.pct}%`;
      if (g.score != null) tail += ` (${g.score} из ${g.scale})`;
    }
    return `${head}\n\n${body}${tail}`;
  }

  /* ── Совпадения: свод по работам ── */

  /* Пара «Иванов ↔ Петров» занимала в таблице столько строк, сколько нашлось
     совпадений: текст отдельной строкой, картинки отдельной. Преподавателю
     нужен другой разрез – работы по алфавиту, а внутри каждой одна строка на
     человека, где текст и изображения идут вместе. Работа из этой же пачки
     видна с обеих сторон: и у Иванова, и у Петрова. */
  function groupMatches(matches, students) {
    const groupOf = new Map((students || []).map(st => [st.fio, st.group || '']));
    const people = new Map();

    const person = (fio, group) => {
      let p = people.get(fio);
      if (!p) people.set(fio, p = { fio, group: group || groupOf.get(fio) || '', links: new Map() });
      if (!p.group && group) p.group = group;
      return p;
    };
    const link = (p, fio, group, where) => {
      const key = fio + '|' + group;
      let l = p.links.get(key);
      if (!l) p.links.set(key, l = { fio, group, where, pct: null, img: false, pages: [] });
      return l;
    };

    for (const m of (matches || [])) {
      const aFio = m.a_fio || m.a;
      const aGroup = m.a_group != null ? m.a_group : (groupOf.get(aFio) || '');
      /* Дайджесты, собранные до появления отдельных полей, хранят вторую
         работу одной строкой вида «Петров П.П. · ИС-21». */
      let bFio = m.b_fio, bGroup = m.b_group;
      if (bFio == null) {
        const s = String(m.b || ''), i = s.lastIndexOf(' · ');
        bFio = i < 0 ? s : s.slice(0, i);
        bGroup = i < 0 ? '' : s.slice(i + 3);
      }
      const bNew = m.b_new != null ? m.b_new : m.where === 'в этой пачке';
      const img = m.pct == null;
      const pages = m.pages
        || (img ? (String(m.kind || '').match(/\d+/g) || []).map(Number) : []);

      const fill = l => {
        if (img) { l.img = true; l.pages.push(...pages); }
        else if (l.pct == null || m.pct > l.pct) l.pct = m.pct;
      };
      fill(link(person(aFio, aGroup), bFio, bGroup, m.where));
      if (bNew) fill(link(person(bFio, bGroup), aFio, aGroup, 'в этой пачке'));
    }

    const list = [...people.values()].map(p => {
      const links = [...p.links.values()];
      links.forEach(l => { l.pages = [...new Set(l.pages)].sort((x, y) => x - y); });
      // Доля неизвестна только у дублей картинок – они уходят вниз списка.
      links.sort((x, y) => (y.pct == null ? -1 : y.pct) - (x.pct == null ? -1 : x.pct)
                        || x.fio.localeCompare(y.fio, 'ru'));
      const pcts = links.map(l => l.pct).filter(v => v != null);
      return { fio: p.fio, group: p.group, links, top: pcts.length ? Math.max(...pcts) : null };
    });
    list.sort((a, b) => a.fio.localeCompare(b.fio, 'ru'));
    return list;
  }

  function matchRows(p, thr) {
    return p.links.map(l => {
      const what = [];
      if (l.pct != null) what.push(`текст ${l.pct}%`);
      if (l.img) what.push(l.pages.length ? `изобр. стр. ${l.pages.join(', ')}` : 'изображения');
      return `<tr>
        <td>${esc(l.fio)}${l.group ? `<br><span class="sub mono">${esc(l.group)}</span>` : ''}</td>
        <td>${esc(what.join(' · '))}</td>
        <td class="sub">${esc(l.where)}</td>
        <td class="num">${l.pct == null
          ? chip('crit', 'дубликат')
          : chip(plagTone(l.pct, thr), l.pct + '%')}</td>
      </tr>`;
    }).join('');
  }

  /* ── Экран «Проверки» ── */

  const checksRoot = $('#checks-screen');
  if (checksRoot) initChecks();

  function initChecks() {
    const seesAll = checksRoot.dataset.seesAll === '1';
    const canDelete = checksRoot.dataset.canDelete === '1';
    let records = {};
    let selected = null;
    let query = '';
    let timer = null;
    let foldOpen = false;
    let matchOpen = new Set();   // раскрытые ФИО в «Совпадениях»

    const visible = () => {
      const list = Object.entries(records).map(([id, d]) => ({ id, ...d }));
      // Новые сверху; проверки с нечитаемой датой уходят в конец списка.
      list.sort((a, b) => stampKey(b.created_at).localeCompare(stampKey(a.created_at)));
      if (!query) return list;
      const q = query.toLowerCase();
      // «2026-08-14» и «14/08» – тот же запрос, что и «14.08.2026».
      const qd = q.replace(/[/-]/g, '.');
      return list.filter(j =>
        j.id.includes(q) ||
        String((j.summary && j.summary.group) || '').toLowerCase().includes(q) ||
        stampForms(j.created_at).some(f => f.includes(qd)) ||
        ((j.summary && j.summary.students) || []).some(s => s.fio.toLowerCase().includes(q)));
    };

    async function load() {
      try {
        const res = await fetch('/jobs', { headers: { Accept: 'application/json' } });
        if (!res.ok) return;
        records = await res.json();
      } catch (e) { return; }
      render();
      const running = Object.values(records).some(d => d.status === 'processing');
      clearTimeout(timer);
      if (running) timer = setTimeout(load, 2000);
    }

    function render() {
      const list = visible();
      const ul = $('#job-list');
      const badge = $('#nav-count');
      if (badge) badge.textContent = Object.keys(records).length;

      if (!list.length) {
        ul.innerHTML = '<li class="empty">' + (query
          ? 'Ничего не найдено. Измените запрос.'
          : 'Проверок пока нет. Начните с кнопки «Новая проверка».') + '</li>';
        $('#detail-pane').innerHTML = '<div class="empty">Выберите проверку в списке слева.</div>';
        renderTiles(list);
        return;
      }
      if (!list.some(j => j.id === selected)) selected = list[0].id;

      ul.innerHTML = list.map(j => {
        const s = j.summary || {};
        const thr = j.threshold || 60;
        let tone, right;
        if (j.status === 'processing') { tone = 'idle'; right = chip('idle', (j.progress || 0) + '%'); }
        else if (j.status === 'cancelled') { tone = 'idle'; right = chip('idle', 'Прервана'); }
        else if (j.status === 'error') { tone = 'crit'; right = chip('crit', 'Ошибка'); }
        else { tone = plagTone(s.plag || 0, thr); right = chip(tone, 'Заимств. ' + (s.plag || 0) + '%'); }

        return `<li role="option" aria-selected="${j.id === selected}">
          <button class="job" data-job="${esc(j.id)}" style="--tone:${toneVar(tone)}" aria-selected="${j.id === selected}">
            <span class="job-top">
              <span class="job-id mono">#${esc(j.id.slice(0, 6))}</span>
              <span class="job-group">${esc(s.group || '–')}</span>
            </span>
            <span class="job-meta">
              <span class="mono">${j.total || 0}</span> файлов
              ${j.status === 'done' ? `· ГОСТ <span class="mono">${s.gost || 0}%</span>` : ''}
              <span style="margin-left:auto;">${right}</span>
            </span>
            <span class="job-meta"><span class="mono">${esc(j.created_at || '')}</span>${seesAll && j.owner_fio ? ' · ' + esc(j.owner_fio) : ''}</span>
          </button></li>`;
      }).join('');

      $$('#job-list .job').forEach(b => b.onclick = () => {
        if (b.dataset.job === selected) return;
        selected = b.dataset.job;
        foldOpen = false;
        matchOpen = new Set();
        const pane = $('#detail-pane');
        pane.classList.remove('swap');
        void pane.offsetWidth;          // перезапуск анимации появления
        pane.classList.add('swap');
        render();
      });
      renderDetail(list.find(j => j.id === selected));
      renderTiles(list);
    }

    function renderTiles(list) {
      const done = list.filter(j => j.status === 'done' && j.summary);
      const files = list.reduce((n, j) => n + (j.total || 0), 0);
      const gosts = done.map(j => j.summary.gost).filter(v => v);
      const matches = done.reduce((n, j) =>
        n + (j.summary.matches_total != null
          ? j.summary.matches_total : (j.summary.matches || []).length), 0);
      const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
      set('#t-checks', list.length);
      set('#t-files', files);
      set('#t-gost', gosts.length ? Math.round(gosts.reduce((a, b) => a + b, 0) / gosts.length) + '%' : '–');
      set('#t-match', matches);
    }

    function renderDetail(j) {
      const pane = $('#detail-pane');
      if (!j) { pane.innerHTML = '<div class="empty">Выберите проверку.</div>'; return; }
      const s = j.summary || {};
      const thr = j.threshold || 60;

      const head = `
        <div class="detail-head">
          <div class="detail-title">
            <h2>${esc(s.group || 'Проверка')}</h2>
            <span class="mono">#${esc(j.id)}</span>
            ${j.status === 'done' ? chip('ok', 'Готово')
              : j.status === 'processing' ? chip('idle', 'Выполняется')
              : j.status === 'cancelled' ? chip('idle', 'Прервана') : chip('crit', 'Ошибка')}
          </div>
          <div class="detail-facts">
            <span><b class="mono">${j.total || 0}</b> отчётов</span>
            <span><b class="mono">${esc(j.created_at || '')}</b></span>
            ${j.owner_fio ? `<span>Преподаватель: <b>${esc(j.owner_fio)}</b></span>` : ''}
            <span>Порог: <b class="mono">${thr}%</b></span>
          </div>
          <div class="detail-actions">
            ${j.status === 'done' ? `
              <a class="btn primary" href="/report/${esc(j.id)}" target="_blank" rel="noopener">Открыть отчёт</a>
              <a class="btn" href="/export/${esc(j.id)}">Скачать PDF</a>
              <button class="btn" id="fb-all">Отзывы студентам</button>` : ''}
            ${j.status === 'processing'
              ? `<button class="btn danger" data-stop="${esc(j.id)}">Прервать проверку</button>` : ''}
            ${canDelete ? `<button class="btn danger" data-del="${esc(j.id)}"
              ${j.status === 'processing' ? 'disabled title="Дождитесь завершения"' : ''}>Удалить</button>` : ''}
          </div>
        </div>`;

      if (j.status === 'processing') {
        pane.innerHTML = head + `
          <div class="section-body">
            <div class="progress-step"><span>${esc(j.step || '')}</span><span class="mono">${j.progress || 0}%</span></div>
            <div class="track big"><div class="fill" style="width:${j.progress || 0}%;--tone:var(--brand)"></div></div>
            <div class="stat-row">
              <span class="stat-chip">Извлечено: <b>${j.done_files || 0} / ${j.total || 0}</b></span>
              <span class="stat-chip">Пар текста: <b>${j.text_pairs || 0}</b></span>
              <span class="stat-chip">Дублей изображений: <b>${j.img_pairs || 0}</b></span>
            </div>
            <p class="hint" style="margin:14px 0 0;">Можно закрыть страницу – проверка продолжится на сервере,
              результат появится в списке.</p>
          </div>`;
        wireDelete(); wireStop(); return;
      }

      if (j.status === 'cancelled') {
        pane.innerHTML = head + `
          <div class="section-body">
            <div class="note" style="margin:0;">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 7.6v.1M12 11v5" stroke-linecap="round"/></svg>
              <span><b>Проверка прервана.</b><br>${esc(j.step || '')}</span>
            </div>
            <div style="margin-top:14px;"><a class="btn primary" href="/new">Загрузить заново</a></div>
          </div>`;
        wireDelete(); return;
      }

      if (j.status === 'error') {
        pane.innerHTML = head + `
          <div class="section-body">
            <div class="note bad" style="margin:0;">
              <span style="flex:none;width:16px;height:16px;color:var(--crit)">${ICON.crit}</span>
              <span><b>Проверка не выполнена.</b><br>${esc(j.step || 'Неизвестная ошибка')}</span>
            </div>
            <div style="margin-top:14px;"><a class="btn primary" href="/new">Загрузить заново</a></div>
          </div>`;
        wireDelete(); return;
      }

      const gt = gostTone(s.gost || 0), pt = plagTone(s.plag || 0, thr);
      const mark = s.grade == null ? null : s.grade;
      const mt = mark == null ? 'idle' : gostTone(mark);
      const markMeter = mark == null ? '' : `
          <div>
            <div class="meter-top"><span class="meter-label">Рекомендуемая оценка за оформление</span>
              <span class="meter-val" style="color:${toneVar(mt)}">${mark}%</span></div>
            <div class="track"><div class="fill" style="width:${mark}%;--tone:${toneVar(mt)}"></div></div>
            <div class="meter-note">${s.grade_score != null
                ? `${s.grade_score} из ${s.scale} · среднее по группе`
                : 'Среднее по группе'}
              ${s.weighted ? chip('warn', 'Веса изменены') : chip('ok', 'Веса равные')}</div>
          </div>`;
      const meters = `
        <div class="meters">
          <div>
            <div class="meter-top"><span class="meter-label">Соответствие ГОСТ 7.32-2017</span>
              <span class="meter-val" style="color:${toneVar(gt)}">${s.gost || 0}%</span></div>
            <div class="track"><div class="fill" style="width:${s.gost || 0}%;--tone:${toneVar(gt)}"></div></div>
            <div class="meter-note">Средняя доля пройденных критериев
              ${chip(gt, gt === 'ok' ? 'В норме' : gt === 'warn' ? 'Есть замечания' : 'Много нарушений')}</div>
          </div>
          ${markMeter}
          <div>
            <div class="meter-top"><span class="meter-label">Максимальное заимствование</span>
              <span class="meter-val" style="color:${toneVar(pt)}">${s.plag || 0}%</span></div>
            <div class="track"><div class="fill" style="width:${s.plag || 0}%;--tone:${toneVar(pt)}"></div></div>
            <div class="meter-note">Порог ${thr}%
              ${chip(pt, pt === 'ok' ? 'Ниже порога' : pt === 'warn' ? 'Близко к порогу' : 'Выше порога')}</div>
          </div>
        </div>`;

      const students = (s.students || []).map((st, i) => {
        const fb = `<td class="act"><button class="btn sm" data-fb="${i}">Отзыв</button></td>`;
        if (st.error) {
          return `<tr><td>${esc(st.fio)}</td><td class="num">–</td><td class="num">–</td>
            <td class="num">–</td>
            <td><span class="code">не обработан: ${esc(st.error)}</span></td>${fb}</tr>`;
        }
        const a = gostTone(st.gost);
        const g = st.grade || {};
        const mt2 = g.pct == null ? 'idle' : gostTone(g.pct);
        /* Работа без извлекаемого текста не сравнивалась ни с чем: «0 %»
           читалось бы как «проверено, чисто». */
        const plagCell = st.plag == null
          ? `<td class="num" title="текст не извлечён – сравнение не проводилось">–</td>`
          : `<td class="num" style="color:${toneVar(plagTone(st.plag, thr))}">${st.plag}%</td>`;
        return `<tr>
          <td>${esc(st.fio)}${st.group ? `<br><span class="sub mono">${esc(st.group)}</span>` : ''}</td>
          <td class="num" style="color:${toneVar(a)}">${st.gost}%</td>
          <td class="num" style="color:${toneVar(mt2)}"><b>${g.pct == null ? '–' : g.pct + '%'}</b>
            ${g.score != null ? `<br><span class="sub mono">${g.score} из ${g.scale}</span>` : ''}</td>
          ${plagCell}
          <td>${st.no_text
            ? '<span class="code">текст не извлечён – проверить вручную</span> '
            : ''}${st.fails.length
            ? '<span class="sub">не пройдено:</span> ' + st.fails.map(c => `<span class="code">${esc(c)}</span>`).join(' ')
            : '<span class="code pass">все критерии пройдены</span>'}</td>
          ${fb}
        </tr>`;
      }).join('');

      // Совпадений может быть десятки тысяч – в дайджесте лежат самые заметные.
      const shownMatches = (s.matches || []).length;
      const totalMatches = s.matches_total != null ? s.matches_total : shownMatches;
      const moreMatches = totalMatches > shownMatches
        ? `<p class="sub" style="padding:12px var(--space-5);margin:0;">Показаны ${shownMatches}
             самых заметных совпадений из ${totalMatches}. Полный список – в отчёте.</p>`
        : '';
      const people = groupMatches(s.matches, s.students);
      const matches = people.length ? people.map(p => `
        <div class="fold">
          <button class="fold-head" type="button" data-mfold="${esc(p.fio)}"
                  aria-expanded="${matchOpen.has(p.fio)}">
            <span class="fold-arrow">▸</span>
            <span>${esc(p.fio)}${p.group ? ` <span class="sub mono">${esc(p.group)}</span>` : ''}</span>
            <span class="spacer"></span>
            <span class="eyebrow">${p.links.length}
              ${plural(p.links.length, ['совпадение', 'совпадения', 'совпадений'])}</span>
            ${p.top == null ? chip('crit', 'дубликат') : chip(plagTone(p.top, thr), p.top + '%')}
          </button>
          <div class="fold-body"><div>
            <div class="tbl-wrap"><table>
              <thead><tr><th>Совпала с</th><th>Что совпало</th><th>Где найдено</th>
                <th class="num">Доля</th></tr></thead>
              <tbody>${matchRows(p, thr)}</tbody>
            </table></div>
          </div></div>
        </div>`).join('')
        : '<div class="empty">Совпадений выше порога не найдено.</div>';

      const fails = (s.fail_counts || []);
      const withIssues = (s.students || []).filter(st => st.error || st.fails.length).length;
      pane.innerHTML = head + meters + `
        <div class="fold">
          <button class="fold-head" type="button" id="students-fold" aria-expanded="${foldOpen}">
            <span class="fold-arrow">▸</span>
            <span>Отчёты студентов</span>
            <span class="spacer"></span>
            <span class="eyebrow">${(s.students || []).length} работ${withIssues ? ` · ${withIssues} с замечаниями` : ' · без замечаний'}</span>
          </button>
          <div class="fold-body"><div>
            <div class="tbl-wrap"><table>
              <thead><tr><th>Студент</th><th class="num">ГОСТ</th><th class="num">Оценка</th>
                <th class="num">Заимств.</th><th>Замечания</th><th></th></tr></thead>
              <tbody>${students || '<tr><td colspan="6" class="empty">Нет данных.</td></tr>'}</tbody>
            </table></div>
          </div></div>
        </div>
        <div class="section-head"><h2>Совпадения</h2><span class="spacer"></span>
          ${people.length ? `<span class="eyebrow">${people.length}
            ${plural(people.length, ['работа', 'работы', 'работ'])} с совпадениями</span>` : ''}</div>
        <div class="match-list">${matches}</div>
        ${moreMatches}
        ${fails.length ? `<details class="violations">
          <summary>Какие критерии ГОСТ чаще всего не пройдены в этой пачке</summary>
          <div class="viol-body">${fails.map(([code, n]) => `<span class="code">${esc(code)} · ${n}</span>`).join('')}</div>
        </details>` : ''}`;
      wireDelete();
      wireFold();
      wireMatchFolds();
      wireFeedback(s, thr);
    }

    /* Раскрытые ФИО переживают перерисовку: список обновляется каждые две
       секунды, пока проверка идёт. */
    function wireMatchFolds() {
      $$('#detail-pane [data-mfold]').forEach(b => {
        b.onclick = () => {
          const open = b.getAttribute('aria-expanded') !== 'true';
          if (open) matchOpen.add(b.dataset.mfold); else matchOpen.delete(b.dataset.mfold);
          b.setAttribute('aria-expanded', open);
        };
      });
    }

    /* Готовый отзыв: то же, что видно в таблице, но словами и одним куском –
       преподаватель копирует его на портал, ничего не переписывая. */
    function wireFeedback(s, thr) {
      const list = s.students || [];
      $$('#detail-pane [data-fb]').forEach(b => {
        b.onclick = () => showFeedback(list[+b.dataset.fb], thr);
      });
      const all = $('#fb-all');
      if (all) all.onclick = () => showFeedback(list, thr);
    }

    function showFeedback(target, thr) {
      const many = Array.isArray(target);
      if (many && !target.length) { toast('В этой проверке нет работ'); return; }
      const build = det => many
        ? target.map(st => feedbackText(st, thr, det)).join('\n\n––––––––\n\n')
        : feedbackText(target, thr, det);

      confirmAction({
        title: many ? 'Отзывы по всей пачке' : 'Отзыв для портала',
        sub: many ? `${target.length} работ, подряд одним текстом`
                  : (target.fio || '') + (target.group ? ` · ${target.group}` : ''),
        body: `
          <label class="check" style="margin-bottom:10px;">
            <input type="checkbox" id="fb-details">
            <span>С подробностями проверки – что именно нашлось</span>
          </label>
          <textarea id="fb-text" class="fb-text" rows="${many ? 16 : 11}"
            aria-label="Текст отзыва">${esc(build(false))}</textarea>
          <p class="hint" style="margin:8px 0 0;">Текст можно поправить прямо здесь – копируется то,
            что осталось в поле.</p>`,
        okText: 'Копировать',
        cancelText: 'Закрыть',
        wide: true,
        onOk: () => copyText($('#fb-text').value,
                             many ? 'Отзывы скопированы' : 'Отзыв скопирован'),
      });

      const box = $('#fb-details');
      if (box) box.onchange = () => { $('#fb-text').value = build(box.checked); };
    }

    /* Список студентов свёрнут по умолчанию; состояние переживает
       перерисовку при опросе выполняющейся проверки. */
    function wireFold() {
      const head = $('#students-fold');
      if (!head) return;
      head.onclick = () => {
        foldOpen = head.getAttribute('aria-expanded') !== 'true';
        head.setAttribute('aria-expanded', foldOpen);
      };
    }

    /* Прервать идущую проверку: партию загрузили не ту, порог задали не тот –
       ждать полчаса до конца, чтобы начать заново, незачем. */
    function wireStop() {
      const b = $('#detail-pane [data-stop]');
      if (!b) return;
      b.onclick = () => confirmAction({
        title: 'Прервать проверку?',
        sub: 'Проверка остановится на ближайшем шаге. Отчёт сформирован не будет, '
           + 'отпечатки в базу не попадут.',
        body: `<p style="margin:0;font-size:13px;">Проверка <b class="mono">#${esc(b.dataset.stop)}</b></p>`,
        okText: 'Прервать', danger: true,
        onOk: async () => {
          const res = await post(`/jobs/${b.dataset.stop}/cancel`);
          const data = await res.json().catch(() => ({}));
          if (res.ok) { toast('Останавливаем проверку…'); load(); }
          else toast(data.error || 'Не удалось прервать проверку');
        },
      });
    }

    function wireDelete() {
      const b = $('#detail-pane [data-del]');
      if (!b || b.disabled) return;
      b.onclick = () => confirmAction({
        title: 'Удалить проверку?',
        sub: 'Отчёт и запись в истории будут удалены. Отпечатки студентов останутся в базе – их удаляют отдельно.',
        body: `<p style="margin:0;font-size:13px;">Проверка <b class="mono">#${esc(b.dataset.del)}</b></p>`,
        okText: 'Удалить', danger: true,
        onOk: async () => {
          const res = await post(`/jobs/${b.dataset.del}/delete`);
          const data = await res.json().catch(() => ({}));
          if (res.ok) { toast('Проверка удалена'); selected = null; load(); }
          else toast(data.error || 'Не удалось удалить проверку');
        },
      });
    }

    const search = $('#job-search');
    if (search) search.oninput = e => { query = e.target.value.trim(); render(); };

    const clearBtn = $('#clear-all');
    /* У записи, которая видит чужие проверки, «очистить» стирает данные всех
       преподавателей, а не только свои, – предупреждение должно это говорить. */
    if (clearBtn) clearBtn.onclick = () => confirmAction({
      title: 'Очистить историю и базу отпечатков?',
      sub: seesAll
        ? 'Будут удалены проверки, отчёты и отпечатки ВСЕХ преподавателей – не только ваши. Отменить нельзя.'
        : 'Будут удалены все ваши проверки, их отчёты и все сохранённые отпечатки. Отменить нельзя.',
      okText: 'Очистить всё', danger: true,
      onOk: async () => {
        const res = await post('/jobs/clear');
        if (res.ok) { toast('История и база очищены'); load(); }
        else toast('Не удалось очистить');
      },
    });

    load();
  }

  /* ── Экран «Новая проверка» ── */

  const uploadForm = $('#upload-form');
  if (uploadForm) initUpload();

  function initUpload() {
    const input = $('#file-input');
    const zone = $('#dropzone');
    const fileList = $('#file-list');
    const startBtn = $('#start-btn');
    const thr = $('#threshold');

    const mb = bytes => (bytes / 1048576).toFixed(1);
    const limitMb = parseInt(uploadForm.dataset.maxMb || '0', 10);

    /* Свой список выбранного, а не input.files: FileList доступен только для
       чтения, а файлы нужно проверять при добавлении, докладывать в несколько
       заходов и убирать по одному. Отправляется тоже он. */
    const OK_NAME = /\.(pdf|docx|odt|doc|zip)$/i;
    let picked = [];
    let overLimit = false;
    let locked = false;

    const same = (a, b) => a.name === b.name && a.size === b.size
      && a.lastModified === b.lastModified;

    const listNames = names => names.slice(0, 3).map(n => `«${n}»`).join(', ')
      + (names.length > 3 ? ` и ещё ${names.length - 3}` : '');

    /* Формат проверяем до отправки: перетащить можно что угодно, а узнавать об
       отказе после получаса загрузки – обидно. */
    const addFiles = list => {
      if (locked) return;
      const wrong = [], empty = [], dup = [];
      for (const f of list) {
        if (!OK_NAME.test(f.name)) wrong.push(f.name);
        else if (!f.size) empty.push(f.name);
        else if (picked.some(p => same(p, f))) dup.push(f.name);
        else picked.push(f);
      }
      if (wrong.length) toast(`Принимаются PDF, DOCX, ODT, DOC и ZIP. Не взято: ${listNames(wrong)}`);
      else if (empty.length) toast(`Пустой файл, брать нечего: ${listNames(empty)}`);
      else if (dup.length) toast(`Уже в списке: ${listNames(dup)}`);
      showFiles();
    };

    const showFiles = () => {
      const total = picked.reduce((s, f) => s + f.size, 0);
      overLimit = !!limitMb && total / 1048576 > limitMb;
      fileList.innerHTML = !picked.length ? '' :
        '<div class="stat-row">' + picked.map((f, i) =>
          `<span class="stat-chip">${esc(f.name)} · <b>${mb(f.size)} МБ</b>` +
          `<button type="button" class="chip-x" data-drop="${i}" title="Убрать файл"` +
          ` aria-label="Убрать ${esc(f.name)}">×</button></span>`).join('') + '</div>' +
        `<p class="hint" style="margin:8px 0 0;">Выбрано файлов: ${picked.length} · ${mb(total)} МБ` +
        (overLimit ? ` – больше допустимых ${limitMb} МБ, уберите лишние` : '') + '</p>';

      $$('#file-list [data-drop]').forEach(b => {
        b.disabled = locked;
        b.onclick = () => { picked.splice(+b.dataset.drop, 1); showFiles(); };
      });
      startBtn.disabled = locked || !picked.length || overLimit;
    };

    input.addEventListener('change', () => {
      addFiles(input.files);
      // Сброс поля: иначе повторный выбор того же файла не считается
      // изменением, и вернуть только что убранный файл было бы нечем.
      input.value = '';
    });
    ['dragenter', 'dragover'].forEach(ev =>
      zone.addEventListener(ev, e => { e.preventDefault(); if (!locked) zone.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(ev =>
      zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.remove('over'); }));
    zone.addEventListener('drop', e => addFiles(e.dataTransfer.files));

    if (thr) thr.oninput = () => { $('#thr-val').textContent = thr.value + '%'; };

    const countGost = () => {
      const n = $$('.gost-cb:checked').length;
      $('#gost-count').textContent = `${n} из ${$$('.gost-cb').length}`;
      recalcWeights();
    };
    $$('.gost-cb').forEach(cb => cb.onchange = countGost);
    $('#gost-all').onclick = () => { $$('.gost-cb').forEach(c => c.checked = true); countGost(); };
    $('#gost-none').onclick = () => { $$('.gost-cb').forEach(c => c.checked = false); countGost(); };
    countGost();

    /* Веса скрыты, пока преподаватель их не открыл: обычный запуск – это
       три клика, а не настройка формулы. */
    const wBtn = $('#gost-weights');
    if (wBtn) wBtn.onclick = () => {
      const on = wBtn.getAttribute('aria-pressed') !== 'true';
      wBtn.setAttribute('aria-pressed', on);
      wBtn.classList.toggle('primary', on);
      $$('.crit-list').forEach(l => l.classList.toggle('weighing', on));
      $('#weights-panel').hidden = !on;
      recalcWeights();
    };

    /* Пока партия загружается и проверяется, поля формы заперты: параметры уже
       ушли на сервер вместе с файлами, и сдвинутый ползунок или снятая галочка
       на идущую проверку не влияют – а выглядит так, будто влияют. Кнопку
       «Прервать» и показ весов не трогаем: смотреть и останавливать можно. */
    const LOCKABLE = '#file-input,#threshold,#use-memory,.gost-cb,.w-in,'
      + '#grade-scale,#gost-all,#gost-none,#w-equal';

    const setLocked = on => {
      locked = on;
      $$(LOCKABLE).forEach(el => { el.disabled = on; });
      zone.classList.toggle('off', on);
      uploadForm.classList.toggle('locked', on);
      showFiles();          // крестики «убрать файл» и кнопка запуска – там же
    };

    /* Полоса идёт сквозная: первые UPLOAD_SHARE процентов – передача файлов на
       сервер, остальное – сама проверка. Иначе при загрузке пачки отчётов
       страница минутами стоит на нуле: fetch не сообщает, сколько уже ушло. */
    const UPLOAD_SHARE = 15;

    const setBar = (pct, step) => {
      const v = Math.max(0, Math.min(100, Math.round(pct)));
      $('#run-pct').textContent = v + '%';
      $('#run-bar').style.width = v + '%';
      if (step != null) $('#run-step').textContent = step;
    };

    /* Идущий запрос: по нему «Прервать» обрывает передачу файлов, пока сама
       проверка ещё не запущена и останавливать на сервере нечего. Отдельная
       отметка нужна потому, что между кусками запроса нет – оборвать в этот
       миг нечего, а решение уже принято. */
    let sending = null;
    let aborting = false;

    /* Запрос с телом: XHR, а не fetch, – нужен ход отправки. */
    const send = (url, data, onSent) => new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      sending = xhr;
      xhr.open('POST', url);
      xhr.setRequestHeader('X-CSRF-Token', CSRF);
      if (onSent) xhr.upload.onprogress = e => {
        if (e.lengthComputable) onSent(e.loaded);
      };
      xhr.onloadend = () => { sending = null; };
      xhr.onabort = () => {
        const err = new Error('Загрузка прервана');
        err.aborted = true;      // повторять нечего: оборвали сами
        reject(err);
      };
      xhr.onload = () => {
        let out = {};
        try { out = JSON.parse(xhr.responseText); } catch (_) { /* не JSON */ }
        if (xhr.status >= 400) {
          const err = new Error(out.error || `Сервер ответил ${xhr.status}`);
          err.status = xhr.status;
          reject(err);
        } else resolve(out);
      };
      xhr.onerror = () => reject(new Error('Связь с сервером прервалась'));
      xhr.ontimeout = () => reject(new Error('Сервер не ответил вовремя'));
      xhr.send(data);
    });

    /* Файлы уходят кусками: партия за курс – это гигабайты, одним запросом
       столько не проходит (лимит nginx, таймаут, память). Куски пишутся на
       сервере в один каталог, проверка запускается после последнего. */
    const sendFiles = async (files, extra) => {
      const start = await send('/upload/start', new FormData());
      const chunkSize = start.chunk_size || 16 * 1048576;
      const totalBytes = files.reduce((s, f) => s + f.size, 0);
      let doneBytes = 0;

      const show = sent => setBar(
        totalBytes ? (doneBytes + sent) / totalBytes * UPLOAD_SHARE : 0,
        `Загрузка на сервер – ${mb(doneBytes + sent)} из ${mb(totalBytes)} МБ…`);

      /* Кусок помечен смещением, поэтому повтор после обрыва связи не
         задваивает байты – сервер узнаёт уже записанное и пропускает его. */
      const stopped = () => Object.assign(new Error('Загрузка прервана'), { aborted: true });

      const sendChunk = async (f, idx, off) => {
        for (let attempt = 1; ; attempt++) {
          if (aborting) throw stopped();
          const part = new FormData();
          part.append('upload_id', start.upload_id);
          part.append('name', f.name);
          /* Номер файла в партии: по одному имени сервер не отличит второй
             «отчет.pdf» от повтора куска первого – и молча терял работу. */
          part.append('idx', String(idx));
          part.append('offset', String(off));
          part.append('chunk', f.slice(off, off + chunkSize), 'part');
          try {
            return await send('/upload/part', part, sent => show(sent));
          } catch (err) {
            // Отказ по сути – не тот файл, нет прав, превышен объём – повторять
            // бессмысленно. Повтор только для обрыва связи и сбоя сервера.
            const worth = !err.aborted && (!err.status || err.status >= 500);
            if (!worth || attempt >= 3) throw err;
            setBar(totalBytes ? doneBytes / totalBytes * UPLOAD_SHARE : 0,
              `Связь прервалась, повтор ${attempt} из 3…`);
            await new Promise(r => setTimeout(r, 1500 * attempt));
          }
        }
      };

      try {
        for (const [idx, f] of files.entries()) {
          for (let off = 0; off < f.size || off === 0; off += chunkSize) {
            await sendChunk(f, idx, off);
            doneBytes += Math.min(chunkSize, f.size - off);
            show(0);
          }
        }
        // Прервать могли и на последнем куске: файлы у сервера целиком, но
        // запускать по ним проверку уже не надо – здесь их ещё уберут.
        if (aborting) throw stopped();
      } catch (err) {
        const cancel = new FormData();
        cancel.append('upload_id', start.upload_id);
        send('/upload/cancel', cancel).catch(() => { /* уже неважно */ });
        throw err;
      }

      /* Прерывать больше нечего: куски у сервера, остаётся получить номер
         проверки. Кнопка вернётся, когда прерывать станет что – саму проверку. */
      cancelBtn.hidden = true;
      setBar(UPLOAD_SHARE, 'Файлы приняты, готовим проверку…');
      extra.append('upload_id', start.upload_id);
      const out = await send('/upload/finish', extra);
      if (!out.job_id) throw new Error('Не удалось начать проверку');
      return out.job_id;
    };

    /* ── Лист ожидания ── */

    const panel = $('#run-panel');
    const runTitle = $('#run-title');
    const cancelBtn = $('#run-cancel');
    const openLink = $('#run-open');
    const reportLink = $('#run-report');
    let watched = null;

    const showRun = j => {
      setBar(UPLOAD_SHARE + (j.progress || 0) * (100 - UPLOAD_SHARE) / 100, j.step || '');
      $('#run-files').textContent = `${j.done_files || 0} / ${j.total || 0}`;
      $('#run-pairs').textContent = j.text_pairs || 0;
      $('#run-imgs').textContent = j.img_pairs || 0;
    };

    /* redirect – проверку только что запустили с этой страницы: дождались конца
       и ушли к результату. Восстановленную после перезагрузки никуда не уводим,
       иначе набранная рядом следующая партия пропала бы. */
    const watch = (jobId, redirect) => {
      watched = jobId;
      panel.hidden = false;
      cancelBtn.hidden = false;
      openLink.hidden = false;
      reportLink.hidden = true;
      setLocked(true);        // и при запуске отсюда, и при восстановлении

      const finish = j => {
        watched = null;
        if (redirect) { window.location.href = '/'; return; }
        setLocked(false);
        cancelBtn.hidden = true;
        runTitle.textContent = j.status === 'done' ? 'Проверка завершена'
          : j.status === 'cancelled' ? 'Проверка прервана' : 'Проверка не выполнена';
        if (j.status === 'done') {
          reportLink.href = `/report/${jobId}`;
          reportLink.hidden = false;
        }
        toast(runTitle.textContent);
      };

      const poll = async () => {
        if (watched !== jobId) return;      // за это время запустили следующую
        let res;
        try { res = await fetch(`/status/${jobId}`); }
        catch (e) { setTimeout(poll, 2000); return; }
        // Проверку удалили из истории – показывать больше нечего.
        if (res.status === 404) { watched = null; panel.hidden = true; setLocked(false); return; }
        if (!res.ok) { setTimeout(poll, 2000); return; }
        const j = await res.json();
        showRun(j);
        if (j.status === 'processing') { setTimeout(poll, 1500); return; }
        finish(j);
      };
      poll();
    };

    /* Проверка идёт на сервере и переживает перезагрузку страницы. Без этого
       после обновления оставалась пустая форма, и по ней нельзя было понять,
       идёт ли проверка вообще. */
    const restore = async () => {
      let list;
      try {
        const res = await fetch('/jobs/active', { headers: { Accept: 'application/json' } });
        if (!res.ok) return;
        list = await res.json();
      } catch (e) { return; }
      if (!list.length || watched) return;
      showRun(list[0]);
      watch(list[0].job_id, false);
    };
    restore();

    cancelBtn.onclick = () => {
      // Номер берём на нажатии: пока висит вопрос, проверка могла и закончиться.
      const jobId = watched;
      if (jobId) {
        confirmAction({
          title: 'Прервать проверку?',
          sub: 'Проверка остановится на ближайшем шаге. Отчёт сформирован не будет, '
             + 'отпечатки в базу не попадут.',
          okText: 'Прервать проверку', danger: true,
          onOk: async () => {
            const res = await post(`/jobs/${jobId}/cancel`);
            const data = await res.json().catch(() => ({}));
            toast(res.ok ? 'Останавливаем проверку…'
                         : (data.error || 'Не удалось прервать проверку'));
          },
        });
        return;
      }
      confirmAction({
        title: 'Прервать загрузку файлов?',
        sub: 'Переданные куски будут удалены с сервера, проверка не начнётся.',
        okText: 'Прервать загрузку', danger: true,
        onOk: () => { aborting = true; if (sending) sending.abort(); },
      });
    };

    uploadForm.addEventListener('submit', async e => {
      e.preventDefault();
      const files = picked.slice();
      if (!files.length) { toast('Сначала выберите отчёты'); return; }
      if (overLimit) {
        const totalMb = files.reduce((s, f) => s + f.size, 0) / 1048576;
        toast(`Выбрано ${totalMb.toFixed(0)} МБ – больше допустимых ${limitMb} МБ`);
        return;
      }

      const data = new FormData();
      data.append('threshold', (thr.value / 100).toFixed(2));
      data.append('gost', $$('.gost-cb:checked').map(c => c.value).join(','));
      data.append('use_memory', $('#use-memory').checked ? '1' : '0');
      data.append('weights', $$('.w-in').map(i => `${i.dataset.code}:${i.value || 0}`).join(','));
      const scale = $('#grade-scale');
      if (scale) data.append('scale', scale.value);

      setLocked(true);
      watched = null;
      aborting = false;
      panel.hidden = false;
      /* Кнопка запуска – внизу длинной формы, а лист ожидания появляется
         вверху. Без прокрутки казалось бы, что нажатие ничего не сделало.
         Прокручивается не окно, а .scroll: у .app высота в экран. */
      (panel.closest('.scroll') || document.scrollingElement).scrollTo({
        top: 0,
        behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
      });
      runTitle.textContent = 'Загрузка файлов';
      cancelBtn.hidden = false;
      openLink.hidden = true;         // уход со страницы оборвал бы передачу
      reportLink.hidden = true;
      setBar(0, 'Загрузка файлов на сервер…');

      let jobId;
      try {
        jobId = await sendFiles(files, data);
      } catch (err) {
        panel.hidden = true;
        setLocked(false);
        toast(err.aborted ? 'Загрузка прервана' : err.message);
        return;
      }

      runTitle.textContent = 'Идёт проверка';
      watch(jobId, true);
    });
  }

  /* ── Экран «База отчётов» ── */

  const baseSearch = $('#base-search');
  if (baseSearch) {
    baseSearch.oninput = () => {
      const q = baseSearch.value.trim().toLowerCase();
      $$('#base-body tr[data-hay]').forEach(tr => {
        tr.hidden = !!q && !tr.dataset.hay.includes(q);
      });
    };
  }

  $$('[data-forget]').forEach(btn => {
    btn.onclick = () => confirmAction({
      title: 'Удалить отпечаток из базы?',
      sub: 'Работа перестанет участвовать в поиске заимствований. Готовые отчёты не изменятся.',
      body: `<p style="margin:0;font-size:13px;">${esc(btn.dataset.who)}</p>`,
      okText: 'Удалить', danger: true,
      onOk: async () => {
        const res = await post('/memory/delete', {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: btn.dataset.forget }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok) { toast('Отпечаток удалён'); btn.closest('tr').remove(); }
        else toast(data.error || 'Не удалось удалить запись');
      },
    });
  });

  /* ── Экран «Обзор»: график и полосы ── */

  const lineWrap = $('#line-chart');
  if (lineWrap) {
    const data = JSON.parse(lineWrap.dataset.series || '[]');   // [["25.07.2026", 12], …]

    function draw() {
      if (!data.length) { lineWrap.innerHTML = '<div class="empty">Пока нет проверок.</div>'; return; }
      const w = lineWrap.clientWidth || 460, h = 190;
      const pad = { l: 32, r: 10, t: 10, b: 24 };
      const values = data.map(d => d[1]);
      const max = Math.max(4, ...values);
      const x = i => data.length === 1 ? pad.l + (w - pad.l - pad.r) / 2
        : pad.l + i * (w - pad.l - pad.r) / (data.length - 1);
      const y = v => pad.t + (1 - v / max) * (h - pad.t - pad.b);
      const line = data.map((d, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(d[1]).toFixed(1)}`).join('');
      const grid = [0, max / 2, max].map(v =>
        `<line x1="${pad.l}" x2="${w - pad.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--line-soft)" stroke-width="1"/>
         <text x="0" y="${y(v) + 4}" font-size="10" fill="var(--muted)">${Math.round(v)}</text>`).join('');
      const last = data.length - 1;

      lineWrap.innerHTML =
        `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Число проверок по дням">
          ${grid}
          <text x="${x(0)}" y="${h - 5}" font-size="10" fill="var(--muted)" text-anchor="start">${data[0][0].slice(0, 5)}</text>
          <text x="${x(last)}" y="${h - 5}" font-size="10" fill="var(--muted)" text-anchor="end">${data[last][0].slice(0, 5)}</text>
          <path d="${line}L${x(last)},${y(0)}L${x(0)},${y(0)}Z" fill="var(--brand)" opacity=".09"/>
          <path d="${line}" fill="none" stroke="var(--brand)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
          <circle cx="${x(last)}" cy="${y(data[last][1])}" r="3.4" fill="var(--brand)" stroke="var(--surface)" stroke-width="2"/>
          <line id="cross" x1="0" x2="0" y1="${pad.t}" y2="${h - pad.b}" stroke="var(--muted)" stroke-width="1" opacity="0"/>
        </svg>
        <div class="tip" id="line-tip"></div>`;

      const svg = lineWrap.querySelector('svg');
      const tip = $('#line-tip', lineWrap);
      const cross = svg.querySelector('#cross');
      svg.addEventListener('pointermove', ev => {
        const r = svg.getBoundingClientRect();
        const px = (ev.clientX - r.left) / r.width * w;
        let i = data.length === 1 ? 0
          : Math.round((px - pad.l) / ((w - pad.l - pad.r) / (data.length - 1)));
        i = Math.max(0, Math.min(data.length - 1, i));
        cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i));
        cross.setAttribute('opacity', '.35');
        tip.style.opacity = 1;
        tip.style.left = (x(i) / w * 100) + '%';
        tip.style.top = (y(data[i][1]) / h * r.height) + 'px';
        tip.innerHTML = `${esc(data[i][0])} · <span class="mono">${data[i][1]}</span>`;
      });
      svg.addEventListener('pointerleave', () => {
        tip.style.opacity = 0; cross.setAttribute('opacity', '0');
      });
    }

    draw();
    window.redrawCharts = draw;
    addEventListener('resize', draw);
  }
})();
