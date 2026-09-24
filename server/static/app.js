/* 小说创作工坊 · 本地工作台前端
   零构建：原生 JS + hash 路由，不依赖任何 npm 包。
   后端 API 契约已定死，将来换 Vue3 只需替换这一层，后端不用动。 */

const view = document.getElementById('view');
const connEl = document.getElementById('conn');

/* ── 全局错误可见化 ──────────────────────────────────────
   「点了没反应」是这类界面最难查的故障：没有报错、没有反馈、看起来一切正常。
   所以任何未被捕获的异常都要在页面上显形，而不是只躺在 Console 里。 */

let lastErrorShown = '';

function showFatal(message, detail) {
  const key = `${message}|${detail || ''}`;
  if (key === lastErrorShown) return; // 同一个错误只提示一次，避免刷屏
  lastErrorShown = key;
  const box = document.createElement('div');
  box.className = 'notice bad';
  box.style.margin = '12px 20px 0';
  box.innerHTML =
    `<strong>界面出错了：</strong>${esc(message)}` +
    (detail ? `<br><span class="muted">${esc(String(detail).slice(0, 300))}</span>` : '') +
    `<br><span class="muted">如果是刚更新过代码，请强制刷新（Ctrl+Shift+R）</span>`;
  document.body.insertBefore(box, document.getElementById('view'));
}

window.addEventListener('error', (e) => {
  showFatal(e.message || '未捕获的脚本错误', e.error && e.error.stack);
});
window.addEventListener('unhandledrejection', (e) => {
  const reason = e.reason;
  showFatal(
    (reason && reason.message) || '未处理的异步错误',
    reason && reason.payload ? JSON.stringify(reason.payload).slice(0, 300) : ''
  );
});

/* ── 工具 ──────────────────────────────────────────────── */

const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const num = (n) => (n == null ? '—' : Number(n).toLocaleString('zh-CN'));

const wan = (n) => {
  if (n == null) return '—';
  return n >= 10000 ? (n / 10000).toFixed(1) + ' 万' : num(n);
};

const bytes = (n) => {
  if (n == null) return '—';
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(0) + ' KB';
  return n + ' B';
};

const when = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('zh-CN', { hour12: false });
};

const ANOMALY_LABEL = {
  toc_block: '目录页',
  duplicate_chapter: '同号章节',
  out_of_order: '编号乱序',
  section_restart: '新分段（番外等）',
  repaired_chapter: '已修复的标题',
  missing_chapter: '疑似缺章',
  numbering_jump: '编号跳变',
  unrepaired_gap: '未修复的缺口',
  large_jump: '编号跳变',
  suspicious_title_shape: '标题像句子（疑似误切）',
  near_empty_chapter: '正文几乎为空（疑似误切）',
};

const STATUS_LABEL = { ok: '正常', repaired: '已修复' };

function notice(kind, html) {
  return `<div class="notice ${kind}">${html}</div>`;
}

function metrics(items) {
  return `<div class="metrics">${items
    .map(
      (it) =>
        `<div class="metric"><div class="k">${esc(it.k)}</div>` +
        `<div class="v${it.small ? ' small' : ''}">${it.html ?? esc(it.v)}</div></div>`
    )
    .join('')}</div>`;
}

/* ── 忙碌标记 ──────────────────────────────────────────────
   用「单次操作」作用域，而不是一个全局开关。

   教训：原来用一个全局 `imp.busy`，上传成功那条分支忘了复位，结果这个页面上
   **之后所有按钮**都被 `if (imp.busy) return;` 无声吞掉——先是「确认导入」没反应，
   刷新前的「开始切分预览」也跟着失效。同一个缺陷咬了两次。

   现在改成 withBusy()：无论成功、失败还是抛异常，退出时一定复位。 */

/* ── 页面内确认框 ──────────────────────────────────────────
   **不要用原生 confirm() / alert()。** 实测：sandbox 里没有 allow-modals 的 iframe
   （内置预览面板就是这么渲染的）会**静默把 confirm() 变成 false** ——
   不弹窗、不报错、不发请求。用户看到的就是「点了没反应」，而控制台一片安静。

   顺带它还解决另外两件事：
     · 原生对话框里放不下成本明细，而「启动前必须展示范围与成本」是既定原则
     · 它阻塞主线程、不可样式化

   所以自己画一个。返回 Promise<boolean>。 */

