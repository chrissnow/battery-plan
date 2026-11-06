// ESS Plan builder — mirrors the "Agile Sessions" export logic and simulates SOC.
// Paste on the "GridSetpoint Control" tab: it reads that tab's flow context.
// Publishes sensor.ess_plan to Home Assistant via MQTT discovery.

const HORIZON_SLOTS = 72;          // half-hours ahead (~36 h)
const NOMINAL_V = 51.2;
const INCLUDE_HEATPUMP_ARRAY = false; // true = add global HeatPumpUsageArray to load
const TOPIC = 'HomeAssistant/Victron/ESSPlan';
const FINISH_EARLY_LONG_MIN = 60;   // Charge Spread "Long Balance Charge" (Sat 23:30 window) — only exists on Saturday nights
const CHARGE_EFF = 0.93;            // grid AC -> battery DC when charging (for cost estimate)

const num = (v, d) => { const n = parseFloat(v); return isNaN(n) ? d : n; };

// From the Cerbo (victron-input-ess nodes in this group)
const ESS_MIN_SOC       = Math.min(num(flow.get('ESSMinSoC'), 5), num(flow.get('MinScheduledSoC'), 20)); // ESS minimum SoC limit (never above the export floor)
const CHARGE_TARGET_SOC = num(flow.get('ScheduleTargetSoC'), 100); // charge schedule SoC
// From the Charge Spread tab (shared to global by "Share Charge Spread settings")
const cs = global.get('ChargeSpread') || {};
const FINISH_EARLY_MIN         = num(cs.finishEarlyMin, 15);
const FINISH_EARLY_BALANCE_MIN = num(cs.finishEarlyBalanceMin, 30);
const MAKEUP_A                 = num(cs.makeUpCurrent, 5);
const first = (...v) => v.find(x => x !== undefined && x !== null);
const round = (v, dp) => Math.round(v * 10 ** dp) / 10 ** dp;

// ---------- inputs from existing flow/global context ----------
const soc0 = num(flow.get('CurrentSoC'), null);
if (soc0 === null) { node.status({ fill: 'red', shape: 'ring', text: 'waiting for CurrentSoC' }); return null; }

const capAh      = num(first(global.get('BattCapAh'), flow.get('BattCapAh')), 1206);
const capKwh     = capAh * NOMINAL_V / 1000;
const eff        = num(flow.get('inverterefficiency'), 0.9);
const reserveSoc = num(flow.get('MinScheduledSoC'), 20);
const maxExpKw   = num(flow.get('maxexportkW'), 11);
const maxSavKw   = num(flow.get('maxsavingexportkW'), 12);
const freeUpcoming = flow.get('FreeSessionUpcoming') === 'FreeSessionUpcoming';
const minRate    = num(freeUpcoming ? first(flow.get('minexportratebeforefree'), flow.get('minexportrate'))
                                    : flow.get('minexportrate'), 12.5);   // p/kWh
const maxChargeA = num(global.get('MaxChargeA'), 344);
const minChargeA = num(global.get('MinChargeA'), 10);
const chargeKw   = maxChargeA * NOMINAL_V / 1000;
// IOG dispatches published by HA on HomeAssistant/Victron/IOGDispatches
const iogRaw     = flow.get('IOGDispatches');
const iogList    = (typeof iogRaw === 'string' ? (() => { try { return JSON.parse(iogRaw); } catch (e) { return []; } })() : iogRaw) || [];
const iog        = (Array.isArray(iogList) ? iogList : []).map(d => [Date.parse(d.start), Date.parse(d.end)]).filter(d => !isNaN(d[0]) && !isNaN(d[1]));
// Charge window: schedule 2 (every day) as used by Charge Spread; falls back to this tab's schedule 1 values
const chgStart   = num(first(cs.startSecs, flow.get('StartSecs')), 84600);          // secs after local midnight
const chgDur     = num(first(cs.chargeSlotDurSec, flow.get('ChargeSlotDurSec')), 21600);
const usageArr   = global.get('TypicalHrUsageArray') || Array(24).fill(800);
const hpArr      = INCLUDE_HEATPUMP_ARRAY ? (global.get('HeatPumpUsageArray') || []) : [];

