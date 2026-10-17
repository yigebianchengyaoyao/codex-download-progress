'use strict';
const byId = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
const token = fragment.get('token') || sessionStorage.getItem('workbench-token') || '';
if (token) sessionStorage.setItem('workbench-token', token);
history.replaceState(null, '', '/');
let state = null, selected = '', initialized = false, stopped = false, lastSignature = '';
const labels = {queued:'排队中',running:'正在下载',completed:'已完成',failed:'失败',cancelled:'已取消',skipped:'已跳过'};
const bytes = value => {
  if (!Number.isFinite(value)) return '大小未知';
  if (value < 1024) return `${value} B`;
  const unit = Math.min(Math.floor(Math.log(value) / Math.log(1024)), 4);
  return `${(value / 1024 ** unit).toFixed(1)} ${['B','KiB','MiB','GiB','TiB'][unit]}`;
};
function notice(message = '') { byId('notice').textContent = message; byId('notice').hidden = !message; }
async function api(path, data) {
  const response = await fetch(path, {headers:{'X-Workbench-Token':token,...(data ? {'Content-Type':'application/json'} : {})},...(data ? {method:'POST',body:JSON.stringify(data)} : {})});
  if (!response.ok) { const error = await response.json(); throw new Error(error.error || '本机服务请求失败'); }
  return response;
}
function current() { return state?.batches.find(batch => batch.batch_id === selected); }
function text(tag, className, value) { const node = document.createElement(tag); node.className = className; node.textContent = value; return node; }
function render() {
  const batches = state.batches;
  const signature = JSON.stringify(batches.map(batch => [batch.batch_id,batch.title]));
  if (signature !== lastSignature) {
    byId('batches').replaceChildren(...batches.map(batch => { const option = document.createElement('option'); option.value = batch.batch_id; option.textContent = batch.title; return option; }));
    lastSignature = signature;
  }
  if (!batches.some(batch => batch.batch_id === selected)) selected = batches.at(-1)?.batch_id || '';
  byId('batches').hidden = !batches.length;
  byId('batches').value = selected;
  const batch = current();
  byId('empty').hidden = !!batch;
  byId('batch-detail').hidden = !batch;
  if (batch) {
    const count = batch.counts;
    byId('batch-title').textContent = batch.title;
    byId('badge').hidden = !batch.demo;
    byId('summary').textContent = `${batch.total} 个文件 · 完成 ${count.completed} · 失败 ${count.failed} · 取消 ${count.cancelled} · 跳过 ${count.skipped} · 去重 ${batch.duplicate_count}`;
    byId('cancel').disabled = batch.status !== 'running' || batch.cancel_requested;
    byId('cancel').textContent = batch.cancel_requested ? '正在取消…' : '取消剩余任务';
    byId('poster').disabled = batch.status !== 'finished';
    byId('jobs').replaceChildren(...batch.jobs.map(job => {
      const row = text('article','job','');
      const head = text('div','job-head','');
      head.append(text('span','job-name',job.filename),text('span',`job-state ${job.status}`,labels[job.status] || job.status));
      const track = text('div','track','');
      const fill = text('div','fill','');
      const progress = job.status === 'completed' ? 100 : Math.max(0,Math.min(100,job.percent || 0));
      fill.style.transform = `scaleX(${progress / 100})`;
      track.append(fill);
      const meta = text('div','job-meta','');
      meta.append(text('span','',`${job.category} · ${bytes(job.downloaded_bytes)} / ${bytes(job.total_bytes)}`),text('span','',job.status === 'running' ? `${bytes(job.speed_bytes_per_second || 0)}/s · ${job.percent == null ? '总量未知' : job.percent.toFixed(1)+'%'}` : ''));
      row.append(head,track,meta);
      if (job.error) row.append(text('p','job-error',job.error));
      return row;
    }));
  }
  const batchTasks = new Set(batches.flatMap(batch => batch.jobs.map(job => job.download_id)));
  const singles = state.downloads.filter(job => !batchTasks.has(job.download_id));
  byId('single-tasks').hidden = !singles.length;
  byId('single-list').replaceChildren(...singles.map(job => text('p','',`${job.path} · ${labels[job.status]} · ${bytes(job.downloaded_bytes)}`)));
}
async function poll() {
  if (stopped) return;
  try {
    state = await (await api('/api/state')).json();
    byId('connection').textContent = '● 仅本机运行';
    if (!initialized) { byId('directory').value = state.directory; initialized = true; }
    render();
  } catch (error) { byId('connection').textContent = '连接已断开'; notice(error.message); }
  if (!stopped) setTimeout(poll,750);
}
async function action(button, task) {
  button.disabled = true;
  try { notice(); await task(); } catch (error) { notice(error.message); } finally { button.disabled = false; }
}
byId('batch-form').addEventListener('submit', event => {
  event.preventDefault();
  action(byId('start'),async () => {
    const urls = byId('urls').value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    const batch = await (await api('/api/batch',{urls,directory:byId('directory').value.trim(),title:byId('title').value.trim(),organize:byId('organize').checked})).json();
    selected = batch.batch_id; byId('poster-preview').hidden = true;
  });
});
byId('demo').addEventListener('click', () => action(byId('demo'),async () => {
  const batch = await (await api('/api/demo',{directory:byId('directory').value.trim()})).json();
  selected = batch.batch_id; byId('poster-preview').hidden = true;
}));
byId('preset').addEventListener('change',event => { byId('title').value = event.target.value; });
byId('import-file').addEventListener('change',event => action(byId('start'),async () => {
  const file = event.target.files[0];
  if (!file) return;
  if (file.size > 262144) throw new Error('TXT 请小于 256 KiB，每批最多 50 个链接');
  byId('urls').value = await file.text(); event.target.value = '';
}));
byId('batches').addEventListener('change',event => { selected = event.target.value; byId('poster-preview').hidden = true; render(); });
byId('cancel').addEventListener('click',() => action(byId('cancel'),() => api('/api/cancel-batch',{batch_id:selected})));
byId('folder').addEventListener('click',() => action(byId('folder'),() => api('/api/open-folder',{batch_id:selected})));
function save(blob, filename) {
  const url = URL.createObjectURL(blob), anchor = document.createElement('a');
  anchor.href = url; anchor.download = filename; anchor.click(); setTimeout(() => URL.revokeObjectURL(url),3000);
}
byId('csv').addEventListener('click',() => action(byId('csv'),async () => {
  const response = await api(`/api/receipt?batch_id=${encodeURIComponent(selected)}&format=csv`);
  save(await response.blob(),`下载回执-${selected}.csv`);
}));
byId('poster').addEventListener('click',() => action(byId('poster'),async () => {
  const receipt = await (await api(`/api/receipt?batch_id=${encodeURIComponent(selected)}`)).json();
  if (receipt.status !== 'finished') throw new Error('请等待本批任务结束后生成分享图');
  const canvas = byId('share-canvas'), context = canvas.getContext('2d');
  context.fillStyle = '#f6f5f0'; context.fillRect(0,0,1080,1440);
  const line = (value,x,y,size=32,color='#232b27') => { context.fillStyle=color; context.font=`${size}px "Microsoft YaHei", sans-serif`; context.fillText(value,x,y); };
  const rule = y => { context.fillStyle='#d9dfd6'; context.fillRect(80,y,920,2); };
  line('DOWNLOAD PROGRESS',80,110,24,'#626c65');
  line(receipt.demo ? '本机演示 / 自生成文件' : '本次下载记录',80,195,32,'#285b40');
  line('每一份文件，都有回执。',80,300,52);
  rule(360);
  line(String(receipt.counts.completed),80,565,180,'#285b40');
  line(`/ ${receipt.total} 个文件完成`,80,650,40);
  line(`成功下载 ${bytes(receipt.completed_bytes)}`,80,750,32);
  rule(810);
  line(`失败 ${receipt.counts.failed}    取消 ${receipt.counts.cancelled}    跳过 ${receipt.counts.skipped}`,80,900,32);
  line(`重复链接已去重 ${receipt.duplicate_count} 个`,80,970,28,'#626c65');
  line(receipt.created_at.slice(0,10),80,1060,28,'#626c65');
  rule(1140);
  line('下载工作台 · 本机真实任务记录',80,1220,30);
  line('已隐藏文件名、下载链接与本地路径',80,1280,26,'#626c65');
  line(receipt.demo ? '演示结果仅说明功能，不代表实际用户使用效果。' : '仅反映这一批次；不代表内容来源或预期哈希已验证。',80,1350,22,'#626c65');
  byId('poster-preview').hidden = false;
}));
byId('save-poster').addEventListener('click',() => byId('share-canvas').toBlob(blob => { if (blob) save(blob,`下载记录-${selected}.png`); },'image/png'));
byId('quit').addEventListener('click',() => action(byId('quit'),async () => {
  const running = state?.downloads.some(job => job.status === 'running') || state?.batches.some(batch => batch.status === 'running');
  if (state?.standalone && running && !confirm('退出将取消未完成的下载，保留部分文件。确定退出？')) return;
  await api('/api/quit',{}); stopped = true; byId('connection').textContent = '服务已退出'; notice('工作台已关闭，可以关闭此标签页。重新打开程序可再次使用。');
}));
poll();
