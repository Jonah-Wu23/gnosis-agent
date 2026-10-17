const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require(require.resolve('typescript-legacy', { paths: [path.join(process.cwd(), 'host')] }));

const folder = path.join(process.cwd(), 'experiments', 'negative_result_offline', 'completion');
const material = JSON.parse(fs.readFileSync(path.join(folder, 'guard_replay_inputs.json'), 'utf8'));
const compiled = new Map();
const results = [];
for (const item of material.events) {
  if (!compiled.has(item.source_sha256)) {
    const source = material.sources[item.source_sha256];
    const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
    compiled.set(item.source_sha256, javascript);
  }
  const context = vm.createContext({ exports: {}, ctx: item.context });
  vm.runInContext(compiled.get(item.source_sha256), context, { timeout: 1000 });
  const output = vm.runInContext('exports.onBoundary(ctx)', context, { timeout: 1000 });
  const taskString = vm.runInContext('String(ctx.task)', context, { timeout: 1000 });
  results.push({
    execution_id: item.execution_id,
    hook: item.context.event.hook,
    source_sha256: item.source_sha256,
    task_value_type: typeof item.context.task,
    task_string: taskString,
    output,
    captured_output: item.captured_output,
    argument_rewrite: Object.hasOwn(output, 'arguments'),
    emitted_calls: output.calls?.length || 0,
    captured_effects: item.captured_effects,
    reference: item.reference,
  });
}
const summary = {
  events: results.length,
  source_variants: compiled.size,
  argument_rewrites: results.filter(item => item.argument_rewrite).length,
  emitted_calls: results.reduce((total, item) => total + item.emitted_calls, 0),
  task_objects_stringified_as_object: results.filter(item => item.hook === 'task_start' && item.task_string === '[object Object]').length,
};
fs.writeFileSync(path.join(folder, 'guard_protocol_replay.json'), JSON.stringify({ summary, results }, null, 2));
process.stdout.write(JSON.stringify(summary) + '\n');