// Agile export rates (Octopus API format, pence)
const raw = (freeUpcoming && flow.get('agileexportprefree')) || flow.get('agileexportraw');
const rates = ((raw && raw.results) || []).map(r => ({
    s: Date.parse(r.valid_from), e: Date.parse(r.valid_to), v: r.value_inc_vat
}));
const rateAt = t => { const r = rates.find(x => t >= x.s && t < x.e); return r ? r.v : null; };

// Session windows: seconds-after-local-midnight (next occurrence) or a date string
function sessionWindow(startVal, endVal, now) {
    const toMs = v => {
        if (v === undefined || v === null || v === 'NA' || v === '') return null;
        const n = Number(v);
        if (!isNaN(n) && n <= 2 * 86400) {
            const d = new Date(now); d.setHours(0, 0, 0, 0);
            return d.getTime() + n * 1000;
        }
        const p = Date.parse(v);
        return isNaN(p) ? null : p;
    };
    let s = toMs(startVal), e = toMs(endVal);
    if (s === null || e === null) return null;
    if (e <= s) e += 86400000;
    if (e <= now) { s += 86400000; e += 86400000; }
    return [s, e];
}

const now = Date.now();
const freeWin = (freeUpcoming || flow.get('FreeSessionActive') === 'FreeSessionActive')
    ? sessionWindow(flow.get('FreeSessionStart'), flow.get('FreeSessionEnd'), now) : null;
const savWin = flow.get('SavingSessionUpcoming') === 'SavingSessionUpcoming'
    ? sessionWindow(flow.get('SavingSessionStart'), flow.get('SavingSessionEnd'), now) : null;

// ---------- slot grid ----------
const HALF = 1800000;
const start0 = Math.floor(now / HALF) * HALF;
const slots = [];
for (let i = 0; i < HORIZON_SLOTS; i++) {
    const s = start0 + i * HALF, e = s + HALF;
    const from = i === 0 ? now : s;
    const d = new Date(s);
    const sod = d.getHours() * 3600 + d.getMinutes() * 60;
    const inSchedule = ((sod - chgStart + 86400) % 86400) < chgDur;
    let finishBy = null;
    if (inSchedule) {
        const mid = new Date(s); mid.setHours(0, 0, 0, 0);
        let bs = mid.getTime() + chgStart * 1000;
        if (bs > s) bs -= 86400000;
        const early = CHARGE_TARGET_SOC >= 100
            ? (new Date(bs).getDay() === 6 ? FINISH_EARLY_LONG_MIN : FINISH_EARLY_BALANCE_MIN)
            : FINISH_EARLY_MIN;
        finishBy = bs + chgDur * 1000 - early * 60000;
    }
    const inFree = !!freeWin && s < freeWin[1] && e > freeWin[0];
    const inSaving = !!savWin && s < savWin[1] && e > savWin[0];
    // IOG dispatch: fraction of this slot covered (outside the normal schedule)
    const iogH = inSchedule ? 0 : iog.reduce((a, w) => a + Math.max(0, Math.min(e, w[1]) - Math.max(from, w[0])), 0) / 3600000;
    const loadW = num(usageArr[d.getHours()], 800) + num(hpArr[d.getHours()], 0);
    slots.push({
        s, e, from, h: (e - from) / 3600000,
        charge: inSchedule || inFree, sched: inSchedule, free: inFree, saving: inSaving,
        iogH, finishBy, loadKw: loadW / 1000, rate: rateAt(s)
    });
}