function askConfirm({ title, html, confirmLabel = '确认', cancelLabel = '取消', danger = false }) {
  return new Promise((resolve) => {
    const back = document.createElement('div');
    back.className = 'modal-back';
    back.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <h2>${esc(title)}</h2>
        <div class="modal-body">${html}</div>
        <div class="row" style="justify-content:flex-end;margin-top:18px">
          <button id="m-cancel">${esc(cancelLabel)}</button>
          <button class="primary${danger ? ' danger' : ''}" id="m-ok">${esc(confirmLabel)}</button>
        </div>
      </div>`;

    const done = (value) => {
      document.removeEventListener('keydown', onKey, true);
      back.remove();
      resolve(value);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        done(false);
      }
    };
    document.addEventListener('keydown', onKey, true);
    back.addEventListener('click', (e) => {
      if (e.target === back) done(false);
    });
    document.body.appendChild(back);
    back.querySelector('#m-cancel').onclick = () => done(false);
    back.querySelector('#m-ok').onclick = () => done(true);
    back.querySelector('#m-ok').focus();
  });
}

/* 只报个结果的提示框（替代 alert，同样因为会被沙箱静默吃掉） */
function tellUser(title, html) {
  return askConfirm({ title, html, confirmLabel: '知道了', cancelLabel: '关闭' });
}

async function withBusy(button, busyLabel, fn) {
  const original = button ? button.innerHTML : '';
  if (button) {
    button.disabled = true;
    button.innerHTML = button.dataset.labelHtml || busyLabel;
  }
  try {
    return await fn();
  } finally {
    // finally 保证复位。这是唯一可靠的位置。
    if (button) {
      button.disabled = false;
      button.innerHTML = original;
    }
  }
}

/* ── API ───────────────────────────────────────────────── */

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { error: text };
  }
  if (!res.ok) {
    const detail = data && data.detail;
    const msg =
      typeof detail === 'string' ? detail : (data && data.error) || `请求失败（${res.status}）`;
    const err = new Error(msg);
    err.payload = data;
    err.status = res.status;
    throw err;
  }
  return data;
}

const getJSON = (p) => api(p);
const postJSON = (p, body) =>
  api(p, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

/* ── 书架 ──────────────────────────────────────────────── */

async function renderShelf() {
  setTab('shelf');
  view.innerHTML = `<p class="muted">加载中…</p>`;
  let works = [];
  try {
    works = await getJSON('/api/works');
  } catch (e) {
    view.innerHTML = notice('bad', `读取书架失败：${esc(e.message)}`);
    return;
  }

  const imported = works.filter((w) => w.imported);

  view.innerHTML = `
    <div class="spread" style="margin-bottom:16px">
      <div>
        <h1>书架</h1>
        <p class="lead">已导入 ${imported.length} 部作品。数据直接来自各自工作区，与命令行脚本读的是同一份。</p>
      </div>
      <a class="btn" href="#/import" style="text-decoration:none;color:inherit">导入作品</a>
    </div>
    ${
      imported.length
        ? `<div class="work-grid">${imported.map(workCard).join('')}</div>`
        : `<div class="empty">
             <strong style="display:block;font-size:15px;margin-bottom:6px">还没有导入作品</strong>
             点右上角「导入作品」，选一个 txt 文件，先看切分预览再确认。
           </div>`
    }
  `;

  view.querySelectorAll('[data-archive]').forEach((btn) => {
    btn.onclick = () => archiveWork(btn.dataset.archive);
  });
}

function workCard(w) {
  const integrity = w.integrity_passed
    ? `<span class="chip ok">完整性通过</span>`
    : `<span class="chip bad">完整性未通过</span>`;
  const anomalies = w.anomalies
    ? `<span class="chip warn">待核对 ${w.anomalies}</span>`
    : `<span class="chip ok">无异常</span>`;
  const progress =
    w.chapters && w.annotated
      ? `<span class="chip info">已标注 ${w.annotated}</span>`
      : '';

  return `
    <div class="work-card">
      <h3>${esc(w.name)}</h3>
      <div class="meta">最近导入 ${when(w.updated_at)}</div>
      <div class="nums">
        <span><b>${num(w.chapters)}</b> 章</span>
        <span><b>${wan(w.chars)}</b> 字</span>
        <span><b>${num(w.segments)}</b> 段</span>
      </div>
      <div class="row wrap" style="margin-bottom:14px">${integrity}${anomalies}${progress}</div>
      <div class="actions">
        <a class="btn" href="#/work/${encodeURIComponent(w.dir_name || w.name)}"
           style="text-decoration:none;color:inherit">查看概览</a>
        <button class="btn ghost" data-archive="${esc(w.dir_name || w.name)}">移出书架</button>
      </div>
    </div>`;
}

async function archiveWork(name) {
  const ok = await askConfirm({
    title: `把「${name}」移出书架？`,
    html:
      '<p style="margin:0 0 8px">它不是被删除。</p>' +
      '<p class="muted" style="margin:0">整个工作区（章节文件、清洗前副本、标注、伏笔台账）' +
      '会原样搬到 <code>workspaces/_archive/</code> 下。想恢复就把它搬回 ' +
      '<code>workspaces/</code> 下一层。</p>',
    confirmLabel: '移出书架',
    danger: true,
  });
  if (!ok) return;
  try {
    await postJSON(`/api/works/${encodeURIComponent(name)}/archive`, {});
    renderShelf();
  } catch (e) {
    await tellUser('移出失败', `<p style="margin:0">${esc(e.message)}</p>`);
  }
}

/* ── 导入向导 ──────────────────────────────────────────── */

/* finished 与 step 分开：step=3 只是「当前渲染哪一步」，
   finished 表示「这一轮已经走完了」。没有它，导完一部再点「导入作品」标签会毫无反应——
   hash 没变就不触发 hashchange，路由不重跑，用户看到的是上一部的完成页。 */
const imp = {
  step: 1,
  file: null,
  work: '',
  preview: null,
  token: null,
  result: null,
  finished: false,
};

function resetImport() {
  imp.step = 1;
  imp.file = null;
  imp.work = '';
  imp.preview = null;
  imp.token = null;
  imp.result = null;
  imp.finished = false;
}

async function renderImport() {
  setTab('import');
  // 上一轮已经完成 → 再来这里应当是全新的一次，而不是把旧的完成页再放一遍。
  if (imp.finished) resetImport();
  if (imp.step === 1) return step1();
  if (imp.step === 2) return step2();
  return step3();
}

function stepsBar() {
  const labels = ['选择文件', '切分预览', '完成'];
  return `<div class="steps">${labels
    .map((label, i) => {
      const n = i + 1;
      const cls = imp.step === n ? 'active' : imp.step > n ? 'done' : '';
      const mark = imp.step > n ? '✓' : n;
      return `${i ? '<span class="bar"></span>' : ''}
        <span class="step ${cls}"><span class="dot">${mark}</span>${esc(label)}</span>`;
    })
    .join('')}</div>`;
}

function step1() {
  view.innerHTML = `
    <h1>导入作品</h1>
    <p class="lead">原作文件不会被修改。导入会在作品自己的工作区里生成章节文件与清洗前副本。</p>
    ${stepsBar()}
    <div class="dropzone" id="drop">
      <strong>把作品文件拖到这里，或点击选择</strong>
      支持 txt，单文件建议不超过 200 MB
    </div>
    <input type="file" id="file" accept=".txt,.md" style="display:none">
    <div class="card" style="margin-top:14px">
      <h3>作品名</h3>
      <div class="row">
        <input type="text" id="workname" placeholder="留空则用文件名" style="flex:1;max-width:360px">
        <span class="muted" id="picked">尚未选择文件</span>
      </div>
    </div>
    <div class="row">
      <button class="primary" id="go" disabled>开始切分预览</button>
      <span class="muted">不会直接导入，下一步会先让你确认切分结果</span>
    </div>
    <div id="err"></div>
  `;

  const drop = document.getElementById('drop');
  const fileInput = document.getElementById('file');
  const go = document.getElementById('go');
  const picked = document.getElementById('picked');

  const pick = (f) => {
    if (!f) return;
    imp.file = f;
    picked.textContent = `${f.name}（${bytes(f.size)}）`;
    if (!document.getElementById('workname').value) {
      document.getElementById('workname').value = f.name.replace(/\.[^.]+$/, '').trim();
    }
    go.disabled = false;
  };

  drop.onclick = () => fileInput.click();
  fileInput.onchange = () => pick(fileInput.files[0]);
  drop.ondragover = (e) => {
    e.preventDefault();
    drop.classList.add('hover');
  };
  drop.ondragleave = () => drop.classList.remove('hover');
  drop.ondrop = (e) => {
    e.preventDefault();
    drop.classList.remove('hover');
    pick(e.dataTransfer.files[0]);
  };
  go.onclick = uploadPreview;
}

async function uploadPreview() {
  if (!imp.file) return;
  const workname = document.getElementById('workname').value.trim();
  const go = document.getElementById('go');
  const errBox = document.getElementById('err');
  errBox.innerHTML = notice(
    'info',
    '正在切分并核验，大文件需要几秒。这一步只读，不会写入任何东西。'
  );

  const form = new FormData();
  form.append('file', imp.file);
  form.append('work', workname);

  await withBusy(go, '<span class="spinner"></span>切分中…', async () => {
    try {
      const data = await api('/api/import/upload', { method: 'POST', body: form });
      imp.preview = data;
      imp.token = data.token;
      imp.work = data.work;
      imp.step = 2;
      renderImport();
    } catch (e) {
      errBox.innerHTML = notice('bad', `切分失败：${esc(e.message)}`);
    }
  });
}

function step2() {
  const p = imp.preview || {};
  const c = p.counts || {};
  const it = p.integrity || {};
  const src = p.source || {};

  const blocking = !it.passed;
  const gate = blocking
    ? notice('bad', '<strong>完整性核验未通过，不能导入。</strong>重建校验没对上，说明切分偏移有误，导入会把问题带到下游。')
    : notice('ok', '完整性核验通过：把章前区段与所有章节切片拼回去，与原文件逐字一致。');

  view.innerHTML = `
    <h1>切分预览</h1>
    <p class="lead">${esc(p.work)} · ${esc(src.file || '')} · ${esc(src.encoding || '')} 编码</p>
    ${stepsBar()}

    ${metrics([
      { k: '识别章节', v: c.chapters },
      { k: '分段', v: c.segments },
      { k: '文件字数', v: wan(src.chars) },
      { k: '异常待核对', v: c.anomalies },
      { k: '未命中标题', v: c.unmatched_title_like },
      { k: '疑似广告', v: c.suspected_ads },
    ])}

    <div class="card" style="margin-top:14px">${gate}</div>

    ${p.volumes && p.volumes.length
      ? `<div class="card"><h3>分段结构</h3><ul class="list">${p.volumes
          .map(
            (v) =>
              `<li>第 ${v.vol_no} 段 · 第 ${v.start_chapter ?? '—'} 至 ${v.end_chapter ?? '—'} 章 · ` +
              `共 ${v.chapter_count} 章${v.unnumbered_count ? `（其中无编号 ${v.unnumbered_count}）` : ''}</li>`
          )
          .join('')}</ul></div>`
      : ''}

    <div class="card">
      <h3>章节样例${p.chapters_truncated ? `（另有 ${p.chapters_truncated} 章未列出）` : ''}</h3>
      <div class="table-wrap">
        <table>
          <thead><tr><th>编号</th><th>章号</th><th>标题</th><th class="num">字数</th><th>状态</th></tr></thead>
          <tbody>${(p.chapters_sample || []).map(chapterRow).join('')}</tbody>
        </table>
      </div>
    </div>

    ${patternBlock(p.pattern_stats)}

    ${listBlock('异常与待核对', (p.anomalies || []).map(anomalyLine), '没有异常，切分很干净。')}

    ${listBlock(
      '格式变体探针：像章节标题但没命中任何模板',
      (p.unmatched_title_like || []).map((u) => `行 ${u.line_no} 「${esc(u.text)}」`),
      '没有未命中的标题。'
    )}

    <div class="card">
      <h3>清洗选项</h3>
      <div style="display:grid;gap:10px">
        <label class="check">
          <input type="checkbox" id="opt-ads">
          <span>删掉盗版站广告与引流（${c.suspected_ads} 条）
            <small>URL、群号、「每日更新 / 搜书神器」这类文本。作者自己写的「求月票」不会被删。</small>
          </span>
        </label>
        <label class="check">
          <input type="checkbox" id="opt-rep">
          <span>删掉反复出现的短行（${c.repeated_lines} 条）
            <small>页眉页脚类。默认只报告不删——误删正文的代价远大于留一行页眉。</small>
          </span>
        </label>
      </div>
    </div>

    <div class="row">
      <button class="primary" id="commit" ${blocking ? 'disabled' : ''}>确认导入</button>
      <button id="back">重新选择文件</button>
      <button id="discard">放弃这次上传</button>
    </div>
    <div id="err" style="margin-top:12px"></div>
  `;

  document.getElementById('back').onclick = () => {
    imp.step = 1;
    renderImport();
  };
  document.getElementById('discard').onclick = discardUpload;
  document.getElementById('commit').onclick = commitImport;
}

function chapterRow(ch) {
  const no = ch.unnumbered ? '<span class="chip">无编号</span>' : ch.chapter_no;
  const title = ch.title ? esc(ch.title) : '<span class="muted">（原标题无标题）</span>';
  const status =
    ch.status === 'repaired' ? '<span class="chip info">已修复</span>' : '<span class="muted">正常</span>';
  return `<tr><td>${esc(ch.id)}</td><td>${no}</td><td>${title}</td>
    <td class="num">${num(ch.chars)}</td><td>${status}</td></tr>`;
}

function anomalyLine(a) {
  const label = ANOMALY_LABEL[a.kind] || a.kind;
  const line = a.line_no ? `（行 ${a.line_no}）` : '';
  return `<span class="chip warn">${esc(label)}</span> ${line} ${esc(a.detail)}`;
}

function patternBlock(stats) {
  if (!stats || !stats.length) return '';
  return `<div class="card">
    <h3>模板命中分布</h3>
    <p class="muted" style="margin:0 0 10px">占比过低的模板几乎一定是误报，重点看这些。</p>
    <ul class="list">${stats
      .map(
        (s) =>
          `<li><code>${esc(s.pattern).slice(0, 70)}</code><br>` +
          `<span class="muted">命中 ${s.count} 次 · 占比 ${(s.share * 100).toFixed(1)}%</span>` +
          `${s.suspected_noise ? ' <span class="chip warn">疑似误报</span>' : ''}` +
          (s.samples && s.samples.length
            ? `<br><span class="muted">样例：${s.samples.map((x) => esc(x)).join(' / ')}</span>`
            : '') +
          `</li>`
      )
      .join('')}</ul>
  </div>`;
}

function listBlock(title, lines, emptyText) {
  return `<div class="card"><h3>${esc(title)}</h3>${
    lines.length
      ? `<ul class="list">${lines.map((l) => `<li>${l}</li>`).join('')}</ul>`
      : `<p class="muted" style="margin:0">${esc(emptyText)}</p>`
  }</div>`;
}

async function commitImport() {
  const btn = document.getElementById('commit');
  const errBox = document.getElementById('err');
  errBox.innerHTML = notice(
    'info',
    '正在写入章节文件并做最终核验。这一步会覆盖该作品已有的导入结果（旧文件会被移入 _review/stale 留痕）。'
  );

  await withBusy(btn, '<span class="spinner"></span>导入中…', async () => {
    try {
      const data = await postJSON('/api/import/commit', {
        token: imp.token,
        work: imp.work,
        strip_ads: document.getElementById('opt-ads')?.checked || false,
        strip_repeated: document.getElementById('opt-rep')?.checked || false,
      });
      imp.result = data;
      imp.step = 3;
      renderImport();
    } catch (e) {
      const detail = e.payload && e.payload.detail;
      let extra = '';
      if (detail && typeof detail === 'object' && detail.integrity) {
        extra = `<pre class="code">${esc(JSON.stringify(detail.integrity, null, 2))}</pre>`;
      }
      errBox.innerHTML = notice('bad', `导入失败：${esc(e.message)}${extra}`);
    }
  });
}

async function discardUpload() {
  if (imp.token) {
    try {
      await api(`/api/import/${imp.token}`, { method: 'DELETE' });
    } catch {
      /* 丢弃失败无所谓，临时目录可以之后手动清 */
    }
  }
  resetImport();
  renderImport();
}

function step3() {
  imp.finished = true; // 标记这一轮结束，下次进入向导会重新开始
  const r = imp.result || {};
  view.innerHTML = `
    <h1>导入完成</h1>
    <p class="lead">${esc(r.work || '')} 已经落到它自己的工作区。</p>
    ${stepsBar()}
    ${notice('ok', `完整性核验通过，共写入 ${num(r.chapters)} 个章节文件。`)}
    ${metrics([
      { k: '章节数', v: r.chapters },
      { k: '完整性核验', v: r.integrity_passed ? '通过' : '未通过' },
    ])}
    <div class="card" style="margin-top:14px">
      <h3>产物位置</h3>
      <dl class="kv">
        <dt>章节文件</dt><dd>${esc(r.files?.chapters_dir || '')}</dd>
        <dt>清洗前副本</dt><dd>${esc(r.files?.raw_dir || '')}</dd>
        <dt>清单</dt><dd>${esc(r.files?.manifest || '')}</dd>
        <dt>清洗日志</dt><dd>${esc(r.files?.cleaning_log || '')}</dd>
      </dl>
    </div>
    <div class="row">
      <a class="btn" href="#/work/${encodeURIComponent(r.work || '')}"
         style="text-decoration:none;color:inherit">查看作品概览</a>
      <button id="again">再导入一部</button>
    </div>
  `;
  document.getElementById('again').onclick = () => {
    resetImport();
    renderImport();
  };
}

/* ── 作品概览 ──────────────────────────────────────────── */

const detailState = { name: '', offset: 0, limit: 50, only: 'all' };
/* 勾选状态要跨页保留：翻到第 3 页勾几章，再翻回来不能丢。 */
const selected = new Set();

async function renderWork(name) {
  setTab('');
  detailState.name = name;
  view.innerHTML = `<p class="muted">加载中…</p>`;

  let d;
  try {
    d = await getJSON(`/api/works/${encodeURIComponent(name)}`);
  } catch (e) {
    view.innerHTML = notice('bad', `读取失败：${esc(e.message)}`) +
      `<button onclick="location.hash='#/shelf'">返回书架</button>`;
    return;
  }

  const s = d.statistics || {};
  const src = d.source || {};
  const it = d.integrity || {};
  const anomalies = await getJSON(`/api/works/${encodeURIComponent(name)}/anomalies`).catch(() => []);

  view.innerHTML = `
    <p class="muted" style="margin-bottom:6px"><a href="#/shelf">← 书架</a></p>
    <h1>${esc(d.name)}</h1>
    <p class="lead">${esc(src.file || '')} · ${esc(src.encoding || '')} 编码 · ${bytes(src.bytes)}</p>

    ${metrics([
      { k: '章节', v: s.chapters },
      { k: '总字数', v: wan(src.chars) },
      { k: '分段', v: s.segments },
      { k: '无编号章节', v: s.unnumbered },
      { k: '平均章长', v: num(s.chars_avg) },
      { k: '已标注', v: d.annotated },
    ])}

    <div class="card" style="margin-top:14px">
      <h3>完整性核验</h3>
      ${it.passed
        ? notice('ok', '通过。把章前区段与所有章节切片拼回去，与原文件逐字一致。')
        : notice('bad', '未通过。切分偏移可能有误，不要用这批数据往下走。')}
      <dl class="kv" style="margin-top:12px">
        <dt>重建校验</dt><dd>${it.reconstruction_ok ? '逐字一致' : '不一致'}（${num(it.rebuilt_chars)} / ${num(it.source_chars)} 字）</dd>
        <dt>字数对账</dt><dd>Σ章节 ${num(it.sum_chapter_chars_raw)} + 章前 ${num(it.pre_chapter_chars)} = ${num(it.source_chars)}，偏差 ${num(it.delta)}</dd>
        <dt>切片校验</dt><dd>${(it.slice_errors || []).length ? `${it.slice_errors.length} 处有误` : '通过'}</dd>
      </dl>
    </div>

    ${d.volumes && d.volumes.length ? `<div class="card"><h3>分段结构</h3><ul class="list">${d.volumes
        .map(
          (v) =>
            `<li>第 ${v.vol_no} 段 · 第 ${v.start_chapter ?? '—'} 至 ${v.end_chapter ?? '—'} 章 · 共 ${v.chapter_count} 章` +
            `${v.unnumbered_count ? `（其中无编号 ${v.unnumbered_count}）` : ''}</li>`
        )
        .join('')}</ul></div>` : ''}

    ${anomalySummary(anomalies)}

    <div class="card" id="task-card">
      <div class="spread" style="margin-bottom:10px">
        <h2 style="margin:0">逐章标注</h2>
        <div class="row">
          <a class="btn" href="#/work/${encodeURIComponent(d.name)}/annotations"
             style="text-decoration:none;color:inherit">查看标注数据</a>
          <a class="btn" href="#/work/${encodeURIComponent(d.name)}/entities"
             style="text-decoration:none;color:inherit">世界观与人物</a>
          <a class="btn" href="#/work/${encodeURIComponent(d.name)}/report"
             style="text-decoration:none;color:inherit">查看体检报告</a>
        </div>
      </div>
      <div id="task-body"><p class="muted">加载中…</p></div>
    </div>

    <div class="card">
      <div class="spread" style="margin-bottom:12px">
        <h2 style="margin:0">章节列表</h2>
        <div class="row">
          <select id="only" style="font:inherit;font-size:13px;padding:5px 8px;border-radius:10px;border:1px solid var(--border-strong);background:var(--surface);color:var(--text)">
            <option value="all">全部</option>
            <option value="unannotated">未标注</option>
            <option value="annotated">已标注</option>
            <option value="review">待复核</option>
            <option value="unnumbered">无编号</option>
            <option value="repaired">标题已修复</option>
          </select>
          <select id="page-size" style="font:inherit;font-size:13px;padding:5px 8px;border-radius:10px;border:1px solid var(--border-strong);background:var(--surface);color:var(--text)">
            <option value="50">每页 50</option>
            <option value="100">每页 100</option>
            <option value="200">每页 200</option>
          </select>
        </div>
      </div>
      <div id="chapter-table"><p class="muted">加载中…</p></div>
    </div>
  `;

  const sel = document.getElementById('only');
  sel.value = detailState.only;
  sel.onchange = () => {
    detailState.only = sel.value;
    detailState.offset = 0;
    loadChapters();
  };
  const sizeSel = document.getElementById('page-size');
  sizeSel.value = String(detailState.limit);
  sizeSel.onchange = () => {
    detailState.limit = Number(sizeSel.value);
    detailState.offset = 0;
    loadChapters();
  };
  loadChapters();
  loadTaskArea(detailState.name);
}

/* ── 模型任务区 ──────────────────────────────────────────
   这一块是「模型调用必须显式」的落点：
     · 进页面只出计划，不调模型
     · 每个按钮都写清范围与成本
     · 没有「一键跑完」——嫌烦应该放宽预算，而不是去掉确认 */

const taskState = { name: '', plan: null, status: null, timer: null, busy: false };

async function loadTaskArea(name) {
  taskState.name = name;
  const box = document.getElementById('task-body');
  if (!box) return;
  box.innerHTML = '<p class="muted">加载中…</p>';

  let plan = null;
  let status = null;
  try {
    plan = await getJSON(`/api/works/${encodeURIComponent(name)}/annotate/plan`);
  } catch (e) {
    box.innerHTML = notice('bad', `读不到标注计划：${esc(e.message)}`);
    return;
  }
  try {
    status = await getJSON(`/api/works/${encodeURIComponent(name)}/annotate/status`);
  } catch {
    status = null;
  }
  taskState.plan = plan;
  taskState.status = status;
  renderTaskArea();

  if (status && status.running) startTaskPolling();
}

function renderTaskArea() {
  const box = document.getElementById('task-body');
  if (!box) return;
  const plan = taskState.plan;
  if (!plan) return;
  const est = plan.estimate || {};
  const st = taskState.status;

  const costLine =
    est.cost_cny == null
      ? `<div class="kv"><dt>费用</dt><dd>${esc(est.price_note || '未填写单价，无法计算')}</dd></div>`
      : `<div class="kv"><dt>费用</dt><dd>≈ ¥${est.cost_cny}（估算）</dd></div>`;

  const warnings = (plan.warnings || [])
    .map((w) => `<li>${esc(w)}</li>`)
    .join('');

  const running = st && st.running;
  const progressHtml = running
    ? `<div class="progress"><div class="bar" style="width:${
        st.total ? Math.round((st.committed / st.total) * 100) : 0
      }%"></div></div>
       <div class="row wrap" style="margin-top:10px">
         <span class="chip info">${esc(st.committed)} / ${esc(st.total)} 章</span>
         <span class="muted">正常 ${num(st.ok)} · 待复核 ${num(st.needs_review)} · 失败 ${num(st.failed)}</span>
         <span class="muted">实测 token ${num(st.total_tokens)}</span>
         ${st.cost_cny != null ? `<span class="muted">≈ ¥${st.cost_cny}</span>` : ''}
         ${st.eta_sec ? `<span class="muted">预计还需 ${Math.round(st.eta_sec)} 秒</span>` : ''}
         <button class="ghost" id="stop-run">中止</button>
       </div>
       ${
         st.current_chapter
           ? `<p class="muted" style="margin-top:6px">正在处理 ${esc(st.current_chapter)}</p>`
           : ''
       }`
    : st && st.status && st.status !== 'never'
      ? `<p class="muted">上次任务：${statusLabel(st.status)} · ${esc(st.message || '')}</p>`
      : '';

  box.innerHTML = `
    <p class="muted" style="margin:0 0 12px">
      模型调用只在点下面的按钮之后发生。进这个页面本身不花钱。
    </p>
    <div class="row wrap" style="margin-bottom:10px">
      <span class="chip">${esc(plan.provider_name || plan.provider)}</span>
      ${modelSelector(plan, 'task-model')}
      <span class="chip">待跑 ${num(plan.pending)} 章</span>
      <span class="chip">已标注 ${num(plan.skipped)} 章</span>
      <span class="chip">并发 ${plan.concurrency}</span>
      ${plan.has_api_key ? '' : '<span class="chip bad">读不到密钥</span>'}
      ${plan.ledger_enabled ? '<span class="chip ok">伏笔台账启用</span>' : ''}
    </div>

    <details>
      <summary>成本估算与计算过程</summary>
      <dl class="kv" style="margin-top:10px">
        <dt>token 合计</dt><dd>输入 ${num(est.input_tokens)} + 输出 ${num(est.output_tokens)} = ${num(est.total_tokens)}</dd>
        <dt>中文换算系数</dt><dd>${esc(est.tokens_per_cjk_char)} tokens/字（${
          est.coefficient_basis === 'measured' ? '实测' : est.coefficient_basis === 'fallback' ? '兜底值' : '服务商实测'
        }）</dd>
        <dt>固定前缀</dt><dd>${num(est.prefix_tokens)} token/章</dd>
        <dt>计费时段</dt><dd>${
          est.price_bucket === 'peak' ? '高峰' : est.price_bucket === 'offpeak' ? '空闲（半价）' : '不分时段'
        }</dd>
        ${costLine}
        ${est.est_seconds ? `<dt>预计耗时</dt><dd>约 ${Math.round(est.est_seconds / 60)} 分钟</dd>` : ''}
      </dl>
      <p class="muted">输出 token 按 500/章 假设；上一章摘要与伏笔清单按 350/章 假设。
        这两项是声明过的假设，不是实测值。</p>
    </details>

    ${warnings ? `<div class="notice warn" style="margin-top:12px"><ul class="list">${warnings}</ul></div>` : ''}

    ${
      plan.over_budget
        ? `<div class="notice warn" style="margin-top:12px">
             <strong>估算已超预算上限。</strong>直接跑会被闸门拦下。
             可以调高配置里的上限、先试跑一小批，或者勾选下面这项明确接受。
             <label class="check" style="margin-top:8px">
               <input type="checkbox" id="over-budget">
               <span>我已知晓超出预算上限，仍要继续（运行中按实测 token 还会再拦一次）</span>
             </label>
           </div>`
        : ''
    }

    <div class="row wrap" style="margin-top:12px">
      <button class="primary" id="trial" ${running ? 'disabled' : ''}>试跑 20 章</button>
      <button id="run-all" ${running ? 'disabled' : ''}>标注未完成的 ${num(plan.pending)} 章</button>
      <button id="rerun-all" ${running ? 'disabled' : ''}>全部重跑 ${num(plan.total_chapters)} 章</button>
      <button id="pick-chapters" ${running ? 'disabled' : ''}>到章节列表里勾选…</button>
    </div>
    <p class="muted" style="margin:8px 0 0">
      默认只跑没标注过的（改过就重跑那一章）。
      「全部重跑」用于改了提示词或作品配置之后——新旧口径的数据不能混着看趋势。
      只想跑某几章，用最后一个按钮去列表里勾选。
    </p>

    <div id="task-progress" style="margin-top:12px">${progressHtml}</div>

    <div id="task-log" style="margin-top:14px">${runLogHtml(st)}</div>

    <div id="task-err"></div>

    <hr style="border:0;border-top:1px solid var(--border);margin:16px 0">
    <div class="spread" style="align-items:flex-start;gap:12px">
      <div>
        <h3 style="margin:0 0 4px">全书大纲</h3>
        <p class="muted" style="margin:0">
          把每章的剧情梗概逐层归约成一份能读的大纲。
          梗概是标注顺带产出的，所以要先跑标注。
        </p>
      </div>
      <div class="row" id="outline-actions">
        <a class="btn ghost" id="outline-link" href="#/work/${encodeURIComponent(taskState.name)}/outline"
           style="text-decoration:none;color:inherit;display:none">查看大纲</a>
        <button id="outline-run">生成大纲</button>
      </div>
    </div>
    <div id="outline-info" style="margin-top:10px"></div>

    <hr style="border:0;border-top:1px solid var(--border);margin:16px 0">
    <div class="spread" style="align-items:flex-start;gap:12px">
      <div>
        <h3 style="margin:0 0 4px">世界观与人物</h3>
        <p class="muted" style="margin:0">
          从正文里抽人物、势力、能力、地点与人物关系。
          原料是正文，这本书有多少字就花多少钱。
        </p>
      </div>
      <div class="row" id="entity-actions">
        <a class="btn ghost" id="entity-link" href="#/work/${encodeURIComponent(taskState.name)}/entities"
           style="text-decoration:none;color:inherit;display:none">查看实体</a>
        <button id="entity-run">生成实体统计</button>
      </div>
    </div>
    <div id="entity-info" style="margin-top:10px"></div>
  `;

  const trial = document.getElementById('trial');
  const runAll = document.getElementById('run-all');
  const stopBtn = document.getElementById('stop-run');

  if (trial) trial.onclick = () => startAnnotation(20, trial);
  if (runAll) runAll.onclick = () => startAnnotation(null, runAll);
  const rerunAll = document.getElementById('rerun-all');
  if (rerunAll) rerunAll.onclick = () => startAnnotation(null, rerunAll, true);
  const pickBtn = document.getElementById('pick-chapters');
  if (pickBtn) {
    pickBtn.onclick = () => {
      const table = document.getElementById('chapter-table');
      if (table) table.scrollIntoView({ behavior: 'smooth', block: 'center' });
    };
  }

  const outlineBtn = document.getElementById('outline-run');
  if (outlineBtn) outlineBtn.onclick = () => startOutline(outlineBtn);
  const entityBtn = document.getElementById('entity-run');
  if (entityBtn) entityBtn.onclick = () => startEntitiesGen(entityBtn);
  loadOutlineInfo();
  loadEntitiesInfo();

  /* 进度轮询每 1.5 秒重绘一次这块区域，勾选状态必须自己记住，
     否则「我已知晓」会在用户不知情的情况下被清掉——那等于确认形同虚设。 */
  const overBox = document.getElementById('over-budget');
  const modelSel = document.getElementById('task-model');
  if (modelSel) {
    modelSel.onchange = () => reloadPlan({ model: modelSel.value });
  }
  if (overBox) {
    overBox.checked = !!taskState.overBudgetChecked;
    overBox.onchange = () => {
      taskState.overBudgetChecked = overBox.checked;
    };
  }
  if (stopBtn) stopBtn.onclick = async () => {
    await postJSON(`/api/works/${encodeURIComponent(taskState.name)}/annotate/stop`, {});
  };
}

/* ── 模型选择 ──────────────────────────────────────────────
   档位价差能到 4.5 倍，模型必须由用户显式选，不能替他默认。
   换模型就重新拉一次计划——成本会跟着变， estimates 不重算就是骗人。 */

function modelSelector(plan, id) {
  const models = plan.available_models || [];
  if (models.length < 2) return '';
  return `<select id="${id}" class="model-select" style="font:inherit;font-size:13px;padding:5px 8px;border-radius:10px;border:1px solid var(--border-strong);background:var(--surface);color:var(--text)">
    ${models
      .map(
        (m) =>
          `<option value="${esc(m.id)}" ${m.id === plan.model ? 'selected' : ''}>${esc(
            m.alias || m.id
          )}${m.role && m.role !== 'main' ? '（' + esc(m.role) + '）' : ''}</option>`
      )
      .join('')}
  </select>`;
}

async function loadEntitiesInfo() {
  const box = document.getElementById('entity-info');
  if (!box) return;
  box.innerHTML = '<p class="muted">读取中…</p>';
  let plan;
  try {
    plan = await getJSON(`/api/works/${encodeURIComponent(taskState.name)}/entities/plan`);
  } catch (e) {
    box.innerHTML = notice('bad', `读不到实体统计计划：${esc(e.message)}`);
    return;
  }
  taskState.entitiesPlan = plan;

  const link = document.getElementById('entity-link');
  if (link && plan.has_result) link.style.display = '';

  const rows = [
    ['分块', `${num(plan.blocks)} 块（每块 ${num(plan.block_size)} 章）`],
    ['正文', `${wan(plan.body_chars)} 字`],
    ['预估 token', `${num(plan.est_input_tokens)} 进 / ${num(plan.est_output_tokens)} 出`],
  ];
  box.innerHTML = `
    <dl class="kv" style="margin-top:0">${rows
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`)
      .join('')}</dl>
    <p style="margin:8px 0 0;font-size:14px">${
      plan.est_cost_cny == null ? esc(plan.price_note) : `预估费用：<strong>≈ ¥${plan.est_cost_cny}</strong>`
    }</p>
  `;
}

async function startEntitiesGen(button) {
  const plan = taskState.entitiesPlan;
  if (!plan) return;

  const ok = await askConfirm({
    title: '生成世界观与人物',
    html:
      `<dl class="kv">` +
      `<dt>服务商</dt><dd>${esc(plan.provider_name || plan.provider)} / ${esc(plan.model)}</dd>` +
      `<dt>范围</dt><dd>${num(plan.total_chapters)} 章，分 ${num(plan.blocks)} 块</dd>` +
      `</dl>` +
      `<p style="margin:14px 0 0;font-size:14px">` +
      (plan.est_cost_cny == null
        ? '费用：<strong>未填写单价，算不出来</strong>'
        : `预估费用：<strong>≈ ¥${plan.est_cost_cny}</strong>`) +
      `</p>` +
      notice('warn', '会发起模型调用、产生费用。'),
    confirmLabel: '开始生成',
  });
  if (!ok) return;

  await withBusy(button, '<span class="spinner"></span>生成中…', async () => {
    try {
      await postJSON(
        `/api/works/${encodeURIComponent(taskState.name)}/entities/start?block_size=${plan.block_size}` +
          `&model=${encodeURIComponent(plan.model)}`,
        {}
      );
      const box = document.getElementById('entity-info');
      if (box) box.innerHTML = notice('info', '正在抽取实体，完成后这里的提示会变。');
      pollEntities();
    } catch (e) {
      await tellUser('启动失败', `<p style="margin:0">${esc(e.message)}</p>`);
    }
  });
}

let entityTimer = null;

function pollEntities() {
  if (entityTimer) return;
  entityTimer = setInterval(async () => {
    let st;
    try {
      st = await getJSON(`/api/works/${encodeURIComponent(taskState.name)}/entities/status`);
    } catch {
      return;
    }
    const box = document.getElementById('entity-info');
    if (box) {
      box.innerHTML = st.running
        ? notice('info', '<span class="spinner"></span>正在抽取实体…')
        : st.has_result
          ? notice('ok', '实体统计已生成，点上面的「查看实体」')
          : notice('warn', '任务结束了但没有产出，检查运行记录');
    }
    if (!st.running) {
      clearInterval(entityTimer);
      entityTimer = null;
    }
  }, 2000);
}

/* ── 大纲 ──────────────────────────────────────────────────
   原料是标注顺带产出的逐章梗概（P22）。没跑过标注就明说缺原料，
   而不是凭空生成一份看着像那么回事的大纲。 */

async function loadOutlineInfo() {
  const box = document.getElementById('outline-info');
  if (!box) return;
  box.innerHTML = '<p class="muted" style="margin:0">读取中…</p>';
  let plan;
  try {
    plan = await getJSON(`/api/works/${encodeURIComponent(taskState.name)}/outline/plan`);
  } catch (e) {
    box.innerHTML = notice('bad', `读不到大纲计划：${esc(e.message)}`);
    return;
  }
  taskState.outlinePlan = plan;

  const link = document.getElementById('outline-link');
  if (link && plan.latest) link.style.display = '';

  const rows = [
    ['有梗概', `${num(plan.summarized)} / ${num(plan.total_chapters)} 章`],
    ['分块', `${num(plan.blocks)} 块（每块 ${num(plan.block_size)} 章）+ 1 次全书归约`],
    ['预估 token', `${num(plan.est_input_tokens)} 进 / ${num(plan.est_output_tokens)} 出`],
  ];
  box.innerHTML = `
    <dl class="kv" style="margin-top:0">
      <dt>模型</dt><dd>${modelSelector(plan, 'outline-model')}</dd>
      ${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('')}
    </dl>
    <p style="margin:8px 0 0;font-size:14px">${
      plan.est_cost_cny == null
        ? esc(plan.price_note)
        : `预估费用：<strong>≈ ¥${plan.est_cost_cny}</strong>`
    }</p>
    ${(plan.warnings || []).map((w) => notice('warn', esc(w))).join('')}
  `;
  const outlineModel = document.getElementById('outline-model');
  if (outlineModel) {
    outlineModel.onchange = () => loadOutlineInfo(outlineModel.value);
  }
}

async function startOutline(button) {
  const plan = taskState.outlinePlan;
  if (!plan) return;
  if (!plan.ready) {
    await tellUser(
      '还生成不了大纲',
      `<p style="margin:0">${esc((plan.warnings || ['需要先跑标注'])[0])}</p>`
    );
    return;
  }

  const ok = await askConfirm({
    title: '生成全书大纲',
    html:
      `<dl class="kv">` +
      `<dt>服务商</dt><dd>${esc(plan.provider_name || plan.provider)} / ${esc(plan.model)}</dd>` +
      `<dt>归约块数</dt><dd>${num(plan.blocks)} + 1</dd>` +
      `<dt>覆盖</dt><dd>${num(plan.summarized)} 章梗概${plan.missing ? `（缺 ${num(plan.missing)} 章）` : ''}</dd>` +
      `</dl>` +
      `<p style="margin:14px 0 0;font-size:14px">` +
      (plan.est_cost_cny == null
        ? '费用：<strong>未填写单价，算不出来</strong>'
        : `预估费用：<strong>≈ ¥${plan.est_cost_cny}</strong>`) +
      `</p>` +
      notice('warn', '会发起模型调用、产生费用。'),
    confirmLabel: '开始生成',
  });
  if (!ok) return;

  await withBusy(button, '<span class="spinner"></span>生成中…', async () => {
    try {
      await postJSON(
        `/api/works/${encodeURIComponent(taskState.name)}/outline/start?block_size=${plan.block_size}` +
          `&model=${encodeURIComponent(plan.model)}`,
        {}
      );
      pollOutline();
    } catch (e) {
      await tellUser('启动失败', `<p style="margin:0">${esc(e.message)}</p>`);
    }
  });
}

let outlineTimer = null;

function pollOutline() {
  if (outlineTimer) return;
  const box = document.getElementById('outline-info');
  outlineTimer = setInterval(async () => {
    let st;
    try {
      st = await getJSON(`/api/works/${encodeURIComponent(taskState.name)}/outline/status`);
    } catch {
      return;
    }
    if (box) {
      box.innerHTML = st.running
        ? notice('info', '<span class="spinner"></span>正在逐段归约…窗口里的进度看下方运行记录')
        : st.has_result
          ? notice('ok', '大纲已生成，点上面的「查看大纲」')
          : notice('warn', '任务结束了，但没有产出大纲，检查运行记录');
    }
    if (!st.running) {
      clearInterval(outlineTimer);
      outlineTimer = null;
      try {
        taskState.outlinePlan = await getJSON(
          `/api/works/${encodeURIComponent(taskState.name)}/outline/plan`
        );
      } catch { /* 保留旧计划 */ }
      renderTaskArea();
    }
  }, 2000);
}

/* ── 运行记录 ──────────────────────────────────────────────
   只显示「失败 20」而不说为什么，等于让人猜。
   这一段把错误按类别摊开、给出下一步该怎么办、并列出失败章节的原始报错。 */

function runLogHtml(st) {
  if (!st || !st.status || st.status === 'never') return '';

  const hints = st.error_hints || [];
  const failures = st.failures || [];
  const parts = [];

  if (st.aborted_reason) {
    parts.push(
      notice(
        'bad',
        `<strong>任务已中止（不是跑完）：</strong>${esc(st.aborted_reason)}` +
          (st.status === 'aborted' ? '<br><span class="muted">修好之后重跑，会接着跑没跑的章节。</span>' : '')
      )
    );
  }

  if (hints.length) {
    parts.push(`
      <details ${st.status !== 'done' || st.failed ? 'open' : ''}>
        <summary>失败原因（按类别，共 ${num(
          hints.reduce((a, b) => a + (b.count || 0), 0)
        )} 次）</summary>
        <div class="table-wrap" style="margin-top:8px">
          <table>
            <thead><tr><th>类别</th><th class="num">次数</th><th>下一步怎么办</th></tr></thead>
            <tbody>${hints
              .map(
                (h) => `<tr>
                  <td>${h.fatal ? '<span class="chip bad">' : '<span class="chip warn">'}${esc(
                    h.kind
                  )}</span>${h.fatal ? '<div class="muted">重试也不会好</div>' : ''}</td>
                  <td class="num">${num(h.count)}</td>
                  <td>${esc(h.hint || '')}</td>
                </tr>`
              )
              .join('')}</tbody>
          </table>
        </div>
      </details>
    `);
  }

  if (failures.length) {
    parts.push(`
      <details style="margin-top:10px">
        <summary>失败章节（${num(failures.length)} 章，未落盘，重跑会重试）</summary>
        <ul class="list" style="margin-top:8px">
          ${failures
            .slice(0, 50)
            .map((f) => {
              const last = (f.errors || [])[f.errors.length - 1] || {};
              return `<li>
                <strong>第${esc(f.chapter_no)}章 ${esc(f.title || '')}</strong>
                <span class="muted">（${esc(f.chapter_id)}）</span>
                <div class="muted" style="margin-top:2px">
                  ${esc(last.kind || '')}${last.http_status ? ` · HTTP ${last.http_status}` : ''}
                  ${last.hint ? ` · ${esc(last.hint)}` : ''}
                </div>
                ${
                  last.detail
                    ? `<pre class="code" style="margin-top:6px;white-space:pre-wrap">${esc(
                        String(last.detail).slice(0, 400)
                      )}</pre>`
                    : ''
                }
              </li>`;
            })
            .join('')}
          ${
            failures.length > 50
              ? `<li class="muted">…其余 ${num(failures.length - 50)} 章见运行记录文件</li>`
              : ''
          }
        </ul>
      </details>
    `);
  }

  if (st.state_file) {
    parts.push(
      `<p class="muted" style="margin:10px 0 0">完整运行记录：<code>${esc(st.state_file)}</code></p>`
    );
  }

  return parts.join('');
}

function statusLabel(s) {
  return { running: '进行中', done: '已完成', aborted: '已中止', failed: '失败', never: '未运行' }[s] || s;
}

async function reloadPlan(extra = {}) {
  const qs = new URLSearchParams();
  if (extra.model) qs.set('model', extra.model);
  if (extra.force) qs.set('force', 'true');
  try {
    taskState.plan = await getJSON(
      `/api/works/${encodeURIComponent(taskState.name)}/annotate/plan${qs.toString() ? '?' + qs : ''}`
    );
    taskState.model = extra.model || taskState.plan.model;
  } catch {
    /* 拉不动就保留旧计划，别把页面搞挂 */
  }
  renderTaskArea();
}

async function startAnnotation(limit, button, force = false) {
  const p = taskState.plan || {};
  const est = p.estimate || {};
  const chapters = limit || p.pending || 0;
  const ratio = est.chapters ? chapters / est.chapters : 0;
  const tokens = est.total_tokens ? Math.round(est.total_tokens * ratio) : null;
  const cost = est.cost_cny == null ? null : (est.cost_cny * ratio).toFixed(2);
  const bucket =
    est.price_bucket === 'peak' ? '高峰' : est.price_bucket === 'offpeak' ? '空闲（半价）' : '不分时段';

  const rows = [
    ['服务商', `${esc(p.provider_name || p.provider)} / ${esc(p.model)}`],
    ['范围', `${num(chapters)} 章${limit ? `（待跑 ${num(p.pending)} 章中的前 ${num(limit)} 章）` : ''}`],
    ['预估 token', tokens == null ? '—' : num(tokens)],
    ['计费时段', esc(bucket)],
    ['并发', `${p.concurrency}${p.ledger_enabled ? '（伏笔台账要求有序）' : ''}`],
  ];

  const ok = await askConfirm({
    title: limit
      ? `试跑前 ${chapters} 章`
      : force
        ? `全部重跑 ${chapters} 章`
        : `标注未完成的 ${chapters} 章`,
    html:
      (force
        ? notice(
            'warn',
            '这会连已标注的章节一起重跑。只在改过提示词或作品配置时这么做——' +
              '否则旧数据没必要花第二遍钱。'
          )
        : '') +
      `<dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('')}</dl>` +
      `<p style="margin:14px 0 0;font-size:14px">` +
      (cost == null
        ? '费用：<strong>未填写单价，算不出来</strong>'
        : `预估费用：<strong>≈ ¥${cost}</strong>`) +
      `</p>` +
      (p.pricing_verified
        ? ''
        : `<p class="muted" style="margin:6px 0 0">单价尚未经你在控制台核对，实际扣费以服务商为准。</p>`) +
      notice('warn', '这一步会真的发起模型调用、产生费用，并且不能撤销。'),
    confirmLabel: `开始 ${num(chapters)} 章`,
  });
  if (!ok) return;

  const errBox = document.getElementById('task-err');
  await withBusy(button, '<span class="spinner"></span>启动中…', async () => {
    try {
      await postJSON(`/api/works/${encodeURIComponent(taskState.name)}/annotate/start`, {
        limit: limit || null,
        model: taskState.model || null,
        force,
        allow_over_budget: !!taskState.overBudgetChecked,
      });
      taskState.status = {
        running: true,
        total: chapters,
        committed: 0,
        ok: 0,
        needs_review: 0,
        failed: 0,
        total_tokens: 0,
      };
      renderTaskArea();
      startTaskPolling();
    } catch (e) {
      errBox.innerHTML = notice('bad', `启动失败：${esc(e.message)}`);
      await tellUser('启动失败', `<p style="margin:0">${esc(e.message)}</p>`);
    }
  });
}

function startTaskPolling() {
  if (taskState.timer) return;
  taskState.timer = setInterval(async () => {
    try {
      taskState.status = await getJSON(
        `/api/works/${encodeURIComponent(taskState.name)}/annotate/status`
      );
    } catch {
      return;
    }
    renderTaskArea();
    if (!taskState.status || !taskState.status.running) {
      stopTaskPolling();
      // 任务结束后计划里的「待跑」变了，重新拉一次
      try {
        taskState.plan = await getJSON(
          `/api/works/${encodeURIComponent(taskState.name)}/annotate/plan`
        );
      } catch { /* 计划拉不动就保留旧的 */ }
      renderTaskArea();
    }
  }, 1500);
}

function stopTaskPolling() {
  if (taskState.timer) {
    clearInterval(taskState.timer);
    taskState.timer = null;
  }
}

function anomalySummary(anomalies) {
  if (!anomalies.length) {
    return `<div class="card"><h3>异常与待核对</h3><p class="muted" style="margin:0">没有异常，切分很干净。</p></div>`;
  }
  const groups = {};
  anomalies.forEach((a) => {
    (groups[a.kind] = groups[a.kind] || []).push(a);
  });
  const rows = Object.entries(groups)
    .map(
      ([kind, items]) => `
      <details class="card" style="margin-bottom:10px">
        <summary>${esc(ANOMALY_LABEL[kind] || kind)} · ${items.length} 条</summary>
        <ul class="list">${items
          .slice(0, 40)
          .map(
            (a) =>
              `<li>${a.line_no ? `<span class="muted">行 ${a.line_no} · </span>` : ''}${esc(a.detail)}</li>`
          )
          .join('')}${items.length > 40 ? `<li class="muted">…其余 ${items.length - 40} 条略</li>` : ''}</ul>
      </details>`
    )
    .join('');
  return `<div><h2>异常与待核对（${anomalies.length}）</h2>${rows}</div>`;
}

async function loadChapters() {
  const box = document.getElementById('chapter-table');
  box.innerHTML = `<p class="muted">加载中…</p>`;
  let data;
  try {
    data = await getJSON(
      `/api/works/${encodeURIComponent(detailState.name)}/chapters` +
        `?offset=${detailState.offset}&limit=${detailState.limit}&only=${detailState.only}`
    );
  } catch (e) {
    box.innerHTML = notice('bad', `读取章节失败：${esc(e.message)}`);
    return;
  }

  const from = data.total ? detailState.offset + 1 : 0;
  const to = Math.min(detailState.offset + detailState.limit, data.total);
  const pages = Math.max(1, Math.ceil(data.total / detailState.limit));
  const page = Math.floor(detailState.offset / detailState.limit) + 1;
  const pageIds = (data.items || []).map((c) => c.id);
  const allChecked = pageIds.length > 0 && pageIds.every((id) => selected.has(id));

  box.innerHTML = `
    <div class="selbar">
      <label class="check" style="margin:0">
        <input type="checkbox" id="sel-page" ${allChecked ? 'checked' : ''}>
        <span>全选本页</span>
      </label>
      <span class="muted">已选 <b id="sel-count">${num(selected.size)}</b> 章</span>
      <div class="row" style="margin-left:auto">
        <button id="sel-clear" class="ghost">清空选择</button>
        <button id="sel-run" class="primary" ${selected.size ? '' : 'disabled'}>标注选中的 ${num(selected.size)} 章</button>
      </div>
    </div>

    <div class="table-wrap">
      <table>
        <thead><tr>
          <th style="width:34px"></th><th>编号</th><th>章号</th><th>标题</th>
          <th class="num">字数</th><th>标注</th><th>切分</th><th class="num">原行</th>
        </tr></thead>
        <tbody>${(data.items || [])
          .map(
            (ch) => `<tr>
              <td><input type="checkbox" class="sel-one" data-id="${esc(ch.id)}"
                    ${selected.has(ch.id) ? 'checked' : ''}></td>
              <td>${esc(ch.id)}</td>
              <td>${ch.unnumbered ? '<span class="chip">无编号</span>' : ch.chapter_no}</td>
              <td>${
                ch.title
                  ? chapterLink(detailState.name, ch.id, esc(ch.title))
                  : '<span class="muted">（无标题）</span>'
              }</td>
              <td class="num">${num(ch.chars)}</td>
              <td>${annotChip(ch)}</td>
              <td>${ch.status === 'repaired' ? '<span class="chip info">已修复</span>' : '<span class="muted">正常</span>'}</td>
              <td class="num muted">${ch.line_no ?? ''}</td>
            </tr>`
          )
          .join('')}</tbody>
      </table>
    </div>

    <div class="spread" style="margin-top:12px">
      <span class="muted">第 ${num(from)} 至 ${num(to)} 条，共 ${num(data.total)} 条${
        data.annotated_total ? `　·　已标注 ${num(data.annotated_total)} 章` : ''
      }</span>
      <div class="row">
        <button id="prev" ${detailState.offset <= 0 ? 'disabled' : ''}>上一页</button>
        <span class="muted">第</span>
        <input type="text" id="page-no" value="${page}" style="width:52px;text-align:center;font:inherit;font-size:13px;padding:5px 6px;border-radius:10px;border:1px solid var(--border-strong);background:var(--surface);color:var(--text)">
        <span class="muted">/ ${num(pages)} 页</span>
        <button id="go-page">跳转</button>
        <button id="next" ${to >= data.total ? 'disabled' : ''}>下一页</button>
      </div>
    </div>
  `;

  const bind = (id, fn) => {
    const el = document.getElementById(id);
    if (el) el.onclick = fn;
  };
  bind('prev', () => {
    detailState.offset = Math.max(0, detailState.offset - detailState.limit);
    loadChapters();
  });
  bind('next', () => {
    detailState.offset += detailState.limit;
    loadChapters();
  });
  bind('go-page', () => jumpToPage(pages));
  const pageInput = document.getElementById('page-no');
  if (pageInput) {
    pageInput.onkeydown = (e) => {
      if (e.key === 'Enter') jumpToPage(pages);
    };
  }

  const pageBox = document.getElementById('sel-page');
  if (pageBox) {
    pageBox.onchange = () => {
      pageIds.forEach((id) => (pageBox.checked ? selected.add(id) : selected.delete(id)));
      loadChapters();
    };
  }
  box.querySelectorAll('.sel-one').forEach((cb) => {
    cb.onchange = () => {
      const id = cb.dataset.id;
      if (cb.checked) selected.add(id);
      else selected.delete(id);
      // 只更新计数与按钮，不整表重绘——重绘会丢掉刚点下去的焦点
      const count = document.getElementById('sel-count');
      if (count) count.textContent = num(selected.size);
      const run = document.getElementById('sel-run');
      if (run) {
        run.disabled = selected.size === 0;
        run.textContent = `标注选中的 ${num(selected.size)} 章`;
      }
    };
  });
  bind('sel-clear', () => {
    selected.clear();
    loadChapters();
  });
  bind('sel-run', () => annotateSelected());
}

function annotChip(ch) {
  const cls = {
    已标注: 'ok',
    待复核: 'warn',
    失败: 'bad',
    未标注: '',
  }[ch.annot_state] || '';
  return `<span class="chip ${cls}">${esc(ch.annot_state || '未标注')}</span>`;
}

function jumpToPage(pages) {
  const input = document.getElementById('page-no');
  if (!input) return;
  const value = parseInt(input.value, 10);
  // 输什么都别把人卡死：越界就夹到边界内，输乱七八糟就回第一页。
  const target = Number.isFinite(value) ? Math.min(Math.max(value, 1), pages) : 1;
  detailState.offset = (target - 1) * detailState.limit;
  loadChapters();
}

async function annotateSelected() {
  const ids = [...selected];
  if (!ids.length) return;

  let plan;
  try {
    plan = await postJSON(
      `/api/works/${encodeURIComponent(detailState.name)}/annotate/plan/selection`,
      { chapter_ids: ids }
    );
  } catch (e) {
    await tellUser('算不出范围', `<p style="margin:0">${esc(e.message)}</p>`);
    return;
  }

  if (!plan.pending) {
    await tellUser(
      '这几章都不用跑',
      '<p style="margin:0">选中的章节都已经有标注且原文未变。' +
        '要重跑请勾上下面那个「连已标注的也重跑」。</p>'
    );
    return;
  }

  const ok = await askConfirm({
    title: `标注选中的 ${plan.pending} 章`,
    html:
      `<dl class="kv">` +
      `<dt>服务商</dt><dd>${esc(plan.provider_name || plan.provider)} / ${esc(plan.model)}</dd>` +
      `<dt>勾选</dt><dd>${num(ids.length)} 章，其中待跑 ${num(plan.pending)} 章</dd>` +
      `</dl>` +
      `<p style="margin:14px 0 0;font-size:14px">` +
      (plan.estimate.cost_cny == null
        ? '费用：<strong>未填写单价，算不出来</strong>'
        : `预估费用：<strong>≈ ¥${plan.estimate.cost_cny}</strong>`) +
      `</p>` +
      `<label class="check" style="margin-top:12px">` +
      `<input type="checkbox" id="force-rerun">` +
      `<span>连已标注的也重跑<small>改过提示词或作品配置时用，否则新旧口径的数据不能混着看</small></span>` +
      `</label>` +
      notice('warn', '会发起模型调用、产生费用。'),
    confirmLabel: `开始 ${num(plan.pending)} 章`,
  });
  if (!ok) return;

  const force = document.getElementById('force-rerun')?.checked || false;
  try {
    await postJSON(
      `/api/works/${encodeURIComponent(detailState.name)}/annotate/start`,
      { chapter_ids: ids, force, allow_over_budget: false }
    );
    selected.clear();
    renderWork(detailState.name); // 回到任务区看进度
  } catch (e) {
    await tellUser('启动失败', `<p style="margin:0">${esc(e.message)}</p>`);
  }
}

/* ── 体检报告 ────────────────────────────────────────────
   图表用内联 SVG 画，不引任何图表库——零构建是这条链路的硬约束，
   为一个页面引入 npm 依赖不值得。 */

function lineSvg(series, opts = {}) {
  const W = 980;
  const H = opts.height || 210;
  const padL = 46;
  const padR = 14;
  const padT = 14;
  const padB = 24;
  const n = Math.max(...series.map((s) => s.values.length), 1);

  const all = series.flatMap((s) => s.values.filter((v) => v != null));
  if (!all.length) return '<p class="muted">无数据</p>';
  let lo = opts.yMin != null ? opts.yMin : Math.min(...all);
  let hi = opts.yMax != null ? opts.yMax : Math.max(...all);
  if (hi === lo) hi = lo + 1;
  const pad = (hi - lo) * 0.08;
  if (opts.yMin == null) lo = Math.max(0, lo - pad);
  if (opts.yMax == null) hi = hi + pad;

  const x = (i) => padL + (i / Math.max(1, n - 1)) * (W - padL - padR);
  const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);

  const grid = [0, 0.5, 1]
    .map((t) => {
      const v = lo + (hi - lo) * (1 - t);
      const yy = padT + t * (H - padT - padB);
      return `<line x1="${padL}" y1="${yy}" x2="${W - padR}" y2="${yy}" stroke="rgba(0,0,0,0.08)"/>
              <text x="${padL - 8}" y="${yy + 4}" text-anchor="end" font-size="11" fill="#8b8a85">${Math.round(v)}</text>`;
    })
    .join('');

  const paths = series
    .map((s) => {
      const segs = [];
      let cur = [];
      s.values.forEach((v, i) => {
        if (v == null) {
          if (cur.length) segs.push(cur);
          cur = [];
          return;
        }
        cur.push(`${x(i)},${y(v)}`);
      });
      if (cur.length) segs.push(cur);
      return segs
        .filter((seg) => seg.length > 1)
        .map((seg) => `<polyline points="${seg.join(' ')}" fill="none" stroke="${s.color}" stroke-width="1.6"/>`)
        .join('');
    })
    .join('');

  const marks = (opts.marks || [])
    .map(
      (m) =>
        `<circle cx="${x(m.i)}" cy="${y(m.v)}" r="3.2" fill="${m.color}">
           <title>${esc(m.title)}</title></circle>`
    )
    .join('');

  const legend = series
    .map(
      (s) =>
        `<span class="legend"><i style="background:${s.color}"></i>${esc(s.name)}</span>`
    )
    .join('');

  return `
    <div class="legend-row">${legend}</div>
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img">
      ${grid}${paths}${marks}
      <text x="${padL}" y="${H - 6}" font-size="11" fill="#8b8a85">第 1 章</text>
      <text x="${W - padR}" y="${H - 6}" text-anchor="end" font-size="11" fill="#8b8a85">第 ${n} 章</text>
    </svg>`;
}

function barsHtml(counts, total) {
  const entries = Object.entries(counts || {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return '<p class="muted">无数据</p>';
  const max = Math.max(...entries.map((e) => e[1]));
  return `<div class="bars">${entries
    .map(
      ([k, v]) => `
      <div class="bar-row">
        <span class="bar-k">${esc(k)}</span>
        <span class="bar-track"><span class="bar-fill" style="width:${max ? (v / max) * 100 : 0}%"></span></span>
        <span class="bar-v">${num(v)}（${total ? ((v / total) * 100).toFixed(1) : '0.0'}%）</span>
      </div>`
    )
    .join('')}</div>`;
}

function tableOrEmpty(headers, rows, emptyText) {
  if (!rows.length) return `<p class="muted">${esc(emptyText)}</p>`;
  return `<div class="table-wrap"><table>
    <thead><tr>${headers.map((h) => `<th>${esc(h)}</th>`).join('')}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody>
  </table></div>`;
}

async function renderReport(name) {
  setTab('');
  view.innerHTML = '<p class="muted">正在聚合报告…</p>';
  let r;
  try {
    r = await getJSON(`/api/works/${encodeURIComponent(name)}/report`);
  } catch (e) {
    view.innerHTML = notice('bad', `生成报告失败：${esc(e.message)}`);
    return;
  }

  const cov = r.coverage || {};
  const s = r.sections || {};
  const len = s.length || {};
  const ec = s.emotion_conflict || {};
  const motive = s.motive || {};
  const fs = s.foreshadow || {};
  const style = s.style_violations || {};
  const hooks = s.hook_types || {};
  const payoffs = s.payoffs || {};
  const vols = s.volumes || {};
  const review = s.review || {};

  const lengthChart = len.available
    ? lineSvg(
        [
          {
            name: `章字数（中位数 ${num(len.median)}）`,
            color: '#185fa5',
            values: len.points.map((p) => p.chars),
          },
        ],
        {
          height: 210,
          marks: (len.outliers?.long || [])
            .concat(len.outliers?.tiny || [])
            .map((p) => ({ i: len.points.indexOf(p), v: p.chars, color: '#a32d2d', title: `第${p.chapter_no}章 ${p.chars} 字` })),
        }
      )
    : '<p class="muted">无数据</p>';

  const ecChart = ec.available
    ? lineSvg(
        [
          { name: '情绪值', color: '#a32d2d', values: ec.series.emotion.map((p) => p.value) },
          { name: '冲突等级', color: '#854f0b', values: ec.series.conflict.map((p) => p.value) },
        ],
        { height: 200, yMin: 0.5, yMax: 5.5 }
      )
    : `<p class="muted">${esc(ec.reason || '无数据')}</p>`;

  const motiveChart = motive.available
    ? lineSvg(
        [{ name: '动机呈现强度', color: '#0f6e56', values: motive.points.map((p) => p.value) }],
        {
          height: 190,
          yMin: 0.5,
          yMax: 5.5,
          marks: motive.weak_chapters
            .map((p) => ({
              i: motive.points.indexOf(p),
              v: p.value,
              color: '#a32d2d',
              title: `第${p.chapter_no}章 强度 ${p.value}`,
            }))
            .filter((m) => m.i >= 0),
        }
      )
    : '<p class="muted">无数据</p>';

  view.innerHTML = `
    <p class="muted" style="margin-bottom:6px">
      <a href="#/work/${encodeURIComponent(name)}">← ${esc(name)}</a>
    </p>
    <h1>结构体检报告 · ${esc(r.work)}</h1>
    <p class="lead">生成于 ${when(r.generated_at)}　·　标注覆盖 ${num(cov.annotated)}/${num(cov.chapters)} 章（${((cov.ratio || 0) * 100).toFixed(1)}%）</p>

    ${cov.note ? notice('warn', esc(cov.note)) : ''}

    <div class="card">
      <h3>① 章节长度</h3>
      ${len.available
        ? `<div class="row wrap" style="margin-bottom:10px">
             <span class="chip">平均 ${num(len.avg)} 字</span>
             <span class="chip">中位数 ${num(len.median)} 字</span>
             <span class="chip">最短 ${num(len.min)}</span>
             <span class="chip">最长 ${num(len.max)}</span>
             <span class="chip warn">过短 ${num(len.outliers.short_total)}</span>
             <span class="chip warn">过长 ${num(len.outliers.long_total)}</span>
             <span class="chip bad">极短 ${num(len.outliers.tiny_total)}</span>
           </div>
           ${lengthChart}
           <p class="muted" style="margin-top:8px">${esc(len.outliers.criteria)}</p>
           ${tableOrEmpty(
             ['章节', '标题', '字数'],
             (len.outliers.tiny || []).slice(0, 12).map((p) => [
               chapterLink(name, p.chapter_id, `第${p.chapter_no}章`),
               esc(p.title || '（无标题）'),
               num(p.chars),
             ]),
             '没有极短章。'
           )}`
        : '<p class="muted">无数据</p>'}
    </div>

    <div class="card">
      <h3>② 情绪与冲突</h3>
      ${ecChart}
      ${
        (ec.flat_runs || []).length
          ? `<div style="margin-top:12px">
               <h3 style="margin:0 0 8px">节奏走平的区段（连续 ${ec.flat_run_min} 章以上量表值不变）</h3>
               <ul class="list">${ec.flat_runs
                 .map(
                   (run) =>
                     `<li><strong>${esc(run.field)}</strong>　第${run.chapters
                       .map((c) => c.chapter_no)
                       .join('、')}章　值 = ${esc(run.chapters[0].value)}
                       <span class="muted">（${run.chapters.length} 章连着没变化，多半是没有新事件进来）</span></li>`
                 )
                 .join('')}</ul>
             </div>`
          : '<p class="muted" style="margin-top:10px">没有连续走平的区段。</p>'
      }
      <p class="muted" style="margin-top:8px">1-5 量表实测存在 ±1 噪声：看趋势可以，单章精确值不可尽信。</p>
    </div>

    <div class="card">
      <h3>③ 主角动机线</h3>
      ${motive.available
        ? `<div class="row wrap" style="margin-bottom:10px">
             <span class="chip">均值 ${motive.avg}</span>
             <span class="chip warn">弱章（≤2）${num(motive.weak_total)}</span>
           </div>
           ${motiveChart}
           <p class="muted" style="margin-top:8px">${esc(motive.note)}</p>`
        : '<p class="muted">无数据</p>'}
    </div>

    <div class="card">
      <h3>④ 伏笔追踪</h3>
      ${fs.available
        ? `<div class="row wrap" style="margin-bottom:10px">
             <span class="chip">共 ${num(fs.total)} 条</span>
             <span class="chip warn">未回收 ${num(fs.open)}</span>
             <span class="chip ok">已回收 ${num(fs.recovered)}</span>
             <span class="chip bad">疑似断点 ${num(fs.stale)}</span>
           </div>
           <p class="muted" style="margin:0 0 10px">${esc(fs.note)}</p>
           ${tableOrEmpty(
             ['编号', '描述', '埋设章', '最后推进', '状态'],
             fs.items
               .slice()
               .sort((a, b) => (b.is_stale ? 1 : 0) - (a.is_stale ? 1 : 0))
               .slice(0, 60)
               .map((it) => [
                 esc(it.id),
                 esc(it.desc || ''),
                 `第${it.planted_chapter_no}章`,
                 `第${it.last_touched_chapter_no}章`,
                 it.status === 'open'
                   ? it.is_stale
                     ? '<span class="chip bad">疑似断点</span>'
                     : '<span class="chip warn">未回收</span>'
                   : '<span class="chip ok">已回收</span>',
               ]),
             '台账里还没有条目。'
           )}`
        : `<p class="muted">${esc(fs.reason || '无数据')}</p>`}
    </div>

    <div class="card">
      <h3>⑤ 钩子与爽点</h3>
      ${hooks.available
        ? `<p class="muted" style="margin:0 0 8px">钩子类型分布（共 ${num(hooks.total)} 章）</p>${barsHtml(hooks.counts, hooks.total)}`
        : `<p class="muted">${esc(hooks.reason || '无数据')}</p>`}
      <div style="height:14px"></div>
      ${payoffs.available
        ? `<p class="muted" style="margin:0 0 8px">爽点共 ${num(payoffs.total)} 处，平均强度 ${payoffs.strength_avg}</p>${barsHtml(payoffs.by_type, payoffs.total)}`
        : `<p class="muted">${esc(payoffs.reason || '无数据')}</p>`}
    </div>

    <div class="card">
      <h3>⑥ 分卷概览</h3>
      ${tableOrEmpty(
        ['卷', '章数', '已标注', '平均字数', '情绪均值', '冲突均值', '动机均值'],
        (vols.items || []).map((v) => [
          `第${v.vol_no}卷`,
          num(v.chapters),
          num(v.annotated),
          num(v.chars_avg),
          v.emotion_avg ?? '—',
          v.conflict_avg ?? '—',
          v.motive_avg ?? '—',
        ]),
        '无数据'
      )}
      ${vols.note ? `<p class="muted" style="margin-top:8px">${esc(vols.note)}</p>` : ''}
    </div>

    <div class="card">
      <h3>⑦ 风格规范违规</h3>
      ${style.available
        ? `${tableOrEmpty(
            ['规则', '说明', '处数', '涉及章数'],
            (style.by_rule || []).map((x) => [esc(x.rule_id), esc(x.desc || ''), num(x.count), num(x.chapters)]),
            '没有违规。'
          )}
          <div style="height:14px"></div>
          ${tableOrEmpty(
            ['章节', '标题', '处数', '规则'],
            (style.top_chapters || []).slice(0, 15).map((x) => [
              chapterLink(name, x.chapter_id, `第${x.chapter_no}章`),
              esc(x.title || ''),
              num(x.count),
              esc((x.rules || []).join('、')),
            ]),
            '没有违规章节。'
          )}
          <p class="muted" style="margin-top:8px">${esc(style.note)}</p>`
        : `<p class="muted">${esc(style.note || '无数据')}</p>`}
    </div>

    <div class="card">
      <h3>⑧ 待复核</h3>
      ${review.available
        ? `<div class="row wrap" style="margin-bottom:10px">
             ${Object.entries(review.by_action || {})
               .map(([k, v]) => `<span class="chip">${esc(k)} ${num(v)}</span>`)
               .join('')}
             <span class="chip warn">需人工 ${num(review.needs_review_total)}</span>
           </div>
           ${tableOrEmpty(
             ['章节', '标题', '复核动作', '提示'],
             (review.needs_review || []).slice(0, 30).map((x) => [
               chapterLink(name, x.chapter_id, `第${x.chapter_no}章`),
               esc(x.title || ''),
               esc(x.review_action),
               esc((x.issues || []).join('；')),
             ]),
             '没有需要复核的章节。'
           )}
           <p class="muted" style="margin-top:8px">${esc(review.note)}</p>`
        : '<p class="muted">无数据</p>'}
    </div>
  `;
}

/* ── 设置：服务商与密钥 ──────────────────────────────────
   两条纪律：
     · 密钥**永不回显**。界面只显示「已配置 / 来源 / 后 4 位」，输入框永远空的。
     · **不写出「改了却不生效」**。保存时会写回当前真正生效的那一个来源
       （环境变量 > .env > secrets.json），并回读确认。 */

const settingsState = { data: null };

async function renderSettings() {
  setTab('settings');
  stopTaskPolling();
  chapterState.data = null;
  view.innerHTML = '<p class="muted">加载中…</p>';

  try {
    settingsState.data = await getJSON('/api/settings');
  } catch (e) {
    view.innerHTML = notice('bad', `读取设置失败：${esc(e.message)}`);
    return;
  }
  drawSettings();
}

function drawSettings() {
  const d = settingsState.data;
  view.innerHTML = `
    <h1>设置</h1>
    <p class="lead">模型服务商与密钥。改完即刻生效，不需要重启服务。</p>

    ${notice('info', d.notes.map((n) => esc(n)).join('<br>'))}

    ${d.providers.map((p) => providerCard(p, d)).join('')}

    <div class="card">
      <h3>预算闸门</h3>
      <p class="muted" style="margin:0">
        当前模式：<strong>${esc(d.gate_mode)}</strong>　
        单次上限 ${num(d.budget.per_task_token_limit)} tokens　
        月度上限 ${num(d.budget.monthly_token_limit)} tokens　
        超出后：${esc(d.budget.on_exceed || 'abort')}
      </p>
      <p class="muted" style="margin:8px 0 0">
        改这些数值请编辑 <code>providers.yaml</code> 的 <code>settings.budget</code>。
        闸门按实测 token 累计拦，估算只是预估。
      </p>
    </div>
  `;

  view.querySelectorAll('[data-save-key]').forEach((btn) => {
    btn.onclick = () => saveKey(btn.dataset.saveKey);
  });
  view.querySelectorAll('[data-clear-key]').forEach((btn) => {
    btn.onclick = () => clearKey(btn.dataset.clearKey);
  });
  view.querySelectorAll('[data-test]').forEach((btn) => {
    btn.onclick = () => testConnection(btn.dataset.test, btn);
  });
  view.querySelectorAll('[data-probe]').forEach((btn) => {
    btn.onclick = () => fullProbe(btn.dataset.probe, btn);
  });
  view.querySelectorAll('[data-toggle-key]').forEach((btn) => {
    btn.onclick = () => {
      const box = document.getElementById(`key-input-${btn.dataset.toggleKey}`);
      box.style.display = box.style.display === 'none' ? '' : 'none';
    };
  });
}

function providerCard(p, d) {
  const keyState = p.key.configured
    ? `<span class="chip ok">已配置</span>
       <span class="muted">${esc(p.key.masked || '')}　来源：${esc(sourceLabel(p.key.source))}</span>`
    : `<span class="chip bad">未配置</span>
       <span class="muted">读不到 ${esc(p.api_key_env || '(未设置引用名)')}</span>`;

  const priceRows = p.models
    .map(
      (m) => `
      <tr>
        <td>${esc(m.alias || m.id)}<div class="muted">${esc(m.id)}${m.role ? ` · ${esc(m.role)}` : ''}</div></td>
        <td class="num">${priceCell(m.pricing.input_offpeak, m.pricing.input_peak)}</td>
        <td class="num">${priceCell(m.pricing.output_offpeak, m.pricing.output_peak)}</td>
        <td class="num">${priceCell(m.pricing.cached_input_offpeak, m.pricing.cached_input_peak)}</td>
        <td class="num">${
          m.measured && m.measured.tokens_per_cjk_char
            ? esc(m.measured.tokens_per_cjk_char) + ' tokens/字'
            : '<span class="muted">未实测</span>'
        }</td>
      </tr>`
    )
    .join('');

  return `
    <div class="card" data-provider="${esc(p.id)}">
      <div class="spread" style="align-items:flex-start;gap:12px;margin-bottom:12px">
        <div>
          <h2 style="margin:0 0 2px">${esc(p.name)}</h2>
          <div class="muted">${esc(p.base_url || '（未填写 base_url）')}</div>
        </div>
        <div class="row wrap">${keyState}</div>
      </div>

      <p class="muted" style="margin:0 0 12px">
        ${
          p.enabled
            ? '<span class="chip ok">已启用</span>'
            : '<span class="chip warn">enabled: false</span>'
        }
        <span class="muted">这个标记目前只作提示，不拦调用；要改请编辑 <code>providers.yaml</code> 的
          <code>enabled</code>，或直接点下面的「测试连接」确认能不能用。</span>
      </p>

      <div class="row wrap" style="margin-bottom:12px">
        <button id="key-btn-${esc(p.id)}" data-toggle-key="${esc(p.id)}" class="primary">
          ${p.key.configured ? '更换密钥' : '填写密钥'}
        </button>
        <button data-test="${esc(p.id)}">测试连接</button>
        <button data-probe="${esc(p.id)}">完整探测</button>
        ${p.key.configured ? `<button class="ghost" data-clear-key="${esc(p.id)}">清除密钥</button>` : ''}
      </div>

      <div id="key-input-${esc(p.id)}" style="display:none;margin-bottom:12px">
        <div class="row wrap">
          <input type="password" id="key-value-${esc(p.id)}" placeholder="粘贴密钥（sk-…）"
                 style="flex:1;min-width:280px" autocomplete="off" spellcheck="false">
          <button class="primary" data-save-key="${esc(p.id)}">保存</button>
        </div>
        <p class="muted" style="margin:8px 0 0">
          保存位置：<code>${esc(sourceFileHint(p, d))}</code>。保存后不回显，只显示后 4 位。
        </p>
      </div>

      <div id="key-result-${esc(p.id)}"></div>

      <details ${p.pricing_verified ? '' : 'open'}>
        <summary>
          定价与实测
          ${p.pricing_verified ? '<span class="chip ok">已核对</span>' : '<span class="chip warn">尚未经你核对</span>'}
        </summary>
        <div class="table-wrap" style="margin-top:10px">
          <table>
            <thead><tr>
              <th>模型</th><th class="num">输入（空闲/高峰）</th><th class="num">输出（空闲/高峰）</th>
              <th class="num">缓存命中</th><th class="num">中文系数</th>
            </tr></thead>
            <tbody>${priceRows}</tbody>
          </table>
        </div>
        <p class="muted" style="margin:10px 0 0">
          单位：元 / 百万 tokens。来源：${esc(p.pricing_source || '未登记')}
          ${p.pricing_date ? `（${esc(p.pricing_date)}）` : ''}。
          <strong>系统不内置任何默认单价</strong>，这些数字只是快照，请以控制台为准。
        </p>
      </details>
    </div>`;
}

function priceCell(offpeak, peak) {
  if (offpeak == null && peak == null) return '<span class="muted">未填</span>';
  const a = offpeak == null ? '—' : offpeak;
  const b = peak == null ? '—' : peak;
  return `${a} / ${b}`;
}

function sourceLabel(s) {
  return { env: '环境变量', dotenv: '.env 文件', file: 'secrets.json' }[s] || s || '未知';
}

/* 保存会写回「当前生效的那个来源」，所以提示要如实说写去哪儿。
   用户看到「保存成功」却依然 401，通常就是因为写错了地方被更优先的来源盖住。 */
function sourceFileHint(p, d) {
  if (p.key.source === 'env') return `环境变量 ${p.api_key_env}（界面改不了）`;
  if (p.key.source === 'dotenv') return d.env_file;
  if (p.key.source === 'file') return d.secrets_file;
  return d.env_file; // 还没配置过：默认落到 .env
}

async function saveKey(providerId) {
  const input = document.getElementById(`key-value-${providerId}`);
  const box = document.getElementById(`key-result-${providerId}`);
  const value = (input.value || '').trim();
  if (!value) {
    box.innerHTML = notice('warn', '请先粘贴密钥。');
    return;
  }
  const btn = view.querySelector(`[data-save-key="${providerId}"]`);
  await withBusy(btn, '<span class="spinner"></span>保存中…', async () => {
    try {
      const r = await api(`/api/settings/providers/${encodeURIComponent(providerId)}/key`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value }),
      });
      if (!r.ok) {
        box.innerHTML = notice('bad', esc(r.message || '保存失败'));
        return;
      }
      input.value = ''; // 存完就清空，别把明文留在页面上
      box.innerHTML = notice('ok', `已保存到 ${esc(r.stored_to)}（生效来源：${esc(sourceLabel(r.source))}）`);
      settingsState.data = await getJSON('/api/settings');
      drawSettings();
      const fresh = document.getElementById(`key-result-${providerId}`);
      if (fresh) fresh.innerHTML = notice('ok', '密钥已保存。建议点一次「测试连接」确认它真的可用。');
    } catch (e) {
      box.innerHTML = notice('bad', `保存失败：${esc(e.message)}`);
    }
  });
}

