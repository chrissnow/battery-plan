// battery-plan-card — Battery Plan integration card.
// Actual SOC (history) + forecast SOC with charge/export bands, and a planned cost table.
// Config: entity (the "SOC at next charge" sensor), soc_entity (optional, defaults to the planner's),
//         hours_past (12), height (240), show_costs (true), show_load (true), title.
// With the planner's weather model on, a load strip shows base load + heating/cooling and the forecast temperature.
const BP_COLORS = { charge: '#3b82f6', iog: '#14b8a6', export: '#f59e0b', saving: '#eab308', grid: '#ef4444' };
const BP_HEAT = '#f97316', BP_COOL = '#0ea5e9', BP_BASE = '#94a3b8';
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
        { name: 'show_load', selector: { boolean: {} } },
      ],
      computeLabel: s => ({ entity: 'Battery Plan sensor (SOC at next charge)', soc_entity: 'Actual SOC sensor (optional)', action_entity: 'Battery action sensor (optional, for past bands)', title: 'Title',
        hours_past: 'History shown', height: 'Chart height', show_costs: 'Show planned costs', show_load: 'Show house load strip' }[s.name]),
    };
  }

  static getStubConfig(hass) {
    const id = Object.keys(hass.states).find(e => e.startsWith('sensor.') && hass.states[e].attributes.forecast && hass.states[e].attributes.days);
    return { entity: id || '', hours_past: 12, show_costs: true };
  }

  setConfig(config) {
    if (!config.entity) throw new Error('Pick the Battery Plan "SOC at next charge" sensor');
    this._config = Object.assign({ hours_past: 12, height: 240, show_costs: true, show_load: true }, config);
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._hist = []; this._histAt = 0; this._key = null;
    if (this._hass) this._render();
  }

  connectedCallback() {
    // draw at the card's real width so text and the load strip stay readable on phones
    if (!this._ro && window.ResizeObserver) {
      this._ro = new ResizeObserver(entries => {
        const w = Math.round(entries[0].contentRect.width);
        if (w > 0 && Math.abs(w - (this._w || 0)) > 8) { this._w = w; this._render(); }
      });
    }
    if (this._ro) this._ro.observe(this);
  }

  disconnectedCallback() { if (this._ro) this._ro.disconnect(); }

  getCardSize() { return (this._config && this._config.show_costs ? 7 : 5) + (this._config && this._config.show_load ? 2 : 0); }
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
    this._loadActual();
  }

  // Actual house load per half hour (load sensors minus excluded), from 5-minute statistics.
  async _loadActual() {
    const st = this._hass.states[this._config.entity];
    const a = (st && st.attributes) || {};
    const inc = a.load_entities || [], exc = a.load_exclude || [];
    if (!this._config.show_load || !inc.length || !this._config.hours_past) return;
    try {
      const r = await this._hass.callWS({
        type: 'recorder/statistics_during_period',
        start_time: new Date(Date.now() - this._config.hours_past * 3600000).toISOString(),
        statistic_ids: inc.concat(exc), period: '5minute', types: ['mean'], units: { power: 'kW' },
      });
      const half = id => {
        const m = new Map();
        ((r && r[id]) || []).forEach(row => {
          const t = typeof row.start === 'number' ? row.start : Date.parse(row.start);
          const k = Math.floor(t / 1800000) * 1800000;
          const v = m.get(k) || [0, 0]; v[0] += Math.max(0, row.mean || 0); v[1] += 1; m.set(k, v);
        });
        return m;
      };
      const incM = inc.map(half), excM = exc.map(half);
      const keys = [...new Set(incM.flatMap(m => [...m.keys()]))].sort((p, q) => p - q);
      this._loadHist = keys.filter(k => incM.every(m => m.has(k))).map(k => {
        let v = incM.reduce((s, m) => s + m.get(k)[0] / m.get(k)[1], 0);
        excM.forEach(m => { if (m.has(k)) v -= m.get(k)[0] / m.get(k)[1]; });
        return [k, Math.max(0, v)];
      });
      this._render();
    } catch (e) { /* optional */ }
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
    const act = a.actual_today;
    const rows = keys.map(k => {
      const p = days[k];
      // today: what has happened so far (actual) plus the plan for the rest of the day
      const whole = k === todayKey && act;
      const d = whole ? {
        ...p,
        import_cost: p.import_cost + act.import_cost, import_kwh: p.import_kwh + act.import_kwh,
        export_value: p.export_value + act.export_value, export_kwh: p.export_kwh + act.export_kwh,
      } : p;
      const net = d.export_value - d.import_cost - sc;
      const split = (x, y) => whole ? ` title="${this._money(x, cur)} so far + ${this._money(y, cur)} planned"` : '';
      const label = k === todayKey ? (whole ? 'Today <small class="part">actual + plan</small>' : 'Today (rest)')
        : new Date(k + 'T12:00:00').toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })
          + (p.hours != null && p.hours < 23 ? ` <small class="part">to ${String(Math.round(p.hours)).padStart(2, '0')}:00</small>` : '');
      const wx = p.weather_kwh > 0.05 ? `<small class="wx">incl. ${p.weather_kwh.toFixed(1)} kWh ${a.weather_model && a.weather_model.cooling_kw_per_c && !a.weather_model.heating_kw_per_c ? 'cooling' : 'heating'}${whole ? ' to come' : ''}</small>` : '';
      // import past the published rates isn't priced: show how much, and no net for the day
      const un = p.unpriced_kwh || 0;
      const allUn = un > 0 && un >= d.import_kwh - 0.05;
      const impCost = allUn ? '—' : this._money(d.import_cost, cur) + (un > 0 ? ' +' : '');
      const impKwh = `${d.import_kwh.toFixed(1)} kWh${un > 0 && !allUn ? `, ${un.toFixed(1)} unpriced` : ''}`;
      const netCell = un > 0 ? '<td title="Some import is unpriced (rates not published yet)">—</td>'
        : `<td class="${net >= 0 ? 'pos' : 'neg'}">${net >= 0 ? '+' : ''}${this._money(net, cur)}</td>`;
      return `<tr><td>${label}${un > 0 ? ' <span title="Rates not published yet">*</span>' : ''}${wx}</td>
        <td${split(act && act.import_cost, p.import_cost)}>${impCost}<small>${impKwh}</small></td>
        <td${split(act && act.export_value, p.export_value)}>${this._money(d.export_value, cur)}<small>${d.export_kwh.toFixed(1)} kWh</small></td>
        ${netCell}</tr>`;
    }).join('');
    return `<table class="cost"><thead><tr><th>${act ? 'Day' : 'Planned'}</th><th>Import</th><th>Export</th><th>Net${sc ? ' <small>inc. standing</small>' : ''}</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  _render() {
    if (!this.shadowRoot || !this._hass || !this._config) return;
    const c = this._config, st = this._hass.states[c.entity];
    const a = (st && st.attributes) || {};
    const pts = (a.forecast || []).map(f => ({ ...f, t: Date.parse(f.t) }));
    const W = Math.max(280, (this._w || 1000) - 24), H = c.height, L = 34, R = 10, T = 10, B = 24;
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
    const step = (W < 520 ? 6 : 3) * 3600000;
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
      const d = new Date(t), hh = d.getHours();
      const txt = hh === 0 ? d.toLocaleDateString(undefined, { weekday: 'short' }) : String(hh).padStart(2, '0') + ':00';
      grid += `<line x1="${x(t)}" x2="${x(t)}" y1="${T}" y2="${H - B}" class="g"/><text x="${x(t)}" y="${H - 6}" text-anchor="middle" class="lbl${hh === 0 ? ' b' : ''}">${txt}</text>`;
    }
    const res = a.reserve_soc;
    let reserve = '';
    if (pts.length && pts[0].fl != null) {
      // stepped floor (day/night) along the forecast, and back over the history: the floor repeats
      // daily, so a past half hour takes the forecast's value at the same time of day
      const HH = 1800000, tod = t => { const dt = new Date(t); return dt.getHours() * 60 + dt.getMinutes(); };
      const byTod = {};
      pts.forEach((p, i) => { const s = i === 0 ? Math.floor(now / HH) * HH : pts[i - 1].t; if (!(tod(s) in byTod)) byTod[tod(s)] = p.fl; });
      let d = '';
      const p0 = Math.floor(t0 / HH) * HH;
      for (let s = p0; s < now; s += HH) {
        const f = byTod[tod(Math.floor(s / HH) * HH)];
        if (f == null) continue;
        const a0 = Math.max(s, t0), a1 = Math.min(s + HH, now);
        d += `${d ? 'L' : 'M'}${x(a0)},${y(f)}L${x(a1)},${y(f)}`;
      }
      d += `${d ? 'L' : 'M'}${x(now)},${y(pts[0].fl)}`;
      pts.forEach((p, i) => { const s = i === 0 ? now : pts[i - 1].t; d += `L${x(s)},${y(p.fl)}L${x(p.t)},${y(p.fl)}`; });
      const lf = pts[pts.length - 1].fl;
      reserve = `<path d="${d}" class="res" fill="none"/><text x="${W - R - 4}" y="${y(lf) - 4}" text-anchor="end" class="lbl">exports stop at ${lf}%</text>`;
    } else if (res != null) {
      reserve = `<line x1="${L}" x2="${W - R}" y1="${y(res)}" y2="${y(res)}" class="res"/><text x="${W - R - 4}" y="${y(res) - 4}" text-anchor="end" class="lbl">exports stop at ${res}%</text>`;
    }
    // the house may use the battery below the export floor, down to the minimum SOC
    const minSoc = parseFloat(a.min_soc);
    const floors = pts.filter(p => p.fl != null).map(p => p.fl).concat(res != null ? [res] : []);
    if (!isNaN(minSoc) && minSoc > 0 && (!floors.length || minSoc < Math.min(...floors))) {
      reserve += `<line x1="${L}" x2="${W - R}" y1="${y(minSoc)}" y2="${y(minSoc)}" class="min"/><text x="${W - R - 4}" y="${y(minSoc) + 12}" text-anchor="end" class="lbl">min ${minSoc}%</text>`;
    }

    const hist = this._hist.filter(h => h[0] >= t0);
    const socNow = parseFloat(a.soc_now);
    const histPath = hist.length ? 'M' + hist.map(h => `${x(h[0]).toFixed(1)},${y(h[1]).toFixed(1)}`).join('L') + (isNaN(socNow) ? '' : `L${x(now)},${y(socNow)}`) : '';
    const fPts = (isNaN(socNow) ? [] : [[now, socNow]]).concat(pts.map(p => [p.t, p.soc]));
    const fPath = fPts.length ? 'M' + fPts.map(p => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join('L') : '';
    const area = fPts.length ? fPath + `L${x(fPts[fPts.length - 1][0])},${y(0)}L${x(fPts[0][0])},${y(0)}Z` : '';
    const legend = Object.keys(BP_COLORS).filter(m => pts.some(p => p.mode === m) || (this._past || []).some(q => q[2] === m && q[1] > t0))
      .map(m => `<span><i style="background:${BP_COLORS[m]}"></i>${BP_NAMES[m]}</span>`).join('');

    // ---- house load strip: base + heating/cooling bars, forecast temperature, actual load ----
    const wm = a.weather_model || null, wmOn = !!(wm && wm.status === 'active');
    const showLoad = c.show_load && pts.some(p => p.ld != null);
    const LH = 96, LT = 6, LB = 8;
    let strip = '';
    if (showLoad) {
      const lh = (this._loadHist || []).filter(h => h[0] + 1800000 > t0 && h[0] < now);
      const top = Math.max(1, ...pts.map(p => p.ld || 0), ...lh.map(h => h[1]));
      const nice = top <= 2 ? 2 : top <= 4 ? 4 : top <= 6 ? 6 : Math.ceil(top / 5) * 5;
      const ly = v => LT + (1 - v / nice) * (LH - LT - LB);
      let bars = '';
      pts.forEach((p, i) => {
        const s0 = i === 0 ? now : pts[i - 1].t, x0 = x(s0) + 0.5, w = Math.max(0, x(p.t) - x(s0) - 1);
        const wh = p.wh || 0, wc = p.wc || 0, base = Math.max(0, (p.ld || 0) - wh - wc);
        bars += `<rect x="${x0}" y="${ly(base)}" width="${w}" height="${ly(0) - ly(base)}" fill="${BP_BASE}" opacity=".45"/>`;
        if (wh > 0.01) bars += `<rect x="${x0}" y="${ly(base + wh)}" width="${w}" height="${ly(base) - ly(base + wh)}" fill="${BP_HEAT}" opacity=".75"/>`;
        if (wc > 0.01) bars += `<rect x="${x0}" y="${ly(base + wh + wc)}" width="${w}" height="${ly(base + wh) - ly(base + wh + wc)}" fill="${BP_COOL}" opacity=".75"/>`;
      });
      let actual = '';
      if (lh.length) {
        actual = 'M' + lh.map(h => `${x(Math.max(h[0], t0)).toFixed(1)},${ly(h[1]).toFixed(1)}L${x(Math.min(h[0] + 1800000, now)).toFixed(1)},${ly(h[1]).toFixed(1)}`).join('L');
      }
      let temp = '', tlab = '';
      const tp = pts.filter(p => p.tf != null);
      if (tp.length > 1) {
        const tmin = Math.floor(Math.min(...tp.map(p => p.tf))) - 1, tmax = Math.ceil(Math.max(...tp.map(p => p.tf))) + 1;
        const ty = v => LT + (1 - (v - tmin) / (tmax - tmin)) * (LH - LT - LB);
        temp = 'M' + tp.map(p => `${x(p.t - 900000).toFixed(1)},${ty(p.tf).toFixed(1)}`).join('L');
        tlab = `<text x="${W - R - 2}" y="${ty(tmax - 1) + 3}" text-anchor="end" class="lbl tl">${tmax - 1}°</text><text x="${W - R - 2}" y="${ty(tmin + 1) + 3}" text-anchor="end" class="lbl tl">${tmin + 1}°</text>`;
      }
      let lgrid = '';
      for (const v of [0, nice / 2, nice]) lgrid += `<line x1="${L}" x2="${W - R}" y1="${ly(v)}" y2="${ly(v)}" class="g"/><text x="${L - 6}" y="${ly(v) + 4}" text-anchor="end" class="lbl">${v}</text>`;
      strip = `<svg class="strip" viewBox="0 0 ${W} ${LH}" preserveAspectRatio="none">
          ${lgrid}${bars}<path d="${actual}" class="act"/><path d="${temp}" class="temp"/>${tlab}
          <text x="${L + 4}" y="${LT + 10}" class="lbl">kW</text>
          <line x1="${x(now)}" x2="${x(now)}" y1="${LT}" y2="${LH - LB}" class="now"/>
          <line class="cur2 now" x1="0" x2="0" y1="${LT}" y2="${LH - LB}" style="display:none"/>
          <rect class="hit" x="${L}" y="${LT}" width="${W - L - R}" height="${LH - LT - LB}" fill="transparent"/>
        </svg>`;
    }
    const kindWord = wm && wm.cooling_kw_per_c && !wm.heating_kw_per_c ? 'Cooling' : 'Heating';
    const wxLine = wmOn && (a.weather_load_today != null || a.weather_load_tomorrow != null)
      ? `<div class="wxl"><i style="background:${kindWord === 'Cooling' ? BP_COOL : BP_HEAT}"></i>${kindWord} adds ${a.weather_load_today != null ? a.weather_load_today.toFixed(1) + ' kWh today' : ''}${a.weather_load_today != null && a.weather_load_tomorrow != null ? ' · ' : ''}${a.weather_load_tomorrow != null ? a.weather_load_tomorrow.toFixed(1) + ' kWh tomorrow' : ''}</div>` : '';
    const model = wm ? this._modelSection(wm) : '';

    // the sensor's state is the SOC at the next charge (older planners put it in an attribute)
    const atCharge = a.soc_at_next_charge != null ? a.soc_at_next_charge : parseFloat(st && st.state);
    const sub = !st ? `Entity ${c.entity} not found`
      : (!isNaN(atCharge) && a.forecast ? `${atCharge}% at next charge · low ${a.min_soc_ahead}%` : 'waiting for plan');

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { display: block; padding: 12px 12px 8px; }
        .hdr { display:flex; justify-content:space-between; align-items:baseline; gap:8px; flex-wrap:wrap; }
        .ttl { font-size: 16px; font-weight: 500; }
        .sub { font-size: 12px; color: var(--secondary-text-color); }
        svg { width: 100%; height: auto; display:block; margin-top: 6px; touch-action: pan-y; }
        .g { stroke: var(--divider-color, #8883); stroke-width: 1; }
        .lbl { fill: var(--secondary-text-color); font-size: 11px; }
        .lbl.b { font-weight: 600; fill: var(--primary-text-color); }
        .res { stroke: var(--error-color, #ef4444); stroke-dasharray: 6 5; stroke-width: 1.2; opacity: .7; }
        .min { stroke: var(--secondary-text-color, #888); stroke-dasharray: 2 4; stroke-width: 1.2; opacity: .8; }
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
        table.cost td small.wx { display:block; color: ${BP_HEAT}; }
        svg.strip { margin-top: 2px; }
        .act { fill:none; stroke: var(--primary-text-color, #444); stroke-width: 1.6; opacity: .6; }
        .temp { fill:none; stroke: ${BP_HEAT}; stroke-width: 1.5; stroke-dasharray: 4 3; opacity: .9; }
        .lbl.tl { fill: ${BP_HEAT}; }
        .wxl { font-size: 12px; color: var(--secondary-text-color); margin-top: 4px; }
        .wxl i, .leg i.sq { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:5px; vertical-align:-1px; opacity: .8; }
        details.model { margin-top: 8px; font-size: 12px; color: var(--secondary-text-color); }
        details.model summary { cursor: pointer; }
        details.model .pt { fill: var(--primary-color, #03a9f4); opacity: .7; }
        details.model .fit { fill:none; stroke: ${BP_HEAT}; stroke-width: 2; }
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
            <rect id="hit" class="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/>
          </svg>
          ${strip}
          <div class="tip" id="tip"></div>
        </div>
        <div class="leg"><span><i style="background:var(--primary-text-color,#444)"></i>Actual</span><span><i style="background:var(--primary-color,#03a9f4);opacity:1"></i>Forecast</span>${legend}${showLoad ? `<span><i class="sq" style="background:${BP_BASE}"></i>House load</span>${wmOn ? `<span><i class="sq" style="background:${kindWord === 'Cooling' ? BP_COOL : BP_HEAT}"></i>${kindWord}</span>` : ''}` : ''}</div>
        ${wxLine}
        ${c.show_costs ? this._costTable(a) : ''}
        ${model}
      </ha-card>`;

    const tip = this.shadowRoot.getElementById('tip'), cur = this.shadowRoot.getElementById('cur'),
      cur2 = this.shadowRoot.querySelector('.cur2');
    const lh = this._loadHist || [];
    const move = ev => {
      const r = ev.currentTarget.ownerSVGElement.getBoundingClientRect();
      const px = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
      const t = t0 + ((px / r.width * W) - L) / (W - L - R) * (t1 - t0);
      let html;
      if (t >= now && pts.length) {
        const p = pts.find(q => q.t >= t) || pts[pts.length - 1];
        html = `<b>${this._fmtTime(p.t, true)}</b> · ${p.soc}%<br>${BP_NAMES[p.mode] || p.mode} · ${p.kw} kW${p.rate != null ? ` · ${p.rate}p export` : ''}`;
        if (showLoad && p.ld != null) {
          const wx = (p.wh || 0) + (p.wc || 0);
          html += `<br>Load ${p.ld.toFixed(1)} kW${wx > 0.01 ? ` (${Math.max(0, p.ld - wx).toFixed(1)} base + ${wx.toFixed(1)} ${p.wc ? 'cooling' : 'heating'})` : ''}${p.tf != null ? ` · ${p.tf}°` : ''}`;
        }
      } else if (hist.length || (this._past || []).length) {
        let h = hist[0]; for (const q of hist) { if (q[0] <= t) h = q; else break; }
        const pa = (this._past || []).find(q => q[0] <= t && t < q[1]);
        html = `<b>${this._fmtTime(t, true)}</b>${h ? ` · ${h[1].toFixed(1)}% actual` : ''}${pa ? `<br>${BP_NAMES[pa[2]] || pa[2]}` : ''}`;
        const la = lh.find(q => q[0] <= t && t < q[0] + 1800000);
        if (showLoad && la) html += `<br>Load ${la[1].toFixed(1)} kW actual`;
      } else return;
      tip.innerHTML = html; tip.style.display = 'block';
      tip.style.left = Math.min(Math.max(0, px - tip.offsetWidth / 2), r.width - tip.offsetWidth) + 'px';
      tip.style.top = '-6px';
      const sx = px / r.width * W;
      [cur, cur2].forEach(l => { if (l) { l.setAttribute('x1', sx); l.setAttribute('x2', sx); l.style.display = ''; } });
    };
    const leave = () => { tip.style.display = 'none'; [cur, cur2].forEach(l => { if (l) l.style.display = 'none'; }); };
    const det = this.shadowRoot.querySelector('details.model');
    if (det) det.addEventListener('toggle', () => { this._modelOpen = det.open; });
    this.shadowRoot.querySelectorAll('.hit').forEach(hit => {
      hit.addEventListener('mousemove', move); hit.addEventListener('touchmove', move, { passive: true });
      hit.addEventListener('mouseleave', leave); hit.addEventListener('touchend', leave);
    });
  }

  // Collapsed "Weather model" panel: status in words and daily load vs temperature with the fitted line.
  _modelSection(wm) {
    const open = this._modelOpen ? ' open' : '';
    let status;
    if (wm.status === 'active') {
      const parts = [];
      if (wm.heating_kw_per_c) parts.push(`+${wm.heating_kw_per_c.toFixed(3)} kW per °C below ${wm.heating_below_c}°C`);
      if (wm.cooling_kw_per_c) parts.push(`+${wm.cooling_kw_per_c.toFixed(3)} kW per °C above ${wm.cooling_above_c}°C`);
      status = `Active: ${parts.join(', ')} · learned from ${wm.days} days${wm.fit_gain != null ? ` · explains ${Math.round(wm.fit_gain * 100)}% of the hour-to-hour variation` : ''}`;
    } else {
      status = `Not in use yet: ${wm.reason || 'learning'}. The plan uses the plain load schedule.`;
    }
    const pts = wm.daily || [];
    let chart = '';
    if (pts.length > 2) {
      const W = Math.min(600, Math.max(280, (this._w || 600) - 24)), H = 200, L = 40, R = 10, T = 16, B = 26;
      const tmin = Math.floor(Math.min(...pts.map(p => p.t))) - 1, tmax = Math.ceil(Math.max(...pts.map(p => p.t))) + 1;
      const kmax = Math.ceil(Math.max(...pts.map(p => p.kwh)) * 1.1);
      const x = t => L + (t - tmin) / (tmax - tmin) * (W - L - R), y = k => T + (1 - k / kmax) * (H - T - B);
      let line = '';
      if (wm.status === 'active' && wm.line_base_kwh != null) {
        const f = t => wm.line_base_kwh + wm.line_hours * ((wm.heating_kw_per_c || 0) * Math.max(0, (wm.heating_below_c || 0) - t)
          + (wm.cooling_kw_per_c || 0) * Math.max(0, t - (wm.cooling_above_c || 99)));
        const ts = []; for (let t = tmin; t <= tmax; t += 0.25) ts.push(t);
        line = `<path class="fit" d="M${ts.map(t => `${x(t).toFixed(1)},${y(f(t)).toFixed(1)}`).join('L')}"/>`;
      }
      let ax = '';
      for (let t = Math.ceil(tmin); t <= tmax; t += Math.max(1, Math.round((tmax - tmin) / 6))) ax += `<text x="${x(t)}" y="${H - 8}" text-anchor="middle" class="lbl">${t}°</text>`;
      for (const k of [0, Math.round(kmax / 2), kmax]) ax += `<line x1="${L}" x2="${W - R}" y1="${y(k)}" y2="${y(k)}" class="g"/><text x="${L - 6}" y="${y(k) + 4}" text-anchor="end" class="lbl">${k}</text>`;
      chart = `<svg viewBox="0 0 ${W} ${H}" style="max-width:${W}px">${ax}${line}${pts.map(p => `<circle class="pt" cx="${x(p.t)}" cy="${y(p.kwh)}" r="4"><title>${p.d}: ${p.kwh} kWh at ${p.t}°C</title></circle>`).join('')}
        <text x="${L + 4}" y="${T - 4}" class="lbl">kWh/day</text><text x="${W - R}" y="${T - 4}" text-anchor="end" class="lbl">vs average outdoor °C</text></svg>
        <div>Each dot is one day's load from the sensors the model learns from; the line is what it expects at that temperature.</div>`;
    }
    return `<details class="model"${open}><summary>Weather model · ${wm.status === 'active' ? 'active' : 'not in use yet'}</summary><div>${status}</div>${chart}</details>`;
  }
}

if (!customElements.get('battery-plan-card')) customElements.define('battery-plan-card', BatteryPlanCard);
window.customCards = window.customCards || [];
if (!window.customCards.some(c => c.type === 'battery-plan-card')) {
  window.customCards.push({ type: 'battery-plan-card', name: 'Battery Plan', description: 'Battery SOC forecast, charge/export plan and planned costs', preview: true });
}
