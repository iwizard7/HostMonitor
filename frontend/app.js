// Host Monitor Frontend Application
let hosts = [];
let selectedHostId = null;
let pingChart = null;
let pollTimer = null;

// DOM Elements
const hostsContainer = document.getElementById('hosts-container');
const activeHostsCount = document.getElementById('active-hosts-count');
const dashboardView = document.getElementById('dashboard-view');
const noHostSelected = document.getElementById('no-host-selected');
const hostDetails = document.getElementById('host-details');

// Details Elements
const headerHostName = document.getElementById('header-host-name');
const headerHostTarget = document.getElementById('header-host-target');
const headerStatusDot = document.getElementById('header-status-indicator');
const headerStatusText = document.getElementById('header-status-text');
const headerInterval = document.getElementById('header-interval');
const btnToggleActive = document.getElementById('btn-toggle-active');
const btnDeleteHost = document.getElementById('btn-delete-host');
const btnExportSingle = document.getElementById('btn-export-single');
const btnExportActive = document.getElementById('btn-export-active');
const titlebarExportText = document.getElementById('titlebar-export-text');

// Metrics Elements
const metricCurrentPing = document.getElementById('metric-current-ping');
const metricTtl = document.getElementById('metric-ttl');
const metricUptime = document.getElementById('metric-uptime');
const metricUptimeBar = document.getElementById('metric-uptime-bar');
const metricPacketLossCount = document.getElementById('metric-packet-loss-count');
const metricPingCounts = document.getElementById('metric-ping-counts');
const metricMinPing = document.getElementById('metric-min-ping');
const metricAvgPing = document.getElementById('metric-avg-ping');
const metricMaxPing = document.getElementById('metric-max-ping');
const pingsLogBody = document.getElementById('pings-log-body');

// Modal Elements
const btnAddHost = document.getElementById('btn-add-host');
const modalAddHost = document.getElementById('modal-add-host');
const btnCloseModal = document.getElementById('btn-close-modal');
const btnCancelModal = document.getElementById('btn-cancel-modal');
const formAddHost = document.getElementById('form-add-host');
const modalError = document.getElementById('modal-error');

// Initialize Chart.js
function initChart() {
  const ctx = document.getElementById('pingChart').getContext('2d');
  
  const gradient = ctx.createLinearGradient(0, 0, 0, 200);
  gradient.addColorStop(0, 'rgba(47, 129, 247, 0.45)');
  gradient.addColorStop(1, 'rgba(47, 129, 247, 0.0)');

  pingChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        label: 'Ping (ms)',
        data: [],
        borderColor: '#2f81f7',
        borderWidth: 2,
        backgroundColor: gradient,
        fill: true,
        tension: 0.35,
        pointRadius: 2,
        pointHoverRadius: 5,
        pointBackgroundColor: '#58a6ff',
        pointBorderColor: '#fff',
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: {
        duration: 350
      },
      interaction: {
        intersect: false,
        mode: 'index'
      },
      scales: {
        x: {
          grid: {
            color: 'rgba(255, 255, 255, 0.05)',
            drawBorder: false,
          },
          ticks: {
            color: '#6e7681',
            font: { family: 'JetBrains Mono', size: 10 },
            maxRotation: 0,
            maxTicksLimit: 8
          }
        },
        y: {
          beginAtZero: true,
          grid: {
            color: 'rgba(255, 255, 255, 0.06)',
            drawBorder: false,
          },
          ticks: {
            color: '#8b949e',
            font: { family: 'JetBrains Mono', size: 10 },
            callback: (val) => `${val}ms`
          }
        }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#161b22',
          titleColor: '#8b949e',
          bodyColor: '#58a6ff',
          borderColor: 'rgba(255, 255, 255, 0.1)',
          borderWidth: 1,
          displayColors: false,
          callbacks: {
            label: (item) => `Пинг: ${item.parsed.y !== null ? item.parsed.y + ' ms' : 'Превышен таймаут'}`
          }
        }
      }
    }
  });
}

