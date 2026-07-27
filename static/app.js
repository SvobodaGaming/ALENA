/* АЛЁНА — интерфейс: список проверок, детали, загрузка, общие элементы. */
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
    t.style.cssText = 'position:fixed;left:50%;bottom:26px;transform:translateX(-50%);' +
      'background:var(--ink);color:var(--surface);padding:9px 16px;border-radius:20px;' +
      'font-size:13px;z-index:60;box-shadow:var(--shadow)';
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
      toast('Не удалось скопировать — выделите текст вручную');
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
      if (!on(r)) { out.textContent = '—'; out.title = 'Критерий снят'; return; }
      // Сумма нулей — вырожденный случай, критерии считаются равными.
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
    if (st.plag != null && thr != null && st.plag >= thr) {
      lines.push(`Совпадение с другой работой — ${st.plag}% (допустимый порог ${thr}%)`);
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

    const visible = () => {
      const list = Object.entries(records).map(([id, d]) => ({ id, ...d }));
      list.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
      if (!query) return list;
      const q = query.toLowerCase();
      return list.filter(j =>
        j.id.includes(q) ||
        String((j.summary && j.summary.group) || '').toLowerCase().includes(q) ||
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
        else if (j.status === 'error') { tone = 'crit'; right = chip('crit', 'Ошибка'); }
        else { tone = plagTone(s.plag || 0, thr); right = chip(tone, 'Заимств. ' + (s.plag || 0) + '%'); }

        return `<li role="option" aria-selected="${j.id === selected}">
          <button class="job" data-job="${esc(j.id)}" style="--tone:${toneVar(tone)}" aria-selected="${j.id === selected}">
            <span class="job-top">
              <span class="job-id mono">#${esc(j.id.slice(0, 6))}</span>
              <span class="job-group">${esc(s.group || '—')}</span>
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
      const matches = done.reduce((n, j) => n + (j.summary.matches || []).length, 0);
      const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
      set('#t-checks', list.length);
      set('#t-files', files);
      set('#t-gost', gosts.length ? Math.round(gosts.reduce((a, b) => a + b, 0) / gosts.length) + '%' : '—');
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
              : j.status === 'processing' ? chip('idle', 'Выполняется') : chip('crit', 'Ошибка')}
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
            <p class="hint" style="margin:14px 0 0;">Можно закрыть страницу — проверка продолжится на сервере,
              результат появится в списке.</p>
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
          return `<tr><td>${esc(st.fio)}</td><td class="num">—</td><td class="num">—</td>
            <td class="num">—</td>
            <td><span class="code">не обработан: ${esc(st.error)}</span></td>${fb}</tr>`;
        }
        const a = gostTone(st.gost), b = plagTone(st.plag, thr);
        const g = st.grade || {};
        const mt2 = g.pct == null ? 'idle' : gostTone(g.pct);
        return `<tr>
          <td>${esc(st.fio)}${st.group ? `<br><span class="sub mono">${esc(st.group)}</span>` : ''}</td>
          <td class="num" style="color:${toneVar(a)}">${st.gost}%</td>
          <td class="num" style="color:${toneVar(mt2)}"><b>${g.pct == null ? '—' : g.pct + '%'}</b>
            ${g.score != null ? `<br><span class="sub mono">${g.score} из ${g.scale}</span>` : ''}</td>
          <td class="num" style="color:${toneVar(b)}">${st.plag}%</td>
          <td>${st.fails.length
            ? '<span class="sub">не пройдено:</span> ' + st.fails.map(c => `<span class="code">${esc(c)}</span>`).join(' ')
            : '<span class="code pass">все критерии пройдены</span>'}</td>
          ${fb}
        </tr>`;
      }).join('');

      const matches = (s.matches || []).length ? s.matches.map(m => `
        <tr>
          <td>${esc(m.a)}</td><td>${esc(m.b)}</td><td>${esc(m.kind)}</td>
          <td class="sub">${esc(m.where)}</td>
          <td class="num">${m.pct == null ? chip('crit', 'дубликат') : chip(plagTone(m.pct, thr), m.pct + '%')}</td>
        </tr>`).join('')
        : '<tr><td colspan="5" class="empty" style="padding:22px;">Совпадений выше порога не найдено.</td></tr>';

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
        <div class="section-head"><h2>Совпадения</h2></div>
        <div class="tbl-wrap"><table>
          <thead><tr><th>Работа</th><th>Совпала с</th><th>Что совпало</th><th>Где найдено</th><th class="num">Доля</th></tr></thead>
          <tbody>${matches}</tbody>
        </table></div>
        ${fails.length ? `<details class="violations">
          <summary>Какие критерии ГОСТ чаще всего не пройдены в этой пачке</summary>
          <div class="viol-body">${fails.map(([code, n]) => `<span class="code">${esc(code)} · ${n}</span>`).join('')}</div>
        </details>` : ''}`;
      wireDelete();
      wireFold();
      wireFeedback(s, thr);
    }

    /* Готовый отзыв: то же, что видно в таблице, но словами и одним куском —
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
        ? target.map(st => feedbackText(st, thr, det)).join('\n\n————————\n\n')
        : feedbackText(target, thr, det);

      confirmAction({
        title: many ? 'Отзывы по всей пачке' : 'Отзыв для портала',
        sub: many ? `${target.length} работ, подряд одним текстом`
                  : (target.fio || '') + (target.group ? ` · ${target.group}` : ''),
        body: `
          <label class="check" style="margin-bottom:10px;">
            <input type="checkbox" id="fb-details">
            <span>С подробностями проверки — что именно нашлось</span>
          </label>
          <textarea id="fb-text" class="fb-text" rows="${many ? 16 : 11}"
            aria-label="Текст отзыва">${esc(build(false))}</textarea>
          <p class="hint" style="margin:8px 0 0;">Текст можно поправить прямо здесь — копируется то,
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

    function wireDelete() {
      const b = $('#detail-pane [data-del]');
      if (!b || b.disabled) return;
      b.onclick = () => confirmAction({
        title: 'Удалить проверку?',
        sub: 'Отчёт и запись в истории будут удалены. Отпечатки студентов останутся в базе — их удаляют отдельно.',
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
    if (clearBtn) clearBtn.onclick = () => confirmAction({
      title: 'Очистить историю и базу отпечатков?',
      sub: 'Будут удалены все ваши проверки, их отчёты и все сохранённые отпечатки. Отменить нельзя.',
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

    const showFiles = () => {
      const files = [...(input.files || [])];
      fileList.innerHTML = files.length
        ? '<div class="stat-row">' + files.map(f =>
            `<span class="stat-chip">${esc(f.name)} · <b>${(f.size / 1048576).toFixed(1)} МБ</b></span>`).join('') + '</div>'
        : '';
      startBtn.disabled = !files.length;
    };
    input.addEventListener('change', showFiles);
    ['dragenter', 'dragover'].forEach(ev =>
      zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(ev =>
      zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.remove('over'); }));
    zone.addEventListener('drop', e => { input.files = e.dataTransfer.files; showFiles(); });

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

    /* Веса скрыты, пока преподаватель их не открыл: обычный запуск — это
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

    /* Полоса идёт сквозная: первые UPLOAD_SHARE процентов — передача файлов на
       сервер, остальное — сама проверка. Иначе при загрузке пачки отчётов
       страница минутами стоит на нуле: fetch не сообщает, сколько уже ушло. */
    const UPLOAD_SHARE = 15;
    const mb = bytes => (bytes / 1048576).toFixed(1);

    const setBar = (pct, step) => {
      const v = Math.max(0, Math.min(100, Math.round(pct)));
      $('#run-pct').textContent = v + '%';
      $('#run-bar').style.width = v + '%';
      if (step != null) $('#run-step').textContent = step;
    };

    const sendFiles = data => new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/upload');
      xhr.setRequestHeader('X-CSRF-Token', CSRF);
      xhr.upload.onprogress = e => {
        if (!e.lengthComputable) return;
        setBar(e.loaded / e.total * UPLOAD_SHARE,
          e.loaded >= e.total
            ? 'Файлы приняты, готовим проверку…'
            : `Загрузка на сервер — ${mb(e.loaded)} из ${mb(e.total)} МБ…`);
      };
      xhr.onload = () => {
        let out = {};
        try { out = JSON.parse(xhr.responseText); } catch (_) { /* не JSON */ }
        if (xhr.status === 413) {
          reject(new Error('Слишком большой объём файлов — лимит 600 МБ на одну проверку'));
        } else if (xhr.status >= 400 || !out.job_id) {
          reject(new Error(out.error || 'Не удалось начать проверку'));
        } else {
          resolve(out.job_id);
        }
      };
      xhr.onerror = () => reject(new Error('Связь с сервером прервалась'));
      xhr.send(data);
    });

    uploadForm.addEventListener('submit', async e => {
      e.preventDefault();
      const data = new FormData();
      [...input.files].forEach(f => data.append('files', f));
      data.append('threshold', (thr.value / 100).toFixed(2));
      data.append('gost', $$('.gost-cb:checked').map(c => c.value).join(','));
      data.append('use_memory', $('#use-memory').checked ? '1' : '0');
      data.append('weights', $$('.w-in').map(i => `${i.dataset.code}:${i.value || 0}`).join(','));
      const scale = $('#grade-scale');
      if (scale) data.append('scale', scale.value);

      startBtn.disabled = true;
      $('#run-progress').hidden = false;
      setBar(0, 'Загрузка файлов на сервер…');

      let jobId;
      try {
        jobId = await sendFiles(data);
      } catch (err) {
        $('#run-progress').hidden = true;
        startBtn.disabled = false;
        toast(err.message);
        return;
      }

      const poll = async () => {
        const res = await fetch(`/status/${jobId}`);
        if (!res.ok) { setTimeout(poll, 2000); return; }
        const j = await res.json();
        setBar(UPLOAD_SHARE + (j.progress || 0) * (100 - UPLOAD_SHARE) / 100, j.step || '');
        $('#run-files').textContent = `${j.done_files || 0} / ${j.total || 0}`;
        $('#run-pairs').textContent = j.text_pairs || 0;
        $('#run-imgs').textContent = j.img_pairs || 0;
        if (j.status === 'processing') { setTimeout(poll, 1500); return; }
        window.location.href = '/';
      };
      poll();
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