async function clearKey(providerId) {
  const ok = await askConfirm({
    title: '清除密钥？',
    html: '<p style="margin:0">清除后该服务商将无法调用，已生成的标注不受影响。</p>',
    confirmLabel: '清除',
    danger: true,
  });
  if (!ok) return;
  const box = document.getElementById(`key-result-${providerId}`);
  try {
    const r = await api(`/api/settings/providers/${encodeURIComponent(providerId)}/key`, {
      method: 'DELETE',
    });
    box.innerHTML = r.ok ? notice('ok', esc(r.message)) : notice('bad', esc(r.message));
    settingsState.data = await getJSON('/api/settings');
    drawSettings();
  } catch (e) {
    box.innerHTML = notice('bad', `清除失败：${esc(e.message)}`);
  }
}

async function testConnection(providerId, btn) {
  const box = document.getElementById(`key-result-${providerId}`);
  box.innerHTML = '<p class="muted">正在测试（只发一句合成测试文本，不涉及作品原文）…</p>';
  await withBusy(btn, '<span class="spinner"></span>测试中…', async () => {
    try {
      const r = await postJSON(
        `/api/settings/providers/${encodeURIComponent(providerId)}/test`,
        {}
      );
      box.innerHTML = stepList(r);
    } catch (e) {
      box.innerHTML = notice('bad', `测试失败：${esc(e.message)}`);
    }
  });
}

