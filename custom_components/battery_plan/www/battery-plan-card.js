// battery-plan-card — Battery Plan integration card.
// Actual SOC (history) + forecast SOC with charge/export bands, and a planned cost table.
// Config: entity (the "SOC at next charge" sensor), soc_entity (optional, defaults to the planner's),
//         hours_past (12), height (240), show_costs (true), title.
const BP_COLORS = { charge: '#3b82f6', iog: '#14b8a6', export: '#f59e0b', saving: '#eab308', grid: '#ef4444' };
const BP_NAMES = { charge: 'Charge', iog: 'IOG top-up', export: 'Export', saving: 'Saving session', grid: 'Battery at min', hold: 'Hold', self: 'House on battery' };

class BatteryPlanCard extends HTMLElement {
  static getConfigForm() {
    return {
      schema: [
        { name: 'entity', required: true, selector: { entity: { domain: 'sensor', integration: 'battery_plan' } } },
        { name: 'soc_entity', selector: { entity: { domain: 'sensor', device_class: 'battery' } } },
        { name: 'action_entity', selector: { entity: { domain: 'sensor', integration: 'battery_plan' } } },
        { name: 'title', selector: { text: {} } },
        { type: 'grid', name: '', schema: [
          { name: 'hours_past', selector: { number: { min: 0, max: 48, mode: 'box', unit_of_measurement: 'h' } } },
          { name: 'height', selector: { number: { min: 120, max: 600, mode: 'box', unit_of_measurement: 'px' } } },
        ] },
        { name: 'show_costs', selector: { boolean: {} } },
      ],
      computeLabel: s => ({ entity: 'Battery Plan sensor (SOC at next charge)', soc_entity: 'Actual SOC sensor (optional)', action_entity: 'Battery action sensor (optional, for past bands)', title: 'Title',
        hours_past: 'History shown', height: 'Chart height', show_costs: 'Show planned costs' }[s.name]),
    };
  }

  static getStubConfig(hass) {
    const id = Object.keys(hass.states).find(e => e.startsWith('sensor.') && hass.states[e].attributes.forecast && hass.states[e].attributes.days);
    return { entity: id || '', hours_past: 12, show_costs: true };
  }

  setConfig(config) {
    if (!config.entity) throw new Error('Pick the Battery Plan "SOC at next charge" sensor');
    this._config = Object.assign({ hours_past: 12, height: 240, show_costs: true }, config);
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._hist = []; this._histAt = 0; this._key = null;
    if (this._hass) this._render();
  }

  getCardSize() { return this._config && this._config.show_costs ? 7 : 5; }
  getGridOptions() { return { columns: 'full', min_columns: 6, rows: 'auto' }; }

  _socEntity() {
    const st = this._hass && this._hass.states[this._config.entity];
    return this._config.soc_entity || (st && st.attributes.soc_entity);
  }

  _actionEntity() {
    const st = this._hass && this._hass.states[this._config.entity];
    return this._config.action_entity || (st && st.attributes.action_entity);
  }

  set hass(hass) {
    this._hass = hass;
    const st = hass.states[this._config.entity];
    const key = st ? st.last_updated : '';
    const now = Date.now();
    if ((this._socEntity() || this._actionEntity()) && now - this._histAt > 300000) { this._histAt = now; this._loadHistory(); }
    if (key !== this._key) { this._key = key; this._render(); }
  }

  async _loadHistory() {
    const soc = this._socEntity(), act = this._actionEntity();
    const ids = [soc, act].filter(Boolean);
    if (!ids.length || !this._config.hours_past) return;
    try {
      const r = await this._hass.callWS({
        type: 'history/history_during_period',
        start_time: new Date(Date.now() - this._config.hours_past * 3600000).toISOString(),
        entity_ids: ids, minimal_response: true, no_attributes: true,
      });
      this._hist = soc ? ((r && r[soc]) || []).map(x => [(x.lu || x.lc) * 1000, parseFloat(x.s)]).filter(x => !isNaN(x[1])) : [];
      // past actions: [start, end, mode]
      const rows = act ? ((r && r[act]) || []).map(x => [(x.lu || x.lc) * 1000, x.s]) : [];
      this._past = rows.map((x, i) => [x[0], i + 1 < rows.length ? rows[i + 1][0] : Date.now(), x[1]]);
      this._render();
    } catch (e) { /* history is optional */ }
  }