// Fetch list of all hosts
async function fetchHosts() {
  try {
    const res = await fetch('/api/hosts');
    if (!res.ok) throw new Error('Ошибка загрузки');
    hosts = await res.json();
    renderSidebar();

    // If a host is selected, refresh its details view
    if (selectedHostId) {
      const current = hosts.find(h => h.id === selectedHostId);
      if (current) {
        updateHostDetails(current);
        await updateHistoryAndChart(selectedHostId);
      } else if (hosts.length > 0) {
        selectHost(hosts[0].id);
      } else {
        selectedHostId = null;
        showNoHost();
      }
    } else if (hosts.length > 0) {
      selectHost(hosts[0].id);
    } else {
      showNoHost();
    }
  } catch (err) {
    console.error('Fetch hosts error:', err);
  }
}

// Render Hosts in Sidebar
function renderSidebar() {
  activeHostsCount.textContent = hosts.filter(h => h.is_active).length;
  hostsContainer.innerHTML = '';

  if (hosts.length === 0) {
    hostsContainer.innerHTML = '<div class="text-center text-muted" style="padding: 20px 0; font-size: 13px;">Нет хостов для мониторинга</div>';
    return;
  }

  hosts.forEach(host => {
    const item = document.createElement('div');
    item.className = `host-item ${host.id === selectedHostId ? 'active' : ''}`;
    item.onclick = () => selectHost(host.id);

    let statusClass = 'offline';
    let pingText = '-- ms';
    let pingClass = '';

    if (!host.is_active) {
      statusClass = 'paused';
      pingText = 'ПАУЗА';
    } else if (host.latest && host.latest.is_reachable) {
      statusClass = 'online';
      const lat = host.latest.latency_ms;
      pingText = `${lat.toFixed(1)} ms`;
      pingClass = lat < 50 ? 'fast' : lat < 120 ? 'slow' : 'down';
    } else if (host.latest && !host.latest.is_reachable) {
      statusClass = 'offline';
      pingText = 'DOWN';
      pingClass = 'down';
    }

    item.innerHTML = `
      <div class="host-item-left">
        <span class="status-dot ${statusClass}"></span>
        <div class="host-info-col">
          <span class="host-item-name">${escapeHtml(host.name)}</span>
          <span class="host-item-target">${escapeHtml(host.target)}</span>
        </div>
      </div>
      <div class="host-item-ping ${pingClass}">${pingText}</div>
    `;

    hostsContainer.appendChild(item);
  });
}

// Select a host
async function selectHost(hostId) {
  selectedHostId = hostId;
  renderSidebar();

  const host = hosts.find(h => h.id === hostId);
  if (!host) return;

  noHostSelected.style.display = 'none';
  hostDetails.style.display = 'flex';

  updateHostDetails(host);
  await updateHistoryAndChart(hostId);
}

function showNoHost() {
  noHostSelected.style.display = 'flex';
  hostDetails.style.display = 'none';
}

