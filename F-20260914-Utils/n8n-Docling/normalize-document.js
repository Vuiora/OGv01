/* Business bridge: reuse the original structure guards without a second LLM. */
const fs = require('node:fs');
const core = require('./workflow-core.js');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const text = String(input.text);
const {masked, kept} = core.protect(text);
core.checkTokens(masked, masked);
if (core.restore(masked, kept) !== text) throw new Error('Document structure round trip failed');
const segments = core.splitDocument(masked, 7000, 'chapter');
process.stdout.write(JSON.stringify({version:1, protected_count:Object.keys(kept).length,
  segment_count:segments.length, round_trip_passed:true}));

