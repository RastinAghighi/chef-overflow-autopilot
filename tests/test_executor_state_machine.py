import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_js_executor_suppresses_duplicate_active_command():
    code = r"""
const { Executor } = require('./agents/executor.js');
const events = [];
const recorder = { record(type, tick, ms, data) { events.push({type, data}); } };
const reservations = { reserve(req) { return { id: req.kind + ':' + req.resource_id }; }, release() {} };
let state = {
  time: 1,
  chefs: [{ id: 0, pos: [1, 1], holding: null, busy: false, hasPath: true, stall: 0 }]
};
const api = {
  getState() { return state; },
  command() { return { success: true }; }
};
const ex = new Executor(api, recorder, reservations, { blockedTicks: 2 });
const first = ex.assign(0, 'bin_3', { kind: 'test' });
const second = ex.assign(0, 'bin_3', { kind: 'test' });
process.stdout.write(JSON.stringify({ first, second, events }));
"""
    proc = subprocess.run(
        ["node", "-e", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    assert data["first"]["success"] is True
    assert data["second"]["success"] is False
    assert data["second"]["error"] == "Duplicate active target suppressed"
    assert any(e["type"] == "executor_decision" and e["data"]["kind"] == "duplicate_suppressed" for e in data["events"])