// Update Details Cards
function updateHostDetails(host) {
  headerHostName.textContent = host.name;
  headerHostTarget.textContent = host.target;
  headerInterval.textContent = `${host.interval_sec}с`;
  if (titlebarExportText) {
    titlebarExportText.textContent = `Выгрузить ${host.name} в CSV`;
  }

  // Status & Dot
  let status = 'Офлайн';
  let dotClass = 'offline';
  let labelClass = 'offline';

  if (!host.is_active) {
    status = 'Приостановлен';
    dotClass = 'paused';
    labelClass = 'paused';
  } else if (host.latest && host.latest.is_reachable) {
    status = 'В сети (Онлайн)';
    dotClass = 'online';
    labelClass = 'online';
  }

  headerStatusDot.className = `status-dot ${dotClass}`;
  headerStatusText.className = `status-label ${labelClass}`;
  headerStatusText.textContent = status;

  // Toggle button icon and tooltip
  btnToggleActive.title = host.is_active ? "Приостановить мониторинг" : "Возобновить мониторинг";
  btnToggleActive.innerHTML = host.is_active ? `
    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
      <rect x="6" y="4" width="4" height="16"></rect>
      <rect x="14" y="4" width="4" height="16"></rect>
    </svg>` : `
    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
      <polygon points="5 3 19 12 5 21 5 3"></polygon>
    </svg>`;

  // Current Ping
  if (host.latest && host.latest.is_reachable && host.latest.latency_ms !== null) {
    metricCurrentPing.textContent = host.latest.latency_ms.toFixed(1);
    metricTtl.textContent = host.latest.ttl ? `TTL: ${host.latest.ttl}` : 'TTL: --';
  } else {
    metricCurrentPing.textContent = host.is_active ? 'Оффлайн' : '--';
    metricTtl.textContent = host.latest && host.latest.error_msg ? host.latest.error_msg : 'TTL: --';
  }

  // Stats
  const st = host.stats || {};
  const uptime = st.uptime_pct !== undefined ? st.uptime_pct : 100;
  metricUptime.textContent = uptime.toFixed(1);
  metricUptimeBar.style.width = `${uptime}%`;
  metricUptimeBar.style.backgroundColor = uptime > 95 ? '#2ea043' : uptime > 80 ? '#d29922' : '#f85149';

  const lossPct = st.packet_loss_pct !== undefined ? st.packet_loss_pct : 0;
  const lostCount = st.lost_pings !== undefined ? st.lost_pings : ((st.total_pings || 0) - (st.successful_pings || 0));
  
  if (metricPacketLossCount) {
    metricPacketLossCount.textContent = lostCount;
    // Color alert if packets are lost
    metricPacketLossCount.style.color = lostCount > 0 ? '#f85149' : 'var(--text-primary)';
  }
  if (metricPingCounts) {
    metricPingCounts.textContent = `${lossPct.toFixed(1)}% • ${st.successful_pings || 0} из ${st.total_pings || 0} получено`;
  }

  metricMinPing.textContent = st.min_latency !== null && st.min_latency !== undefined ? `${st.min_latency} мс` : '--';
  metricAvgPing.textContent = st.avg_latency !== null && st.avg_latency !== undefined ? `${st.avg_latency} мс` : '--';
  metricMaxPing.textContent = st.max_latency !== null && st.max_latency !== undefined ? `${st.max_latency} мс` : '--';
}

// Fetch host recent history for Chart and Table
async function updateHistoryAndChart(hostId) {
  try {
    const res = await fetch(`/api/hosts/${hostId}/history?limit=60`);
    if (!res.ok) return;
    const history = await res.json();

    // Update Chart
    const labels = [];
    const dataPoints = [];

    history.forEach(item => {
      const dt = new Date(item.timestamp);
      labels.push(dt.toLocaleTimeString());
      dataPoints.push(item.is_reachable ? item.latency_ms : null);
    });

    if (pingChart) {
      pingChart.data.labels = labels;
      pingChart.data.datasets[0].data = dataPoints;
      pingChart.update('none'); // silent update without full re-render
    }

    // Update Log Table (last 15 records)
    renderLogTable(history.slice(-15).reverse());
  } catch (err) {
    console.error('History fetch error:', err);
  }
}

function renderLogTable(records) {
  if (!records || records.length === 0) {
    pingsLogBody.innerHTML = `<tr><td colspan="5" class="text-center text-muted">Нет данных</td></tr>`;
    return;
  }

  pingsLogBody.innerHTML = records.map(r => {
    const date = new Date(r.timestamp);
    const timeStr = date.toLocaleTimeString() + `.${date.getMilliseconds().toString().padStart(3, '0')}`;
    const statusPill = r.is_reachable
      ? `<span class="badge" style="background: rgba(46, 160, 67, 0.2); color: #3fb950;">ONLINE</span>`
      : `<span class="badge" style="background: rgba(248, 81, 73, 0.2); color: #f85149;">OFFLINE</span>`;
    const latency = r.latency_ms !== null ? `${r.latency_ms.toFixed(2)} мс` : '<span style="color: #f85149;">--</span>';
    const ttl = r.ttl !== null && r.ttl !== undefined ? r.ttl : '--';
    const error = r.error_msg ? `<span style="color: #f85149;">${escapeHtml(r.error_msg)}</span>` : '<span class="text-muted">ОК</span>';

    return `
      <tr>
        <td>${timeStr}</td>
        <td>${statusPill}</td>
        <td><strong>${latency}</strong></td>
        <td>${ttl}</td>
        <td>${error}</td>
      </tr>
    `;
  }).join('');
}