  _fmtTime(t, withDay) {
    const o = { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' };
    if (withDay) o.weekday = 'short';
    return new Date(t).toLocaleString(this._hass.locale ? this._hass.locale.language : undefined, o);
  }

  _money(v, cur) {
    try { return new Intl.NumberFormat(undefined, { style: 'currency', currency: cur || 'GBP' }).format(v); }
    catch (e) { return v.toFixed(2); }
  }

  _costTable(a) {
    const days = a.days || {};
    const keys = Object.keys(days).sort();
    if (!keys.length) return '';
    const cur = (this._hass.config && this._hass.config.currency) || 'GBP';
    const sc = a.standing_charge || 0;
    const todayKey = new Date().toLocaleDateString('en-CA');
    const rows = keys.map(k => {
      const d = days[k];
      const net = d.export_value - d.import_cost - sc;
      const label = k === todayKey ? 'Today (rest)' : new Date(k + 'T12:00:00').toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })
        + (d.hours != null && d.hours < 23 ? ` <small class="part">to ${String(Math.round(d.hours)).padStart(2, '0')}:00</small>` : '');
      return `<tr><td>${label}${d.priced === false ? ' <span title="Some import is unpriced (rates not published yet)">*</span>' : ''}</td>
        <td>${this._money(d.import_cost, cur)}<small>${d.import_kwh.toFixed(1)} kWh</small></td>
        <td>${this._money(d.export_value, cur)}<small>${d.export_kwh.toFixed(1)} kWh</small></td>
        <td class="${net >= 0 ? 'pos' : 'neg'}">${net >= 0 ? '+' : ''}${this._money(net, cur)}</td></tr>`;
    }).join('');
    return `<table class="cost"><thead><tr><th>Planned</th><th>Import</th><th>Export</th><th>Net${sc ? ' <small>inc. standing</small>' : ''}</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  _render() {
    if (!this.shadowRoot || !this._hass || !this._config) return;
    const c = this._config, st = this._hass.states[c.entity];
    const a = (st && st.attributes) || {};
    const pts = (a.forecast || []).map(f => ({ ...f, t: Date.parse(f.t) }));
    const W = 1000, H = c.height, L = 34, R = 10, T = 10, B = 24;
    const now = Date.now();
    const t0 = now - (c.hours_past || 0) * 3600000;
    const t1 = pts.length ? pts[pts.length - 1].t : now + 24 * 3600000;
    const x = t => L + (t - t0) / (t1 - t0) * (W - L - R);
    const y = v => T + (100 - v) / 100 * (H - T - B);

    let bands = '';
    (this._past || []).forEach(([s, e, m]) => {
      if (!BP_COLORS[m] || e <= t0) return;
      const a0 = Math.max(s, t0), a1 = Math.min(e, now);
      if (a1 > a0) bands += `<rect x="${x(a0)}" y="${T}" width="${x(a1) - x(a0)}" height="${H - T - B}" fill="${BP_COLORS[m]}" opacity="0.16"/>`;
    });
    pts.forEach((p, i) => {
      if (!BP_COLORS[p.mode]) return;
      const s = i === 0 ? now : pts[i - 1].t;
      bands += `<rect x="${x(s)}" y="${T}" width="${Math.max(0, x(p.t) - x(s))}" height="${H - T - B}" fill="${BP_COLORS[p.mode]}" opacity="0.16"/>`;
    });
    let grid = '';
    for (const v of [0, 25, 50, 75, 100]) {
      grid += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" class="g"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end" class="lbl">${v}</text>`;
    }
    const step = 3 * 3600000;
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
      const d = new Date(t), hh = d.getHours();
      const txt = hh === 0 ? d.toLocaleDateString(undefined, { weekday: 'short' }) : String(hh).padStart(2, '0') + ':00';
      grid += `<line x1="${x(t)}" x2="${x(t)}" y1="${T}" y2="${H - B}" class="g"/><text x="${x(t)}" y="${H - 6}" text-anchor="middle" class="lbl${hh === 0 ? ' b' : ''}">${txt}</text>`;
    }
    const res = a.reserve_soc;
    const reserve = res != null ? `<line x1="${L}" x2="${W - R}" y1="${y(res)}" y2="${y(res)}" class="res"/><text x="${W - R - 4}" y="${y(res) - 4}" text-anchor="end" class="lbl">floor ${res}%</text>` : '';

    const hist = this._hist.filter(h => h[0] >= t0);
    const socNow = parseFloat(a.soc_now);
    const histPath = hist.length ? 'M' + hist.map(h => `${x(h[0]).toFixed(1)},${y(h[1]).toFixed(1)}`).join('L') + (isNaN(socNow) ? '' : `L${x(now)},${y(socNow)}`) : '';
    const fPts = (isNaN(socNow) ? [] : [[now, socNow]]).concat(pts.map(p => [p.t, p.soc]));
    const fPath = fPts.length ? 'M' + fPts.map(p => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join('L') : '';
    const area = fPts.length ? fPath + `L${x(fPts[fPts.length - 1][0])},${y(0)}L${x(fPts[0][0])},${y(0)}Z` : '';
    const legend = Object.keys(BP_COLORS).filter(m => pts.some(p => p.mode === m) || (this._past || []).some(q => q[2] === m && q[1] > t0))
      .map(m => `<span><i style="background:${BP_COLORS[m]}"></i>${BP_NAMES[m]}</span>`).join('');

    const sub = !st ? `Entity ${c.entity} not found` : (a.soc_at_next_charge != null ? `${a.soc_at_next_charge}% at next charge · low ${a.min_soc_ahead}%` : 'waiting for plan');

    this.shadowRoot.innerHTML = `
      <style>
        ha-card { padding: 12px 12px 8px; }
        .hdr { display:flex; justify-content:space-between; align-items:baseline; gap:8px; flex-wrap:wrap; }
        .ttl { font-size: 16px; font-weight: 500; }
        .sub { font-size: 12px; color: var(--secondary-text-color); }
        svg { width: 100%; height: auto; display:block; margin-top: 6px; touch-action: pan-y; }
        .g { stroke: var(--divider-color, #8883); stroke-width: 1; }
        .lbl { fill: var(--secondary-text-color); font-size: 11px; }
        .lbl.b { font-weight: 600; fill: var(--primary-text-color); }
        .res { stroke: var(--error-color, #ef4444); stroke-dasharray: 6 5; stroke-width: 1.2; opacity: .7; }
        .now { stroke: var(--primary-text-color); stroke-width: 1; opacity: .5; }
        .hist { fill:none; stroke: var(--primary-text-color, #444); stroke-width: 2; opacity: .55; }
        .fc { fill:none; stroke: var(--primary-color, #03a9f4); stroke-width: 2.5; stroke-linejoin: round; }
        .area { fill: var(--primary-color, #03a9f4); opacity: .08; }
        .leg { display:flex; flex-wrap:wrap; gap: 4px 14px; font-size: 12px; color: var(--secondary-text-color); margin-top: 4px; }
        .leg i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:5px; opacity:.6; vertical-align:-1px; }
        .tip { position:absolute; pointer-events:none; background: var(--card-background-color, #fff); border:1px solid var(--divider-color);
               border-radius:6px; padding:4px 8px; font-size:12px; white-space:nowrap; display:none; box-shadow: 0 2px 6px #0003; z-index: 1; }
        .wrap { position: relative; }
        table.cost { width:100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
        table.cost th { text-align:right; font-weight:500; color: var(--secondary-text-color); padding: 4px 6px; border-bottom: 1px solid var(--divider-color); }
        table.cost th:first-child, table.cost td:first-child { text-align:left; }
        table.cost td { text-align:right; padding: 5px 6px; vertical-align: top; }
        table.cost td small.part { display:inline; }
        table.cost td small { display:block; font-size: 11px; color: var(--secondary-text-color); }
        td.pos { color: var(--success-color, #16a34a); font-weight: 600; }
        td.neg { color: var(--error-color, #dc2626); font-weight: 600; }
      </style>
      <ha-card>
        <div class="hdr"><span class="ttl">${c.title || 'Battery plan'}</span><span class="sub">${sub}</span></div>
        <div class="wrap">
          <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
            ${bands}${grid}${reserve}
            <path d="${area}" class="area"/><path d="${histPath}" class="hist"/><path d="${fPath}" class="fc"/>
            <line x1="${x(now)}" x2="${x(now)}" y1="${T}" y2="${H - B}" class="now"/>
            <text x="${x(now) + 4}" y="${T + 11}" class="lbl">now</text>
            <line id="cur" x1="0" x2="0" y1="${T}" y2="${H - B}" class="now" style="display:none"/>
            <rect id="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/>
          </svg>
          <div class="tip" id="tip"></div>
        </div>
        <div class="leg"><span><i style="background:var(--primary-text-color,#444)"></i>Actual</span><span><i style="background:var(--primary-color,#03a9f4);opacity:1"></i>Forecast</span>${legend}</div>
        ${c.show_costs ? this._costTable(a) : ''}
      </ha-card>`;

    const svg = this.shadowRoot.querySelector('svg'), tip = this.shadowRoot.getElementById('tip'),
      cur = this.shadowRoot.getElementById('cur'), hit = this.shadowRoot.getElementById('hit');
    const move = ev => {
      const r = svg.getBoundingClientRect();
      const px = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
      const t = t0 + ((px / r.width * W) - L) / (W - L - R) * (t1 - t0);
      let html;
      if (t >= now && pts.length) {
        const p = pts.find(q => q.t >= t) || pts[pts.length - 1];
        html = `<b>${this._fmtTime(p.t, true)}</b> · ${p.soc}%<br>${BP_NAMES[p.mode] || p.mode} · ${p.kw} kW${p.rate != null ? ` · ${p.rate}p export` : ''}`;
      } else if (hist.length || (this._past || []).length) {
        let h = hist[0]; for (const q of hist) { if (q[0] <= t) h = q; else break; }
        const pa = (this._past || []).find(q => q[0] <= t && t < q[1]);
        html = `<b>${this._fmtTime(t, true)}</b>${h ? ` · ${h[1].toFixed(1)}% actual` : ''}${pa ? `<br>${BP_NAMES[pa[2]] || pa[2]}` : ''}`;
      } else return;
      tip.innerHTML = html; tip.style.display = 'block';
      tip.style.left = Math.min(Math.max(0, px - tip.offsetWidth / 2), r.width - tip.offsetWidth) + 'px';
      tip.style.top = '-6px';
      const sx = px / r.width * W; cur.setAttribute('x1', sx); cur.setAttribute('x2', sx); cur.style.display = '';
    };
    const leave = () => { tip.style.display = 'none'; cur.style.display = 'none'; };
    hit.addEventListener('mousemove', move); hit.addEventListener('touchmove', move, { passive: true });
    hit.addEventListener('mouseleave', leave); hit.addEventListener('touchend', leave);
  }
}

if (!customElements.get('battery-plan-card')) customElements.define('battery-plan-card', BatteryPlanCard);
window.customCards = window.customCards || [];
if (!window.customCards.some(c => c.type === 'battery-plan-card')) {
  window.customCards.push({ type: 'battery-plan-card', name: 'Battery Plan', description: 'Battery SOC forecast, charge/export plan and planned costs', preview: true });
}