async function fullProbe(providerId, btn) {
  const box = document.getElementById(`key-result-${providerId}`);
  box.innerHTML = '<p class="muted">正在做完整探测（会多花几秒和极少量费用），结果会存档…</p>';
  await withBusy(btn, '<span class="spinner"></span>探测中…', async () => {
    try {
      const r = await postJSON(
        `/api/settings/providers/${encodeURIComponent(providerId)}/probe`,
        {}
      );
      box.innerHTML =
        notice('ok', '探测完成，报告已存档。') +
        `<pre class="code" style="max-height:320px">${esc(r.text || '')}</pre>`;
    } catch (e) {
      box.innerHTML = notice('bad', `探测失败：${esc(e.message)}`);
    }
  });
}

function stepList(r) {
  const rows = (r.steps || [])
    .map(
      (s) => `
      <li>
        <span class="chip ${s.ok ? 'ok' : 'bad'}">${s.ok ? '通过' : '失败'}</span>
        <strong>${esc(s.name)}</strong>
        <span class="muted">${esc(s.detail || '')}</span>
        ${s.hint ? `<div style="margin-top:2px">→ ${esc(s.hint)}</div>` : ''}
        ${s.raw ? `<pre class="code" style="margin-top:6px;white-space:pre-wrap">${esc(s.raw)}</pre>` : ''}
      </li>`
    )
    .join('');
  const head = r.ok
    ? notice('ok', `连接正常（模型 ${esc(r.model)}）。可以开始标注了。`)
    : notice('bad', '连接没通，看下面的原因。');
  return head + `<ul class="list">${rows}</ul>`;
}