// ---------- simulation ----------
function simulate(exp) {
    let kwh = soc0 / 100 * capKwh;
    const rows = [];
    const resKwh = reserveSoc / 100 * capKwh, minKwh = ESS_MIN_SOC / 100 * capKwh;
    const tgt = CHARGE_TARGET_SOC / 100 * capKwh;
    let spreadA = num(global.get('SpreadMaxA'), null);
    for (let i = 0; i < slots.length; i++) {
        const sl = slots[i];
        let batt, mode, expKw = 0, impKw = 0;
        if (sl.charge) {
            let amps = maxChargeA;
            if (sl.sched && !sl.free) {
                // Charge Spread: Ah still needed / hours left to finishBy + make-up, clamped.
                // Not recalculated once within 5% of target (keeps last value).
                const socPct = kwh / capKwh * 100;
                if (socPct + 5 <= CHARGE_TARGET_SOC || spreadA === null) {
                    const hrs = (sl.finishBy - sl.from) / 3600000;
                    const needAh = (CHARGE_TARGET_SOC - socPct) / 100 * capAh;
                    spreadA = hrs <= 0.1 ? maxChargeA : needAh / hrs + MAKEUP_A;
                }
                amps = Math.min(maxChargeA, Math.max(minChargeA, spreadA));
            }
            const kw = amps * NOMINAL_V / 1000;
            batt = kwh < tgt ? Math.min(kw, (tgt - kwh) / sl.h) : 0;
            mode = batt > 0 ? 'charge' : 'hold';
            impKw = batt / CHARGE_EFF + sl.loadKw;          // house on grid during scheduled charge
        } else if (sl.iogH > 0) {
            // IOG dispatch outside the schedule: grid-charges at full rate, house on grid
            const avail = Math.max(0, tgt - kwh);
            batt = Math.min(chargeKw * sl.iogH, avail) / sl.h;
            const restH = sl.h - sl.iogH;
            impKw = batt / CHARGE_EFF + sl.loadKw * sl.iogH / sl.h;
            if (restH > 0) batt -= sl.loadKw / eff * restH / sl.h;
            mode = kwh < tgt - 0.05 ? 'iog' : 'hold';
        } else {
            const loadB = sl.loadKw / eff;
            const want = exp[i] || 0;
            if (want > 0) {
                // export stops at MinScheduledSoC (Discharge Floor), load continues
                const roomKw = Math.max(0, (kwh - resKwh) / sl.h);
                const expB = Math.min(want / eff, Math.max(0, roomKw - loadB));
                expKw = expB * eff;
                batt = -(loadB + expB);
            } else {
                batt = -loadB;
            }
            mode = expKw > 0.05 ? (sl.saving ? 'saving' : 'export') : 'self';
            if (kwh + batt * sl.h < minKwh) {
                batt = Math.min(0, (minKwh - kwh) / sl.h); mode = 'grid'; expKw = 0;
                impKw = Math.max(0, sl.loadKw + batt * eff);   // battery covers what it can
            }
        }
        kwh += batt * sl.h;
        rows.push({ kwh, batt, mode, expKw, impKw });
    }
    return rows;
}

// ---------- export allocation (same idea as Agile Sessions) ----------
// Before each charge window: keep enough for the load + reserve, spend the rest
// exporting at max rate in the best-priced slots at or above minexportrate.
const exp = new Array(slots.length).fill(0);
slots.forEach((sl, i) => { if (sl.saving && !sl.charge) exp[i] = maxSavKw; });

const reservePts = [];
slots.forEach((sl, i) => { if (sl.charge && (i === 0 || !slots[i - 1].charge)) reservePts.push(i); });

let segStart = 0;
for (const r of reservePts) {
    if (r === 0) { segStart = 1; continue; }
    let rows = simulate(exp);
    let resSoc = reserveSoc;
    if (slots[r].free && freeWin) {
        const freeH = (freeWin[1] - freeWin[0]) / 3600000;
        resSoc = Math.max(reserveSoc, 100 - freeH * chargeKw / capKwh * 100);
    }
    let surplus = rows[r - 1].kwh - resSoc / 100 * capKwh;     // battery kWh to spare
    const cands = [];
    for (let j = segStart; j < r; j++) {
        const sl = slots[j];
        if (sl.charge || sl.saving || sl.iogH > 0) continue;
        if (sl.rate !== null && sl.rate >= minRate) cands.push(j);
    }
    cands.sort((a, b) => slots[b].rate - slots[a].rate);
    for (const j of cands) {
        if (surplus <= 0.05) break;
        const kw = Math.min(maxExpKw, surplus * eff / slots[j].h);
        exp[j] = kw;
        surplus -= kw * slots[j].h / eff;
    }
    segStart = r;
    // find end of this charge window for next segment
    while (segStart < slots.length && slots[segStart].charge) segStart++;
}

