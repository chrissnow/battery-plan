// node run_js.js scenario.json -> prints attrs JSON from the Node-RED planner
const fs = require('fs');
const sc = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const code = fs.readFileSync(__dirname + '/planner.js', 'utf8');
const res = [];
sc.export_gbp.forEach((v, i) => { const s = Date.parse(sc.rates_start) + i * 1800000;
  res.push({ valid_from: new Date(s).toISOString().replace('.000', ''), valid_to: new Date(s + 1800000).toISOString().replace('.000', ''), value_inc_vat: v * 100 }); });
const flow = { CurrentSoC: sc.soc, MinScheduledSoC: sc.reserve, ESSMinSoC: sc.min_soc, ScheduleTargetSoC: sc.target, maxexportkW: sc.max_export_kw,
  maxsavingexportkW: sc.saving_kw, minexportrate: sc.min_price, inverterefficiency: 0.9, StartSecs: sc.start_secs, ChargeSlotDurSec: sc.dur_secs,
  agileexportraw: { results: res.reverse() }, IOGDispatches: JSON.stringify(sc.dispatches || []) };
if (sc.saving) Object.assign(flow, { SavingSessionUpcoming: 'SavingSessionUpcoming', SavingSessionStart: sc.saving[0], SavingSessionEnd: sc.saving[1] });
const glob = { BattCapAh: 1206, MaxChargeA: sc.max_a, MinChargeA: 10, TypicalHrUsageArray: sc.load_w,
  ChargeSpread: { finishEarlyMin: 15, finishEarlyBalanceMin: 30, makeUpCurrent: 5 } };
const mk = o => ({ get: k => o[k], set: (k, v) => o[k] = v });
const RealDate = Date, NOW = RealDate.parse(sc.now);
global.Date = class extends RealDate { constructor(...a) { super(...(a.length ? a : [NOW])) } static now() { return NOW } };
const out = new Function('flow', 'global', 'node', code)(mk(flow), mk(glob), { status() {} });
process.stdout.write(out[0][2].payload);