/* ── 章节阅读 ────────────────────────────────────────────
   章节列表只给元信息，看正文要再点一层。正文按需读——
   一本 933 章的作品，一进页面就把全部正文拉下来既慢又没意义。 */

const chapterState = { name: '', id: '', data: null, raw: false };

function chapterLink(name, id, label) {
  return `<a href="#/work/${encodeURIComponent(name)}/chapter/${encodeURIComponent(id)}">${label}</a>`;
}

async function renderChapter(name, chapterId) {
  setTab('');
  chapterState.name = name;
  chapterState.id = chapterId;
  view.innerHTML = '<p class="muted">读取章节…</p>';

  let d;
  try {
    d = await getJSON(
      `/api/works/${encodeURIComponent(name)}/chapters/${encodeURIComponent(chapterId)}` +
        (chapterState.raw ? '?raw=true' : '')
    );
  } catch (e) {
    view.innerHTML = notice('bad', `读取章节失败：${esc(e.message)}`) +
      `<p><a href="#/work/${encodeURIComponent(name)}">← 返回作品</a></p>`;
    return;
  }
  chapterState.data = d;
  drawChapter();
}

function drawChapter() {
  const d = chapterState.data;
  if (!d) return;
  const f = d.fields || {};
  const title = `第${d.chapter_no}章 ${d.title}`.trim();
  const ann = d.annotation;

  const stats = [
    { k: '字数', v: num(f.char_count ?? d.chars) },
    { k: '段落', v: num(f.para_count) },
    { k: '平均段长', v: f.avg_para_chars ?? '—' },
    { k: '对话占比', v: f.dialogue_ratio != null ? (f.dialogue_ratio * 100).toFixed(1) + '%' : '—' },
    { k: '人称检测', v: f.perspective_detected || '—' },
    { k: '风格违规', v: (f.style_violations || []).length + ' 类' },
  ];

  const violations = f.style_violations || [];
  const modelKeys = ann
    ? [
        ['perspective', '视角'],
        ['hook_strength', '钩子强度'],
        ['hook_type', '钩子类型'],
        ['emotion', '情绪值'],
        ['conflict', '冲突等级'],
        ['info_release', '信息释放'],
        ['mainline_progress', '主线推进'],
        ['motive_strength', '动机强度'],
        ['subplot_count', '支线数'],
        ['scene_switches', '场景切换'],
        ['time_span', '时间跨度'],
      ]
    : [];

  view.innerHTML = `
    <p class="muted" style="margin-bottom:6px">
      <a href="#/work/${encodeURIComponent(chapterState.name)}">← ${esc(chapterState.name)}</a>
    </p>

    <div class="spread chapter-head">
      <div>
        <h1>${esc(title)}</h1>
        <p class="lead">${esc(d.id)} · 第 ${num(d.index + 1)} / ${num(d.total)} 章${
          d.unnumbered ? ' · 无编号' : ''
        }</p>
      </div>
      <div class="row">
        <button id="prev-ch" ${d.prev ? '' : 'disabled'}>← 上一章</button>
        <button id="next-ch" ${d.next ? '' : 'disabled'}>下一章 →</button>
      </div>
    </div>

    ${metrics(stats)}

    <div class="card" style="margin-top:14px">
      <div class="spread" style="margin-bottom:10px">
        <h3 style="margin:0">正文</h3>
        <div class="row">
          <button class="ghost ${d.variant === 'cleaned' ? 'on' : ''}" id="v-clean">清洗后</button>
          <button class="ghost ${d.variant === 'raw' ? 'on' : ''}" id="v-raw">清洗前原文</button>
        </div>
      </div>
      <div class="prose">${renderParagraphs(d.text)}</div>
    </div>

    ${
      violations.length
        ? `<div class="card"><h3>风格违规（${violations.length} 类）</h3><ul class="list">${violations
            .map(
              (v) =>
                `<li><span class="chip warn">${esc(v.rule_id)}</span> ${esc(v.desc || '')} · ${num(
                  v.count
                )} 处</li>`
            )
            .join('')}</ul></div>`
        : ''
    }

    <div class="card">
      <h3>标注结果</h3>
      ${
        ann
          ? `<div class="row wrap" style="margin-bottom:10px">
               <span class="chip ${ann.status === 'ok' ? 'ok' : 'warn'}">${
                 ann.status === 'ok' ? '正常' : '待复核'
               }</span>
               <span class="chip">${esc(ann.review_action || '')}</span>
               ${
                 (ann.provenance || {}).model
                   ? `<span class="muted">${esc(ann.provenance.model)}</span>`
                   : ''
               }
             </div>
             <dl class="kv">${modelKeys
               .map(
                 ([key, label]) =>
                   `<dt>${esc(label)}</dt><dd>${
                     f[key] == null ? '—' : esc(f[key])
                   }</dd>`
               )
               .join('')}</dl>
             ${
               (f.payoffs || []).length
                 ? `<div style="height:12px"></div><div class="muted" style="margin-bottom:6px">爽点</div>
                    <ul class="list">${(f.payoffs || [])
                      .map(
                        (p) =>
                          `<li>${esc(p['类型'] || '')} · 强度 ${esc(p['强度'] ?? '')} · ${esc(
                            p['锚点'] || ''
                          )}</li>`
                      )
                      .join('')}</ul>`
                 : ''
             }
             ${
               (f.foreshadows || []).length
                 ? `<div style="height:12px"></div><div class="muted" style="margin-bottom:6px">伏笔动作</div>
                    <ul class="list">${(f.foreshadows || [])
                      .map(
                        (x) =>
                          `<li>${esc(x['动作'] || '')} ${esc(x['编号'] || '（未编号）')} · ${esc(
                            x['描述'] || ''
                          )}</li>`
                      )
                      .join('')}</ul>`
                 : ''
             }`
          : `<p class="muted" style="margin:0">这一章还没有标注。上面的统计是脚本轨现算的，不需要模型。</p>`
      }
    </div>
  `;

  const prev = document.getElementById('prev-ch');
  const next = document.getElementById('next-ch');
  if (prev) prev.onclick = () => gotoChapter(d.prev);
  if (next) next.onclick = () => gotoChapter(d.next);
  document.getElementById('v-clean').onclick = () => switchVariant(false);
  document.getElementById('v-raw').onclick = () => switchVariant(true);
}

