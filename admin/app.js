'use strict';
const $ = id => document.getElementById(id);
let token = '', state = null, busy = false, polling = null, lastMode = 'rule', noticeSource = 'action';
const modeButtons = [...document.querySelectorAll('[data-mode]')];
function notice(message, error = false, source = 'action') {
  noticeSource = source;
  $('notice').textContent = message || '';
  $('notice').hidden = !message;
  $('notice').classList.toggle('error', error);
}
function lock(value) {
  busy = value;
  $('power-switch').disabled = value;
  modeButtons.forEach(button => button.disabled = value);
  $('duration').disabled = value || Boolean(state?.enabled);
  if (value) $('power-description').textContent = '正在应用设置，请保持页面打开…';
}
function render(next) {
  state = next;
  if (next.mode === 'rule' || next.mode === 'global') lastMode = next.mode;
  const effective = next.routing_active ? next.mode : 'direct';
  const selected = next.enabled ? next.mode : 'direct';
  $('power-switch').setAttribute('aria-checked', String(next.enabled));
  modeButtons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.mode === selected)));
  $('state-badge').textContent = next.state === 'active' ? '分流已启用' : next.state === 'degraded' ? '状态待确认' : '使用原网络';
  $('state-badge').className = `badge ${next.state === 'active' ? '' : next.state === 'degraded' ? 'degraded' : 'off'}`;
  $('power-description').textContent = next.state === 'degraded' ? '代理状态未能完整确认。可关闭总开关恢复原网络。' : next.enabled ? '仅对下方列出的设备或网络生效。' : '代理已暂停，设备使用原来的上网路径。';
  const chinaProxy = effective === 'global';
  const overseasProxy = effective === 'rule' || effective === 'global';
  $('route-china').textContent = next.state === 'degraded' ? '待确认' : chinaProxy ? '代理' : '直连';
  $('route-china').classList.toggle('proxy', chinaProxy);
  $('route-overseas').textContent = next.state === 'degraded' ? '待确认' : overseasProxy ? '代理' : '直连';
  $('route-overseas').classList.toggle('proxy', overseasProxy);
  const scope = next.scope || {};
  $('scope-title').textContent = scope.type === 'networks' ? '指定网络中的设备' : '单台设备';
  const identifiers = scope.type === 'networks' ? (scope.networks || []).join('、') : scope.device_ipv4;
  $('scope-description').textContent = [identifiers, (scope.interfaces || []).join('、')].filter(Boolean).join(' · ') || '尚未配置接管范围';
  $('proxy-name').textContent = next.selected_proxy || '尚未连接';
  $('engine-description').textContent = next.engine_active ? '代理服务正在运行' : '代理服务已停止';
  const remaining = next.remaining_seconds;
  $('remaining').hidden = !next.enabled || remaining == null;
  $('session-label').textContent = next.enabled ? '自动恢复直连倒计时' : '下次启用时自动恢复直连';
  if (next.enabled && remaining != null) {
    const seconds = Math.max(0, Math.floor(remaining));
    $('remaining').textContent = `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, '0')} 秒`;
  }
  $('duration').disabled = busy || next.enabled;
  $('last-updated').textContent = `更新于 ${new Date().toLocaleTimeString('zh-CN', {hour12: false})}`;
  if (next.message && !busy) notice(next.message, next.state === 'degraded', 'status');
  else if (!busy && noticeSource !== 'action' && $('notice').classList.contains('error')) notice('');
}
async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {'Authorization': `Bearer ${token}`, ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store', credentials: 'omit', redirect: 'error'
  });
  let result;
  try { result = await response.json(); } catch { throw new Error('无法读取路由器响应，请检查本地连接。'); }
  if (!response.ok) {
    if (result.status) render(result.status);
    if (response.status === 401) logout();
    throw new Error(response.status === 401 ? '管理口令不正确或已失效。' : result.error || '操作未完成，请刷新状态后重试。');
  }
  return result;
}
async function refresh() {
  if (busy || !token) return;
  try { render(await api('/api/status')); }
  catch (error) { if (token) notice(error.message, true, 'poll'); }
}
function logout() {
  stopTelemetry();
  clearInterval(polling); polling = null; token = ''; state = null;
  $('token').value = '';
  $('dashboard').hidden = true; $('login').hidden = false; $('logout').hidden = true;
  $('token').focus();
}
async function control(enabled, mode) {
  if (busy || !state) return;
  const payload = {enabled, mode: mode || lastMode, minutes: Number($('duration').value)};
  lock(true); notice(enabled ? '正在连接代理并应用分流…' : '正在恢复原来的上网路径…');
  try {
    render(await api('/api/control', payload));
    notice(enabled ? '设置已生效。切换模式不会延长已有试用时间。' : '已恢复原网络。');
  } catch (error) { notice(error.message, true); }
  finally { lock(false); await refresh(); }
}
$('login-form').addEventListener('submit', async event => {
  event.preventDefault(); $('login-error').hidden = true;
  token = $('token').value.trim(); $('login-button').disabled = true;
  try {
    const next = await api('/api/status');
    $('token').value = ''; $('login').hidden = true; $('dashboard').hidden = false; $('logout').hidden = false;
    render(next); lock(false); notice('');
    clearInterval(polling); polling = setInterval(refresh, 5000); startTelemetry();
  } catch (error) {
    token = ''; $('login-error').textContent = error.message; $('login-error').hidden = false;
  } finally { $('login-button').disabled = false; }
});
$('power-switch').addEventListener('click', () => control(!state.enabled, lastMode));
modeButtons.forEach(button => button.addEventListener('click', () => control(button.dataset.mode !== 'direct', button.dataset.mode === 'direct' ? lastMode : button.dataset.mode)));
$('logout').addEventListener('click', logout);
window.addEventListener('pagehide', () => { token = ''; });
