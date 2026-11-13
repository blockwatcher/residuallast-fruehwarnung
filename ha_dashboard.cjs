const WebSocket = require('ws');
const base = process.env.HASS_URL.replace(/^http/, 'ws').replace(/\/$/, '');
// Token aus HASS_TOKEN, sonst aus der Datei, auf die HASS_TOKEN_FILE zeigt.
const token =
  process.env.HASS_TOKEN ||
  (process.env.HASS_TOKEN_FILE
    ? require('fs').readFileSync(process.env.HASS_TOKEN_FILE, 'utf8').trim()
    : undefined);
const ws = new WebSocket(base + '/api/websocket');

let id = 1;
const pending = {};
function send(msg) { const mid = id++; msg.id = mid; return new Promise((res, rej) => { pending[mid] = { res, rej }; ws.send(JSON.stringify(msg)); }); }

const cards = [
  { type: 'markdown', content:
    "## ⚡ Strompreis-Frühwarnung\n\n**Status:** {% set b = states('sensor.dunkelflaute_status') %}{{ {'günstig':'🟢 günstig','normal':'⚪ normal','erhöht':'🟠 erhöht','hoch':'🔴 hoch'}.get(b, b) }}\n\n{{ state_attr('sensor.dunkelflaute_status','recommendation') }}" },
  { type: 'markdown', content:
    "### 5-Tage-Vorschau (Residuallast = Preistreiber)\n\n| Tag | Peak-Residuallast | Ø Erneuerbar | Band |\n|:--|--:|--:|:--|\n{% for r in state_attr('sensor.dunkelflaute_status','prognose') -%}\n| {{ r.day }} | {{ r.peak_resid }} GW | {{ r.avg_share }} % | {{ {'günstig':'🟢','normal':'⚪','erhöht':'🟠','hoch':'🔴'}.get(r.band,'') }} {{ r.band }} |\n{% endfor %}\n\n_Kalibrierung k={{ state_attr('sensor.dunkelflaute_status','kalibrierung_k') }} · energy-charts Ø-Anteil {{ state_attr('sensor.dunkelflaute_status','energy_charts_ren_share_avg') }} %_" },
  { type: 'gauge', entity: 'sensor.residuallast_prognose_max', name: 'Peak-Residuallast (5 Tage)', unit: 'GW', min: 20, max: 70, needle: true, severity: { green: 20, yellow: 48, red: 55 } },
];

const dashConfig = { title: 'Energie-Warnung', views: [ { title: 'Strompreis-Warnung', path: 'warnung', icon: 'mdi:flash-alert', cards: [ { type: 'vertical-stack', cards } ] } ] };

ws.on('message', async (data) => {
  const m = JSON.parse(data);
  if (m.type === 'auth_required') { ws.send(JSON.stringify({ type: 'auth', access_token: token })); return; }
  if (m.type === 'auth_invalid') { console.error('AUTH_INVALID'); process.exit(2); }
  if (m.type === 'auth_ok') {
    try {
      const list = await send({ type: 'lovelace/dashboards/list' });
      let dash = (list.result || []).find(d => d.url_path === 'energie-warnung');
      if (!dash) {
        const cr = await send({ type: 'lovelace/dashboards/create', url_path: 'energie-warnung', title: 'Energie-Warnung', mode: 'storage', require_admin: false, show_in_sidebar: true, icon: 'mdi:flash-alert' });
        if (!cr.success) throw new Error('create failed: ' + JSON.stringify(cr.error));
        dash = cr.result; console.log('DASHBOARD_CREATED');
      } else { console.log('DASHBOARD_EXISTS'); }
      const sv = await send({ type: 'lovelace/config/save', url_path: 'energie-warnung', config: dashConfig });
      if (!sv.success) throw new Error('save failed: ' + JSON.stringify(sv.error));
      console.log('CONFIG_SAVED');
      process.exit(0);
    } catch (e) { console.error('ERR', e.message); process.exit(1); }
    return;
  }
  if (m.id && pending[m.id]) { pending[m.id].res(m); delete pending[m.id]; }
});
ws.on('error', (e) => { console.error('WS_ERROR', e.message); process.exit(3); });
setTimeout(() => { console.error('TIMEOUT'); process.exit(4); }, 25000);