function gotoChapter(entry) {
  if (!entry || !entry.id) return;
  location.hash = `#/work/${encodeURIComponent(chapterState.name)}/chapter/${encodeURIComponent(entry.id)}`;
}

async function switchVariant(raw) {
  if (chapterState.raw === raw) return;
  chapterState.raw = raw;
  await renderChapter(chapterState.name, chapterState.id);
}

function renderParagraphs(text) {
  // 按空行分段。正文一律走 esc()，用户数据不能被当成 HTML 执行。
  return String(text || '')
    .split(/\n{2,}/)
    .map((block) => block.trim())
    .filter(Boolean)
    .map((block) => `<p>${esc(block).replace(/\n/g, '<br>')}</p>`)
    .join('');
}

/* 左右方向键翻章。看整本书时鼠标点按钮很慢，键盘是标注台的既定交互。 */
document.addEventListener('keydown', (e) => {
  if (!chapterState.data) return;
  if (e.target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return;
  if (e.key === 'ArrowLeft') gotoChapter(chapterState.data.prev);
  if (e.key === 'ArrowRight') gotoChapter(chapterState.data.next);
});

/* ── 大纲页 ────────────────────────────────────────────── */

async function renderOutline(name) {
  setTab('');
  view.innerHTML = '<p class="muted">读取大纲…</p>';
  let data;
  try {
    data = await getJSON(`/api/works/${encodeURIComponent(name)}/outline`);
  } catch (e) {
    view.innerHTML = notice('bad', `读取大纲失败：${esc(e.message)}`);
    return;
  }
  if (!data.exists) {
    view.innerHTML =
      notice('warn', '还没有生成过大纲。') +
      '<p class="muted" style="margin:0 0 12px">' +
      '大纲的原料是标注顺带产出的逐章剧情梗概，所以要先跑标注（至少跑一部分）。' +
      '</p>' +
      `<a class="btn" href="#/work/${encodeURIComponent(name)}" style="text-decoration:none;color:inherit">回作品页</a>`;
    return;
  }

  const o = data.outline;
  const meta = (o && o._meta) || {};
  const blocks = data.blocks || [];
  const okBlocks = blocks.filter((b) => b.summary);

  const structure = (o && o.structure) || [];
  const threads = (o && o.main_threads) || [];
  const turns = (o && o.key_turns) || [];

  view.innerHTML = `
    <p class="muted" style="margin-bottom:6px">
      <a href="#/work/${encodeURIComponent(name)}">← ${esc(name)}</a>
    </p>
    <h1>《${esc(name)}》大纲</h1>
    <p class="lead">
      覆盖 ${num(meta.summarized)}/${num(meta.chapters)} 章 · 归约 ${num(meta.blocks_used)} 段${
        meta.generated_at ? ` · 生成于 ${when(meta.generated_at)}` : ''
      }
    </p>

    ${
      meta.missing
        ? notice('warn', `有 ${num(meta.missing)} 章缺梗概（没跑过标注或标注失败），大纲里没有这部分内容。`)
        : ''
    }
    ${
      meta.blocks_failed
        ? notice('bad', `有 ${num(meta.blocks_failed)} 段归约失败，这些段的内容不在下面的大纲里。`)
        : ''
    }

    ${
      o
        ? `
      ${o.logline ? `<div class="card"><h3>一句话</h3><p style="margin:0;font-size:15px">${esc(o.logline)}</p></div>` : ''}
      ${o.premise ? `<div class="card"><h3>开局设定</h3><p style="margin:0">${esc(o.premise)}</p></div>` : ''}

      ${
        structure.length
          ? `<div class="card"><h2>分段结构</h2>${structure
              .map(
                (item) => `
              <div style="padding:10px 0;border-bottom:1px solid var(--border)">
                <strong>${esc(item.part || '（未命名）')}</strong>
                <p style="margin:6px 0 0">${esc(item.gist || '')}</p>
                ${item.turn ? `<p class="muted" style="margin:6px 0 0">关键转折：${esc(item.turn)}</p>` : ''}
              </div>`
              )
              .join('')}</div>`
          : ''
      }

      ${
        threads.length
          ? `<div class="card"><h2>线索</h2>${tableOrEmpty(
              ['线索', '走向'],
              threads.map((t) => [esc(t.thread || ''), esc(t.gist || '')]),
              '无'
            )}</div>`
          : ''
      }

      ${
        turns.length
          ? `<div class="card"><h2>关键转折（按顺序）</h2><ol style="margin:0;padding-left:22px">${turns
              .map((t) => `<li style="margin-bottom:6px">${esc(t)}</li>`)
              .join('')}</ol></div>`
          : ''
      }

      ${o.ending ? `<div class="card"><h2>结局</h2><p style="margin:0">${esc(o.ending)}</p></div>` : ''}

      ${
        o.confidence
          ? `<div class="card"><p class="muted" style="margin:0">模型自评置信度：${esc(
              o.confidence
            )}${
              (o.uncertain_fields || []).length
                ? ` · 依据不足的字段：${esc((o.uncertain_fields || []).join('、'))}`
                : ''
            }</p></div>`
          : ''
      }
    `
        : notice('warn', '全书归约没有成功，只有下面的分段梗概。')
    }

    ${
      (data.errors || []).length
        ? `<div class="card"><h3>未完成的部分</h3><ul class="list">${data.errors
            .map((x) => `<li>${esc(x)}</li>`)
            .join('')}</ul></div>`
        : ''
    }

    <div class="card">
      <h2>逐段梗概（${num(okBlocks.length)}/${num(blocks.length)} 段）</h2>
      ${blocks
        .map(
          (b) => `
        <details style="padding:8px 0;border-bottom:1px solid var(--border)">
          <summary>${esc(b.range || '')}${
            b.summary ? '' : ' <span class="chip bad">归约失败</span>'
          }</summary>
          <p style="margin:8px 0 0">${
            b.summary ? esc(b.summary) : `<span class="muted">${esc(b.error || '未完成')}</span>`
          }</p>
          <p class="muted" style="margin:6px 0 0">${
            (b.chapters || [])
              .slice(0, 6)
              .map((c) => `第${c.chapter_no}章 ${esc(c.title || '')}`)
              .join(' · ')
          }${(b.chapters || []).length > 6 ? ` … 共 ${b.chapters.length} 章` : ''}</p>
        </details>`
        )
        .join('')}
    </div>
  `;
}

/* ── 标注总览 ──────────────────────────────────────────────
   体检报告给的是汇总，单章阅读给的是某一章的全部。
   中间缺「这本书标出来的数据长什么样」——一页表格扫完。 */

const annotListState = { name: '', offset: 0, limit: 50, only: 'all' };

async function renderAnnotations(name) {
  setTab('');
  stopTaskPolling();
  chapterState.data = null;
  annotListState.name = name;
  view.innerHTML = '<p class="muted">加载中…</p>';

  let data;
  try {
    data = await getJSON(
      `/api/works/${encodeURIComponent(name)}/annotations` +
        `?offset=${annotListState.offset}&limit=${annotListState.limit}&only=${annotListState.only}`
    );
  } catch (e) {
    view.innerHTML = notice('bad', `读取标注失败：${esc(e.message)}`);
    return;
  }

  const total = data.total || 0;
  const from = total ? annotListState.offset + 1 : 0;
  const to = Math.min(annotListState.offset + annotListState.limit, total);
  const pages = Math.max(1, Math.ceil(total / annotListState.limit));
  const page = Math.floor(annotListState.offset / annotListState.limit) + 1;

  const rows = (data.items || [])
    .map(
      (r) => `<tr>
        <td>${chapterLink(name, r.chapter_id, `第${r.chapter_no}章`)}</td>
        <td>${esc((r.title || '').slice(0, 14))}</td>
        <td class="num">${num(r.chars)}</td>
        <td>${esc(r.perspective || '—')}</td>
        <td class="num">${r.hook_strength ?? '—'}<span class="muted"> ${esc(r.hook_type || '')}</span></td>
        <td class="num">${r.emotion ?? '—'}</td>
        <td class="num">${r.conflict ?? '—'}</td>
        <td class="num">${r.mainline_progress ?? '—'}</td>
        <td class="num">${num(r.payoff_count)}</td>
        <td class="num">${num(r.foreshadow_count)}</td>
        <td>${esc((r.chapter_summary || '').slice(0, 40))}</td>
      </tr>`
    )
    .join('');

  view.innerHTML = `
    <p class="muted" style="margin-bottom:6px">
      <a href="#/work/${encodeURIComponent(name)}">← ${esc(name)}</a>
    </p>
    <h1>标注数据 · ${esc(name)}</h1>
    <p class="lead">共 ${num(total)} 章有标注　·　1-5 量表实测有 ±1 噪声，看趋势别抠单章值　·　点章节号看完整标注</p>

    <div class="spread" style="margin-bottom:12px">
      <select id="annot-only" style="font:inherit;font-size:13px;padding:5px 8px;border-radius:10px;border:1px solid var(--border-strong);background:var(--surface);color:var(--text)">
        <option value="all">全部</option>
        <option value="review">仅待复核</option>
        <option value="ok">仅正常</option>
      </select>
      <span class="muted">梗概列只显示前 40 字，完整内容在单章阅读页里</span>
    </div>

    ${
      total
        ? `<div class="table-wrap" style="max-height:none">
             <table>
               <thead><tr>
                 <th>章节</th><th>标题</th><th class="num">字数</th><th>视角</th>
                 <th class="num">钩子</th><th class="num">情绪</th><th class="num">冲突</th>
                 <th class="num">主线</th><th class="num">爽点</th><th class="num">伏笔</th>
                 <th>梗概</th>
               </tr></thead>
               <tbody>${rows}</tbody>
             </table>
           </div>
           <div class="spread" style="margin-top:12px">
             <span class="muted">第 ${num(from)} 至 ${num(to)} 条，共 ${num(total)} 条</span>
             <div class="row">
               <button id="a-prev" ${annotListState.offset <= 0 ? 'disabled' : ''}>上一页</button>
               <span class="muted">第 ${num(page)} / ${num(pages)} 页</span>
               <button id="a-next" ${to >= total ? 'disabled' : ''}>下一页</button>
             </div>
           </div>`
        : notice(
            'warn',
            '还没有任何标注。回作品页点「试跑 20 章」先跑一批，这里就会有数据。'
          )
    }
  `;

  const only = document.getElementById('annot-only');
  if (only) {
    only.value = annotListState.only;
    only.onchange = () => {
      annotListState.only = only.value;
      annotListState.offset = 0;
      renderAnnotations(name);
    };
  }
  const prev = document.getElementById('a-prev');
  const next = document.getElementById('a-next');
  if (prev) prev.onclick = () => {
    annotListState.offset = Math.max(0, annotListState.offset - annotListState.limit);
    renderAnnotations(name);
  };
  if (next) next.onclick = () => {
    annotListState.offset += annotListState.limit;
    renderAnnotations(name);
  };
}

/* ── 世界观与人物（实体统计） ──────────────────────────── */

async function renderEntities(name) {
  setTab('');
  stopTaskPolling();
  chapterState.data = null;
  view.innerHTML = '<p class="muted">加载中…</p>';

  let latest;
  try {
    latest = await getJSON(`/api/works/${encodeURIComponent(name)}/entities`);
  } catch (e) {
    view.innerHTML = notice('bad', `读取实体统计失败：${esc(e.message)}`);
    return;
  }

  if (!latest.exists) {
    view.innerHTML =
      notice('warn', '还没有生成过实体统计。') +
      '<p class="muted" style="margin:0 0 12px">它会从正文里抽取人物、势力、能力、地点与人物关系，' +
      '所以必须真的调模型——这本书有多少字就花多少钱，生成前会先给你看估算。</p>' +
      `<a class="btn" href="#/work/${encodeURIComponent(name)}" style="text-decoration:none;color:inherit">回作品页生成</a>`;
    return;
  }

  const m = latest.merged || {};
  const counts = m.counts || {};
  const failed = (latest.blocks || []).filter((b) => b.error);
  const table = (headers, rows) =>
    rows.length
      ? `<div class="table-wrap"><table><thead><tr>${headers
          .map((h) => `<th>${esc(h)}</th>`)
          .join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`
      : '<p class="muted">无数据</p>';

  view.innerHTML = `
    <p class="muted" style="margin-bottom:6px">
      <a href="#/work/${encodeURIComponent(name)}">← ${esc(name)}</a>
    </p>
    <h1>世界观与人物 · ${esc(name)}</h1>
    <p class="lead">生成于 ${when(latest.generated_at)}　·　模型 ${esc(latest.model)}</p>

    <div class="row wrap" style="margin-bottom:14px">
      <span class="chip">人物 ${num(counts.characters)}</span>
      <span class="chip">势力 ${num(counts.factions)}</span>
      <span class="chip">能力 ${num(counts.abilities)}</span>
      <span class="chip">地点 ${num(counts.locations)}</span>
      <span class="chip">关系 ${num(counts.relations)}</span>
      ${failed.length ? `<span class="chip bad">失败 ${num(failed.length)} 块</span>` : ''}
    </div>

    ${failed.length ? notice('bad', `有 ${failed.length} 块抽取失败，这些段里的实体不在下面的表里：${esc(failed.map((b) => b.range).join('、'))}`) : ''}

    <div class="card">
      <h2>人物（${num(counts.characters)}）</h2>
      ${table(
        ['人物', '定位', '身份', '首次出现'],
        (m.characters || []).map(
          (c) =>
            `<tr><td><strong>${esc(c.name)}</strong></td><td>${esc(c.role || '—')}</td>` +
            `<td>${esc(c.identity || '—')}</td><td>第${c.first_chapter ?? '—'}章</td></tr>`
        )
      )}
    </div>

    <div class="card">
      <h2>人物关系（${num(counts.relations)}）</h2>
      ${table(
        ['人物', '关系', '对象', '说明'],
        (m.relations || []).map(
          (r) =>
            `<tr><td>${esc(r.from)}</td><td>${esc(r.type || '—')}</td>` +
            `<td>${esc(r.to)}</td><td>${esc(r.note || '—')}</td></tr>`
        )
      )}
    </div>

    <div class="card">
      <h2>势力（${num(counts.factions)}）</h2>
      ${table(
        ['势力', '立场', '首次出现'],
        (m.factions || []).map(
          (f) => `<tr><td>${esc(f.name)}</td><td>${esc(f.stance || '—')}</td><td>第${f.first_chapter ?? '—'}章</td></tr>`
        )
      )}
    </div>

    <div class="card">
      <h2>能力（${num(counts.abilities)}）</h2>
      ${table(
        ['能力', '会的人', '效果', '首次出现'],
        (m.abilities || []).map(
          (a) =>
            `<tr><td>${esc(a.name)}</td><td>${esc(a.holder || '—')}</td>` +
            `<td>${esc(a.effect || '—')}</td><td>第${a.first_chapter ?? '—'}章</td></tr>`
        )
      )}
    </div>

    <div class="card">
      <h2>地点（${num(counts.locations)}）</h2>
      ${table(
        ['地点', '说明', '首次出现'],
        (m.locations || []).map(
          (l) => `<tr><td>${esc(l.name)}</td><td>${esc(l.note || '—')}</td><td>第${l.first_chapter ?? '—'}章</td></tr>`
        )
      )}
    </div>

    <p class="muted">实体由模型从正文抽取，名字与关系可能有遗漏或归并错误，重要设定请以原文为准。</p>
  `;
}

/* ── 路由 ──────────────────────────────────────────────── */

function setTab(name) {
  document.querySelectorAll('.tab').forEach((el) => {
    el.classList.toggle('active', el.dataset.tab === name);
  });
}

function route() {
  // 渲染异常必须显形。否则一个 JS 报错会让界面停在原地，
  // 用户看到的只是「点了没反应」，而真正的错误躺在 Console 里。
  try {
    chapterState.data = null; // 离开阅读页就清掉，否则方向键会在别的页面上翻章
    const hash = location.hash || '#/shelf';
    const parts = hash.replace(/^#\/?/, '').split('/');
    if (parts[0] === 'import') {
      stopTaskPolling();
      chapterState.data = null;
      return renderImport();
    }
    if (parts[0] === 'settings') return renderSettings();
    if (parts[0] === 'work' && parts[1]) {
      if (parts[2] === 'report') {
        stopTaskPolling(); // 离开作品页就别再轮询了，否则后台一直在打接口
        chapterState.data = null;
        return renderReport(decodeURIComponent(parts[1]));
      }
      if (parts[2] === 'entities') {
        return renderEntities(decodeURIComponent(parts[1]));
      }
      if (parts[2] === 'annotations') {
        return renderAnnotations(decodeURIComponent(parts[1]));
      }
      if (parts[2] === 'outline') {
        stopTaskPolling();
        chapterState.data = null;
        return renderOutline(decodeURIComponent(parts[1]));
      }
      if (parts[2] === 'chapter' && parts[3]) {
        stopTaskPolling();
        return renderChapter(decodeURIComponent(parts[1]), decodeURIComponent(parts.slice(3).join('/')));
      }
      return renderWork(decodeURIComponent(parts[1]));
    }
    stopTaskPolling();
    return renderShelf();
  } catch (e) {
    showFatal('渲染页面时出错', e && e.stack);
    return undefined;
  }
}

window.addEventListener('hashchange', route);

/* 停在 #/import 时再点「导入作品」标签，hash 没变 → 不触发 hashchange → 路由不重跑。
   表现和「点了没反应」一模一样。这里补一次手动渲染。 */
document.querySelector('.tab[data-tab="import"]')?.addEventListener('click', () => {
  if (imp.finished) resetImport();
  if ((location.hash || '').replace(/^#\/?/, '') === 'import') route();
});

async function boot() {
  try {
    const h = await getJSON('/api/health');
    document.getElementById('version').textContent = 'v' + h.version;
    connEl.textContent = '服务运行中';
    connEl.className = 'conn ok';
  } catch {
    connEl.textContent = '服务不可用';
    connEl.className = 'conn bad';
  }
  route();
}

boot();
