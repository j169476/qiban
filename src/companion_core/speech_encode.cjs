// Private local synthesis conversion; no target accounts or sending commands.
'use strict';
const codec = require(process.argv[2]);
const maxMs = Number(process.argv[3]);
if (!Number.isInteger(maxMs) || maxMs < 1 || maxMs > 600000) process.exit(2);
let size = 0; const chunks = [];
process.stdin.on('data', chunk => {
  size += chunk.length;
  if (size > maxMs * 48 + 1024) process.exit(2);
  chunks.push(chunk);
});
process.stdin.on('end', async () => {
  try {
    const input = Buffer.concat(chunks);
    if (!codec.isWav(input)) process.exit(2);
    const output = await codec.encode(input, 0);
    if (output.duration > maxMs || codec.getDuration(output.data) > maxMs) process.exit(3);
    if (!output.data.length || output.data.length > 1024 * 1024) process.exit(2);
    process.stdout.write(JSON.stringify({audio: Buffer.from(output.data).toString('base64'),
      actual_audio_ms: codec.getDuration(output.data)}));
  } catch (_) { process.exitCode = 2; }
});