const rows = simulate(exp);

// ---------- outputs ----------
const iso = t => new Date(t).toISOString();
const forecast = rows.map((r, i) => ({
    t: iso(slots[i].e),
    soc: round(r.kwh / capKwh * 100, 1),
    kw: round(r.batt, 2),
    exp: round(r.expKw, 2),
    ik: round(r.impKw * slots[i].h, 3),     // grid import kWh in slot
    ek: round(r.expKw * slots[i].h, 3),     // grid export kWh in slot
    mode: r.mode,
    rate: slots[i].rate === null ? null : round(slots[i].rate, 2)
}));

const plan = [];
rows.forEach((r, i) => {
    if (r.mode === 'self') return;
    const last = plan[plan.length - 1];
    if (last && last.mode === r.mode && last._i === i - 1) {
        last.end = iso(slots[i].e); last._i = i; last.kwh += Math.abs(r.batt) * slots[i].h;
        last.end_soc = round(r.kwh / capKwh * 100, 1);
    } else {
        plan.push({ mode: r.mode, start: iso(i === 0 ? now : slots[i].s), end: iso(slots[i].e), _i: i,
                    kwh: Math.abs(r.batt) * slots[i].h, end_soc: round(r.kwh / capKwh * 100, 1) });
    }
});
plan.forEach(p => { delete p._i; p.kwh = round(p.kwh, 1); });

const firstRes = reservePts.find(r => r > 0);
const socAtCharge = firstRes !== undefined ? forecast[firstRes - 1].soc : forecast[forecast.length - 1].soc;
const next = plan[0] || null;
const exportKwh = rows.reduce((a, r, i) => a + r.expKw * slots[i].h, 0);
const minSocAhead = Math.min(...forecast.map(f => f.soc));

const attrs = {
    soc_now: soc0,
    soc_at_next_charge: socAtCharge,
    min_soc_ahead: minSocAhead,
    reserve_soc: reserveSoc,
    capacity_kwh: round(capKwh, 1),
    charge_kw: round(chargeKw, 2),
    max_export_kw: maxExpKw,
    min_export_rate: minRate,
    export_kwh_planned: round(exportKwh, 1),
    next_action: next ? next.mode : 'self',
    next_action_start: next ? next.start : null,
    next_action_end: next ? next.end : null,
    iog_dispatches: iog.map(w => [iso(w[0]), iso(w[1])]),
    free_session: freeWin ? [iso(freeWin[0]), iso(freeWin[1])] : null,
    saving_session: savWin ? [iso(savWin[0]), iso(savWin[1])] : null,
    rates_until: rates.length ? iso(Math.max(...rates.map(r => r.e))) : null,
    plan,
    forecast,
    generated_at: iso(now)
};

const discovery = {
    topic: 'homeassistant/sensor/nr_ess_plan/config',
    retain: true,
    payload: JSON.stringify({
        name: 'ESS Plan',
        unique_id: 'nr_ess_plan',
        default_entity_id: 'sensor.ess_plan',
        state_topic: TOPIC + '/state',
        json_attributes_topic: TOPIC + '/attributes',
        unit_of_measurement: '%',
        device_class: 'battery',
        state_class: 'measurement',
        icon: 'mdi:battery-clock',
        expire_after: 1800,
        device: { identifiers: ['nr_ess_planner'], name: 'Node-RED ESS Planner', manufacturer: 'Node-RED' }
    })
};
const state = { topic: TOPIC + '/state', retain: true, payload: String(socAtCharge) };
const attributes = { topic: TOPIC + '/attributes', retain: true, payload: JSON.stringify(attrs) };

node.status({ fill: 'green', shape: 'dot',
    text: `next ${attrs.next_action} ${next ? new Date(next.start).toTimeString().slice(0, 5) : ''} | export ${attrs.export_kwh_planned} kWh | ${socAtCharge}% at charge` });
return [[discovery, state, attributes]];
