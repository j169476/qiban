// Private bounded SILK-to-mono PCM pipe. Never accepts media paths or URLs.
'use strict';
const codec = require(process.argv[2]);
const maxMs = Number(process.argv[3]);
if (!Number.isInteger(maxMs) || maxMs < 1 || maxMs > 600000) process.exit(2);
const chunks = []; let size = 0;
process.stdin.on('data', chunk => {
  size += chunk.length;
  if (size > 10 * 1024 * 1024) process.exit(2);
  chunks.push(chunk);
});
process.stdin.on('end', async () => {
  try {
    const data = Buffer.concat(chunks);
    if (!codec.isSilk(data)) process.exit(2);
    if (codec.getDuration(data) > maxMs) process.exit(3);
    const result = await codec.decode(data, 16000);
    if (!result.data.length || result.data.length % 2) process.exit(2);
    if (result.data.length > maxMs * 32) process.exit(3);
    process.stdout.write(result.data);
  } catch (_) { process.exitCode = 2; }
});