// Button Handlers
function exportCurrentHost() {
  if (!selectedHostId) {
    alert('Пожалуйста, выберите хост для выгрузки статистики.');
    return;
  }
  window.location.href = `/api/hosts/${selectedHostId}/export/csv`;
}

btnExportSingle.addEventListener('click', exportCurrentHost);
if (btnExportActive) {
  btnExportActive.addEventListener('click', exportCurrentHost);
}

btnToggleActive.addEventListener('click', async () => {
  if (!selectedHostId) return;
  const host = hosts.find(h => h.id === selectedHostId);
  if (!host) return;

  try {
    await fetch(`/api/hosts/${selectedHostId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_active: !host.is_active })
    });
    await fetchHosts();
  } catch (e) {
    alert('Не удалось изменить статус: ' + e.message);
  }
});

btnDeleteHost.addEventListener('click', async () => {
  if (!selectedHostId) return;
  const host = hosts.find(h => h.id === selectedHostId);
  if (!host) return;

  if (!confirm(`Вы действительно хотите удалить хост "${host.name}" (${host.target}) и всю его историю?`)) {
    return;
  }

  try {
    await fetch(`/api/hosts/${selectedHostId}`, { method: 'DELETE' });
    selectedHostId = null;
    await fetchHosts();
  } catch (e) {
    alert('Ошибка при удалении: ' + e.message);
  }
});

// Modal Handlers
btnAddHost.addEventListener('click', () => {
  modalError.style.display = 'none';
  formAddHost.reset();
  modalAddHost.style.display = 'flex';
});

function closeModal() {
  modalAddHost.style.display = 'none';
}

btnCloseModal.addEventListener('click', closeModal);
btnCancelModal.addEventListener('click', closeModal);
modalAddHost.addEventListener('click', (e) => {
  if (e.target === modalAddHost) closeModal();
});

formAddHost.addEventListener('submit', async (e) => {
  e.preventDefault();
  modalError.style.display = 'none';

  const name = document.getElementById('input-host-name').value.trim();
  const target = document.getElementById('input-host-target').value.trim();
  const interval_sec = parseFloat(document.getElementById('input-host-interval').value);

  if (!name || !target) {
    showModalError('Заполните название и адрес хоста');
    return;
  }

  try {
    const res = await fetch('/api/hosts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, target, interval_sec })
    });

    const data = await res.json();
    if (!res.ok) {
      showModalError(data.detail || 'Не удалось добавить хост');
      return;
    }

    closeModal();
    selectedHostId = data.id;
    await fetchHosts();
  } catch (err) {
    showModalError(err.message || 'Сетевая ошибка');
  }
});

function showModalError(msg) {
  modalError.textContent = msg;
  modalError.style.display = 'block';
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// Traceroute Elements
const btnTraceroute = document.getElementById('btn-traceroute');
const tracerouteCard = document.getElementById('traceroute-card');
const tracerouteTargetBadge = document.getElementById('traceroute-target-badge');
const btnCloseTraceroute = document.getElementById('btn-close-traceroute');
const btnRerunTraceroute = document.getElementById('btn-rerun-traceroute');
const tracerouteLoading = document.getElementById('traceroute-loading');
const tracerouteChain = document.getElementById('traceroute-chain');
const tracerouteTableBody = document.getElementById('traceroute-table-body');

async function executeTraceroute() {
  if (!selectedHostId) return;
  const host = hosts.find(h => h.id === selectedHostId);
  if (!host) return;

  tracerouteCard.style.display = 'block';
  tracerouteTargetBadge.textContent = `${host.name} (${host.target})`;
  tracerouteLoading.style.display = 'flex';
  tracerouteChain.innerHTML = '';
  tracerouteTableBody.innerHTML = '';

  try {
    const res = await fetch(`/api/hosts/${selectedHostId}/traceroute`);
    if (!res.ok) throw new Error('Ошибка запуска трассировки');
    const data = await res.json();
    renderTraceroute(data.hops, host.target);
  } catch (err) {
    tracerouteChain.innerHTML = `<div class="text-muted" style="color: #f85149;">Ошибка: ${escapeHtml(err.message)}</div>`;
  } finally {
    tracerouteLoading.style.display = 'none';
  }
}

function renderTraceroute(hops, target) {
  tracerouteChain.innerHTML = '';
  tracerouteTableBody.innerHTML = '';

  if (!hops || hops.length === 0) {
    tracerouteChain.innerHTML = '<div class="text-muted">Нет доступных данных трассировки</div>';
    return;
  }

  // Local device hop
  const localNode = document.createElement('div');
  localNode.className = 'hop-node';
  localNode.innerHTML = `
    <span class="hop-badge-num">СТАРТ</span>
    <span class="hop-node-icon">💻</span>
    <span class="hop-name">Ваш Mac</span>
    <span class="hop-ip">localhost</span>
    <span class="hop-latency-pill fast">0 ms</span>
  `;
  tracerouteChain.appendChild(localNode);

  hops.forEach((hop, idx) => {
    // Connector arrow
    const arrow = document.createElement('div');
    arrow.className = 'hop-connector';
    arrow.innerHTML = '➔';
    tracerouteChain.appendChild(arrow);

    const isLast = (idx === hops.length - 1) || (hop.ip === target);
    const isTimeout = hop.status === 'timeout';
    
    let latClass = 'fast';
    let latText = '--';
    if (!isTimeout && hop.avg_ms !== null) {
      latText = `${hop.avg_ms} ms`;
      latClass = hop.avg_ms < 40 ? 'fast' : hop.avg_ms < 90 ? 'medium' : 'slow';
    } else {
      latText = '* * *';
      latClass = 'loss';
    }

    const icon = isLast ? '🎯' : isTimeout ? '🛡️' : '🌐';

    const node = document.createElement('div');
    node.className = `hop-node ${isLast ? 'endpoint' : ''} ${isTimeout ? 'timeout' : ''}`;
    node.innerHTML = `
      <span class="hop-badge-num">ХОП ${hop.hop}</span>
      <span class="hop-node-icon">${icon}</span>
      <span class="hop-name" title="${escapeHtml(hop.host)}">${escapeHtml(hop.host)}</span>
      <span class="hop-ip">${escapeHtml(hop.ip)}</span>
      <span class="hop-latency-pill ${latClass}">${latText}</span>
    `;
    tracerouteChain.appendChild(node);

    // Add row in table
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><strong>#${hop.hop}</strong></td>
      <td>${escapeHtml(hop.host)}</td>
      <td><code>${escapeHtml(hop.ip)}</code></td>
      <td><strong>${latText}</strong></td>
      <td>${isTimeout 
        ? '<span class="badge" style="background: rgba(248,81,73,0.15); color: #f85149;">ТАЙМАУТ</span>' 
        : '<span class="badge" style="background: rgba(46,160,67,0.15); color: #3fb950;">ДОСТУПЕН</span>'}</td>
    `;
    tracerouteTableBody.appendChild(tr);
  });
}

if (btnTraceroute) {
  btnTraceroute.addEventListener('click', executeTraceroute);
}
if (btnRerunTraceroute) {
  btnRerunTraceroute.addEventListener('click', executeTraceroute);
}
if (btnCloseTraceroute) {
  btnCloseTraceroute.addEventListener('click', () => {
    tracerouteCard.style.display = 'none';
  });
}

// Window init
window.addEventListener('DOMContentLoaded', () => {
  initChart();
  fetchHosts();
  // Poll every 1.5s for real-time dashboard updates
  pollTimer = setInterval(fetchHosts, 1500);
});
