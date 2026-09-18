'use strict';
let telemetryPolling = null, telemetryPending = false, chartSamples = [], chartAnchor = 0;
const metricEmpty = '—';
function scaledBytes(value, rate = false) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return null;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let unit = 0, number = value;
  while (number >= 1000 && unit < units.length - 1) { number /= 1000; unit++; }
  const digits = number >= 100 || unit === 0 ? 0 : number >= 10 ? 1 : 2;
  return {number: number.toFixed(digits), unit: units[unit] + (rate ? '/s' : '')};
}
function metricText(id, value, unit = '') {
  const target = $(id); target.replaceChildren();
  if (value == null) { target.textContent = metricEmpty; return; }
  target.append(document.createTextNode(value));
  if (unit) { const small = document.createElement('small'); small.textContent = unit; target.append(small); }
}
function byteMetric(id, value) {
  const formatted = scaledBytes(value, true);
  metricText(id, formatted?.number, formatted?.unit);
}
function prettyBytes(value, rate = false) {
  const formatted = scaledBytes(value, rate);
  return formatted ? `${formatted.number} ${formatted.unit}` : metricEmpty;
}
function elapsed(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return metricEmpty;
  const seconds = Math.floor(value);
  if (seconds >= 86400) return `${Math.floor(seconds / 86400)} 天 ${Math.floor(seconds % 86400 / 3600)} 小时`;
  if (seconds >= 3600) return `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}
function chartPath(samples, key, anchor, maximum, gap) {
  let path = '', previous = null;
  for (const sample of samples) {
    const value = sample[key];
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) { previous = null; continue; }
    const x = (sample.timestamp_ms - (anchor - 300000)) / 300000 * 900;
    const y = 138 - Math.min(value / maximum, 1) * 136;
    const continuous = previous && sample.session_id === previous.session_id && sample.timestamp_ms - previous.timestamp_ms <= gap;
    path += `${continuous ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)} `;
    previous = sample;
  }
  return path.trim();
}
function renderTelemetry(data) {
  const stale = data.stale === true;
  const useful = !stale && (data.state === 'running' || data.state === 'warming');
  byteMetric('metric-down', useful ? data.download_bytes_per_second : null);
  byteMetric('metric-up', useful ? data.upload_bytes_per_second : null);
  metricText('metric-connections', useful && Number.isSafeInteger(data.connections) && data.connections >= 0 ? data.connections.toLocaleString('zh-CN') : null);
  metricText('metric-cpu', useful && Number.isFinite(data.cpu_percent) && data.cpu_percent >= 0 ? data.cpu_percent.toFixed(1) : null, '%');
  const memory = scaledBytes(useful ? data.memory_bytes : null);
  metricText('metric-memory', memory?.number, memory?.unit);
  $('total-down').textContent = `↓ ${prettyBytes(useful ? data.download_total_bytes : null)}`;
  $('total-up').textContent = `↑ ${prettyBytes(useful ? data.upload_total_bytes : null)}`;
  $('metric-uptime').textContent = elapsed(useful ? data.uptime_seconds : null);
  const label = data.state === 'warming' && data.sampled_at_ms == null ? '等待采样' : stale ? '采样延迟' : ({running:'每 2 秒更新',warming:'正在计算速率',stopped:'代理未运行',unavailable:'暂不可用'}[data.state] || '暂不可用');
  $('telemetry-state').textContent = label;
  $('telemetry-state').className = `telemetry-state ${useful ? 'live' : stale || data.state === 'unavailable' ? 'stale' : ''}`;
  $('telemetry-note').textContent = data.state === 'stopped' ? '代理未运行，当前原网络流量不在统计范围内。历史只在内存中保留五分钟。' : stale || data.state === 'unavailable' ? '当前采样不可用，数值暂不显示；历史曲线不代表当前流量。' : '仅统计 Mihomo 处理的流量，含直连和后台探测。累计随进程或计数重置；CPU 100% 表示一个核心。';
  const history = Array.isArray(data.history) ? data.history : [];
  const newest = history.reduce((latest, sample) => Number.isFinite(sample.timestamp_ms) ? Math.max(latest, sample.timestamp_ms) : latest, 0);
  chartAnchor = Number.isFinite(data.sampled_at_ms) ? data.sampled_at_ms : newest;
  chartAnchor += Number.isFinite(data.age_seconds) ? Math.max(0, data.age_seconds) * 1000 : 0;
  chartSamples = history.filter(sample => Number.isFinite(sample.timestamp_ms) && sample.timestamp_ms >= chartAnchor - 300000 && sample.timestamp_ms <= chartAnchor).sort((a,b) => a.timestamp_ms - b.timestamp_ms);
  const numbers = chartSamples.flatMap(sample => [sample.download_bytes_per_second, sample.upload_bytes_per_second]).filter(value => typeof value === 'number' && Number.isFinite(value) && value >= 0);
  const maximum = Math.max(1000, ...numbers) * 1.1;
  const gap = Math.max(6, Number(data.interval_seconds) * 3 || 6) * 1000;
  $('chart-download').setAttribute('d', chartPath(chartSamples, 'download_bytes_per_second', chartAnchor, maximum, gap));
  $('chart-upload').setAttribute('d', chartPath(chartSamples, 'upload_bytes_per_second', chartAnchor, maximum, gap));
  $('chart-max').textContent = numbers.length ? prettyBytes(maximum, true) : metricEmpty;
  $('chart-middle').textContent = numbers.length ? prettyBytes(maximum / 2, true) : metricEmpty;
  $('chart-empty').hidden = chartSamples.filter(sample => [sample.download_bytes_per_second, sample.upload_bytes_per_second].some(value => typeof value === 'number' && Number.isFinite(value) && value >= 0)).length >= 2;
  $('chart-empty').textContent = data.state === 'stopped' ? '启用代理后开始记录流量' : '等待足够的速率样本';
  $('traffic-chart').classList.toggle('stale', stale || data.state === 'unavailable');
}
async function refreshTelemetry() {
  if (!token || telemetryPending || document.hidden) return;
  telemetryPending = true;
  const currentToken = token;
  try {
    const data = await api('/api/telemetry');
    if (token === currentToken && token) renderTelemetry(data);
  } catch {
    if (token === currentToken && token) {
      renderTelemetry({state:'unavailable',stale:true,history:chartSamples,sampled_at_ms:chartAnchor});
    }
  } finally { telemetryPending = false; }
}
function startTelemetry() { clearInterval(telemetryPolling); refreshTelemetry(); telemetryPolling = setInterval(refreshTelemetry, 2000); }
function stopTelemetry() { clearInterval(telemetryPolling); telemetryPolling = null; chartSamples = []; renderTelemetry({state:'unavailable',history:[]}); }
$('traffic-svg').addEventListener('pointermove', event => {
  if (!chartSamples.length) return;
  const bounds = event.currentTarget.getBoundingClientRect();
  const fraction = Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width));
  const timestamp = chartAnchor - 300000 + fraction * 300000;
  const closest = chartSamples.reduce((best,sample) => Math.abs(sample.timestamp_ms - timestamp) < Math.abs(best.timestamp_ms - timestamp) ? sample : best);
  const x = Math.max(0, Math.min(900, (closest.timestamp_ms - chartAnchor + 300000) / 300000 * 900));
  $('chart-cursor').setAttribute('x1', x); $('chart-cursor').setAttribute('x2', x); $('chart-cursor').setAttribute('visibility', 'visible');
  $('chart-inspect').textContent = `${new Date(closest.timestamp_ms).toLocaleTimeString('zh-CN',{hour12:false})}　↓ ${prettyBytes(closest.download_bytes_per_second,true)}　↑ ${prettyBytes(closest.upload_bytes_per_second,true)}`;
});
$('traffic-svg').addEventListener('pointerleave', () => { $('chart-cursor').setAttribute('visibility','hidden'); $('chart-inspect').textContent = '指向曲线查看当时速率'; });
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshTelemetry(); });
